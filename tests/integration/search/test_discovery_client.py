"""SDK discovery over TCP on the filesystem transport, with per-call credentials."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import grpc
import pytest

from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.lib.request_credentials import request_api_key
from nexus.remote.rpc_transport import RPCTransport
from nexus.remote.service_proxy import RemoteServiceProxy


class Host(search_pb2_grpc.SearchServiceServicer):
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, str]]] = []
        self.line = "needle current"
        self.filters = 7
        self.truncated = False

    def Glob(self, request: Any, context: Any) -> Any:
        self.calls.append(("glob", request, dict(context.invocation_metadata())))
        return search_pb2.GlobResponse(
            paths=[] if request.HasField("files") and not request.files.paths else ["/docs/a.md"],
            applied_filters=self.filters,
            truncated=self.truncated,
        )

    def Grep(self, request: Any, context: Any) -> Any:
        self.calls.append(("grep", request, dict(context.invocation_metadata())))
        if request.HasField("files") and not request.files.paths:
            return search_pb2.GrepResponse(applied_filters=self.filters)
        return search_pb2.GrepResponse(
            matches=[
                search_pb2.GrepMatch(
                    path="/docs/a.md",
                    line_number=3,
                    line=self.line,
                    before=["# API", "before"],
                    after=["after"],
                    section=search_pb2.GrepSection(
                        heading="API", depth=1, line_start=1, line_end=4
                    ),
                )
            ],
            applied_filters=self.filters,
        )


@pytest.fixture
def discovery():
    host = Host()
    with ThreadPoolExecutor(max_workers=4) as pool:
        server = grpc.server(pool)
        search_pb2_grpc.add_SearchServiceServicer_to_server(host, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        transport = RPCTransport(f"127.0.0.1:{port}", auth_token="default-key")
        try:
            yield host, transport, RemoteServiceProxy(transport.call_rpc, "search")
        finally:
            transport.close()
            server.stop(0).wait()


def test_sdk_refinement_preserves_scope_context_and_live_responses(discovery):
    host, _transport, search = discovery
    files = search.glob("**/*.md", path="/docs")
    assert files == ["/docs/a.md"]
    rows = search.grep(
        "needle",
        path="/docs",
        files=files,
        block_type="code",
        section="API",
        before_context=2,
        after_context=1,
    )
    assert rows == [
        {
            "file": "/docs/a.md",
            "line": 3,
            "content": "needle current",
            "before_context": [{"line": 1, "content": "# API"}, {"line": 2, "content": "before"}],
            "after_context": [{"line": 4, "content": "after"}],
            "section": {"heading": "API", "depth": 1, "line_start": 1, "line_end": 4},
        }
    ]
    host.line = "needle revised"
    assert search.grep("needle", files=files)[0]["content"] == "needle revised"
    assert search.grep("needle", files=[]) == []
    assert search.glob("**/*", files=[]) == []
    assert host.calls[-1][1].HasField("files")
    assert all(metadata["authorization"] == "Bearer default-key" for _, _, metadata in host.calls)


def test_credentials_are_request_scoped_and_empty_bearer_remains_explicit(discovery):
    host, _transport, search = discovery
    for credential in ("alice-key", "bob-key", ""):
        token = request_api_key.set(credential)
        try:
            search.grep("needle", files=["/docs/a.md"])
        finally:
            request_api_key.reset(token)
        assert host.calls[-1][2]["authorization"] == f"Bearer {credential}"
        assert host.calls[-1][1].auth_token == ""
    search.grep("needle")
    assert host.calls[-1][2]["authorization"] == "Bearer default-key"


def test_glob_refinement_rejects_an_incomplete_working_set(discovery):
    host, _transport, search = discovery
    host.truncated = True
    with pytest.raises(ValueError, match="narrow the path or working set"):
        search.glob("**/*", path="/docs")


def test_per_call_credentials_preserve_presence_and_request_identity(discovery):
    host, transport, _search = discovery
    transport.call_rpc("grep", {"pattern": "needle", "files": []}, auth_token="")
    assert host.calls[-1][2]["authorization"] == "Bearer "
    caller = request_api_key.set("caller-key")
    try:
        transport.call_rpc("grep", {"pattern": "needle"}, auth_token="override-key")
    finally:
        request_api_key.reset(caller)
    assert host.calls[-1][2]["authorization"] == "Bearer caller-key"
    assert host.calls[-1][1].auth_token == ""


def test_scoped_root_is_preserved_at_the_typed_rpc_boundary(discovery):
    host, _transport, search = discovery
    root = "/zone/tenant-a/workspace"
    search.glob("**/*.md", path=root)
    assert host.calls[-1][1].root_path == root
    assert host.calls[-1][1].pattern == "**/*.md"


@pytest.mark.parametrize(
    "operation,kwargs",
    [
        ("glob", {"files": []}),
        ("grep", {"files": []}),
        ("grep", {"block_type": "code"}),
        ("grep", {"section": "API"}),
        ("grep", {"files": [], "block_type": "code", "section": "API"}),
    ],
)
def test_partial_or_old_filter_acknowledgements_fail(discovery, operation, kwargs):
    host, _transport, search = discovery
    host.filters = 0
    with pytest.raises(RuntimeError, match="every requested discovery filter"):
        getattr(search, operation)("needle", **kwargs)


def test_partial_acknowledgement_cannot_drop_markdown_selection(discovery):
    host, _transport, search = discovery
    host.filters = search_pb2.DISCOVERY_FILTER_FILES
    with pytest.raises(RuntimeError, match="every requested discovery filter"):
        search.grep("needle", files=["/docs/a.md"], block_type="code", section="API")


def test_unsupported_modes_and_invalid_working_sets_are_rejected_before_rpc(discovery):
    host, _transport, search = discovery
    with pytest.raises(ValueError, match="current file bytes"):
        search.grep("needle", search_mode="parsed")
    with pytest.raises(ValueError, match="list of VFS paths"):
        search.grep("needle", files="/docs/a.md")
    assert not host.calls
