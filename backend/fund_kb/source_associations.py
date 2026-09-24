"""Reading-only source closure. Does not change chunk IDs, outlines or indexes.

Only explicit, uniquely located references are resolved. Natural paragraphs are
not invented from PDF lines. Callers must supply one currently authorized and
hash-checked source version, including its unchanged v3 outline.
"""
from __future__ import annotations

import re

from .source_sections import (
    _NUMBER,
    _REF_NUMBERS,
    _checked_blocks,
    _number,
    _Outline,
    _page,
    _read_references,
    _reference_targets,
    _reference_text,
)

_PART = rf"第\s*[（(]?\s*{_NUMBER}\s*[）)]?\s*[章节条款项]"
_PATH = rf"{_PART}(?:\s*{_PART})*"
_ATTACHMENT_LABEL = rf"附[件录表]\s*(?:{_NUMBER}|[A-Za-z][A-Za-z0-9]*)"
_ATTACHMENT_ID = re.compile(rf"(?P<kind>附[件录表])\s*(?P<number>{_NUMBER}|[A-Za-z][A-Za-z0-9]*)")
_ATTACHMENT_HEADING = re.compile(_ATTACHMENT_ID.pattern + r"(?=$|[\s：:、.．（(])")
# Preserve a qualified external enumeration as ONE foreign locator. Unsupported
# lists/ranges remain unresolved at locate_path; never reinterpret their suffix
# as a local reference. Parenthetical title qualifiers are part of identity, not
# permission to substitute a different edition with the same base title.
_EXTERNAL_PART = rf"第\s*[（(]?\s*{_REF_NUMBERS}\s*[）)]?\s*[章节条款项]"
_EXTERNAL_PATH = rf"{_EXTERNAL_PART}(?:\s*(?:[、,，及和与]|至|到)?\s*{_EXTERNAL_PART})*"
_NOTE_LABEL = rf"(?:脚注\s*{_NUMBER}|\[\^[^\]\s]+\])"
_EXTERNAL_LOCATOR = rf"(?:{_ATTACHMENT_LABEL}(?:\s*{_EXTERNAL_PATH})?|{_EXTERNAL_PATH}|{_NOTE_LABEL})"
_EXTERNAL = re.compile(rf"《(?P<title>[^《》\n]+)》"
    rf"(?P<qualifier>(?:[ \t]*[（(][^（）()\n]+[）)])*)\s*(?P<path>{_EXTERNAL_LOCATOR})?")
_COMPOUND = re.compile(rf"{_PART}(?:\s*{_PART})+")
_RELATIVE = re.compile(r"(?P<direction>前|后|上一|下一)(?P<unit>[章节条款项])")
_ATTACHMENT = re.compile(rf"(?:参见|参照|按照|依据|依照|见|按)\s*(?P<label>{_ATTACHMENT_LABEL})"
                         rf"(?P<path>\s*{_PATH})?")
_PARTS = re.compile(rf"第\s*[（(]?\s*(?P<number>{_NUMBER})\s*[）)]?\s*(?P<unit>[章节条款项])")
_FOOTNOTE = re.compile(rf"脚注\s*(?P<number>{_NUMBER})|\[\^(?P<label>[^\]\s]+)\](?!\s*[:：])")
_MARKDOWN_NOTE = re.compile(r"^\s*\[\^(?P<label>[^\]\s]+)\]:", re.MULTILINE)
_TYPED_NOTE = re.compile(rf"^\s*(?:脚注\s*)?(?P<number>{_NUMBER})(?=\s|[.、．:：])")


def _attachment_key(match):
    number = _number(match["number"])
    return match["kind"], number if number is not None else match["number"]


