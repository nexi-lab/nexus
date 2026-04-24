"""Regression tests for the embedded-hub-mode startup rejection (#3784).

The hub deployment must run as a two-service stack (``nexusd`` RPC +
``nexus mcp serve --url …`` frontend) so that per-request bearer tokens
become the api_key of the remote ``NexusFS`` connection, letting the RPC
server's ``DatabaseAPIKeyAuth`` enforce per-token identity and zone
isolation on every tool call.

"Embedded hub mode" (``NEXUS_DATABASE_URL`` set + ``NEXUS_URL`` unset +
``--transport http``) would accept bearer tokens but run every tool call
against the ambient local ``NexusFS`` — no identity scoping, no zone
isolation in the tool path. ``_reject_embedded_hub_mode`` refuses that
configuration at startup so operators see a clear error instead of a
silently unsafe deployment.
"""

from __future__ import annotations

import click
import pytest

from nexus.cli.commands.mcp import _reject_embedded_hub_mode


class TestRejectEmbeddedHubMode:
    def test_stdio_transport_always_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stdio is single-user; hub-mode rejection only applies to http."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://x/y")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        _reject_embedded_hub_mode("stdio")  # must not raise

    def test_http_without_database_url_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plain local http mode (no hub database) is unchanged."""
        monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
        monkeypatch.delenv("NEXUS_URL", raising=False)
        _reject_embedded_hub_mode("http")  # must not raise

    def test_http_with_nexus_url_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Remote-mode MCP frontend is the supported hub deployment."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://x/y")
        monkeypatch.setenv("NEXUS_URL", "http://nexus:2026")
        _reject_embedded_hub_mode("http")  # must not raise

    def test_http_embedded_hub_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NEXUS_DATABASE_URL set + no remote URL + http → reject."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://x/y")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        # Stub profile resolution so ambient user config can't satisfy
        # the remote-URL check during the test.
        from nexus.cli import config as _config

        class _NotRemote:
            is_remote = False

        monkeypatch.setattr(_config, "resolve_connection", lambda **_: _NotRemote())
        with pytest.raises(click.ClickException) as exc_info:
            _reject_embedded_hub_mode("http")
        message = exc_info.value.message
        assert "two-service" in message.lower()
        assert "nexusd" in message.lower()
        assert "docker-compose.hub.yml" in message or "docs/hub-deploy.md" in message

    def test_remote_url_cli_flag_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--remote-url CLI flag satisfies the remote-URL check even when
        NEXUS_URL env is unset (regression for round-4 finding #3)."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://x/y")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        _reject_embedded_hub_mode("http", remote_url="http://nexus:2026")

    def test_profile_remote_url_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Active profile with a remote URL also counts as a safe shape."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://x/y")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        from nexus.cli import config as _config

        class _Remote:
            is_remote = True

        monkeypatch.setattr(_config, "resolve_connection", lambda **_: _Remote())
        _reject_embedded_hub_mode("http")

    def test_sse_transport_not_affected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only 'http' is the concerning path — sse has different defaults."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://x/y")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        _reject_embedded_hub_mode("sse")  # must not raise
