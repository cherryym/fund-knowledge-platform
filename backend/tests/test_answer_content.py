import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_answer_source_routing import rows

from fund_kb import ai
from fund_kb.answer_content import ANALYSIS_SCHEMA, assemble, request, schema
from fund_kb.answer_retrieval import retrieve_answer_context


def source():
    return rows('基金估值规则', ['估值日无报价且最近交易日后未发生重大事件的，应采用最近交易日的报价确定公允价值。'])


def draft():
    return {'status':'ANSWERED', 'summary':'应先判断最近交易日后是否发生重大事件，再选择适用的报价依据。',
        'claims':[{'text':'没有当日报价不等于无条件沿用旧价，仍需核对重大事件条件。','evidence_ids':['E1']}],
        'analysis':{'interpretation':'本题按基金持有资产的估值问题理解。',
            'checks':[{'title':'核对适用条件','reason':'采用最近报价的规则附有未发生重大事件的条件。','evidence_ids':['E1']}],
            'branches':[{'condition':'最近交易日后未发生重大事件','action':'按资料给出的最近交易日报价规则核对。','evidence_ids':['E1']}]}}


def generate(reply=None, preliminary=None):
    records = source()
    evidence, manifest = retrieve_answer_context('估值日无报价应该如何处理',records)
    if preliminary is not None:
        manifest['question_analysis'] = {'unverified': True, 'plan': preliminary}
    diagnostics, calls = {}, []
    def callback(payload):
        calls.append(payload)
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(reply or draft(), ensure_ascii=False)}}]}
    result = ai.generate_answer('估值日无报价应该如何处理','answer',{},evidence,
        SimpleNamespace(llm_provider='http',llm_model='synthetic',llm_base_url=''),str(uuid4()),
        completion_client=callback, answer_scope='reference', source_analysis=manifest,
        diagnostics=diagnostics, compact_output=True)
    return result, calls, diagnostics, records


def test_compact_generator_owns_content_server_owns_identities_and_envelope():
    result, calls, diagnostic, records = generate()
    assert len(calls) == 1 and result['analysis']['checks']
    assert result['review_status'] == 'REQUIRES_EXPERT'
    assert result['citations'][0]['version_id'] == records[0]['version_id']
    assert result['citations'][0]['content_sha256'] == records[0]['content_sha256']
    assert result['claims'][0]['id'] == 'C1'
    body = json.loads(calls[0]['messages'][1]['content'])
    assert 'output_skeleton' not in body
    for value in (records[0]['resource_id'], records[0]['version_id'], records[0]['block_id'], records[0]['content_sha256']):
        assert value not in json.dumps(calls[0]['messages'])
    assert body['evidence'][0]['id'] == 'E1'
    assert diagnostic['request_manifest']['server_owned_citations'] is True
    assert diagnostic['request_manifest']['prompt_version'] == 'fund-answer-content-v1'
    ai.validate_answer(result, records, mode='grounded', answer_scope='reference')


def test_final_schema_and_model_analysis_contract_agree():
    assert ai.answer_validator().schema['$defs']['analysis'] == ANALYSIS_SCHEMA


def test_native_provider_schema_is_inline_and_mode_specific_without_old_envelope_instructions():
    from fund_kb.answer_content import provider_schema, prompt
    from jsonschema import Draft202012Validator
    answer = provider_schema(ai.answer_validator().schema, 'answer')
    assert '$ref' not in json.dumps(answer) and '$defs' not in answer
    assert answer['properties']['solution'] == {'type':'null'}
    Draft202012Validator(answer).validate(draft())
    solution = provider_schema(ai.answer_validator().schema, 'solution')
    assert '$ref' not in json.dumps(solution)
    assert 'output_skeleton' not in prompt('reference')


def test_missing_method_gap_remains_visible_but_cannot_mask_a_prohibition():
    from fund_kb.ai import _check_named_method_support
    _check_named_method_support('本次证据未提供指数收益法的具体参数和适用条件。', '一般估值原则')
    _check_named_method_support('无法确认指数收益法在此场景的适用性，需补充对应资料。', '一般估值原则')
    _check_named_method_support('不能据此推断指数收益法被禁止。', '本通知不包括停牌证券')
    _check_named_method_support('本次未提供指数收益法资料，不应推断其适用于本案。', '一般估值原则')
    with pytest.raises(ai.AnswerValidationError, match='NAMED_METHOD_SUPPORT_MISSING'):
        _check_named_method_support('本次资料未提供指数收益法，因此禁止使用指数收益法。', '一般估值原则')
    with pytest.raises(ai.AnswerValidationError, match='NAMED_METHOD_SUPPORT_MISSING'):
        _check_named_method_support('不属于该通知范围，不能直接套用其估值方法（如指数收益法）。', '不包括临时停牌证券')


@pytest.mark.parametrize('text', ['C_3H < R', '若 C_3H < R，则核对回售状态。', '`R < C_3H`', 'C_3H &lt; R 时需区分报价', 'NAV < 1'])
def test_financial_variable_comparisons_are_not_html(text):
    assert ai._unsafe_text(text) is False


@pytest.mark.parametrize('text', ['<script>alert(1)</script>', 'X <SCRIPT>alert(1)</SCRIPT>',
    'R <A href="https://example.invalid">', 'R <B onclick="alert(1)">', '<svg/onload=alert(1)>',
    '&lt;IMG src=x onerror=alert(1)&gt;', '<iframe src=x>', 'javascript:alert(1)'])
def test_comparison_exception_does_not_allow_tags_or_handlers(text):
    assert ai._unsafe_text(text) is True


