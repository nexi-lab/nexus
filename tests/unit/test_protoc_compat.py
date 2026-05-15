"""Tests for the protoc compatibility wrapper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_protoc_compat() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "scripts" / "protoc-compat.py"
    spec = importlib.util.spec_from_file_location("protoc_compat", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_protoc_prefers_vendored_before_path(monkeypatch, tmp_path):
    module = _load_protoc_compat()
    cargo_home = tmp_path / "cargo"
    vendored = (
        cargo_home
        / "registry"
        / "src"
        / "index"
        / "protoc-bin-vendored-linux-x86_64-3.2.0"
        / "bin"
        / "protoc"
    )
    vendored.parent.mkdir(parents=True)
    vendored.write_text("#!/bin/sh\n")

    path_bin = tmp_path / "bin"
    path_bin.mkdir()
    system_protoc = path_bin / "protoc"
    system_protoc.write_text("#!/bin/sh\n")

    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    monkeypatch.setenv("PATH", str(path_bin))
    monkeypatch.delenv("NEXUS_REAL_PROTOC", raising=False)
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")

    assert module._real_protoc() == str(vendored)
