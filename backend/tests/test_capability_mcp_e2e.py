"""Official SDK -> real stdio sidecar -> real uvicorn/create_app -> synthetic DB.

SDK code runs only in the sidecar venv subprocess. The application runs with
constructor-only settings, fresh SQLite and synthetic published fixtures. No
host credentials, model provider, real service or production DB is used.
"""
import json
import os
import socket
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import uvicorn
from sqlalchemy import select
from test_reference_review import make_version

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.api import create_app
from fund_kb.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "integrations" / "fundkb_mcp"


class SyntheticSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # No os.environ, .env, file secrets or host account discovery by Settings.
        return (init_settings,)


def publish_fixture(app, version_id, owner_id):
    """Seed an already-published synthetic fixture, not a real approval workflow."""
    with app.state.session_factory.begin() as db:
        version = db.get(m.ResourceVersion, version_id)
        version.content_sha256 = svc.check_frozen_hash(db, version)
        version.state = "APPROVED"
        release = m.Release(id=str(uuid4()), resource_id=version.resource_id, version_id=version.id,
            state="ACTIVE", publisher_id=owner_id, activated_at=svc.now(), manifest={"synthetic": True})
        db.add(release)
        db.flush()
        db.get(m.Resource, version.resource_id).active_release_id = release.id


def definition(source_id):
    return {"schema_version": 1, "name": "MCP组合贯通合成能力", "description": "测试实际接入链路",
        "triggers": ["合成协议验收"], "limitations": ["不执行真实金融任务"], "inputs": [],
        "steps": [{"id": "collect", "title": "读取并报告", "kind": "agent", "instructions": "读取绑定的合成依据并返回底稿",
            "depends_on": [], "required_tools": ["read_bound_sources"],
            "outputs": [{"key": "work", "label": "底稿", "type": "string", "required": True, "description": "合成结果"}],
            "checks": ["核对来源定位"], "risk": "read_only"},
            {"id": "review", "title": "人工核对", "kind": "human", "instructions": "仅人类会话确认",
                "depends_on": ["collect"], "required_tools": [], "outputs": [], "checks": ["核对底稿"], "risk": "read_only"}],
        "deliverables": ["合成底稿"], "source_version_ids": [source_id], "source_scope": "reference"}


