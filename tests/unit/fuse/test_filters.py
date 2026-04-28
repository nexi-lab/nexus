"""Tests for FUSE path filtering helpers."""

import builtins
import importlib
import sys
import types


def test_filters_import_and_fallback_without_rust_kernel(monkeypatch) -> None:
    real_import = builtins.__import__
    existing_kernel = sys.modules.get("nexus_kernel")
    sys.modules["nexus_kernel"] = types.ModuleType("nexus_kernel")

    def fake_import(name, *args, **kwargs):
        if name == "nexus_kernel":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("nexus.fuse.filters", None)
    sys.modules.pop("nexus_kernel", None)
    try:
        filters = importlib.import_module("nexus.fuse.filters")

        assert filters.RUST_AVAILABLE is False
        assert filters.is_os_metadata_file(".DS_Store")
        assert filters.filter_os_metadata(["keep.txt", ".DS_Store", "._keep.txt"]) == ["keep.txt"]
    finally:
        sys.modules.pop("nexus.fuse.filters", None)
        if existing_kernel is not None:
            sys.modules["nexus_kernel"] = existing_kernel
        else:
            sys.modules.pop("nexus_kernel", None)
