"""Rule-extracted source metadata candidates: issuer, tier, dates, abolition, versions.

Candidates are reasoning hints, never confirmed facts. Each one is bound to the
exact version content hash and to the located block ids. Extraction never
changes legal_status/valid_from/RelationEdge, never replaces an administrator's
source-authority fact, and never calls a model.
"""
from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select

from . import models as m
from . import services as svc

EXTRACTOR_VERSION = "source-metadata-rules-v2-20261001"
PREFIX = "source-metadata:"
STATUS = "EXTRACTED_UNCONFIRMED"

# (canonical, full names usable anywhere, short forms matched in the title only).
# Longer / more specific names first: "中证协" must win over "中证".
_ISSUERS = (
    ("全国人大常委会", ("全国人民代表大会常务委员会",), ("全国人大常委会",)),
    ("中国证监会", ("中国证券监督管理委员会",), ("证监会",)),
    ("财政部", ("中华人民共和国财政部",), ("财政部",)),
    ("中国人民银行", ("中国人民银行",), ()),
    ("中国证券投资基金业协会", ("中国证券投资基金业协会",), ("中基协", "基金业协会")),
    ("中国证券业协会", ("中国证券业协会",), ("中证协",)),
    ("中国银行间市场交易商协会", ("中国银行间市场交易商协会",), ("交易商协会",)),
    ("上海证券交易所", ("上海证券交易所",), ("上交所",)),
    ("深圳证券交易所", ("深圳证券交易所",), ("深交所",)),
    ("北京证券交易所", ("北京证券交易所",), ("北交所",)),
    ("全国中小企业股份转让系统", ("全国中小企业股份转让系统",), ()),
    ("中国金融期货交易所", ("中国金融期货交易所",), ("中金所",)),
    ("上海国际能源交易中心", ("上海国际能源交易中心",), ()),
    ("上海期货交易所", ("上海期货交易所",), ()),
    ("郑州商品交易所", ("郑州商品交易所",), ()),
    ("大连商品交易所", ("大连商品交易所",), ()),
    ("广州期货交易所", ("广州期货交易所",), ()),
    ("上海黄金交易所", ("上海黄金交易所",), ()),
    ("中国证券登记结算", ("中国证券登记结算",), ("中国结算",)),
    ("银行间市场清算所（上海清算所）", ("银行间市场清算所",), ("上海清算所",)),
    ("中央国债登记结算（中债）", ("中央国债登记结算",), ("中债",)),
    ("中国外汇交易中心", ("中国外汇交易中心",), ("外汇交易中心", "交易中心")),
    ("中证指数", ("中证指数有限公司",), ("中证",)),
    ("深圳证券信息（国证）", ("深圳证券信息有限公司",), ("国证债券",)),
)
_EXCHANGES = {"上海证券交易所", "深圳证券交易所", "北京证券交易所", "全国中小企业股份转让系统", "中国金融期货交易所",
              "上海国际能源交易中心", "上海期货交易所", "郑州商品交易所", "大连商品交易所", "广州期货交易所",
              "上海黄金交易所", "中国证券登记结算"}
_PRICING_VENDORS = {"银行间市场清算所（上海清算所）", "中央国债登记结算（中债）", "中国外汇交易中心", "中证指数",
                    "深圳证券信息（国证）"}
_SELF_REGULATORS = {"中国证券投资基金业协会", "中国证券业协会", "中国银行间市场交易商协会"}

TIER_ORDER = ("法律", "证监会/人民银行规章及规范性文件", "会计准则及财政部会计规定", "行业自律规则", "交易场所业务规则",
              "估值服务机构方法与数据说明", "估值服务质量报告", "实务手册与案例", "其他来源")

