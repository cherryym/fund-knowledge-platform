import json

import pytest

from fund_kb.provider_stream import ResponseStream, StreamError


def event(kind, **payload):
    return ('event: '+kind+'\r\ndata: '+json.dumps({'type':kind, **payload}, ensure_ascii=False)+'\r\n\r\n').encode()


@pytest.mark.parametrize('width', [1, 2, 7, 4096])
def test_waits_for_full_terminal_and_handles_utf8_split_chunks(width):
    expected = {'status':'completed', 'output':[{'type':'message','content':[{'type':'output_text','text':'合成完整结果'}]}]}
    raw = event('response.output_text.delta', delta='这不是完整结果') + event('response.completed', response=expected)
    collector = ResponseStream(50000)
    for i in range(0,len(raw),width):
        collector.feed(raw[i:i+width])
    assert collector.finish() == expected


@pytest.mark.parametrize('raw', [event('response.output_text.delta',delta='只有部分输出'), b'data: [DONE]\n\n',
    b'data: invalid\n\n', event('response.completed',response={'status':'in_progress'}), b'\xff'])
def test_partial_malformed_or_non_terminal_stream_never_succeeds(raw):
    collector = ResponseStream(50000)
    with pytest.raises(StreamError):
        collector.feed(raw)
        collector.finish()


def test_incomplete_status_is_preserved_for_caller_rejection():
    collector = ResponseStream(50000)
    value = {'status':'incomplete', 'output':[], 'incomplete_details':{'reason':'max_output_tokens'}}
    assert collector.feed(event('response.incomplete',response=value)) == value


def test_size_limit_covers_deltas_not_just_final_response():
    with pytest.raises(StreamError, match='PROVIDER_RESPONSE_TOO_LARGE'):
        ResponseStream(32).feed(event('response.output_text.delta',delta='x'*100))
