"""Real central HTTP/SQLite maintenance tests; synthetic corpus, zero model calls.

The extension is mounted in memory when main has not yet registered it. Existing
api.py and existing tests are never edited by this worker.
"""
from __future__ import annotations

import copy
import inspect
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.dialects import mysql, oracle

from fund_kb import api
from fund_kb import api_wiki_maintenance as routes
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb import wiki_maintenance as wm
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.settings import Settings
from test_wiki import WikiClient, grant, page, uid


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Read/write guards and schema validation remain the real central dispatcher.
    if '"api_wiki_maintenance"' not in inspect.getsource(api.create_app):
        original = api.importlib.import_module

        def import_module(name):
            base = original(name)
            if name != "fund_kb.api_admin_review":
                return base
            return SimpleNamespace(__name__=base.__name__, PATHS={**base.PATHS, **routes.PATHS},
                SCHEMAS={**base.SCHEMAS, **routes.SCHEMAS}, HANDLERS={**base.HANDLERS, **routes.HANDLERS},
                replay_authority=lambda ctx, cached: routes.replay_authority(ctx, cached)
                    if ctx.operation in routes.HANDLERS else base.replay_authority(ctx, cached))

        monkeypatch.setattr(api, "importlib", SimpleNamespace(import_module=import_module))
    monkeypatch.setattr(api, "READ_ONLY_OPERATIONS", api.READ_ONLY_OPERATIONS | routes.READ_ONLY_OPERATIONS)
    settings = Settings(app_env="development", auth_mode="demo", storage_dir=tmp_path / "files",
        database_url=f"sqlite:///{tmp_path / 'maintenance.sqlite'}", allowed_origins=["http://testserver"],
        retrieval_mode="wiki", answer_engine="wiki_reader")
    app = api.create_app(settings)
    app.state.raise_test_errors = True
    owner, reader, space = uid(), uid(), uid()
    with TestClient(app) as client:
        with app.state.session_factory.begin() as db:
            db.add_all([m.User(id=owner, external_subject="demo:maint-editor", display_name="编辑甲"),
                m.User(id=reader, external_subject="demo:maint-reader", display_name="读者乙"),
                m.Space(id=space, name="合成估值知识库")])
            db.flush()
            for actor, roles in ((owner, ["reader", "editor", "reviewer", "publisher", "admin"]), (reader, ["reader"])):
                db.add_all(m.SpaceMember(space_id=space, user_id=actor, role=role) for role in roles)
        instance = WikiClient(app, client, owner, reader, space)
        instance.login()
        yield instance


def meta(env, rid):
    response = env.call("GET", f"/wiki/entries/{rid}/maintenance")
    assert response.status_code == 200, response.text
    assert response.headers["etag"] == response.json()["etag"]
    return response.json()


def save(env, rid, aliases, *, key=None, etag=None, canonical_key=None):
    return env.call("PUT", f"/wiki/entries/{rid}/maintenance", {"canonical_key": canonical_key,
        "aliases": aliases, "reason": "统一术语入口"}, etag=etag or meta(env, rid)["etag"], key=key)


def propose(env, ids, kind="REVISION", **extra):
    response = env.call("POST", "/wiki/maintenance/proposals", {"space_id": env.space,
        "kind": kind, "resource_ids": ids, "reason": "需审阅并保留历史版本", **extra})
    assert response.status_code == 201, response.text
    return response.json()


def review(env, proposal, decision="ACCEPT", *, key=None, etag=None):
    return env.call("POST", f'/wiki/maintenance/proposals/{proposal["id"]}/review',
        {"decision": decision, "comment": "人工维护审阅，不作为专业核验"}, etag=etag or proposal["etag"], key=key)