def test_snapshot_caption_is_metadata_not_an_unsupported_financial_quantity():
    titles = ['合成方法（2020年12月快照）']
    ai._check_grounded_text('本次证据为2020年12月快照，不代表现行规则。', '原始规则正文', source_titles=titles)
    for text in ['应持有2020年。', '价格为12元。', '按2021年12月快照执行。']:
        with pytest.raises(ai.AnswerValidationError, match='NUMERIC_UNIT_SUPPORT_MISSING'):
            ai._check_grounded_text(text, '原始规则正文', source_titles=titles)


def test_typed_condition_is_not_an_execution_claim_but_action_still_is():
    text = '估值日处于回售登记日至实际收款日之间（已确认行使回售权）'
    ai._check_grounded_text(text, '原文定义回售登记日至实际收款日的规则', completion=False)
    for text,completion in [('已完成审批。',True), ('本助手已确认回售。',False), ('已为你完成审批。',False), ('我已经为您确认回售。',False)]:
        with pytest.raises(ai.AnswerValidationError, match='EXECUTION_OR_APPROVAL_UNVERIFIED'):
            ai._check_grounded_text(text, '规则要求先核对回售状态', completion=completion)
    with pytest.raises(ai.AnswerValidationError, match='NUMERIC_UNIT_SUPPORT_MISSING'):
        ai._check_grounded_text('回售后7日', '规则要求先核对回售状态', completion=False)


def test_synthesis_receives_preliminary_plan_only_as_unverified_not_evidence():
    plan = {'interpretation': '合成初步理解', 'initial_assessment': '合成待核对判断', 'search_queries': ['合成检索方向']}
    _, calls, _, _ = generate(preliminary=plan)
    body = json.loads(calls[0]['messages'][1]['content'])
    assert body['preliminary_analysis']['unverified'] is True
    assert body['preliminary_analysis']['plan'] == {key:value for key,value in plan.items() if key!='initial_assessment'}
    assert 'initial_assessment' not in body['preliminary_analysis']['plan']
    assert all('合成初步理解' not in item['text'] for item in body['evidence'])
    assert '核对、修正或否定' in calls[0]['messages'][0]['content']


def test_conditional_cards_keep_exact_source_condition_and_consequence():
    _, calls, _, records = generate()
    body = json.loads(calls[0]['messages'][1]['content'])
    assert body['conditional_clauses']
    for card in body['conditional_clauses']:
        assert card['condition_text'] + '，' + card['consequence_text'] in records[0]['text']
        assert card['evidence_id'] == 'E1'


@pytest.mark.parametrize('modify,code', [
    (lambda d: d.update(summary={}), 'ANSWER_CONTENT_SCHEMA_INVALID'),
    (lambda d: d.update(run_id=str(uuid4())), 'ANSWER_CONTENT_SCHEMA_INVALID'),
    (lambda d: d['claims'][0].update(evidence_ids=['E999']), 'CONTENT_EVIDENCE_ID_UNKNOWN'),
    (lambda d: d['analysis']['checks'][0].update(evidence_ids=['E999']), 'CONTENT_EVIDENCE_ID_UNKNOWN'),
    (lambda d: d['analysis'].update(checks=[]), 'PUBLIC_ANALYSIS_REQUIRED'),
    (lambda d: d['analysis']['branches'][0].update(action='已完成全部审批。'), 'EXECUTION_OR_APPROVAL_UNVERIFIED'),
    (lambda d: d['analysis']['checks'][0].update(reason='规定必须在7日内完成。'), 'NUMERIC_UNIT_SUPPORT_MISSING'),
    (lambda d: d['analysis'].update(interpretation='<script>alert(1)</script>'), 'UNSAFE_MODEL_OUTPUT'),
    (lambda d: d['claims'][0].update(text='该规则不适用于停牌，所以禁止使用指数收益法。'), 'NAMED_METHOD_SUPPORT_MISSING'),
    (lambda d: d['analysis']['branches'][0].update(action='应改用AAP模型。'), 'NAMED_METHOD_SUPPORT_MISSING'),
])
def test_compact_rejection_never_becomes_extractive_success(modify,code):
    data = draft(); modify(data)
    with pytest.raises(ai.AnswerValidationError, match='SYNTHESIS_OUTPUT_REJECTED') as caught:
        generate(data)
    assert caught.value.diagnostic['cause'] == code


def test_schema_diagnostic_has_safe_path_not_values():
    from fund_kb.answer_content import safe_schema_errors
    from jsonschema import Draft202012Validator
    data = draft(); data['summary'] = {'sensitive-input':'not for log'}
    errors = safe_schema_errors(Draft202012Validator(schema(ai.answer_validator().schema)).iter_errors(data))
    assert errors == [{'path':'$.summary','rule':'type'}]
    assert 'sensitive-input' not in json.dumps(errors)


def test_citation_registry_is_not_rewritten_or_mutated():
    records = source()
    baseline = ai._evidence_answer('无报价','answer',{},records,str(uuid4()),answer_scope='reference')
    before = copy.deepcopy(baseline)
    result = assemble(draft(),baseline,baseline['citations'],ai.answer_validator().schema)
    result['citations'][0]['excerpt'] = 'test mutation'
    assert baseline == before


def test_model_request_is_smaller_than_legacy_system_and_input_for_same_source():
    result,calls,_,records = generate()
    model_text = json.dumps(calls[0]['messages'],ensure_ascii=False)
    from fund_kb.answer_prompt import build_answer_system_prompt
    assert len(calls[0]['messages'][0]['content']) < len(build_answer_system_prompt('reference'))
    assert len(model_text.encode()) < 40000
