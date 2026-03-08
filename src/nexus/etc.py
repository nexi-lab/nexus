"""File-based brick configuration loader for $STATE_DIR/etc/conf.d/.

Each Nexus brick reads its own config file from /etc/conf.d/{name}.
Files are TOML format (tomllib is stdlib since Python 3.11).

This module has ZERO Nexus imports — only pathlib, tomllib, os, logging.
It can be called before the VFS boots (plain file I/O).

Usage by bricks::

    from nexus.etc import get_brick_config

    cfg = get_brick_config("mounts")
    auto_sync = cfg.get("auto_sync", False)
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

__all__ = [
    "get_brick_config",
    "load_toml_file",
    "resolve_etc_dir",
    "resolve_state_dir",
]

logger = logging.getLogger(__name__)

_STATE_DIR_DEFAULT = "~/.nexus"


def resolve_state_dir() -> Path:
    """Return NEXUS_STATE_DIR from env, or ``~/.nexus``."""
    raw = os.environ.get("NEXUS_STATE_DIR", _STATE_DIR_DEFAULT)
    return Path(raw).expanduser()


def resolve_etc_dir(state_dir: str | Path | None = None) -> Path:
    """Return ``$STATE_DIR/etc``.

    Args:
        state_dir: Override for NEXUS_STATE_DIR.  When *None*,
            :func:`resolve_state_dir` is used.
    """
    base = Path(state_dir).expanduser() if state_dir is not None else resolve_state_dir()
    return base / "etc"


def load_toml_file(path: Path) -> dict[str, Any]:
    """Read a single TOML file.

    Returns:
        Parsed dict on success, empty dict if the file is missing or
        contains invalid TOML (a warning is logged on parse errors).
    """
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        logger.warning("Malformed TOML in %s — ignoring", path)
        return {}


def get_brick_config(
    name: str,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load ``/etc/conf.d/{name}`` as TOML.

    Called by individual bricks at construction time.

    Args:
        name: Brick name (e.g. ``"mounts"``, ``"llm"``, ``"cache"``).
        state_dir: Override for NEXUS_STATE_DIR.

    Returns:
        Parsed config dict, or ``{}`` if the file is missing.
    """
    etc_dir = resolve_etc_dir(state_dir)
    return load_toml_file(etc_dir / "conf.d" / name)
