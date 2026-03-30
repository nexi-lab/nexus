"""Centralized path resolution for the nexus-fs package.

Single source of truth for all filesystem paths used by nexus-fs:
state directory, persistent directory, metadata DB, mounts file, etc.

Two directories
---------------
**State directory** (``state_dir()``)
    Runtime metadata that can be regenerated: ``metadata.db``, ``mounts.json``.
    Safe to delete — nexus-fs recreates it on next mount.
    Uses ``platformdirs`` for cross-platform XDG-compliant paths:
        Linux:  ~/.local/state/nexus-fs/
        macOS:  ~/Library/Application Support/nexus-fs/
        Windows: %LOCALAPPDATA%/nexus-fs/
    Override: ``NEXUS_FS_STATE_DIR``

**Persistent directory** (``persistent_dir()``)
    Secrets that must survive state resets: OAuth tokens, encryption keys.
    Created with restricted permissions (``0o700``).
    Default: ``~/.nexus/`` (legacy, pre-XDG).
    Override: ``NEXUS_FS_PERSISTENT_DIR``

The split exists because state can be wiped without data loss, but secrets
cannot be regenerated without re-authenticating.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import PlatformDirs

_dirs = PlatformDirs(appname="nexus-fs", appauthor=False)

# ── State directory (runtime metadata, mount config) ───────────────────────


def state_dir() -> Path:
    """Return the nexus-fs state directory, creating it if needed.

    Resolution order:
    1. ``NEXUS_FS_STATE_DIR`` environment variable
    2. Platform-specific state directory via platformdirs
    """
    override = os.environ.get("NEXUS_FS_STATE_DIR")
    p = Path(override) if override else Path(_dirs.user_state_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def metadata_db() -> Path:
    """Return the path to the SQLite metadata database."""
    return state_dir() / "metadata.db"


def mounts_file() -> Path:
    """Return the path to the mounts.json file."""
    return state_dir() / "mounts.json"


# ── Mount persistence helpers ─────────────────────────────────────────────


def _normalize_mount_entry(entry: "str | dict") -> dict:
    """Normalize a mount entry to ``{"uri": ..., "at": ...}`` form.

    Handles both the legacy format (plain URI string) and the new
    format (dict with ``uri`` and optional ``at``).
    """
    if isinstance(entry, str):
        return {"uri": entry, "at": None}
    return {"uri": entry["uri"], "at": entry.get("at")}


def load_persisted_mounts() -> list[dict]:
    """Load persisted mount entries from ``mounts.json``.

    Returns a list of ``{"uri": str, "at": str | None}`` dicts.
    Backward-compatible with the legacy ``["uri", ...]`` format.
    Returns an empty list if the file doesn't exist or is invalid.
    """
    import json

    mf = mounts_file()
    try:
        with open(mf) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(raw, list):
        return []

    return [_normalize_mount_entry(e) for e in raw]


def build_mount_args(entries: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Convert persisted entries to ``mount()`` arguments.

    Returns:
        ``(uris, mount_overrides)`` ready to pass to ``mount(*uris, mount_overrides=...)``.
    """
    uris: list[str] = []
    overrides: dict[str, str] = {}
    for entry in entries:
        uri = entry["uri"]
        if uri not in uris:
            uris.append(uri)
        if entry.get("at"):
            overrides[uri] = entry["at"]
    return uris, overrides


def save_persisted_mounts(entries: list[dict], *, merge: bool = True) -> None:
    """Persist mount entries to ``mounts.json``, merging by default.

    Args:
        entries: List of ``{"uri": str, "at": str | None}`` dicts to save.
        merge: If True (default), merge with existing entries. Entries
            with the same URI are updated; new entries are appended.
    """
    import json
    import tempfile

    if merge:
        existing = load_persisted_mounts()
        # Index existing by URI for dedup
        by_uri = {e["uri"]: e for e in existing}
        for entry in entries:
            by_uri[entry["uri"]] = entry
        entries = list(by_uri.values())

    mf = mounts_file()
    fd, tmp = tempfile.mkstemp(dir=mf.parent, suffix=".tmp")
    try:
        with open(fd, "w") as f:
            json.dump(entries, f)
            f.flush()
        os.replace(tmp, mf)
    except BaseException:
        with __import__("contextlib").suppress(OSError):
            os.unlink(tmp)
        raise


# ── Persistent directory (OAuth tokens, encryption keys) ───────────────────


def persistent_dir() -> Path:
    """Return the nexus-fs persistent data directory, creating it if needed.

    Resolution order:
    1. ``NEXUS_FS_PERSISTENT_DIR`` environment variable
    2. ``~/.nexus/`` (legacy default, maintained for backwards compatibility)

    This directory stores secrets (OAuth tokens, encryption keys).
    Created with mode ``0o700`` (owner-only access) on POSIX systems.
    """
    override = os.environ.get("NEXUS_FS_PERSISTENT_DIR")
    p = Path(override) if override else Path.home() / ".nexus"
    p.mkdir(parents=True, exist_ok=True)
    # Restrict permissions to owner-only on POSIX (no-op on Windows where
    # chmod is unsupported).
    import contextlib

    with contextlib.suppress(OSError):
        p.chmod(0o700)
    return p


def token_manager_db() -> Path:
    """Return the path to the OAuth token manager database."""
    return persistent_dir() / "nexus.db"


def oauth_key_path() -> Path:
    """Return the path to the OAuth encryption key file."""
    return persistent_dir() / "auth" / "oauth.key"
