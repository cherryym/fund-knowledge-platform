"""Lossless semantic embedding inputs for already authorized source records.

Only the supplied token counter and ``source_sections`` are used: no I/O, model,
application state, source normalization, or instruction execution. Offsets are
half-open Python character offsets. The caller owns ACL/hash verification and
must supply an untruncated, deterministic counter including special tokens.

Document order is first appearance of (resource_id, version_id); within each
document it is (ordinal, block_id). Smallest section ownership partitions nested
ranges, including front matter and parent introductions. Structure, sentence
ends and table headers remain heuristics: missing importer metadata, OCR reading
order, abbreviations, and unmarked headings cannot be repaired here. Hard cuts
can divide a grapheme cluster, but never lose a source character.

Overlap is optional and yields to complete paragraphs/sentences and progress.
It repeats a table header when known, otherwise a preceding suffix. Context may
shrink to preserve a whole short section. Nonmonotonic token counts are checked
on actual composed inputs; packing is not guaranteed to be globally maximal.
Impossible budgets raise rather than returning partial/truncated results.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache

from .source_sections import build_document_sections

__all__ = ["build_semantic_units"]

_SENTENCE_END = re.compile(r'''(?:[。！？!?]+|\.(?=\s|["'”’)\]）】》」』]|$))["'”’)\]）】》」』]*[ \t]*''')
_DEPENDENT = re.compile(
    r"^(?:但|但是|不过|然而|否则|除非|除外|其中|并且|并|且|以及|或者|或|及|的|和|与|"
    r"but\b|however\b|unless\b|except\b|provided\b|otherwise\b|and\b|or\b)", re.IGNORECASE
)
_DEPENDENT_AFTER = re.compile(r"\s*" + _DEPENDENT.pattern[1:], re.IGNORECASE)
_NUMBER_ONLY = re.compile(r"^(?:第[\s零〇一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾\d]+[条章节]|"
                          r"[（(]?[零〇一二三四五六七八九十百\d]+[）)、.．])\s*$")
_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x85\u2028\u2029]")
_BLANK_LINE = re.compile(r"(?:\r\n|[\n\r\x85\u2028\u2029])[ \t]*(?:\r\n|[\n\r\x85\u2028\u2029])")
_WORD_END = re.compile(r"\s+")
_MARKDOWN_RULE = re.compile(r"^[\s|:\-]+$")
_JOURNAL_LINE = re.compile(r"^\s*(?:借|贷)(?:方)?\s*[:：]", re.MULTILINE)
_NOTE_LINE = re.compile(r"^\s*\d+\s+[^\W\d_].*[。.!！?？]\s*$", re.UNICODE)


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _last_fit(candidates, fits: Callable[[int], bool], first: int = 0) -> int | None:
    """Exponential/binary probing; return only an actually measured fitting end."""
    if first >= len(candidates) or not fits(candidates[first]):
        return None
    low, high = first, first + 1
    while high < len(candidates) and fits(candidates[high]):
        low, high = high, first + (high - first) * 2 + 1
    high = min(high, len(candidates))
    while low + 1 < high:
        middle = (low + high) // 2
        if fits(candidates[middle]):
            low = middle
        else:
            high = middle
    return candidates[low]


def _compact_title(title: str) -> str:
    # These are context labels only. Source text and returned section_path stay exact.
    title = " ".join(title.split())
    quoted = re.findall(r"《([^《》]+)》", title)
    if len(quoted) == 1 and re.search(r"通知|印发|发布|转发", title):
        return quoted[0]
    return title


def _prefixes(title: str, section: dict, count, context_tokens: int) -> list[str]:
    if context_tokens == 0:
        return [""]
    structural = section["boundary"] in {
        "heading", "chinese_part", "chinese_chapter", "chinese_section", "chinese_article",
        "chinese_item", "chinese_subitem", "arabic_item", "arabic_subitem",
    }
    path = [*section["parent_titles"], section["title"]] if structural else []
    labels = []
    for label in [title, *path]:
        compact = _compact_title(label)
        if compact and compact not in labels:
            labels.append(compact)
    if not labels:
        return [""]

    def pack(items):
        return " / ".join(items) + "\n\n"

    # Keep the closest heading first, then its parents; avoid repeating a notice shell.
    selected, variants = [], []
    for label in reversed(labels):
        candidate = pack([label, *selected])
        if count(candidate) <= context_tokens:
            selected.insert(0, label)
            variants.append(candidate)
    if not variants:
        label = labels[-1]
        end = _last_fit(range(1, len(label) + 1), lambda n: count(pack([label[:n]])) <= context_tokens)
        if end is not None:
            variants.append(pack([label[:end]]))
    return [*reversed(variants), ""]


@dataclass(frozen=True)
class _Source:
    row: dict
    start: int
    end: int


class _Text:
    """Exact joined source and reversible mappings, including disjoint header repeats."""

    def __init__(self, rows: list[dict], section: dict):
        self.sources = []
        parts, position = [], 0
        for row in rows:
            if not row["text"]:
                continue
            if parts:
                position += 1
            self.sources.append(_Source(row, position, position + len(row["text"])))
            parts.append(row["text"])
            position += len(row["text"])
        self.text = "\n".join(parts)
        self.ends = [source.end for source in self.sources]
        self.separators = set(self.ends[:-1])
        self.headers = []
        paragraphs = {len(self.text)}
        # An explicit or inferred section heading belongs with its first body paragraph.
        heading_end = 0
        if self.sources and not (section["boundary"].startswith("preamble")
                                 or section["boundary"].startswith("physical")
                                 or section["boundary"] == "unstructured_document"):
            first = self.sources[0]
            if first.row["text"].strip() == section["title"].strip():
                heading_end = first.end

        for index, source in enumerate(self.sources):
            value = source.row["text"]
            lines = list(_LINE_BREAK.finditer(value))
            table = source.row.get("block_type") == "table"
            # A tabular plain-text record can arrive without optional block metadata.
            table = table or bool(lines and ("\t" in value or value.lstrip().startswith("|")))
            header_end = None
            if table and lines and value[:lines[0].start()].strip():
                header_end = lines[0].start()
                if len(lines) > 1 and _MARKDOWN_RULE.fullmatch(value[lines[0].end():lines[1].start()]):
                    header_end = lines[1].start()
                self.headers.append((source.start, source.start + header_end, source.end))
            for match in lines:
                end = source.start + match.end()
                if table and (header_end is None or match.start() > header_end):
                    paragraphs.add(end)
            for match in _BLANK_LINE.finditer(value):
                paragraphs.add(source.start + match.end())
            if index + 1 < len(self.sources):
                following = self.sources[index + 1].row["text"].lstrip()
                location = source.row.get("locator") or {}
                pdf_line = any(key in location for key in ("source_page", "page", "page_number"))
                closed = bool(re.search(r'''[。.!！?？]["'”’)\]）】》」』]*\s*$''', value))
                if ((table or not pdf_line or closed) and not _NUMBER_ONLY.fullmatch(value.strip())
                        and not _DEPENDENT.match(following)):
                    paragraphs.add(source.end)

        self.paragraphs = sorted({self.end(n) for n in paragraphs if n > heading_end})
        # Keep an exception with the preceding sentence whenever their combined text fits.
        self.sentences = sorted({self.end(match.end()) for match in _SENTENCE_END.finditer(self.text)
                                 if match.end() > heading_end
                                 and not _DEPENDENT_AFTER.match(self.text, match.end())}
                                | {len(self.text)})
        self.words = sorted({self.end(match.end()) for match in _WORD_END.finditer(self.text)
                             if match.end() > heading_end} | {len(self.text)})
        self.header_starts = [header[0] for header in self.headers]
        self.protected = []
        self.protected_starts = []

    def keep_context(self, count, max_tokens: int) -> None:
        """V3: keep fitting entry/table/note groups together, without rewriting.

        Entry punctuation and source roles are structural signals, not a list of
        asset names. Oversized tables retain the original row/header splitting;
        impossible whole groups remain fully reachable via the reading outline.
        """
        candidates = []
        header_starts = set(self.header_starts)
        for i, source in enumerate(self.sources):
            row, value = source.row, source.row["text"]
            journal = bool(_JOURNAL_LINE.search(value))
            table = row.get("block_type") == "table" or source.start in header_starts
            note = (row.get("block_type") == "footnote" or (row.get("locator") or {}).get("role") == "footnote"
                    or bool(_NOTE_LINE.fullmatch(value)))
            if not (journal or table or note):
                continue
            if journal and i + 1 < len(self.sources) and _JOURNAL_LINE.search(self.sources[i + 1].row["text"]):
                continue  # Classify the complete contiguous entry once, at its end.
            first = i
            if note and i:
                first = i - 1
            elif journal:
                while first and _JOURNAL_LINE.search(self.sources[first - 1].row["text"]):
                    first -= 1
            if first and re.search(r"[:：]\s*$", self.sources[first - 1].row["text"]):
                first -= 1
            candidates.append((self.sources[first].start, source.end))
        for left, right in candidates:
            if self.protected and self.protected[-1][1] >= left:
                left = self.protected[-1][0]
            if count(self.text[left:right]) <= max_tokens:
                if self.protected and self.protected[-1][1] >= left:
                    self.protected.pop()
                self.protected.append((left, right))
        self.protected_starts = [left for left, _ in self.protected]
        for name in ("paragraphs", "sentences", "words"):
            boundaries = getattr(self, name)
            boundaries = [end for end in boundaries if self.cut_allowed(end)]
            boundaries.extend(self.end(left) for left, _ in self.protected if left)
            boundaries.extend(right for _, right in self.protected)
            setattr(self, name, sorted(set(boundaries)))

    def cut_allowed(self, end: int) -> bool:
        index = bisect_left(self.protected_starts, end) - 1
        return index < 0 or end >= self.protected[index][1]

    def start(self, value: int) -> int:
        return value + 1 if value in self.separators else value

    def end(self, value: int) -> int:
        return value - 1 if value - 1 in self.separators else value

    def ranges(self, start: int, end: int, repeat=None) -> list[tuple[int, int]]:
        ranges = []
        for left, right in ([repeat] if repeat else []) + [(start, end)]:
            left, right = self.start(left), self.end(right)
            if right <= left:
                continue
            if ranges and self.start(ranges[-1][1]) >= left:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], right))
            else:
                ranges.append((left, right))
        return ranges

    def render(self, start: int, end: int, repeat=None) -> str:
        return "\n".join(self.text[left:right] for left, right in self.ranges(start, end, repeat))

    def spans(self, start: int, end: int, repeat=None) -> list[dict]:
        result, offset = [], 0
        for left, right in self.ranges(start, end, repeat):
            for index in range(bisect_right(self.ends, left), len(self.sources)):
                source = self.sources[index]
                if source.start >= right:
                    break
                first, last = max(left, source.start), min(right, source.end)
                result.append({"block_id": source.row["block_id"], "ordinal": source.row["ordinal"],
                               "content_sha256": source.row["content_sha256"],
                               "start": first - source.start, "end": last - source.start,
                               "text_start": offset + first - left, "text_end": offset + last - left})
            offset += right - left + 1
        return result


def _groups(rows: list[dict], *, structure_version: str = "v2"):
    # The canonical record.text wins over any optional/stale data.text, without writes.
    adapted = []
    for row in rows:
        data = dict(row.get("data") or {})
        data.update(text=row["text"], caption="", expression_text="", items=[])
        adapted.append({**row, "search_text": row["text"], "data": data})
    sections = build_document_sections(adapted, structure_version=structure_version)
    owners = {}
    for index, section in enumerate(sections):
        for block_id in section["block_ids"]:
            previous = owners.get(block_id)
            if previous is None or len(section["block_ids"]) < len(sections[previous]["block_ids"]):
                owners[block_id] = index
    current, group = None, []
    for row in rows:
        owner = owners[row["block_id"]]
        if group and owner != current:
            yield sections[current], group
            group = []
        current = owner
        group.append(row)
    if group:
        yield sections[current], group


def _overlap(source: _Text, previous_start: int, start: int, count, budget: int):
    if not budget:
        return None
    header_index = bisect_right(source.header_starts, start) - 1
    if header_index >= 0:
        left, right, table_end = source.headers[header_index]
        if right < start < table_end and count(source.text[left:right]) <= budget:
            return left, right
    end = source.end(start)
    # Never repeat the entire preceding slice; at least its first character is new only once.
    size = _last_fit(range(1, end - previous_start),
                     lambda n: count(source.render(end - n, end)) <= budget)
    if size is None:
        return None
    left = source.start(end - size)
    for boundaries in (source.paragraphs, source.sentences, source.words):
        pos = bisect_left(boundaries, left)
        if pos < len(boundaries):
            candidate = source.start(boundaries[pos])
            if candidate < end and count(source.render(candidate, end)) <= budget:
                left = candidate
                break
    return (left, end) if left < end else None


def _cut(source: _Text, start: int, prefixes: list[str], repeat, count, max_tokens: int, oversized):
    protected = bisect_right(source.protected_starts, start) - 1
    if protected >= 0:
        left, right = source.protected[protected]
        if left <= start < right:
            # A complete fitting group takes priority over a longer title prefix.
            first = next((i for i, p in enumerate(prefixes)
                          if count(p + source.render(start, right)) <= max_tokens), len(prefixes) - 1)
            prefixes = prefixes[first:]

    def choose(candidates, prefix, first=0, *, natural=True):
        if first >= len(candidates):
            return None
        if natural:
            previous = oversized.get((prefix, candidates[first]))
            if previous and start > previous[0] and candidates[first] - start > previous[1]:
                return None
        # Optional repeated text never forces a whole fitting paragraph to be split.
        for prior in ([repeat, None] if repeat else [None]):
            def fits(end, prior=prior):
                if not source.cut_allowed(end):
                    return False
                fits_budget = count(prefix + source.render(start, end, prior)) <= max_tokens
                if natural and prior is None and not fits_budget:
                    # Retry an enormous failing suffix when its remaining range halves.
                    # Re-tokenizing it after each small cut would be quadratic. This
                    # is a packing hint only: every admitted input is still measured.
                    oversized[prefix, end] = (start, (end - start) // 2)
                return fits_budget
            if fits(candidates[first]):
                return _last_fit(candidates, fits, first), prefix, prior
        return None

    candidates = range(start + 1, len(source.text) + 1)
    for prefix in prefixes:
        for boundaries in (source.paragraphs, source.sentences, source.words):
            result = choose(boundaries, prefix, bisect_right(boundaries, start))
            if result is not None:
                return result
        result = choose(candidates, prefix, natural=False)
        if result is not None:
            return result
    # A tokenizer's count is not necessarily monotone (a longer word may merge).
    # Exhaust before declaring an impossible character/budget; never silently skip it.
    for end in candidates:
        if source.cut_allowed(end) and count(source.render(start, end)) <= max_tokens:
            return end, "", None
    raise ValueError(f"max_tokens={max_tokens} cannot encode source text at character offset {start}")


def build_semantic_units(records: Iterable[dict], *, token_count: Callable[[str], int],
                         max_tokens: int = 480, overlap_tokens: int = 64,
                         context_tokens: int = 64, structure_version: str = "v2") -> list[dict]:
    """Build exact, reversible semantic inputs, isolated by resource AND version.

    ``text`` is the newline join of the source fragments identified by spans;
    ``embedding_text`` adds only a bounded real-title/section-path prefix. Every
    character of a nonblank source block is covered, including its whitespace.
    All-blank groups and empty blocks need not produce an embedding. Empty input
    returns []. Invalid identities, types, counters and budgets fail explicitly.

    ``max_tokens`` bounds the complete input. The other budgets are upper bounds,
    include the counter's special tokens, and can shrink to zero to make progress.
    Unit IDs bind document/version, section, exact spans/hashes, and embedding
    input; unit_index is a zero-based global sequence, not part of the identity.
    ``structure_version='v2'`` preserves legacy groups, spans and identities.
    Opt-in ``v3`` uses the revised outline and an independent unit-ID namespace;
    callers must also bind their projection schema/fingerprint to that version.
    """
    if structure_version not in ("v2", "v3"):
        raise ValueError("structure_version must be 'v2' or 'v3'")
    for name, value in (("max_tokens", max_tokens), ("overlap_tokens", overlap_tokens),
                        ("context_tokens", context_tokens)):
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
        if value < (1 if name == "max_tokens" else 0):
            raise ValueError(f"{name} is outside its valid range")
    if not callable(token_count):
        raise TypeError("token_count must be callable")

    @lru_cache(maxsize=256)
    def count(text):
        value = token_count(text)
        if type(value) is not int or value < 0:
            raise ValueError("token_count must return a nonnegative integer including special tokens")
        return value

    documents = {}
    for row in records:
        if not isinstance(row, dict):
            raise TypeError("records must contain dictionaries")
        for key in ("resource_id", "version_id", "block_id", "content_sha256"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"records require a nonempty string {key}")
        if type(row.get("ordinal")) is not int:
            raise ValueError("records require integer ordinal values")
        if not isinstance(row.get("text"), str) or not isinstance(row.get("title", ""), str):
            raise TypeError("record text and title must be strings")
        documents.setdefault((row["resource_id"], row["version_id"]), []).append(row)

    result = []
    for identity, document in documents.items():
        rows = sorted(document, key=lambda row: (row["ordinal"], row["block_id"]))
        title = next((row.get("title", "") for row in rows if row.get("title")), "")
        for section, group in _groups(rows, structure_version=structure_version):
            if not any(row["text"].strip() for row in group):
                continue
            source = _Text(group, section)
            if structure_version == "v3":
                source.keep_context(count, max_tokens)
            prefixes = _prefixes(title, section, count, min(context_tokens, max_tokens))
            whole_prefix = next((p for p in prefixes if count(p + source.text) <= max_tokens), None)
            start, previous_start = 0, 0
            oversized = {}
            while start < len(source.text):
                if whole_prefix is not None:
                    end, prefix, repeat = len(source.text), whole_prefix, None
                else:
                    repeat = _overlap(source, previous_start, start, count,
                                      min(overlap_tokens, max_tokens)) if start else None
                    end, prefix, repeat = _cut(source, start, prefixes, repeat, count, max_tokens, oversized)
                end = source.end(end)
                text = source.render(start, end, repeat)
                spans = source.spans(start, end, repeat)
                embedding_text = prefix + text
                if end <= start or not spans or count(embedding_text) > max_tokens:
                    raise ValueError("semantic unit failed progress or complete-input token budget validation")
                unit = {"section_id": section["section_id"], "section_title": section["title"],
                        "section_path": [*section["parent_titles"], section["title"]],
                        "text": text, "embedding_text": embedding_text, "source_spans": spans,
                        "block_ids": list(dict.fromkeys(span["block_id"] for span in spans))}
                unit["unit_id"] = "semantic_" + _hash([f"semantic-sections-{structure_version}", identity, unit])
                unit["unit_index"] = len(result)
                result.append(unit)
                previous_start, start = start, source.start(end)
    return result
