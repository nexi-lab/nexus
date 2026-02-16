"""Automated sync/async parity tests for domain clients.

Verifies that every sync domain client method has a corresponding async method
with the same name (Decision #11).

Issue #1603: Domain client parity tests.
"""

from __future__ import annotations

import pytest

from nexus.remote.domain.mcp import AsyncMCPClient, MCPClient
from nexus.remote.domain.memory import AsyncMemoryClient, MemoryClient
from nexus.remote.domain.oauth import AsyncOAuthClient, OAuthClient
from nexus.remote.domain.sandbox import AsyncSandboxClient, SandboxClient
from nexus.remote.domain.share_links import AsyncShareLinksClient, ShareLinksClient
from nexus.remote.domain.skills import AsyncSkillsClient, SkillsClient


def _public_methods(cls: type) -> set[str]:
    """Get all public callable method names from a class."""
    return {
        name for name in dir(cls) if not name.startswith("_") and callable(getattr(cls, name, None))
    }


PARITY_PAIRS = [
    ("skills", SkillsClient, AsyncSkillsClient),
    ("sandbox", SandboxClient, AsyncSandboxClient),
    ("oauth", OAuthClient, AsyncOAuthClient),
    ("mcp", MCPClient, AsyncMCPClient),
    ("share_links", ShareLinksClient, AsyncShareLinksClient),
    ("memory", MemoryClient, AsyncMemoryClient),
]


@pytest.mark.parametrize(
    "domain,sync_cls,async_cls",
    PARITY_PAIRS,
    ids=[p[0] for p in PARITY_PAIRS],
)
def test_domain_sync_async_parity(domain, sync_cls, async_cls):
    """Every sync domain client method must have an async counterpart."""
    sync_methods = _public_methods(sync_cls)
    async_methods = _public_methods(async_cls)
    assert sync_methods == async_methods, (
        f"Parity gap in {domain}: "
        f"sync-only={sync_methods - async_methods}, "
        f"async-only={async_methods - sync_methods}"
    )


def test_async_only_domains_have_methods():
    """Verify async-only domains (admin, ace, llm) have expected methods."""
    from nexus.remote.domain.ace import AsyncACEClient
    from nexus.remote.domain.admin import AsyncAdminClient
    from nexus.remote.domain.llm import AsyncLLMClient

    admin_methods = _public_methods(AsyncAdminClient)
    assert admin_methods >= {"create_key", "list_keys", "get_key", "revoke_key", "update_key"}

    ace_methods = _public_methods(AsyncACEClient)
    assert ace_methods >= {
        "start_trajectory",
        "log_step",
        "complete_trajectory",
        "add_feedback",
        "query_trajectories",
    }

    llm_methods = _public_methods(AsyncLLMClient)
    assert llm_methods >= {"read", "read_detailed", "read_stream", "create_reader"}


class TestFacadeAccessors:
    """Verify facade @cached_property accessors exist on main clients."""

    def test_sync_client_has_domain_accessors(self):
        """RemoteNexusFS should have skills, sandbox, oauth, mcp, share_links."""
        from nexus.remote.client import RemoteNexusFS

        for name in ("skills", "sandbox", "oauth", "mcp", "share_links"):
            assert hasattr(RemoteNexusFS, name), f"Missing accessor: {name}"

    def test_async_client_has_domain_accessors(self):
        """AsyncRemoteNexusFS should have all domain accessors."""
        from nexus.remote.async_client import AsyncRemoteNexusFS

        for name in (
            "skills",
            "sandbox",
            "oauth",
            "mcp",
            "share_links",
            "memory",
            "admin",
            "ace",
            "llm",
        ):
            assert hasattr(AsyncRemoteNexusFS, name), f"Missing accessor: {name}"


class TestBackwardsCompat:
    """Verify backwards-compat flat method delegation via __getattr__."""

    def test_sync_domain_method_map_coverage(self):
        """_DOMAIN_METHOD_MAP must cover all expected domain methods."""
        from nexus.remote.client import _DOMAIN_METHOD_MAP

        # Spot-check key methods
        assert _DOMAIN_METHOD_MAP["skills_create"] == ("skills", "create")
        assert _DOMAIN_METHOD_MAP["sandbox_connect"] == ("sandbox", "connect")
        assert _DOMAIN_METHOD_MAP["oauth_list_providers"] == ("oauth", "list_providers")
        assert _DOMAIN_METHOD_MAP["mcp_mount"] == ("mcp", "mount")
        assert _DOMAIN_METHOD_MAP["create_share_link"] == ("share_links", "create")

    def test_async_domain_method_map_extends_sync(self):
        """_ASYNC_DOMAIN_METHOD_MAP must include all sync entries + async-only."""
        from nexus.remote.async_client import _ASYNC_DOMAIN_METHOD_MAP
        from nexus.remote.client import _DOMAIN_METHOD_MAP

        for key, value in _DOMAIN_METHOD_MAP.items():
            assert _ASYNC_DOMAIN_METHOD_MAP[key] == value, f"Missing in async map: {key}"

        # Verify async-only entries exist
        assert "admin_create_key" in _ASYNC_DOMAIN_METHOD_MAP
        assert "ace_start_trajectory" in _ASYNC_DOMAIN_METHOD_MAP
