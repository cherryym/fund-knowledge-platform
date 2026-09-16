"""Question-specific source routing and source-anchored context, not invented facts.

Only caller-authorized records enter this module. All structure is a rebuildable
projection of current source blocks; no original text/hash or review state changes.
"""
import re
from collections import defaultdict

ROUTING_VERSION = "fund-source-routing-v3"
_HAN = "一二三四五六七八九十百〇零两"
_TOC = re.compile(r"\.{3,}|…{2,}|·{4,}")
# Retrieval vocabulary describes questions and evidence, never a business answer,
# a preferred resource UUID, or a numerical valuation rule.
_EVENTS = {
    "trading_suspension": ("停牌", "暂停交易"),
    "bond_put": ("回售",),
    "restricted_stock": ("流通受限", "限售", "流动性折扣", "AAP"),
    "delisting": ("退市",),
    "quote_disruption": ("无报价", "没有报价", "非活跃市场", "无活跃市场"),
}
_SPECIAL_SCOPES = ("可转债", "可交换债", "流通受限", "港股通", "期权", "期货", "货币市场基金", "证券公司", "私募")
_FACETS = {
    "scope": r"适用|所称|是指|包括|不含|含投资人|含投资者|本文以|执行估值核算",
    "exclusion": r"不包括|不适用|不包含|不属于|除外",
    "no_quote": r"(?:无|没有|不能获取|无法获取)(?:市场)?(?:报价|市价)|最近交易日.*报价",
    "quote_reliability": r"(?:报价|市价).*(?:不能真实|代表性|不具代表|不能反映)",
    "inactive_market": r"(?:不存在|没有|无|非)活跃市场",
    "valuation_method": r"估值技术|估值方法|估值模型|AAP模型|公允价值|计算.*估值|估值.*计算|取价|看长估值|看短估值|估值全价",
    "material_impact": r"潜在估值调整|(?:重大变化|重大事件).*净值|净值的影响",
    "governance_decision": r"估值委员会|估值决策|估值.*管理制度|估值政策和程序",
    "governance_review": r"定期复核|审阅机制|一致性|一贯性|托管人.*(?:复核|协商|审阅)",
    "governance_disclosure": r"披露|临时公告",
    "governance_auditor": r"会计师事务所|审计.*意见",
    "governance_responsibility": r"不能免除|第一责任人|相关责任|估值工作机制",
    "exercise_state": r"(?:已|未|不|放弃|选择|确认|申报|登记|撤销).{0,12}(?:回售|行权)|(?:回售|行权).{0,12}(?:前|后|日|期|状态|确认|申报)",
    "quote_options": r"看长|看短|行权估值|到期估值|估值全价|估值净价|长待偿期|短待偿期",
    "quote_conditions": r"(?:若|如果|情形|否则|已确认|未行使|行使回售权).*(?:看长|看短|行权估值|到期估值|估值全价|长待偿期|短待偿期)",
    "trading_procedure": r"申报|委托|撤单|撮合|挂单|下单|交易时间|交易规则",
}


def _body(record):
    # OCR labels are provenance, not headings or parts of a wrapped sentence.
    # This is a matching-only view; selected text/data/hash remain untouched.
    return re.sub(r"^【原件[^】]*】\s*", "", str(record.get("text", ""))).strip()


