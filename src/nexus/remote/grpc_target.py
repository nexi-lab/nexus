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


def _grpc_port(default: int = 2028) -> int:
    grpc_port_str = os.getenv("NEXUS_GRPC_PORT")
    if not grpc_port_str:
        with contextlib.suppress(Exception):
            pf = Path("nexus.yaml")
            if pf.exists():
                import yaml

                with open(pf) as f:
                    pc = yaml.safe_load(f) or {}
                grpc_port_str = str(pc.get("ports", {}).get("grpc", ""))
    if grpc_port_str:
        try:
            return int(grpc_port_str)
        except ValueError:
            return default
    return default


def resolve_grpc_target(
    server_url: str,
    *,
    cfg_data_dir: str | None = None,
) -> tuple[str, int, "ZoneTlsConfig | None"]:
    """Resolve ``(grpc_address, grpc_port, tls_config)`` for *server_url*.

    Mirrors the remote-profile resolution in ``nexus.connect`` exactly so
    a preflight reflects real SDK connection behavior.

    Raises:
        RuntimeError: ``NEXUS_GRPC_TLS=true`` but no certificates resolve
            (fail-closed — same as the SDK).
    """
    grpc_port = _grpc_port()
    parsed = urlparse(server_url)
    grpc_address = f"{parsed.hostname}:{grpc_port}"

    tls_config: ZoneTlsConfig | None = None
    grpc_tls_env = os.getenv("NEXUS_GRPC_TLS", "").lower()
    tls_enabled = grpc_tls_env in ("true", "1", "yes")
    tls_disabled = grpc_tls_env in ("false", "0", "no")
    tls_from_config = False
    data_dir = os.getenv("NEXUS_DATA_DIR")
    if data_dir and not tls_disabled:
        tls_enabled = True  # NEXUS_DATA_DIR auto-detect (backward compat)
    if not data_dir:
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
