"""Tests for export-time credential stripping wiring."""

from nexus.bricks.portability.export_service import (
    _apply_credential_stripping,
)
from nexus.bricks.portability.models import PlaceholderRef


def test_apply_strip_replaces_provider_key():
    rows_by_table = {
        "providers": [{"name": "anthropic", "api_key": "sk-ant-secret"}],
        "federations": [{"name": "eng", "auth_token": "tok"}],
    }
    out_rows, placeholders = _apply_credential_stripping(rows_by_table, workspace_root=None)
    assert out_rows["providers"][0]["api_key"] == "${PROVIDER_KEY_anthropic}"
    assert out_rows["federations"][0]["auth_token"] == "${HUB_TOKEN_eng}"
    assert PlaceholderRef("PROVIDER_KEY_anthropic", "providers.anthropic.api_key") in placeholders
    assert PlaceholderRef("HUB_TOKEN_eng", "federations.eng.auth_token") in placeholders


def test_apply_strip_runs_regex_backstop_on_documents():
    rows_by_table = {
        "documents": [
            {"path": "/x", "body": "Token is sk-ant-aaaaaaaaaaaaaaaaaaaa here"},
        ],
    }
    out_rows, _placeholders = _apply_credential_stripping(rows_by_table, workspace_root=None)
    assert "sk-ant-aaaaaaaaaaaaaaaaaaaa" not in out_rows["documents"][0]["body"]
    assert "***REDACTED***" in out_rows["documents"][0]["body"]