def question_plan(question, context=None):
    context = context or {}
    text = question + " " + " ".join(str(v) for v in context.values())
    assets = [name for name, tokens in {
        "股票": ("股票", "股权", "A股", "港股", "限售股"),
        "债券": ("债券", "可转债", "同业存单", "固收"),
        "基金": ("FOF", "基金份额", "ETF", "基金分红"),
        "衍生品": ("期权", "期货", "衍生品", "互换"),
    }.items() if any(token.lower() in text.lower() for token in tokens)]
    purchase = bool(re.search(r"买入|购买|买进|购入|买股票|买债券|购置|取得|申购", text))
    posting = bool(re.search(r"入账|记账|账务|会计|分录|核算|计量|交易费用|手续费", text))
    valuation = bool(re.search(r"估值|公允价值|取价|市值|估值日", text))
    validity = bool(re.search(r"现行|最新|有效|废止|效力|监管|法规|修订", text))
    events = [name for name, tokens in _EVENTS.items() if any(t.lower() in text.lower() for t in tokens)]
    if "bond_put" in events and not assets:
        assets = ["债券"]
    exchange = bool(re.search(r"申报|委托|挂单|下单|撮合|撤单|买卖|能否交易|交易规则", text)) and not valuation
    assumed_valuation = bool(events) and not (valuation or posting or exchange or validity)
    if assumed_valuation:
        valuation = True
    special = [token for token in ("流通受限", "限售", "停牌", "退市", "AAP", "港股通", "流动性折扣", "SPPI")
               if token.lower() in text.lower()]
    special += [term for name in events for term in _EVENTS[name] if term in text and term not in special]
    if exchange:
        intent, roles = "exchange_rules", ["exchange_rules", "source_document", "regulation"]
    elif validity and not posting and not purchase:
        intent, roles = "rule_validity", ["regulation", "specialist_guidance", "accounting_manual"]
    elif events and valuation:
        intent, roles = "specialist_valuation", ["specialist_guidance", "regulation", "accounting_manual"]
    elif posting or (purchase and assets):
        intent, roles = "fund_accounting_practice", ["accounting_manual", "specialist_guidance", "regulation"]
    elif special and (valuation or re.search(r"模型|参数|折扣|计算", text)):
        intent, roles = "specialist_valuation", ["specialist_guidance", "accounting_manual", "regulation"]
    else:
        intent, roles = "general_knowledge", ["source_document", "knowledge"]
    dimensions = []
    if purchase:
        dimensions.append("initial_measurement")
    if valuation:
        dimensions.append("subsequent_measurement")
    if posting:
        dimensions.append("accounting_entries")
    if not dimensions:
        dimensions = ["topic_explanation"]
    requirements = {}
    if exchange and events:
        dimensions = ["applicability", "trading_procedure"]
        requirements = {"applicability": ["scope"], "trading_procedure": ["trading_procedure"]}
    elif events and intent == "specialist_valuation":
        dimensions = ["applicability", "subsequent_measurement", "governance"]
        requirements = {"applicability": ["scope"], "subsequent_measurement": ["valuation_method"],
            "governance": ["governance_decision", "governance_review", "governance_disclosure",
                           "governance_auditor", "governance_responsibility"]}
        if any(e in events for e in ("trading_suspension", "quote_disruption")):
            if re.search(r'限售|锁定期|流通受限|AAP|流动性折扣|流动性折价|适用边界', text, re.I):
                dimensions.insert(1, "inapplicability")
                requirements["inapplicability"] = ["exclusion"]
            requirements["subsequent_measurement"] += ["no_quote", "quote_reliability", "inactive_market", "material_impact"]
        if "bond_put" in events:
            dimensions[2:2] = ["exercise_state", "quote_selection"]
            requirements.update(exercise_state=["exercise_state"], quote_selection=["quote_options", "quote_conditions"])
        if posting:
            dimensions.append("accounting_entries")
            requirements["accounting_entries"] = ["accounting_entries"]
    return {"routing_version": ROUTING_VERSION, "intent": intent, "assets": assets,
        "purchase_event": purchase, "special_cases": special, "dimensions": dimensions,
        "focus_terms": list(dict.fromkeys([term for event in events for term in _EVENTS[event]] +
                                          [term for term in _SPECIAL_SCOPES if term in text])),
        "events": events, "dimension_requirements": requirements,
        "critical_dimensions": ([d for d in dimensions if d in
            {"subsequent_measurement", "exercise_state", "quote_selection", "trading_procedure"}]),
        "interpretation": {"task": "交易申报" if exchange else ("基金持仓估值" if valuation else intent),
            "assumptions": ["按平台业务背景，将事件泛问解释为基金持仓估值问题。"] if assumed_valuation else [],
            "basis": "question_and_context"},
        "scenario": {"assets": assets, "events": events,
            "facts_to_confirm": (["回售条款及适用品种", "估值日及回售申报/确认状态", "报价口径及可用价格"]
                                 if "bond_put" in events else
                                 (["估值日及停牌原因", "报价可用性及代表性", "重大事件及净值影响"]
                                  if "trading_suspension" in events and not exchange else []))},
        "preferred_source_roles": roles,
        "authority_note": "主来源仅表示与本题业务场景更贴合，不表示效力更高或已完成核验。"}


def source_role(title):
    if any(term in title for term in ("交易规则", "审核关注", "交易结算", "限价申报", "竞价")):
        return "exchange_rules"
    if "会计" in title and any(term in title for term in ("手册", "实务", "操作")):
        return "accounting_manual"
    if any(term in title for term in ("估值指引", "估值处理标准", "估值编制", "估值方法", "估值技术", "估值的参考方法")):
        return "specialist_guidance"
    if any(term in title for term in ("准则", "管理办法", "管理规定", "法律", "条例", "监管", "指导意见")):
        return "regulation"
    if any(term in title for term in ("交易规则", "审核关注", "交易结算")):
        return "exchange_rules"
    return "source_document"


