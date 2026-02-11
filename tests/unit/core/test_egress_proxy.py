"""Tests for EgressProxyManager (Issue #1000).

Covers Squid config generation, proxy lifecycle, network management,
container network config, and environment variable override.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.sandbox.egress_proxy import (
    PROXY_CONTAINER_NAME,
    PROXY_NETWORK_NAME,
    PROXY_PORT,
    EgressProxyManager,
    build_squid_config,
    get_allowlist_from_env,
    validate_domain,
)

# ---------------------------------------------------------------------------
# Squid config generation
# ---------------------------------------------------------------------------


class TestBuildSquidConfig:
    def test_deny_all_when_empty(self) -> None:
        config = build_squid_config(())
        assert "http_access deny all" in config
        assert "allowed_domains" not in config

    def test_allow_all_with_wildcard(self) -> None:
        config = build_squid_config(("*",))
        assert "http_access allow all" in config

    def test_specific_domains(self) -> None:
        config = build_squid_config(("api.openai.com", "pypi.org"))
        assert ".api.openai.com" in config
        assert ".pypi.org" in config
        assert "http_access allow CONNECT SSL_ports allowed_domains" in config
        assert "http_access allow allowed_domains" in config
        assert "http_access deny all" in config

    def test_security_hardening_headers(self) -> None:
        config = build_squid_config(("example.com",))
        assert "forwarded_for delete" in config
        assert "via off" in config
        assert "X-Forwarded-For deny all" in config

    def test_port_configured(self) -> None:
        config = build_squid_config(())
        assert f"http_port {PROXY_PORT}" in config

    def test_caching_disabled(self) -> None:
        config = build_squid_config(())
        assert "cache deny all" in config

    def test_wildcard_among_others_allows_all(self) -> None:
        config = build_squid_config(("api.openai.com", "*"))
        assert "http_access allow all" in config

    def test_rejects_shell_injection_in_domain(self) -> None:
        with pytest.raises(ValueError, match="Invalid egress domain"):
            build_squid_config(("api.openai.com'; rm -rf / #",))

    def test_rejects_spaces_in_domain(self) -> None:
        with pytest.raises(ValueError, match="Invalid egress domain"):
            build_squid_config(("evil domain.com",))

    def test_rejects_semicolons(self) -> None:
        with pytest.raises(ValueError, match="Invalid egress domain"):
            build_squid_config(("evil;.com",))


# ---------------------------------------------------------------------------
# Domain validation
# ---------------------------------------------------------------------------


class TestValidateDomain:
    def test_valid_domains(self) -> None:
        assert validate_domain("api.openai.com") == "api.openai.com"
        assert validate_domain("pypi.org") == "pypi.org"
        assert validate_domain("files.pythonhosted.org") == "files.pythonhosted.org"
        assert validate_domain("a-b-c.example.co.uk") == "a-b-c.example.co.uk"

    def test_wildcard(self) -> None:
        assert validate_domain("*") == "*"

    def test_rejects_shell_metacharacters(self) -> None:
        for bad in ["'; rm -rf /", "$(whoami)", "`id`", "a&b", "a|b", "a;b"]:
            with pytest.raises(ValueError):
                validate_domain(bad)

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            validate_domain("")

    def test_rejects_spaces(self) -> None:
        with pytest.raises(ValueError):
            validate_domain("has space.com")

    def test_rejects_leading_hyphen(self) -> None:
        with pytest.raises(ValueError):
            validate_domain("-invalid.com")


# ---------------------------------------------------------------------------
# Environment variable allowlist
# ---------------------------------------------------------------------------


class TestGetAllowlistFromEnv:
    def test_returns_none_when_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert get_allowlist_from_env() is None

    def test_parses_comma_separated(self) -> None:
        with patch.dict(
            "os.environ",
            {"NEXUS_SANDBOX_EGRESS_ALLOWLIST": "a.com, b.com, c.com"},
        ):
            result = get_allowlist_from_env()
            assert result == ("a.com", "b.com", "c.com")

    def test_strips_whitespace(self) -> None:
        with patch.dict(
            "os.environ",
            {"NEXUS_SANDBOX_EGRESS_ALLOWLIST": " x.com , y.com "},
        ):
            result = get_allowlist_from_env()
            assert result == ("x.com", "y.com")

    def test_filters_empty_entries(self) -> None:
        with patch.dict(
            "os.environ",
            {"NEXUS_SANDBOX_EGRESS_ALLOWLIST": "a.com,,b.com,"},
        ):
            result = get_allowlist_from_env()
            assert result == ("a.com", "b.com")

    def test_empty_string_returns_empty_tuple(self) -> None:
        with patch.dict(
            "os.environ",
            {"NEXUS_SANDBOX_EGRESS_ALLOWLIST": ""},
        ):
            result = get_allowlist_from_env()
            assert result == ()


# ---------------------------------------------------------------------------
# EgressProxyManager lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_docker() -> MagicMock:
    """Mock Docker client with network and container support."""
    client = MagicMock()

    # Mock network operations
    mock_network = MagicMock()
    mock_network.name = PROXY_NETWORK_NAME
    client.networks.create.return_value = mock_network
    client.networks.get.side_effect = Exception("not found")

    # Mock bridge network for dual-homing
    mock_bridge = MagicMock()
    client.networks.get.side_effect = lambda name: (
        mock_bridge if name == "bridge" else (_ for _ in ()).throw(Exception("not found"))
    )

    # Mock container operations
    mock_container = MagicMock()
    mock_container.status = "running"
    client.containers.run.return_value = mock_container

    return client


@pytest.fixture()
def proxy_mgr(mock_docker: MagicMock) -> EgressProxyManager:
    return EgressProxyManager(mock_docker)


class TestEgressProxyManagerInit:
    def test_default_values(self, proxy_mgr: EgressProxyManager) -> None:
        assert proxy_mgr.network_name == PROXY_NETWORK_NAME
        assert proxy_mgr.proxy_url == f"http://{PROXY_CONTAINER_NAME}:{PROXY_PORT}"

    def test_custom_values(self, mock_docker: MagicMock) -> None:
        mgr = EgressProxyManager(
            mock_docker,
            proxy_image="custom-squid:1.0",
            network_name="custom-net",
            container_name="custom-proxy",
        )
        assert mgr.network_name == "custom-net"
        assert mgr.proxy_url == f"http://custom-proxy:{PROXY_PORT}"

    def test_not_running_initially(self, proxy_mgr: EgressProxyManager) -> None:
        assert proxy_mgr.is_running is False


class TestEnsureNetwork:
    def test_creates_internal_network(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        proxy_mgr.ensure_network()
        mock_docker.networks.create.assert_called_once_with(
            PROXY_NETWORK_NAME,
            driver="bridge",
            internal=True,
            labels={"nexus.role": "egress-proxy"},
        )

    def test_reuses_existing_network(self, mock_docker: MagicMock) -> None:
        existing_network = MagicMock()
        mock_docker.networks.get.side_effect = lambda name: (
            existing_network if name == PROXY_NETWORK_NAME else MagicMock()
        )
        mgr = EgressProxyManager(mock_docker)

        mgr.ensure_network()
        mock_docker.networks.create.assert_not_called()


class TestEnsureRunning:
    def test_starts_proxy_container(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        url = proxy_mgr.ensure_running(("api.openai.com",))

        assert url == proxy_mgr.proxy_url
        mock_docker.containers.run.assert_called_once()
        run_kwargs = mock_docker.containers.run.call_args
        assert run_kwargs.kwargs["name"] == PROXY_CONTAINER_NAME
        assert run_kwargs.kwargs["detach"] is True
        assert run_kwargs.kwargs["mem_limit"] == "128m"

    def test_connects_to_bridge_network(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        proxy_mgr.ensure_running(("api.openai.com",))

        # Should call networks.get("bridge") to connect proxy to internet
        bridge_calls = [c for c in mock_docker.networks.get.call_args_list if c.args[0] == "bridge"]
        assert len(bridge_calls) >= 1

    def test_idempotent_same_domains(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        proxy_mgr.ensure_running(("api.openai.com",))
        # Second call with same domains should not restart
        proxy_mgr.ensure_running(("api.openai.com",))

        assert mock_docker.containers.run.call_count == 1

    def test_restarts_on_different_domains(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        proxy_mgr.ensure_running(("api.openai.com",))
        proxy_mgr.ensure_running(("pypi.org",))

        # Should have started twice (stop + restart)
        assert mock_docker.containers.run.call_count == 2


class TestStop:
    def test_stops_and_removes_container(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        proxy_mgr.ensure_running(("api.openai.com",))
        container = mock_docker.containers.run.return_value

        proxy_mgr.stop()

        container.stop.assert_called_once_with(timeout=5)
        container.remove.assert_called_once_with(force=True)
        assert proxy_mgr.is_running is False

    def test_stop_when_not_running(self, proxy_mgr: EgressProxyManager) -> None:
        # Should not raise
        proxy_mgr.stop()


class TestCleanup:
    def test_stops_proxy_and_removes_network(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        proxy_mgr.ensure_running(("api.openai.com",))
        proxy_mgr.cleanup()

        container = mock_docker.containers.run.return_value
        container.stop.assert_called_once()
        container.remove.assert_called_once()


class TestGetContainerNetworkConfig:
    def test_empty_domains_returns_empty(
        self,
        proxy_mgr: EgressProxyManager,
    ) -> None:
        config = proxy_mgr.get_container_network_config(())
        assert config == {}

    def test_returns_network_and_proxy_env(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        config = proxy_mgr.get_container_network_config(("api.openai.com",))

        assert config["network"] == PROXY_NETWORK_NAME
        env = config["environment"]
        assert env["HTTP_PROXY"] == proxy_mgr.proxy_url
        assert env["HTTPS_PROXY"] == proxy_mgr.proxy_url
        assert env["http_proxy"] == proxy_mgr.proxy_url
        assert env["https_proxy"] == proxy_mgr.proxy_url
        assert env["NO_PROXY"] == "localhost,127.0.0.1"

    def test_starts_proxy_if_not_running(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        assert proxy_mgr.is_running is False

        proxy_mgr.get_container_network_config(("pypi.org",))

        # Proxy should have been started
        mock_docker.containers.run.assert_called_once()


# ---------------------------------------------------------------------------
# Per-sandbox domain merging
# ---------------------------------------------------------------------------


class TestPerSandboxDomains:
    def test_register_single_sandbox(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        proxy_mgr.register_sandbox("sb1", ("api.openai.com",))
        assert mock_docker.containers.run.call_count == 1

    def test_register_multiple_sandboxes_merges_domains(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        proxy_mgr.register_sandbox("sb1", ("api.openai.com",))
        proxy_mgr.register_sandbox("sb2", ("pypi.org",))

        # Proxy restarted with merged domains
        assert mock_docker.containers.run.call_count == 2

    def test_unregister_sandbox_updates_allowlist(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        proxy_mgr.register_sandbox("sb1", ("api.openai.com",))
        proxy_mgr.register_sandbox("sb2", ("pypi.org",))

        proxy_mgr.unregister_sandbox("sb1")
        # Proxy restarted again with only sb2 domains
        assert mock_docker.containers.run.call_count == 3

    def test_wildcard_in_any_sandbox_allows_all(
        self,
        proxy_mgr: EgressProxyManager,
    ) -> None:
        proxy_mgr._sandbox_domains = {
            "sb1": ("api.openai.com",),
            "sb2": ("*",),
        }
        assert proxy_mgr._merged_domains() == ("*",)

    def test_merged_domains_sorted(
        self,
        proxy_mgr: EgressProxyManager,
    ) -> None:
        proxy_mgr._sandbox_domains = {
            "sb1": ("pypi.org",),
            "sb2": ("api.openai.com",),
        }
        assert proxy_mgr._merged_domains() == ("api.openai.com", "pypi.org")

    def test_get_container_network_config_with_sandbox_id(
        self,
        proxy_mgr: EgressProxyManager,
        mock_docker: MagicMock,
    ) -> None:
        config = proxy_mgr.get_container_network_config(("api.openai.com",), sandbox_id="sb1")
        assert config["network"] == PROXY_NETWORK_NAME
        assert "sb1" in proxy_mgr._sandbox_domains