def scan(env, **extra):
    response = env.call("POST", "/wiki/maintenance/scans", {"space_id": env.space, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def get_proposal(env, pid):
    response = env.call("GET", f"/wiki/maintenance/proposals/{pid}")
    assert response.status_code == 200, response.text
    return response.json()


def native_block(text, ordinal=0, citations=()):
    return {"block_id": uid(), "ordinal": ordinal, "block_type": "paragraph",
            "data": {"text": text, "text_format": "markdown"}, "locator": {}, "citations": list(citations)}


def context(env, db):
    request = SimpleNamespace(app=env.app, state=SimpleNamespace(trace_id=uid()), headers={}, path_params={})
    return svc.Context(request=request, db=db, user=db.get(m.User, env.owner), data={}, query={}, operation="compiledWiki")


def source_snapshot(env, source):
    with env.db() as db:
        version = db.get(m.ResourceVersion, source[1])
        resource = db.get(m.Resource, source[0])
        blob = db.get(m.Blob, version.source_blob_id) if version.source_blob_id else None
        return {"resource_id": resource.id, "version_id": version.id, "access_epoch": resource.access_epoch,
                "content_sha256": svc.check_frozen_hash(db, version), "source_blob_sha256": blob.sha256 if blob else None}


def compiled_proposal(env, target, candidate, snapshots, job_id=None):
    with env.db.begin() as db:
        return wm.propose_compiled_revision(context(env, db), target[0], candidate, snapshots, compilation_job_id=job_id)


def test_contract_get_is_readonly_and_alias_save_is_audited_idempotent(env):
    rid, vid, _ = page(env, "停牌股票估值", tags=["alias:停牌估值", "需复核"])
    original = meta(env, rid)
    assert original["aliases"] == ["停牌估值"] and original["canonical_resource_id"] == rid
    assert original["revision"] == 0 and original["can_edit"]
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.RuntimePolicy)) == 0
        baseline = svc.version_dict(db, db.get(m.ResourceVersion, vid))
    key = uid()
    saved = save(env, rid, ["交易暂停估值"], etag=original["etag"], canonical_key="估值.停牌", key=key)
    assert saved.status_code == 200, saved.text
    replay = save(env, rid, ["交易暂停估值"], etag=original["etag"], canonical_key="估值.停牌", key=key)
    assert replay.status_code == 200 and replay.json() == saved.json()
    assert saved.json()["etag"] != original["etag"]
    with env.db() as db:
        assert svc.version_dict(db, db.get(m.ResourceVersion, vid)) == baseline
        assert db.get(m.Resource, rid).tags == ["alias:停牌估值", "需复核"]
        assert db.scalar(select(func.count()).select_from(m.AuditEvent)
                         .where(m.AuditEvent.action == "wiki.maintenance.metadata_saved")) == 1
    resolved = env.call("GET", f"/wiki/maintenance/resolve?space_id={env.space}&title=交易暂停估值")
    assert resolved.status_code == 200 and resolved.json()["resource_id"] == rid
    removed = env.call("GET", f"/wiki/maintenance/resolve?space_id={env.space}&title=停牌估值")
    assert removed.status_code == 404


def test_csrf_auth_etag_and_schema_cannot_be_bypassed(env):
    rid, _, _ = page(env)
    tag = meta(env, rid)["etag"]
    path = f"/wiki/entries/{rid}/maintenance"
    payload = {"canonical_key": None, "aliases": ["交易费用"], "reason": "术语归类"}
    assert env.client.put("/api/v1" + path, json=payload).status_code == 403
    no_etag = env.call("PUT", path, payload)
    assert no_etag.status_code in {400, 422, 428}
    assert env.call("PUT", path, {**payload, "canonical_resource_id": uid()}, etag=tag).status_code == 422
    env.client.cookies.clear()
    assert env.call("GET", path).status_code == 401


@pytest.mark.parametrize("aliases,key", [(["ＡBC", "abc"], None), (["费用\u202e"], None),
    (["[[停牌]]"], None), (["<script>"], None), ([" "], None), (["费用"], "费用")])
def test_alias_validation(env, aliases, key):
    rid, _, _ = page(env)
    assert save(env, rid, aliases, canonical_key=key).status_code == 422


