"""``nexus-fs doctor`` — diagnostic tool for the slim filesystem package.

Three-section diagnostic: Environment, Backends, Mounts.
Check logic is strictly separated from rendering for testability.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Check result model (4-state, independent of main CLI's 3-state enum)
# ---------------------------------------------------------------------------

_CHECK_TIMEOUT_S = 3.0
_OVERALL_TIMEOUT_S = 5.0


class DoctorStatus(Enum):
    """Four-state status for nexus-fs diagnostic checks."""

    PASS = "pass"
    FAIL = "fail"
    NOT_INSTALLED = "not_installed"
    CONNECTED = "connected"


@dataclass(frozen=True)
class DoctorCheckResult:
    """Structured result from a single diagnostic check."""

    name: str
    status: DoctorStatus
    message: str
    fix_hint: str | None = None
    latency_ms: float | None = None
    install_cmd: str | None = None


# ---------------------------------------------------------------------------
# Section 1: Environment checks
# ---------------------------------------------------------------------------


def check_python_version() -> DoctorCheckResult:
    """Check Python version (>= 3.11 required, per pyproject.toml)."""
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 11):
        return DoctorCheckResult(
            name="python",
            status=DoctorStatus.FAIL,
            message=f"Python {major}.{minor} detected; 3.11+ required",
            fix_hint="Install Python 3.11 or later",
        )
    return DoctorCheckResult(
        name="python",
        status=DoctorStatus.PASS,
        message=f"Python {major}.{minor}",
    )


def check_nexus_fs_version() -> DoctorCheckResult:
    """Check nexus-fs package version."""
    try:
        version = importlib.metadata.version("nexus-fs")
        return DoctorCheckResult(
            name="nexus-fs",
            status=DoctorStatus.PASS,
            message=f"v{version}",
        )
    except importlib.metadata.PackageNotFoundError:
        # Fallback: try reading __version__ from the module
        try:
            from nexus.fs import __version__

            return DoctorCheckResult(
                name="nexus-fs",
                status=DoctorStatus.PASS,
                message=f"v{__version__} (dev)",
            )
        except Exception:
            return DoctorCheckResult(
                name="nexus-fs",
                status=DoctorStatus.FAIL,
                message="unable to determine version",
            )


def check_nexus_kernel_version() -> DoctorCheckResult:
    """Check nexus-kernel (Rust/pyo3) availability."""
    try:
        import nexus_kernel

        version = getattr(nexus_kernel, "__version__", "unknown")
        return DoctorCheckResult(
            name="nexus-kernel",
            status=DoctorStatus.PASS,
            message=f"v{version}",
        )
    except ImportError:
        return DoctorCheckResult(
            name="nexus-kernel",
            status=DoctorStatus.NOT_INSTALLED,
            message="Rust accelerator not installed (optional)",
            install_cmd="pip install nexus-pyo3",
        )


# ---------------------------------------------------------------------------
# Section 2: Backend checks (installed + credentials)
# ---------------------------------------------------------------------------

_BACKEND_EXTRAS: dict[str, tuple[str, str]] = {
    "s3": ("boto3", "nexus-fs[s3]"),
    "gcs": ("google.cloud.storage", "nexus-fs[gcs]"),
    "gdrive": ("googleapiclient", "nexus-fs[gdrive]"),
}


def check_backend_installed(scheme: str) -> DoctorCheckResult:
    """Check if a backend's optional dependency is installed."""
    if scheme == "local":
        return DoctorCheckResult(
            name=f"{scheme}-backend",
            status=DoctorStatus.PASS,
            message="built-in (no extra deps)",
        )

    if scheme not in _BACKEND_EXTRAS:
        return DoctorCheckResult(
            name=f"{scheme}-backend",
            status=DoctorStatus.NOT_INSTALLED,
            message=f"unknown backend scheme '{scheme}'",
        )

    module_name, pip_extra = _BACKEND_EXTRAS[scheme]
    try:
        importlib.import_module(module_name)
        return DoctorCheckResult(
            name=f"{scheme}-backend",
            status=DoctorStatus.PASS,
            message="installed",
        )
    except ImportError:
        return DoctorCheckResult(
            name=f"{scheme}-backend",
            status=DoctorStatus.NOT_INSTALLED,
            message="not installed",
            install_cmd=f"pip install {pip_extra}",
        )


