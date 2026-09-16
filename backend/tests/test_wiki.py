"""Real HTTP/SQLite Wiki tests, with a synthetic Provider and no external model calls."""
from __future__ import annotations

import copy
import json
import threading
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb import wiki
from fund_kb.api import create_app
from fund_kb.api_wiki import HANDLERS, PATHS, SCHEMAS
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.settings import Settings


def uid():
    return str(uuid4())


class WikiClient:
    def __init__(self, app, client, owner, reader, space):
        self.app, self.client, self.owner, self.reader, self.space = app, client, owner, reader, space
        self.db = app.state.session_factory
        self.settings = app.state.settings
        self.connection = uid()
        self.csrf = ""

    def login(self, actor=None):
        result = self.client.post("/api/v1/auth/demo", json={"user_id": actor or self.owner}, headers={"Origin": "http://testserver"})
        assert result.status_code == 200, result.text
        self.csrf = result.json()["csrf_token"]

    def call(self, method, path, data=None, *, etag=None, key=None):
        headers = {"Origin": "http://testserver", "X-CSRF-Token": self.csrf, "Idempotency-Key": key or uid()}
        if etag is not None:
            headers["If-Match"] = etag
        return self.client.request(method, "/api/v1" + path, json=data, headers=headers)


@pytest.fixture
def env(tmp_path):
    settings = Settings(app_env="development", auth_mode="demo", storage_dir=tmp_path / "data",
                        database_url=f"sqlite:///{tmp_path / 'wiki.sqlite'}", allowed_origins=["http://testserver"],
                        retrieval_mode="wiki", answer_engine="structured")
    app = create_app(settings)
    app.state.raise_test_errors = True
    owner, reader, space = uid(), uid(), uid()
    with TestClient(app) as client:
        with app.state.session_factory.begin() as db:
            db.add_all([m.User(id=owner, external_subject="demo:wiki-editor", display_name="Wiki编辑者"),
                        m.User(id=reader, external_subject="demo:wiki-reader", display_name="Wiki读者"),
                        m.Space(id=space, name="合成Wiki空间")])
            db.flush()
            for actor, roles in ((owner, ["reader", "editor", "reviewer", "publisher", "admin"]), (reader, ["reader"])):
                for role in roles:
                    db.add(m.SpaceMember(space_id=space, user_id=actor, role=role))
        result = WikiClient(app, client, owner, reader, space)
        result.login()
        yield result


def page(env, title="费用口径", text="核对费用计提基数与适用日期。", *, kind="knowledge", category="估值/费用",
         tags=None, restricted=False, state="APPROVED", valid_from=None, valid_to=None, cites=(), resource_id=None):
    rid, vid, bid = resource_id or uid(), uid(), uid()
    with env.db.begin() as db:
        resource = db.get(m.Resource, rid)
        if not resource:
            resource = m.Resource(id=rid, space_id=env.space, kind=kind, name=title, category=category,
                                  tags=tags or [], owner_id=env.owner, restricted=restricted)
            db.add(resource)
            db.flush()
        versions = list(db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == rid)))
        blob_id = None
        if kind == "document":
            blob_id = uid()
            db.add(m.Blob(id=blob_id, space_id=env.space, object_key=f"synthetic/{blob_id}.txt",
                          sha256=text_sha256(text), size_bytes=len(text.encode()), mime_type="text/plain", scan_state="CLEAN"))
            db.flush()
        version = m.ResourceVersion(id=vid, resource_id=rid, version_no=len(versions) + 1, state="DRAFT",
                    author_id=env.owner, title=title, knowledge_type="source" if kind == "document" else
                    "solution_template" if kind == "template" else "faq", origin="UPLOAD" if kind == "document" else "HUMAN",
                    source_blob_id=blob_id, source_verified=kind == "document", legal_status="NOT_APPLICABLE",
                    valid_from=valid_from or date(2020, 1, 1), valid_to=valid_to, applicability={}, required_facts=[])
        db.add(version)
        db.flush()
        data = {"text": text, "text_format": "markdown"}
        canonical = block_text({"block_type": "paragraph", "data": data})
        db.add(m.ContentBlock(version_id=vid, block_id=bid, ordinal=0, block_type="paragraph", data=data,
                             locator={"label": "合成来源条款"}, search_text=canonical, content_sha256=text_sha256(canonical)))
        db.flush()
        for source in cites:
            db.add(m.EvidenceLink(id=uid(), from_version_id=vid, from_block_id=bid, to_version_id=source[1],
                                 to_block_id=source[2], purpose="FACT"))
        db.flush()
        version.content_sha256 = svc.check_frozen_hash(db, version)
        version.state = state
        if state == "APPROVED":
            if resource.active_release_id:
                db.get(m.Release, resource.active_release_id).state = "SUPERSEDED"
                db.flush()
            release = m.Release(id=uid(), resource_id=rid, version_id=vid, state="ACTIVE", publisher_id=env.owner,
                                activated_at=svc.now(), manifest={"synthetic": True})
            db.add(release)
            db.flush()
            resource.active_release_id = release.id
    return rid, vid, bid


