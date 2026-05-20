"""Shared remote gRPC target resolution (Issue #4132).

A single source of truth for "given a remote hub URL, what gRPC address
and TLS config does the remote SDK actually use?" — so the SDK
(`nexus.connect(profile="remote")`) and the `nexus doctor remote`
preflight resolve identically. Previously the preflight constructed
`RPCTransport` with only address+token and could report a TLS-enabled
hub (that the SDK connects to fine) as insecure/unreachable.

Port precedence:  NEXUS_GRPC_PORT env  >  nexus.yaml ``ports.grpc``  >  2028
TLS precedence:   NEXUS_GRPC_TLS env (true/false)  >  NEXUS_DATA_DIR
                  auto-detect  >  nexus.yaml ``tls``  >  cfg.data_dir;
                  fail-closed when TLS is explicitly requested but no
                  certificates resolve.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from nexus.security.tls.config import ZoneTlsConfig


def _grpc_port(default: int = 2028, *, trust_local_project: bool = True) -> int:
    """Resolve the gRPC port.

    NEXUS_GRPC_PORT env > nexus.yaml ``ports.grpc`` > *default*.

    A *present-but-invalid* value (non-integer) is a configuration error:
    fail fast with a clear message rather than silently dialing 2028 —
    both the SDK and ``nexus doctor remote`` rely on this resolver, so a
    silent fallback would produce false preflight results or connect to
    an unrelated local service.

    When *trust_local_project* is False (an explicit remote target), the
    cwd ``./nexus.yaml`` is NOT consulted — local project ports must not
    poison a different remote hub (e.g. local ``ports.grpc: 3028`` must
    not make ``--url http://prod:2026`` dial ``prod:3028``).
    """
    grpc_port_str = os.getenv("NEXUS_GRPC_PORT")
    source = "NEXUS_GRPC_PORT"
    if not grpc_port_str and trust_local_project:
        source = "nexus.yaml ports.grpc"
        with contextlib.suppress(Exception):
            pf = Path("nexus.yaml")
            if pf.exists():
                import yaml

                with open(pf) as f:
                    pc = yaml.safe_load(f) or {}
                grpc_port_str = str(pc.get("ports", {}).get("grpc", ""))
    if not grpc_port_str:
        return default
    try:
        port = int(grpc_port_str)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid gRPC port {grpc_port_str!r} from {source}: must be an integer."
        ) from None
    if not (0 < port < 65536):
        raise ValueError(f"Invalid gRPC port {port} from {source}: must be 1–65535.")
    return port


def resolve_grpc_target(
    server_url: str,
    *,
    cfg_data_dir: str | None = None,
    trust_local_project: bool = True,
) -> tuple[str, int, "ZoneTlsConfig | None"]:
    """Resolve ``(grpc_address, grpc_port, tls_config)`` for *server_url*.

    Mirrors the remote-profile resolution in ``nexus.connect`` exactly so
    a preflight reflects real SDK connection behavior.

    *trust_local_project*: when False (an explicit remote target — e.g.
    ``nexus doctor remote --url`` or ``connect(profile="remote",
    url=...)`` not proven to be the locally-managed stack), the cwd
    ``./nexus.yaml`` is IGNORED for port/TLS/data_dir. Only env
    (``NEXUS_GRPC_PORT``, ``NEXUS_GRPC_TLS``, ``NEXUS_DATA_DIR``,
    ``NEXUS_TLS_*``) and explicit *cfg_data_dir* apply, so a local
    project cannot poison a different remote hub's port/TLS.

    Raises:
        RuntimeError: ``NEXUS_GRPC_TLS=true`` but no certificates resolve
            (fail-closed — same as the SDK).
    """
    grpc_port = _grpc_port(trust_local_project=trust_local_project)
    parsed = urlparse(server_url)
    host = parsed.hostname or "localhost"
    # Force IPv4 for ``localhost``: macOS resolves localhost to ``::1``
    # first ("Happy Eyeballs"), but Docker Desktop / OrbStack publish
    # port maps on IPv4 (0.0.0.0) only — so gRPC's first attempt hits
    # ``[::1]:<port>`` and gets "Socket closed", while only the IPv4
    # fallback would have worked. Pin to 127.0.0.1 so the channel
    # picks the right family immediately. Non-localhost hosts are
    # untouched (DNS-resolved targets keep dual-stack behavior).
    if host in ("localhost", "::1"):
        host = "127.0.0.1"
    grpc_address = f"{host}:{grpc_port}"

    tls_config: ZoneTlsConfig | None = None
    grpc_tls_env = os.getenv("NEXUS_GRPC_TLS", "").lower()
    tls_enabled = grpc_tls_env in ("true", "1", "yes")
    tls_disabled = grpc_tls_env in ("false", "0", "no")
    tls_from_config = False
    data_dir = os.getenv("NEXUS_DATA_DIR")
    if data_dir and not tls_disabled:
        tls_enabled = True  # NEXUS_DATA_DIR auto-detect (backward compat)
    if not data_dir and trust_local_project:
        project_yaml = Path("nexus.yaml")
        if project_yaml.exists():
            with contextlib.suppress(Exception):
                import yaml

                with open(project_yaml) as f:
                    project_cfg = yaml.safe_load(f) or {}
                data_dir = project_cfg.get("data_dir")
                if not grpc_tls_env:
                    tls_from_config = bool(project_cfg.get("tls"))
                    tls_enabled = tls_from_config
    if not data_dir:
        data_dir = cfg_data_dir

    if data_dir and tls_enabled:
        from nexus.security.tls.config import ZoneTlsConfig

        tls_intentional = grpc_tls_env in ("true", "1", "yes") or tls_from_config
        tls_config = (
            ZoneTlsConfig.from_data_dir_any(data_dir)
            if tls_intentional
            else ZoneTlsConfig.from_data_dir(data_dir)
        )

    tls_explicit = grpc_tls_env in ("true", "1", "yes")
    if tls_explicit and tls_config is None and os.getenv("NEXUS_TLS_CERT"):
        from nexus.security.tls.config import ZoneTlsConfig

        with contextlib.suppress(Exception):
            tls_config = ZoneTlsConfig.from_env()
    if tls_explicit and tls_config is None:
        raise RuntimeError(
            "NEXUS_GRPC_TLS=true but no TLS certificates found. "
            "Provide certs via NEXUS_TLS_CERT/KEY/CA, "
            "in {data_dir}/tls/, or set data_dir in nexus.yaml."
        )

    return grpc_address, grpc_port, tls_config
