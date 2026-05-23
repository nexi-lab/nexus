"""Runtime discovery checks for create_app(...).state.exposed_methods."""

import os
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.surface_coverage import runtime_discovery
from scripts.surface_coverage.runtime_discovery import (
    RUNTIME_BUILD_COMMAND,
    compare_runtime_exposed_methods,
    discover_runtime_exposed_methods,
    matrix_rpc_method_names,
    runtime_kernel_syscall_method_names,
)
from scripts.surface_coverage.schema import (
    Operation,
    ProfileStatus,
    SurfaceCoverage,
    TransportCell,
)


def _coverage(*ops: Operation) -> SurfaceCoverage:
    return SurfaceCoverage(schema_version=1, modules=[], operations=list(ops))


def _rpc_op(
    op_id: str,
    transport: str,
    name: str,
    *,
    sandbox: ProfileStatus = ProfileStatus.SUPPORTED,
) -> Operation:
    return Operation(
        id=op_id,
        module=op_id.split(".", 1)[0],
        summary="runtime row",
        transports={transport: TransportCell(name=name, source="src/x.py:1")},
        profiles={
            "lite": ProfileStatus.SUPPORTED,
            "sandbox": sandbox,
            "full": ProfileStatus.SUPPORTED,
        },
    )


def _touch_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _install_fake_kernel_syscalls(monkeypatch: pytest.MonkeyPatch, names: set[str]) -> None:
    fake_kernel_dispatch = types.ModuleType("nexus.server._kernel_syscall_dispatch")
    cast(Any, fake_kernel_dispatch).KERNEL_SYSCALL_NAMES = frozenset(names)
    monkeypatch.setitem(sys.modules, "nexus.server._kernel_syscall_dispatch", fake_kernel_dispatch)


def test_runtime_build_command_targets_cluster_binary() -> None:
    assert RUNTIME_BUILD_COMMAND == "cargo build --release -p nexus-cluster --bin nexusd-cluster"


def test_resolve_runtime_kernel_binary_prefers_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configured = _touch_executable(tmp_path / "custom" / "nexusd-cluster")
    worktree = _touch_executable(tmp_path / "repo" / "target" / "release" / "nexusd-cluster")
    monkeypatch.setenv("NEXUS_KERNEL_BINARY", str(configured))

    assert (
        runtime_discovery.resolve_runtime_kernel_binary(repo_root=worktree.parents[2]) == configured
    )


