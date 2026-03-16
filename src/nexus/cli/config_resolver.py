"""Unified configuration resolution for Nexus CLI and daemon.

Implements a single precedence chain:
    CLI flag > environment variable > profile config file > default

Used by both ``nexus`` (client) and ``nexusd`` (daemon) to resolve
configuration consistently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_NEXUS_DIR = Path.home() / ".nexus"
_CONFIG_FILE = _NEXUS_DIR / "config.yaml"
_PROFILES_FILE = _NEXUS_DIR / "profiles.yaml"

# Default values for all known configuration keys
_DEFAULTS: dict[str, Any] = {
    "url": "http://localhost:2026",
    "api_key": None,
    "zone_id": None,
    "output.format": "text",
    "output.color": True,
    "timing.enabled": False,
    "timing.verbosity": 0,
    "connection.timeout": 10.0,
    "connection.pool_size": 10,
}

# Mapping from config key to environment variable name
_ENV_VARS: dict[str, str] = {
    "url": "NEXUS_URL",
    "api_key": "NEXUS_API_KEY",
    "zone_id": "NEXUS_ZONE_ID",
    "timing.enabled": "NEXUS_TIMING",
    "connection.timeout": "NEXUS_TIMEOUT",
}


@dataclass(frozen=True)
class ResolvedValue:
    """A config value with its source for display in `nexus config show`."""

    value: Any
    source: str  # "flag", "env", "profile", "config", "default"


@dataclass(frozen=True)
class ConfigResolver:
    """Resolves configuration from multiple sources with clear precedence.

    Precedence (highest to lowest):
        1. CLI flags (passed as overrides)
        2. Environment variables
        3. Active profile in ~/.nexus/profiles.yaml
        4. User config in ~/.nexus/config.yaml
        5. Built-in defaults
    """

    overrides: dict[str, Any] = field(default_factory=dict)
    _config_cache: dict[str, Any] | None = field(default=None, repr=False)
    _profile_cache: dict[str, Any] | None = field(default=None, repr=False)

    def get(self, key: str) -> Any:
        """Get the resolved value for a config key (value only)."""
        return self.resolve(key).value

    def resolve(self, key: str) -> ResolvedValue:
        """Get the resolved value with source annotation."""
        # 1. CLI flag override
        if key in self.overrides and self.overrides[key] is not None:
            return ResolvedValue(value=self.overrides[key], source="flag")

        # 2. Environment variable
        env_var = _ENV_VARS.get(key)
        if env_var:
            env_val = os.environ.get(env_var)
            if env_val is not None:
                return ResolvedValue(value=_coerce(key, env_val), source="env")

        # 3. Active profile
        profile_val = self._get_from_profile(key)
        if profile_val is not None:
            return ResolvedValue(value=profile_val, source="profile")

        # 4. Config file
        config_val = self._get_from_config(key)
        if config_val is not None:
            return ResolvedValue(value=config_val, source="config")

        # 5. Default
        default = _DEFAULTS.get(key)
        return ResolvedValue(value=default, source="default")

    def resolve_all(self) -> dict[str, ResolvedValue]:
        """Resolve all known config keys with their sources."""
        return {key: self.resolve(key) for key in _DEFAULTS}

    def _get_from_profile(self, key: str) -> Any | None:
        """Look up key in the active profile."""
        profiles = self._load_profiles()
        if not profiles:
            return None
        active_name = profiles.get("current_profile")
        if not active_name:
            return None
        active = profiles.get("profiles", {}).get(active_name, {})
        return _nested_get(active, key)

    def _get_from_config(self, key: str) -> Any | None:
        """Look up key in the user config file."""
        config = self._load_config()
        return _nested_get(config, key)

    def _load_profiles(self) -> dict[str, Any]:
        """Load profiles file (cached)."""
        if self._profile_cache is not None:
            return self._profile_cache
        data = _load_yaml(_PROFILES_FILE)
        # Bypass frozen -- one-time cache population
        object.__setattr__(self, "_profile_cache", data)
        return data

    def _load_config(self) -> dict[str, Any]:
        """Load user config file (cached)."""
        if self._config_cache is not None:
            return self._config_cache
        data = _load_yaml(_CONFIG_FILE)
        object.__setattr__(self, "_config_cache", data)
        return data


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning empty dict if missing or invalid."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            result = yaml.safe_load(f) or {}
        if not isinstance(result, dict):
            return {}
        return result
    except Exception:  # noqa: BLE001
        return {}


def _nested_get(data: dict[str, Any], dotted_key: str) -> Any | None:
    """Get a value from a nested dict using dot notation (e.g. 'output.format')."""
    parts = dotted_key.split(".")
    current: Any = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _coerce(key: str, raw: str) -> Any:
    """Coerce a string env var value to the expected type for the key."""
    default = _DEFAULTS.get(key)
    if isinstance(default, bool):
        return raw.lower() in ("true", "1", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return raw
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw
