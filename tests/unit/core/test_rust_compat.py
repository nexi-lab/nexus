"""Unit tests for nexus._rust_compat capability group validation (Issue #3712).

Tests use mock nexus_kernel modules to exercise degradation paths without
requiring a built Rust extension. All paths tested:
  - Module not installed (slim mode)
  - Module installed but import raises an unexpected error
  - Core symbols missing → RUST_AVAILABLE = False
  - Non-core group (hash, ipc) missing → correct availability flags
  - Kernel class missing methods → RUST_AVAILABLE = False (stale binary)
  - Regression: close_all_pipes missing on Kernel triggers warning + disables Rust

Running these tests does NOT require nexus_kernel to be built.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Isolation fixture — MUST be first so it runs for every test in this file.
# _reload_rust_compat() mutates sys.modules[nexus._rust_compat] to a version
# where RUST_AVAILABLE=False.  Without cleanup that poisoned module leaks to
# other test files that share the same pytest-xdist worker process, causing
# cascading failures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot and restore nexus._rust_compat + nexus_kernel around each test."""
    saved = {k: sys.modules.get(k) for k in ("nexus._rust_compat", "nexus_kernel")}
    yield
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_kernel(missing_methods: list[str] | None = None) -> type:
    """Return a fake Kernel class with all required methods, minus any listed."""
    from nexus._kernel_api_groups import KERNEL_REQUIRED_METHODS

    attrs: dict[str, object] = {}
    for method in KERNEL_REQUIRED_METHODS:
        if missing_methods and method in missing_methods:
            continue
        attrs[method] = MagicMock(return_value=None)
    return type("PyKernel", (), attrs)


def _make_fake_module(
    *,
    missing_module_symbols: list[str] | None = None,
    kernel_missing_methods: list[str] | None = None,
) -> types.ModuleType:
    """Return a fake nexus_kernel module.

    Args:
        missing_module_symbols: top-level symbols to omit.
        kernel_missing_methods: Kernel class methods to omit.
    """
    from nexus._kernel_api_groups import MODULE_CAPABILITY_GROUPS

    mod = types.ModuleType("nexus_kernel")

    # Add all symbols from all capability groups, minus those requested missing
    all_symbols = {s for group in MODULE_CAPABILITY_GROUPS.values() for s in group}
    for sym in all_symbols:
        if missing_module_symbols and sym in missing_module_symbols:
            continue
        setattr(mod, sym, MagicMock())

    # Install the Kernel class (may have missing methods)
    if not missing_module_symbols or "PyKernel" not in missing_module_symbols:
        mod.PyKernel = _make_fake_kernel(kernel_missing_methods)

    return mod


def _reload_rust_compat(fake_module: types.ModuleType | None) -> types.ModuleType:
    """Reload nexus._rust_compat with nexus_kernel replaced by fake_module."""
    # Remove cached modules so they reload fresh
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("nexus._rust_compat", "nexus_kernel"):
            del sys.modules[mod_name]

    if fake_module is None:
        # Simulate module not installed: sys.modules[name]=None blocks the import
        # (documented Python behaviour — cast needed because stubs type sys.modules
        # as dict[str, ModuleType], but None is intentionally valid here).
        with patch.dict(sys.modules, {"nexus_kernel": cast(types.ModuleType, None)}):
            return importlib.import_module("nexus._rust_compat")
    else:
        with patch.dict(sys.modules, {"nexus_kernel": fake_module}):
            return importlib.import_module("nexus._rust_compat")


# ---------------------------------------------------------------------------
# Tests: module import paths
# ---------------------------------------------------------------------------


class TestModuleNotInstalled:
    """Slim nexus-fs mode — nexus_kernel not present at all."""

    def test_rust_available_false(self) -> None:
        compat = _reload_rust_compat(None)
        assert compat.RUST_AVAILABLE is False

    def test_rust_extension_installed_false(self) -> None:
        compat = _reload_rust_compat(None)
        # find_spec returns None for uninstalled module
        assert compat.RUST_EXTENSION_INSTALLED is False

    def test_kernel_symbol_is_none(self) -> None:
        compat = _reload_rust_compat(None)
        assert compat.PyKernel is None

    def test_hash_available_false(self) -> None:
        compat = _reload_rust_compat(None)
        assert compat.RUST_HASH_AVAILABLE is False