def test_alias_conflicts_in_visible_domain_but_hidden_match_not_an_oracle(env):
    left = page(env, "股票估值")
    page(env, "公开可见标题")
    hidden = page(env, "秘密口径", restricted=True)
    conflict = save(env, left[0], ["公开可见标题"])
    assert conflict.status_code == 409 and conflict.json()["code"] == "WIKI_ALIAS_CONFLICT"
    hidden_match = save(env, left[0], ["秘密口径"])
    assert hidden_match.status_code == 200, hidden_match.text
    assert hidden[0] not in hidden_match.text
    assert env.call("GET", f"/wiki/entries/{hidden[0]}/maintenance").status_code == 404
    # Once both matches are genuinely visible, ambiguity is explicit.
    grant(env, hidden[0], env.owner)
    result = env.call("GET", f"/wiki/maintenance/resolve?space_id={env.space}&title=秘密口径")
    assert result.status_code == 409 and result.json()["code"] == "WIKI_TITLE_AMBIGUOUS"


def test_reader_cannot_write_or_review_and_replay_rechecks_editor_role(env):
    rid, _, _ = page(env)
    tag, key = meta(env, rid)["etag"], uid()
    assert save(env, rid, ["计提费用"], etag=tag, key=key).status_code == 200
    proposal = propose(env, [rid])
    env.login(env.reader)
    view = meta(env, rid)
    assert not view["can_edit"]
    assert save(env, rid, ["费率"], etag=view["etag"]).status_code == 403
    assert review(env, proposal).status_code == 403
    with env.db.begin() as db:
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.user_id == env.owner, m.SpaceMember.role == "editor"))
    env.login()
    assert save(env, rid, ["计提费用"], etag=tag, key=key).status_code == 403


def test_stale_alias_etag_and_two_concurrent_writers(env):
    rid, _, _ = page(env)
    etag = meta(env, rid)["etag"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda name: save(env, rid, [name], etag=etag), ["定义一", "定义二"]))
    assert sorted(r.status_code for r in responses) == [200, 412]
    assert save(env, rid, ["第三个"], etag=etag).status_code == 412


def test_catalog_is_metadata_only_filters_scope_and_hides_unlisted_target(env, monkeypatch):
    left = page(env, "左知识", tags=["alias:左别名"])
    right = page(env, "右知识")
    p = propose(env, [left[0], right[0]], "CONSOLIDATION", target_resource_id=right[0])
    assert review(env, p).status_code == 200
    statements = []
    event.listen(env.app.state.engine, "before_cursor_execute", lambda c, cur, sql, *args: statements.append(sql))
    monkeypatch.setattr(svc, "resource_access", lambda *a, **kw: pytest.fail("catalog must not call full body guard"))
    with env.db() as db:
        user = db.get(m.User, env.owner)
        both = wm.catalog_metadata(db, user, env.space, {left[0], right[0]})
        one = wm.catalog_metadata(db, user, env.space, {left[0]})
    assert both[left[0]]["canonical_resource_id"] == right[0]
    assert one[left[0]]["canonical_resource_id"] is None and not one[left[0]]["canonical_available"]
    assert right[0] not in str(one)
    assert not any("content_blocks" in sql.lower() for sql in statements)


def test_revision_accept_uses_new_draft_preserves_old_body_citations_and_provenance(env):
    source = page(env, "来源规则", kind="document")
    rid, vid, _ = page(env, "估值规则", cites=[source])
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=uid(), name="wiki-provenance:" + rid, updated_by=env.owner,
            config={"resource_id": rid, "source_mode": "published", "source_version_ids": [source[1]]}))
        db.flush()
        original_version = svc.version_dict(db, db.get(m.ResourceVersion, vid))
        original_release = db.get(m.Resource, rid).active_release_id
        original_provenance = copy.deepcopy(wm._policy(db, "wiki-provenance:" + rid).config)
    p = propose(env, [rid], draft_title="估值规则修订工作稿")
    key = uid()
    response = review(env, p, key=key)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "ACCEPTED" and data["snapshot_current"]
    assert data["result"]["action"] == "REVISION_DRAFT_CREATED" and data["result"]["requires_edit"]
    new_id = data["result"]["new_version_id"]
    replay = review(env, p, key=key)
    assert replay.status_code == 200 and replay.json() == data
    assert review(env, get_proposal(env, p["id"])).status_code == 409
    with env.db() as db:
        version = db.get(m.ResourceVersion, new_id)
        assert version.resource_id == rid and version.base_version_id == vid and version.version_no == 2
        assert version.state == "DRAFT" and version.origin == "COPY" and not version.source_verified
        assert version.content_sha256 is None
        assert svc.content_blocks(db, new_id) == original_version["blocks"]
        assert svc.version_dict(db, db.get(m.ResourceVersion, vid)) == original_version
        assert db.get(m.Resource, rid).active_release_id == original_release
        assert wm._policy(db, "wiki-provenance:" + rid).config == original_provenance
        provenance = wm._policy(db, wm.REVISION_PREFIX + new_id).config
        assert provenance["original_provenance"] == original_provenance
        assert not provenance["formal_evidence_allowed"]
        assert db.scalar(select(func.count()).select_from(m.ResourceVersion)
                         .where(m.ResourceVersion.resource_id == rid)) == 2


