"""Runtime discovery for FastAPI exposed RPC methods."""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from scripts.surface_coverage.schema import ProfileStatus, SurfaceCoverage
from scripts.surface_coverage.validate import ValidationFinding

RUNTIME_BUILD_COMMAND = "cargo build --release -p nexus-cluster --bin nexusd-cluster"
_KERNEL_BINARY_ENV = "NEXUS_KERNEL_BINARY"
_KERNEL_BINARY_NAMES = ("nexusd-cluster", "nexus-cluster")
_TARGET_PROFILES = ("release", "debug")


_RUNTIME_DISCOVERABLE_PROFILE_STATUSES = {
    ProfileStatus.SUPPORTED,
    ProfileStatus.ADMIN_ONLY,
}


def matrix_rpc_method_names(coverage: SurfaceCoverage, *, profile: str | None = None) -> set[str]:
    """Return matrix method names expected to appear in runtime RPC discovery."""

    methods: set[str] = set()
    for op in coverage.operations:
        if (
            profile is not None
            and op.profiles.get(profile) not in _RUNTIME_DISCOVERABLE_PROFILE_STATUSES
        ):
            continue
        for transport in ("grpc_expose", "grpc_call"):
            cell = op.transports.get(transport)
            if cell is not None:
                if transport == "grpc_expose" and cell.name.startswith("brick:"):
                    continue
                methods.add(cell.name)
    return methods


def compare_runtime_exposed_methods(
    *,
    matrix_methods: set[str],
    runtime_methods: set[str],
) -> list[ValidationFinding]:
    """Compare committed matrix RPC names to runtime-discovered RPC names."""

    findings: list[ValidationFinding] = []
    for method in sorted(runtime_methods - matrix_methods):
        findings.append(
            ValidationFinding(
                code="runtime_exposed_method_missing_matrix_row",
                operation_id=method,
                field="transports.grpc_expose_or_grpc_call",
                severity="error",
                message="runtime method is not represented by any grpc_expose or grpc_call matrix row",
            )
        )
    for method in sorted(matrix_methods - runtime_methods):
        findings.append(
            ValidationFinding(
                code="matrix_rpc_method_missing_runtime_discovery",
                operation_id=method,
                field="app.state.exposed_methods",
                severity="error",
                message=(
                    "matrix grpc_expose/grpc_call method was not discovered from "
                    "create_app(...).state.exposed_methods"
                ),
            )
        )
    return findings


def runtime_kernel_syscall_method_names() -> set[str]:
    """Return static generic Call names handled before dynamic RPC dispatch."""

    from nexus.server._kernel_syscall_dispatch import KERNEL_SYSCALL_NAMES

    return set(KERNEL_SYSCALL_NAMES)


def resolve_runtime_kernel_binary(*, repo_root: Path | None = None) -> Path | None:
    """Return an available gRPC kernel binary for runtime discovery, if any."""

    configured = os.environ.get(_KERNEL_BINARY_ENV)
    if configured:
        resolved = _resolve_configured_binary(configured)
        if resolved is not None:
            return resolved

    root = repo_root or _repo_root()
    for candidate in _worktree_binary_candidates(root):
        if _is_executable_file(candidate):
            return candidate

    for binary_name in _KERNEL_BINARY_NAMES:
        resolved = shutil.which(binary_name)
        if resolved:
            return Path(resolved)

    return None


def discover_runtime_exposed_methods(*, data_dir: Path) -> set[str]:
    """Build a minimal Nexus app and return app.state.exposed_methods keys."""

    kernel_binary = resolve_runtime_kernel_binary()
    if kernel_binary is None:
        raise FileNotFoundError(
            "nexusd-cluster/nexus-cluster binary not found; build it with "
            f"`{RUNTIME_BUILD_COMMAND}` or put nexusd-cluster/nexus-cluster on PATH"
        )

    with _temporary_kernel_binary(kernel_binary):
        import nexus
        from nexus.server.fastapi_server import create_app

        nx = nexus.connect(config={"profile": "sandbox", "data_dir": str(data_dir)})
        try:
            app = create_app(nexus_fs=nx)
            exposed = getattr(app.state, "exposed_methods", {})
            return set(exposed) | runtime_kernel_syscall_method_names()
        finally:
            close = getattr(nx, "close", None)
            if callable(close):
                close()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_configured_binary(configured: str) -> Path | None:
    path = Path(configured).expanduser()
    if _is_executable_file(path):
        return path

    resolved = shutil.which(configured)
    if resolved:
        return Path(resolved)

    return None


def _worktree_binary_candidates(repo_root: Path) -> Iterator[Path]:
    suffix = ".exe" if os.name == "nt" else ""
    target_roots = (repo_root / "target", repo_root / "rust" / "target")
    for target_root in target_roots:
        for profile in _TARGET_PROFILES:
            for binary_name in _KERNEL_BINARY_NAMES:
                yield target_root / profile / f"{binary_name}{suffix}"


def _is_executable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


@contextlib.contextmanager
def _temporary_kernel_binary(kernel_binary: Path) -> Iterator[None]:
    previous = os.environ.get(_KERNEL_BINARY_ENV)
    os.environ[_KERNEL_BINARY_ENV] = str(kernel_binary)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_KERNEL_BINARY_ENV, None)
        else:
            os.environ[_KERNEL_BINARY_ENV] = previous
