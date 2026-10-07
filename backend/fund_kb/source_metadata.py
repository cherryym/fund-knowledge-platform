"""Rule-extracted source metadata candidates: issuer, tier, dates, abolition, versions.

Candidates are reasoning hints, never confirmed facts. Each one is bound to the
exact version content hash and to the located block ids. Extraction never
changes legal_status/valid_from/RelationEdge, never replaces an administrator's
source-authority fact, and never calls a model.

A document's own identity (issuer, document number, publication date) is read
only from its own declarations: the title, a rule-library metadata table, the
header publication record, standalone header/signature lines. Website chrome
(copyright, ICP) and documents it cites never count.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from sqlalchemy import select

from . import models as m
from . import services as svc

EXTRACTOR_VERSION = "source-metadata-rules-v3-20261007"
PREFIX = "source-metadata:"
STATUS = "EXTRACTED_UNCONFIRMED"

# (canonical, full names, short forms). In a title the earliest name wins (a joint
# title names its lead issuer first); at equal positions the longer name wins.
_ISSUERS = (
    ("全国人大常委会", ("全国人民代表大会常务委员会",), ("全国人大常委会",)),
    ("国务院", ("中华人民共和国国务院",), ("国务院",)),
    ("中国证监会", ("中国证券监督管理委员会",), ("中国证监会", "证监会")),
    ("财政部", ("中华人民共和国财政部",), ("财政部",)),
    ("国家税务总局", ("国家税务总局",), ("税务总局",)),
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
    ("中债资信评估", ("中债资信评估有限责任公司",), ("中债资信",)),
    ("中央国债登记结算（中债）", ("中央国债登记结算",), ("中债",)),
    ("中国外汇交易中心", ("中国外汇交易中心",), ("外汇交易中心", "交易中心")),
    ("中证指数", ("中证指数有限公司",), ("中证",)),
    ("深圳证券信息（国证）", ("深圳证券信息有限公司",), ("国证债券",)),
)
_EXCHANGES = {"上海证券交易所", "深圳证券交易所", "北京证券交易所", "全国中小企业股份转让系统", "中国金融期货交易所",
              "上海国际能源交易中心", "上海期货交易所", "郑州商品交易所", "大连商品交易所", "广州期货交易所",
              "上海黄金交易所", "中国证券登记结算"}
_PRICING_VENDORS = {"银行间市场清算所（上海清算所）", "中债资信评估", "中央国债登记结算（中债）", "中国外汇交易中心",
                    "中证指数", "深圳证券信息（国证）"}
_SELF_REGULATORS = {"中国证券投资基金业协会", "中国证券业协会", "中国银行间市场交易商协会"}
# Own document number prefix -> issuer (used only when the title names none).
_NUMBER_ISSUERS = ((r"中国证券监督管理委员会|证监", "中国证监会"), (r"中基协|中国基金业协会", "中国证券投资基金业协会"),
                   (r"中证协", "中国证券业协会"), (r"中国人民银行|人民银行|银发", "中国人民银行"),
                   (r"财会|财税|财金|财政部", "财政部"), (r"国家税务总局|税务总局|国税", "国家税务总局"),
                   (r"国务院|国发|国办发", "国务院"), (r"上证", "上海证券交易所"), (r"深证", "深圳证券交易所"),
                   (r"北证", "北京证券交易所"), (r"股转", "全国中小企业股份转让系统"), (r"中金所", "中国金融期货交易所"),
                   (r"上期", "上海期货交易所"), (r"郑商", "郑州商品交易所"), (r"大商", "大连商品交易所"),
                   (r"广期所", "广州期货交易所"), (r"交易商协会|中市协", "中国银行间市场交易商协会"),
                   (r"中国结算", "中国证券登记结算"))

TIER_ORDER = ("法律", "行政法规", "证监会/人民银行规章及规范性文件", "会计准则及财政部会计规定", "财税规范性文件",
              "行业自律规则", "交易场所业务规则", "估值服务机构方法与数据说明", "估值服务质量报告", "实务手册与案例",
              "其他来源")

_DATE = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
_CN_DATE = r"[○〇零一二三四五六七八九]{4}\s*年\s*[一二三四五六七八九十]{1,2}\s*月\s*[一二三四五六七八九十]{1,3}\s*日"
# "自2014年7月1日起停止执行" ends a rule; it is not an effective date.
_EFFECTIVE = re.compile(r"自\s*(?:" + _DATE + r"|(?:公布|发布|印发|颁布)之日)\s*起(?:(?!停止|不再)[^。；\n]){0,8}?(施行|执行|实施|生效)")
# "自发布之日起至2023年3月31日实施完毕": a transition/implementation deadline, not an expiry.
_TRANSITION = re.compile(r"至\s*" + _DATE + r"\s*(?:前|止)?\s*(?:实施完毕|完成实施|过渡期(?:结束|届满)?)")
# Who a document is written for, from its title only (a hint, never an applicability ruling).
_SUBJECTS = (("证券公司", "证券公司"), ("私募", "私募基金"), ("资产管理产品", "资管产品"), ("货币市场基金", "货币市场基金"),
             ("基金中基金", "基金中基金"), ("公开募集", "公募基金"), ("证券投资基金", "证券投资基金"),
             ("银行间", "银行间市场"), ("REITs|不动产投资信托", "公募REITs"))
_ABOLISH = re.compile(r"(同时废止|予以废止|即行废止|废止[。；]?$|(?<!不)停止执行)")
_NUMBER_CORE = r"[〔\[【［]\s*(?:19|20)\d{2}\s*[〕\]】］]\s*第?\s*\d{1,4}\s*号"
_DOC_NUMBER = re.compile(r"([一-龥A-Za-z]{1,16}\s*" + _NUMBER_CORE +
                         r"|(?:中国证券监督管理委员会|证监会|国务院|中华人民共和国主席)令\s*第\s*\d{1,4}\s*号"
                         r"|(?:财政部\s*(?:国家)?税务总局|国家税务总局|税务总局|财政部)公告\s*(?:19|20)\d{2}\s*年\s*第\s*\d{1,4}\s*号)")
_BARE_NUMBER = re.compile(_NUMBER_CORE)
_TITLE = re.compile(r"《((?:[^《》]|《[^《》]*》)+)》")
_TITLES = r"((?:《(?:[^《》]|《[^《》]*》)+》[、和及与]?)+)"
# Publication is read only from the document's own declaration ("现公布《…》", "现将《…》予以发布") or
# from its own title ("关于发布《…》的通知"); a history sentence ("财政部修订印发了《…》") is not one.
_DECLARED_PUBLISH = (re.compile(r"(?:现|特此|现予|兹)(?:予以)?(?:公布|发布|印发)(?:实施)?" + _TITLES),
                     re.compile(r"现将" + _TITLES + r"(?:予以)?(?:公布|发布|印发)"))
_TITLE_PUBLISH = re.compile(r"^关于(?:修订)?(?:发布|印发|公布)(?:实施)?" + _TITLES)
# "…通知—附件：附件1：《…》": an annex is part of its notice; it has no number of its own and publishes nothing.
_ANNEX = re.compile(r"[—\-－]\s*附件|附件[\d一二三四五六七八九十]*\s*[：:]")
# A header publication record: "(2007年6月18日证监会令第46号公布 自2007年7月5日起施行)".
_RECORD = re.compile(r"[（(][^（）()《》]{0,140}?(?:公布|发布|印发|颁布)[^（）()《》]{0,60}[）)]")
_PUBLISH_VERB = re.compile(r"公布|发布|印发|颁布")
# Rule-library and law-database metadata tables (one cell per tab; an empty cell stays empty).
_META = (("issuer", re.compile(r"【发布部门】\s*([^\t\n【】]*)|(?<![一-龥])发文单位\t([^\t\n]*)")),
         ("number", re.compile(r"【发布文号】\s*([^\t\n【】]*)|(?<![一-龥])文号\t([^\t\n]*)")),
         ("published", re.compile(r"【发布日期】\s*([^\t\n【】]*)|(?<![一-龥])发文日期\t([^\t\n]*)")))
_ISO_DATE = re.compile(r"(\d{4})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})")
# Website chrome is never a signature or a header.
_CHROME = re.compile(r"版权|Copyright|©|ICP|公网安备|网站|主办|承办|技术支持|联系我们|地址|电话|邮箱|传真|邮编|微信|English|首页|登录")
_SIGNATURE_WORDS = re.compile(r"公告|文件|印发|发布|发文|[、，,\s　]")
_PARTIAL = re.compile(r"^\s*(?:[（(][^（）()]{0,40}[）)])?\s*(?:中)?\s*(?:第[一二三四五六七八九十百零〇\d、]+[条款项点章节]"
                      r"|第\s*[（(][一二三四五六七八九十\d]+[）)]\s*[项款]|“)")
_CONTAINER = re.compile(r"^\s*(?:[（(][^（）()]{0,40}[）)])?\s*中?\s*(?:的)?\s*附件\s*[\d一二三四五六七八九十]*\s*《")
_EXCEPT = re.compile(r"除[^。；]{0,240}?外")
_VERSION = re.compile(r"[（(]\s*(?P<y1>\d{4})\s*年(?:\s*(?P<m1>\d{1,2})\s*月)?\s*(?:修订版|修订稿|修订|修正|版|历史版)\s*[）)]"
                      r"|[（(]\s*(?P<y4>\d{4})\s*年\s*(?P<m4>\d{1,2})\s*月\s*[）)]"
                      r"|(?P<y2>\d{4})\s*年\s*(?P<m2>\d{1,2})\s*月版"
                      r"|[（(]\s*V\s*(?P<v>\d+)(?:\.(?P<v2>\d+))?\s*[）)]"
                      r"|[—\-－]\s*(?P<y3>(?:19|20)\d{2})\b")
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


def _named_issuer(text):
    """Earliest issuer name in a short text (a title, a table cell, a signature line)."""
    best = None
    for name, full, short in _ISSUERS:
        for pattern in (*full, *short):
            position = (text or "").find(pattern)
            if position >= 0 and (best is None or (position, -len(pattern)) < best[0]):
                best = ((position, -len(pattern)), name)
    return best[1] if best else None


def _clean_number(text, match):
    number = unicodedata.normalize("NFKC", re.sub(r"\s+", "", match.group(0)))
    # "2025年5月9日广期所发〔2025〕170号": the leading 日 belongs to the date before it.
    if number[:1] in "年月日" and re.search(r"\d\s*$", text[:match.start()]):
        number = number[1:]
    return number


def _fold(text):
    return unicodedata.normalize("NFKC", text or "")


def _numbers_in(text):
    text = _fold(text)
    return [_clean_number(text, match) for match in _DOC_NUMBER.finditer(text)]


def _meta_fields(blocks):
    """Values from a rule-library / law-database metadata table in the first blocks."""
    found = {}
    for _, text in blocks[:40]:
        for key, pattern in _META:
            match = pattern.search(text)
            value = next((g for g in match.groups() if g), "").strip() if match else ""
            if value and key not in found:
                found[key] = value
    return found


def _header_record(blocks):
    """First header publication record: (number, publication date). Two blocks are joined because
    PDF lines often split the record."""
    for index in range(min(len(blocks), 40)):
        if "\t" in blocks[index][1]:
            continue
        joined = _fold(re.sub(r"\s*\n\s*", "", blocks[index][1] + (blocks[index + 1][1] if index + 1 < len(blocks) else "")))
        for record in _RECORD.finditer(joined):
            text = record.group(0)
            if re.search(r"根据|依据|废止", text) or joined[:record.start()].rstrip().endswith("》"):
                continue
            match = _DOC_NUMBER.search(text) or _BARE_NUMBER.search(text)
            number = _clean_number(text, match) if match else None
            verb = _PUBLISH_VERB.search(text)
            dates = [d for d in re.finditer(_DATE, text) if d.end() <= verb.start()]
            # "2024年12月20日，经…理事会审议通过，…〔2025〕2号公告发布": an approval date is not the publication date.
            dates = [d for i, d in enumerate(dates)
                     if not re.search(r"审议|通过", text[d.end():dates[i + 1].start() if i + 1 < len(dates) else verb.start()])]
            published = _date(dates[-1].groups()) if dates else None
            if number or published:
                return number, published
    return None, None


def _standalone_number(blocks):
    for _, text in blocks[:40]:
        if "\t" in text:
            continue
        for line in _fold(text).split("\n"):
            line = line.strip()
            match = _DOC_NUMBER.search(line)
            if (match and len(line) <= len(match.group(0)) + 10 and "《" not in line
                    and not re.search(r"根据|依据|按照|参照|废止|关于", line)):
                return _clean_number(line, match)
    return None


def _own_number(title, blocks, meta):
    if meta.get("number"):
        found = _numbers_in(meta["number"])
        return found[0] if found else unicodedata.normalize("NFKC", re.sub(r"\s+", "", meta["number"]))[:40]
    if _ANNEX.search(title or ""):
        return None
    title = _fold(title)
    for match in _DOC_NUMBER.finditer(title):
        # "关于财税[2016]140号文件部分条款的政策解读" cites another document.
        if "关于" in title[max(0, match.start() - 3):match.start()] or title[match.end():].startswith("文件"):
            continue
        return _clean_number(title, match)
    record_number, _ = _header_record(blocks)
    return record_number or _standalone_number(blocks)


def _signature_lines(blocks):
    """Standalone header/signature lines (issuer name and/or date only), outside website chrome."""
    for bid, text in (*blocks[:8], *blocks[-8:]):
        for line in text.split("\n"):
            line = line.strip()
            if line and len(line) <= 40 and not _CHROME.search(line) and "《" not in line:
                yield bid, line


def _line_issuer(blocks):
    for bid, line in _signature_lines(blocks):
        name = _named_issuer(line)
        if not name:
            continue
        rest = line
        for _, full, short in _ISSUERS:
            for pattern in sorted((*full, *short), key=len, reverse=True):
                rest = rest.replace(pattern, "")
        rest = re.sub(_DATE + "|" + _CN_DATE, "", rest)
        if not _SIGNATURE_WORDS.sub("", rest):
            return {"value": name, "located": "block", "block_id": bid}
    return None


def _issuer(title, blocks, meta, number):
    name = _named_issuer(title)
    if name:
        return {"value": name, "located": "title"}
    name = _named_issuer(meta.get("issuer", ""))
    if name:
        return {"value": name, "located": "metadata_table"}
    for pattern, name in _NUMBER_ISSUERS:
        if number and re.match(pattern, number):
            return {"value": name, "located": "document_number"}
    return _line_issuer(blocks)


def _tier(title, category, issuer):
    name = (issuer or {}).get("value")
    if re.match(r"^中华人民共和国.{1,30}条例", title) or name == "国务院":
        return "行政法规", "title" if name != "国务院" else "issuer"
    if re.match(r"^中华人民共和国.{1,30}法", title) and not re.search(r"细则|条例|办法|规定", title):
        return "法律", "title"
    if name in {"财政部", "国家税务总局"} and re.search(r"税|营业税改征", title):
        return "财税规范性文件", "issuer"
    if re.search(r"企业会计准则|会计处理(?:的|暂行)?规定|金融工具准则|会计准则", _TITLE.sub("", title)) and name in (None, "财政部"):
        return "会计准则及财政部会计规定", "title"
    if re.search(r"质量分析报告|质量检验|质量投诉", title):
        return "估值服务质量报告", "title"
    if re.search(r"SOP|笔记|内部资料", title):
        return "实务手册与案例", "title"
    if name in {"中国证监会", "中国人民银行"}:
        return "证监会/人民银行规章及规范性文件", "issuer"
    if name in _SELF_REGULATORS:
        return "行业自律规则", "issuer"
    if name in _EXCHANGES:
        if re.search(r"估值.{0,6}(?:服务|产品)|收益率曲线", title):
            return "估值服务机构方法与数据说明", "issuer"
        return "交易场所业务规则", "issuer"
    if name in _PRICING_VENDORS or name == "财政部" and "收益率曲线" in title:
        return "估值服务机构方法与数据说明", "issuer"
    if re.search(r"手册|操作实务|案例", title):
        return "实务手册与案例", "title"
    if re.search(r"管理(?:试行)?办法|管理规定|指导意见|编报规则", title):
        return "证监会/人民银行规章及规范性文件", "title"
    # Valuation/accounting guidelines and operating rules come from self-regulators; other fund
    # guidelines and decisions ("…侧袋机制指引", "…参与国债期货交易指引") from the CSRC.
    if (re.search(r"估值|核算|(?:工作|操作)(?:指引|细则)|自律", title)
            and re.search(r"指引|规定|标准|细则|规则|办法|规范", title)):
        return "行业自律规则", "title"
    if re.search(r"证券投资基金.{0,30}(?:指引|规定|决定|通知)", title):
        return "证监会/人民银行规章及规范性文件", "title"
    if re.search(r"估值方法|编制说明|编制方法|方法说明|估值服务|收益率曲线|估值产品|估值手册", title):
        return "估值服务机构方法与数据说明", "title"
    if re.search(r"期权|期货|合约|结算|交易规则|交易细则|业务规则", title):
        return "交易场所业务规则", "title"
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
    g = match.groupdict()
    label = match.group(0).strip()
    for year, month in (("y1", "m1"), ("y4", "m4"), ("y2", "m2")):
        if g[year]:
            return label, [int(g[year]), int(g[month] or 0)]
    if g["v"]:
        return label, [0, int(g["v"]), int(g["v2"] or 0)]
    return label, [int(g["y3"]), 0]


def base_title(title):
    text = _NOISE.sub("", title or "")
    text = _VERSION.sub("", text)
    text = re.sub(r"^QCCDC\s*\d+\s*", "", text)
    return _norm(text)


def _standalone_date(parts):
    """First line that is only a date (optionally after an issuer name): a signature date."""
    for part in parts:
        for line in part.split("\n"):
            line = line.strip()
            match = re.search(_DATE, line)
            if match and len(line) <= 40 and not _CHROME.search(line):
                rest = re.sub(_DATE, "", line)
                for _, full, short in _ISSUERS:
                    for pattern in sorted((*full, *short), key=len, reverse=True):
                        rest = rest.replace(pattern, "")
                if not _SIGNATURE_WORDS.sub("", rest):
                    return _date(match.groups())
    return None


def _after(raw, sentence):
    """Raw block text after a newline-flattened sentence (its line breaks kept)."""
    end = re.sub(r"\s+", "", sentence)[-8:]
    match = re.search(r"\s*".join(map(re.escape, end)), raw) if end else None
    return raw[match.end():] if match else ""


def _abolition_targets(clause):
    """Abolished titles in a clause. Titles inside "除…外" are exceptions; a parent notice that is only
    the container of a named annex ("《…通知》中附件2《…》") is not itself abolished; a title followed by
    an article/item or a quoted provision is abolished only in part."""
    masked = _EXCEPT.sub(lambda match: "　" * len(match.group(0)), clause)
    targets = []
    for match in _TITLE.finditer(masked):
        title = clause[match.start() + 1:match.end() - 1].strip()
        tail = masked[match.end():]
        next_title = tail.find("《")
        near = tail[:next_title] if next_title >= 0 else tail[:40]
        if _CONTAINER.match(tail[:80]):
            continue
        target = {"title": title}
        number = _DOC_NUMBER.search(near[:40])
        if number:
            target["document_number"] = _clean_number(near, number)
        if _PARTIAL.match(near):
            target["partial"] = True
        targets.append(target)
    return targets


def extract(title, category, blocks):
    """blocks: ordered (block_id, text). Returns a candidate dict without identity fields."""
    blocks = [(bid, text or "") for bid, text in blocks]
    meta = _meta_fields(blocks)
    own_number = _own_number(title or "", blocks, meta)
    issuer = _issuer(title or "", blocks, meta, own_number)
    tier, basis = _tier(title, category, issuer)
    label, key = _version(title)
    _, record_date = _header_record(blocks)
    table_date = _ISO_DATE.search(meta.get("published", ""))
    published = (_date(table_date.groups()) if table_date else None) or record_date
    numbers, effective, abolition, publishes = [], [], [], []
    seen_numbers = set()
    for index, (bid, text) in enumerate(blocks):
        for number in _numbers_in(text):
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
                        # "自公布之日起施行": a signature date line after the clause, else the header record.
                        tail = [_after(blocks[index][1], sentence)] + [t for _, t in blocks[index + 1:index + 4]]
                        derived = _standalone_date(tail) or published
                        # A revision's clause dates from the revision, never from the original edition.
                        if derived and not (key and key[0] and int(derived[:4]) < key[0]):
                            item["derived_date"] = derived
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
                targets = _abolition_targets(clause)
                if targets:
                    abolition.append({"text": sentence[:160], "block_id": bid, "targets": targets})
            for pattern in _DECLARED_PUBLISH:
                for match in pattern.finditer(sentence):
                    for name in _TITLE.findall(match.group(1)):
                        if len(publishes) < 4:
                            publishes.append({"title": name.strip(), "block_id": bid})
    title_publish = None if _ANNEX.search(title or "") else _TITLE_PUBLISH.match(title or "")
    if title_publish:
        known = {p["title"] for p in publishes}
        publishes += [{"title": name.strip(), "block_id": None} for name in _TITLE.findall(title_publish.group(1))
                      if name.strip() not in known][:max(0, 4 - len(publishes))]
    signature = None
    for bid, text in reversed(blocks[-8:]):
        date = _standalone_date([text])
        if date:
            signature = {"date": date, "block_id": bid}
            break
    if not signature and published:
        signature = {"date": published, "block_id": None}
    subjects = list(dict.fromkeys(label for pattern, label in _SUBJECTS if re.search(pattern, title)))
    return {"issuer": issuer, "tier": tier, "tier_basis": basis, "status_hints": _status_hints(title, category),
            "subjects": subjects, "own_document_number": own_number,
            "version_label": label, "version_key": key, "base_title": base_title(title),
            "document_numbers": numbers, "effective_statements": effective, "signature_date": signature,
            "abolition_statements": abolition, "publishes": publishes}


def _matches(target_title, candidate):
    """Title reference -> library document. Explicitly different versions never match."""
    target_full = _norm(_NOISE.sub("", target_title))
    target_base = base_title(target_title)
    target_key = _version(target_title)[1]
    title = _norm(_NOISE.sub("", candidate["title"]))
    own = _norm(_NOISE.sub("", _TITLE_PUBLISH.sub("", candidate["title"], count=1)))
    if len(target_base) < 6 or not title:
        return False
    if target_key and candidate["version_key"] and target_key != candidate["version_key"]:
        return False
    if target_key and not candidate["version_key"]:
        return target_full == title
    return (target_full == title or target_base == candidate["base_title"]
            or len(target_base) >= 10 and target_base in own)


def _year(entry):
    """Best dated year of a candidate: its title version, else its effective or signature date."""
    key = entry.get("version_key")
    if key and key[0]:
        return key[0]
    for item in entry.get("effective_statements", []):
        date = item.get("date") or item.get("derived_date")
        if date and item.get("mode") != "transition_deadline":
            return int(date[:4])
    signature = entry.get("signature_date") or {}
    return int(signature["date"][:4]) if signature.get("date") else None


def link(entries):
    """Cross-document hints inside one space. entries: version_id -> candidate (mutated)."""
    for entry in entries.values():
        entry["links"] = {"abolishes": [], "abolished_by": [], "partially_abolishes": [], "partially_abolished_by": [],
                          "newer_versions": [], "older_versions": [], "publishes": [], "published_by": []}
    for vid, entry in entries.items():
        year = _year(entry)
        for statement in entry["abolition_statements"]:
            for target in statement["targets"]:
                forward, backward = (("partially_abolishes", "partially_abolished_by") if target.get("partial")
                                     else ("abolishes", "abolished_by"))
                for other_id, other in entries.items():
                    other_year = _year(other)
                    # A document cannot abolish an edition dated after it.
                    if other_id == vid or year and other_year and other_year > year:
                        continue
                    if _matches(target["title"], other) and other_id not in entry["links"][forward]:
                        entry["links"][forward].append(other_id)
                        other["links"][backward].append(vid)
        for item in entry["publishes"]:
            # A document never publishes another edition of itself.
            matched = [other_id for other_id, other in entries.items()
                       if other_id != vid and other["base_title"] != entry["base_title"] and _matches(item["title"], other)]
            if len(matched) > 1 and year:
                # An unversioned title names the edition published at the time.
                matched = [other_id for other_id in matched if _year(entries[other_id]) == year] or matched
            for other_id in matched:
                if other_id not in entry["links"]["publishes"]:
                    entry["links"]["publishes"].append(other_id)
                    entries[other_id]["links"]["published_by"].append(vid)
    groups = defaultdict(list)
    for vid, entry in entries.items():
        if entry["base_title"] and len(entry["base_title"]) >= 6:
            groups[entry["base_title"]].append(vid)
    for members in groups.values():
        if len(members) < 2:
            continue
        # Order only comparable editions: V-number vs V-number, dated vs dated (an unversioned title
        # takes its own effective or signature year).
        keys = {}
        for vid in members:
            key = entries[vid]["version_key"]
            if not key:
                year = _year(entries[vid])
                key = [year, 0] if year else None
            if key:
                keys[vid] = key
        for vid, mine in keys.items():
            for other, theirs in keys.items():
                if other == vid or len(mine) != len(theirs) or (mine[0] == 0) != (theirs[0] == 0):
                    continue
                explicit = bool(entries[vid]["version_key"] and entries[other]["version_key"])
                # A year taken from dates orders editions only across different years.
                if theirs > mine if explicit else theirs[0] > mine[0]:
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
