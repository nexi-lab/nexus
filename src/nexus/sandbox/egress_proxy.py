"""Egress proxy manager for sandbox network isolation.

Manages a shared Squid proxy container that mediates outbound network
access for sandboxed agents. Agent containers that need egress access
join an internal Docker network and use the proxy for all outbound requests.

Architecture::

    [Agent Container] --internal net--> [Squid Proxy] --bridge--> Internet

The internal network has no direct internet access (``internal=True``).
Only the proxy container is dual-homed (internal + default bridge),
so it can route traffic out while agents cannot bypass the proxy.

Issue #1000: Enhance agent sandboxing with network isolation.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Domain name validation: alphanumeric, hyphens, dots only
_DOMAIN_PATTERN = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*$"
)


def validate_domain(domain: str) -> str:
    """Validate a domain name against injection attacks.

    Args:
        domain: Domain string to validate.

    Returns:
        The validated domain.

    Raises:
        ValueError: If the domain contains invalid characters.
    """
    if domain == "*":
        return domain
    if not _DOMAIN_PATTERN.match(domain):
        raise ValueError(f"Invalid egress domain: {domain!r}")
    return domain


# Default proxy image (lightweight Squid)
DEFAULT_PROXY_IMAGE = "ubuntu/squid:latest"

# Container and network names
PROXY_CONTAINER_NAME = "nexus-egress-proxy"
PROXY_NETWORK_NAME = "nexus-egress"

# Default proxy port inside the container
PROXY_PORT = 3128

# Environment variable for custom egress allowlist
EGRESS_ALLOWLIST_ENV = "NEXUS_SANDBOX_EGRESS_ALLOWLIST"


def build_squid_config(allowed_domains: tuple[str, ...]) -> str:
    """Build Squid configuration with domain allowlist.

    All domains are validated before being included in the config
    to prevent shell injection or Squid config injection.

    Args:
        allowed_domains: Tuple of allowed domains. Use ``("*",)`` for all.

    Returns:
        Squid configuration as string.

    Raises:
        ValueError: If any domain contains invalid characters.
    """
    # Validate all domains before building config
    for domain in allowed_domains:
        validate_domain(domain)

    lines = [
        "# Auto-generated Squid config for Nexus egress proxy",
        "",
        f"http_port {PROXY_PORT}",
        "",
        "# Disable caching (filter only, not a cache)",
        "cache deny all",
        "",
    ]

    if "*" in allowed_domains:
        lines.extend(
            [
                "# All egress allowed",
                "http_access allow all",
            ]
        )
    elif allowed_domains:
        # Build domain ACL — Squid matches .domain.com for subdomains
        domain_list = " ".join(f".{d}" for d in allowed_domains)
        lines.extend(
            [
                "# Domain allowlist",
                f"acl allowed_domains dstdomain {domain_list}",
                "",
                "# Allow CONNECT (HTTPS) to allowed domains only",
                "acl SSL_ports port 443",
                "acl CONNECT method CONNECT",
                "http_access allow CONNECT SSL_ports allowed_domains",
                "",
                "# Allow HTTP to allowed domains",
                "http_access allow allowed_domains",
                "",
                "# Deny everything else",
                "http_access deny all",
            ]
        )
    else:
        lines.extend(
            [
                "# No egress allowed",
                "http_access deny all",
            ]
        )

    lines.extend(
        [
            "",
            "# Security hardening — strip client identity headers",
            "forwarded_for delete",
            "via off",
            "request_header_access X-Forwarded-For deny all",
            "",
        ]
    )

    return "\n".join(lines) + "\n"


def get_allowlist_from_env() -> tuple[str, ...] | None:
    """Read egress allowlist override from environment.

    Returns:
        Tuple of validated domain strings, or None if env var is not set.

    Raises:
        ValueError: If any domain in the env var is invalid.
    """
    raw = os.environ.get(EGRESS_ALLOWLIST_ENV)
    if raw is None:
        return None
    domains = tuple(validate_domain(d.strip()) for d in raw.split(",") if d.strip())
    if domains and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Egress allowlist from env: %s",
            ", ".join(domains),
        )
    return domains


class EgressProxyManager:
    """Manages a shared egress proxy container for sandbox network isolation.

    The proxy runs as a Docker container on a dedicated internal network.
    Sandbox containers that need egress access join this network and use
    the proxy (``HTTP_PROXY``/``HTTPS_PROXY``) for all outbound requests.

    Usage::

        proxy_mgr = EgressProxyManager(docker_client)
        config = proxy_mgr.get_container_network_config(
            allowed_domains=("api.openai.com", "pypi.org"),
        )
        # Merge config into container's docker_kwargs before creation

    Args:
        docker_client: Docker SDK client instance.
        proxy_image: Docker image for the Squid proxy container.
        network_name: Name for the internal egress network.
        container_name: Name for the proxy container.
    """

    def __init__(
        self,
        docker_client: Any,
        proxy_image: str = DEFAULT_PROXY_IMAGE,
        network_name: str = PROXY_NETWORK_NAME,
        container_name: str = PROXY_CONTAINER_NAME,
    ) -> None:
        self._docker_client = docker_client
        self._proxy_image = proxy_image
        self._network_name = network_name
        self._container_name = container_name
        self._proxy_container: Any | None = None
        self._network: Any | None = None
        self._current_domains: tuple[str, ...] = ()
        # Per-sandbox allowlists — union of all active sandboxes' domains
        self._sandbox_domains: dict[str, tuple[str, ...]] = {}
        self._lock = threading.Lock()

    @property
    def proxy_url(self) -> str:
        """HTTP proxy URL for agent containers on the egress network."""
        return f"http://{self._container_name}:{PROXY_PORT}"

    @property
    def network_name(self) -> str:
        """Docker network name for egress-enabled containers."""
        return self._network_name

    @property
    def is_running(self) -> bool:
        """Check if proxy container is currently running."""
        if self._proxy_container is None:
            return False
        try:
            self._proxy_container.reload()
            return self._proxy_container.status == "running"
        except Exception:
            return False

    def register_sandbox(
        self,
        sandbox_id: str,
        allowed_domains: tuple[str, ...],
    ) -> str:
        """Register a sandbox's egress domains and update the proxy.

        Thread-safe. The proxy's effective allowlist is the union of all
        registered sandboxes' domains.

        Args:
            sandbox_id: Sandbox identifier.
            allowed_domains: Domains this sandbox needs access to.

        Returns:
            Proxy URL for the sandbox's environment variables.
        """
        with self._lock:
            self._sandbox_domains[sandbox_id] = allowed_domains
            merged = self._merged_domains()
        return self.ensure_running(merged)

    def unregister_sandbox(self, sandbox_id: str) -> None:
        """Remove a sandbox's domains from the proxy allowlist.

        Thread-safe. If no sandboxes remain, the proxy keeps running
        with an empty allowlist (deny all).

        Args:
            sandbox_id: Sandbox identifier.
        """
        with self._lock:
            self._sandbox_domains.pop(sandbox_id, None)
            merged = self._merged_domains()
        if self.is_running:
            self.ensure_running(merged)

    def _merged_domains(self) -> tuple[str, ...]:
        """Compute union of all registered sandbox domains.

        If any sandbox has wildcard ``("*",)``, the result is ``("*",)``.
        """
        all_domains: set[str] = set()
        for domains in self._sandbox_domains.values():
            if "*" in domains:
                return ("*",)
            all_domains.update(domains)
        return tuple(sorted(all_domains))

    def ensure_network(self) -> Any:
        """Ensure the internal egress Docker network exists.

        Creates an internal bridge network with no direct internet access.
        Only the proxy container (dual-homed) can route traffic outbound.

        Returns:
            Docker network object.
        """
        try:
            self._network = self._docker_client.networks.get(self._network_name)
            logger.debug("Egress network '%s' already exists", self._network_name)
        except Exception:
            self._network = self._docker_client.networks.create(
                self._network_name,
                driver="bridge",
                internal=True,  # No direct internet access from this network
                labels={"nexus.role": "egress-proxy"},
            )
            logger.info("Created internal egress network '%s'", self._network_name)
        return self._network

    def ensure_running(
        self,
        allowed_domains: tuple[str, ...] = (),
    ) -> str:
        """Ensure proxy container is running with the given allowlist.

        Idempotent: if already running with the same allowlist, this is a no-op.
        If the allowlist changed, the proxy is restarted with new config.

        Args:
            allowed_domains: Domains to allow through the proxy.
                Empty tuple = deny all. ``("*",)`` = allow all.

        Returns:
            Proxy URL for ``HTTP_PROXY`` / ``HTTPS_PROXY`` env vars.
        """
        # Ensure network exists first
        self.ensure_network()

        # Check if proxy is already running with same config
        if self.is_running and self._current_domains == allowed_domains:
            logger.debug("Egress proxy already running with current config")
            return self.proxy_url

        # Stop existing proxy if running with different config
        if self.is_running:
            logger.info("Reconfiguring egress proxy with new allowlist")
            self.stop()

        # Generate Squid config
        squid_config = build_squid_config(allowed_domains)

        try:
            # Encode config as base64 to avoid shell injection —
            # domain names are validated, but belt-and-suspenders
            import base64

            config_b64 = base64.b64encode(squid_config.encode()).decode()

            self._proxy_container = self._docker_client.containers.run(
                image=self._proxy_image,
                name=self._container_name,
                detach=True,
                network=self._network_name,
                command=[
                    "sh",
                    "-c",
                    f"echo '{config_b64}' | base64 -d > /etc/squid/squid.conf && squid -N",
                ],
                labels={"nexus.role": "egress-proxy"},
                # Security hardening for the proxy container itself
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                read_only=True,
                tmpfs={"/var/spool/squid": "size=50m", "/var/run": "size=1m"},
                mem_limit="128m",
                cpu_quota=50000,  # 0.5 CPU
                cpu_period=100000,
                pids_limit=64,
            )

            # Connect proxy to default bridge network for outbound access
            bridge = self._docker_client.networks.get("bridge")
            bridge.connect(self._proxy_container)

            self._current_domains = allowed_domains
            logger.info(
                "Started egress proxy (allowed_domains=%d, image=%s)",
                len(allowed_domains),
                self._proxy_image,
            )
            return self.proxy_url

        except Exception:
            logger.exception("Failed to start egress proxy")
            raise

    def update_allowlist(self, allowed_domains: tuple[str, ...]) -> None:
        """Update the proxy domain allowlist.

        Restarts the proxy container with the new configuration.

        Args:
            allowed_domains: New domain allowlist.
        """
        if self._current_domains == allowed_domains:
            return
        self.ensure_running(allowed_domains)

    def stop(self) -> None:
        """Stop and remove the proxy container."""
        if self._proxy_container is not None:
            try:
                self._proxy_container.stop(timeout=5)
                self._proxy_container.remove(force=True)
                logger.info("Stopped egress proxy container")
            except Exception as e:
                logger.warning("Error stopping egress proxy: %s", e)
            finally:
                self._proxy_container = None
                self._current_domains = ()

    def cleanup(self) -> None:
        """Stop proxy and remove the egress network."""
        self.stop()
        if self._network is not None:
            try:
                self._network.remove()
                logger.info("Removed egress network '%s'", self._network_name)
            except Exception as e:
                logger.warning("Error removing egress network: %s", e)
            finally:
                self._network = None

    def get_container_network_config(
        self,
        allowed_domains: tuple[str, ...],
        sandbox_id: str | None = None,
    ) -> dict[str, Any]:
        """Get Docker kwargs for connecting an agent container to the proxy.

        Returns the network and environment settings needed for an agent
        container to route egress through the proxy. Merge into the
        container's ``docker_kwargs`` before creation.

        If ``sandbox_id`` is provided, registers this sandbox's domains
        so the proxy allowlist is the union of all active sandboxes.

        Args:
            allowed_domains: Domains this container should be able to access.
                If empty, returns empty dict (no proxy = ``network=none``).
            sandbox_id: Optional sandbox ID for per-sandbox domain tracking.

        Returns:
            Dict of Docker kwargs to merge into container config.
        """
        if not allowed_domains:
            # No egress needed — profile's network_mode=none stands
            return {}

        # Register sandbox and ensure proxy is running with merged domains
        if sandbox_id is not None:
            proxy_url = self.register_sandbox(sandbox_id, allowed_domains)
        else:
            proxy_url = self.ensure_running(allowed_domains)

        return {
            "network": self._network_name,
            "environment": {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "NO_PROXY": "localhost,127.0.0.1",
                "no_proxy": "localhost,127.0.0.1",
            },
        }
