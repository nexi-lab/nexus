"""Unit tests for nexus.cli.commands._hub_common helpers."""

from __future__ import annotations

import pytest

from nexus.cli.commands._hub_common import (
    format_table,
    get_session_factory,
    parse_duration,
)


def test_get_session_factory_uses_nexus_database_url(monkeypatch):
    monkeypatch.setenv("NEXUS_DATABASE_URL", "sqlite:///:memory:")
    factory = get_session_factory()
    session = factory()
    assert session is not None
    session.close()


def test_get_session_factory_raises_when_unset(monkeypatch):
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="NEXUS_DATABASE_URL"):
        get_session_factory()


def test_parse_duration_days():
    from datetime import timedelta

    assert parse_duration("90d") == timedelta(days=90)


def test_parse_duration_hours():
    from datetime import timedelta

    assert parse_duration("6h") == timedelta(hours=6)


def test_parse_duration_minutes():
    from datetime import timedelta

    assert parse_duration("30m") == timedelta(minutes=30)


def test_parse_duration_invalid():
    with pytest.raises(ValueError, match="duration"):
        parse_duration("banana")


def test_format_table_renders_headers_and_rows():
    out = format_table(
        headers=["a", "b"],
        rows=[["1", "22"], ["333", "4"]],
    )
    lines = out.splitlines()
    assert lines[0].startswith("a")
    assert "333" in out
