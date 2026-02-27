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
        """Auto-detect TLS config from ``{data_dir}/tls/``.

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

    @property
    def ca_pem(self) -> bytes:
        return self.ca_cert_path.read_bytes()

    @property
    def node_cert_pem(self) -> bytes:
        return self.node_cert_path.read_bytes()

    @property
    def node_key_pem(self) -> bytes:
        return self.node_key_path.read_bytes()