def grant(env, rid, actor, permissions=("read",)):
    with env.db.begin() as db:
        for permission in permissions:
            db.add(m.ResourceGrant(resource_id=rid, user_id=actor, permission=permission))


class FakeProvider:
    def __init__(self, env):
        self.env, self.calls, self.requests = env, 0, []
        self.revision, self.available = 1, True
        self.after_call = None
        self.output_transform = None
        self.open_transactions = {}
        event.listen(env.app.state.engine, "begin", lambda conn: self.open_transactions.__setitem__(id(conn), threading.get_ident()))
        event.listen(env.app.state.engine, "commit", lambda conn: self.open_transactions.pop(id(conn), None))
        event.listen(env.app.state.engine, "rollback", lambda conn: self.open_transactions.pop(id(conn), None))

    def resolve_connection(self, db, user, space_id, connection_id, model_id, settings,
                           require_transfer=False, expected_revision=None):
        svc.space_access(db, user, space_id, "editor")
        assert require_transfer is True
        if not self.available:
            raise wiki.WikiBuildError("PROVIDER_NOT_CONFIGURED")
        if expected_revision is not None and expected_revision != self.revision:
            raise wiki.WikiBuildError("PROVIDER_REVISION_CHANGED")
        return {"id": connection_id, "revision": self.revision, "protocol": "openai", "base_url": "https://example.invalid/v1",
                "model_id": model_id, "provider_id": "synthetic", "brand": "test", "name": "模拟模型",
                "api_key": "synthetic-private-value-never-persist"}

    def public_snapshot(self, snapshot):
        # Even a provider accidentally returning its private dict must not cause
        # wiki.py to persist arbitrary fields.
        return dict(snapshot)

    def complete(self, snapshot, messages, max_tokens=4096, json_mode=True, timeout=60):
        assert threading.get_ident() not in self.open_transactions.values(), "model call must be outside DB transaction"
        self.calls += 1
        data = json.loads(messages[1]["content"])
        self.requests.append(data)
        semantic_mode = "source_dispositions" in data["schema"]["properties"]
        assert json_mode and max_tokens <= 4096 and 0 < timeout <= (180 if semantic_mode else 45)
        output = {"pages": [{"title": f"费用核对Wiki{self.calls}", "category": "估值/费用", "knowledge_type": "faq",
                    "aliases": [f"费用核对别名{self.calls}"], "links": [],
                    "blocks": [{"markdown": "根据来源，应核对费用计提基数与日期。", "evidence_ids": [data["sources"][0]["id"]]}]}], "gaps": []}
        if self.output_transform:
            output = self.output_transform(output, data)
        if self.after_call:
            self.after_call()
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100}}


@pytest.fixture
def provider(env, monkeypatch):
    fake = FakeProvider(env)
    monkeypatch.setattr(wiki, "_provider_module", lambda: fake)
    return fake


def build_job(env, sources, *, max_pages=3, key=None):
    response = env.call("POST", "/wiki/builds", {"space_id": env.space, "source_resource_ids": [s[0] for s in sources],
                    "model_selection": {"connection_id": env.connection, "model_id": "synthetic-test-model"},
                    "max_pages": max_pages, "consent": True}, key=key)
    assert response.status_code == 202, response.text
    jid = response.json()["id"]
    with env.db.begin() as db:
        job = db.get(m.Job, jid)
        job.state, job.attempts, job.lease_until = "RUNNING", 1, svc.now() + timedelta(minutes=2)
    return jid