def _heading(record):
    text = _body(record)
    if not text or len(text) > 90 or _TOC.search(text) or re.fullmatch(r"\d+", text):
        return None
    if re.search(r"[。；！？\n]|(?:应|须|不得|不能|如果|使潜在)", text):
        return None
    if re.match(r"^第\s*\d+\s*步[：:]", text):
        return 2, text
    if record.get("block_type") == "heading":
        if not re.search(r"[，,]", text):
            return int(record.get("data", {}).get("level", 2)), text
        return None
    if re.match(rf"^第[{_HAN}\d]+章\s*\S", text):
        return 1, text
    if re.match(rf"^[{_HAN}]+、", text) and not re.search(r"[，,。；：]", text):
        return 2, text
    if re.match(rf"^[（(][{_HAN}]+[）)]", text) and not re.search(r"[，,。；：]", text):
        return 3, text
    return None


def source_structure(candidates):
    versions = defaultdict(list)
    for record in candidates:
        versions[record["version_id"]].append(record)
    result = {}
    for vid, records in versions.items():
        stack = []
        ordered = sorted(records, key=lambda r: r.get("ordinal", 0))
        for index, record in enumerate(ordered):
            if index and record.get("ordinal", 0) != ordered[index - 1].get("ordinal", 0) + 1:
                stack = []  # A missing block may have introduced a different section.
            heading = _heading(record)
            if heading:
                level, title = heading
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title, record["block_id"]))
            result[(vid, record["block_id"])] = {"section_path": [item[1] for item in stack],
                "section_anchor_ids": [item[2] for item in stack], "heading": bool(heading)}
    return versions, result


def _score(terms, record, plan, structure):
    text, title = record["text"], record.get("title", "")
    path = " / ".join(structure["section_path"])
    score = sum((3 if term in text else 0) + (1 if term in title else 0) for term in terms)
    if plan["assets"]:
        score += 8 if any(asset in path for asset in plan["assets"]) else 0
        if source_role(title) == "accounting_manual" and path and not any(asset in path for asset in plan["assets"]):
            score -= 20
    if plan["purchase_event"] and re.search(r"买入|购入|取得|初始计量|初始确认", text):
        score += 10
    if _TOC.search(text) or re.fullmatch(r"\s*\d+\s*", text):
        return -100
    return score


def _paragraph_window(seed, records, structure, *, journal=False):
    """Keep a wrapped sentence / accounting entry intact, retaining original IDs."""
    ordered = sorted(records, key=lambda r: r.get("ordinal", 0))
    index = next(i for i, row in enumerate(ordered) if row["block_id"] == seed["block_id"])
    selected = []
    path = structure[(seed["version_id"], seed["block_id"])]["section_path"]
    for row in ordered[index:index + (10 if journal else 12)]:
        meta = structure[(row["version_id"], row["block_id"])]
        text = row["text"].strip()
        if selected and row.get("ordinal", 0) != selected[-1].get("ordinal", 0) + 1:
            break
        if selected and (meta["heading"] or (path and meta["section_path"] != path)
                         or re.match(r"^\d+、", text)):
            break
        if row.get("block_type") == "table" or re.fullmatch(r"\d+", text):
            break
        selected.append(row)
        if not journal and re.search(r"[。；！？][”’\"）)]*$", text):
            break
    return selected


_CLAUSE = re.compile(rf"^(?:[（(][{_HAN}\d]+[）)]|[{_HAN}]+、|第[{_HAN}\d]+条|[a-z][）)]|情形[{_HAN}\d]+)")
_END = re.compile(r"[。；！？][”’\"）)]*$")