def test_existing_draft_is_never_overwritten(env):
    rid, vid, _ = page(env, state="DRAFT")
    p = propose(env, [rid])
    response = review(env, p)
    assert response.status_code == 409 and response.json()["code"] == "DRAFT_EXISTS"
    assert get_proposal(env, p["id"])["status"] == "PROPOSED"
    with env.db() as db:
        assert db.get(m.ResourceVersion, vid).state == "DRAFT"
        assert db.scalar(select(func.count()).select_from(m.ResourceVersion)) == 1


def test_consolidation_only_maps_navigation_and_rejects_cycles(env):
    a = page(env, "统一术语", text="条件A。")
    b = page(env, "统一术语", text="条件B，不保证是重复知识。")
    before = {}
    with env.db() as db:
        before = {vid: svc.version_dict(db, db.get(m.ResourceVersion, vid)) for _, vid, _ in (a, b)}
    p = propose(env, [a[0], b[0]], "CONSOLIDATION", target_resource_id=b[0])
    accepted = review(env, p)
    assert accepted.status_code == 200, accepted.text
    assert meta(env, a[0])["canonical_resource_id"] == b[0]
    resolved = env.call("GET", f"/wiki/maintenance/resolve?space_id={env.space}&title=统一术语")
    assert resolved.status_code == 200 and resolved.json()["resource_id"] == b[0]
    assert set(resolved.json()["matched_resource_ids"]) == {a[0], b[0]}
    cycle = env.call("POST", "/wiki/maintenance/proposals", {"space_id": env.space, "kind": "CONSOLIDATION",
        "resource_ids": [a[0], b[0]], "target_resource_id": a[0], "reason": "试图反向归并"})
    assert cycle.status_code == 409 and cycle.json()["code"] == "CANONICAL_TARGET_NOT_ROOT"
    with env.db() as db:
        for rid, vid, _ in (a, b):
            assert db.get(m.Resource, rid).deleted_at is None
            assert svc.version_dict(db, db.get(m.ResourceVersion, vid)) == before[vid]


def test_source_update_creates_reviewable_revision_not_automatic_rewrite(env):
    old = page(env, "来源V1", kind="document", text="旧规则正文。")
    derived = page(env, "由来源形成的知识", cites=[old])
    new = page(env, "来源V2", kind="document", text="新规则待人工确认适用性。", resource_id=old[0])
    result = scan(env, kinds=["source_changes"])
    assert result["resource_count"] == 1 and result["created_count"] == 1
    proposal = result["items"][0]
    assert proposal["kind"] == "REVISION" and proposal["status"] == "PROPOSED"
    assert proposal["verification_status"] == "HEURISTIC_SUGGESTION"
    assert proposal["source_changes"] == [{"source_resource_id": old[0], "title": "来源V2", "old_version_id": old[1],
        "old_version_no": 1, "new_version_id": new[1], "new_version_no": 2, "new_state": "APPROVED",
        "change_type": "NEW_SOURCE_VERSION", "review_required": True}]
    again = scan(env, kinds=["source_changes"])
    assert again["created_count"] == 0 and again["existing_count"] == 1
    response = review(env, proposal)
    assert response.status_code == 200, response.text
    with env.db() as db:
        new_draft = response.json()["result"]["new_version_id"]
        cites = svc.evidence_links(db, new_draft)
        assert {c.to_version_id for c in cites} == {old[1]}
        assert db.get(m.ResourceVersion, derived[1]).state == "APPROVED"


