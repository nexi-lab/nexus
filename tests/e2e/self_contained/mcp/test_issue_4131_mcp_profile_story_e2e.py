"""Real MCP profile story E2E and timings for issue #4131."""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from sqlalchemy import create_engine

from nexus.bricks.mcp.middleware import ToolNamespaceMiddleware
from nexus.bricks.mcp.profiles import grant_tools_for_profile, load_profiles
from nexus.bricks.mcp.server import create_mcp_server, reset_request_api_key, set_request_api_key
from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.consistency.metastore_version_store import MetastoreVersionStore
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.bricks.workflows.actions import BaseAction
from nexus.bricks.workflows.types import (
    ActionResult,
    WorkflowAction,
    WorkflowContext,
    WorkflowDefinition,
)
from nexus.contracts.types import OperationContext
from nexus.storage.models import Base
from tests.testkit.metadata import InMemoryNexusFS

pytestmark = pytest.mark.e2e


EXPECTED_PROFILE_TOOLS: dict[str, set[str]] = {
    "minimal": {
        "nexus_read_file",
        "nexus_list_files",
        "nexus_file_info",
        "nexus_glob",
    },
    "coding": {
        "nexus_read_file",
        "nexus_list_files",
        "nexus_file_info",
        "nexus_glob",
        "nexus_write_file",
        "nexus_edit_file",
        "nexus_delete_file",
        "nexus_mkdir",
        "nexus_rmdir",
        "nexus_rename_file",
        "nexus_grep",
    },
    "search": {
        "nexus_read_file",
        "nexus_list_files",
        "nexus_file_info",
        "nexus_glob",
        "nexus_grep",
        "nexus_semantic_search",
    },
    "execution": {
        "nexus_read_file",
        "nexus_list_files",
        "nexus_file_info",
        "nexus_glob",
        "nexus_write_file",
        "nexus_edit_file",
        "nexus_delete_file",
        "nexus_mkdir",
        "nexus_rmdir",
        "nexus_rename_file",
        "nexus_grep",
        "nexus_python",
        "nexus_bash",
        "nexus_sandbox_create",
        "nexus_sandbox_list",
        "nexus_sandbox_stop",
    },
    "full": {
        "nexus_read_file",
        "nexus_list_files",
        "nexus_file_info",
        "nexus_glob",
        "nexus_write_file",
        "nexus_edit_file",
        "nexus_delete_file",
        "nexus_mkdir",
        "nexus_rmdir",
        "nexus_rename_file",
        "nexus_grep",
        "nexus_python",
        "nexus_bash",
        "nexus_sandbox_create",
        "nexus_sandbox_list",
        "nexus_sandbox_stop",
        "nexus_semantic_search",
        "nexus_list_workflows",
        "nexus_execute_workflow",
        "nexus_discovery_search_tools",
        "nexus_discovery_list_servers",
        "nexus_discovery_get_tool_details",
        "nexus_discovery_load_tools",
        "nexus_hub_admin",
    },
}

OPTIONAL_SANDBOX_TOOLS = {
    "nexus_python",
    "nexus_bash",
    "nexus_sandbox_create",
    "nexus_sandbox_list",
    "nexus_sandbox_stop",
}


class _NoopWorkflowAction(BaseAction):
    async def execute(self, context: WorkflowContext) -> ActionResult:
        return ActionResult(
            action_name=self.name,
            success=True,
            output={"trigger": context.trigger_context},
        )


DISCOVERY_INDEXED_TOOLS = {
    "nexus_read_file",
    "nexus_write_file",
    "nexus_edit_file",
    "nexus_list_files",
    "nexus_delete_file",
    "nexus_mkdir",
    "nexus_rmdir",
    "nexus_rename_file",
    "nexus_file_info",
    "nexus_glob",
    "nexus_grep",
    "nexus_semantic_search",
    "nexus_list_workflows",
    "nexus_execute_workflow",
}