def _semantic_groups(records, structure):
    """Reassemble source paragraphs before matching; never invent missing text.

    A source gap, a new numbered clause, and an evidenced heading are boundaries.
    Fragmented formula sections cannot supply isolated recommendations. Prose
    overviews in those sections remain usable with an explicit detail gap.
    """
    units, pending = [], []

    def finish():
        if pending:
            body = "".join(_body(r) for r in pending)
            units.append({"records": list(pending), "text": body,
                "complete": bool(_END.search(body) or body.endswith(("：", ":"))
                                 or all(r.get("block_type") in {"table", "list", "formula"} for r in pending))})
            pending.clear()

    ordered = sorted(records, key=lambda r: r.get("ordinal", 0))
    for index, row in enumerate(ordered):
        text = _body(row)
        meta = structure[(row["version_id"], row["block_id"])]
        if index and row.get("ordinal", 0) != ordered[index - 1].get("ordinal", 0) + 1:
            finish()
        if meta["heading"] or not text or _TOC.search(text):
            finish()
            continue
        if re.fullmatch(r"\d+", text) and index + 1 < len(ordered):
            page = row.get("locator", {}).get("source_page")
            next_page = ordered[index + 1].get("locator", {}).get("source_page")
            if page is not None and str(page) == text and next_page is not None and next_page != page:
                finish()  # Evidenced page footer; never treat it as a formula operand.
                continue
        if pending and (_CLAUSE.match(text) or row.get("block_type") in {"table", "list", "formula"}):
            finish()
        if not pending and re.fullmatch(r"\d+", text):
            continue
        pending.append(row)
        if _END.search(text) or row.get("block_type") in {"table", "list", "formula"}:
            finish()
    finish()
    merged = []
    for unit in units:
        if merged:
            previous = merged[-1]
            first = unit["records"][0]
            last = previous["records"][-1]
            article = re.match(rf"^第[{_HAN}\d]+条", previous["text"])
            same_section = structure[(first["version_id"], first["block_id"])]["section_anchor_ids"] == \
                structure[(last["version_id"], last["block_id"])]["section_anchor_ids"]
            if article and not _CLAUSE.match(unit["text"]) and same_section and \
                    first.get("ordinal", 0) == last.get("ordinal", 0) + 1:
                previous["records"].extend(unit["records"])
                previous["text"] += unit["text"]
                previous["complete"] = previous["complete"] and unit["complete"]
                continue
        merged.append(unit)
    units = merged
    broken_sections = set()
    for unit in units:
        if any(not re.search(r"[\u3400-\u9fff]", _body(r)) and r.get("block_type") == "paragraph"
               for r in unit["records"]):
            unit["complete"] = False
            row = unit["records"][0]
            broken_sections.add(tuple(structure[(row["version_id"], row["block_id"])]["section_anchor_ids"]))
    for unit in units:
        row = unit["records"][0]
        section = tuple(structure[(row["version_id"], row["block_id"])]["section_anchor_ids"])
        unit["detail_gap"] = section in broken_sections or unit["text"].endswith(("：", ":"))
        if unit["detail_gap"] and re.search(r"若|如果|情形|推荐|则|如下公式|使用.*公式", unit["text"]):
            unit["complete"] = False
    return units


def _event_match(text, plan):
    if any(term.lower() in text.lower() for event in plan["events"] for term in _EVENTS[event]):
        return True
    # A trading interruption can be described by its observable pricing condition.
    # This broadens recall without asserting that every suspension is an inactive market.
    return bool(any(e in plan["events"] for e in ("trading_suspension", "quote_disruption"))
                and re.search(_FACETS["no_quote"] + "|" + _FACETS["inactive_market"] + "|" + _FACETS["quote_reliability"], text))


def _event_exclusion(text, plan):
    events = "|".join(re.escape(term) for event in plan["events"] for term in _EVENTS[event])
    return bool(re.search(rf"(?:不包括|不适用于|不包含|不属于)[^。；]*(?:{events})|"
                          rf"(?:{events})[^。；]*(?:不适用|除外)", text, re.I))