def test_snapshot_staleness_blocks_accept_but_allows_explicit_rejection(env):
    rid, _, _ = page(env)
    p = propose(env, [rid])
    assert save(env, rid, ["后续修改别名"]).status_code == 200
    assert review(env, p).status_code == 412
    fresh = get_proposal(env, p["id"])
    assert not fresh["snapshot_current"]
    rejected_accept = review(env, fresh)
    assert rejected_accept.status_code == 409
    assert rejected_accept.json()["code"] == "MAINTENANCE_SNAPSHOT_CHANGED"
    rejected = review(env, fresh, "REJECT")
    assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"


def test_source_revoke_hides_proposal_list_detail_and_replay(env):
    source = page(env, "受限来源", kind="document", restricted=True)
    grant(env, source[0], env.owner)
    derived = page(env, "受来源权限约束知识", cites=[source])
    p = propose(env, [derived[0]])
    key = uid()
    assert review(env, p, "REJECT", key=key).status_code == 200
    with env.db.begin() as db:
        db.execute(delete(m.ResourceGrant).where(m.ResourceGrant.resource_id == source[0]))
    assert env.call("GET", f'/wiki/maintenance/proposals/{p["id"]}').status_code == 404
    response = env.call("GET", f"/wiki/maintenance/proposals?space_id={env.space}")
    assert response.status_code == 200 and response.json()["items"] == []
    assert derived[0] not in response.text and source[0] not in response.text
    assert review(env, p, "REJECT", key=key).status_code == 404


def test_cross_space_and_personal_library_boundary(env):
    left = page(env, "公开知识")
    other = uid()
    with env.db.begin() as db:
        db.add(m.Space(id=other, name="另一个库"))
        db.flush()
        db.add_all(m.SpaceMember(space_id=other, user_id=env.owner, role=role)
                   for role in ["reader", "editor", "reviewer", "publisher", "admin"])
    old_space, env.space = env.space, other
    right = page(env, "另一个库知识")
    env.space = old_space
    cross = env.call("POST", "/wiki/maintenance/proposals", {"space_id": env.space, "kind": "CONSOLIDATION",
        "resource_ids": [left[0], right[0]], "target_resource_id": left[0], "reason": "不允许跨库归并"})
    assert cross.status_code == 404
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=uid(), name="space-governance:" + env.space, updated_by=env.owner,
            config={"schema_version": 1, "space_id": env.space, "kind": "personal", "owner_id": env.owner}))
    env.login(env.reader)
    assert env.call("GET", f"/wiki/entries/{left[0]}/maintenance").status_code == 404
    assert env.call("GET", f"/wiki/maintenance/proposals?space_id={env.space}").status_code == 404


def test_team_editor_collaboration_without_false_business_approval(env):
    a, b = page(env, "口径甲"), page(env, "口径乙")
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=uid(), name="space-governance:" + env.space, updated_by=env.owner,
            config={"schema_version": 1, "space_id": env.space, "kind": "team", "owner_id": env.owner}))
        db.add(m.SpaceMember(space_id=env.space, user_id=env.reader, role="editor"))
    p = propose(env, [a[0], b[0]], "CONFLICT")
    env.login(env.reader)
    accepted = review(env, get_proposal(env, p["id"]))
    assert accepted.status_code == 200, accepted.text
    data = accepted.json()
    assert data["conflict_state"] == "OPEN" and data["review"]["business_verification"] == "NOT_EVALUATED"
    closed = env.call("POST", f'/wiki/maintenance/proposals/{p["id"]}/resolve',
        {"comment": "已登记适用日期差异，后续另行专业审核"}, etag=data["etag"])
    assert closed.status_code == 200, closed.text
    assert closed.json()["conflict_state"] == "RESOLVED"
    assert closed.json()["resolution"]["business_verification"] == "NOT_EVALUATED"
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.ReviewDecision)) == 0


