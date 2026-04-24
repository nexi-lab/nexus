"""Regression tests for the embedded-hub-mode startup rejection (#3784)
and the fail-closed ``NEXUS_MCP_REQUIRE_BEARER`` request-time check.

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

from typing import Any

import click
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nexus.cli.commands.mcp import _APIKeyMiddleware, _reject_embedded_hub_mode


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

    def test_profile_name_threaded_to_resolve_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (round 6): ``--profile hub`` must be passed through
        to ``resolve_connection`` so a profile-only remote URL works."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://x/y")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        from nexus.cli import config as _config

        seen_kwargs: dict[str, Any] = {}

        class _Remote:
            is_remote = True

        def _stub(**kwargs: Any) -> Any:
            seen_kwargs.update(kwargs)
            return _Remote()

        monkeypatch.setattr(_config, "resolve_connection", _stub)

        # Simulate the root `nexus --profile hub` command populating
        # ctx.obj the same way it does in production.
        import click as _click

        @_click.command()
        @_click.pass_context
        def _fake(ctx: _click.Context) -> None:
            ctx.ensure_object(dict)
            ctx.obj["profile"] = "hub"
            _reject_embedded_hub_mode("http")

        from click.testing import CliRunner

        result = CliRunner().invoke(_fake, [], standalone_mode=False)
        assert result.exception is None, result.output
        assert seen_kwargs.get("profile_name") == "hub"

    def test_sse_transport_not_affected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only 'http' is the concerning path — sse has different defaults."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://x/y")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        _reject_embedded_hub_mode("sse")  # must not raise


async def _ok(_request: Any) -> Any:
    return PlainTextResponse("ok")


async def _health(_request: Any) -> Any:
    return PlainTextResponse("healthy")


def _client() -> TestClient:
    app = Starlette(
        routes=[Route("/mcp", _ok, methods=["POST"]), Route("/health", _health)],
    )
    app.add_middleware(_APIKeyMiddleware)
    return TestClient(app)


class TestRequireBearerGate:
    """NEXUS_MCP_REQUIRE_BEARER=true gates requests at the APIKey middleware
    so the tool layer can't fall back to an ambient _default_nx connection
    seeded with NEXUS_API_KEY / profile credentials (#3784 round 5)."""

    def test_missing_bearer_allowed_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_MCP_REQUIRE_BEARER", raising=False)
        resp = _client().post("/mcp")
        assert resp.status_code == 200

    def test_missing_bearer_rejected_when_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_MCP_REQUIRE_BEARER", "true")
        resp = _client().post("/mcp")
        assert resp.status_code == 401
        assert "missing_bearer_token" in resp.text
        assert resp.headers["WWW-Authenticate"].startswith("Bearer")

    def test_valid_bearer_passes_when_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_MCP_REQUIRE_BEARER", "true")
        resp = _client().post("/mcp", headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200

    def test_x_nexus_api_key_passes_when_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_MCP_REQUIRE_BEARER", "true")
        resp = _client().post("/mcp", headers={"X-Nexus-API-Key": "sk-good"})
        assert resp.status_code == 200

    def test_health_always_passes_without_bearer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Container healthchecks must succeed even in fail-closed mode."""
        monkeypatch.setenv("NEXUS_MCP_REQUIRE_BEARER", "true")
        resp = _client().get("/health")
        assert resp.status_code == 200

    def test_false_values_disable_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("false", "0", "no", "", "off"):
            monkeypatch.setenv("NEXUS_MCP_REQUIRE_BEARER", val)
            resp = _client().post("/mcp")
            assert resp.status_code == 200, f"val={val!r} should not gate"