def check_backend_credentials(scheme: str) -> DoctorCheckResult:
    """Check credential presence and validity for a backend.

    Calls _credentials.py for presence, then validates if possible.
    """
    if scheme == "local":
        return DoctorCheckResult(
            name=f"{scheme}-creds",
            status=DoctorStatus.PASS,
            message="no credentials needed",
        )

    if scheme == "gdrive":
        return DoctorCheckResult(
            name=f"{scheme}-creds",
            status=DoctorStatus.PASS,
            message="deferred to `nexus auth gdrive`",
        )

    # Check presence first
    from nexus.contracts.exceptions import CloudCredentialError
    from nexus.fs._credentials import discover_credentials

    try:
        source_info = discover_credentials(scheme)
    except CloudCredentialError as exc:
        return DoctorCheckResult(
            name=f"{scheme}-creds",
            status=DoctorStatus.FAIL,
            message="credentials not found",
            fix_hint=str(exc),
        )

    # Validate credentials
    if scheme == "s3":
        from nexus.fs._credentials import validate_aws_credentials

        result = validate_aws_credentials()
        if result["valid"]:
            return DoctorCheckResult(
                name=f"{scheme}-creds",
                status=DoctorStatus.PASS,
                message=f"valid ({source_info.get('source', 'unknown')} — {result.get('arn', '')})",
            )
        return DoctorCheckResult(
            name=f"{scheme}-creds",
            status=DoctorStatus.FAIL,
            message=f"credentials found but invalid: {result.get('error', 'unknown')}",
            fix_hint="Check AWS credentials: aws sts get-caller-identity",
        )

    if scheme == "gcs":
        from nexus.fs._credentials import validate_gcs_credentials

        result = validate_gcs_credentials()
        if result["valid"]:
            return DoctorCheckResult(
                name=f"{scheme}-creds",
                status=DoctorStatus.PASS,
                message=(
                    f"valid ({source_info.get('source', 'unknown')}"
                    f" — project: {result.get('project', '')})"
                ),
            )
        return DoctorCheckResult(
            name=f"{scheme}-creds",
            status=DoctorStatus.FAIL,
            message=f"credentials found but invalid: {result.get('error', 'unknown')}",
            fix_hint="Refresh credentials: gcloud auth application-default login",
        )

    # Fallback for other schemes
    return DoctorCheckResult(
        name=f"{scheme}-creds",
        status=DoctorStatus.PASS,
        message=f"source: {source_info.get('source', 'unknown')}",
    )


# ---------------------------------------------------------------------------
# Section 3: Mount connectivity checks
# ---------------------------------------------------------------------------


