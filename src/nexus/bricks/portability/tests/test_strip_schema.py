"""Tests for schema-aware credential stripper."""

from nexus.bricks.portability.models import PlaceholderRef
from nexus.bricks.portability.strip import SchemaStripper


def test_strips_provider_api_key():
    stripper = SchemaStripper()
    rows = [{"name": "anthropic", "api_key": "sk-ant-real-secret"}]
    result = stripper.strip_table("providers", rows)
    assert result.rows[0]["api_key"] == "${PROVIDER_KEY_anthropic}"
    assert (
        PlaceholderRef(name="PROVIDER_KEY_anthropic", field="providers.anthropic.api_key")
        in result.placeholders
    )


def test_strips_federation_auth_token():
    stripper = SchemaStripper()
    rows = [{"name": "eng_hub", "auth_token": "tok-secret", "url": "https://hub"}]
    result = stripper.strip_table("federations", rows)
    assert result.rows[0]["auth_token"] == "${HUB_TOKEN_eng_hub}"
    assert result.rows[0]["url"] == "https://hub"


def test_strips_webhook_secret():
    stripper = SchemaStripper()
    rows = [{"name": "ci", "secret": "whsec_xyz"}]
    result = stripper.strip_table("webhooks", rows)
    assert result.rows[0]["secret"] == "${WEBHOOK_SECRET_ci}"


def test_strips_workspace_path():
    stripper = SchemaStripper(workspace_root="/Users/alice/projects")
    rows = [{"path": "/Users/alice/projects/myapp/file.py"}]
    result = stripper.strip_table("documents", rows)
    assert result.rows[0]["path"] == "${WORKSPACE_ROOT}/myapp/file.py"


def test_passes_through_unknown_table():
    stripper = SchemaStripper()
    rows = [{"data": "anything"}]
    result = stripper.strip_table("random_unknown", rows)
    assert result.rows == rows
    assert result.placeholders == []


def test_handles_null_sensitive_field():
    stripper = SchemaStripper()
    rows = [{"name": "anthropic", "api_key": None}]
    result = stripper.strip_table("providers", rows)
    assert result.rows[0]["api_key"] is None
    assert result.placeholders == []