def test_sdk_stdio_to_actual_app_guided_run_waits_for_human(tmp_path):
    sdk_python = SIDECAR / ".venv" / "bin" / "python"
    assert sdk_python.is_file(), "Install requirements-test.txt in the isolated sidecar venv; do not install SDK in backend"
    owner_id, space_id = str(uuid4()), str(uuid4())
    observations = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        settings = SyntheticSettings(app_env="development", auth_mode="demo", cookie_secure=False,
            database_url=f"sqlite:///{tmp_path / 'synthetic-e2e.sqlite3'}", storage_dir=tmp_path / "objects",
            qdrant_path=tmp_path / "vectors", allowed_origins=[origin], llm_provider="evidence",
            retrieval_mode="wiki", auto_create_schema=True)
        app = create_app(settings)
        synthetic_token = None

        async def observed_app(scope, receive, send):
            # Observe only transport facts, never retain the raw Authorization or
            # Cookie. All requests still execute the actual application unchanged.
            record = None
            if scope["type"] == "http":
                headers = dict(scope["headers"])
                if b"authorization" in headers:
                    record = {"method": scope["method"], "path": scope["path"],
                        "bearer_matches": headers[b"authorization"] == ("Bearer " + synthetic_token).encode(),
                        "has_cookie": b"cookie" in headers}
                    observations.append(record)

            async def observed_send(message):
                if record is not None and message["type"] == "http.response.start":
                    record["status"] = message["status"]
                await send(message)

            await app(scope, receive, observed_send)

        server = uvicorn.Server(uvicorn.Config(observed_app, host="127.0.0.1", port=port, lifespan="on",
            loop="asyncio", http="h11", log_config=None, log_level="critical", access_log=False,
            timeout_graceful_shutdown=5))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            assert server.started, "Synthetic loopback application did not start within 10 seconds"
            with app.state.session_factory.begin() as db:
                db.add(m.User(id=owner_id, external_subject="demo:synthetic-mcp-e2e", display_name="合成用户", active=True))
                db.add(m.Space(id=space_id, name="MCP临时合成库"))
                db.flush()
                for role in ("reader", "editor", "admin"):
                    db.add(m.SpaceMember(space_id=space_id, user_id=owner_id, role=role))
            source_id = make_version(SimpleNamespace(db=app.state.session_factory, space=space_id, owner=owner_id), "document")
            publish_fixture(app, source_id, owner_id)
            with app.state.session_factory() as db:
                block = db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == source_id)).one()
                expected_source = {"version_id": source_id, "block_id": block.block_id, "text": block.search_text,
                    "content_sha256": block.content_sha256, "locator": block.locator}
            with httpx.Client(base_url=origin + "/api/v1", trust_env=False, follow_redirects=False, timeout=10) as client:
                login = client.post("/auth/demo", json={"user_id": owner_id}, headers={"Origin": origin})
                assert login.status_code == 200, "Synthetic Cookie login failed"
                headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"], "Idempotency-Key": str(uuid4())}
                cap = client.post("/capabilities", json={"space_id": space_id, "definition": definition(source_id)}, headers=headers)
                assert cap.status_code == 201, cap.text
                cap = cap.json()
                publish_fixture(app, cap["version_id"], owner_id)
                created = client.post("/agent-access", json={"request_id": str(uuid4()), "name": "合成MCP贯通凭据",
                    "space_id": space_id, "scopes": ["capabilities:read", "runs:write", "sources:read"],
                    "expires_at": svc.primitive(svc.now() + timedelta(minutes=10))}, headers=headers)
                assert created.status_code == 201, "Synthetic credential creation failed"
                synthetic_token = created.json()["token"]
            assert observations == []
            child = subprocess.run([str(sdk_python), "-B", str(SIDECAR / "tests" / "sdk_application_client.py")],
                input=json.dumps({"space_id": space_id, "owner_id": owner_id, "capability_id": cap["resource_id"],
                    "source": expected_source}), text=True, capture_output=True, timeout=45, check=False,
                env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "FKB_AGENT_API_URL": origin + "/api/v1",
                    "FKB_AGENT_TOKEN": synthetic_token})
            assert synthetic_token not in child.stdout + child.stderr
            assert child.returncode == 0, {"sdk": child.stdout, "transport": observations}
            result = json.loads(child.stdout)
            assert result["ok"] and result["state"] == "WAITING_HUMAN" and result["revision"] == 2
            assert result["source_record_count"] == 1 and result["source_version_id"] == source_id
            assert result["human_review_tool_absent"] and result["human_review_call_rejected"]
            assert result["calls"] == ["list_capabilities", "get_capability", "start_workflow", "read_bound_sources",
                "get_next_steps", "report_step", "get_workflow"]
            assert len(observations) == 7 and all(row["bearer_matches"] and not row["has_cookie"] for row in observations)
            assert [row["status"] for row in observations] == [200, 200, 201, 200, 200, 200, 200]
            assert not any("/review" in row["path"] for row in observations)
            with app.state.session_factory() as db:
                stored = db.get(m.RuntimePolicy, result["run_id"])
                assert stored.config["state"] == "WAITING_HUMAN" and stored.config["owner_id"] == owner_id
                assert stored.config["steps"][0]["outputs"] == {"work": "合成底稿已回传，待人工核对"}
                assert stored.config["steps"][1]["reviewed_by"] is None
                assert db.scalars(select(m.Job)).all() == []
                audits = db.scalars(select(m.AuditEvent)).all()
                assert "capability_step.human_reviewed" not in {row.action for row in audits}
                receipts = db.scalars(select(m.IdempotencyRecord)).all()
                assert not any(row.route == "/api/v1/agent-access" for row in receipts)
                persisted = json.dumps([db.scalars(select(m.RuntimePolicy.config)).all(),
                    [row.details for row in audits], [row.response for row in receipts]])
                assert synthetic_token not in persisted
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=2)
            assert not thread.is_alive(), "Synthetic application did not stop"