def _focus_units(plan, versions, structure):
    items = []
    for vid, records in versions.items():
        if not records or records[0].get("kind") != "document":
            continue
        title = records[0].get("title", "")
        role = source_role(title)
        if any(term in title and term not in plan["focus_terms"] for term in ("证券公司", "私募")):
            continue
        # A classification test is a different task, even when it mentions puts.
        if re.search(r"SPPI|现金流量特征|本金和利息.*测试", title, re.I) and "SPPI" not in plan["special_cases"]:
            continue
        if plan["intent"] == "exchange_rules":
            if role != "exchange_rules":
                continue
        elif role == "exchange_rules":
            continue
        units = _semantic_groups(records, structure)
        excluded = [u for u in units if _event_exclusion(u["text"], plan)]
        excludes_source = any(re.search(r"所称|本指引|本通知|适用范围|定义", u["text"]) for u in excluded)
        different_subtype = any(term in title and term not in plan["special_cases"] and
            term not in " ".join(plan["assets"]) and term not in plan["focus_terms"] for term in _SPECIAL_SCOPES)
        title_focus = _event_match(title, plan)
        focused = [u for u in units if _event_match(u["text"], plan)]
        if not focused and not title_focus:
            continue
        direct_method = any(re.search(_FACETS["valuation_method"], u["text"])
                            and _event_match(u["text"], plan) for u in units)
        for unit in units:
            body = unit["text"]
            row = unit["records"][0]
            path = " / ".join(structure[(vid, row["block_id"])]["section_path"])
            is_exclusion = unit in excluded
            if (excludes_source or different_subtype) and not is_exclusion:
                continue
            if re.search(r"SPPI|现金流量特征.*测试", path, re.I) and "SPPI" not in plan["special_cases"]:
                continue
            if any(asset in path for asset in ("股票", "债券", "衍生品")) and plan["assets"] and not any(
                    asset in path for asset in plan["assets"]):
                continue
            # An event in a different chapter cannot make this a relevant buy-in.
            if re.search(r"买入|购入|初始确认|初始计量|基金赎回|债务违约", body + path) and not _event_match(body, plan):
                continue
            facets = {name for name, pattern in _FACETS.items() if re.search(pattern, body)}
            if body.endswith(("：", ":")):
                facets -= {"exercise_state", "quote_conditions"}  # A branch introduction omits its branches.
            if is_exclusion:
                facets = {"exclusion"}
            elif not (_event_match(body + path, plan) or title_focus):
                # Cross-section expansion is only for valuation scope/governance
                # inside an already focus-matched valuation document.
                if not direct_method or not re.search(r"估值|公允价值", body):
                    continue
                facets &= {"scope", "material_impact", "governance_decision", "governance_review",
                           "governance_disclosure", "governance_auditor", "governance_responsibility"}
            if "bond_put" in plan["events"]:
                if not re.search(r"(?:已|未|不|放弃|选择|确认|申报|登记|撤销|行使).{0,12}(?:回售|行权)|(?:回售|行权).{0,12}(?:状态|确认|申报|登记|收款)", body):
                    facets.discard("exercise_state")
                # Duration/convexity is a different output even though its formula
                # mentions long/short valuations and projected cash flows.
                if re.search(r"久期|凸性|基点价值", body + path):
                    continue
            if plan["intent"] == "exchange_rules" and not is_exclusion and "trading_procedure" in facets:
                facets.add("scope")  # The event-specific trading prohibition is itself a scope rule.
            dimensions = [d for d, required in plan["dimension_requirements"].items() if facets.intersection(required)]
            if not dimensions:
                continue
            items.append({**unit, "facets": facets, "dimensions": dimensions, "version_id": vid,
                "source_role": role, "direct": _event_match(body, plan), "exclusion": is_exclusion})
    return items


def _retrieve_focused(question, candidates, plan, versions, structure, limit, max_bytes):
    items = _focus_units(plan, versions, structure)
    by_version = defaultdict(list)
    for item in items:
        if item["complete"] and not item["exclusion"]:
            by_version[item["version_id"]].append(item)
    required = set(f for fs in plan["dimension_requirements"].values() for f in fs)

    def source_rank(vid):
        units = by_version[vid]
        facets = set().union(*(u["facets"] for u in units)) & required
        title = versions[vid][0].get("title", "")
        historical = bool(re.search(r"已废止|已失效|历史版本", title))
        # Relevance/coverage, not publication dates or review/legal-state promotion.
        event_specialist = source_role(title) == "specialist_guidance" and _event_match(title, plan)
        return (not historical, event_specialist if "bond_put" in plan["events"] else False,
                len(facets), sum(u["direct"] for u in units), title, vid)

    primary_id = max(by_version, key=source_rank) if by_version else None
    evidence, selected, used_bytes, covered, omitted = [], set(), 0, set(), []
    chosen_units = []
    remaining = [u for u in items if u["complete"]]
    while remaining:
        # Cover missing facets first; primary-source clauses win ties. Negative
        # applicability is useful evidence but can never become a pricing method.
        item = max(remaining, key=lambda u: (len((u["facets"] & required) - covered),
            u["version_id"] == primary_id, u["exclusion"], u["direct"],
            -len(u["records"]), -u["records"][0].get("ordinal", 0)))
        remaining.remove(item)
        new_facets = (item["facets"] & required) - covered
        if not new_facets and item["version_id"] != primary_id:
            continue
        rows = item["records"]
        size = sum(len(r["text"].encode()) for r in rows)
        if len(evidence) + len(rows) > limit or used_bytes + size > max_bytes:
            omitted.append({"version_id": item["version_id"], "block_ids": [r["block_id"] for r in rows],
                            "reason": "complete_group_exceeds_budget", "facets": sorted(new_facets)})
            continue
        gid = f"P{len(chosen_units) + 1}"
        chosen_units.append(item)
        reason = "原文明确排除本事件，作为不适用边界" if item["exclusion"] else "事件条件与回答维度匹配的完整原文语义组"
        role = "primary" if item["version_id"] == primary_id else "supporting"
        for row in rows:
            meta = structure[(row["version_id"], row["block_id"])]
            evidence.append({**row, "context_group": gid, "retrieval_role": role,
                "retrieval_reason": reason, "source_role": item["source_role"],
                "section_path": meta["section_path"], "section_anchor_ids": meta["section_anchor_ids"],
                "retrieval_channels": ["event_focus", "answer_dimensions", "document_structure"],
                "answer_dimension": item["dimensions"][0], "answer_dimensions": item["dimensions"],
                "coverage_facets": sorted(item["facets"]), "semantic_group_block_ids": [r["block_id"] for r in rows],
                "semantic_group_complete": True})
            selected.add((row["version_id"], row["block_id"]))
        used_bytes += size
        covered |= item["facets"] & required
    # Precise citations only, and only after rule groups have received the budget.
    linked_count = 0
    for row in candidates:
        if row.get("kind") != "knowledge" or not any((c.get("version_id"), c.get("block_id")) in selected
                for c in row.get("source_citations", [])):
            continue
        size = len(row["text"].encode())
        if linked_count >= 3 or len(evidence) >= limit or used_bytes + size > max_bytes:
            continue
        evidence.append({**row, "retrieval_role": "related_knowledge", "retrieval_reason": "知识页精确引用已选原文块",
                         "source_role": "knowledge", "answer_dimension": None})
        used_bytes += size
        linked_count += 1
    gaps = []
    if not primary_id:
        gaps.append("未找到同时匹配事件和任务的完整来源段落；不能用同资产的买入、分类测试或其他事件替代。")
    detail_gap = any(not u["complete"] or u["detail_gap"] for u in items if u["version_id"] == primary_id)
    if detail_gap:
        gaps.append("主来源存在断句、公式碎片或条件分支不完整；所选概述不证明已取得完整计算及行权判断规则。")
    if omitted:
        gaps.append("完整语义组超过本次证据预算；相关维度保留缺口。")
    primary = versions[primary_id][0] if primary_id else None
    plan.update(primary_source_id=primary["resource_id"] if primary else None,
                primary_version_id=primary_id, gaps=gaps, omitted_groups=omitted,
                evidence_budget={"max_blocks": limit, "max_utf8_bytes": max_bytes}, semantic_detail_gap=detail_gap)
    evidence.sort(key=lambda r: (r.get("retrieval_role") != "primary", r.get("title", ""), r.get("ordinal", 0)))
    return evidence, context_manifest(evidence, plan)


