"""Synthetic credentials/DB only. No production settings, DB, network or model."""
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from fund_kb import agent_access as access
from fund_kb import api_agent_access as api_access
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.auth import token_hash
from fund_kb.db import Base, build_engine, make_session_factory


@pytest.fixture
def env(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'synthetic-agent.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine.execution_options(sqlite_transaction_mode="IMMEDIATE"))
    ids = {key: str(uuid4()) for key in ("owner", "other", "personal", "other_personal", "team", "legacy")}
    with factory.begin() as db:
        for key in ("owner", "other"):
            db.add(m.User(id=ids[key], external_subject="synthetic:" + key, display_name=key, active=True))
        for key in ("personal", "other_personal", "team", "legacy"):
            db.add(m.Space(id=ids[key], name="合成库" + key))
        db.flush()
        for key, owner_key in (("personal", "owner"), ("other_personal", "other"), ("team", "owner")):
            db.add(m.RuntimePolicy(name="space-governance:" + ids[key], updated_by=ids[owner_key],
                config={"schema_version": 1, "space_id": ids[key], "kind": "team" if key == "team" else "personal",
                    "owner_id": ids[owner_key]}))
        # Even an explicit foreign admin cannot enter somebody else's personal library.
        db.add(m.SpaceMember(space_id=ids["personal"], user_id=ids["other"], role="admin"))
        db.add(m.SpaceMember(space_id=ids["legacy"], user_id=ids["owner"], role="reader"))
        for key in ("owner", "other"):
            db.add(m.LoginSession(user_id=ids[key], token_hash=token_hash("synthetic-cookie-" + key),
                csrf_token="synthetic-csrf", expires_at=svc.now() + timedelta(hours=1)))
    yield SimpleNamespace(db=factory, engine=engine, **ids)
    engine.dispose()


def request(*, cookie="owner", authorization=None, method="GET", path="/agent-access", headers=()):
    values = [(b"host", b"testserver"), (b"cookie", ("kb_session=synthetic-cookie-" + cookie).encode())]
    if authorization is not None:
        values.append((b"authorization", authorization.encode()))
    values += [(key.lower().encode(), value.encode()) for key, value in headers]
    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace()))
    req = Request({"type": "http", "method": method, "scheme": "http", "server": ("testserver", 80),
        "path": "/api/v1" + path, "raw_path": ("/api/v1" + path).encode(), "query_string": b"",
        "headers": values, "app": app, "path_params": {}})
    req.state.trace_id = str(uuid4())
    return req


def context(env, db, *, who="owner", data=None, space="personal", operation="createAgentAccess", req=None):
    req = req or request(cookie=who, method="POST" if operation != "listAgentAccess" else "GET")
    return svc.Context(req, db, db.get(m.User, getattr(env, who)), data or {},
        {"space_id": getattr(env, space)}, operation)


def create_input(env, *, space="personal", scopes=None):
    return {"request_id": str(uuid4()), "name": "合成离线Agent", "space_id": getattr(env, space),
        "scopes": sorted(access.SCOPES) if scopes is None else scopes,
        "expires_at": svc.primitive(svc.now() + timedelta(days=1))}


def issue(env, *, space="personal", scopes=None, who="owner"):
    with env.db.begin() as db:
        return access.create_access(context(env, db, who=who, data=create_input(env, space=space, scopes=scopes)))


def authenticate(env, token, operation="getCapability", *, db=None, method=None):
    req = request(authorization="Bearer " + token, method=method or (
        "POST" if operation in access.WRITE_OPERATIONS else "GET"))
    if db is not None:
        return access.authenticate_agent(req, db, operation), req
    with env.db() as session:
        return access.authenticate_agent(req, session, operation), req


def test_one_time_random_secret_hash_only_at_rest_and_owner_metadata(env):
    created = issue(env)
    token = created["token"]
    assert len(base64.urlsafe_b64decode(token.split(".")[1] + "=")) == 32
    assert token != issue(env)["token"]
    assert created["access"]["revision"] == 1 and created["access"]["revoked_at"] is None
    with env.db() as db:
        configs = db.scalars(select(m.RuntimePolicy.config)).all()
        audits = db.scalars(select(m.AuditEvent.details)).all()
        receipts = db.scalars(select(m.IdempotencyRecord.response)).all()
        persisted = json.dumps([configs, audits, receipts])
        assert token not in persisted and token.split(".")[1] not in persisted
        assert token_hash(token) in persisted
        result = access.list_access(context(env, db, operation="listAgentAccess"))
        assert result["can_create"] and result["base_url"] == "http://testserver/api/v1"
        assert set(result["items"][0]) == {"id", "name", "space_id", "scopes", "expires_at", "revoked_at", "created_at", "revision"}
        assert not db.new and not db.dirty and not db.deleted
    user, req = authenticate(env, token)
    assert user.id == env.owner
    assert req.state.agent_access == created["access"]
    assert "token" not in json.dumps(req.state.agent_access)