@dataclass
class PerfRecord:
    api: str
    elapsed_ms: float
    status: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _resolve_cluster_binary() -> Path | None:
    root = _repo_root()
    for directory in (
        root / "target" / "debug",
        root / "target" / "release",
        root / "rust" / "target" / "debug",
        root / "rust" / "target" / "release",
    ):
        for name in ("nexusd-cluster", "nexus-cluster"):
            candidate = directory / name
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _text(result: Any) -> str:
    return "\n".join(getattr(item, "text", str(item)) for item in result.content)


def _decode_json_result(result: Any) -> Any:
    return json.loads(_text(result))


async def _timed_list(client: Client, api: str, records: list[PerfRecord]) -> list[str]:
    start = time.perf_counter()
    tools = await client.list_tools()
    elapsed_ms = (time.perf_counter() - start) * 1000
    names = sorted(tool.name for tool in tools)
    records.append(PerfRecord(api=api, elapsed_ms=elapsed_ms, status=f"{len(names)} tools"))
    return names


async def _timed_call(
    client: Client,
    api: str,
    arguments: dict[str, Any],
    records: list[PerfRecord],
    *,
    record_api: str | None = None,
) -> Any:
    start = time.perf_counter()
    result = await client.call_tool(api, arguments, raise_on_error=False)
    elapsed_ms = (time.perf_counter() - start) * 1000
    text = _text(result)
    status = "ok"
    if text.startswith("Error:"):
        status = "expected_error"
    else:
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(text)
            if isinstance(payload, dict) and payload.get("error"):
                status = "expected_error"
    records.append(PerfRecord(api=record_api or api, elapsed_ms=elapsed_ms, status=status))
    return result


def _print_perf(records: list[PerfRecord]) -> None:
    print("\nISSUE_4131_MCP_E2E_PERF")
    for record in records:
        print(f"{record.api}: {record.elapsed_ms:.2f} ms ({record.status})")


