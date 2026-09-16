"""Regression from bounded live diagnostic, not a replay of its lost full answer."""
import pytest

from fund_kb.ai import AnswerValidationError, _check_grounded_text

LIVE_DIAGNOSTIC = '基于上述阶段划分，建议核对时分别检查交易日初始计量、相关交易费用是否归入当期损益，以及估值日公允价值及其变动损益是否对应，需按适用制度确认。该建议是依据手册口径组合的核对方向，不是已授权的操作流程，也不表示任何账务核对已完成。'


@pytest.mark.parametrize('text', [LIVE_DIAGNOSTIC,
    ('本次提供的损益类/摊余成本类债券估值日计量相关节点为DRAFT(待核)状态，'
     '其内容不能作为已确认结论，仅可用于条件式框架说明。')])
def test_actual_negative_statement_is_not_a_completed_business_claim(text):
    _check_grounded_text(text, '')


@pytest.mark.parametrize('text', [
    '任何账务核对已完成。', '该建议不表示已经取得审批但已完成过账。',
    '也不表示任何账务核对已完成；实际已完成。', '不是不表示任何账务核对已完成。',
    '也不表示任何账务核对已完成，付款已完成。',
    '也不表示任何账务核对已完成但实际已批准。',
])
def test_negation_cannot_hide_a_positive_or_double_negative(text):
    with pytest.raises(AnswerValidationError, match='EXECUTION_OR_APPROVAL_UNVERIFIED'):
        _check_grounded_text(text, '')
