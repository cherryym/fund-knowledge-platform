"""Synthetic model reply for the server-owned citation content contract."""
import copy


def reply_for(payload):
    if 'output_skeleton' in payload:
        return copy.deepcopy(payload['output_skeleton'])
    evidence = payload['evidence'][0]
    return {'status':'ANSWERED', 'summary':evidence['text'],
        'claims':[{'text':evidence['text'],'evidence_ids':[evidence['id']]}],
        'analysis':{'interpretation':'按本轮问题核对适用资料。',
            'checks':[{'title':'资料依据','reason':evidence['text'],'evidence_ids':[evidence['id']]}], 'branches':[]},
        'limitations':[]}
