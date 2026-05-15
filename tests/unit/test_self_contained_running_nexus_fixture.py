from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_self_contained_conftest():
    path = Path(__file__).resolve().parents[1] / "e2e" / "self_contained" / "conftest.py"
    spec = importlib.util.spec_from_file_location("self_contained_conftest_under_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_running_nexus_ci_skip_build_uses_prebuilt_image(monkeypatch):
    conftest = _load_self_contained_conftest()
    cfg = {
        "image_ref": "ghcr.io/nexi-lab/nexus:edge",
        "image_channel": "edge",
        "services": ["nexus", "postgres", "dragonfly"],
        "compose_profiles": ["core", "cache"],
    }

    monkeypatch.setenv("NEXUS_E2E_SKIP_BUILD", "1")

    normalized = conftest._normalize_running_nexus_config(cfg, "sk-e2e-test")

    assert normalized["image_ref"] == "nexus-server:latest"
    assert normalized["image_pin"] == "tag"
    assert "image_channel" not in normalized
    assert conftest._running_nexus_up_cmd("/venv/bin/nexus") == [
        "/venv/bin/nexus",
        "up",
        "--no-build",
    ]
