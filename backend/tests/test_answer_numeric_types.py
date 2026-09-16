"""Numeric business facts are distinct from standard names; no network calls."""
from decimal import Decimal

import pytest

from fund_kb.ai import AnswerValidationError, _check_grounded_text, _quantities


@pytest.mark.parametrize("identifier", ["CAS 22", "CAS22", "IFRS 9", "IFRS9", "IAS 32", "ASBE 22", "cas 22"])
def test_standard_identifier_is_not_a_business_number(identifier):
    text = f"本次尚缺适用会计准则（如{identifier}）原文，需补充资料后核对。"
    _check_grounded_text(text, "")
    assert _quantities(text) == set()


@pytest.mark.parametrize("amount,unit,expected", [("22", "元", ("元", Decimal(22))),
    ("22", "%", ("ratio", Decimal('.22'))), ("22", "天", ("天", Decimal(22))),
    ("22", "年", ("年", Decimal(22))), ("22", "月", ("月", Decimal(22))),
    ("22", "bp", ("ratio", Decimal('.0022')))])
@pytest.mark.parametrize("prefix", ["CAS ", "CAS", "IFRS ", "NAV", ""])
def test_identifier_prefix_cannot_hide_unsupported_business_quantity(prefix, amount, unit, expected):
    text = f"金额或期限为{prefix}{amount}{unit}。"
    assert expected in _quantities(text)
    with pytest.raises(AnswerValidationError, match="NUMERIC_UNIT_SUPPORT_MISSING"):
        _check_grounded_text(text, "")
    _check_grounded_text(text, f"合成记录载明{amount}{unit}")


def test_standard_number_does_not_license_same_number_as_price():
    with pytest.raises(AnswerValidationError, match="NUMERIC_UNIT_SUPPORT_MISSING"):
        _check_grounded_text("按22元估值", "CAS 22")


def test_exact_live_gap_disclaimer_passes_without_any_numeric_fact_whitelist():
    _check_grounded_text("含回售权债券估值还需结合适用会计准则（如CAS 22）、基金估值指引及交易所/银行间市场规则综合判断，"
        "本次证据未直接引用这些上位规则原文，存在合规口径衔接缺口。", "")
