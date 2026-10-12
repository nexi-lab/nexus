"""Native Kernel discovery and signed Search fixtures for integration tests."""

import os
import shutil
from pathlib import Path

import pytest


def _build_directories() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[2]
    return (
        root / "rust" / "target" / "release",
        root / "rust" / "target" / "debug",
        root / "target" / "release",
        root / "target" / "debug",
    )


def resolve_kernel_binary() -> Path | None:
    """Prefer the configured binary, then a worktree build, then PATH."""
    configured = os.environ.get("NEXUS_KERNEL_BINARY")
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
        resolved = shutil.which(configured)
        if resolved:
            return Path(resolved)
        pytest.fail(f"NEXUS_KERNEL_BINARY is unavailable: {configured}")

    for directory in _build_directories():
        for name in ("nexusd-cluster", "nexus-cluster", "nexusd-cluster.exe", "nexus-cluster.exe"):
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate

    for name in ("nexusd-cluster", "nexus-cluster"):
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved)
    return None


def _resolve_search_library(kernel: Path) -> Path | None:
    configured = os.environ.get("NEXUS_SEARCH_PLUGIN_LIBRARY")
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
        pytest.fail(f"NEXUS_SEARCH_PLUGIN_LIBRARY is unavailable: {configured}")
    for directory in (kernel.parent, *_build_directories()):
        for name in (
            "libnexus_search_plugin.dylib",
            "libnexus_search_plugin.so",
            "nexus_search_plugin.dll",
        ):
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def configure_search_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a real signed Search library for a test's spawned Kernel."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    kernel = resolve_kernel_binary()
    library = _resolve_search_library(kernel) if kernel else None
    if kernel is None or library is None:
        reason = "native Search E2E requires the pinned cluster binary and Search library"
        if os.environ.get("CI"):
            pytest.fail(reason)
        pytest.skip(reason)
    monkeypatch.setenv("NEXUS_KERNEL_BINARY", str(kernel))

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    plugin_copy = plugins_dir / library.name
    shutil.copy2(library, plugin_copy)
    signing_key = Ed25519PrivateKey.generate()
    plugin_copy.with_name(plugin_copy.name + ".sig").write_bytes(
        signing_key.sign(plugin_copy.read_bytes())
    )
    trust_dir = tmp_path / "trusted_keys"
    trust_dir.mkdir()
    public_key = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    (trust_dir / "live-e2e.pub").write_text(
        base64.b64encode(public_key).decode() + "\n", encoding="utf8"
    )
    monkeypatch.setenv("NEXUS_PLUGIN_DIR", str(plugins_dir))
    monkeypatch.setenv("NEXUS_LOCAL_TRUSTED_KEYS_DIR", str(trust_dir))