def test_scan_idempotent_without_omitting_authorized_duplicate_candidates(env):
    left = page(env, "同一个口径", text="情况一")
    right = page(env, "同一个口径", text="情况二")
    page(env, "同一个口径", text="隐藏情况", restricted=True)
    body, key = {"space_id": env.space, "kinds": ["duplicates"]}, uid()
    first = env.call("POST", "/wiki/maintenance/scans", body, key=key)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["resource_count"] == 2 and data["created_count"] == 1 and not data["truncated"]
    assert set(data["items"][0]["resource_ids"]) == {left[0], right[0]}
    replay = env.call("POST", "/wiki/maintenance/scans", body, key=key)
    assert replay.status_code == 200 and replay.json() == data
    again = scan(env, kinds=["duplicates"])
    assert again["created_count"] == 0 and again["existing_count"] == 1
    page(env, "后来新增可见知识")
    assert env.call("POST", "/wiki/maintenance/scans", body, key=key).status_code == 409


def test_fuzzy_titles_same_category_type_and_complete_equal_body_are_suggestions(env):
    near_a = page(env, "股票停牌估值处理规则", text="原口径适用范围不同。", category="股票")
    near_b = page(env, "股票停牌估值处理规范", text="新口径，不能假定完全相同。", category="股票")
    outside = page(env, "股票停牌估值处理规程", text="不同比较域。", category="测试/其他")
    equal_a = page(env, "特殊情形甲", text="完整相同正文，引用效力仍需另核。", category="A")
    equal_b = page(env, "非相似标题乙", text="完整相同正文，引用效力仍需另核。", category="B")
    result = scan(env, kinds=["duplicates"])
    proposals = {frozenset(x["resource_ids"]): x for x in result["items"]}
    assert frozenset((near_a[0], near_b[0])) in proposals
    fuzzy = proposals[frozenset((near_a[0], near_b[0]))]
    assert fuzzy["detection"]["title_similarity"] >= fuzzy["detection"]["title_similarity_threshold"] == 0.82
    assert "TITLE_SIMILARITY_SAME_CATEGORY_TYPE" in fuzzy["detection"]["methods"]
    assert fuzzy["verification_status"] == "HEURISTIC_SUGGESTION" and fuzzy["status"] == "PROPOSED"
    exact = proposals[frozenset((equal_a[0], equal_b[0]))]
    assert "EXACT_COMPLETE_BODY" in exact["detection"]["methods"] and len(exact["detection"]["body_sha256"]) == 64
    assert all(outside[0] not in x["resource_ids"] for x in result["items"])
    assert not result["model_invoked"] and not result["truncated"]


def test_exact_body_scan_uses_last_block_not_excerpt(env):
    a, b = page(env, "非相似甲", text="相同前缀", category="A"), page(env, "不同标题乙", text="相同前缀", category="B")
    with env.db.begin() as db:
        for source, suffix in ((a, "尾部甲"), (b, "尾部乙")):
            version = db.get(m.ResourceVersion, source[1])
            for n in range(1, 102):
                data = {"text": f"全文第{n}段" if n < 101 else suffix}
                text = block_text({"block_type": "paragraph", "data": data})
                db.add(m.ContentBlock(version_id=version.id, block_id=uid(), ordinal=n, block_type="paragraph",
                    data=data, locator={}, search_text=text, content_sha256=text_sha256(text)))
            db.flush()
            version.content_sha256 = svc.check_frozen_hash(db, version)
    assert scan(env, kinds=["duplicates"])["items"] == []


def test_catalog_over_one_thousand_ids_is_complete_without_body_loading(env):
    ids = [uid() for _ in range(1007)]
    with env.db.begin() as db:
        db.add_all(m.Resource(id=rid, space_id=env.space, name=f"条目{n}", owner_id=env.owner,
            kind="knowledge", tags=[f"alias:别名{n}"]) for n, rid in enumerate(ids))
    statements = []
    event.listen(env.app.state.engine, "before_cursor_execute", lambda c, cur, sql, *args: statements.append(sql))
    with env.db() as db:
        result = wm.catalog_metadata(db, db.get(m.User, env.owner), env.space, ids)
    assert set(result) == set(ids) and result[ids[-1]]["aliases"] == ["别名1006"]
    assert not any("content_blocks" in sql.lower() or "resource_versions" in sql.lower() for sql in statements)


