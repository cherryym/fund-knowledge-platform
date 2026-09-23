"""Reading-only source closure. Does not change chunk IDs, outlines or indexes.

Only explicit, uniquely located references are resolved. Natural paragraphs are
not invented from PDF lines. Callers must supply one currently authorized and
hash-checked source version, including its unchanged v3 outline.
"""
from __future__ import annotations

import re

from .source_sections import (_NUMBER, _Outline, _checked_blocks, _number,
                              _read_references, _reference_text)

_PART = rf"第\s*[（(]?\s*{_NUMBER}\s*[）)]?\s*[章节条款项]"
_PATH = rf"{_PART}(?:\s*{_PART})*"
_EXTERNAL = re.compile(rf"《(?P<title>[^《》\n]+)》\s*(?P<path>{_PATH})?")
_COMPOUND = re.compile(rf"{_PART}(?:\s*{_PART})+")
_RELATIVE = re.compile(r"(?P<direction>前|后|上一|下一)(?P<unit>[章节条款项])")
_ATTACHMENT = re.compile(rf"(?:参见|参照|按照|依据|依照|见|按)\s*(?P<label>附[件录]\s*(?:{_NUMBER}|[A-Za-z]))")
_PARTS = re.compile(rf"第\s*[（(]?\s*(?P<number>{_NUMBER})\s*[）)]?\s*(?P<unit>[章节条款项])")


def locate_path(outline, path):
    """Unique structural path, or nearest complete article plus an explicit gap.

    款 is an unnumbered legal paragraph, not automatically a numbered 项. If the
    outline cannot represent it, the enclosing article is useful context ONLY.
    """
    tree = _Outline(outline)
    parts = list(_PARTS.finditer(path))
    if not parts or re.sub(_PARTS, "", path).strip():
        return {"status": "unresolved", "target_section_ids": []}
    parent = None
    for part in parts:
        number, unit = _number(part["number"]), part["unit"]
        if unit == "款":
            return {"status": "broader_context" if parent is not None else "unresolved",
                    "target_section_ids": [outline[parent]["section_id"]] if parent is not None else [],
                    "reason": "natural_paragraph_not_structurally_numbered"}
        families = {"章": {2}, "节": {3}, "条": {4}, "项": {5, 6, 7, 8}}[unit]
        candidates = [i for i, (family, n) in enumerate(tree.markers)
                      if family in families and n == number
                      and (parent is None or i != parent and parent in tree.ancestors(i))]
        # Nested item levels are not interchangeable. Use only the closest
        # numbered child level, never match another event's item by its number.
        if parent is not None and unit == "项":
            candidates = [i for i in candidates if not any(
                a != parent and tree.markers[a][0] in families
                for a in list(tree.ancestors(i))[1:]
                if a != parent and parent in tree.ancestors(a))]
        if len(candidates) != 1:
            return {"status": "ambiguous" if candidates else "unresolved", "target_section_ids": []}
        parent = candidates[0]
    return {"status": "resolved", "target_section_ids": [outline[parent]["section_id"]]}