def retrieve_answer_context(question, candidates, *, limit=28, context=None, max_bytes=14000):
    """Return primary-led evidence and an auditable relation projection."""
    from .retrieval import rank_evidence, tokenize
    plan = question_plan(question, context)
    versions, structure = source_structure(candidates)
    if plan["events"] and plan["intent"] in {"specialist_valuation", "exchange_rules"}:
        return _retrieve_focused(question, candidates, plan, versions, structure, limit, max_bytes)
    terms = set(tokenize(question))
    scored = []
    for record in candidates:
        if not record.get("text", "").strip() or len(record["text"].encode()) > 8000:
            continue
        meta = structure[(record["version_id"], record["block_id"])]
        # Cheap prefilter before tokenizing whole source bodies. Structure supplies
        # context after a seed is chosen; neighboring lines need not repeat the query.
        if not any(term in record["text"] or term in record.get("title", "") for term in terms):
            continue
        score = _score(terms, record, plan, meta)
        if score > 0:
            scored.append((score, record))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["version_id"], pair[1].get("ordinal", 0)))
    docs = [row for _, row in scored if row.get("kind") == "document"]
    preferred = [row for row in docs if source_role(row.get("title", "")) == plan["preferred_source_roles"][0]]
    if plan["intent"] == "specialist_valuation" and plan["special_cases"]:
        matched = [row for row in preferred if any(term.lower() in row.get("title", "").lower() for term in plan["special_cases"])]
        preferred = matched or preferred
    primary = preferred[0] if preferred else (docs[0] if docs else None)
    primary_id = primary["version_id"] if primary else None
    groups, seen, used_bytes = [], set(), 0

    def add_group(rows, role, reason, dimension=None):
        nonlocal used_bytes
        fresh = [row for row in rows if (row["version_id"], row["block_id"]) not in seen]
        size = sum(len(row["text"].encode()) for row in fresh)
        if not fresh or sum(len(g["records"]) for g in groups) + len(fresh) > limit or used_bytes + size > max_bytes:
            return
        group_id = f"P{len(groups) + 1}"
        enriched = []
        for row in fresh:
            seen.add((row["version_id"], row["block_id"]))
            meta = structure[(row["version_id"], row["block_id"])]
            enriched.append({**row, "context_group": group_id, "retrieval_role": role,
                "retrieval_reason": reason, "source_role": source_role(row.get("title", "")),
                "section_path": meta["section_path"], "section_anchor_ids": meta["section_anchor_ids"],
                "retrieval_channels": ["source_routing", "document_structure"],
                "answer_dimension": dimension, "semantic_group_block_ids": [r["block_id"] for r in rows],
                "semantic_group_complete": bool(rows and (_END.search(_body(rows[-1])) or dimension == "accounting_entries"))})
        groups.append({"id": group_id, "role": role, "reason": reason, "dimension": dimension, "records": enriched})
        used_bytes += size

    if primary and plan["intent"] == "fund_accounting_practice":
        primary_records = versions[primary_id]
        scoped = [row for row in primary_records if not plan["assets"] or any(
            asset in " / ".join(structure[(primary_id, row["block_id"])]["section_path"]) for asset in plan["assets"])]
        scoped = scoped or primary_records
        for dimension, pattern in (
            ("initial_measurement", r"(?:买入|购入|取得).{0,16}(?:时|初始)|交易日.{0,12}初始计量"),
            ("subsequent_measurement", r"估值日.{0,25}(?:公允|计量)|后续计量"),
            ("accounting_entries", r"^\d+、(?:买入|购入).{0,14}(?:交易日|时)$"),
        ):
            if dimension not in plan["dimensions"]:
                continue
            seeds = [row for row in scoped if re.search(pattern, row["text"].strip())
                     and not structure[(primary_id, row["block_id"])]["heading"]]
            if seeds:
                add_group(_paragraph_window(seeds[0], primary_records, structure, journal=dimension == "accounting_entries"),
                    "primary", "主来源相关业务章节及连续原文", dimension)
    if primary and not groups:
        add_group(_paragraph_window(primary, versions[primary_id], structure) or [primary],
            "primary", "与当前问题类型最贴合的可读来源")

    # Follow REAL block citations in both directions. Never substitute a Wiki
    # claim for a missing source anchor or turn similarity into a citation.
    selected_pairs = set(seen)
    linked_wiki = [row for row in candidates if row.get("kind") == "knowledge" and any(
        (link["version_id"], link["block_id"]) in selected_pairs for link in row.get("source_citations", []))]
    for row in linked_wiki[:3]:
        add_group([row], "related_knowledge", "知识页精确引用了主来源命中块")

    support_pool = [row for _, row in scored if row["version_id"] != primary_id and row.get("kind") == "document"
                    and source_role(row.get("title", "")) != "exchange_rules"]
    support_pool = [row for row in support_pool if not any(term in row.get("title", "") and term not in question
                    for term in ("证券公司", "私募"))]
    support_pool = support_pool[:250]
    per_version = set()
    for row in rank_evidence(question, support_pool, None, limit=20):
        if row["version_id"] in per_version or len(per_version) >= 2:
            continue
        if plan["intent"] == "fund_accounting_practice" and not plan["special_cases"] and any(
            term in row.get("title", "") for term in ("流通受限", "港股通", "期权", "期货", "黄金", "退市")):
            continue
        add_group(_paragraph_window(row, versions[row["version_id"]], structure) or [row],
            "supporting", "辅助核对规则或条件，不覆盖主来源的会计实务解释")
        per_version.add(row["version_id"])
    if not groups:
        # Unknown business intent still uses authorized lexical evidence.
        for row in rank_evidence(question, [r for _, r in scored[:300]], None, limit=min(limit, 6)):
            add_group([row], "supporting", "未识别到专门主来源，保留相关可读资料")
    evidence = [row for group in groups for row in group["records"]]
    gaps = []
    if plan["intent"] == "fund_accounting_practice" and not preferred:
        gaps.append("未找到可读且匹配本题的会计实务手册，不能伪装已查阅主手册。")
    if primary and primary.get("kind") == "document" and not linked_wiki:
        gaps.append("主来源命中块尚无精确知识页引用，本次直接使用原文，不构造虚假知识关联。")
    plan.update(primary_source_id=primary["resource_id"] if primary else None,
        primary_version_id=primary_id, gaps=gaps)
    return evidence, context_manifest(evidence, plan)


