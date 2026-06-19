"""Op-id normalization across transports."""

import pytest

from scripts.surface_coverage.normalize import (
    normalize_cli,
    normalize_grpc_call,
    normalize_grpc_typed,
    normalize_http,
    normalize_mcp,
    normalize_sdk,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("nexus fs read", "fs.read"),
        ("nexus rebac grant", "rebac.grant"),
        ("nexus mounts list", "mounts.list"),
        ("nexus workspace snapshot create", "workspace.snapshot_create"),
    ],
)
def test_normalize_cli(raw, expected):
    assert normalize_cli(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("VFS.Read", "fs.read"),
        ("VFS.Write", "fs.write"),
        ("ReBAC.Grant", "rebac.grant"),
    ],
)
def test_normalize_grpc_typed(raw, expected):
    assert normalize_grpc_typed(raw) == expected


def test_normalize_grpc_call_passthrough():
    # generic Call names are already in module.verb form
    assert normalize_grpc_call("fs.read") == "fs.read"
    assert normalize_grpc_call("rebac.grant") == "rebac.grant"


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("POST", "/api/v1/fs/read", "fs.read"),
        ("GET", "/api/v1/rebac/grants", "rebac.grants"),
        ("POST", "/api/v1/workspace/snapshot/create", "workspace.snapshot_create"),
    ],
)
def test_normalize_http(method, path, expected):
    assert normalize_http(method, path) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("nexus_fs_read", "fs.read"),
        ("nexus_rebac_grant", "rebac.grant"),
        ("nexus_glob", "search.glob"),
        ("nexus_grep", "search.grep"),
        ("nexus_mkdir", "filesystem.mkdir"),
        ("nexus_rmdir", "filesystem.rmdir"),
    ],
)
def test_normalize_mcp(raw, expected):
    assert normalize_mcp(raw) == expected


def test_normalize_sdk():
    assert normalize_sdk("NexusClient", "read") == "fs.read"
    assert normalize_sdk("NexusClient", "rebac_grant") == "rebac.grant"