_DATE = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
_EFFECTIVE = re.compile(r"自\s*(?:" + _DATE + r"|(?:公布|发布|印发|颁布)之日)\s*起[^。；\n]{0,8}?(施行|执行|实施|生效)")
# "自发布之日起至2023年3月31日实施完毕": a transition/implementation deadline, not an expiry.
_TRANSITION = re.compile(r"至\s*" + _DATE + r"\s*(?:前|止)?\s*(?:实施完毕|完成实施|过渡期(?:结束|届满)?)")
# Who a document is written for, from its title only (a hint, never an applicability ruling).
_SUBJECTS = (("证券公司", "证券公司"), ("私募", "私募基金"), ("资产管理产品", "资管产品"), ("货币市场基金", "货币市场基金"),
             ("基金中基金", "基金中基金"), ("公开募集", "公募基金"), ("证券投资基金", "证券投资基金"),
             ("银行间", "银行间市场"), ("REITs|不动产投资信托", "公募REITs"))
_ABOLISH = re.compile(r"(同时废止|予以废止|即行废止|废止[。；]?$|(?<!不)停止执行)")
_DOC_NUMBER = re.compile(r"([一-龥A-Za-z]{1,12}\s*[〔\[【]\s*(?:19|20)\d{2}\s*[〕\]】]\s*第?\s*\d{1,4}\s*号"
                         r"|(?:中国证券监督管理委员会|证监会)令\s*第\s*\d{1,4}\s*号)")
_TITLE = re.compile(r"《((?:[^《》]|《[^《》]*》)+)》")
_PUBLISHES = re.compile(r"(?:现|特此|现予|现将)?(?:公布|发布|印发)(?:实施)?(?:《((?:[^《》]|《[^《》]*》)+)》)")
_VERSION = re.compile(r"[（(]\s*(?:(\d{4})\s*年(?:\s*(\d{1,2})\s*月)?\s*(?:修订版|修订稿|修订|版|历史版))\s*[）)]"
                      r"|(\d{4})\s*年\s*(\d{1,2})\s*月版"
                      r"|[（(]\s*V\s*(\d+)(?:\.(\d+))?\s*[）)]"
                      r"|[—\-－]\s*((?:19|20)\d{2})\b")
_NOISE = re.compile(r"[（(](?:\d{4}\s*年\s*\d{1,2}\s*月\s*)?快照[）)]|[（(]正文[）)]|[（(]官方发布页[）)]|[（(]规则库页面[）)]"
                    r"|[（(]新闻稿摘要[）)]|[（(]转排阅读稿[）)]|[（(]已修订[）)]|\.pdf$|\.docx?$|\.xlsx$")
_PUNCT = re.compile(r"[\s《》〈〉<>“”\"'‘’（）()\[\]【】—\-－_:：·、，,.。;；]")


def _norm(text):
    text = (text or "").replace("中国证券监督管理委员会", "中国证监会")
    return _PUNCT.sub("", text).casefold()


def _date(match_groups):
    year, month, day = match_groups
    try:
        year, month, day = int(year), int(month), int(day)
    except (TypeError, ValueError):
        return None
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _issuer(title, blocks):
    for name, full, short in _ISSUERS:
        if any(p in (title or "") for p in (*full, *short)):
            return {"value": name, "located": "title"}
    # Body text: full names only; short forms are too ambiguous ("中华人民共和国证券" contains "国证").
    for bid, text in (*blocks[:3], *blocks[-5:]):
        for name, full, _ in _ISSUERS:
            if any(p in (text or "") for p in full):
                return {"value": name, "located": "block", "block_id": bid}
    return None