def execute(env, jid, callback=None):
    return wiki.execute_build(env.settings, env.db, jid, 1, callback or (lambda stage, details: None))


def test_contract_registered_and_no_vector_or_model_required(env):
    assert len(HANDLERS) == 10
    assert "getWikiCompilationSpecs" in HANDLERS
    spec = env.app.openapi()
    assert all("/api/v1" + p in spec["paths"] or p in spec["paths"] for p in PATHS)
    assert SCHEMAS["WikiBuildInput"]["properties"]["source_resource_ids"]["maxItems"] == 8
    assert env.app.state.vector_index is None
    response = env.call("GET", f"/wiki/workspace?space_id={env.space}")
    assert response.status_code == 200 and response.json()["mode"] == "wiki"


def test_wikilinks_parse_aliases_ignore_code_and_escaped_links():
    links = wiki.parse_wikilinks("[[费用口径]] [[费用口径|别名]] [[份额类别|类别]] `[[代码中]]`\n```txt\n[[代码块]]\n```\n\\[[转义]] ![[嵌入]]")
    assert [link["title"] for link in links] == ["费用口径", "份额类别"]


def test_workspace_categories_tags_and_search_exclude_hidden_sources(env):
    page(env, "费用知识", tags=["费用", "alias:手续费"], text="费用计提基数应核对。")
    page(env, "隐藏知识", category="隐藏/目录", restricted=True)
    page(env, "来源文档", kind="document")
    response = env.call("GET", f"/wiki/workspace?space_id={env.space}&q=计提&category=估值")
    assert response.status_code == 200, response.text
    data = response.json()
    assert [p["name"] for p in data["pages"]] == ["费用知识"]
    assert "隐藏" not in json.dumps(data, ensure_ascii=False)
    assert data["tags"] == [{"name": "费用", "count": 1}]
    assert next(c for c in data["categories"] if c["path"] == "估值")["count"] == 1


def test_links_alias_resolution_and_hidden_duplicate_do_not_leak(env):
    target = page(env, "费用口径", tags=["alias:手续费"])
    hidden = page(env, "费用口径", restricted=True)
    origin = page(env, "基金核对", text="依据[[手续费|费用说明]]核对。[[未提供资料]]")
    response = env.call("GET", f"/wiki/resolve?space_id={env.space}&title=手续费")
    assert response.status_code == 200 and response.json()["resource_id"] == target[0]
    links = env.call("GET", f"/wiki/pages/{origin[0]}/links").json()
    assert links["outgoing"][0]["id"] == target[0]
    assert links["unresolved"] == [{"title": "未提供资料"}]
    back = env.call("GET", f"/wiki/pages/{target[0]}/links").json()
    assert back["incoming"][0]["id"] == origin[0]
    assert hidden[0] not in json.dumps(links)
    page(env, "另一个费用", tags=["alias:手续费"])
    assert env.call("GET", f"/wiki/resolve?space_id={env.space}&title=手续费").status_code == 409
    assert env.call("GET", f"/wiki/pages/{hidden[0]}/links").status_code == 404


def test_graph_includes_document_and_wiki_and_never_writes_relation_enum(env):
    source = page(env, "费用原始资料", kind="document")
    target = page(env, "日期口径")
    origin = page(env, "费用核对", text="核对[[日期口径]]。", cites=[source])
    hidden = page(env, "隐藏节点", restricted=True)
    result = env.call("GET", f"/wiki/graph?space_id={env.space}&focus_id={origin[0]}&depth=1").json()
    assert {n["id"] for n in result["nodes"]} == {source[0], target[0], origin[0]}
    assert {e["origin"] for e in result["edges"]} == {"citation", "wikilink"}
    assert hidden[0] not in json.dumps(result)
    assert all(e["source"] in {n["id"] for n in result["nodes"]} and e["target"] in {n["id"] for n in result["nodes"]} for e in result["edges"])
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.RelationEdge)) == 0
    bounded = env.call("GET", f"/wiki/graph?space_id={env.space}&limit=1").json()
    assert len(bounded["nodes"]) == 1 and bounded["truncated"] is True and not bounded["edges"]


