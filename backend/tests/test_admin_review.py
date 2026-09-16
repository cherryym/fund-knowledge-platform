"""Real local HTTP, immutable sources, original-only release, no model/network."""
import copy

import pytest
from sqlalchemy import delete, select
from test_wiki import env, provider, page
from test_wiki_unverified import draft_source, build_draft

from fund_kb import admin_review, models as m, services as svc, wiki
from fund_kb.jobs import JobDispatcher


@pytest.fixture
def release_worker(env):
    dispatcher = JobDispatcher(env.settings, env.db, None)
    env.app.state.job_dispatcher = dispatcher.run
    yield dispatcher
    dispatcher.close()


def real_source(env, *, empty=False):
    source = draft_source(env)
    with env.db.begin() as db:
        v = db.get(m.ResourceVersion, source[1])
        blob = db.get(m.Blob, v.source_blob_id)
        text = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == v.id)).data['text']
        env.app.state.storage.write_bytes(blob.object_key, text.encode())
        v.legal_status = 'UNKNOWN'
        v.valid_from = None
        if empty:
            db.execute(delete(m.ContentBlock).where(m.ContentBlock.version_id == v.id))
    return source


def confirm(env, vid, **overrides):
    status = env.call('GET', f'/versions/{vid}/admin-review')
    assert status.status_code == 200, status.text
    payload = {'reviewed_sha256': status.json()['current_sha256'], 'reason': '管理员明确确认现有资料有效并同意发布',
        'confirm_valid_sources': True, 'acknowledge_not_independent': True, **overrides}
    return env.call('POST', f'/versions/{vid}/admin-review', payload, etag=status.headers['etag'])


def publish(env, vid):
    version = env.call('GET', f'/versions/{vid}')
    response = env.call('POST', f'/versions/{vid}/publish', etag=version.headers['etag'])
    assert response.status_code == 202, response.text
    result = env.call('GET', '/jobs/' + response.json()['id'])
    assert result.json()['state'] == 'SUCCEEDED', result.text
    return result.json()


@pytest.mark.parametrize('empty', [False, True])
def test_admin_source_publication_preserves_hash_dates_and_review_truth(env, release_worker, empty):
    source = real_source(env, empty=empty)
    with env.db() as db:
        v = db.get(m.ResourceVersion, source[1])
        frozen = svc.check_frozen_hash(db, v)
        epoch = db.get(m.Resource, source[0]).access_epoch
    r = confirm(env, source[1])
    assert r.status_code == 200, r.text
    assert r.json()['confirmation']['independent_review'] is False
    publish(env, source[1])
    with env.db() as db:
        v = db.get(m.ResourceVersion, source[1])
        assert v.state == 'APPROVED' and v.source_verified
        assert svc.check_frozen_hash(db, v) == frozen == v.content_sha256
        assert v.legal_status == 'UNKNOWN' and v.valid_from is None
        assert db.get(m.Resource, source[0]).access_epoch == epoch
        assert not svc.independent_review(db, v)
        assert not list(db.scalars(select(m.ReviewDecision)))
        release = svc.released(db, v.id)
        assert release.manifest['review']['mode'] == 'ADMIN_CONFIRMED'
        assert release.manifest['content_mode'] == ('original_only' if empty else 'parsed')
        assert not svc.evidence_version_eligible(db, db.get(m.User, env.owner), v)


def test_wiki_source_first_then_publish_preserves_graph_and_future_build(env, provider, release_worker):
    source = real_source(env)
    _, built = build_draft(env, source)
    vid, rid = built['created_version_ids'][0], built['created_resource_ids'][0]
    with env.db() as db:
        original = copy.deepcopy(wiki._policy(db, f'wiki-provenance:{rid}').config)
    denied = confirm(env, vid)
    assert denied.status_code == 409 and denied.json()['code'] == 'ADMIN_REVIEW_SOURCE_NOT_PUBLISHED'
    assert confirm(env, source[1]).status_code == 200
    publish(env, source[1])
    approved = confirm(env, vid)
    assert approved.status_code == 200, approved.text
    publish(env, vid)
    with env.db() as db:
        assert wiki._policy(db, f'wiki-provenance:{rid}').config == original
        v = db.get(m.ResourceVersion, vid)
        assert admin_review.confirmation(db, v)
        assert not svc.evidence_version_eligible(db, db.get(m.User, env.owner), v)
        sources, _ = wiki.choose_build_sources(db, db.get(m.User, env.owner), env.space, [source[0]], source_mode='unverified_draft')
        assert sources
    graph = env.call('GET', f'/wiki/graph?space_id={env.space}').json()
    assert {source[0], rid}.issubset({n['id'] for n in graph['nodes']})
    assert any(e['source'] == rid and e['target'] == source[0] for e in graph['edges'])


@pytest.mark.parametrize('mode', ['reader', 'hash', 'confirm', 'quarantine', 'source_change', 'stale_etag'])
def test_admin_confirmation_fails_closed(env, mode):
    source = real_source(env)
    status = env.call('GET', f'/versions/{source[1]}/admin-review')
    data = {'reviewed_sha256': status.json()['current_sha256'], 'reason': '测试管理员明确确认授权发布',
        'confirm_valid_sources': True, 'acknowledge_not_independent': True}
    if mode == 'reader': env.login(env.reader)
    if mode == 'hash': data['reviewed_sha256'] = '0' * 64
    if mode == 'confirm': data['acknowledge_not_independent'] = False
    if mode in {'quarantine', 'source_change', 'stale_etag'}:
        with env.db.begin() as db:
            v = db.get(m.ResourceVersion, source[1])
            if mode == 'quarantine': db.get(m.Blob, v.source_blob_id).scan_state = 'QUARANTINED'
            if mode == 'source_change': v.title += ' changed'
            if mode == 'stale_etag': v.revision += 1
    result = env.call('POST', f'/versions/{source[1]}/admin-review', data, etag=status.headers['etag'])
    assert result.status_code in {403, 404, 409, 412, 422}, result.text
    with env.db() as db:
        assert db.get(m.ResourceVersion, source[1]).state == 'DRAFT'
        assert not db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like('admin-review:%')))


def test_changed_approved_body_and_revoked_authority_cannot_publish(env, release_worker):
    source = real_source(env)
    assert confirm(env, source[1]).status_code == 200
    with env.db.begin() as db:
        v = db.get(m.ResourceVersion, source[1]); v.title += ' mutated'
    version = env.call('GET', f'/versions/{source[1]}')
    denied = env.call('POST', f'/versions/{source[1]}/publish', etag=version.headers['etag'])
    assert denied.status_code == 409
