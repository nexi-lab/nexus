"""gRPC typed extractor: parse `rpc <Name>(...)` from .proto via regex."""

from pathlib import Path

from scripts.surface_coverage.extract_grpc_typed import extract_grpc_typed_methods


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
    real = repo_root / "proto/nexus/grpc/vfs/vfs.proto"
    if not real.exists():
        return
    results = extract_grpc_typed_methods(real)
    assert all("." in r.method for r in results)
