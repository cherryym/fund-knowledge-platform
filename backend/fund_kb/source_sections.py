"""Lossless, deterministic reading sections for ONE supplied document/version.

No I/O, application imports, model calls, text limits, or changes to source blocks.
``build_document_sections`` returns complete nested heading ranges (parents include
their descendants), plus any front matter. With no confirmed headings it returns
whole physical pages, joining uncertain sentence/table continuations. Without page
metadata either, the complete supplied document is the only defensible fallback.

``select_document_sections`` chooses the smallest complete range for each anchor,
in document order. Parent *titles* provide context without loading parent bodies.
The caller can still explicitly read every source block or any full parent range.
IDs identify this structural selection, not authority, a content hash, or an ACL
decision; the caller must continue to check the document/version and permissions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["build_document_sections", "expand_reading_sections", "resolve_section_references",
           "select_document_sections", "select_parent_document_sections", "select_sections_from_outline"]

_NUMBER = r"[零〇○一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟\d]+"
_LEGAL = re.compile(rf"^(第\s*{_NUMBER}\s*([编篇部章节条]))(.*)$")
_ITEM = re.compile(r"^([零〇一二三四五六七八九十百]+\s*[、．.])(.*)$")
_SUBITEM = re.compile(r"^([（(]\s*[零〇一二三四五六七八九十百]+\s*[）)])(.*)$")
# A decimal/date is not an item: a dot requires following whitespace, and
# the title must start with something other than another number.
_ARABIC_ITEM = re.compile(r"^(\d+\s*(?:、|[.．](?=\s)))(\s*[^\d\s].*)$")
_ARABIC_SUBITEM = re.compile(r"^([（(]\s*\d+\s*[）)])(\s*[^\d\s].*)$")
_TOC = re.compile(r"(?:[.．·•…⋯]\s*){2,}(?:[\dIVXLCDMivxlcdm]+(?:\s*[-—–]\s*\d+)?)?\s*$")
_PAGE_LABEL = re.compile(r"第\s*(\d+)\s*页")
_PAGE_NUMBER = re.compile(r"(?:[-—–]\s*)?\d+(?:\s*[-—–])?|第\s*\d+\s*页(?:\s*[/／共]\s*\d+\s*页?)?")
_FOOTNOTE = re.compile(r"^\d+\s+[^\W\d_].*[。.!！?？]$", re.UNICODE)
_SENTENCE_PUNCTUATION = re.compile(r"[。！？!?；;，,]|[:：].+\S")
_QUANTITY = re.compile(
    r"\d+(?:[.．]\d+)?\s*(?:[%％元年月日天倍]|万元|亿元|个月|个工作日|bps?\b)", re.IGNORECASE
)
_PROSE = re.compile(
    r"应当|必须|不得|不应|可以|按照|按日|按月|按年|按季|每日|每月|每年|"
    r"不超过|不少于|不低于|应[在于按由以将予向]|须[在于按由以向]|费率为|费用为|"
    r"^(?:本基金|本公司|本办法|本规定|投资者应|管理人应|托管人应|如果|若|当|除非)"
)
_REFERENCE_TAIL = re.compile(r"^(?:规定|所|的|中|及|和|至|到|之|要求|适用|执行)")
_CONTINUED = re.compile(r"^(?:[（(]?续[表页）)]|表\s*\S*\s*[（(]续|continued\b)", re.IGNORECASE)
_DEPENDENT_START = re.compile(r"^(?:但是|但|其中|否则|并且|并|且|以及|或者|或|及|的|和|与|除外|除非)")
_RANKS = {"编": (1, "chinese_part"), "篇": (1, "chinese_part"), "部": (1, "chinese_part"),
          "章": (2, "chinese_chapter"), "节": (3, "chinese_section"), "条": (4, "chinese_article")}
_MARGIN_KINDS = {"header", "footer", "page_header", "page_footer", "footnote"}

# V3 recognises numbering separately from title content. Dates are removed only
# from a temporary classification string, never from a source block or span.
_ARABIC_ITEM_V3 = re.compile(r"^(\d+\s*(?:、|[.．](?=\s)))(.*)$")
_ARABIC_SUBITEM_V3 = re.compile(r"^([（(]\s*\d+\s*[）)])(.*)$")
_DATE = re.compile(r"\d{4}\s*(?:年|[-/.．])\s*\d{1,2}\s*(?:月|[-/.．])\s*\d{1,2}\s*日?")
_RELATIVE_DATE = re.compile(r"(?<![A-Za-z])[TtDdＴＤ](?:\s*[+＋\-－]\s*\d+)?\s*日?(?![A-Za-z])")
_TOC_LABEL = re.compile(r"目\s*录|contents|table of contents", re.IGNORECASE)
_TOC_PAGE = re.compile(r"\s+[\dIVXLCDMivxlcdm]+(?:\s*[-—–]\s*\d+)?\s*$")
_TOC_ROLES = {"toc", "toc_entry", "table_of_contents", "contents"}


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(block: dict) -> str:
    # Tables stay atomic; never copy/flatten their rows just to infer a heading.
    if block.get("block_type") == "table":
        return ""
    data = _mapping(block.get("data"))
    for value in (data.get("text"), block.get("search_text"), block.get("text"),
                  data.get("caption"), data.get("expression_text")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    items = data.get("items")
    return "\n".join(str(item) for item in items) if isinstance(items, list) else ""


def _page(block: dict) -> int | None:
    locator = _mapping(block.get("locator"))
    # source_page is the native PDF/OCR field. The others are compatibility paths.
    for key in ("source_page", "page", "page_number"):
        value = locator.get(key)
        if isinstance(value, str) and value.strip().isdecimal():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    label = locator.get("label")
    match = _PAGE_LABEL.search(label) if isinstance(label, str) else None
    return int(match.group(1)) if match else None


def _margin(block: dict, text: str) -> bool:
    locator = _mapping(block.get("locator"))
    return (block.get("block_type") in _MARGIN_KINDS
            or locator.get("role") in _MARGIN_KINDS
            or bool(_PAGE_NUMBER.fullmatch(text)))


def _bbox(block: dict) -> tuple | None:
    locator = _mapping(block.get("locator"))
    space = locator.get("coordinate_space", "top_left_points")
    box = locator.get("bbox")
    if (not isinstance(space, str) or not space.startswith("top_left")
            or not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(type(value) in {int, float} for value in box)):
        return None
    if not (box[0] <= box[2] and box[1] <= box[3]):
        return None
    return tuple(box)


def _title_like(text: str) -> bool:
    text = text.strip().rstrip(":：").strip()
    return (bool(re.search(r"[\u3400-\u9fffA-Za-z]", text))
            and not _SENTENCE_PUNCTUATION.search(text)
            and not _QUANTITY.search(text)
            and not _PROSE.search(text))


@dataclass
class _Heading:
    start: int
    title: str
    boundary: str
    rank: float | None
    level: int | None
    end: int = 0
    parents: tuple[str, ...] = ()
    family: int | None = None
    number: int | None = None


def _heading(block: dict, text: str, index: int) -> _Heading | None:
    kind = block.get("block_type", "paragraph")
    explicit = kind == "heading"
    if kind not in {"heading", "paragraph", "text"} or not text or _margin(block, text):
        return None
    line = text.splitlines()[0].strip()
    if _TOC.search(line) or _FOOTNOTE.fullmatch(line):
        return None
    rank, boundary, title = None, "heading", line
    legal = _LEGAL.match(line)
    if legal:
        marker, unit, tail = legal.groups()
        stripped = tail.strip()
        # "第十条规定的费率" is a reference in prose, not the start of Article 10.
        reference = bool(_REFERENCE_TAIL.match(stripped))
        if not reference and (not stripped or unit == "条" or _title_like(stripped)):
            rank, boundary = _RANKS[unit]
            if unit == "条" and stripped and not _title_like(stripped):
                title = marker  # The entire inline clause remains in its source block.
    else:
        for pattern, item_rank, item_boundary in (
            (_ITEM, 5, "chinese_item"), (_SUBITEM, 6, "chinese_subitem"),
            (_ARABIC_ITEM, 7, "arabic_item"), (_ARABIC_SUBITEM, 8, "arabic_subitem")
        ):
            match = pattern.match(line)
            if match and _title_like(match.group(2)):
                rank, boundary = item_rank, item_boundary
                break
    if not explicit and rank is None:
        return None
    level = None
    if explicit:
        value = _mapping(block.get("data")).get("level", 2)
        if isinstance(value, str) and value.isdecimal():
            value = int(value)
        level = value if type(value) is int and 1 <= value <= 6 else 2
        boundary = "heading"
    return _Heading(index, title, boundary, rank, level)


def _structure_version(value: str) -> None:
    if value not in ("v2", "v3"):
        raise ValueError("structure_version must be 'v2' or 'v3'")


def _number(value: str) -> int | None:
    value = re.sub(r"\s+", "", value)
    if value.isdecimal():
        try:
            return int(value)
        except ValueError:
            return None  # Excessively large source numbering is not a usable ID.
    digits = dict(zip("零〇○一二三四五六七八九两壹贰叁肆伍陆柒捌玖", (0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,
                                                                 2, 1, 2, 3, 4, 5, 6, 7, 8, 9)))
    scales = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000,
              "万": 10000, "亿": 100000000}
    total, part, digit = 0, 0, 0
    for char in value:
        if char in digits:
            digit = digit * 10 + digits[char]
        elif char in scales:
            scale = scales[char]
            if scale < 10000:
                part += (digit or 1) * scale
            else:
                total = (total + part + digit) * scale
                part = 0
            digit = 0
        else:
            return None
    return total + part + digit if value else None


def _marker(text: str) -> tuple[int | None, int | None]:
    legal = _LEGAL.match(text)
    if legal:
        return _RANKS[legal[2]][0], _number(re.sub(r"^第|[编篇部章节条]$", "", legal[1]).strip())
    for pattern, family in ((_ITEM, 5), (_SUBITEM, 6), (_ARABIC_ITEM_V3, 7), (_ARABIC_SUBITEM_V3, 8)):
        match = pattern.match(text)
        if match:
            return family, _number(re.sub(r"[、．.（）()\s]", "", match[1]))
    return None, None


def _title_like_v3(text: str) -> bool:
    # A standalone calendar date is not a title. A numbered relative day (T+1)
    # can be a process stage even when it has no additional noun phrase.
    # A dated parenthetical stage label may contain a colon and footnote marker.
    # It is still a heading; a colon in ordinary outside prose is not removed.
    classified = re.sub(r"[（(][^（）()\n]*[）)]",
        lambda m: "" if _RELATIVE_DATE.search(m[0]) or _DATE.search(m[0]) else m[0], text)
    classified = _DATE.sub("", classified)
    classified = _RELATIVE_DATE.sub("日期", classified)
    return _title_like(classified)


def _heading_v3(block: dict, text: str, index: int) -> _Heading | None:
    line = text.splitlines()[0].strip() if text else ""
    compound = _LEGAL.match(line)
    if compound and re.match(rf"^第\s*{_NUMBER}\s*[章节条款项]", compound[3]):
        return None  # 第三条第一款规定... is a citation, not a new article.
    heading = _heading(block, text, index)
    if (not heading and text and block.get("block_type", "paragraph") in {"paragraph", "text", "heading"}
            and not _margin(block, text)):
        line = text.splitlines()[0].strip()
        if not _TOC.search(line) and not _FOOTNOTE.fullmatch(line):
            legal = _LEGAL.match(line)
            if legal:
                marker, unit, tail = legal.groups()
                # A separated inline article body may start with 适用/执行/规定.
                # A tight 第四条规定的... remains a reference in ordinary prose.
                reference = bool(_REFERENCE_TAIL.match(tail))
                if not reference and (unit == "条" or not tail.strip() or _title_like_v3(tail)):
                    rank, boundary = _RANKS[unit]
                    title = marker if unit == "条" and not _title_like_v3(tail) else line
                    heading = _Heading(index, title, boundary, rank, None)
            for pattern, rank, boundary in ((_ITEM, 5, "chinese_item"), (_SUBITEM, 6, "chinese_subitem"),
                                             (_ARABIC_ITEM_V3, 7, "arabic_item"),
                                             (_ARABIC_SUBITEM_V3, 8, "arabic_subitem")):
                match = pattern.match(line)
                if match and _title_like_v3(match[2]):
                    heading = _Heading(index, line, boundary, rank, None)
                    break
    if heading:
        heading.family, heading.number = _marker(text.splitlines()[0].strip())
        # Explicit heading levels may all be h2 after import. Numbering still
        # supplies a family even when v2's date classifier rejected its rank.
        if heading.rank is None and heading.family is not None:
            heading.rank = heading.family
    return heading


def _toc_indices(rows: list[dict], texts: list[str], candidates: list[_Heading | None]) -> set[int]:
    """Use TOC roles, leaders, page suffixes and repeated body titles; no rewrites."""
    lines = [text.splitlines()[0].strip() if text else "" for text in texts]
    keys = [re.sub(r"\s+", "", _TOC_PAGE.sub("", _TOC.sub("", line))) for line in lines]
    last_heading = {keys[i]: i for i, h in enumerate(candidates) if h}
    excluded, seen, pending = set(), set(), []
    active, declared = False, False
    for index, (row, line) in enumerate(zip(rows, lines)):
        roles = (row.get("block_type"), _mapping(row.get("data")).get("role"),
                 _mapping(row.get("locator")).get("role"))
        explicit = any(role in _TOC_ROLES for role in roles)
        label, dotted = bool(_TOC_LABEL.fullmatch(line)), bool(_TOC.search(line))
        paged = bool(_TOC_PAGE.search(line)) and bool(candidates[index])
        repeated = last_heading.get(keys[index], index) > index
        if label or explicit or dotted or (paged and repeated):
            active = True
            declared = declared or label
            excluded.update([*pending, index])
            pending = []
            if not label:
                seen.add(keys[index])
        elif active:
            if candidates[index]:
                if keys[index] in seen and not paged:
                    if declared:
                        excluded.update(pending)
                    active, declared, pending = False, False, []  # Body restarts a TOC title.
                elif paged or repeated:
                    excluded.update([*pending, index])
                    pending = []
                    seen.add(keys[index])
                else:
                    pending.append(index)
            elif not line or _margin(row, line):
                excluded.add(index)
            else:
                # Unconfirmed headings preceding prose are body headings.
                active, declared, pending = False, False, []
    if active and declared:
        excluded.update(pending)
    return excluded


def _close_headings_v3(stack: list[_Heading], current: _Heading) -> None:
    # Match an existing numbering family before comparing levels. This preserves
    # a local sequence such as 一 -> articles -> 二, instead of nesting 二 in a 条.
    peer = next((h for h in reversed(stack) if current.family is not None and h.family == current.family), None)
    if peer:
        position = stack.index(peer)
        nested_reset = (current.family in {5, 6, 7, 8} and current.number == 1
                        and any(h.family == 4 for h in stack[position + 1:]))
        if not nested_reset:
            current.rank = peer.rank
    if current.family == 4:
        article = next((i for i in range(len(stack) - 1, -1, -1) if stack[i].family == 4), None)
        if article is not None:
            for heading in stack[article:]:
                heading.end = current.start
            del stack[article:]
        # Items already open before the first article are containers in THIS
        # scope. Promote only those ancestors, not every Chinese item in a file.
        containers = [h for h in stack if h.rank is not None and h.rank >= 4]
        for index, heading in enumerate(containers):
            heading.rank = 3 + (index + 1) / (len(containers) + 1)
    _close_headings(stack, current)


def _ordered(blocks: list[dict]) -> list[dict]:
    seen = set()
    for block in blocks:
        block_id = block.get("block_id")
        if not isinstance(block_id, str) or not block_id.strip() or block_id in seen:
            raise ValueError("source blocks require unique, nonempty string block_id values")
        if type(block.get("ordinal")) is not int:
            raise ValueError("source blocks require integer ordinal values")
        seen.add(block_id)
    return sorted(blocks, key=lambda block: (block["ordinal"], block["block_id"]))


def _make_section(rows: list[dict], title: str, boundary: str, parents: Iterable[str] = ()) -> dict:
    block_ids = [block["block_id"] for block in rows]
    parent_titles = list(parents)
    # No list position, Python hash(), mutable ordinal, or random UUID in the ID.
    identity = json.dumps([boundary, title, parent_titles, block_ids], ensure_ascii=False, separators=(",", ":"))
    return {"section_id": "section_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
            "title": title, "block_ids": block_ids,
            "start_ordinal": rows[0]["ordinal"], "end_ordinal": rows[-1]["ordinal"],
            "parent_titles": parent_titles, "boundary": boundary}


def _close_headings(stack: list[_Heading], current: _Heading) -> None:
    def close_from(position: int) -> None:
        for previous in stack[position:]:
            previous.end = current.start
        del stack[position:]

    if current.rank is not None:
        # Chinese structural families retain their hierarchy even when a PDF
        # supplies paragraph blocks or an importer assigns every heading h2.
        while stack:
            position = next((i for i in range(len(stack) - 1, -1, -1)
                             if stack[i].rank is not None), None)
            if position is None or stack[position].rank < current.rank:
                break
            close_from(position)
        if current.level is not None:
            while stack and stack[-1].rank is None and stack[-1].level >= current.level:
                close_from(len(stack) - 1)
    else:
        # A real heading's level closes any inferred descendants of its previous
        # explicit peer. Unnumbered h2 sections can also live below a PDF chapter.
        while stack:
            position = next((i for i in range(len(stack) - 1, -1, -1)
                             if stack[i].level is not None), None)
            if position is None:
                if current.level == 1:
                    close_from(0)
                break
            if stack[position].level < current.level:
                break
            close_from(position)


@dataclass
class _Page:
    start: int
    end: int
    number: int | None
    first: int | None
    last: int | None


def _physical_pages(rows: list[dict], texts: list[str]) -> list[_Page]:
    pages: list[_Page] = []
    start, number = 0, None
    for index, block in enumerate(rows):
        page = _page(block)
        if page is not None and number is not None and page != number:
            pages.append(_Page(start, index, number, None, None))
            start = index
        if page is not None:
            number = page
    pages.append(_Page(start, len(rows), number, None, None))
    for page in pages:
        content = []
        for index in range(page.start, page.end):
            block, text = rows[index], texts[index]
            if (_margin(block, text) or _FOOTNOTE.fullmatch(text)
                    or (not text and block.get("block_type") not in {"table", "image", "attachment"})):
                continue
            if page.first is None:
                page.first = index
            page.last = index
            content.append(index)
        boxes = {index: _bbox(rows[index]) for index in content}
        if content and all(box is not None for box in boxes.values()):
            # The PDF importer appends tables after ALL page text. Use physical
            # coordinates only for edge decisions, keeping source ordinal order
            # and every block in the output. At a shared edge the full table wins.
            page.first = min(content, key=lambda i: (boxes[i][1], rows[i].get("block_type") != "table",
                                                      boxes[i][0], i))
            page.last = max(content, key=lambda i: (boxes[i][3], rows[i].get("block_type") == "table",
                                                      boxes[i][2], i))
    return pages


def _table_id(block: dict) -> object:
    for value in (_mapping(block.get("data")), _mapping(block.get("locator"))):
        if value.get("table_id") is not None:
            return value["table_id"]
    return None


def _continues(rows: list[dict], texts: list[str], left: int | None, right: int | None) -> bool:
    if left is None or right is None:
        return True  # Blank/missing edge evidence cannot establish a sentence end.
    previous, following = rows[left], rows[right]
    tail, head = texts[left], texts[right]
    if _CONTINUED.search(head) or re.search(r"(?:下页续|续下页|见下页|未完待续)[。.:：）)]?$", tail):
        return True
    for metadata in (_mapping(following.get("data")), _mapping(following.get("locator"))):
        if any(metadata.get(key) for key in ("continued", "is_continuation", "continued_from")):
            return True
    left_table, right_table = previous.get("block_type") == "table", following.get("block_type") == "table"
    if left_table and right_table:
        left_id, right_id = _table_id(previous), _table_id(following)
        # Unknown table identity is not evidence that adjacent table pieces differ.
        return left_id is None or right_id is None or left_id == right_id
    if left_table:
        return bool(_DEPENDENT_START.search(head))
    if not tail:
        return True
    end = tail.rstrip().rstrip('”’"\'）)]】》」』').rstrip()
    if _DEPENDENT_START.search(head):
        return True
    if re.search(r"\d[.]$", end) and re.match(r"\d", head):
        return True
    return not bool(re.search(r"[。.!！?？]$", end))


def _fallback_sections(rows: list[dict], texts: list[str], *, preamble: bool = False) -> list[dict]:
    if not rows:
        return []
    pages = _physical_pages(rows, texts)
    if all(page.number is None for page in pages):
        return [_make_section(rows, "文档前置内容" if preamble else "完整文档（无结构及页码）",
                              "preamble" if preamble else "unstructured_document")]
    sections = []
    first = previous = pages[0]
    last_content = first.last

    def emit(last: _Page) -> None:
        joined = first is not last
        title = f"第{first.number}–{last.number}页" if joined else f"第{first.number}页"
        boundary = "physical_pages_continuation" if joined else "physical_page"
        if preamble:
            title, boundary = "文档前置内容 · " + title, "preamble_" + boundary
        sections.append(_make_section(rows[first.start:last.end], title, boundary))

    for page in pages[1:]:
        adjacent = previous.number is None or page.number is None or page.number == previous.number + 1
        if not adjacent or not _continues(rows, texts, last_content, page.first):
            emit(previous)
            first = page
            last_content = None
        if page.last is not None:
            last_content = page.last
        previous = page
    emit(previous)
    return sections


def build_document_sections(blocks: list[dict], *, structure_version: str = "v2") -> list[dict]:
    """Build all complete source sections; every supplied block is covered.

    Order is (ordinal, block_id); ordinal ranges are inclusive. Empty *text* blocks
    are retained. Ambiguous Arabic numbered prose stays body text. Chinese/Arabic
    nominal headings and legal article starts are recognized; TOC dot leaders,
    page numbers and footnotes are not headings. Source IDs must be unique nonempty strings and
    ordinals integers; malformed identity raises ValueError instead of losing rows.
    ``v2`` retains the original algorithm and section identities. ``v3`` enables
    context-sensitive mixed numbering, TOC ranges and dated process headings.
    """
    _structure_version(structure_version)
    rows = _ordered(blocks)
    if not rows:
        return []
    texts = [_text(block) for block in rows]
    if structure_version == "v3":
        candidates = [_heading_v3(block, text, i) for i, (block, text) in enumerate(zip(rows, texts))]
        excluded = _toc_indices(rows, texts, candidates)
        headings, stack = [], []
        for index, current in enumerate(candidates):
            if current is None or index in excluded:
                continue
            _close_headings_v3(stack, current)
            current.end = len(rows)
            current.parents = tuple(parent.title for parent in stack)
            stack.append(current)
            headings.append(current)
        if not headings:
            return _fallback_sections(rows, texts)
        first = headings[0].start
        return [*_fallback_sections(rows[:first], texts[:first], preamble=True),
                *(_make_section(rows[h.start:h.end], h.title, h.boundary, h.parents) for h in headings)]
    toc_pages, dotted_counts = set(), {}
    for block, text in zip(rows, texts):
        page = _page(block)
        if page is None:
            continue
        if re.fullmatch(r"目\s*录|contents|table of contents", text, re.IGNORECASE):
            toc_pages.add(page)
        if text and _TOC.search(text.splitlines()[0]):
            dotted_counts[page] = dotted_counts.get(page, 0) + 1
    toc_pages.update(page for page, count in dotted_counts.items() if count >= 2)
    headings: list[_Heading] = []
    stack: list[_Heading] = []
    for index, (block, text) in enumerate(zip(rows, texts)):
        if _page(block) in toc_pages:
            continue
        current = _heading(block, text, index)
        if current is None:
            continue
        _close_headings(stack, current)
        current.end = len(rows)
        current.parents = tuple(parent.title for parent in stack)
        stack.append(current)
        headings.append(current)
    if not headings:
        return _fallback_sections(rows, texts)
    first = headings[0].start
    sections = _fallback_sections(rows[:first], texts[:first], preamble=True)
    sections.extend(_make_section(rows[heading.start:heading.end], heading.title,
                                  heading.boundary, heading.parents) for heading in headings)
    return sections


def select_document_sections(blocks: list[dict], anchor_block_ids: Iterable[object] | None, *,
                             structure_version: str = "v2") -> list[dict]:
    """Select complete minimal sections, de-duplicated in source order.

    Unknown/malformed anchors are ignored; no legal anchor returns []. A string is
    treated as one ID, never as characters. If a parent is explicitly anchored,
    children it already contains are omitted from the result. Two sibling anchors
    select two siblings, never promote their common parent. No source text is cut.
    """
    _structure_version(structure_version)
    if isinstance(anchor_block_ids, str):
        anchor_block_ids = [anchor_block_ids]
    if not isinstance(anchor_block_ids, Iterable):
        return []
    anchors = {anchor for anchor in anchor_block_ids if isinstance(anchor, str) and anchor.strip()}
    if not anchors or not any(block.get("block_id") in anchors for block in blocks):
        return []
    return select_sections_from_outline(build_document_sections(blocks, structure_version=structure_version), anchors)


def select_sections_from_outline(sections: list[dict], anchors: set[str]) -> list[dict]:
    """Choose complete ranges from an already built outline of the SAME source.

    This avoids reparsing a large manual for every scoped read. The reader owns
    construction of this outline and still validates current source hashes.
    """
    owners: dict[str, int] = {}
    for index, section in enumerate(sections):
        for block_id in section["block_ids"]:
            if block_id not in anchors:
                continue
            previous = owners.get(block_id)
            if previous is None or len(section["block_ids"]) < len(sections[previous]["block_ids"]):
                owners[block_id] = index
    result, covered = [], set()
    for index in sorted(set(owners.values())):
        section = sections[index]
        if not all(block_id in covered for block_id in section["block_ids"]):
            result.append(section)
            covered.update(section["block_ids"])
    return result


class _Outline:
    """Containment by source ranges, never by duplicate titles or business terms."""

    def __init__(self, sections: list[dict]):
        self.sections = sections
        self.by_id = {s["section_id"]: i for i, s in enumerate(sections)}
        if len(self.by_id) != len(sections):
            raise ValueError("outline requires unique section_id values")
        ordered_ids = list(dict.fromkeys(bid for s in sections for bid in s["block_ids"]))
        positions = {bid: i for i, bid in enumerate(ordered_ids)}
        self.parents, self.owners, self.ranges, self.markers = [], {}, [], []
        self.ordered_ids = ordered_ids
        self.toc_blocks: set[str] = set()
        stack = []
        for i, section in enumerate(sections):
            ids = section["block_ids"]
            if not ids or len(set(ids)) != len(ids):
                raise ValueError("outline requires nonempty, unique block ranges")
            start, end = positions[ids[0]], positions[ids[-1]] + 1
            if ordered_ids[start:end] != ids or (self.ranges and start <= self.ranges[-1][0]):
                raise ValueError("outline must contain complete ranges in source order")
            while stack and start >= self.ranges[stack[-1]][1]:
                stack.pop()
            if stack and end > self.ranges[stack[-1]][1]:
                raise ValueError("outline ranges must nest, not cross")
            self.parents.append(stack[-1] if stack else None)
            self.ranges.append((start, end))
            self.markers.append(_marker(section["title"]) if section["boundary"] in {
                "heading", "chinese_part", "chinese_chapter", "chinese_section", "chinese_article",
                "chinese_item", "chinese_subitem", "arabic_item", "arabic_subitem",
            } else (None, None))
            self.owners.update((bid, i) for bid in ids)
            stack.append(i)

    def selected(self, selected: list[dict]) -> list[int]:
        indexes = []
        for section in selected:
            index = self.by_id.get(section.get("section_id"))
            if index is None or section != self.sections[index]:
                raise ValueError("selected sections must come from the supplied outline")
            indexes.append(index)
        return self.minimal_union(indexes)

    def minimal_union(self, indexes: Iterable[int]) -> list[int]:
        result, end = [], -1
        for index in sorted(set(indexes)):
            start, stop = self.ranges[index]
            if start >= end:
                result.append(index)
                end = stop
        return result

    def ancestors(self, index: int):
        while index is not None:
            yield index
            index = self.parents[index]


def select_parent_document_sections(blocks: list[dict], anchor_block_ids: Iterable[object] | None, *,
                                    levels: int = 1, structure_version: str = "v3") -> list[dict]:
    """Read complete structural parents of the minimal anchored sections.

    ``levels=1`` reads the immediate parent (e.g. all stages of a numbered event);
    zero keeps the anchored section. At the root, keep that complete root. There
    is no word-based event classifier, fixed depth limit or body truncation.
    Unknown anchors follow select_document_sections' empty-result convention.
    """
    if type(levels) is not int or levels < 0:
        raise ValueError("levels must be a nonnegative integer")
    outline = build_document_sections(blocks, structure_version=structure_version)
    if isinstance(anchor_block_ids, str):
        anchor_block_ids = [anchor_block_ids]
    anchors = {a for a in anchor_block_ids if isinstance(a, str) and a.strip()} if isinstance(
        anchor_block_ids, Iterable) else set()
    tree = _Outline(outline)
    selected = select_sections_from_outline(outline, anchors)
    expanded = []
    for index in tree.selected(selected):
        for _ in range(levels):
            if tree.parents[index] is None:
                break
            index = tree.parents[index]
        expanded.append(index)
    return [outline[i] for i in tree.minimal_union(expanded)]


_REF_JOIN = r"(?:[、,，及和与]|至|到)"
_REF_NUMBERS = rf"{_NUMBER}(?:\s*{_REF_JOIN}\s*(?:第\s*)?{_NUMBER})*"
_CLAUSE_REFERENCE = re.compile(rf"(?P<scope>本章|本节|本条)?\s*第\s*(?P<numbers>{_REF_NUMBERS})\s*"
                               r"(?P<unit>[章节条款项])")
_ITEM_REFERENCE = re.compile(
    rf"(?:参照|参见|按照|依照|依据|见)\s*(?P<scope>本章|本节|本条|本款|本项)?\s*(?:第\s*)?"
    rf"(?P<numbers>{_REF_NUMBERS})\s*(?P<unit>[款项])?"
    rf"(?!\s*(?:[\d年月日天元%％.．/章节条]|{_REF_JOIN}\s*(?:第\s*)?{_NUMBER}))"
    r"(?:\s*(?:处理|办理|执行|规定))?"
)


def _reference_text(block: dict) -> tuple[str, str]:
    data = _mapping(block.get("data"))
    for field, value in (("data.text", data.get("text")), ("search_text", block.get("search_text")),
                         ("text", block.get("text")), ("data.caption", data.get("caption")),
                         ("data.expression_text", data.get("expression_text"))):
        if isinstance(value, str) and value.strip():
            return field, value
    # Do not manufacture character offsets by flattening table cells or list items.
    return "", ""


def _checked_blocks(tree: _Outline, blocks: list[dict]) -> dict[str, dict]:
    rows = _ordered(blocks)
    for key in ("resource_id", "version_id"):
        if len({b[key] for b in rows if b.get(key) is not None}) > 1:
            raise ValueError("reading expansion requires one resource/version")
    if [b["block_id"] for b in rows] != tree.ordered_ids:
        raise ValueError("blocks and outline must describe the same complete source")
    by_id = {b["block_id"]: b for b in rows}
    for section in tree.sections:
        if (by_id[section["block_ids"][0]]["ordinal"] != section["start_ordinal"]
                or by_id[section["block_ids"][-1]]["ordinal"] != section["end_ordinal"]):
            raise ValueError("blocks and outline ordinal ranges differ")
    # Citation exclusion only: this does not alter or rebuild the supplied v2/v3
    # ranges. A TOC inside a document-title range is still not a dependency list.
    texts = [_text(row) for row in rows]
    candidates = [_heading_v3(row, text, i) for i, (row, text) in enumerate(zip(rows, texts))]
    tree.toc_blocks = {rows[i]["block_id"] for i in _toc_indices(rows, texts, candidates)}
    return by_id


def _reference_targets(tree: _Outline, owner: int, numbers: list[int], unit: str | None,
                       scope: str | None) -> tuple[str, int | None, list[int]]:
    # A natural paragraph (款) is not a numbered item (项). The outline has no
    # verified natural-paragraph identities; never certify a guessed mapping.
    if unit == "款" or scope == "本款":
        return "unresolved", None, []
    family = {"章": 2, "节": 3, "条": 4}.get(unit)
    families = {family} if family else {5, 6, 7, 8}
    ancestors = list(tree.ancestors(owner))
    scopes = []
    if scope:
        scope_family = {"本章": {2}, "本节": {3}, "本条": {4}, "本款": {5, 6, 7, 8},
                        "本项": {5, 6, 7, 8}}[scope]
        parent = next((i for i in ancestors if tree.markers[i][0] in scope_family), None)
        if parent is None:
            return "unresolved", None, []
        scopes.append((parent, True))
    else:
        # Walk only the numbering scopes containing the reference. A plain 4/5
        # inside step (1) can refer to its enclosing event's sibling items; it
        # can never find an identically named item in an unrelated chapter.
        for i in ancestors:
            if tree.markers[i][0] in families:
                candidate = (tree.parents[i], False)
                if candidate not in scopes:
                    scopes.append(candidate)
        if not scopes:
            scopes.append((owner, False))  # An introduction may cite its own children.
    first_partial = None
    for parent, descendants in scopes:
        candidates = {}
        for i, (candidate_family, number) in enumerate(tree.markers):
            if candidate_family not in families or number not in numbers:
                continue
            in_scope = tree.parents[i] == parent
            if descendants:
                in_scope = i != parent and parent in tree.ancestors(i)
            if in_scope:
                candidates.setdefault(number, []).append(i)
        if any(len(matches) > 1 for matches in candidates.values()):
            return "ambiguous", parent, []
        if numbers and all(number in candidates for number in numbers):
            return "resolved", parent, [candidates[number][0] for number in numbers]
        if candidates and first_partial is None:
            first_partial = parent
        # Explicitly typed articles have a single structural numbering scope.
        if family or scope:
            break
    return "unresolved", first_partial, []


def _read_references(tree: _Outline, selected: list[int], blocks: dict[str, dict]) -> list[dict]:
    references = []
    for index in selected:
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

        def own_heading(match, locations=locations, text=text) -> bool:
            start = match.start() + len(match[0]) - len(match[0].lstrip())
            for left, right, bid, _ in locations:
                if left <= start < right:
                    owner = tree.owners[bid]
                    numbers = re.findall(_NUMBER, match["numbers"])
                    return (match.re is _CLAUSE_REFERENCE and len(numbers) == 1
                            and tree.sections[owner]["block_ids"][0] == bid
                            and tree.markers[owner][0] is not None
                            and tree.markers[owner] == ({"章": 2, "节": 3, "条": 4}.get(match["unit"]),
                                                       _number(numbers[0]))
                            and not text[left:start].strip())
            return False

        matches = sorted([match for pattern in (_ITEM_REFERENCE, _CLAUSE_REFERENCE)
                          for match in pattern.finditer(text)], key=lambda m: (m.start(), -m.end()))
        last_end = -1
        for match in matches:
            if match.start() < last_end or own_heading(match):
                continue
            start = match.start() + len(match[0]) - len(match[0].lstrip())
            end = match.end()
            qualified = False
            continuation = _CLAUSE_REFERENCE.match(text, end)
            while continuation and not own_heading(continuation):
                # 第三条第一款 is not two independent sibling citations. Until
                # that compound path is unambiguous, report the entire path.
                qualified, end = True, continuation.end()
                continuation = _CLAUSE_REFERENCE.match(text, end)
            spans = []
            for left, right, bid, field in locations:
                first, last = max(start, left), min(end, right)
                if first < last:
                    spans.append({"block_id": bid, "ordinal": blocks[bid]["ordinal"],
                                  "content_sha256": blocks[bid].get("content_sha256"), "text_field": field,
                                  "start": first - left, "end": last - left,
                                  "text_start": first - start, "text_end": last - start})
            if not spans or any(span["block_id"] in tree.toc_blocks for span in spans):
                continue
            bid = spans[0]["block_id"]
            owner = tree.owners[bid]
            numbers = [_number(n) for n in re.findall(_NUMBER, match["numbers"])]
            if any(n is None for n in numbers):
                numbers = []
            if re.search(r"至|到", match["numbers"]):
                if len(numbers) == 2 and 0 <= numbers[1] - numbers[0] < len(tree.sections):
                    numbers = list(range(numbers[0], numbers[1] + 1))
                else:
                    numbers = []
            status, scope, targets = _reference_targets(tree, owner, numbers, match["unit"], match["scope"])
            if qualified:
                status, scope, targets = "unresolved", None, []
            prefix = re.split(r"[。！？!?；;]", text[:start])[-1].rstrip()
            external = re.search(r"《[^《》]+》\s*$|(?:前|后|上|下|另一|其他)(?:一)?[章节条款项]\s*$|"
                                 rf"附[件录]\s*(?:{_NUMBER})?\s*$|第\s*{_NUMBER}\s*[章节]\s*$", prefix)
            if external:
                status, scope, targets = "external", None, []
            references.append({"text": text[start:end], "source_section_id": tree.sections[owner]["section_id"],
                               "source_spans": spans, "numbers": numbers, "status": status,
                               "scope_section_id": tree.sections[scope]["section_id"] if scope is not None else None,
                               "target_section_ids": [tree.sections[i]["section_id"] for i in targets]})
            last_end = end
    return references


def _reference_result(tree: _Outline, references: list[dict], indexes: Iterable[int]) -> dict:
    return {"sections": [tree.sections[i] for i in tree.minimal_union(indexes)], "references": references,
            "warnings": [{"code": reference["status"] + "_reference", "text": reference["text"],
                          "source_section_id": reference["source_section_id"],
                          "source_spans": reference["source_spans"]}
                         for reference in references if reference["status"] != "resolved"]}


def resolve_section_references(blocks: list[dict], anchor_block_ids: Iterable[object] | None, *,
                               structure_version: str = "v3") -> dict:
    """Resolve one hop of explicit same-scope numbering in complete anchored sections.

    Returns ``sections`` (only resolved targets), ``references`` and ``warnings``.
    Each reference carries exact text and half-open character source_spans with
    text_field, block identity, optional supplied hash and offsets into reference
    text. Resolved/ambiguous/unresolved/external are structural navigation states,
    not authority or evidence of applicability. No title/topic matching is used.
    """
    outline = build_document_sections(blocks, structure_version=structure_version)
    selected = select_document_sections(blocks, anchor_block_ids, structure_version=structure_version)
    tree = _Outline(outline)
    references = _read_references(tree, tree.selected(selected), _checked_blocks(tree, blocks))
    targets = [tree.by_id[sid] for reference in references for sid in reference["target_section_ids"]]
    return _reference_result(tree, references, targets)


def expand_reading_sections(outline: list[dict], selected: list[dict], *, blocks: list[dict] | None = None,
                            purpose: str = "dependencies") -> dict:
    """Expand an existing v2 OR v3 outline; never silently reparse it as another version.

    purpose is ``parent``, ``dependencies`` or ``parent_and_dependencies``. Parent
    expansion promotes each original selection once. Dependency expansion follows
    explicit resolved references to a fixed point, with a visited set, no depth
    cap, and no automatic parent promotion of dependency targets. Returned sections
    include the original selection. Without blocks, dependency requests report
    ``blocks_required`` and return all known ranges; no text or links are invented.
    The caller owns current ACL/hash validation and supplies the SAME complete
    document/version for outline and blocks. Inputs are never mutated.
    """
    if purpose not in {"parent", "dependencies", "parent_and_dependencies"}:
        raise ValueError("purpose must be parent, dependencies or parent_and_dependencies")
    tree = _Outline(outline)
    indexes = tree.selected(selected)
    if purpose in {"parent", "parent_and_dependencies"}:
        indexes = tree.minimal_union(tree.parents[i] if tree.parents[i] is not None else i for i in indexes)
    if purpose == "parent":
        return _reference_result(tree, [], indexes)
    if blocks is None:
        result = _reference_result(tree, [], indexes)
        result["warnings"].append({"code": "blocks_required"})
        return result
    by_id = _checked_blocks(tree, blocks)
    pending, scheduled, visited = deque(indexes), set(indexes), set()
    references, seen_references = [], set()
    while pending:
        index = pending.popleft()
        if index in visited:
            continue
        visited.add(index)
        for reference in _read_references(tree, [index], by_id):
            key = tuple((s["block_id"], s["text_field"], s["start"], s["end"]) for s in reference["source_spans"])
            if key not in seen_references:
                seen_references.add(key)
                references.append(reference)
            for sid in reference["target_section_ids"]:
                target = tree.by_id[sid]
                if target not in scheduled:
                    scheduled.add(target)
                    pending.append(target)
    references.sort(key=lambda r: (tree.owners[r["source_spans"][0]["block_id"]],
                                   r["source_spans"][0]["ordinal"], r["source_spans"][0]["start"]))
    return _reference_result(tree, references, visited)