def test_graph_version_time_and_revoked_dependency_are_current(env):
    old = page(env, "版本页面", text="旧版规则")
    newer = page(env, "版本页面", text="未来规则", resource_id=old[0], valid_from=date(2099, 1, 1))
    expired = page(env, "过期资料", valid_to=date(2021, 1, 1))
    current = env.call("GET", f"/wiki/graph?space_id={env.space}&business_date=2026-09-07").json()
    assert next(n for n in current["nodes"] if n["id"] == old[0])["version_id"] == old[1]
    assert newer[1] not in json.dumps(current) and expired[0] not in json.dumps(current)
    source = page(env, "受控原件", kind="document")
    derived = page(env, "派生知识", cites=[source])
    with env.db.begin() as db:
        db.get(m.Resource, source[0]).restricted = True
    assert env.call("GET", f"/wiki/pages/{derived[0]}/links").status_code == 404
    assert derived[0] not in env.call("GET", f"/wiki/graph?space_id={env.space}").text


def test_empty_taxonomy_crud_etag_and_nonempty_protection(env):
    first = env.call("GET", f"/wiki/taxonomy?space_id={env.space}")
    assert first.json()["revision"] == 0 and first.headers["etag"] == '"0"'
    created = env.call("POST", "/wiki/categories", {"space_id": env.space, "path": "空分类/子分类"})
    assert created.status_code == 201, created.text
    assert {c["path"] for c in created.json()["categories"]} == {"空分类", "空分类/子分类"}
    assert all(c["count"] == 0 for c in created.json()["categories"])
    missing_etag = env.call("PATCH", "/wiki/categories", {"space_id": env.space, "path": "空分类", "new_path": "新分类"})
    assert missing_etag.status_code == 428
    renamed = env.call("PATCH", "/wiki/categories", {"space_id": env.space, "path": "空分类", "new_path": "新分类"}, etag=created.headers["etag"])
    assert renamed.status_code == 200, renamed.text
    assert {c["path"] for c in renamed.json()["categories"]} == {"新分类", "新分类/子分类"}
    blocked = env.call("DELETE", "/wiki/categories", {"space_id": env.space, "path": "新分类"}, etag=renamed.headers["etag"])
    assert blocked.status_code == 409 and blocked.json()["code"] == "CATEGORY_NOT_EMPTY"
    deleted = env.call("DELETE", "/wiki/categories", {"space_id": env.space, "path": "新分类/子分类"}, etag=renamed.headers["etag"])
    assert deleted.status_code == 200, deleted.text
    assert env.call("DELETE", "/wiki/categories", {"space_id": env.space, "path": "新分类"}, etag=renamed.headers["etag"]).status_code == 412
    assert env.call("DELETE", "/wiki/categories", {"space_id": env.space, "path": "新分类"}, etag=deleted.headers["etag"]).status_code == 200


def test_taxonomy_rename_moves_resource_without_deleting_and_reader_cannot_write(env):
    resource = page(env)
    read = env.call("GET", f"/wiki/taxonomy?space_id={env.space}")
    moved = env.call("PATCH", "/wiki/categories", {"space_id": env.space, "path": "估值", "new_path": "运营核对"}, etag=read.headers["etag"])
    assert moved.status_code == 200, moved.text
    with env.db() as db:
        assert db.get(m.Resource, resource[0]).category == "运营核对/费用"
        assert db.get(m.ResourceVersion, resource[1]).state == "APPROVED"
    env.login(env.reader)
    assert env.call("POST", "/wiki/categories", {"space_id": env.space, "path": "新分类"}).status_code == 403


def test_model_build_creates_unpublished_cited_safe_draft_and_no_private_snapshot(env, provider):
    source = page(env, "来源文档", kind="document")
    jid = build_job(env, [source])
    assert provider.calls == 0
    result = execute(env, jid)
    assert provider.calls == 1 and result["model"]["called"] is True
    assert result["coverage"]["professional_accuracy"] == "NOT_EVALUATED"
    assert len(result["created_version_ids"]) == 1
    with env.db() as db:
        version = db.get(m.ResourceVersion, result["created_version_ids"][0])
        assert version.state == "DRAFT" and version.origin == "AI_DRAFT" and version.source_verified is False
        assert db.get(m.Resource, version.resource_id).active_release_id is None
        links = list(db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == version.id)))
        assert {(e.to_version_id, e.to_block_id) for e in links} == {(source[1], source[2])}
        assert version.content_sha256 == svc.check_frozen_hash(db, version)
        serialized = json.dumps([p.config for p in db.scalars(select(m.RuntimePolicy))])
        assert "synthetic-private-value" not in serialized
        assert "synthetic-private-value" not in json.dumps(db.get(m.Job, jid).payload)
    env.login(env.reader)
    assert result["created_resource_ids"][0] not in env.call("GET", f"/wiki/workspace?space_id={env.space}").text


