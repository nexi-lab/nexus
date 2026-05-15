"""TLS configuration resolved from ``{data_dir}/tls/`` or explicit env vars."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ZoneTlsConfig:
    """Paths to TLS material for a Nexus zone node.

    Construct via :meth:`from_data_dir` for auto-detection or pass paths
    explicitly.  Property accessors read PEM bytes lazily.
    """

    ca_cert_path: Path
    node_cert_path: Path
    node_key_path: Path
    known_zones_path: Path

    @classmethod
    def from_data_dir(cls, data_dir: str | Path) -> ZoneTlsConfig | None:
        """Auto-detect TLS config from ``{data_dir}/tls/`` (Raft layout).

        Recognizes the Raft/federation layout only: ``ca.pem``,
        ``node.pem``, ``node-key.pem``.  Used by ZoneManager to decide
        whether to auto-generate federation certs — must NOT match the
        OpenSSL layout to avoid suppressing Raft bootstrap.

        Returns ``None`` if the expected certificate files do not exist.
        """
        tls_dir = Path(data_dir) / "tls"
        ca = tls_dir / "ca.pem"
        cert = tls_dir / "node.pem"
        key = tls_dir / "node-key.pem"
        if not (ca.exists() and cert.exists() and key.exists()):
            return None
        return cls(
            ca_cert_path=ca,
            node_cert_path=cert,
            node_key_path=key,
            known_zones_path=tls_dir / "known_zones",
        )

    @classmethod
    def from_data_dir_any(cls, data_dir: str | Path) -> ZoneTlsConfig | None:
        """Auto-detect TLS config from ``{data_dir}/tls/`` (any layout).

        Checks Raft-style first, then OpenSSL-style (``ca.crt``,
        ``server.crt``, ``server.key`` from ``nexus init --tls``).
        Used by the gRPC server/client — NOT by ZoneManager.
        """
        cfg = cls.from_data_dir(data_dir)
        if cfg is not None:
            return cfg
        tls_dir = Path(data_dir) / "tls"
        ca = tls_dir / "ca.crt"
        cert = tls_dir / "server.crt"
        key = tls_dir / "server.key"
        if not (ca.exists() and cert.exists() and key.exists()):
            return None
        return cls(
            ca_cert_path=ca,
            node_cert_path=cert,
            node_key_path=key,
            known_zones_path=tls_dir / "known_zones",
        )

    @classmethod
    def from_env(cls) -> ZoneTlsConfig | None:
        """Resolve TLS config from environment variables or auto-detection.

        Checks ``NEXUS_TLS_CERT``/``NEXUS_TLS_KEY``/``NEXUS_TLS_CA`` first,
        then auto-detects from ``{NEXUS_DATA_DIR}/tls/``.  Returns ``None``
        when no TLS material is available.
        """
        import os

        cert = os.environ.get("NEXUS_TLS_CERT")
        key = os.environ.get("NEXUS_TLS_KEY")
        ca = os.environ.get("NEXUS_TLS_CA")
        if cert and key and ca:
            return cls(
                ca_cert_path=Path(ca),
                node_cert_path=Path(cert),
                node_key_path=Path(key),
                known_zones_path=Path(ca).parent / "known_zones",
            )

        data_dir = os.environ.get("NEXUS_DATA_DIR")
        if data_dir:
            return cls.from_data_dir(data_dir)

        return None

    @property
    def ca_pem(self) -> bytes:
        return self.ca_cert_path.read_bytes()

    @property
    def node_cert_pem(self) -> bytes:
        return self.node_cert_path.read_bytes()

    @property
    def node_key_pem(self) -> bytes:
        return self.node_key_path.read_bytes()