def test_duplicate_request_cannot_replay_secret_or_create_again_across_spaces(env):
    data = create_input(env)
    with env.db.begin() as db:
        created = access.create_access(context(env, db, data=data))
    for changed in (data, {**data, "name": "different"}, {**data, "space_id": env.team}):
        with env.db.begin() as db, pytest.raises(svc.APIError) as err:
            access.create_access(context(env, db, data=changed))
        assert err.value.status == 409 and err.value.details == {"access_id": created["access"]["id"]}
        assert created["token"] not in str(err.value)
    with env.db.begin() as db:
        # A request UUID is scoped to a user, so another user cannot squat on it.
        second = access.create_access(context(env, db, who="other", data={**data, "space_id": env.team}))
        assert second["access"]["id"] != created["access"]["id"]


def test_database_unique_constraint_and_concurrent_creation(env):
    data = create_input(env)
    def create():
        try:
            with env.db.begin() as db:
                return access.create_access(context(env, db, data=data))
        except svc.APIError as exc:
            return exc.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sum(isinstance(item, dict) for item in results) == 1 and results.count(409) == 1
    with env.db() as db:
        row = db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.startswith(access.POLICY_PREFIX))).one()
        duplicate = m.RuntimePolicy(name=row.name, config=row.config, updated_by=row.updated_by)
    with env.db.begin() as db, pytest.raises(IntegrityError):
        db.add(duplicate)
        db.flush()


@pytest.mark.parametrize("scope", sorted(access.SCOPES))
@pytest.mark.parametrize("operation", sorted(access.AGENT_OPERATIONS))
def test_scope_operation_matrix(env, scope, operation):
    token = issue(env, scopes=[scope])["token"]
    if access.AGENT_OPERATIONS[operation] == scope:
        user, req = authenticate(env, token, operation)
        assert user.id == env.owner
        access.authorize_agent_space(req, env.personal)
    else:
        with pytest.raises(svc.APIError) as err:
            authenticate(env, token, operation)
        assert err.value.status == 403 and err.value.code == "AGENT_SCOPE_REQUIRED"


@pytest.mark.parametrize("operation", ["reviewCapabilityRun", "createCapability", "updateCapability",
    "getCapabilitySkill", "exportCapabilitySkill", "publishVersion", "createReview", "deleteResource",
    "setModelPolicy", "getMe", "readVersionContent", "getHealth", "listAgentAccess", "createAgentAccess",
    "revokeAgentAccess", "unknownOperation"])
def test_valid_bearer_plus_valid_cookie_cannot_enter_other_operations(env, operation):
    token = issue(env)["token"]
    with pytest.raises(svc.APIError) as err:
        authenticate(env, token, operation)
    assert err.value.status == 403 and err.value.code == "AGENT_OPERATION_FORBIDDEN"


@pytest.mark.parametrize("authorization", ["", "Basic abc", "Bearer", "Bearer malformed", "Bearer  x",
    "Bearer fkb_agent_" + "0" * 32 + "." + "a" * 43 + " extra"])
def test_authorization_presence_never_falls_back_to_cookie(env, authorization):
    req = request(authorization=authorization)
    with env.db() as db, pytest.raises(svc.APIError) as err:
        access.authenticate_agent(req, db, "getCapability")
    assert err.value.status == 401 and req.state.agent_access is None


def test_absence_duplicate_headers_forged_secret_unknown_selector_and_wrong_method(env):
    with env.db() as db:
        req = request()
        assert access.authenticate_agent(req, db, "getMe") is None
        access.authorize_agent_space(req, env.other_personal)
    token = issue(env)["token"]
    duplicate = request(authorization="Bearer " + token, headers=[("Authorization", "Bearer " + token)])
    with env.db() as db, pytest.raises(svc.APIError):
        access.authenticate_agent(duplicate, db, "getCapability")
    for invalid in (token.rsplit(".", 1)[0] + "." + "Z" * 43, "fkb_agent_" + "0" * 32 + "." + "a" * 43):
        with pytest.raises(svc.APIError) as err:
            authenticate(env, invalid)
        assert err.value.status == 401
    with pytest.raises(svc.APIError) as err:
        authenticate(env, token, method="POST")
    assert err.value.status == 403
    with pytest.raises(svc.APIError):
        access.authorize_agent_space(request(authorization="Bearer " + token), env.personal)