def _locate_path(tree, path, *, owner=None):
    parent = None
    attachment = _ATTACHMENT_ID.match(path.strip())
    if attachment:
        path = path.strip()[attachment.end():].strip()
        if path and not _PARTS.match(path):
            return {"status": "unresolved", "target_section_ids": []}
        candidates = [i for i, section in enumerate(tree.sections)
                      if (heading := _ATTACHMENT_HEADING.match(section["title"]))
                      and _attachment_key(heading) == _attachment_key(attachment)]
        if len(candidates) != 1:
            return {"status": "ambiguous" if candidates else "unresolved", "target_section_ids": []}
        parent = candidates[0]
        if not path:
            return {"status": "resolved", "target_section_ids": [tree.sections[parent]["section_id"]]}

    parts = list(_PARTS.finditer(path))
    if not parts or re.sub(_PARTS, "", path).strip():
        return {"status": "unresolved", "target_section_ids": []}
    for part in parts:
        number, unit = _number(part["number"]), part["unit"]
        if unit == "款":
            return {"status": "broader_context" if parent is not None else "unresolved",
                    "target_section_ids": [tree.sections[parent]["section_id"]] if parent is not None else [],
                    "reason": "natural_paragraph_not_structurally_numbered"}
        families = {"章": {2}, "节": {3}, "条": {4}, "项": {5, 6, 7, 8}}[unit]
        if parent is None and owner is not None:
            status, _, candidates = _reference_targets(tree, owner, [number], unit, None)
            if status != "resolved":
                return {"status": status, "target_section_ids": []}
        else:
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
    return {"status": "resolved", "target_section_ids": [tree.sections[parent]["section_id"]]}


def locate_path(outline, path):
    """Unique incoming structural path, or an enclosing article with a gap.

    A named attachment/table must have an exact, unique real heading. 款 is an
    unnumbered legal paragraph, not automatically a numbered 项. Local compound
    references additionally use their source's numbering scope via _locate_path.
    """
    return _locate_path(_Outline(outline), path)


def _footnote_definitions(blocks):
    definitions = {}
    for bid, block in blocks.items():
        field, text = _reference_text(block)
        matches = [(("markdown", m["label"]), m) for m in _MARKDOWN_NOTE.finditer(text)]
        if block.get("block_type") == "footnote" or (block.get("locator") or {}).get("role") == "footnote":
            match = _TYPED_NOTE.match(text)
            if match:
                matches.append((("number", _number(match["number"])), match))
        for key, match in matches:
            definitions.setdefault(key, []).append({"block_id": bid, "page": _page(block),
                "text_field": field, "start": match.start(), "end": match.end()})
    return definitions


def _note_key(match):
    return ("number", _number(match["number"])) if match["number"] else ("markdown", match["label"])


def _locate_footnote(tree, footnotes, match, *, page=None):
    key = _note_key(match)
    candidates = footnotes.get(key, [])
    if key[0] == "number" and len(candidates) > 1 and page is not None:
        local = [d for d in candidates if d["page"] == page]
        if local:
            candidates = local
    target = candidates[0]["block_id"] if len(candidates) == 1 else None
    return {"status": "resolved" if target else "ambiguous" if candidates else "unresolved",
            "target_section_ids": [tree.sections[tree.owners[target]]["section_id"]] if target else [],
            "target_block_ids": [target] if target else []}