def _tier(title, category, issuer):
    name = (issuer or {}).get("value")
    if re.match(r"^中华人民共和国.{1,30}法", title) and "细则" not in title:
        return "法律", "title"
    if re.search(r"企业会计准则|会计处理规定|金融工具准则|会计准则", title) and name in (None, "财政部"):
        return "会计准则及财政部会计规定", "title"
    if re.search(r"质量分析报告|质量检验|质量投诉", title):
        return "估值服务质量报告", "title"
    if name in {"中国证监会", "中国人民银行"}:
        return "证监会/人民银行规章及规范性文件", "issuer"
    if name in _SELF_REGULATORS:
        return "行业自律规则", "issuer"
    if name in _EXCHANGES:
        return "交易场所业务规则", "issuer"
    if name in _PRICING_VENDORS or name == "财政部" and "收益率曲线" in title:
        return "估值服务机构方法与数据说明", "issuer"
    if re.search(r"手册|操作实务|案例", title):
        return "实务手册与案例", "title"
    if re.search(r"管理办法|管理规定|指导意见|编报规则", title):
        return "证监会/人民银行规章及规范性文件", "title"
    if re.search(r"证券投资基金.{0,30}(?:指引|规定|标准|细则)|估值.{0,12}(?:指引|处理标准)", title):
        return "行业自律规则", "title"
    if re.search(r"估值方法|编制说明|编制方法|方法说明|估值服务|收益率曲线|估值产品|估值手册", title):
        return "估值服务机构方法与数据说明", "title"
    return "其他来源", "default"


def _status_hints(title, category):
    hints = []
    if (category or "").endswith("历史参考与征求意见"):
        hints.append("资料分类：历史参考与征求意见")
    for pattern, hint in ((r"征求意见", "征求意见稿"), (r"试行", "试行"), (r"快照", "网页快照"),
                          (r"暂缓实施", "暂缓实施说明"), (r"已修订|历史版", "已修订/历史版本"),
                          (r"新闻稿|摘要", "新闻稿或摘要（非正式全文）")):
        if re.search(pattern, title):
            hints.append(hint)
    return hints


def _version(title):
    match = None
    for match in _VERSION.finditer(title):
        pass
    if not match:
        return None, None
    g = match.groups()
    if g[0]:
        return match.group(0).strip(), [int(g[0]), int(g[1] or 0)]
    if g[2]:
        return match.group(0).strip(), [int(g[2]), int(g[3])]
    if g[4]:
        return match.group(0).strip(), [0, int(g[4]), int(g[5] or 0)]
    return match.group(0).strip(), [int(g[6]), 0]


def base_title(title):
    text = _NOISE.sub("", title or "")
    text = _VERSION.sub("", text)
    text = re.sub(r"^QCCDC\s*\d+\s*", "", text)
    return _norm(text)


def extract(title, category, blocks):
    """blocks: ordered (block_id, text). Returns a candidate dict without identity fields."""
    blocks = [(bid, text or "") for bid, text in blocks]
    issuer = _issuer(title, blocks)
    tier, basis = _tier(title, category, issuer)
    label, key = _version(title)
    numbers, effective, abolition, publishes = [], [], [], []
    seen_numbers = set()
    for index, (bid, text) in enumerate(blocks):
        for raw in _DOC_NUMBER.findall(text):
            number = re.sub(r"\s+", "", raw)
            if number not in seen_numbers and len(numbers) < 8:
                seen_numbers.add(number)
                numbers.append({"text": number, "block_id": bid})
        # Source lines often break inside a sentence or a document number.
        flat = re.sub(r"\s*\n\s*", "", text)
        for sentence in re.split(r"(?<=[。；])", flat):
            sentence = sentence.strip()
            if not sentence:
                continue
            for match in _EFFECTIVE.finditer(sentence):
                if len(effective) < 4:
                    date = _date(match.groups()[:3]) if match.group(1) else None
                    item = {"text": match.group(0).replace(" ", "")[:80], "block_id": bid,
                            "date": date, "mode": "date" if date else "on_publication"}
                    if not date:
                        # "自公布之日起施行": the signing date nearest after the clause is a derived hint.
                        tail = [flat[flat.find(sentence) + len(sentence):]] + [t for _, t in blocks[index + 1:index + 4]]
                        found = next((d for part in tail for d in [re.search(_DATE, part)] if d), None)
                        if found and _date(found.groups()):
                            item["derived_date"] = _date(found.groups())
                    effective.append(item)
            for match in _TRANSITION.finditer(sentence):
                date = _date(match.groups()[:3])
                if date and len(effective) < 4:
                    effective.append({"text": match.group(0).replace(" ", "")[:80], "block_id": bid, "date": date,
                                      "mode": "transition_deadline"})
            keyword = _ABOLISH.search(sentence)
            if keyword and "不停止执行" not in sentence and len(abolition) < 6:
                # Only titles in the abolished clause: after the last "施行/实施" before the keyword.
                head = sentence[:keyword.start()]
                # Titles may contain "执行" themselves; search the cut word outside 《》 only.
                masked = _TITLE.sub(lambda match: "　" * len(match.group(0)), head)
                cut = max(masked.rfind(word) for word in ("施行", "实施", "执行", "生效"))
                clause = head[cut + 2:] if cut >= 0 else head
                targets = [{"title": t.strip()} for t in _TITLE.findall(clause)]
                for target in targets:
                    tail = clause.split(target["title"], 1)[-1][:40]
                    number = _DOC_NUMBER.search(tail)
                    if number:
                        target["document_number"] = re.sub(r"\s+", "", number.group(0))
                if targets:
                    abolition.append({"text": sentence[:160], "block_id": bid, "targets": targets})
            for match in _PUBLISHES.finditer(sentence):
                if len(publishes) < 4 and match.group(1):
                    publishes.append({"title": match.group(1).strip(), "block_id": bid})
    signature = None
    for bid, text in reversed(blocks[-5:]):
        dates = list(re.finditer(_DATE, text))
        if dates:
            date = _date(dates[-1].groups())
            if date:
                signature = {"date": date, "block_id": bid}
                break
    subjects = list(dict.fromkeys(label for pattern, label in _SUBJECTS if re.search(pattern, title)))
    return {"issuer": issuer, "tier": tier, "tier_basis": basis, "status_hints": _status_hints(title, category),
            "subjects": subjects,
            "version_label": label, "version_key": key, "base_title": base_title(title),
            "document_numbers": numbers, "effective_statements": effective, "signature_date": signature,
            "abolition_statements": abolition, "publishes": publishes}