def test_compiled_candidate_is_previewable_full_text_and_applied_only_to_new_draft(env):
    source = page(env, "估值来源", kind="document")
    target = page(env, "已有主知识", text="原稿完整保留", cites=[source])
    cites = [{"version_id": source[1], "block_id": source[2], "purpose": "RULE"}]
    blocks = [native_block(f"## 第{n}个完整规则\n\n" + "适用条件及例外完整保留。" * 20, n, cites) for n in range(105)]
    candidate = {"title": "已有主知识的新编译候选", "blocks": blocks, "knowledge_type": "rule"}
    snap = source_snapshot(env, source)
    job_id = uid()
    proposed = compiled_proposal(env, target, candidate, [snap], job_id)
    assert proposed["origin"] == "COMPILER" and proposed["has_compiled_candidate"]
    assert proposed["candidate_block_count"] == 105 and proposed["application_blockers"] == []
    assert "compiled_revision" not in proposed
    same = compiled_proposal(env, target, candidate, [snap], job_id)
    assert same["id"] == proposed["id"]
    previous_compilation = {"schema_version": 1, "profile": "topic", "marker": "original"}
    next_compilation = {"schema_version": 1, "profile": "atomic_rule", "marker": "new_candidate"}
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=uid(), name="wiki-compilation:" + target[1], updated_by=env.owner,
                               config=previous_compilation))
        policy = db.get(m.RuntimePolicy, proposed["id"])
        policy.config = {**policy.config, "compilation_metadata": next_compilation}
    proposed = get_proposal(env, proposed["id"])
    preview = env.call("GET", f'/wiki/maintenance/proposals/{proposed["id"]}?include_candidate=true')
    assert preview.status_code == 200, preview.text
    assert preview.json()["compiled_revision"]["candidate"]["blocks"] == blocks
    assert preview.json()["compiled_revision"]["source_snapshots"] == [snap]
    listed = env.call("GET", f"/wiki/maintenance/proposals?space_id={env.space}")
    assert listed.status_code == 200 and "compiled_revision" not in listed.json()["items"][0]
    with env.db() as db:
        before = svc.version_dict(db, db.get(m.ResourceVersion, target[1]))
        assert db.scalar(select(func.count()).select_from(m.ResourceVersion)
                         .where(m.ResourceVersion.resource_id == target[0])) == 1
    accepted = review(env, proposed)
    assert accepted.status_code == 200, accepted.text
    new_id = accepted.json()["result"]["new_version_id"]
    assert accepted.json()["result"]["compiled_candidate_applied"]
    with env.db() as db:
        new = db.get(m.ResourceVersion, new_id)
        assert new.resource_id == target[0] and new.base_version_id == target[1]
        assert new.state == "DRAFT" and not new.source_verified
        assert new.origin == "AI_DRAFT" and new.legal_status == "UNKNOWN"
        assert svc.content_blocks(db, new_id) == blocks
        assert svc.version_dict(db, db.get(m.ResourceVersion, target[1])) == before
        provenance = wm._policy(db, wm.REVISION_PREFIX + new_id).config
        assert provenance["compiled_source_snapshots"] == [snap]
        assert len(provenance["compiled_revision_digest"]) == 64
        assert wm._policy(db, "wiki-compilation:" + new_id).config == {**next_compilation,
            "resource_id": target[0], "version_id": new_id, "space_id": env.space,
            "compiled_content_sha256": new.content_sha256}
        assert wm._policy(db, "wiki-compilation:" + target[1]).config == previous_compilation