def _enhanced_references(tree, index, blocks, footnotes):
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
    heading_starts = [left for left, _, bid, _ in locations
                      if tree.sections[tree.owners[bid]]["block_ids"][0] == bid
                      and tree.sections[tree.owners[bid]]["boundary"] in {
                          "heading", "chinese_part", "chinese_chapter", "chinese_section", "chinese_article",
                          "chinese_item", "chinese_subitem", "arabic_item", "arabic_subitem"}]
    enhanced, occupied = [], []
    for pattern in (_EXTERNAL, _ATTACHMENT, _COMPOUND, _RELATIVE, _FOOTNOTE):
        for match in pattern.finditer(text):
            # Wrapping inside source prose is allowed; crossing the next real
            # heading is not. This applies to every part, not only the first.
            boundary = next((left for left in heading_starts if match.start() < left < match.end()), None)
            if boundary is not None:
                match = pattern.match(text, match.start(), boundary)
                if match is None:
                    continue
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            start, end = match.span()
            external_path = match["path"] if pattern is _EXTERNAL else None
            if pattern is _EXTERNAL and not external_path:
                end = match.end("qualifier")
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
                    and tree.sections[owner]["title"].strip() == text[start:end].strip()):
                continue  # A source's own title is not a dependency on itself.
            ref = {"text": text[start:end], "source_section_id": tree.sections[owner]["section_id"],
                   "source_spans": spans, "target_section_ids": [], "status": "unresolved"}
            if pattern is _EXTERNAL:
                ref.update(status="external", target_title=match["title"] + match["qualifier"].strip(),
                           locator=(external_path or "").strip())
            elif pattern is _COMPOUND:
                ref.update(_locate_path(tree, match[0], owner=owner))
            elif pattern is _ATTACHMENT:
                ref.update(_locate_path(tree, match["label"] + (match["path"] or "")))
            elif pattern is _FOOTNOTE:
                candidates = footnotes.get(_note_key(match), [])
                first = spans[0]
                if any(d["block_id"] == first["block_id"] and d["text_field"] == first["text_field"]
                       and d["start"] <= first["start"] < first["end"] <= d["end"] for d in candidates):
                    continue  # A typed definition marker is not a self-reference.
                ref.update(_locate_footnote(tree, footnotes, match, page=_page(blocks[first["block_id"]])))
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
    """Close explicit dependencies over one caller-verified source version.

    Numbered sibling groups apply to seeds AND discovered targets until stable,
    stopping before chapter/section parents. Exact locator receipts retain the
    original target; structural_additions describes the wider reading envelope.
    Explicit section reads can disable all sibling promotion. Footnotes require
    an explicit marker and a typed numeric or Markdown definition; ordinary
    numbered prose is not a definition. Ambiguities remain visible gaps.
    """
    tree = _Outline(outline)
    checked = _checked_blocks(tree, blocks)
    footnotes = _footnote_definitions(checked)
    seeds = tree.selected(selected)
    incoming = {path: _locate_footnote(tree, footnotes, note) if (note := _FOOTNOTE.fullmatch(path.strip()))
                else _locate_path(tree, path) for path in dict.fromkeys(locators)}
    seeds += [tree.by_id[sid] for result in incoming.values() for sid in result["target_section_ids"]]
    additions, grouped = [], set()

    def complete_group(index):
        while structural_groups:
            parent = tree.parents[index]
            if parent is None or tree.markers[parent][0] not in {4, 5, 6, 7, 8}:
                break
            siblings = [i for i in range(len(outline)) if tree.parents[i] == parent
                        and tree.markers[i][0] in {5, 6, 7, 8}]
            if index not in siblings or len(siblings) < 2:
                break
            if index not in grouped:
                grouped.add(index)
                additions.append({"from_section_id": outline[index]["section_id"],
                    "to_section_id": outline[parent]["section_id"], "reason": "numbered_sibling_group"})
            index = parent
        return index

    seeds = [complete_group(index) for index in seeds]
    pending, visited, references, inspected_leads = list(tree.minimal_union(seeds)), set(), {}, set()
    while pending:
        index = pending.pop()
        if index in visited:
            continue
        visited.add(index)
        found = _enhanced_references(tree, index, checked, footnotes)
        for parent in list(tree.ancestors(index))[1:]:
            if parent in inspected_leads:
                continue
            inspected_leads.add(parent)
            children = [i for i, p in enumerate(tree.parents) if p == parent]
            first_child = min((tree.ranges[i][0] for i in children), default=tree.ranges[parent][1])
            leading_ids = set(tree.ordered_ids[tree.ranges[parent][0]:first_child])
            found.extend(r for r in _enhanced_references(tree, parent, checked, footnotes)
                         if all(s["block_id"] in leading_ids for s in r["source_spans"]))
        for ref in found:
            key = tuple((s["block_id"], s["start"], s["end"]) for s in ref["source_spans"])
            references[key] = ref
            for sid in ref["target_section_ids"]:
                target = complete_group(tree.by_id[sid])
                if target not in visited:
                    pending.append(target)
    return {"sections": [outline[i] for i in tree.minimal_union(visited)],
            "references": sorted(references.values(), key=lambda r: (r["source_spans"][0]["ordinal"], r["source_spans"][0]["start"])),
            "incoming": incoming, "structural_additions": additions}