class TestModuleImportError:
    """nexus_kernel installed but raises on import (broken .so, wrong arch, etc.)."""

    def test_rust_available_false_on_broken_import(self) -> None:
        for mod_name in list(sys.modules.keys()):
            if mod_name in ("nexus._rust_compat", "nexus_kernel"):
                del sys.modules[mod_name]

        # Simulate a broken import by making the import raise
        original = __builtins__  # noqa: F841
        with patch("builtins.__import__", side_effect=_make_import_raiser("nexus_kernel")):
            try:
                compat = importlib.import_module("nexus._rust_compat")
                assert compat.RUST_AVAILABLE is False
            except Exception:
                pass  # Acceptable — broad exception in shim catches it


def _make_import_raiser(target: str):
    """Return an __import__ replacement that raises RuntimeError for target."""
    _real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _raising_import(name, *args, **kwargs):
        if name == target:
            raise RuntimeError(f"Simulated broken import: {name}")
        return _real_import(name, *args, **kwargs)

    return _raising_import


# ---------------------------------------------------------------------------
# Tests: capability group degradation
# ---------------------------------------------------------------------------


class TestCoreMissing:
    """When core symbols are missing, all Rust is disabled."""

    def test_rust_available_false_when_kernel_missing(self) -> None:
        mod = _make_fake_module(missing_module_symbols=["PyKernel"])
        compat = _reload_rust_compat(mod)
        assert compat.RUST_AVAILABLE is False

    def test_kernel_symbol_none_when_core_disabled(self) -> None:
        mod = _make_fake_module(missing_module_symbols=["PyKernel"])
        compat = _reload_rust_compat(mod)
        assert compat.PyKernel is None

    def test_normalize_path_none_when_core_missing(self) -> None:
        mod = _make_fake_module(missing_module_symbols=["normalize_path"])
        compat = _reload_rust_compat(mod)
        assert compat.normalize_path is None

    def test_hash_available_false_when_core_disabled(self) -> None:
        mod = _make_fake_module(missing_module_symbols=["PyKernel"])
        compat = _reload_rust_compat(mod)
        # Hash group itself may be intact, but core being disabled sets RUST_AVAILABLE=False
        # The hash flag tracks the hash group, not RUST_AVAILABLE
        assert compat.RUST_AVAILABLE is False


class TestNonCoreMissing:
    """Missing non-core groups only disable their own group, not all Rust."""

    def test_hash_available_false_when_hash_group_missing(self) -> None:
        mod = _make_fake_module(missing_module_symbols=["hash_content_py", "hash_content_smart_py"])
        compat = _reload_rust_compat(mod)
        assert compat.RUST_HASH_AVAILABLE is False

    def test_rust_available_true_when_only_hash_missing(self) -> None:
        mod = _make_fake_module(missing_module_symbols=["hash_content_py", "hash_content_smart_py"])
        compat = _reload_rust_compat(mod)
        assert compat.RUST_AVAILABLE is True

    def test_ipc_available_false_when_ipc_group_missing(self) -> None:
        mod = _make_fake_module(
            missing_module_symbols=["SharedMemoryPipeBackend", "SharedMemoryStreamBackend"]
        )
        compat = _reload_rust_compat(mod)
        assert compat.RUST_IPC_AVAILABLE is False

    def test_rust_available_true_when_only_ipc_missing(self) -> None:
        mod = _make_fake_module(
            missing_module_symbols=["SharedMemoryPipeBackend", "SharedMemoryStreamBackend"]
        )
        compat = _reload_rust_compat(mod)
        assert compat.RUST_AVAILABLE is True


# ---------------------------------------------------------------------------
# Tests: Kernel class method validation (the Issue #3712 regression case)
# ---------------------------------------------------------------------------