def test_incremental_skip_and_crash_receipt_do_not_call_model_twice(env, provider):
    source = page(env, kind="document")
    first = build_job(env, [source])
    result = execute(env, first)
    replay = execute(env, first)
    assert provider.calls == 1 and replay["created_version_ids"] == result["created_version_ids"]
    second = execute(env, build_job(env, [source]))
    assert provider.calls == 1 and second["model"]["called"] is False
    assert second["coverage"]["status"] == "NO_NEW_CONTENT"
    assert second["created_resource_ids"] == []


def test_existing_human_draft_is_preserved(env, provider):
    human = page(env, "费用核对Wiki1", text="人工维护正文不可覆盖。", state="DRAFT")
    source = page(env, kind="document")
    result = execute(env, build_job(env, [source]))
    assert result["created_resource_ids"] == []
    assert result["skipped"][0]["reason"] == "EXISTING_VISIBLE_PAGE_PRESERVED"
    with env.db() as db:
        assert db.get(m.ContentBlock, (human[1], human[2])).search_text == "人工维护正文不可覆盖。"


def test_new_hidden_title_does_not_leak_or_block_visible_generated_page(env, provider):
    hidden = page(env, "费用核对Wiki1", state="DRAFT", restricted=True)
    source = page(env, kind="document")
    result = execute(env, build_job(env, [source]))
    assert result["created_resource_ids"] and not result["skipped"]
    assert hidden[0] not in json.dumps(result)
    assert "费用核对Wiki1" not in provider.requests[0]["existing_titles"]


@pytest.mark.parametrize("mutation", ["cancel_before", "cancel_after", "stale_attempt", "source_acl", "source_suspended", "connection_changed"])
def test_build_fence_and_revalidation_prevent_writes(env, provider, mutation):
    source = page(env, kind="document")
    jid = build_job(env, [source])
    def modify():
        with env.db.begin() as db:
            if mutation.startswith("cancel"):
                db.get(m.Job, jid).cancel_requested = True
            elif mutation == "stale_attempt":
                db.get(m.Job, jid).attempts = 2
            elif mutation == "source_acl":
                db.get(m.Resource, source[0]).restricted = True
            elif mutation == "source_suspended":
                db.get(m.Resource, source[0]).suspended = True
            elif mutation == "connection_changed":
                provider.revision += 1
    if mutation == "cancel_before":
        def checkpoint(stage, details):
            if stage == "WIKI_GENERATING":
                modify()
    else:
        provider.after_call = modify
        checkpoint = None
    with pytest.raises((wiki.WikiBuildError, svc.APIError)):
        execute(env, jid, checkpoint)
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.ResourceVersion).where(m.ResourceVersion.origin == "AI_DRAFT")) == 0
    if mutation == "cancel_before":
        assert provider.calls == 0


@pytest.mark.parametrize("bad", ["missing_citation", "unsafe_markdown", "too_many_pages"])
def test_model_output_validation_never_writes_bad_draft(env, provider, bad):
    source = page(env, kind="document")
    def transform(output, data):
        if bad == "missing_citation":
            output["pages"][0]["blocks"][0]["evidence_ids"] = ["NOT_AUTHORIZED"]
        elif bad == "unsafe_markdown":
            output["pages"][0]["blocks"][0]["markdown"] = '<img src="https://example.invalid/tracking">'
        else:
            output["pages"].append({**copy.deepcopy(output["pages"][0]), "title": "第二页"})
        return output
    provider.output_transform = transform
    with pytest.raises(wiki.WikiBuildError):
        execute(env, build_job(env, [source], max_pages=1))
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.ResourceVersion).where(m.ResourceVersion.origin == "AI_DRAFT")) == 0


