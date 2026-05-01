"""TOFU (trust-on-first-use) signer trust store for archives (#3793).

JSON file at `~/.nexus/trusted_signers.json` mapping pubkey-b64 → metadata.
A signer is trusted if its pubkey is present in the file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TrustStore:
    """Persistent TOFU trust store for ed25519 archive signers."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                return data
            return {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def is_trusted(self, pubkey_b64: str) -> bool:
        return pubkey_b64 in self._read()

    def pin(self, pubkey_b64: str, label: str = "") -> None:
        data = self._read()
        data[pubkey_b64] = {
            "first_seen": datetime.now(UTC).isoformat(),
            "label": label,
        }
        self._write(data)

    def all_trusted(self) -> dict[str, dict[str, Any]]:
        return self._read()


__all__ = ["TrustStore"]