def _matches(target_title, candidate):
    """Title reference -> library document. Explicitly different versions never match."""
    target_full = _norm(_NOISE.sub("", target_title))
    target_base = base_title(target_title)
    target_key = _version(target_title)[1]
    title = _norm(_NOISE.sub("", candidate["title"]))
    if len(target_base) < 6 or not title:
        return False
    if target_key and candidate["version_key"] and target_key != candidate["version_key"]:
        return False
    if target_key and not candidate["version_key"]:
        return target_full == title
    return (target_full == title or target_base == candidate["base_title"]
            or len(target_base) >= 10 and target_base in title)


def link(entries):
    """Cross-document hints inside one space. entries: version_id -> candidate (mutated)."""
    for entry in entries.values():
        entry["links"] = {"abolishes": [], "abolished_by": [], "newer_versions": [], "older_versions": [],
                          "publishes": [], "published_by": []}
    for vid, entry in entries.items():
        for statement in entry["abolition_statements"]:
            for target in statement["targets"]:
                for other_id, other in entries.items():
                    if (other_id != vid and _matches(target["title"], other)
                            and other_id not in entry["links"]["abolishes"]):
                        entry["links"]["abolishes"].append(other_id)
                        other["links"]["abolished_by"].append(vid)
        for item in entry["publishes"]:
            for other_id, other in entries.items():
                if other_id != vid and _matches(item["title"], other) and other_id not in entry["links"]["publishes"]:
                    entry["links"]["publishes"].append(other_id)
                    other["links"]["published_by"].append(vid)
    groups = defaultdict(list)
    for vid, entry in entries.items():
        if entry["base_title"] and len(entry["base_title"]) >= 6:
            groups[entry["base_title"]].append(vid)
    for members in groups.values():
        if len(members) < 2:
            continue
        keyed = [vid for vid in members if entries[vid]["version_key"]]
        for vid in keyed:
            for other in keyed:
                mine, theirs = entries[vid]["version_key"], entries[other]["version_key"]
                # Only order comparable labels: year/month vs year/month, V-number vs V-number.
                if other != vid and len(mine) == len(theirs) and (mine[0] == 0) == (theirs[0] == 0) and theirs > mine:
                    entries[vid]["links"]["newer_versions"].append(other)
                    entries[other]["links"]["older_versions"].append(vid)
    return entries