def test_no_model_no_fake_llm_success_and_unpublished_source_is_rejected(env, provider):
    draft = page(env, kind="document", state="DRAFT")
    body = {"space_id": env.space, "source_resource_ids": [draft[0]], "model_selection": {"connection_id": env.connection, "model_id": "test"}, "consent": True}
    assert env.call("POST", "/wiki/builds", body).status_code == 409
    source = page(env, kind="document")
    body["source_resource_ids"] = [source[0]]
    provider.available = False
    response = env.call("POST", "/wiki/builds", body)
    assert response.status_code == 503 and provider.calls == 0
    assert env.call("GET", f"/wiki/workspace?space_id={env.space}").status_code == 200


def test_source_limit_consent_and_coverage_budget_are_explicit(env, provider):
    source = page(env, kind="document", text="合成费用来源需要核对。" * 1000)
    body = {"space_id": env.space, "source_resource_ids": [uid() for _ in range(9)], "model_selection": {"connection_id": env.connection, "model_id": "test"}, "consent": True}
    assert env.call("POST", "/wiki/builds", body).status_code == 422
    body.update(source_resource_ids=[source[0]], consent=False)
    assert env.call("POST", "/wiki/builds", body).status_code == 422
    first = execute(env, build_job(env, [source]))
    assert first["coverage"]["truncated"] is True
    assert first["coverage"]["input_utf8_bytes"] <= wiki.MAX_INPUT_BYTES
    assert first["coverage"]["total_source_fragments"] > first["coverage"]["cited_fragments"]
    first_offset = provider.requests[0]["sources"][0]["char_start"]
    execute(env, build_job(env, [source]))
    assert provider.requests[1]["sources"][0]["char_start"] > first_offset


def test_wiki_evidence_prefers_published_knowledge_with_authorized_original(env):
    source = page(env, "费用原件", kind="document")
    knowledge = page(env, "费用知识", cites=[source])
    page(env, "仅原件命中", kind="document", text="费用原件其他内容。")
    with env.db() as db:
        records = wiki.choose_wiki_evidence(db, db.get(m.User, env.owner), env.space, "费用", limit=4)
    assert records[0]["resource_id"] == knowledge[0]
    assert {r["resource_id"] for r in records} == {source[0], knowledge[0]}


def test_category_and_build_replays_recheck_current_authority(env, provider):
    key = uid()
    body = {"space_id": env.space, "path": "长期空目录"}
    created = env.call("POST", "/wiki/categories", body, key=key)
    assert created.status_code == 201
    replay = env.call("POST", "/wiki/categories", body, key=key)
    assert replay.json() == created.json()
    with env.db.begin() as db:
        db.delete(db.get(m.SpaceMember, (env.space, env.owner, "admin")))
    assert env.call("POST", "/wiki/categories", body, key=key).status_code == 403
    source = page(env, kind="document")
    build_key = uid()
    job_id = build_job(env, [source], key=build_key)
    with env.db() as db:
        payload = db.get(m.Job, job_id).payload
    with env.db.begin() as db:
        db.get(m.Resource, source[0]).restricted = True
    request = {k: payload[k] for k in ("space_id", "source_resource_ids", "model_selection", "max_pages", "consent")}
    assert env.call("POST", "/wiki/builds", request, key=build_key).status_code == 404


def test_generated_bidirectional_links_are_visible_to_author_only_until_review(env, provider):
    original = page(env, kind="document")
    def output(raw, data):
        first = raw["pages"][0]
        first["title"], first["links"] = "费用基础", ["费用排查"]
        second = copy.deepcopy(first)
        second["title"], second["links"], second["aliases"] = "费用排查", ["费用基础"], []
        raw["pages"].append(second)
        return raw
    provider.output_transform = output
    built = execute(env, build_job(env, [original]))
    links = env.call("GET", f"/wiki/pages/{built['created_resource_ids'][0]}/links").json()
    assert {link["id"] for link in links["outgoing"]} == {built["created_resource_ids"][1]}
    assert {link["id"] for link in links["incoming"]} == {built["created_resource_ids"][1]}
    assert links["sources"][0]["id"] == original[0]
    env.login(env.reader)
    assert env.call("GET", f"/wiki/pages/{built['created_resource_ids'][0]}/links").status_code == 404