def test_personal_owner_boundary_and_token_space_boundary(env):
    with env.db.begin() as db, pytest.raises(svc.APIError) as err:
        access.create_access(context(env, db, who="other", data=create_input(env)))
    assert err.value.status == 404
    _, req = authenticate(env, issue(env)["token"])
    for space_id in (env.other_personal, env.team, env.legacy):
        with pytest.raises(svc.APIError) as err:
            access.authorize_agent_space(req, space_id)
        assert err.value.status == 404
    with env.db() as db, pytest.raises(svc.APIError) as err:
        access.list_access(context(env, db, who="other", operation="listAgentAccess"))
    assert err.value.status == 404


@pytest.mark.parametrize("change", ["user_disabled", "expired", "revoked", "scope_removed", "personal_owner",
    "invalid_governance", "space_deleted", "legacy_removed"])
def test_next_call_observes_current_state_not_stale_identity_map(env, change):
    created = issue(env, space="legacy" if change == "legacy_removed" else "personal")
    token = created["token"]
    with env.db() as existing:
        authenticate(env, token, db=existing)
        existing.get(m.RuntimePolicy, created["access"]["id"])
        existing.get(m.User, env.owner)
        existing.commit()  # A new HTTP transaction, with deliberately retained ORM objects.
        with env.db.begin() as db:
            row = db.get(m.RuntimePolicy, created["access"]["id"])
            if change == "user_disabled": db.get(m.User, env.owner).active = False
            elif change == "expired":
                row.config = {**row.config, "created_at": svc.primitive(svc.now() - timedelta(days=2)),
                    "expires_at": svc.primitive(svc.now() - timedelta(days=1))}
            elif change == "revoked": row.config = {**row.config, "revoked_at": svc.primitive(svc.now())}
            elif change == "scope_removed": row.config = {**row.config, "scopes": ["sources:read"]}
            elif change in {"personal_owner", "invalid_governance"}:
                governance = db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "space-governance:" + env.personal)).one()
                governance.config = {**governance.config, **({"owner_id": env.other} if change == "personal_owner" else {"kind": "invalid"})}
            elif change == "space_deleted":
                db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == env.personal))
                db.delete(db.get(m.Space, env.personal))
            elif change == "legacy_removed":
                db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == env.legacy))
        with pytest.raises(svc.APIError):
            authenticate(env, token, db=existing)


def test_team_current_public_read_is_not_a_frozen_editor_grant(env):
    created = issue(env, who="other", space="team", scopes=["capabilities:read"])
    with env.db.begin() as db:
        db.add(m.SpaceMember(space_id=env.team, user_id=env.other, role="editor"))
    authenticate(env, created["token"])
    with env.db.begin() as db:
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == env.team, m.SpaceMember.user_id == env.other))
    # Governed teams remain readable to an active user; no editor power is delegated.
    authenticate(env, created["token"])
    with pytest.raises(svc.APIError):
        authenticate(env, created["token"], "createCapabilityRun")


@pytest.mark.parametrize("change", ["extra", "schema_version", "owner_id", "scopes", "duplicate_scopes", "empty_scopes",
    "expires_at", "created_at", "revoked_at", "token_sha256", "request_id", "updated_by"])
def test_corrupt_record_fails_closed(env, change):
    created = issue(env)
    with env.db.begin() as db:
        row = db.get(m.RuntimePolicy, created["access"]["id"])
        values = {"extra": {"unexpected": True}, "schema_version": {"schema_version": True},
            "owner_id": {"owner_id": "bad"}, "scopes": {"scopes": ["*"]}, "duplicate_scopes": {"scopes": ["runs:write"] * 2},
            "empty_scopes": {"scopes": []}, "expires_at": {"expires_at": "2099-01-01T00:00:00"},
            "created_at": {"created_at": "invalid"}, "revoked_at": {"revoked_at": "invalid"},
            "token_sha256": {"token_sha256": "invalid"}, "request_id": {"request_id": str(uuid4())}}
        if change == "updated_by": row.updated_by = env.other
        else:
            # Python considers True == 1; force a SQL update so this corruption
            # actually reaches JSON storage instead of being ignored by ORM diffing.
            db.execute(update(m.RuntimePolicy).where(m.RuntimePolicy.id == row.id)
                .values(config={**row.config, **values[change]}))
    with pytest.raises(svc.APIError) as err:
        authenticate(env, created["token"])
    assert err.value.status == 401


@pytest.mark.parametrize("change", [{"name": " "}, {"name": "x" * 201}, {"request_id": "bad"},
    {"space_id": "bad"}, {"scopes": []}, {"scopes": ["runs:write", "runs:write"]}, {"scopes": ["admin"]},
    {"scopes": [["runs:write"]]}, {"expires_at": "2099-01-01T00:00:00"}, {"expires_at": "2000-01-01T00:00:00Z"},
    {"expires_at": "2099-02-30T00:00:00Z"}, {"token": "forged"}, {"owner_id": "forged"}])
