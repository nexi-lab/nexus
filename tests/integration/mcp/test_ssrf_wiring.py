"""Integration test: MCP mount refuses internal / metadata URLs (Issue #3792)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.bricks.mcp.models import MCPMount
from nexus.lib.events import register_audit_sink
from nexus.lib.security.url_validator import SSRFBlocked


@pytest.fixture
def mount_manager():
    from nexus.bricks.mcp.mount import MCPMountManager

    # Validation path does not touch the filesystem — leave it unset (None).
    return MCPMountManager()


@pytest.mark.asyncio
async def test_sse_mount_rejects_metadata_url(mount_manager) -> None:
    mount = MCPMount(
        name="evil",
        description="metadata target",
        transport="sse",
        url="http://169.254.169.254/mcp",
    )

    with pytest.raises(SSRFBlocked) as excinfo:
        await mount_manager._create_sse_client(mount)

    assert excinfo.value.reason in {"cloud_metadata", "blocked_network"}


@pytest.mark.asyncio
async def test_sse_mount_rejects_rfc1918_url(mount_manager) -> None:
    mount = MCPMount(
        name="intranet",
        description="RFC1918 target",
        transport="sse",
        url="http://10.0.0.1/mcp",
    )

    with pytest.raises(SSRFBlocked):
        await mount_manager._create_sse_client(mount)


@pytest.mark.asyncio
async def test_sse_mount_emits_audit_event_on_block(mount_manager) -> None:
    captured: list[tuple[str, dict]] = []

    handle = register_audit_sink(lambda n, p: captured.append((n, p)))
    try:
        mount = MCPMount(
            name="evil",
            description="blocked",
            transport="sse",
            url="http://10.0.0.1/mcp",
        )
        with pytest.raises(SSRFBlocked):
            await mount_manager._create_sse_client(mount)
    finally:
        handle.remove()

    assert len(captured) == 1
    name, payload = captured[0]
    assert name == "security.ssrf_blocked"
    assert payload["url"] == "http://10.0.0.1/mcp"
    assert payload["integration"] == "mcp"
    assert payload["mount_name"] == "evil"
    assert payload["reason"]  # any string


@pytest.mark.asyncio
async def test_sse_mount_uses_pinned_factory_for_public_url(mount_manager) -> None:
    """For a public URL, sse_client must be called with a pinned factory."""
    mount = MCPMount(
        name="okay",
        description="public endpoint",
        transport="sse",
        url="https://example.com/mcp",
    )

    recorded: dict[str, object] = {}

    # The mcp sse_client is async-context-managed. Have the fake yield nothing
    # useful and raise to short-circuit the rest of _create_sse_client.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_sse_client(url, headers=None, httpx_client_factory=None, **kw):
        recorded["url"] = url
        recorded["factory"] = httpx_client_factory
        raise RuntimeError("short-circuit test")
        yield None  # unreachable

    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch("mcp.client.sse.sse_client", fake_sse_client),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with pytest.raises(RuntimeError, match="short-circuit"):
            await mount_manager._create_sse_client(mount)

    assert recorded["url"] == "https://example.com/mcp"
    assert recorded["factory"] is not None


@pytest.mark.asyncio
async def test_sse_mount_honors_allow_private_override() -> None:
    """When SSRFConfig.allow_private=True is threaded through the manager,
    an RFC1918 URL is permitted."""
    from nexus.bricks.mcp.mount import MCPMountManager
    from nexus.config import SSRFConfig

    manager = MCPMountManager(ssrf_config=SSRFConfig(allow_private=True))
    mount = MCPMount(
        name="intranet",
        description="permitted private",
        transport="sse",
        url="http://10.0.0.1/mcp",
    )

    # Short-circuit the sse_client call so we only exercise the validation path.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_sse_client(url, headers=None, httpx_client_factory=None, **kw):
        raise RuntimeError("short-circuit test")
        yield None  # unreachable

    with (
        patch("mcp.client.sse.sse_client", fake_sse_client),
        pytest.raises(RuntimeError, match="short-circuit"),
    ):
        await manager._create_sse_client(mount)


@pytest.mark.asyncio
async def test_sse_mount_honors_extra_deny_cidrs() -> None:
    """An operator-added CIDR in SSRFConfig.extra_deny_cidrs must block
    matching URLs even if they would otherwise pass."""
    from nexus.bricks.mcp.mount import MCPMountManager
    from nexus.config import SSRFConfig

    manager = MCPMountManager(
        ssrf_config=SSRFConfig(extra_deny_cidrs=["203.0.113.0/24"]),
    )
    mount = MCPMount(
        name="denied",
        description="operator deny",
        transport="sse",
        url="https://svcmesh.example.com/mcp",
    )

    with patch("socket.getaddrinfo") as mock_dns, pytest.raises(SSRFBlocked) as excinfo:
        mock_dns.return_value = [(2, 1, 6, "", ("203.0.113.50", 443))]
        await manager._create_sse_client(mount)

    assert excinfo.value.reason == "extra_deny_cidr"
    assert excinfo.value.cidr == "203.0.113.0/24"


@pytest.mark.asyncio
async def test_factory_boot_threads_ssrf_config_to_mcp_service() -> None:
    """Regression: operator-configured SSRFConfig must reach MCPMountManager
    through the real factory boot path (not just direct construction).

    Prior to this test, _wired.py read nx._config.security.ssrf, but
    nx._config is attached only after create_nexus_fs() returns, so the
    read resolved to None and operator config was silently dropped.
    """
    from unittest.mock import MagicMock

    from nexus.config import SecurityConfig, SSRFConfig
    from nexus.factory._wired import _boot_post_kernel_services

    nx = MagicMock()
    nx._config = None  # simulate pre-attach state (real factory behavior)
    router = MagicMock()
    router.list_mounts = MagicMock(return_value=[])

    security = SecurityConfig(ssrf=SSRFConfig(extra_deny_cidrs=["10.50.0.0/16"]))

    services: dict = {}

    def svc_on(name: str) -> bool:
        return name == "mcp"

    # _boot_post_kernel_services constructs MCPService etc.; run with a stubbed
    # MCPService so we can capture the ssrf_config kwarg it was called with.
    captured: dict = {}

    class _FakeMCPService:
        def __init__(self, *, ssrf_config=None, **kw):
            captured["ssrf_config"] = ssrf_config

    with patch("nexus.bricks.mcp.mcp_service.MCPService", _FakeMCPService):
        # Other services touched by _boot_post_kernel_services may need
        # extensive stubbing; swallow any boot-time failures and only assert
        # that MCPService received the threaded config before the failure.
        try:
            await _boot_post_kernel_services(nx, router, services, svc_on, security_config=security)
        except Exception:
            pass

    assert captured.get("ssrf_config") is not None
    assert captured["ssrf_config"].extra_deny_cidrs == ("10.50.0.0/16",)
