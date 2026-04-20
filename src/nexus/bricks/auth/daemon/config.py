"""Daemon TOML config at ~/.nexus/daemon.toml (#3804)."""

from __future__ import annotations

import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path


class DaemonConfigError(Exception):
    """Config file missing, unparseable, or missing required keys."""


_REQUIRED = (
    "server_url",
    "tenant_id",
    "principal_id",
    "machine_id",
    "key_path",
    "jwt_cache_path",
    "server_pubkey_path",
)


@dataclass(frozen=True)
class DaemonConfig:
    server_url: str
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    machine_id: uuid.UUID
    key_path: Path
    jwt_cache_path: Path
    server_pubkey_path: Path

    @classmethod
    def load(cls, path: Path) -> DaemonConfig:
        if not path.exists():
            raise DaemonConfigError(f"daemon config not found: {path}")
        try:
            raw = tomllib.loads(path.read_text())
        except Exception as exc:
            raise DaemonConfigError(f"failed to parse {path}: {exc}") from exc
        missing = [k for k in _REQUIRED if k not in raw]
        if missing:
            raise DaemonConfigError(f"config missing keys: {missing}")
        return cls(
            server_url=raw["server_url"],
            tenant_id=uuid.UUID(raw["tenant_id"]),
            principal_id=uuid.UUID(raw["principal_id"]),
            machine_id=uuid.UUID(raw["machine_id"]),
            key_path=Path(raw["key_path"]),
            jwt_cache_path=Path(raw["jwt_cache_path"]),
            server_pubkey_path=Path(raw["server_pubkey_path"]),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f'server_url = "{self.server_url}"',
            f'tenant_id = "{self.tenant_id}"',
            f'principal_id = "{self.principal_id}"',
            f'machine_id = "{self.machine_id}"',
            f'key_path = "{self.key_path}"',
            f'jwt_cache_path = "{self.jwt_cache_path}"',
            f'server_pubkey_path = "{self.server_pubkey_path}"',
        ]
        path.write_text("\n".join(lines) + "\n")
