import copy
import json
from uuid import uuid4

import pytest
import yaml
from test_api import SPACE
from test_api import api as api  # noqa: PLC0414

from fund_kb.capability_schema import FORMAT, starter
from fund_kb.capability_skill import skill_package


def detail():
    return {"resource_id": str(uuid4()), "version_id": str(uuid4()), "space_id": str(uuid4()),
        "revision": 2, "version_no": 1, "state": "DRAFT", "manifest_sha256": "a" * 64,
        "content_sha256": "b" * 64, "definition": starter(), "source_bindings": []}


def test_export_preserves_full_definition_ids_and_hashes_without_mutating_input():
    data = detail()
    before = copy.deepcopy(data)
    package = skill_package(data)
    assert data == before
    assert set(package) == {"filename", "skill_markdown", "workflow_json", "connector_markdown"}
    assert package["filename"] == "SKILL.md"
    workflow = package["workflow_json"]
    assert workflow["format"] == FORMAT
    for key in before:
        assert workflow[key] == before[key]
    workflow["definition"]["inputs"].clear()
    assert data["definition"]["inputs"] == before["definition"]["inputs"]


@pytest.mark.parametrize("description", ['中文: "引号"\n下一行', 'FOF <场景> #估值', '---\nname: injected\n---'])
def test_frontmatter_has_safe_discriminating_metadata(description):
    data = detail()
    data["definition"]["description"] = description
    package = skill_package(data)
    metadata = yaml.safe_load(package["skill_markdown"].split("\n---\n", 1)[0].removeprefix("---\n"))
    assert metadata["name"] == "fundkb-" + data["resource_id"].replace("-", "")
    assert len(metadata["name"]) < 64
    assert isinstance(metadata["description"], str) and metadata["description"]
    assert "<" not in metadata["description"] and ">" not in metadata["description"]
    assert package["workflow_json"]["definition"]["description"] == description


def test_export_allowlist_never_copies_surrounding_credentials_or_permissions():
    data = detail()
    data.update(token="SYNTHETIC-SECRET", credentials={"key": "SYNTHETIC-SECRET"}, permissions={"can_run": False})
    package = skill_package(data)
    assert "SYNTHETIC-SECRET" not in json.dumps(package)
    assert "permissions" not in package["workflow_json"]
    for link in ("[workflow.json](workflow.json)", "[connector.md](connector.md)"):
        assert link in package["skill_markdown"]


def test_export_rejects_unsafe_identity_instead_of_generating_filesystem_path():
    data = detail()
    data["resource_id"] = "../../unrelated"
    with pytest.raises(ValueError):
        skill_package(data)


def test_api_contract_only_documents_bearer_on_allowed_operations():
    from fund_kb.agent_access import AGENT_OPERATIONS
    from fund_kb.api_capabilities import PATHS, SECURITY_SCHEMES
    assert SECURITY_SCHEMES["agentBearer"]["scheme"] == "bearer"
    operations = [spec for path in PATHS.values() for spec in path.values()]
    allowed = {spec["operationId"] for spec in operations if {"agentBearer": []} in spec["security"]}
    assert allowed == set(AGENT_OPERATIONS)
    for spec in operations:
        assert spec["security"][0].get("cookieAuth") == []
        if spec["operationId"] in allowed:
            assert spec["x-agent-scope"] == AGENT_OPERATIONS[spec["operationId"]]


def test_actual_http_export_preserves_authorized_draft_and_is_read_only(api):
    from sqlalchemy import func, select

    from fund_kb import models as m

    created = api.call("POST", "/capabilities", {"space_id": SPACE, "definition": starter()})
    assert created.status_code == 201, created.text
    value = created.json()
    def counts():
        with api.app.state.session_factory() as db:
            return [db.scalar(select(func.count()).select_from(model)) for model in
                (m.ResourceVersion, m.AuditEvent, m.RuntimePolicy, m.Job, m.IdempotencyRecord)]
    before = counts()
    for _ in range(2):
        exported = api.call("GET", "/capability-versions/" + value["version_id"] + "/skill")
        assert exported.status_code == 200, exported.text
        assert "no-store" in exported.headers["cache-control"]
        package = exported.json()
        assert package["workflow_json"]["definition"] == value["definition"]
        assert package["workflow_json"]["manifest_sha256"] == value["manifest_sha256"]
        assert package["workflow_json"]["state"] == "DRAFT"
    assert counts() == before