def test_native_provider_connection_contract_without_credentials_or_network(env, monkeypatch):
    from fund_kb import providers
    result = env.call("POST", "/model-connections", {"space_id": env.space, "name": "合成本地服务",
        "provider_id": "ollama", "base_url": "http://127.0.0.1:11434", "protocol": "ollama", "enabled": True,
        "credential_mode": "none", "allow_document_transfer": True,
        "custom_models": [{"id": "synthetic-test-model", "name": "合成模型", "brand": "test"}]})
    assert result.status_code == 201, result.text
    env.connection = result.json()["id"]
    source = page(env, kind="document")
    calls = []
    def complete(snapshot, messages, **kwargs):
        assert snapshot["api_key"] is None and snapshot["protocol"] == "ollama"
        calls.append(snapshot["id"])
        sources = json.loads(messages[1]["content"])["sources"]
        output = {"pages": [{"title": "原生适配契约页", "category": "估值", "knowledge_type": "faq", "aliases": [],
                             "links": [], "blocks": [{"markdown": "按来源核对费用。", "evidence_ids": [sources[0]["id"]]}]}], "gaps": []}
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}]}
    monkeypatch.setattr(providers, "complete", complete)
    built = execute(env, build_job(env, [source]))
    assert calls == [env.connection] and built["created_version_ids"]
    # A disabled real connection must fail clearly at queue time, not invent a successful build.
    with env.db.begin() as db:
        policy = db.get(m.RuntimePolicy, env.connection)
        policy.config = {**policy.config, "enabled": False}
    body = {"space_id": env.space, "source_resource_ids": [source[0]], "max_pages": 3,
            "model_selection": {"connection_id": env.connection, "model_id": "synthetic-test-model"}, "consent": True}
    denied = env.call("POST", "/wiki/builds", body)
    assert denied.status_code == 503 and denied.json()["code"] == "CONNECTION_DISABLED"


def test_job_result_guard_rechecks_source_access_after_generation(env, provider):
    source = page(env, kind="document")
    jid = build_job(env, [source])
    execute(env, jid)
    with env.db() as db:
        wiki.authorize_build_result(db, db.get(m.User, env.owner), db.get(m.Job, jid))
    with env.db.begin() as db:
        db.get(m.Resource, source[0]).restricted = True
    with env.db() as db, pytest.raises(svc.APIError):
        wiki.authorize_build_result(db, db.get(m.User, env.owner), db.get(m.Job, jid))


def test_graph_source_hash_and_scan_state_fail_closed(env):
    source = page(env, kind="document")
    linked = page(env, "派生页面", cites=[source])
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, source[1])
        db.get(m.Blob, version.source_blob_id).scan_state = "REJECTED"
    result = env.call("GET", f"/wiki/graph?space_id={env.space}").json()
    assert source[0] not in json.dumps(result) and linked[0] not in json.dumps(result)


def test_cross_space_original_and_backlink_follow_only_explicit_authorized_citations(env):
    source_space = env.space
    source = page(env, "跨空间原件", kind="document")
    other_space = uid()
    with env.db.begin() as db:
        db.add(m.Space(id=other_space, name="授权知识空间"))
        db.flush()
        for role in ("reader", "editor", "reviewer", "admin"):
            db.add(m.SpaceMember(space_id=other_space, user_id=env.owner, role=role))
    env.space = other_space
    knowledge = page(env, "跨空间Wiki", cites=[source])
    unrelated = page(env, "同空间无关页")
    result = env.call("GET", f"/wiki/pages/{knowledge[0]}/links").json()
    assert result["sources"][0]["id"] == source[0]
    incoming = env.call("GET", f"/wiki/pages/{source[0]}/links").json()
    assert knowledge[0] in {link["id"] for link in incoming["incoming"]}
    graph = env.call("GET", f"/wiki/graph?space_id={source_space}&focus_id={source[0]}").json()
    assert knowledge[0] in {node["id"] for node in graph["nodes"]}
    assert unrelated[0] not in json.dumps(graph)
    with env.db.begin() as db:
        db.delete(db.get(m.SpaceMember, (other_space, env.owner, "reader")))
        db.delete(db.get(m.SpaceMember, (other_space, env.owner, "editor")))
        db.delete(db.get(m.SpaceMember, (other_space, env.owner, "reviewer")))
        db.delete(db.get(m.SpaceMember, (other_space, env.owner, "admin")))
    assert knowledge[0] not in env.call("GET", f"/wiki/pages/{source[0]}/links").text