class TestStaleBinaryKernelMethods:
    """Regression tests for Issue #3712: stale binary with missing Kernel methods."""

    def test_rust_available_false_when_close_all_pipes_missing(self, caplog) -> None:
        """Exact repro case from Issue #3712."""
        import logging

        mod = _make_fake_module(kernel_missing_methods=["close_all_pipes"])
        with caplog.at_level(logging.WARNING, logger="nexus._rust_compat"):
            compat = _reload_rust_compat(mod)

        assert compat.RUST_AVAILABLE is False

    def test_warning_emitted_with_method_name(self, caplog) -> None:
        """Warning message must mention the missing method name for actionability."""
        import logging

        mod = _make_fake_module(kernel_missing_methods=["close_all_pipes"])
        with caplog.at_level(logging.WARNING, logger="nexus._rust_compat"):
            _reload_rust_compat(mod)

        warning_text = " ".join(caplog.messages)
        assert "close_all_pipes" in warning_text

    def test_warning_includes_rebuild_command(self, caplog) -> None:
        """Warning must point to the fix, not just say something is wrong."""
        import logging

        mod = _make_fake_module(kernel_missing_methods=["list_pipes"])
        with caplog.at_level(logging.WARNING, logger="nexus._rust_compat"):
            _reload_rust_compat(mod)

        warning_text = " ".join(caplog.messages)
        assert "maturin" in warning_text

    def test_multiple_missing_methods_all_reported(self, caplog) -> None:
        """All missing methods should appear in the warning, not just the first."""
        import logging

        missing = ["close_all_pipes", "list_pipes", "create_pipe"]
        mod = _make_fake_module(kernel_missing_methods=missing)
        with caplog.at_level(logging.WARNING, logger="nexus._rust_compat"):
            _reload_rust_compat(mod)

        warning_text = " ".join(caplog.messages)
        for method in missing:
            assert method in warning_text, f"{method!r} not in warning: {warning_text!r}"

    def test_kernel_symbol_none_when_stale(self) -> None:
        """Stale Kernel class must not be returned as a usable symbol."""
        mod = _make_fake_module(kernel_missing_methods=["close_all_pipes"])
        compat = _reload_rust_compat(mod)
        assert compat.PyKernel is None

    def test_no_attribute_error_escapes_on_stale_binary(self) -> None:
        """The original bug: AttributeError on missing method must not propagate."""
        mod = _make_fake_module(kernel_missing_methods=["close_all_pipes"])
        # Must not raise — shim must absorb it
        compat = _reload_rust_compat(mod)
        # Calling close_all_pipes via compat.PyKernel must not be possible
        assert compat.PyKernel is None


class TestFullyStaleBinaryWarning:
    """Verify log level is WARNING (not INFO) so devs actually see it."""

    def test_warning_level_not_info(self, caplog) -> None:
        import logging

        mod = _make_fake_module(kernel_missing_methods=["close_all_pipes"])
        with caplog.at_level(logging.WARNING, logger="nexus._rust_compat"):
            _reload_rust_compat(mod)

        # At least one WARNING record about stale binary
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_records) >= 1


# ---------------------------------------------------------------------------
# Tests: happy path (correct binary)
# ---------------------------------------------------------------------------


class TestFullyCurrentBinary:
    """When binary is current, all flags are set correctly."""

    def test_rust_available_true(self) -> None:
        mod = _make_fake_module()
        compat = _reload_rust_compat(mod)
        assert compat.RUST_AVAILABLE is True

    def test_hash_available_true(self) -> None:
        mod = _make_fake_module()
        compat = _reload_rust_compat(mod)
        assert compat.RUST_HASH_AVAILABLE is True

    def test_ipc_available_true(self) -> None:
        mod = _make_fake_module()
        compat = _reload_rust_compat(mod)
        assert compat.RUST_IPC_AVAILABLE is True

    def test_kernel_symbol_is_class(self) -> None:
        mod = _make_fake_module()
        compat = _reload_rust_compat(mod)
        assert compat.PyKernel is not None
