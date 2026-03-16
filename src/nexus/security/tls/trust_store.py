"""TOFU (Trust On First Use) trust store for zone CA certificates.

Modelled after SSH ``known_hosts``: on first contact the peer zone's CA
fingerprint is pinned.  Subsequent connections verify the fingerprint.
If it changes, a ``ZoneCertificateChangedError`` is raised — the operator
must explicitly ``nexus tls forget-zone`` before reconnecting.

Storage format (JSONL, one JSON object per line)::

    {"zone_id":"shared","ca_fingerprint":"SHA256:abc...","ca_pem":"-----BEGIN...","first_seen":"2026-02-27T10:30:00Z","last_verified":"...","peer_addresses":["10.0.0.2:2126"]}
"""

from __future__ import annotations

import datetime
import enum
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from nexus.security.tls.certgen import cert_fingerprint

logger = logging.getLogger(__name__)


class TofuResult(enum.Enum):
    """Outcome of a TOFU verification."""

    TRUSTED_NEW = "trusted_new"
    TRUSTED_KNOWN = "trusted_known"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"


class ZoneCertificateChangedError(Exception):
    """Raised when a known zone presents a different CA fingerprint.

    Similar to SSH's ``REMOTE HOST IDENTIFICATION HAS CHANGED`` warning.
    """

    def __init__(self, zone_id: str, expected: str, got: str) -> None:
        self.zone_id = zone_id
        self.expected_fingerprint = expected
        self.got_fingerprint = got
        super().__init__(
            f"@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            f"@    WARNING: ZONE CERTIFICATE CHANGED!    @\n"
            f"@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            f"Zone '{zone_id}' CA fingerprint changed.\n"
            f"  Expected: {expected}\n"
            f"  Got:      {got}\n"
            f"This could indicate a MITM attack or certificate rotation.\n"
            f"If expected, run: nexus tls forget-zone {zone_id}"
        )


@dataclass
class TrustedZone:
    """A pinned zone entry in the trust store."""

    zone_id: str
    ca_fingerprint: str
    ca_pem: str
    first_seen: str
    last_verified: str
    peer_addresses: list[str] = field(default_factory=list)


class TofuTrustStore:
    """File-backed TOFU trust store (JSONL format).

    Trust is per-node (not per-zone via Raft) because trust decisions are
    node-local — the same way SSH ``known_hosts`` is per-user, not shared.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, TrustedZone] = {}
        self._load()

    def _load(self) -> None:
        """Load entries from JSONL file."""
        if not self._path.exists():
            return
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                self._entries[obj["zone_id"]] = TrustedZone(
                    zone_id=obj["zone_id"],
                    ca_fingerprint=obj["ca_fingerprint"],
                    ca_pem=obj["ca_pem"],
                    first_seen=obj["first_seen"],
                    last_verified=obj["last_verified"],
                    peer_addresses=obj.get("peer_addresses", []),
                )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Skipping malformed trust store entry: %s", exc)

    def _save(self) -> None:
        """Persist all entries to JSONL file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for entry in self._entries.values():
            lines.append(
                json.dumps(
                    {
                        "zone_id": entry.zone_id,
                        "ca_fingerprint": entry.ca_fingerprint,
                        "ca_pem": entry.ca_pem,
                        "first_seen": entry.first_seen,
                        "last_verified": entry.last_verified,
                        "peer_addresses": entry.peer_addresses,
                    }
                )
            )
        self._path.write_text("\n".join(lines) + "\n" if lines else "")

    def verify_or_trust(
        self,
        zone_id: str,
        ca_cert: x509.Certificate,
        peer_address: str,
    ) -> TofuResult:
        """Verify a peer zone's CA certificate against the trust store.

        - First contact: pin fingerprint → ``TRUSTED_NEW``
        - Known + matching: update last_verified → ``TRUSTED_KNOWN``
        - Known + mismatched: raise ``ZoneCertificateChangedError``
        """
        fp = cert_fingerprint(ca_cert)
        now = datetime.datetime.now(datetime.UTC).isoformat()

        existing = self._entries.get(zone_id)
        if existing is None:
            # First contact — pin
            ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
            self._entries[zone_id] = TrustedZone(
                zone_id=zone_id,
                ca_fingerprint=fp,
                ca_pem=ca_pem,
                first_seen=now,
                last_verified=now,
                peer_addresses=[peer_address],
            )
            self._save()
            logger.info(
                "TOFU: pinned zone '%s' CA fingerprint %s (peer %s)",
                zone_id,
                fp,
                peer_address,
            )
            return TofuResult.TRUSTED_NEW

        if existing.ca_fingerprint != fp:
            raise ZoneCertificateChangedError(zone_id, existing.ca_fingerprint, fp)

        # Known and matching — update metadata
        existing.last_verified = now
        if peer_address not in existing.peer_addresses:
            existing.peer_addresses.append(peer_address)
        self._save()
        return TofuResult.TRUSTED_KNOWN

    def get_ca_pem(self, zone_id: str) -> bytes | None:
        """Get the trusted CA PEM for a zone, or ``None`` if unknown."""
        entry = self._entries.get(zone_id)
        if entry is None:
            return None
        return entry.ca_pem.encode("ascii")

    def remove(self, zone_id: str) -> bool:
        """Remove a zone from the trust store.  Returns True if it existed."""
        if zone_id not in self._entries:
            return False
        del self._entries[zone_id]
        self._save()
        logger.info("TOFU: removed zone '%s' from trust store", zone_id)
        return True

    def build_ca_bundle(self, local_ca_path: Path) -> Path:
        """Write a combined CA bundle (local CA + all trusted zone CAs).

        Returns the path to the bundle file, which can be used as
        ``tls_ca_path`` for gRPC channels that need to trust multiple CAs.
        """
        bundle_path = self._path.parent / "ca-bundle.pem"
        parts: list[str] = []

        # Include local CA first
        if local_ca_path.exists():
            parts.append(local_ca_path.read_text().strip())

        # Append all trusted zone CAs
        for entry in self._entries.values():
            parts.append(entry.ca_pem.strip())

        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text("\n".join(parts) + "\n" if parts else "")
        return bundle_path

    def list_trusted(self) -> list[TrustedZone]:
        """List all trusted zones."""
        return list(self._entries.values())