async def check_mount_connectivity(mount_point: str, fs: Any) -> DoctorCheckResult:
    """Check connectivity to a mounted backend by listing its root.

    Args:
        mount_point: The mount point path (e.g., "/s3/my-bucket").
        fs: A SlimNexusFS instance.
    """
    start = time.perf_counter()
    try:
        fs.ls(mount_point)
        latency_ms = (time.perf_counter() - start) * 1000
        return DoctorCheckResult(
            name=mount_point,
            status=DoctorStatus.CONNECTED,
            message=f"connected ({latency_ms:.0f}ms)",
            latency_ms=latency_ms,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        error_type = type(exc).__name__
        return DoctorCheckResult(
            name=mount_point,
            status=DoctorStatus.FAIL,
            message=f"{error_type}: {exc}",
            fix_hint="Check network connectivity and backend permissions",
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Runner — concurrent execution with per-check timeouts
# ---------------------------------------------------------------------------


async def _run_with_timeout(
    coro: Any,
    timeout_s: float = _CHECK_TIMEOUT_S,
    fallback_name: str = "unknown",
) -> DoctorCheckResult:
    """Run a check coroutine with a timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except TimeoutError:
        return DoctorCheckResult(
            name=fallback_name,
            status=DoctorStatus.FAIL,
            message=f"timed out after {timeout_s:.0f}s",
            fix_hint="Check network connectivity and DNS resolution",
        )


async def _run_all_checks_inner(
    fs: Any | None = None,
) -> dict[str, list[DoctorCheckResult]]:
    """Core check logic — called within the overall timeout wrapper."""
    # Section 1: Environment (sync checks, run in thread pool)
    env_checks = [check_python_version, check_nexus_fs_version, check_nexus_kernel_version]
    env_coros = [asyncio.to_thread(fn) for fn in env_checks]

    # Section 2: Backends (sync checks, run in thread pool)
    schemes = ["s3", "gcs", "local", "gdrive"]
    backend_install_coros = [asyncio.to_thread(check_backend_installed, s) for s in schemes]
    backend_cred_coros = [asyncio.to_thread(check_backend_credentials, s) for s in schemes]

    # Run env + backend checks concurrently with per-check timeouts
    all_coros = env_coros + backend_install_coros + backend_cred_coros
    all_names = (
        ["python", "nexus-fs", "nexus-kernel"]
        + [f"{s}-backend" for s in schemes]
        + [f"{s}-creds" for s in schemes]
    )

    wrapped = [
        _run_with_timeout(coro, fallback_name=name)
        for coro, name in zip(all_coros, all_names, strict=True)
    ]
    results = await asyncio.gather(*wrapped, return_exceptions=True)

    # Safely collect results (convert unexpected exceptions to FAIL)
    collected: list[DoctorCheckResult] = []
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            collected.append(
                DoctorCheckResult(
                    name=all_names[i],
                    status=DoctorStatus.FAIL,
                    message=f"unexpected error: {r}",
                )
            )
        else:
            collected.append(r)

    env_results = collected[:3]
    backend_results = collected[3:]

    # Interleave install + cred results per scheme
    install_results = backend_results[: len(schemes)]
    cred_results = backend_results[len(schemes) :]
    backends_combined: list[DoctorCheckResult] = []
    for inst, cred in zip(install_results, cred_results, strict=True):
        backends_combined.append(inst)
        # Only show cred check if backend is installed
        if inst.status != DoctorStatus.NOT_INSTALLED:
            backends_combined.append(cred)

    # Section 3: Mounts (async, need fs instance)
    mount_results: list[DoctorCheckResult] = []
    if fs is not None:
        mounts = fs.list_mounts()
        if mounts:
            mount_coros = [
                _run_with_timeout(
                    check_mount_connectivity(mp, fs),
                    fallback_name=mp,
                )
                for mp in mounts
            ]
            raw_mount = await asyncio.gather(*mount_coros, return_exceptions=True)
            for i, r in enumerate(raw_mount):
                if isinstance(r, BaseException):
                    mount_results.append(
                        DoctorCheckResult(
                            name=mounts[i],
                            status=DoctorStatus.FAIL,
                            message=f"unexpected error: {r}",
                        )
                    )
                else:
                    mount_results.append(r)
        else:
            mount_results.append(
                DoctorCheckResult(
                    name="mounts",
                    status=DoctorStatus.PASS,
                    message="no mounts configured",
                    fix_hint='Mount a backend: nexus.fs.mount("s3://bucket")',
                )
            )

    return {
        "Environment": env_results,
        "Backends": backends_combined,
        "Mounts": mount_results,
    }


async def run_all_checks(
    fs: Any | None = None,
    overall_timeout: float = _OVERALL_TIMEOUT_S,
) -> dict[str, list[DoctorCheckResult]]:
    """Run all diagnostic checks with an overall timeout.

    Guarantees completion within ``overall_timeout`` seconds.

    Args:
        fs: Optional SlimNexusFS instance for mount connectivity checks.
            If None, mount checks are skipped.
        overall_timeout: Maximum wall-clock seconds for the entire run.

    Returns:
        Dict mapping section name to list of check results.
    """
    try:
        return await asyncio.wait_for(
            _run_all_checks_inner(fs=fs),
            timeout=overall_timeout,
        )
    except TimeoutError:
        return {
            "Environment": [
                DoctorCheckResult(
                    name="doctor",
                    status=DoctorStatus.FAIL,
                    message=f"overall timeout after {overall_timeout:.0f}s",
                    fix_hint="Some checks are slow. Check network connectivity.",
                )
            ],
            "Backends": [],
            "Mounts": [],
        }


# ---------------------------------------------------------------------------
# Tip generator
# ---------------------------------------------------------------------------


def generate_tip(results: dict[str, list[DoctorCheckResult]]) -> str | None:
    """Generate a context-aware tip based on check results.

    Returns a single actionable suggestion, or None if everything looks good.
    """
    all_results = [r for section in results.values() for r in section]
    statuses = {r.name: r.status for r in all_results}

    # Priority 1: Any failures — suggest fixing the most critical one
    failures = [r for r in all_results if r.status == DoctorStatus.FAIL]
    if failures:
        first = failures[0]
        if first.fix_hint:
            return f"Fix: {first.fix_hint}"
        return f"Investigate: {first.name} — {first.message}"

    # Priority 2: No backends installed beyond local
    cloud_installed = any(
        statuses.get(f"{s}-backend") == DoctorStatus.PASS for s in ("s3", "gcs", "gdrive")
    )
    if not cloud_installed:
        return "Try a cloud backend: pip install nexus-fs[s3] or nexus-fs[gcs]"

    # Priority 3: Only one mount — suggest adding another
    mount_results = results.get("Mounts", [])
    connected = [r for r in mount_results if r.status == DoctorStatus.CONNECTED]
    if len(connected) == 1:
        return "Mount a second backend to try cross-backend copy"

    # Priority 4: No mounts at all
    if not connected and mount_results:
        first = mount_results[0]
        if first.name == "mounts":
            return 'Mount a backend: fs = await nexus.fs.mount("s3://bucket")'

    return None


# ---------------------------------------------------------------------------
# Renderer — Rich output, separated from check logic
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    DoctorStatus.PASS: "[green]✓[/green]",
    DoctorStatus.FAIL: "[red]✗[/red]",
    DoctorStatus.NOT_INSTALLED: "[dim]○[/dim]",
    DoctorStatus.CONNECTED: "[green]●[/green]",
}


def render_doctor(
    results: dict[str, list[DoctorCheckResult]],
    console: Any | None = None,
) -> None:
    """Render diagnostic results to the terminal.

    Args:
        results: Output from ``run_all_checks()``.
        console: Optional Rich Console instance. Creates one if not provided.
    """
    from rich.console import Console
    from rich.table import Table

    if console is None:
        console = Console()

    for section_name, checks in results.items():
        if not checks:
            continue

        table = Table(
            title=section_name,
            show_header=False,
            show_edge=False,
            show_lines=False,
            padding=(0, 1),
            title_style="bold",
        )
        table.add_column("status", width=3, no_wrap=True)
        table.add_column("name", style="cyan", no_wrap=True)
        table.add_column("detail")

        for check in checks:
            icon = _STATUS_ICONS[check.status]

            detail = check.message
            if check.install_cmd and check.status == DoctorStatus.NOT_INSTALLED:
                # Escape Rich markup in install commands (e.g., nexus-fs[s3])
                escaped_cmd = check.install_cmd.replace("[", r"\[")
                detail += f"  [dim]({escaped_cmd})[/dim]"
            if check.fix_hint and check.status == DoctorStatus.FAIL:
                detail += f"\n       [dim]{check.fix_hint}[/dim]"

            table.add_row(icon, check.name, detail)

        console.print(table)
        console.print()

    # Summary line
    all_checks = [r for section in results.values() for r in section]
    total = len(all_checks)
    passed = sum(1 for c in all_checks if c.status in (DoctorStatus.PASS, DoctorStatus.CONNECTED))
    failed = sum(1 for c in all_checks if c.status == DoctorStatus.FAIL)
    not_installed = sum(1 for c in all_checks if c.status == DoctorStatus.NOT_INSTALLED)

    summary_parts = [f"[green]{passed} passed[/green]"]
    if failed:
        summary_parts.append(f"[red]{failed} failed[/red]")
    if not_installed:
        summary_parts.append(f"[dim]{not_installed} not installed[/dim]")

    console.print(f"[bold]{total} checks:[/bold] {', '.join(summary_parts)}")

    # Context-aware tip
    tip = generate_tip(results)
    if tip:
        console.print(f"\n[cyan]Tip:[/cyan] {tip}")