def test_resolve_runtime_kernel_binary_prefers_worktree_before_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = _touch_executable(tmp_path / "repo" / "target" / "debug" / "nexusd-cluster")
    path_binary = _touch_executable(tmp_path / "bin" / "nexusd-cluster")
    monkeypatch.delenv("NEXUS_KERNEL_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(path_binary.parent))

    assert (
        runtime_discovery.resolve_runtime_kernel_binary(repo_root=worktree.parents[2]) == worktree
    )


def test_resolve_runtime_kernel_binary_returns_none_without_cluster_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("NEXUS_KERNEL_BINARY", raising=False)
    monkeypatch.setenv("PATH", "")

    assert runtime_discovery.resolve_runtime_kernel_binary(repo_root=tmp_path / "repo") is None


def test_matrix_rpc_method_names_uses_grpc_expose_and_call_cells() -> None:
    coverage = _coverage(
        _rpc_op("filesystem.read", "grpc_expose", "read_file"),
        _rpc_op("filesystem.write", "grpc_call", "write"),
        _rpc_op("filesystem.cli", "cli", "nexus write"),
    )

    assert matrix_rpc_method_names(coverage) == {"read_file", "write"}


def test_matrix_rpc_method_names_filters_by_runtime_profile() -> None:
    coverage = _coverage(
        _rpc_op("filesystem.read", "grpc_call", "read"),
        _rpc_op(
            "oauth.list_providers",
            "grpc_expose",
            "oauth_list_providers",
            sandbox=ProfileStatus.UNAVAILABLE,
        ),
    )

    assert matrix_rpc_method_names(coverage, profile="sandbox") == {"read"}


def test_matrix_rpc_method_names_skips_brick_inventory_rows() -> None:
    coverage = _coverage(
        _rpc_op("auth.brick", "grpc_expose", "brick:auth"),
        _rpc_op("filesystem.read", "grpc_call", "read"),
    )

    assert matrix_rpc_method_names(coverage) == {"read"}


def test_runtime_kernel_syscall_method_names_reads_dispatch_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel_syscalls(monkeypatch, {"read", "write"})

    assert runtime_kernel_syscall_method_names() == {"read", "write"}


def test_kernel_syscall_grpc_call_matrix_rows_are_runtime_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel_syscalls(monkeypatch, {"read"})
    coverage = _coverage(
        _rpc_op("filesystem.read", "grpc_call", "read"),
        _rpc_op("filesystem.dynamic", "grpc_expose", "dynamic_method"),
    )

    findings = compare_runtime_exposed_methods(
        matrix_methods=matrix_rpc_method_names(coverage),
        runtime_methods=runtime_kernel_syscall_method_names() | {"dynamic_method"},
    )

    assert findings == []


def test_compare_runtime_exposed_methods_reports_runtime_only_methods() -> None:
    findings = compare_runtime_exposed_methods(
        matrix_methods={"read", "write"},
        runtime_methods={"read", "write", "runtime_only"},
    )

    assert [(f.code, f.operation_id, f.field, f.severity, f.message) for f in findings] == [
        (
            "runtime_exposed_method_missing_matrix_row",
            "runtime_only",
            "transports.grpc_expose_or_grpc_call",
            "error",
            "runtime method is not represented by any grpc_expose or grpc_call matrix row",
        )
    ]


def test_compare_runtime_exposed_methods_reports_missing_runtime_methods() -> None:
    findings = compare_runtime_exposed_methods(
        matrix_methods={"read", "write"},
        runtime_methods={"read"},
    )

    assert [(f.code, f.operation_id, f.field, f.severity, f.message) for f in findings] == [
        (
            "matrix_rpc_method_missing_runtime_discovery",
            "write",
            "app.state.exposed_methods",
            "error",
            "matrix grpc_expose/grpc_call method was not discovered from "
            "create_app(...).state.exposed_methods",
        )
    ]


def test_discover_runtime_exposed_methods_sets_resolved_kernel_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    kernel_binary = _touch_executable(tmp_path / "repo" / "target" / "release" / "nexusd-cluster")
    seen: dict[str, str | None] = {}

    class FakeNexusFs:
        def close(self) -> None:
            seen["during_close"] = os.environ.get("NEXUS_KERNEL_BINARY")

    def connect(config: dict[str, str]) -> FakeNexusFs:
        seen["config_profile"] = config["profile"]
        seen["config_data_dir"] = config["data_dir"]
        seen["during_connect"] = os.environ.get("NEXUS_KERNEL_BINARY")
        return FakeNexusFs()

    def create_app(*, nexus_fs: FakeNexusFs) -> types.SimpleNamespace:
        seen["during_create_app"] = os.environ.get("NEXUS_KERNEL_BINARY")
        return types.SimpleNamespace(
            state=types.SimpleNamespace(exposed_methods={"read": object(), "write": object()})
        )

    fake_nexus = types.ModuleType("nexus")
    cast(Any, fake_nexus).connect = connect
    fake_server = types.ModuleType("nexus.server")
    fake_fastapi_server = types.ModuleType("nexus.server.fastapi_server")
    cast(Any, fake_fastapi_server).create_app = create_app
    monkeypatch.setitem(sys.modules, "nexus", fake_nexus)
    monkeypatch.setitem(sys.modules, "nexus.server", fake_server)
    monkeypatch.setitem(sys.modules, "nexus.server.fastapi_server", fake_fastapi_server)
    _install_fake_kernel_syscalls(monkeypatch, {"sys_read"})
    monkeypatch.delenv("NEXUS_KERNEL_BINARY", raising=False)
    monkeypatch.setattr(runtime_discovery, "resolve_runtime_kernel_binary", lambda: kernel_binary)

    methods = discover_runtime_exposed_methods(data_dir=tmp_path / "data")

    assert methods == {"read", "write", "sys_read"}
    assert seen == {
        "config_profile": "sandbox",
        "config_data_dir": str(tmp_path / "data"),
        "during_connect": str(kernel_binary),
        "during_create_app": str(kernel_binary),
        "during_close": str(kernel_binary),
    }
    assert "NEXUS_KERNEL_BINARY" not in os.environ


def test_discover_runtime_exposed_methods_skips_without_runtime(tmp_path: Path) -> None:
    kernel_binary = runtime_discovery.resolve_runtime_kernel_binary(repo_root=Path.cwd())
    if kernel_binary is None:
        pytest.skip(f"requires runtime build: {RUNTIME_BUILD_COMMAND}")
    methods = discover_runtime_exposed_methods(data_dir=tmp_path)
    assert isinstance(methods, set)
    assert methods, "runtime discovery should return at least one exposed method"
