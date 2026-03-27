"""Centralized path resolution for the nexus-fs package.

Single source of truth for all filesystem paths used by nexus-fs:
state directory, persistent directory, metadata DB, mounts file, etc.

Uses ``platformdirs`` for cross-platform XDG-compliant paths:
    Linux:  ~/.local/state/nexus-fs/
    macOS:  ~/Library/Application Support/nexus-fs/
    Windows: %LOCALAPPDATA%/nexus-fs/

Override with ``NEXUS_FS_STATE_DIR`` environment variable.
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

    This directory stores secrets (OAuth tokens, encryption keys) and must
    have restricted permissions.
    """
    override = os.environ.get("NEXUS_FS_PERSISTENT_DIR")
    p = Path(override) if override else Path.home() / ".nexus"
    p.mkdir(parents=True, exist_ok=True)
    return p


def token_manager_db() -> Path:
    """Return the path to the OAuth token manager database."""
    return persistent_dir() / "nexus.db"


def oauth_key_path() -> Path:
    """Return the path to the OAuth encryption key file."""
    return persistent_dir() / "auth" / "oauth.key"
