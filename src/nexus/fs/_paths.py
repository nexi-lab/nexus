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

import datetime
import os
import sys
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
    """Normalize a mount entry to the canonical dict form.

    Handles the legacy plain-string format and all versioned dict forms.
    Missing metadata fields default to None so callers can always do
    ``entry.get("created_at")`` without KeyError.

    Canonical shape::

        {
            "uri":          str,
            "at":           str | None,
            "name":         str | None,   # user-assigned human label
            "created_at":   str | None,   # ISO-8601 UTC
            "created_by":   str | None,   # "pid=N exe=nexus-fs"
            "last_used_at": str | None,   # ISO-8601 UTC
        }
    """
    if isinstance(entry, str):
        return {
            "uri": entry,
            "at": None,
            "name": None,
            "created_at": None,
            "created_by": None,
            "last_used_at": None,
        }
    return {
        "uri": entry["uri"],
        "at": entry.get("at"),
        "name": entry.get("name"),
        "created_at": entry.get("created_at"),
        "created_by": entry.get("created_by"),
        "last_used_at": entry.get("last_used_at"),
    }


def load_persisted_mounts() -> list[dict]:
    """Load persisted mount entries from ``mounts.json``.

    Returns a list of ``{"uri": str, "at": str | None}`` dicts.
    Backward-compatible with the legacy ``["uri", ...]`` format.
    Returns an empty list if the file doesn't exist or is invalid.

    Note: A missing file is silently treated as an empty list (normal first-run
    state).  A *corrupt* file (invalid JSON) emits a warning to stderr and also
    returns an empty list so the process can continue, but the warning tells the
    user their state file needs attention rather than silently losing their mounts.
    """
    import json
    import sys

    mf = mounts_file()
    try:
        with open(mf) as f:
            raw = json.load(f)
    except OSError:
        # File doesn't exist or isn't readable — normal on first run.
        return []
    except json.JSONDecodeError as exc:
        # File exists but is corrupt.  Warn loudly so the user knows to restore
        # from the .bak backup rather than silently losing their mount config.
        print(
            f"warning: nexus-fs: mounts.json is corrupt and will be ignored "
            f"({mf}: {exc}). Restore from a .bak backup if needed.",
            file=sys.stderr,
        )
        return []

    if not isinstance(raw, list):
        # Valid JSON but wrong shape — treat as empty, no warning needed.
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


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.datetime.now(datetime.UTC).isoformat()


def _created_by_stamp() -> str:
    """Return a human-readable 'who created this entry' string."""
    exe = Path(sys.argv[0]).name if sys.argv else "unknown"
    return f"pid={os.getpid()} exe={exe}"


def save_persisted_mounts(entries: list[dict], *, merge: bool = True) -> None:
    """Persist mount entries to ``mounts.json``, merging by default.

    Args:
        entries: List of mount entry dicts.  At minimum each must have a
            ``"uri"`` key.  Other fields (``at``, ``name``, ``created_at``,
            ``created_by``, ``last_used_at``) are optional — missing ones
            are auto-populated or preserved from the existing entry.
        merge: If True (default), merge with existing entries by URI:
            - New URI → ``created_at`` / ``created_by`` are stamped now.
            - Existing URI → ``created_at`` / ``created_by`` preserved;
              ``last_used_at`` updated to now; ``name`` preserved if the
              incoming entry omits it.
            - ``--at`` change on an existing URI → warning to stderr (the
              new value wins; mount() prevents true dual-mount of same URI).

    Concurrency note: the write is atomic on local POSIX/Windows filesystems
    (mkstemp + os.replace).  It is NOT atomic across NFS/CIFS — two
    concurrent writers on a network-backed NEXUS_FS_STATE_DIR can silently
    drop each other's changes.  Use isolated NEXUS_FS_STATE_DIR paths in
    parallel test workers (via ``monkeypatch.setenv`` or ``ephemeral_mount``).
    """
    import json
    import tempfile

    if merge:
        existing = load_persisted_mounts()
        by_uri = {e["uri"]: e for e in existing}
        now = _now_iso()
        creator = _created_by_stamp()

        for entry in entries:
            uri = entry["uri"]
            prev = by_uri.get(uri)

            if prev:
                # Warn on silent --at replacement
                if prev.get("at") != entry.get("at"):
                    print(
                        f"warning: nexus-fs: mount point for {uri!r} changed "
                        f"from {prev.get('at')!r} to {entry.get('at')!r}.",
                        file=sys.stderr,
                    )
                merged: dict = {**prev, **entry}
                # Preserve provenance from the original creation
                merged["created_at"] = prev.get("created_at") or now
                merged["created_by"] = prev.get("created_by") or creator
                merged["last_used_at"] = now
                # Keep existing name when the caller doesn't supply one
                if not entry.get("name"):
                    merged["name"] = prev.get("name")
            else:
                merged = dict(entry)
                merged.setdefault("created_at", now)
                merged.setdefault("created_by", creator)
                merged.setdefault("last_used_at", now)
                merged.setdefault("name", None)

            by_uri[uri] = merged

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