@pytest.mark.asyncio
async def test_issue_4131_mcp_profiles_real_protocol_and_perf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _resolve_cluster_binary()
    if cluster is None:
        pytest.skip(
            "issue #4131 real MCP E2E requires nexusd-cluster/nexus-cluster; "
            "build it with `cargo build -p nexus-cluster`"
        )

    monkeypatch.setenv("NEXUS_KERNEL_BINARY", str(cluster))
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    monkeypatch.setenv("NEXUS_ENABLE_WRITE_BUFFER", "false")
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "0")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setenv("NEXUS_TXTAI_USE_API_EMBEDDINGS", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    import nexus

    nx = nexus.connect(
        config={
            "data_dir": str(tmp_path / "data"),
            "profile": "full",
            "database_url": f"sqlite:///{tmp_path / 'records.db'}",
            "enforce_permissions": False,
        }
    )
    nx._init_cred = OperationContext(user_id="admin", groups=[], is_admin=True)
    workflow_engine = nx.service("workflow_engine")
    assert workflow_engine is not None
    workflow_engine.action_registry["noop"] = _NoopWorkflowAction
    workflow_engine.load_workflow(
        WorkflowDefinition(
            name="issue4131_noop",
            version="1.0",
            actions=[WorkflowAction(name="noop", type="noop")],
        ),
        enabled=True,
    )
    sandbox_rpc = nx.service("sandbox_rpc")
    sandbox_providers = (
        sandbox_rpc.available_providers()
        if sandbox_rpc is not None and hasattr(sandbox_rpc, "available_providers")
        else []
    )
    records: list[PerfRecord] = []

    try:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        rebac = EnhancedReBACManager(
            engine=engine,
            cache_ttl_seconds=1,
            version_store=MetastoreVersionStore(InMemoryNexusFS()),
            namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
        )
        middleware = ToolNamespaceMiddleware(
            rebac_manager=rebac,
            zone_id=None,
            cache_ttl=60,
            revision_window=1,
        )
        server = await create_mcp_server(nx=nx, tool_namespace_middleware=middleware)
        profiles = load_profiles(_repo_root() / "src" / "nexus" / "config" / "tool_profiles.yaml")

        async with Client(server, timeout=30) as unfiltered:
            registered = set(await _timed_list(unfiltered, "tools/list unfiltered", records))

        for profile_name, expected_tools in EXPECTED_PROFILE_TOOLS.items():
            subject_id = f"issue4131-{profile_name}"
            grant_tools_for_profile(
                rebac_manager=rebac,
                subject=("api_key", subject_id),
                profile=profiles.get_profile(profile_name),
            )
            middleware.invalidate()
            token = set_request_api_key(subject_id)
            try:
                async with Client(server, timeout=30) as client:
                    names = set(await _timed_list(client, f"tools/list {profile_name}", records))
            finally:
                reset_request_api_key(token)

            expected_visible = expected_tools & registered
            missing_optional = expected_tools - registered
            assert missing_optional <= OPTIONAL_SANDBOX_TOOLS
            assert names == expected_visible

        full_subject = "issue4131-full-calls"
        grant_tools_for_profile(
            rebac_manager=rebac,
            subject=("api_key", full_subject),
            profile=profiles.get_profile("full"),
        )
        middleware.invalidate()
        token = set_request_api_key(full_subject)
        try:
            async with Client(server, timeout=30) as client:
                await _timed_call(client, "nexus_mkdir", {"path": "/issue4131"}, records)
                await _timed_call(
                    client,
                    "nexus_write_file",
                    {"path": "/issue4131/api.txt", "content": "needle before\n"},
                    records,
                )

                read_result = await _timed_call(
                    client, "nexus_read_file", {"path": "/issue4131/api.txt"}, records
                )
                assert "needle before" in _text(read_result)

                info = _decode_json_result(
                    await _timed_call(
                        client, "nexus_file_info", {"path": "/issue4131/api.txt"}, records
                    )
                )
                assert info["exists"] is True
                assert info["is_directory"] is False

                listed = _decode_json_result(
                    await _timed_call(client, "nexus_list_files", {"path": "/issue4131"}, records)
                )
                assert listed["count"] >= 1

                globbed = _decode_json_result(
                    await _timed_call(
                        client,
                        "nexus_glob",
                        {
                            "pattern": "*.txt",
                            "path": "/issue4131",
                            "files": ["/issue4131/api.txt"],
                        },
                        records,
                    )
                )
                assert "/issue4131/api.txt" in globbed["items"]

                grep = _decode_json_result(
                    await _timed_call(
                        client,
                        "nexus_grep",
                        {
                            "pattern": "needle",
                            "path": "/issue4131",
                            "files": ["/issue4131/api.txt"],
                        },
                        records,
                    )
                )
                assert grep["count"] >= 1

                semantic = _text(
                    await _timed_call(
                        client,
                        "nexus_semantic_search",
                        {"query": "needle", "path": "/issue4131", "search_mode": "keyword"},
                        records,
                    )
                )
                assert semantic.startswith("{") or "Semantic search not available" in semantic

                edit = _decode_json_result(
                    await _timed_call(
                        client,
                        "nexus_edit_file",
                        {
                            "path": "/issue4131/api.txt",
                            "edits": [{"old_str": "before", "new_str": "after"}],
                        },
                        records,
                    )
                )
                assert edit["success"] is True

                await _timed_call(
                    client,
                    "nexus_rename_file",
                    {
                        "old_path": "/issue4131/api.txt",
                        "new_path": "/issue4131/api-renamed.txt",
                    },
                    records,
                )
                renamed = await _timed_call(
                    client, "nexus_read_file", {"path": "/issue4131/api-renamed.txt"}, records
                )
                assert "needle after" in _text(renamed)

                medium_content = "".join(f"line {i}: value\n" for i in range(2000))
                await _timed_call(
                    client,
                    "nexus_write_file",
                    {"path": "/issue4131/medium.txt", "content": medium_content},
                    records,
                    record_api="nexus_write_file medium_2k",
                )
                medium_edit = _decode_json_result(
                    await _timed_call(
                        client,
                        "nexus_edit_file",
                        {
                            "path": "/issue4131/medium.txt",
                            "edits": [
                                {
                                    "old_str": "line 1500: value",
                                    "new_str": "line 1500: changed",
                                }
                            ],
                        },
                        records,
                        record_api="nexus_edit_file medium_2k_exact",
                    )
                )
                assert medium_edit["success"] is True
                assert records[-1].elapsed_ms < 1500

                search_tools = _decode_json_result(
                    await _timed_call(
                        client,
                        "nexus_discovery_search_tools",
                        {"query": "read file", "top_k": 5},
                        records,
                    )
                )
                assert any(tool["name"] == "nexus_read_file" for tool in search_tools["tools"])

                servers = _decode_json_result(
                    await _timed_call(client, "nexus_discovery_list_servers", {}, records)
                )
                assert servers["total_tools"] == len(
                    EXPECTED_PROFILE_TOOLS["full"] & registered & DISCOVERY_INDEXED_TOOLS
                )

                details = _decode_json_result(
                    await _timed_call(
                        client,
                        "nexus_discovery_get_tool_details",
                        {"tool_name": "nexus_read_file"},
                        records,
                    )
                )
                assert details["found"] is True

                loaded = _decode_json_result(
                    await _timed_call(
                        client,
                        "nexus_discovery_load_tools",
                        {"tool_names": ["nexus_read_file", "nexus_write_file"]},
                        records,
                    )
                )
                assert (
                    "nexus_read_file" in loaded["loaded"]
                    or "nexus_read_file" in loaded["already_loaded"]
                )

                workflows = _decode_json_result(
                    await _timed_call(client, "nexus_list_workflows", {}, records)
                )
                assert any(item["name"] == "issue4131_noop" for item in workflows)

                workflow_exec = _decode_json_result(
                    await _timed_call(
                        client,
                        "nexus_execute_workflow",
                        {"name": "issue4131_noop", "inputs": '{"source": "mcp-e2e"}'},
                        records,
                    )
                )
                assert workflow_exec["workflow_name"] == "issue4131_noop"
                assert workflow_exec["status"] == "succeeded"

                hub_admin = _decode_json_result(
                    await _timed_call(
                        client,
                        "nexus_hub_admin",
                        {"action": "status", "arguments": {}},
                        records,
                    )
                )
                assert hub_admin["postgres"] == "ok"

                if registered >= OPTIONAL_SANDBOX_TOOLS:
                    create_args: dict[str, Any] = {"name": "issue4131-sandbox", "ttl_minutes": 1}
                    docker_template = os.getenv("NEXUS_ISSUE4131_DOCKER_TEMPLATE")
                    if "docker" in sandbox_providers and docker_template:
                        create_args["provider"] = "docker"
                        create_args["template_id"] = docker_template
                    elif "monty" in sandbox_providers:
                        create_args["provider"] = "monty"
                    sandbox_create = await _timed_call(
                        client,
                        "nexus_sandbox_create",
                        create_args,
                        records,
                    )
                    sandbox_create_text = _text(sandbox_create)
                    if '"error"' not in sandbox_create_text and not sandbox_create_text.startswith(
                        "Error:"
                    ):
                        sandbox_info = json.loads(sandbox_create_text)
                        sandbox_id = sandbox_info["sandbox_id"]
                        await _timed_call(client, "nexus_sandbox_list", {}, records)
                        await _timed_call(
                            client,
                            "nexus_python",
                            {"sandbox_id": sandbox_id, "code": 'print("issue4131 sandbox")'},
                            records,
                        )
                        await _timed_call(
                            client,
                            "nexus_bash",
                            {"sandbox_id": sandbox_id, "command": "echo issue4131"},
                            records,
                        )
                        await _timed_call(
                            client,
                            "nexus_sandbox_stop",
                            {"sandbox_id": sandbox_id},
                            records,
                        )
                else:
                    for sandbox_tool in sorted(OPTIONAL_SANDBOX_TOOLS):
                        if sandbox_tool not in registered:
                            records.append(PerfRecord(sandbox_tool, 0.0, "not_registered"))

                await _timed_call(
                    client, "nexus_delete_file", {"path": "/issue4131/api-renamed.txt"}, records
                )
                await _timed_call(
                    client, "nexus_delete_file", {"path": "/issue4131/medium.txt"}, records
                )
                await _timed_call(
                    client,
                    "nexus_rmdir",
                    {"path": "/issue4131", "recursive": True},
                    records,
                )
        finally:
            reset_request_api_key(token)
    finally:
        _print_perf(records)
        close = getattr(nx, "close", None)
        if callable(close):
            close()