def context_manifest(evidence, plan=None):
    """Rebuild after budget pruning, so metadata never promises removed evidence."""
    sources, groups, edges = {}, {}, []
    rows_by_group = defaultdict(list)
    selected = {(row["version_id"], row["block_id"]) for row in evidence}
    for row in evidence:
        vid, bid = row["version_id"], row["block_id"]
        sources.setdefault(vid, {"resource_id": row["resource_id"], "version_id": vid, "title": row.get("title", ""),
            "role": row.get("retrieval_role", "supporting"), "source_role": row.get("source_role", source_role(row.get("title", ""))),
            "state": row.get("state", "UNKNOWN"), "legal_status": row.get("legal_status", "UNKNOWN"),
            "source_verified": row.get("source_verified", False)})
        gid = row.get("context_group", f"{vid}:{bid}")
        rows_by_group[gid].append(row)
        group = groups.setdefault(gid, {"id": gid, "version_id": vid, "role": row.get("retrieval_role", "supporting"),
            "section_path": row.get("section_path", []), "ordered_block_ids": [], "reason": row.get("retrieval_reason", ""),
            "dimension": row.get("answer_dimension")})
        previous = group["ordered_block_ids"][-1] if group["ordered_block_ids"] else None
        group["ordered_block_ids"].append(bid)
        edges.append({"type": "CONTAINS", "from": vid, "to": bid})
        prior = rows_by_group[gid][-2] if len(rows_by_group[gid]) > 1 else None
        if previous and prior and prior["version_id"] == vid and row.get("ordinal") is not None and \
                row["ordinal"] == prior.get("ordinal", -2) + 1:
            edges.append({"type": "CONTINUES", "version_id": vid, "from": previous, "to": bid})
        for link in row.get("source_citations", []):
            if (link["version_id"], link["block_id"]) in selected:
                edges.append({"type": "CITES", "from_version_id": vid, "from": bid,
                    "to_version_id": link["version_id"], "to": link["block_id"]})
    current_plan = {**(plan or {})}
    if current_plan:
        facets, covered, incomplete_groups = set(), set(), []
        for gid, rows in rows_by_group.items():
            expected = set().union(*(set(r.get("semantic_group_block_ids", [r["block_id"]])) for r in rows))
            retained = {r["block_id"] for r in rows}
            complete = expected <= retained and all(r.get("semantic_group_complete", True) for r in rows)
            groups[gid]["complete"] = complete
            groups[gid]["dimensions"] = sorted(set(d for r in rows for d in
                r.get("answer_dimensions", [r.get("answer_dimension")]) if d))
            if not complete:
                incomplete_groups.append(gid)
                continue
            facets.update(f for r in rows for f in r.get("coverage_facets", []))
            covered.update(groups[gid]["dimensions"])
        requirements = current_plan.get("dimension_requirements", {})
        if requirements:
            covered = {d for d, fs in requirements.items() if set(fs) <= facets}
        current_plan["covered_dimensions"] = sorted(covered)
        current_plan["uncovered_dimensions"] = [value for value in current_plan.get("dimensions", []) if value not in covered]
        missing_facets = {d: [f for f in fs if f not in facets] for d, fs in requirements.items() if not set(fs) <= facets}
        # `ready` permits a qualified explanation from complete relevant evidence.
        # `complete` is strictly stronger: no required dimension/detail is missing.
        # Neither flag verifies applicability facts or the independent legal axis.
        complete = bool(covered) and not current_plan["uncovered_dimensions"] and not incomplete_groups \
            and not current_plan.get("semantic_detail_gap", False) and current_plan.get("primary_version_id") in sources
        ready = bool({"valuation_method", "trading_procedure"} & facets) if requirements else bool(covered)
        ready = ready and current_plan.get("primary_version_id") in sources
        current_plan["coverage"] = {
            "status": "EMPTY" if not facets and not covered else ("COMPLETE" if complete else "PARTIAL"),
            "covered_dimensions": current_plan["covered_dimensions"],
            "missing_dimensions": current_plan["uncovered_dimensions"], "missing_facets": missing_facets,
            "incomplete_groups": incomplete_groups,
            "ready": ready, "ready_for_synthesis": ready, "complete": complete,
            "missing_critical_dimensions": [d for d in current_plan.get("critical_dimensions", []) if d not in covered],
            "requires_qualification": not complete or bool(current_plan.get("scenario", {}).get("facts_to_confirm")),
            "meaning": "仅表示保留原文对所需维度的覆盖，不代表业务事实已确认、规则现行或可直接采用具体价格。"}
        current_plan["authority"] = {"legal_status_by_version": {vid: s["legal_status"] for vid, s in sources.items()},
            "unverified_version_ids": [vid for vid, s in sources.items() if not s["source_verified"] or s["legal_status"] == "UNKNOWN"],
            "note": "效力独立保留原始元数据；来源相关性、发布时间、已发布状态均不将UNKNOWN提升为有效。"}
        current_plan["gaps"] = [g for g in current_plan.get("gaps", []) if not g.startswith("未覆盖回答维度：")]
        if current_plan["uncovered_dimensions"]:
            current_plan["gaps"].append("未覆盖回答维度：" + "、".join(current_plan["uncovered_dimensions"]) + "。")
        if current_plan.get("primary_version_id") not in sources:
            current_plan["primary_source_id"] = current_plan["primary_version_id"] = None
    return {"plan": current_plan, "sources": list(sources.values()), "passages": list(groups.values()),
        "relations": edges, "boundary": "章节连续关系不等于规范效力；CITES仅来自已有精确块引用。"}
