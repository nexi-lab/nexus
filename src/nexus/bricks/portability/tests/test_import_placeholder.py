"""Tests for restore placeholder guard."""

from nexus.bricks.portability.import_service import (
    _apply_injections,
    _scan_for_placeholders,
)


def test_scan_finds_placeholder_tokens():
    rows = [{"api_key": "${PROVIDER_KEY_anthropic}"}, {"value": "no placeholder"}]
    found = _scan_for_placeholders(rows)
    assert found == {"PROVIDER_KEY_anthropic"}


def test_apply_injections_replaces_placeholder():
    rows = [{"api_key": "${PROVIDER_KEY_anthropic}"}]
    out = _apply_injections(rows, {"PROVIDER_KEY_anthropic": "sk-ant-real"})
    assert out[0]["api_key"] == "sk-ant-real"


def test_unmatched_placeholder_raises():
    rows = [{"api_key": "${PROVIDER_KEY_anthropic}"}]
    out = _apply_injections(rows, injections={})
    remaining = _scan_for_placeholders(out)
    assert remaining == {"PROVIDER_KEY_anthropic"}


def test_partial_injection_still_raises():
    """When some are injected, the missing list is still surfaced."""
    rows = [
        {"api_key": "${PROVIDER_KEY_anthropic}"},
        {"auth_token": "${HUB_TOKEN_eng}"},
    ]
    out = _apply_injections(rows, {"PROVIDER_KEY_anthropic": "real"})
    remaining = _scan_for_placeholders(out)
    assert remaining == {"HUB_TOKEN_eng"}