def test_invalid_create_input_rejected_without_rows(env, change):
    with env.db.begin() as db, pytest.raises(svc.APIError) as err:
        access.create_access(context(env, db, data={**create_input(env), **change}))
    assert err.value.status == 422
    with env.db() as db:
        assert not db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.startswith(access.POLICY_PREFIX))).all()


def test_owner_revoke_revision_receipts_no_bearer_management_and_lost_acl_cleanup(env):
    created = issue(env, space="legacy")
    access_id = created["access"]["id"]
    with env.db.begin() as db:
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == env.legacy))
    with env.db() as db:
        listed = access.list_access(context(env, db, operation="listAgentAccess", space="legacy"))
        assert listed["items"] == [created["access"]] and not listed["can_create"]
    def revoke_context(db, *, etag='"1"', who="owner", bearer=False):
        req = request(cookie=who, method="POST", path=f"/agent-access/{access_id}/revoke",
            authorization="Bearer " + created["token"] if bearer else None, headers=[("If-Match", etag)])
        req.scope["path_params"] = {"id": access_id}
        return context(env, db, who=who, data={"reason": "合成撤销"}, operation="revokeAgentAccess", req=req)
    for kwargs, status in (({"etag": '"0"'}, 412), ({"who": "other"}, 404), ({"bearer": True}, 403)):
        with env.db.begin() as db, pytest.raises(svc.APIError) as err:
            access.revoke_access(revoke_context(db, **kwargs))
        assert err.value.status == status
    with env.db.begin() as db:
        result = api_access.revoke_record(revoke_context(db))
        assert result.headers["ETag"] == '"2"'
        assert result.body["revoked_at"] is not None
        cached = {"body": result.body, "headers": result.headers}
    with env.db() as db:
        assert api_access.replay_authority(revoke_context(db), cached)
        with pytest.raises(svc.APIError):
            api_access.replay_authority(revoke_context(db, who="other"), cached)
        with pytest.raises(svc.APIError):
            api_access.replay_authority(revoke_context(db, bearer=True), cached)
        with pytest.raises(svc.APIError):
            api_access.replay_authority(context(env, db), {"body": created})
    with pytest.raises(svc.APIError):
        authenticate(env, created["token"])
    with env.db.begin() as db:
        row = db.get(m.RuntimePolicy, access_id)
        row.config = {**row.config, "name": "状态已变更"}
    with env.db() as db, pytest.raises(svc.APIError) as err:
        api_access.replay_authority(revoke_context(db), cached)
    assert err.value.status == 409


def test_create_handler_fails_closed_without_write_exception(env, monkeypatch):
    from fund_kb import api
    monkeypatch.setattr(api, "WRITE_EXCEPTIONS", set(api.WRITE_EXCEPTIONS) - {"createAgentAccess"})
    with env.db.begin() as db, pytest.raises(svc.APIError) as err:
        api_access.create_record(context(env, db, data=create_input(env)))
    assert err.value.code == "AGENT_ACCESS_UNSAFE_INTEGRATION"


def test_create_handler_refuses_an_already_started_generic_receipt(env, monkeypatch):
    from fund_kb import api
    monkeypatch.setattr(api, "WRITE_EXCEPTIONS", set(api.WRITE_EXCEPTIONS) | {"createAgentAccess"})
    key = str(uuid4())
    with env.db.begin() as db:
        db.add(m.IdempotencyRecord(actor_id=env.owner, http_method="POST", route="/api/v1/agent-access",
            key=key, request_sha256="a" * 64, state="STARTED", expires_at=svc.now() + timedelta(days=1)))
    with env.db.begin() as db, pytest.raises(svc.APIError) as err:
        req = request(method="POST", headers=[("Idempotency-Key", key)])
        api_access.create_record(context(env, db, data=create_input(env), req=req))
    assert err.value.code == "AGENT_ACCESS_UNSAFE_INTEGRATION"
    with env.db() as db:
        assert not db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.startswith(access.POLICY_PREFIX))).all()


def test_openapi_contract_has_cookie_security_no_generic_create_receipt_and_closed_shapes():
    from jsonschema import Draft202012Validator
    for schema in api_access.SCHEMAS.values():
        Draft202012Validator.check_schema(schema)
    create = api_access.PATHS["/agent-access"]["post"]
    assert create["security"] == [{"cookieAuth": [], "csrf": []}]
    assert create["parameters"] == []
    revoke = api_access.PATHS["/agent-access/{id}/revoke"]["post"]
    assert {"$ref": "#/components/parameters/Idempotency"} in revoke["parameters"]
    assert all(schema["additionalProperties"] is False for schema in api_access.SCHEMAS.values())