def _enhanced_references(tree, index, blocks):
    """Preserve exact original character spans even across source block breaks."""
    fragments, locations, offset = [], [], 0
    for bid in tree.sections[index]["block_ids"]:
        field, value = _reference_text(blocks[bid])
        if not value:
            continue
        if fragments:
            offset += 1
        locations.append((offset, offset + len(value), bid, field))
        fragments.append(value)
        offset += len(value)
    text = "\n".join(fragments)
    enhanced, occupied = [], []
    for pattern in (_EXTERNAL, _COMPOUND, _RELATIVE, _ATTACHMENT):
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            start, end = match.span()
            external_path = match["path"] if pattern is _EXTERNAL else None
            if external_path:
                # A following source heading on a new block is not the quoted
                # document's locator (while a wrapped inline citation is).
                for left, right, bid, _ in locations:
                    if left <= match.start("path") < right:
                        owner = tree.owners[bid]
                        part = _PARTS.match(external_path)
                        if (part and tree.sections[owner]["block_ids"][0] == bid
                                and not text[left:match.start("path")].strip()
                                and tree.markers[owner] == ({"章": 2, "节": 3, "条": 4}.get(part["unit"]), _number(part["number"]))):
                            external_path = ""
                            end = match.end("title") + 1
                        break
            spans = []
            for left, right, bid, field in locations:
                first, last = max(start, left), min(end, right)
                if first < last:
                    spans.append({"block_id": bid, "ordinal": blocks[bid]["ordinal"],
                        "content_sha256": blocks[bid].get("content_sha256"), "text_field": field,
                        "start": first - left, "end": last - left,
                        "text_start": first - match.start(), "text_end": last - match.start()})
            if not spans or any(s["block_id"] in tree.toc_blocks for s in spans):
                continue
            owner = tree.owners[spans[0]["block_id"]]
            if (pattern is _EXTERNAL
                    and tree.sections[owner]["block_ids"][0] == spans[0]["block_id"]
                    and tree.sections[owner]["title"].strip() == f"《{match['title']}》"):
                continue  # A source's own title is not a dependency on itself.
            ref = {"text": text[start:end], "source_section_id": tree.sections[owner]["section_id"],
                   "source_spans": spans, "target_section_ids": [], "status": "unresolved"}
            if pattern is _EXTERNAL:
                ref.update(status="external", target_title=match["title"], locator=(external_path or "").strip())
            elif pattern is _COMPOUND:
                ref.update(locate_path(tree.sections, match[0]))
            elif pattern is _ATTACHMENT:
                label = re.sub(r"\s+", "", match["label"])
                candidates = [i for i, section in enumerate(tree.sections)
                    if re.match(re.escape(label) + r"(?:[\s：:、.．]|$)", section["title"])]
                ref.update(status="resolved" if len(candidates) == 1 else "ambiguous" if candidates else "unresolved",
                    target_section_ids=[tree.sections[candidates[0]]["section_id"]] if len(candidates) == 1 else [])
            else:
                families = {"章": {2}, "节": {3}, "条": {4}, "项": {5, 6, 7, 8}, "款": set()}[match["unit"]]
                ancestor = next((i for i in tree.ancestors(owner) if tree.markers[i][0] in families), None)
                if ancestor is not None:
                    family, number = tree.markers[ancestor]
                    step = -1 if match["direction"] in {"前", "上一"} else 1
                    candidates = [i for i, marker in enumerate(tree.markers)
                        if marker == (family, number + step) and tree.parents[i] == tree.parents[ancestor]]
                    ref.update(status="resolved" if len(candidates) == 1 else "ambiguous" if candidates else "unresolved",
                        target_section_ids=[tree.sections[candidates[0]]["section_id"]] if len(candidates) == 1 else [])
            occupied.append((start, end))
            enhanced.append(ref)

    def overlaps(left, right):
        return any(a["block_id"] == b["block_id"] and a["start"] < b["end"] and b["start"] < a["end"]
                   for a in left["source_spans"] for b in right["source_spans"])
    simple = _read_references(tree, [index], blocks)
    return [r for r in simple if not any(overlaps(r, new) for new in enhanced)] + enhanced


def complete_source_context(outline, selected, *, blocks, locators=(), structural_groups=True):
    tree = _Outline(outline)
    checked = _checked_blocks(tree, blocks)
    seeds = tree.selected(selected)
    incoming = {path: locate_path(outline, path) for path in dict.fromkeys(locators)}
    seeds += [tree.by_id[sid] for result in incoming.values() for sid in result["target_section_ids"]]
    additions = []
    if structural_groups:
        for index in list(seeds):
            parent = tree.parents[index]
            if parent is None or tree.markers[parent][0] not in {4, 5, 6, 7, 8}:
                continue
            siblings = [i for i in range(len(outline)) if tree.parents[i] == parent
                        and tree.markers[i][0] in {5, 6, 7, 8}]
            if index in siblings and len(siblings) > 1:
                seeds.append(parent)
                additions.append({"from_section_id": outline[index]["section_id"],
                    "to_section_id": outline[parent]["section_id"], "reason": "numbered_sibling_group"})
    pending, visited, references, inspected_leads = list(tree.minimal_union(seeds)), set(), {}, set()
    while pending:
        index = pending.pop()
        if index in visited:
            continue
        visited.add(index)
        found = _enhanced_references(tree, index, checked)
        for parent in list(tree.ancestors(index))[1:]:
            if parent in inspected_leads:
                continue
            inspected_leads.add(parent)
            children = [i for i, p in enumerate(tree.parents) if p == parent]
            first_child = min((tree.ranges[i][0] for i in children), default=tree.ranges[parent][1])
            leading_ids = set(tree.ordered_ids[tree.ranges[parent][0]:first_child])
            found.extend(r for r in _enhanced_references(tree, parent, checked)
                         if all(s["block_id"] in leading_ids for s in r["source_spans"]))
        for ref in found:
            key = tuple((s["block_id"], s["start"], s["end"]) for s in ref["source_spans"])
            references[key] = ref
            pending.extend(tree.by_id[sid] for sid in ref["target_section_ids"] if tree.by_id[sid] not in visited)
    return {"sections": [outline[i] for i in tree.minimal_union(visited)],
            "references": sorted(references.values(), key=lambda r: (r["source_spans"][0]["ordinal"], r["source_spans"][0]["start"])),
            "incoming": incoming, "structural_additions": additions}
