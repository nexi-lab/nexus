"""CLI configuration — profile management and connection resolution.

Manages ~/.nexus/config.yaml with named connection profiles and settings.
Provides resolve_connection() as the single source of truth for "which server
am I connecting to and why?"

Config file format:
    current-profile: production
    profiles:
      local:
        url: http://localhost:2026
        api-key: nx_test_local_dev
        zone-id: default
      production:
        url: https://nexus.prod.example.com
        api-key: nx_live_prod_abc123
        zone-id: us-west-1
    settings:
      output:
        format: table
        color: true
      connection:
        timeout: 30
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".nexus"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
_FILE_PERMISSIONS = 0o600  # Owner read/write only
_DIR_PERMISSIONS = 0o700  # Owner rwx only

# Settings that nexus config set/get/reset can modify
SUPPORTED_SETTINGS: dict[str, Any] = {
    "default-zone-id": None,
    "output.format": "table",
    "output.color": True,
    "timing.enabled": False,
    "timing.verbosity": "normal",
    "connection.timeout": 30,
    "connection.pool-size": 10,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileEntry:
    """A single named connection profile."""

    url: str | None = None
    api_key: str | None = None
    zone_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.url is not None:
            d["url"] = self.url
        if self.api_key is not None:
            d["api-key"] = self.api_key
        if self.zone_id is not None:
            d["zone-id"] = self.zone_id
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileEntry:
        return cls(
            url=data.get("url"),
            api_key=data.get("api-key"),
            zone_id=data.get("zone-id"),
        )


@dataclass
class NexusCliConfig:
    """Parsed ~/.nexus/config.yaml."""

    current_profile: str | None = None
    profiles: dict[str, ProfileEntry] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.current_profile is not None:
            d["current-profile"] = self.current_profile
        if self.profiles:
            d["profiles"] = {name: p.to_dict() for name, p in self.profiles.items()}
        if self.settings:
            d["settings"] = self.settings
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NexusCliConfig:
        profiles: dict[str, ProfileEntry] = {}
        raw_profiles = data.get("profiles", {})
        if isinstance(raw_profiles, dict):
            for name, pdata in raw_profiles.items():
                if isinstance(pdata, dict):
                    profiles[name] = ProfileEntry.from_dict(pdata)
        settings = data.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}
        return cls(
            current_profile=data.get("current-profile"),
            profiles=profiles,
            settings=settings,
        )


@dataclass(frozen=True)
class ResolvedConnection:
    """Result of resolve_connection() — the effective connection config with source annotations."""

    url: str | None = None
    api_key: str | None = None
    zone_id: str | None = None
    source: str = "default (local)"

    @property
    def is_remote(self) -> bool:
        return bool(self.url and self.url.strip())


# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------


def get_config_path() -> Path:
    """Return the config file path (~/.nexus/config.yaml)."""
    return CONFIG_FILE


def load_cli_config(path: Path | None = None) -> NexusCliConfig:
    """Load CLI config from disk. Returns empty config if file doesn't exist."""
    config_path = path or CONFIG_FILE
    if not config_path.exists():
        return NexusCliConfig()
    _check_file_permissions(config_path)
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return NexusCliConfig()
    return NexusCliConfig.from_dict(data)


