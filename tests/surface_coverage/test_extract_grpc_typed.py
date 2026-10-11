"""gRPC typed extractor: parse `rpc <Name>(...)` from .proto via regex."""

from pathlib import Path

from scripts.surface_coverage.extract_grpc_typed import (
    extract_generated_grpc_methods,
    extract_grpc_typed_methods,
)


def test_extract_grpc_typed_from_fixture(tmp_path: Path):
    f = tmp_path / "vfs.proto"
    f.write_text(
        "syntax = 'proto3';\n"
        "package nexus.vfs;\n"
        "\n"
        "service VFS {\n"
        "  rpc Read (ReadRequest) returns (ReadResponse);\n"
        "  rpc Write (WriteRequest) returns (WriteResponse);\n"
        "  rpc Stat (StatRequest) returns (StatResponse);\n"
        "}\n"
        "\n"
        "service Search {\n"
        "  rpc Query (QueryRequest) returns (QueryResponse);\n"
        "}\n"
    )
    results = extract_grpc_typed_methods(f)
    methods = {r.method for r in results}
    assert methods == {"VFS.Read", "VFS.Write", "VFS.Stat", "Search.Query"}


def test_extract_grpc_typed_real_proto_smoke(repo_root: Path):
    real = repo_root / "src/nexus/grpc/vfs/vfs_pb2_grpc.py"
    methods = {r.method for r in extract_generated_grpc_methods(real)}
    assert {"NexusVFSService.Read", "NexusVFSService.BatchRead", "NexusVFSService.Ping"} <= methods


def test_generated_bindings_use_stub_routes_and_ignore_server_helpers(tmp_path: Path):
    binding = tmp_path / "search_pb2_grpc.py"
    binding.write_text(
        "class SearchServiceStub:\n"
        "    def __init__(self, channel):\n"
        "        self.Query = channel.unary_unary('/nexus.search.v1.SearchService/Query')\n"
        "        self.Watch = channel.unary_stream('/nexus.search.v1.SearchService/Watch')\n"
        "        self.Upload = channel.stream_unary('/nexus.search.v1.SearchService/Upload')\n"
        "        self.Chat = channel.stream_stream('/nexus.search.v1.SearchService/Chat')\n"
        "        self.Invalid = channel.unary_unary('invalid')\n"
        "class SearchService:\n"
        "    def Helper(self, channel):\n"
        "        return channel.unary_unary('/nexus.search.v1.SearchService/Hidden')\n"
    )
    results = extract_generated_grpc_methods(binding)
    assert {r.method for r in results} == {
        "SearchService.Query",
        "SearchService.Watch",
        "SearchService.Upload",
        "SearchService.Chat",
    }
    assert next(r.source for r in results if r.method == "SearchService.Query") == f"{binding}:3"