def test_compiled_unadmitted_draft_source_is_retained_but_cannot_bypass_existing_governance(env):
    source = page(env, "新来源草稿", kind="document", state="DRAFT")
    target = page(env, "旧知识")
    block = native_block("完整候选保留等待来源治理", citations=[{"version_id": source[1], "block_id": source[2], "purpose": "RULE"}])
    p = compiled_proposal(env, target, {"title": "候选", "blocks": [block]}, [source_snapshot(env, source)])
    assert p["application_blockers"] == ["COMPILED_REVISION_SOURCE_NOT_ADMITTED"]
    preview = env.call("GET", f'/wiki/maintenance/proposals/{p["id"]}?include_candidate=true')
    assert preview.status_code == 200 and preview.json()["compiled_revision"]["candidate"]["blocks"] == [block]
    failed = review(env, p)
    assert failed.status_code == 409 and failed.json()["code"] == "COMPILED_REVISION_SOURCE_NOT_ADMITTED"
    assert get_proposal(env, p["id"])["status"] == "PROPOSED"
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.ResourceVersion)
                         .where(m.ResourceVersion.resource_id == target[0])) == 1


def test_compiled_snapshot_and_exact_citations_are_required(env):
    source = page(env, "已发布来源", kind="document")
    target = page(env, "旧知识")
    block = native_block("不能捏造来源", citations=[{"version_id": source[1], "block_id": uid(), "purpose": "RULE"}])
    snap = source_snapshot(env, source)
    with pytest.raises(svc.APIError) as error:
        compiled_proposal(env, target, {"title": "新稿", "blocks": [block]}, [snap])
    assert error.value.code == "INVALID_COMPILED_CITATION"
    block["citations"][0]["block_id"] = source[2]
    with pytest.raises(svc.APIError) as error:
        compiled_proposal(env, target, {"title": "新稿", "blocks": [block]}, [{**snap, "access_epoch": 0}])
    assert error.value.code == "COMPILED_SOURCE_SNAPSHOT_CHANGED"
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.RuntimePolicy)
            .where(m.RuntimePolicy.name.like(wm.PROPOSAL_PREFIX + "%"))) == 0


def test_compiled_source_permission_rechecked_before_preview_and_accept(env):
    source = page(env, "保密来源", kind="document", restricted=True)
    grant(env, source[0], env.owner)
    target = page(env, "旧知识")
    block = native_block("候选中的保密正文不应泄漏", citations=[{"version_id": source[1], "block_id": source[2], "purpose": "FACT"}])
    p = compiled_proposal(env, target, {"title": "新稿", "blocks": [block]}, [source_snapshot(env, source)])
    with env.db.begin() as db:
        db.execute(delete(m.ResourceGrant).where(m.ResourceGrant.resource_id == source[0]))
    response = env.call("GET", f'/wiki/maintenance/proposals/{p["id"]}?include_candidate=true')
    assert response.status_code == 404 and "候选中的保密正文" not in response.text
    assert review(env, p).status_code == 404


def test_empty_scan_replay_is_invalidated_by_alias_change_or_new_source_version(env):
    source = page(env, "来源一", kind="document")
    target = page(env, "独立知识", cites=[source])
    for mutation in ("alias", "source"):
        key = uid()
        body = {"space_id": env.space, "kinds": ["source_changes"]}
        response = env.call("POST", "/wiki/maintenance/scans", body, key=key)
        assert response.status_code == 200 and response.json()["items"] == []
        if mutation == "alias":
            assert save(env, target[0], ["新录入别名"]).status_code == 200
        else:
            page(env, "新版来源", kind="document", resource_id=source[0])
        replay = env.call("POST", "/wiki/maintenance/scans", body, key=key)
        assert replay.status_code == 409 and replay.json()["code"] == "MAINTENANCE_REPLAY_CHANGED"


def test_portable_lock_statements_compile_for_oracle_oceanbase():
    queries = [select(m.Space).where(m.Space.id == uid()).with_for_update(),
        select(m.Resource).where(m.Resource.id == uid()).with_for_update(),
        select(m.ResourceVersion).where(m.ResourceVersion.id == uid()).with_for_update(),
        select(m.RuntimePolicy).where(m.RuntimePolicy.name.like("wiki-maint-proposal:%"))]
    for dialect in (oracle.dialect(), mysql.dialect()):
        for query in queries:
            sql = str(query.compile(dialect=dialect))
            assert "sqlite" not in sql.lower() and "json_extract" not in sql.lower()