def save_cli_config(config: NexusCliConfig, path: Path | None = None) -> None:
    """Save CLI config to disk with 0600 permissions."""
    config_path = path or CONFIG_FILE
    _ensure_config_dir(config_path.parent)
    data = config.to_dict()
    tmp_path = config_path.with_suffix(".yaml.tmp")
    with open(tmp_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.chmod(tmp_path, _FILE_PERMISSIONS)
    tmp_path.rename(config_path)


def _ensure_config_dir(dir_path: Path) -> None:
    """Create config directory with 0700 permissions if it doesn't exist."""
    if not dir_path.exists():
        dir_path.mkdir(parents=True, mode=_DIR_PERMISSIONS)
    elif not dir_path.is_dir():
        msg = f"Config path exists but is not a directory: {dir_path}"
        raise FileExistsError(msg)


def _check_file_permissions(path: Path) -> None:
    """Warn if config file has overly permissive permissions."""
    try:
        file_stat = path.stat()
        mode = file_stat.st_mode
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            from rich.console import Console

            Console(stderr=True).print(
                f"[yellow]Warning:[/yellow] {path} has permissions "
                f"{oct(stat.S_IMODE(mode))}. "
                f"Recommended: {oct(_FILE_PERMISSIONS)} (owner read/write only). "
                f"Fix with: chmod 600 {path}"
            )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Connection resolution
# ---------------------------------------------------------------------------


def resolve_connection(
    remote_url: str | None = None,
    remote_api_key: str | None = None,
    profile_name: str | None = None,
    zone_id: str | None = None,
    config: NexusCliConfig | None = None,
) -> ResolvedConnection:
    """Resolve the effective connection parameters.

    Precedence (highest to lowest):
        1. Explicit CLI flags (--remote-url / --remote-api-key)
           Note: NEXUS_URL / NEXUS_API_KEY env vars are mapped to these by Click.
        2. Named profile (--profile flag)
        3. current-profile from ~/.nexus/config.yaml
        4. Local default (no URL)

    Args:
        remote_url: From --remote-url flag or NEXUS_URL env (via Click envvar).
        remote_api_key: From --remote-api-key flag or NEXUS_API_KEY env.
        profile_name: From --profile flag.
        zone_id: From --zone-id flag or NEXUS_ZONE_ID env.
        config: Pre-loaded CLI config (loaded lazily if None).

    Returns:
        ResolvedConnection with effective values and source annotation.
    """
    # Strip whitespace from URL to avoid connecting to " " (whitespace bug fix)
    if remote_url is not None:
        remote_url = remote_url.strip() or None
    if remote_api_key is not None:
        remote_api_key = remote_api_key.strip() or None

    # Precedence 1: Explicit CLI flags / env vars
    if remote_url:
        return ResolvedConnection(
            url=remote_url,
            api_key=remote_api_key,
            zone_id=zone_id,
            source="--remote-url flag / NEXUS_URL env",
        )

    # Load config lazily if not provided
    if config is None:
        config = load_cli_config()

    # Precedence 2: Named profile from --profile flag
    effective_profile_name = profile_name
    source_prefix = "--profile flag"

    # Precedence 3: current-profile from config file
    if effective_profile_name is None and config.current_profile:
        effective_profile_name = config.current_profile
        source_prefix = f"current-profile in {get_config_path()}"

    if effective_profile_name and effective_profile_name in config.profiles:
        profile = config.profiles[effective_profile_name]
        return ResolvedConnection(
            url=profile.url,
            api_key=profile.api_key,
            zone_id=zone_id or profile.zone_id,
            source=f"profile '{effective_profile_name}' ({source_prefix})",
        )

    if effective_profile_name and effective_profile_name not in config.profiles:
        from rich.console import Console

        Console(stderr=True).print(
            f"[yellow]Warning:[/yellow] Profile '{effective_profile_name}' "
            f"not found in {get_config_path()}. Falling back to local default."
        )

    # Precedence 4: Local default
    return ResolvedConnection(
        url=None,
        api_key=None,
        zone_id=zone_id,
        source="default (local)",
    )


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def get_setting(settings: dict[str, Any], key: str) -> Any:
    """Get a nested setting by dotted key (e.g., 'output.format')."""
    parts = key.split(".")
    current: Any = settings
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return SUPPORTED_SETTINGS.get(key)
        current = current[part]
    return current


def set_setting(settings: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """Set a nested setting by dotted key. Returns new dict (immutable)."""
    parts = key.split(".")
    result = _deep_copy_dict(settings)
    current = result
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = _coerce_value(value)
    return result


def reset_setting(settings: dict[str, Any], key: str) -> dict[str, Any]:
    """Reset a setting to its default. Returns new dict (immutable)."""
    default = SUPPORTED_SETTINGS.get(key)
    if default is None:
        # Remove the key entirely
        parts = key.split(".")
        result = _deep_copy_dict(settings)
        current = result
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                return result  # Key path doesn't exist, nothing to reset
            current = current[part]
        current.pop(parts[-1], None)
        return result
    return set_setting(settings, key, default)


def _coerce_value(value: str | Any) -> Any:
    """Coerce string values to appropriate Python types."""
    if not isinstance(value, str):
        return value
    lower = value.lower()
    if lower in ("true", "yes"):
        return True
    if lower in ("false", "no"):
        return False
    if lower == "null" or lower == "none":
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _deep_copy_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Deep copy a dict of primitives and nested dicts."""
    result: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            result[k] = _deep_copy_dict(v)
        elif isinstance(v, list):
            result[k] = list(v)
        else:
            result[k] = v
    return result


def get_merged_settings(config: NexusCliConfig) -> dict[str, tuple[Any, str]]:
    """Get all settings with their values and sources.

    Returns dict of key -> (value, source) for display in `nexus config show`.
    """
    merged: dict[str, tuple[Any, str]] = {}
    for key, default in SUPPORTED_SETTINGS.items():
        file_value = get_setting(config.settings, key)
        if file_value != default:
            merged[key] = (file_value, str(get_config_path()))
        else:
            merged[key] = (default, "default")
    return merged
