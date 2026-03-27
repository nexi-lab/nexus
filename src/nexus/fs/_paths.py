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
