"""Real HTTP contract/CSRF/idempotency with synthetic documents, no network."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_api import EDITOR, READER, SPACE
from test_api import api as api  # noqa: PLC0414
from test_reference_review import make_version

from fund_kb import models as m


def setup(api):
    env = SimpleNamespace(db=api.app.state.session_factory, owner=EDITOR, space=SPACE)
    old, new, proof = [make_version(env, "document") for _ in range(3)]
    return {"space_id": SPACE, "predecessor_version_id": old, "successor_version_id": new,
        "evidence_version_id": proof, "effective_from": "2023-05-01", "scope": "full", "reason": "合成公告确认"}


def test_create_list_etag_revoke_and_idempotent_replay(api):
    data = setup(api); key = str(uuid4())
    response = api.call("POST", "/source-authority", data, key=key)
    assert response.status_code == 201, response.text
    record = response.json()
    replay = api.call("POST", "/source-authority", data, key=key)
    assert replay.status_code == 201 and replay.json() == record
    listed = api.call("GET", "/source-authority?space_id=" + SPACE)
    assert listed.status_code == 200 and listed.json()["items"] == [record]
    assert listed.json()["can_manage"] is True
    get = api.call("GET", "/source-authority/" + record["id"])
    assert get.status_code == 200 and get.headers["etag"] == '"1"'
    rejected = api.call("POST", "/source-authority/" + record["id"] + "/revoke", {"reason": "撤销合成记录"}, etag='"0"')
    assert rejected.status_code == 412
    revoked = api.call("POST", "/source-authority/" + record["id"] + "/revoke", {"reason": "撤销合成记录"}, etag=get.headers["etag"])
    assert revoked.status_code == 200 and revoked.json()["state"] == "REVOKED"
    stale = api.call("POST", "/source-authority", data, key=key)
    assert stale.status_code == 409


@pytest.mark.parametrize("change", ["date", "blank_reason", "scope", "uuid", "snapshots"])
def test_api_rejects_untrusted_fields_and_invalid_inputs(api, change):
    data = setup(api)
    if change == "date": data["effective_from"] = "2023-02-30"
    elif change == "blank_reason": data["reason"] = "  "
    elif change == "scope": data["scope"] = "all_effective"
    elif change == "uuid": data["successor_version_id"] = "forged"
    else: data["snapshots"] = {"forged": {"content_sha256": "a" * 64}}
    assert api.call("POST", "/source-authority", data).status_code == 422


def test_acl_revocation_invalidates_idempotent_receipt_and_hides_endpoints(api):
    data = setup(api); key = str(uuid4())
    response = api.call("POST", "/source-authority", data, key=key)
    assert response.status_code == 201
    with api.app.state.session_factory.begin() as db:
        proof = db.get(m.ResourceVersion, data["evidence_version_id"])
        db.get(m.Resource, proof.resource_id).restricted = True
    assert api.call("POST", "/source-authority", data, key=key).status_code == 404
    assert api.call("GET", "/source-authority/" + response.json()["id"]).status_code == 404
    assert api.call("GET", "/source-authority?space_id=" + SPACE).json()["items"] == []
    api.login(READER)
    assert api.call("POST", "/source-authority", data).status_code == 403
    assert api.call("GET", "/source-authority?space_id=" + SPACE).json()["can_manage"] is False


def test_state_changing_call_requires_csrf_and_no_get_writes(api):
    data = setup(api)
    assert api.call("POST", "/source-authority", data, headers={"X-CSRF-Token": ""}).status_code == 403
    assert api.call("GET", "/source-authority?space_id=" + SPACE).json()["items"] == []


def test_source_url_update_queues_projection_refresh_for_that_document_only(api, monkeypatch):
    from fund_kb import vector_indexing
    data = setup(api); vid = data["evidence_version_id"]
    queued = []
    monkeypatch.setattr(vector_indexing, "queue_resource_index", lambda ctx, rid: queued.append(rid))
    version = api.call("GET", f"/versions/{vid}")
    response = api.call("PATCH", f"/document-versions/{vid}/source-properties",
        {"source_url": "https://official.example/notice"}, etag=version.headers["etag"])
    assert response.status_code == 200, response.text
    assert queued == [version.json()["resource_id"]]


def test_prefill_route_is_read_only_and_not_misrouted_as_a_record_uuid(api):
    response = api.call("GET", "/source-authority/suggestions?space_id=" + SPACE)
    assert response.status_code == 200, response.text
    assert response.json()["can_manage"] is True and response.json()["items"] == []
    api.login(READER)
    response = api.call("GET", "/source-authority/suggestions?space_id=" + SPACE)
    assert response.status_code == 200 and response.json()["can_manage"] is False
