"""Read-only prefill from accessible replacement notices and registered facts.

Suggestions never establish legal effect. Ambiguous titles/dates stay unresolved;
only the existing explicit administrator confirmation may persist a fact.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import date

from sqlalchemy import or_, select

from . import models as m
from . import services as svc
from . import source_authority as authority

BOOK = r"《([^《》]{2,400})》"
BOOK_RE = re.compile(BOOK)
REPEAL = re.compile(r"(?:同时|相应|即行|予以)?(?:废止|停止(?:执行|适用)|不再执行)")
ISSUE = re.compile(r"现(?:予|将)?(?:公布|发布|印发)\s*" + BOOK)
ISSUE_AFTER = re.compile(r"现[^。；\n《]{0,24}" + BOOK + r"[^。；\n]{0,16}(?:公布|发布|印发)")
ISSUE_NAMED = re.compile(r"(?:新修订的|修订后的|修订的)\s*" + BOOK + r"[^。；]{0,120}现予以发布")
PAIR = re.compile(BOOK + r"[^。；\n《]{0,100}?(?:替代|取代)\s*" + BOOK)
DATE = r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?"
START_DATE = re.compile(r"(?:自|于)\s*" + DATE + r"\s*起?[^。；\n]{0,12}(?:施行|实施|执行|生效)")
PUBLICATION_START = re.compile(r"自(?:公布|发布|印发)之日起(?:施行|实施|执行|生效)")
UNCERTAIN = re.compile(r"(?:尚未|并未|不会|不能|不应|不得|不予|没有|未予|拟予|拟将|建议|可能|征求意见|草案|不再废止|(?:不|未|未被)(?:予以|被|直接|同时)?(?:废止|替代|停止))")
BINDING_KEYS = ("version_id", "revision", "access_epoch", "content_sha256")
ISSUER_ALIASES = {"中国证券监督管理委员会": "中国证监会", "中国证券投资基金业协会": "中基协",
    "中国银行保险监督管理委员会": "中国银保监会", "中华人民共和国财政部": "财政部"}


def _title(text):
    value = unicodedata.normalize("NFKC", text or "")
    value = re.sub(r"\s|[\u200b\ufeff]", "", value)
    value = re.sub(r"\.(?:pdf|docx?|html?|txt|md)$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\((?:官方发布页|转排阅读稿)\)$", "", value)
    return value.strip("《》:：")


def _title_keys(title):
    """Exact title variants, not semantic/fuzzy nearest-document selection."""
    value = re.split(r"[—–]附件[:：]", title or "")[-1]
    value = re.sub(r"^(?:附件\d*[:：])+(?:《)?", "", value)
    value = _title(value)
    keys = {value} if value else set()
    # Normalize recognized issuer abbreviations, not arbitrary issuer removal:
    # equal topic titles from two different regulators must remain different.
    for long, short in ISSUER_ALIASES.items():
        if value.startswith(long):
            keys.add(short + value[len(long):])
    return keys


def _calendar(groups):
    try:
        return date(*(int(x) for x in groups)).isoformat()
    except (ValueError, TypeError):
        return None


def _clean_context(text):
    return BOOK_RE.sub("《文件》", text)


def extract_facts(records):
    """Conservative grammatical extraction; no model, business-keyword rules or writes."""
    facts = []
    issuance = []
    for row in records:
        if UNCERTAIN.search(_clean_context(row["text"])):
            continue
        for match in [*ISSUE.finditer(row["text"]), *ISSUE_AFTER.finditer(row["text"]), *ISSUE_NAMED.finditer(row["text"])]:
            title = match[1]
            if not re.search(r"废止|清理|失效目录|目录|清单", title):
                issuance.append((row["ordinal"], title))
    for row in records:
        # Keep paragraph boundaries for locator binding, but a complete clause
        # can include multiple titles and parentheses before the repeal verb.
        for sentence in re.split(r"(?<=[。；;])", row["text"]):
            plain = _clean_context(sentence)
            if UNCERTAIN.search(plain):
                continue
            pair = PAIR.search(sentence)
            if pair:
                candidates = [(pair[2], pair[1], "partial" if re.search(r"部分|第[^，。；]{1,12}[条款项]", plain) else "full")]
            else:
                repeal = REPEAL.search(sentence)
                if not repeal:
                    continue
                before = sentence[:repeal.start()]
                books = list(BOOK_RE.finditer(before))
                issued_here = [match[1] for match in [*ISSUE.finditer(sentence), *ISSUE_AFTER.finditer(sentence), *ISSUE_NAMED.finditer(sentence)]]
                previous = [title for ordinal, title in issuance if ordinal <= row["ordinal"]]
                titles = set(issued_here or previous)
                successor = next(iter(titles)) if len(titles) == 1 else None
                old_books = [match for match in books if match[1] not in issued_here]
                if not old_books:
                    continue
                candidates = []
                for index, book in enumerate(old_books):
                    end = old_books[index + 1].start() if index + 1 < len(old_books) else len(before)
                    tail = _clean_context(before[book.end():end])
                    partial = bool(re.search(r"部分|第[^，。；《]{1,12}[条款项]", tail))
                    candidates.append((book[1], successor, "partial" if partial else "full"))
            all_dates = {value for item in records for match in START_DATE.finditer(item["text"])
                if (value := _calendar(match.groups()))}
            invalid_explicit_date = any(_calendar(match.groups()) is None for item in records for match in START_DATE.finditer(item["text"]))
            effective = next(iter(all_dates)) if len(all_dates) == 1 and not invalid_explicit_date else None
            # Only an explicit 'effective on publication' clause lets a unique
            # dated signature be used. Website generation/upload dates cannot.
            if not all_dates and not invalid_explicit_date and any(PUBLICATION_START.search(item["text"]) for item in records):
                signatures = set()
                for item in records:
                    if item["ordinal"] < row["ordinal"]:
                        continue
                    match = re.fullmatch(r"\s*" + DATE + r"\s*", item["text"])
                    if match and (value := _calendar(match.groups())):
                        signatures.add(value)
                effective = next(iter(signatures)) if len(signatures) == 1 else None
            for predecessor, successor, scope in candidates:
                if predecessor == successor:
                    continue
                facts.append({"predecessor_title": predecessor, "successor_title": successor or "后继规则待匹配",
                    "effective_from": effective, "scope": scope, "evidence": row,
                    "reason": "系统从来源中的明确替代条款预填，请核对版本、日期和适用范围：" + sentence.strip()})
    return facts


def suggestions(db, user, space_id):
    from .reference_evidence import reference_evidence
    from .wiki_catalog import build_catalog
    if isinstance(user, str):
        user = db.get(m.User, user)
    svc.space_access(db, user, space_id)
    pages = build_catalog(db, user, space_id, scope="reference")
    documents = {p["version_id"]: p for p in pages.values() if p["kind"] == "document"}
    titles = defaultdict(set)
    for vid, page in documents.items():
        for text in [page["title"], *page.get("aliases", [])]:
            for key in _title_keys(text):
                titles[key].add(vid)
    read_cache, bindings = {}, {}

    def read(vid):
        if vid not in read_cache:
            values = reference_evidence(db, user, space_id, {}, reading=True, version_ids={vid})
            if not values:
                svc.fail(404, "SOURCE_PREFILL_UNAVAILABLE", "预填来源当前不可读取")
            version = svc.version_access(db, user, vid)
            snap = {**authority._header(db, version), "content_sha256": svc.check_frozen_hash(db, version)}
            bindings[vid] = {key: snap[key] for key in BINDING_KEYS}
            read_cache[vid] = sorted(values, key=lambda row: (row["ordinal"], row["block_id"]))
        return read_cache[vid]

    def match(title, *, exclude=()):
        candidates = set()
        for key in _title_keys(title):
            candidates |= titles[key]
        candidates -= set(exclude)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def finish(item):
        present = {item[key] for key in authority.VERSION_KEYS if item.get(key)}
        for vid in present:
            read(vid)
        item["expected_sources"] = [bindings[vid] for vid in sorted(present)]
        item["missing_fields"] = [key for key in (*authority.VERSION_KEYS, "effective_from", "scope") if not item.get(key)]
        item["status"] = "CONFIRMED" if item.get("existing_record_id") else "NEEDS_INPUT" if item["missing_fields"] else "READY"
        item["id"] = svc.digest(["source-authority-prefill-v1", space_id,
            {k: v for k, v in item.items() if k != "id"}])
        return item

    items, registered_keys, blocked_pairs, notes = [], set(), set(), []
    for row in authority._rows(db, space_id):
        c = row.config
        try:
            shown = authority._present(db, user, row)
            pair = (c["predecessor_version_id"], c["successor_version_id"])
            if c["state"] == "REVOKED":
                blocked_pairs.add(pair)
                continue  # Do not repeatedly recommend a fact the user revoked.
            registered_keys.add((*pair, c["effective_from"], c["scope"]))
            proof = read(c["evidence_version_id"])
            matches = [record for record in proof if REPEAL.search(record["text"]) or "替代" in record["text"]]
            selected = matches or proof[:1]
            items.append(finish({"space_id": space_id, "origin": "registered_fact", "existing_record_id": row.id,
                "existing_validation_state": shown["validation_state"],
                **{key: shown[key] for key in (*authority.VERSION_KEYS, "predecessor_title", "successor_title", "evidence_title", "effective_from", "scope", "reason")},
                "excerpts": [{k: value[k] for k in ("version_id", "block_id", "text", "locator")} for value in selected]}))
        except (svc.APIError, KeyError, TypeError):
            notes.append("部分绑定来源当前不可核对，未复用其预填内容。")
    # SQL batches are an execution bound, never a cap on accessible documents.
    proof_ids = set()
    ids = list(documents)
    for start in range(0, len(ids), 400):
        for vid, text in db.execute(select(m.ContentBlock.version_id, m.ContentBlock.search_text).where(
                m.ContentBlock.version_id.in_(ids[start:start + 400]),
                or_(m.ContentBlock.search_text.contains("废止"), m.ContentBlock.search_text.contains("替代"),
                    m.ContentBlock.search_text.contains("停止执行"), m.ContentBlock.search_text.contains("停止适用")))):
            if BOOK_RE.search(text):
                proof_ids.add(vid)
    seen = set()
    for proof_id in sorted(proof_ids):
        try:
            facts = extract_facts(read(proof_id))
        except svc.APIError:
            continue
        for fact in facts:
            if fact["successor_title"] == "后继规则待匹配" and any(
                    re.search(r"本(?:规则|办法|规定|指引|通知|标准)[^。；\n]{0,24}自", row["text"])
                    and START_DATE.search(row["text"]) for row in read(proof_id)):
                fact["successor_title"] = documents[proof_id]["title"]
            new = match(fact["successor_title"])
            old = match(fact["predecessor_title"], exclude=[new, proof_id])
            key = (old, new, fact["effective_from"], fact["scope"])
            if key in registered_keys or (old, new) in blocked_pairs:
                continue
            duplicate = (key, proof_id, fact["predecessor_title"], fact["successor_title"])
            if duplicate in seen:
                continue
            seen.add(duplicate)
            row = fact["evidence"]
            item = {"space_id": space_id, "origin": "source_text", "existing_record_id": None,
                "existing_validation_state": None, "predecessor_version_id": old, "successor_version_id": new,
                "evidence_version_id": proof_id, "predecessor_title": documents[old]["title"] if old else fact["predecessor_title"],
                "successor_title": documents[new]["title"] if new else fact["successor_title"],
                "evidence_title": documents[proof_id]["title"], "effective_from": fact["effective_from"],
                "scope": fact["scope"], "reason": (f"系统预填：核对《{fact['predecessor_title']}》与《{fact['successor_title']}》的"
                    f"{'全部' if fact['scope'] == 'full' else '部分'}替代关系，起始日期{fact['effective_from'] or '待确认'}。"
                    f"依据《{documents[proof_id]['title']}》中的条款；请核对下方原文，不代表全文现行效力或专家审核。"),
                "excerpts": [{key: row[key] for key in ("version_id", "block_id", "text", "locator")} ]}
            try:
                items.append(finish(item))
            except svc.APIError:
                continue
    items.sort(key=lambda item: ({"READY": 0, "NEEDS_INPUT": 1, "CONFIRMED": 2}[item["status"]], item["predecessor_title"], item["id"]))
    return {"items": items, "can_manage": "admin" in svc.roles(db, user, space_id),
        "notes": ["已根据可读来源预填；只有你明确确认后才登记生效。未唯一识别的要素保留待补，不从上传日期推断法规日期。", *sorted(set(notes))]}
