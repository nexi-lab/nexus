"""Tests for the `nexus extensions` CLI."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.cli.commands.extensions import extensions as extensions_cmd
from nexus.extensions.store import reset_store


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_store()
    yield
    reset_store()


def _seed(monkeypatch, manifests):
    from nexus.extensions import store as store_mod

    fake = store_mod.ManifestStore()
    for m in manifests:
        fake._register(m, source="test")
    monkeypatch.setattr(store_mod, "_STORE", fake)


def test_list_table(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    result = CliRunner().invoke(extensions_cmd, ["list"])
    assert result.exit_code == 0
    assert "hn" in result.output
    assert "search" in result.output
    assert "koi" in result.output


def test_list_json_filtered_by_kind(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    result = CliRunner().invoke(extensions_cmd, ["list", "--kind", "connector", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [m["name"] for m in payload] == ["hn"]


def test_info_disambiguates(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    result = CliRunner().invoke(extensions_cmd, ["info", "hn", "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["name"] == "hn"


def test_info_unknown_returns_error(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    result = CliRunner().invoke(extensions_cmd, ["info", "ghost"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_check_reports_status(monkeypatch):
    from nexus.extensions.manifest import PluginManifest

    _seed(
        monkeypatch,
        [PluginManifest(name="ok", module="m", factory="F", import_probes=("sys",))],
    )
    result = CliRunner().invoke(extensions_cmd, ["check", "ok"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["available"] is True


def test_kinds_lists_all():
    result = CliRunner().invoke(extensions_cmd, ["kinds"])
    assert result.exit_code == 0
    assert "connector" in result.output
    assert "brick" in result.output
    assert "plugin" in result.output


def test_available_only_filter(monkeypatch):
    from nexus.extensions.manifest import PluginManifest

    _seed(
        monkeypatch,
        [
            PluginManifest(name="ok", module="m", factory="F", import_probes=("sys",)),
            PluginManifest(
                name="missing",
                module="m",
                factory="F",
                import_probes=("nonexistent_xyz",),
            ),
        ],
    )
    result = CliRunner().invoke(extensions_cmd, ["list", "--available-only", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    names = {m["name"] for m in payload}
    assert names == {"ok"}
