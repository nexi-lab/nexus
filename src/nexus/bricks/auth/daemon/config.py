"""Daemon TOML config at ~/.nexus/daemons/<profile>/daemon.toml (#3804).

**Profile-scoped:** each enrolled (tenant, server) pair gets its own directory
under ``~/.nexus/daemons/<profile>/``. The profile name defaults to the
sanitized server host (``localhost-2026``, ``api.nexus.ai``), but can be
overridden with ``--profile NAME`` on enrollment. This matches the
multi-tenant spirit of epic #3788 — the same laptop can push credentials
to multiple Nexus instances without clobbering each other's state.

Directory layout per profile:

    ~/.nexus/daemons/<profile>/
        daemon.toml
        machine.key
        jwt.cache
        server.pub.pem
        queue.db
        status.json
"""

from __future__ import annotations

import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class DaemonConfigError(Exception):
    """Config file missing, unparseable, or missing required keys."""


_REQUIRED = (
    "profile",
    "server_url",
    "tenant_id",
    "principal_id",
    "machine_id",
    "key_path",
    "jwt_cache_path",
    "server_pubkey_path",
)

_PROFILE_SANITIZE = re.compile(r"[^a-zA-Z0-9._-]+")


def default_profile_for(server_url: str) -> str:
    """Derive a filesystem-safe profile name from a server URL.

    Examples::

        "http://localhost:2026"     -> "localhost-2026"
        "https://api.nexus.ai"      -> "api.nexus.ai"
        "https://api.nexus.ai:8443" -> "api.nexus.ai-8443"
    """
    parsed = urlparse(server_url)
    host = parsed.hostname or "default"
    port = parsed.port
    raw = f"{host}-{port}" if port else host
    cleaned = _PROFILE_SANITIZE.sub("-", raw).strip("-")
    return cleaned or "default"


def daemons_root(nexus_home: Path) -> Path:
    """``~/.nexus/daemons`` — parent of all per-profile dirs."""
    return nexus_home / "daemons"


def profile_dir(nexus_home: Path, profile: str) -> Path:
    """``~/.nexus/daemons/<profile>`` — root of one enrollment's state."""
    return daemons_root(nexus_home) / profile


def list_profiles(nexus_home: Path) -> list[str]:
    """Enumerate existing profile directories (for ``nexus daemon list``)."""
    root = daemons_root(nexus_home)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "daemon.toml").exists())


@dataclass(frozen=True)
class DaemonConfig:
    profile: str
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
            profile=raw["profile"],
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
            f'profile = "{self.profile}"',
            f'server_url = "{self.server_url}"',
            f'tenant_id = "{self.tenant_id}"',
            f'principal_id = "{self.principal_id}"',
            f'machine_id = "{self.machine_id}"',
            f'key_path = "{self.key_path}"',
            f'jwt_cache_path = "{self.jwt_cache_path}"',
            f'server_pubkey_path = "{self.server_pubkey_path}"',
        ]
        path.write_text("\n".join(lines) + "\n")


__all__ = [
    "DaemonConfig",
    "DaemonConfigError",
    "daemons_root",
    "default_profile_for",
    "list_profiles",
    "profile_dir",
]
