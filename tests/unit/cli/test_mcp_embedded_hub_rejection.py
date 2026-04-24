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

    def test_sse_transport_rejected_same_as_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression (round 10): SSE is also a network transport and must
        be refused in embedded hub mode for the same reason as HTTP —
        bearer tokens would be accepted but tool calls would fall back to
        ambient ``_default_nx``."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://x/y")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        from nexus.cli import config as _config

        class _NotRemote:
            is_remote = False

        monkeypatch.setattr(_config, "resolve_connection", lambda **_: _NotRemote())
        with pytest.raises(click.ClickException):
            _reject_embedded_hub_mode("sse")


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


class TestResolvedRemoteUrlPropagation:
    """Regression (round 7): the URL resolved via ``resolve_connection``
    (profile / env / flag) must be passed into ``create_mcp_server`` as
    ``remote_url=…`` so per-request bearer tokens actually open per-request
    remote connections instead of falling through to the ambient
    ``_default_nx``.
    """

    def test_profile_only_url_reaches_create_mcp_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        # Scrub env so profile resolution is the only source.
        monkeypatch.delenv("NEXUS_URL", raising=False)
        monkeypatch.delenv("NEXUS_API_KEY", raising=False)

        # Stub resolve_connection to simulate a profile that yields a
        # remote URL.
        from nexus.cli import config as _config

        captured: dict[str, Any] = {}

        class _Resolved:
            is_remote = True
            url = "http://nexus:2026"
            api_key = "sk-from-profile"

        monkeypatch.setattr(_config, "resolve_connection", lambda **_: _Resolved())

        # Stub asyncio.run and create_mcp_server so we exercise the
        # resolution path without actually starting a server.
        from nexus.cli.commands import mcp as _mcp_mod

        def _fake_asyncio_run(coro: Any) -> Any:
            # Let the coroutine run to the point where it calls
            # create_mcp_server — we've stubbed that to record the kwargs.
            import asyncio as _asyncio

            return _asyncio.new_event_loop().run_until_complete(coro)

        async def _fake_get_filesystem(url: str | None, key: str | None, **_: Any) -> Any:
            return None  # create_mcp_server will be stubbed before using nx

        async def _fake_create_mcp_server(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return None  # return None so _async_serve short-circuits

        monkeypatch.setattr(_mcp_mod, "get_filesystem", _fake_get_filesystem)
        # create_mcp_server is imported inside _async_serve via
        # `from nexus.bricks.mcp import create_mcp_server`, so patch at
        # the source.
        from nexus.bricks import mcp as _bricks_mcp

        monkeypatch.setattr(_bricks_mcp, "create_mcp_server", _fake_create_mcp_server)

        # Invoke the serve command with stdio transport (no http run,
        # no port open) but still exercise the resolution path.
        @click_command_wrapper()
        def _driver() -> None:
            from nexus.cli.commands.mcp import serve

            serve.callback(
                transport="stdio",
                host="127.0.0.1",
                port=8081,
                api_key=None,
                remote_url=None,
                remote_api_key=None,
            )

        result = CliRunner().invoke(_driver, ["--profile", "hub"], standalone_mode=False)
        assert result.exception is None, result.output
        assert captured.get("remote_url") == "http://nexus:2026"


def click_command_wrapper():  # helper for the profile-threading test
    import click as _click

    def _decorator(fn: Any) -> Any:
        @_click.command()
        @_click.option("--profile", default=None)
        @_click.pass_context
        def _cmd(ctx: _click.Context, profile: str | None) -> None:
            ctx.ensure_object(dict)
            ctx.obj["profile"] = profile
            fn()

        return _cmd

    return _decorator


class TestAutoPromoteRequireBearer:
    """Regression (round 8): when transport=http resolves BOTH a remote URL
    AND an ambient api_key (CLI flag / env / profile), the MCP server's
    ``_default_nx`` is seeded with that key — so missing bearer tokens
    silently execute as the profile identity. Auto-promote
    ``NEXUS_MCP_REQUIRE_BEARER=true`` for that shape."""

    def _stub_serve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stub _async_serve to short-circuit serve() before it opens a port."""
        from nexus.cli.commands import mcp as _mcp_mod

        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(_mcp_mod, "_async_serve", _noop)

    def test_auto_promote_when_profile_yields_remote_and_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NEXUS_MCP_REQUIRE_BEARER", raising=False)
        monkeypatch.delenv("NEXUS_MCP_ALLOW_AMBIENT_KEY", raising=False)
        monkeypatch.delenv("NEXUS_URL", raising=False)
        monkeypatch.delenv("NEXUS_API_KEY", raising=False)

        from nexus.cli import config as _config

        class _Resolved:
            is_remote = True
            url = "http://nexus:2026"
            api_key = "sk-ambient"

        monkeypatch.setattr(_config, "resolve_connection", lambda **_: _Resolved())
        self._stub_serve(monkeypatch)

        from nexus.cli.commands.mcp import serve

        serve.callback(
            transport="http",
            host="127.0.0.1",
            port=8081,
            api_key=None,
            remote_url=None,
            remote_api_key=None,
        )
        import os as _os

        assert _os.environ.get("NEXUS_MCP_REQUIRE_BEARER") == "true"

    def test_opt_out_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NEXUS_MCP_ALLOW_AMBIENT_KEY=true disables the auto-promote."""
        monkeypatch.delenv("NEXUS_MCP_REQUIRE_BEARER", raising=False)
        monkeypatch.setenv("NEXUS_MCP_ALLOW_AMBIENT_KEY", "true")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        monkeypatch.delenv("NEXUS_API_KEY", raising=False)

        from nexus.cli import config as _config

        class _Resolved:
            is_remote = True
            url = "http://nexus:2026"
            api_key = "sk-ambient"

        monkeypatch.setattr(_config, "resolve_connection", lambda **_: _Resolved())
        self._stub_serve(monkeypatch)

        from nexus.cli.commands.mcp import serve

        serve.callback(
            transport="http",
            host="127.0.0.1",
            port=8081,
            api_key=None,
            remote_url=None,
            remote_api_key=None,
        )
        import os as _os

        assert _os.environ.get("NEXUS_MCP_REQUIRE_BEARER") is None

    def test_auto_promote_covers_sse_transport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression (round 10): SSE is also a network transport with the
        same ambient-key fallback shape — the auto-promote must cover it
        (not only http)."""
        monkeypatch.delenv("NEXUS_MCP_REQUIRE_BEARER", raising=False)
        monkeypatch.delenv("NEXUS_MCP_ALLOW_AMBIENT_KEY", raising=False)
        monkeypatch.delenv("NEXUS_URL", raising=False)
        monkeypatch.delenv("NEXUS_API_KEY", raising=False)

        from nexus.cli import config as _config

        class _Resolved:
            is_remote = True
            url = "http://nexus:2026"
            api_key = "sk-ambient"

        monkeypatch.setattr(_config, "resolve_connection", lambda **_: _Resolved())
        self._stub_serve(monkeypatch)

        from nexus.cli.commands.mcp import serve

        serve.callback(
            transport="sse",
            host="127.0.0.1",
            port=8081,
            api_key=None,
            remote_url=None,
            remote_api_key=None,
        )
        import os as _os

        assert _os.environ.get("NEXUS_MCP_REQUIRE_BEARER") == "true"

    def test_no_promote_for_stdio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stdio is single-user; auto-promote should not kick in."""
        monkeypatch.delenv("NEXUS_MCP_REQUIRE_BEARER", raising=False)
        monkeypatch.delenv("NEXUS_MCP_ALLOW_AMBIENT_KEY", raising=False)

        from nexus.cli import config as _config

        class _Resolved:
            is_remote = True
            url = "http://nexus:2026"
            api_key = "sk-ambient"

        monkeypatch.setattr(_config, "resolve_connection", lambda **_: _Resolved())
        self._stub_serve(monkeypatch)

        from nexus.cli.commands.mcp import serve

        serve.callback(
            transport="stdio",
            host="127.0.0.1",
            port=8081,
            api_key=None,
            remote_url=None,
            remote_api_key=None,
        )
        import os as _os

        assert _os.environ.get("NEXUS_MCP_REQUIRE_BEARER") is None


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
