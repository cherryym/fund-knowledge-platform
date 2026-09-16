"""Portable task guidance, not arbitrary executable code or an authority grant."""
from __future__ import annotations

import json

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from . import services as svc

FORMAT = "fundkb.agent-capability.v1"
LABEL = "Agent能力定义 v1"
KEY = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$"}
TEXT = {"type": "string", "minLength": 1, "pattern": r"\S"}
STRINGS = {"type": "array", "items": TEXT}
FIELD = {"type": "object", "additionalProperties": False,
    "required": ["key", "label", "type", "required", "description"],
    "properties": {"key": KEY, "label": TEXT, "type": {"enum": ["string", "number", "integer", "boolean", "date", "json"]},
        "required": {"type": "boolean"}, "description": {"type": "string"}}}
STEP = {"type": "object", "additionalProperties": False,
    "required": ["id", "title", "kind", "instructions", "depends_on", "required_tools", "outputs", "checks", "risk"],
    "properties": {"id": KEY, "title": TEXT, "kind": {"enum": ["agent", "human"]}, "instructions": TEXT,
        "depends_on": {"type": "array", "uniqueItems": True, "items": KEY},
        "required_tools": {**STRINGS, "uniqueItems": True}, "outputs": {"type": "array", "items": FIELD},
        "checks": {**STRINGS, "minItems": 1}, "risk": {"enum": ["read_only", "draft_write", "external_write", "financial_action"]}}}
DEFINITION = {"type": "object", "additionalProperties": False,
    "required": ["schema_version", "name", "description", "triggers", "limitations", "inputs", "steps", "deliverables", "source_version_ids", "source_scope"],
    "properties": {"schema_version": {"type": "integer", "const": 1}, "name": {**TEXT, "maxLength": 300}, "description": TEXT,
        "triggers": {**STRINGS, "minItems": 1}, "limitations": STRINGS, "inputs": {"type": "array", "items": FIELD},
        "steps": {"type": "array", "minItems": 1, "items": STEP}, "deliverables": {**STRINGS, "minItems": 1},
        "source_version_ids": {"type": "array", "uniqueItems": True, "items": {"type": "string", "format": "uuid"}},
        "source_scope": {"enum": ["reference", "formal"]}}}


def validate_json(value, *, status=422, code="CAPABILITY_VALUES_INVALID"):
    """Reject non-finite numbers at every depth before persistence or hashing."""
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        svc.fail(status, code, "能力数据须为有效JSON，数字必须有限且文本编码有效")


def validate_definition(value):
    validate_json(value, code="CAPABILITY_DEFINITION_INVALID")
    try:
        Draft202012Validator(DEFINITION, format_checker=FormatChecker()).validate(value)
    except ValidationError as exc:
        svc.fail(422, "CAPABILITY_DEFINITION_INVALID", "能力定义字段不完整或类型无效", field=".".join(map(str, exc.absolute_path)))
    identifiers = [step["id"] for step in value["steps"]]
    if len(set(identifiers)) != len(identifiers):
        svc.fail(422, "CAPABILITY_STEP_DUPLICATE", "步骤标识不能重复")
    for fields in [value["inputs"], *(step["outputs"] for step in value["steps"])]:
        if len({field["key"] for field in fields}) != len(fields):
            svc.fail(422, "CAPABILITY_FIELD_DUPLICATE", "同一组输入或输出的字段标识不能重复")
    graph = {step["id"]: set(step["depends_on"]) for step in value["steps"]}
    if any(dependency not in graph for dependencies in graph.values() for dependency in dependencies):
        svc.fail(422, "CAPABILITY_DEPENDENCY_MISSING", "步骤依赖指向不存在的步骤")
    remaining, resolved = dict(graph), set()
    while remaining:
        ready = {key for key, dependencies in remaining.items() if dependencies <= resolved}
        if not ready:
            svc.fail(422, "CAPABILITY_DEPENDENCY_CYCLE", "步骤依赖成环，无法执行")
        resolved |= ready
        remaining = {key: dependencies for key, dependencies in remaining.items() if key not in ready}
    if any(step["risk"] in {"external_write", "financial_action"} and step["kind"] != "human" for step in value["steps"]):
        svc.fail(422, "CAPABILITY_HUMAN_GATE_REQUIRED", "外部写入或金融动作必须是人工检查点，本平台不直接执行资金或过账工具")
    return value


def validate_values(fields, values, *, partial=False):
    validate_json(values)
    types = {"number": "number", "integer": "integer", "boolean": "boolean", "string": "string", "date": "string"}
    properties = {}
    for field in fields:
        properties[field["key"]] = ({"type": types[field["type"]]} if field["type"] != "json" else {})
        if field["type"] == "date":
            properties[field["key"]]["format"] = "date"
        if field["type"] == "string" and field["required"]:
            properties[field["key"]].update(minLength=1, pattern=r"\S")
    schema = {"type": "object", "additionalProperties": False, "properties": properties,
        "required": [] if partial else [field["key"] for field in fields if field["required"]]}
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(values)
    except ValidationError as exc:
        svc.fail(422, "CAPABILITY_VALUES_INVALID", "输入或交付字段不符合能力要求", field=".".join(map(str, exc.absolute_path)))


def starter():
    return {"schema_version": 1, "name": "估值事项查证与处理底稿", "description": "围绕具体估值事项，组织来源、比较口径并形成可复核的处理底稿。",
        "triggers": ["需要将估值问题转化为有依据、有检查点的处理底稿"],
        "limitations": ["不直接过账、付款、交易或发布正式制度", "资料不足或冲突时保留缺口，不编造执行结果"],
        "inputs": [{"key": "business_date", "label": "业务日期", "type": "date", "required": True, "description": "适用规则与处理时点"},
            {"key": "question", "label": "待处理事项", "type": "string", "required": True, "description": "对象、异常及目标"}],
        "steps": [{"id": "evidence", "title": "查证依据与适用条件", "kind": "agent",
            "instructions": "理解事项和业务日期，读取绑定知识与原文；按需要查找补充依据，核对版本、适用范围与冲突。没有绑定来源或依据不足时明确报告缺口。",
            "depends_on": [], "required_tools": ["read_bound_sources"],
            "outputs": [{"key": "evidence_summary", "label": "依据与缺口", "type": "string", "required": True, "description": "包含来源定位、适用条件与尚未解决的问题"}],
            "checks": ["来源真实可访问，业务日期与适用范围已核对"], "risk": "read_only"},
            {"id": "workpaper", "title": "形成处理底稿", "kind": "agent",
                "instructions": "根据已核对的输入与依据自主分析，区分事实、推断和建议，形成处理步骤、核对要求和待确认事项。交付实际底稿内容，不仅报告已完成。",
                "depends_on": ["evidence"], "required_tools": [],
                "outputs": [{"key": "workpaper", "label": "底稿正文", "type": "string", "required": True, "description": "可由复核人检查的完整成果"}],
                "checks": ["底稿包含处理依据、步骤、输出、检查点及缺口"], "risk": "draft_write"},
            {"id": "review", "title": "人工核对交付物", "kind": "human",
                "instructions": "核对底稿和原文依据，确认可用范围；未解决的业务问题不能当成已执行结果。此确认不是交易或过账授权。",
                "depends_on": ["workpaper"], "required_tools": [], "outputs": [],
                "checks": ["原文与底稿对应，处理边界和未完成事项清楚"], "risk": "read_only"}],
        "deliverables": ["来源及适用性清单", "处理底稿与待确认事项"], "source_version_ids": [], "source_scope": "reference"}
