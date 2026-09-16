"""Subprocess-only official SDK client for the actual-app synthetic E2E test.

The backend test passes only its freshly created synthetic credential and a
loopback API URL. This helper never imports backend modules or reads local data.
"""
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = {"list_capabilities", "get_capability", "start_workflow", "get_next_steps", "report_step",
    "read_bound_sources", "get_workflow"}
phase = "initialize"


async def exercise(expected):
    global phase
    env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
        "FKB_AGENT_API_URL": os.environ["FKB_AGENT_API_URL"], "FKB_AGENT_TOKEN": os.environ["FKB_AGENT_TOKEN"]}
    params = StdioServerParameters(command=sys.executable,
        args=["-B", str(Path(__file__).resolve().parents[1] / "server.py")], env=env)
    calls = []
    async with stdio_client(params) as (read, write):  # noqa: SIM117 - explicit transport/session lifetime
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
            initialized = await session.initialize()
            assert initialized.protocolVersion == "2025-11-25"
            listing = await session.list_tools()
            assert {tool.name for tool in listing.tools} == TOOLS
            assert not any("review" in tool.name for tool in listing.tools)

            async def call(name, arguments):
                global phase
                phase = name
                result = await session.call_tool(name, arguments=arguments)
                assert not result.isError, "Application tool returned an error"
                assert json.loads(result.content[0].text) == result.structuredContent
                assert env["FKB_AGENT_TOKEN"] not in result.content[0].text
                calls.append(name)
                return result.structuredContent["data"]

            listed = await call("list_capabilities", {"space_id": expected["space_id"]})
            assert [item["resource_id"] for item in listed["items"]] == [expected["capability_id"]]
            cap = await call("get_capability", {"capability_id": expected["capability_id"]})
            assert cap["state"] == "APPROVED" and cap["permissions"]["can_run"]
            assert cap["definition"]["source_version_ids"] == [expected["source"]["version_id"]]
            created = await call("start_workflow", {"version_id": cap["version_id"], "inputs": {},
                "mode": "guided", "agent_label": "合成SDK贯通测试", "request_id": str(uuid4())})
            assert created["state"] == "WAITING_AGENT" and created["owner_id"] == expected["owner_id"]
            run_id = created["id"]
            sources = await call("read_bound_sources", {"run_id": run_id})
            assert len(sources["records"]) == 1
            source = sources["records"][0]
            for key in ("version_id", "block_id", "text", "content_sha256", "locator"):
                assert source[key] == expected["source"][key]
            next_steps = await call("get_next_steps", {"run_id": run_id})
            assert [step["id"] for step in next_steps["ready_steps"]] == ["collect"]
            reported = await call("report_step", {"run_id": run_id, "step_id": "collect",
                "revision": next_steps["revision"], "status": "reported", "outputs": {"work": "合成底稿已回传，待人工核对"},
                "note": "已核对本次合成来源定位；没有执行金融操作", "request_id": str(uuid4())})
            assert reported["state"] == "WAITING_HUMAN"
            final = await call("get_workflow", {"run_id": run_id})
            assert final["state"] == "WAITING_HUMAN" and final["revision"] == reported["revision"]
            assert final["steps"][0]["state"] == "REPORTED"
            assert final["steps"][0]["report_channel"] == "agent_token"
            assert final["steps"][1]["state"] == "PENDING" and final["steps"][1]["reviewed_by"] is None
            phase = "no_human_review_tool"
            rejected = await session.call_tool("human_review", arguments={"run_id": run_id})
            assert rejected.isError and env["FKB_AGENT_TOKEN"] not in rejected.content[0].text
            return {"ok": True, "protocol_version": initialized.protocolVersion, "tools": sorted(TOOLS), "calls": calls,
                "run_id": run_id, "state": final["state"], "revision": final["revision"],
                "source_record_count": len(sources["records"]), "source_version_id": source["version_id"],
                "human_review_tool_absent": True, "human_review_call_rejected": True}


def main():
    try:
        expected = json.load(sys.stdin)
        result = asyncio.run(exercise(expected))
    except Exception as exc:  # noqa: BLE001 - fixed diagnostic, never emit credentials/response bodies
        print(json.dumps({"ok": False, "phase": phase, "exception_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
