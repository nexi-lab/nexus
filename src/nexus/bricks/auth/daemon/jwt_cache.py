"""Pluggable JWT cache for the daemon (#3804).

Default behavior: try the OS keychain via ``keyring``; on any backend error
(keyring not configured, headless Linux without secret-service, locked
keychain), fall back to a flat file at ``~/.nexus/daemon/jwt.cache``
(mode 0600). The file path is also used as the sole store during tests where
``NEXUS_DAEMON_JWT_CACHE_BACKEND=file`` overrides auto-detection.

Rationale: macOS/Windows usually have a working keychain; Linux servers
often don't (no gnome-keyring, no dbus). A single hard-coded backend
breaks one side or the other, hence the probe-then-fallback design.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

_KEYRING_SERVICE = "com.nexus.daemon"
_KEYRING_USERNAME = "jwt"


class JwtCache(Protocol):
    def load(self) -> str | None: ...
    def store(self, token: str) -> None: ...


class FileJwtCache:
    """Atomic 0600 file cache. Always available; used as keyring fallback."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str | None:
        if not self._path.exists():
            return None
        text = self._path.read_text().strip()
        return text or None

    def store(self, token: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(self._path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(self._path, 0o600)


class KeyringJwtCache:
    """OS keychain-backed cache. Raises on first keyring call if unavailable."""

    def __init__(
        self,
        service: str = _KEYRING_SERVICE,
        username: str = _KEYRING_USERNAME,
    ) -> None:
        self._service = service
        self._username = username

    def load(self) -> str | None:
        import keyring

        return keyring.get_password(self._service, self._username)

    def store(self, token: str) -> None:
        import keyring

        keyring.set_password(self._service, self._username, token)


def make_jwt_cache(file_path: Path) -> JwtCache:
    """Select the best cache for this environment; always succeeds.

    Order of preference:

    1. ``NEXUS_DAEMON_JWT_CACHE_BACKEND=file`` → ``FileJwtCache`` (test hook).
    2. Working OS keychain (``keyring`` imports + roundtrip probe succeeds) →
       ``KeyringJwtCache``.
    3. ``FileJwtCache(file_path)`` as a last-resort fallback.
    """
    if os.environ.get("NEXUS_DAEMON_JWT_CACHE_BACKEND") == "file":
        return FileJwtCache(file_path)
    try:
        import keyring

        probe = "__nexus_daemon_cache_probe__"
        keyring.set_password(_KEYRING_SERVICE, probe, "ok")
        got = keyring.get_password(_KEYRING_SERVICE, probe)
        keyring.delete_password(_KEYRING_SERVICE, probe)
        if got == "ok":
            return KeyringJwtCache()
        log.info("keyring probe returned %r; falling back to file", got)
    except Exception as exc:
        log.info("keyring unavailable (%s); falling back to file cache", exc)
    return FileJwtCache(file_path)


__all__ = [
    "FileJwtCache",
    "JwtCache",
    "KeyringJwtCache",
    "make_jwt_cache",
]