def _latest_documents(db, space_id):
    resources = list(db.scalars(select(m.Resource).where(m.Resource.space_id == space_id,
        m.Resource.kind == "document", m.Resource.deleted_at.is_(None))))
    latest = {}
    for version in db.scalars(select(m.ResourceVersion).where(
            m.ResourceVersion.resource_id.in_([r.id for r in resources]))):
        if version.resource_id not in latest or version.version_no > latest[version.resource_id].version_no:
            latest[version.resource_id] = version
    return {r.id: r for r in resources}, latest


def extract_space(db, space_id):
    """Pure computation over the space's latest document versions (no writes)."""
    resources, latest = _latest_documents(db, space_id)
    entries = {}
    for rid, version in latest.items():
        blocks = []
        for bid, data, search in db.execute(select(m.ContentBlock.block_id, m.ContentBlock.data,
                m.ContentBlock.search_text).where(m.ContentBlock.version_id == version.id)
                .order_by(m.ContentBlock.ordinal)):
            text = data.get("text") if isinstance(data, dict) else None
            blocks.append((bid, text if isinstance(text, str) else search or ""))
        if not blocks:
            continue
        entry = extract(version.title, resources[rid].category, blocks)
        entry.update(schema_version=1, extractor_version=EXTRACTOR_VERSION, status=STATUS, space_id=space_id,
                     resource_id=rid, version_id=version.id, content_sha256=version.content_sha256,
                     title=version.title, category=resources[rid].category or "")
        entries[version.id] = entry
    return link(entries)


def store(db, entries, actor_id, *, trace_id):
    """Upsert one policy per version; returns (created, updated, unchanged)."""
    created = updated = unchanged = 0
    names = {PREFIX + vid: entry for vid, entry in entries.items()}
    existing = {p.name: p for p in db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.in_(list(names))))}
    stamp = svc.primitive(svc.now())
    for name, entry in names.items():
        policy = existing.get(name)
        comparable = {k: v for k, v in (policy.config if policy else {}).items() if k != "extracted_at"}
        if policy and comparable == entry:
            unchanged += 1
            continue
        config = {**entry, "extracted_at": stamp}
        if policy:
            svc.bump(db, policy, config=config, updated_by=actor_id)
            updated += 1
        else:
            db.add(m.RuntimePolicy(id=svc.uid(), name=name, config=config, updated_by=actor_id))
            created += 1
    db.add(m.AuditEvent(id=svc.uid(), actor_id=actor_id, action="source_metadata.extracted", object_type="system",
        object_id=None, outcome="SUCCESS", trace_id=trace_id, details={"extractor_version": EXTRACTOR_VERSION,
            "versions": len(entries), "created": created, "updated": updated, "unchanged": unchanged,
            "legal_status_changed": False, "confirmed_facts_changed": False, "model_calls": 0}))
    return created, updated, unchanged


def load(db, version_ids):
    """Current candidates only: a changed version hash makes its candidate invisible."""
    version_ids = list(dict.fromkeys(version_ids))
    if not version_ids:
        return {}
    hashes = {}
    for start in range(0, len(version_ids), 400):
        ids = version_ids[start:start + 400]
        hashes.update(dict(db.execute(select(m.ResourceVersion.id, m.ResourceVersion.content_sha256)
            .where(m.ResourceVersion.id.in_(ids))).all()))
    result = {}
    for start in range(0, len(version_ids), 400):
        names = [PREFIX + vid for vid in version_ids[start:start + 400]]
        for policy in db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.in_(names))):
            config = policy.config or {}
            vid = policy.name.removeprefix(PREFIX)
            if (config.get("extractor_version") == EXTRACTOR_VERSION and config.get("status") == STATUS
                    and hashes.get(vid) and config.get("content_sha256") == hashes[vid]):
                result[vid] = config
    return result
