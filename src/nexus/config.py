"""Configuration system for Nexus."""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class NexusConfig(BaseModel):
    """
    Unified configuration for all Nexus deployment modes.

    Configuration is loaded from (in order of priority):
    1. Explicitly provided config dict/object
    2. Environment variables (NEXUS_*)
    3. Config file (./nexus.yaml, ~/.nexus/config.yaml)
    4. Defaults (embedded mode with ./nexus-data)
    """

    # Deployment mode
    mode: str = Field(
        default="embedded",
        description="Deployment mode: embedded, monolithic, or distributed",
    )

    # Embedded mode settings
    data_dir: str | None = Field(
        default="./nexus-data", description="Data directory for embedded mode"
    )
    cache_size_mb: int = Field(default=100, description="Cache size in megabytes")
    enable_vector_search: bool = Field(default=True, description="Enable vector search")
    enable_llm_cache: bool = Field(default=True, description="Enable LLM KV cache")
    db_path: str | None = Field(
        default=None, description="SQLite database path (auto-generated if None)"
    )

    # Multi-tenant isolation settings
    tenant_id: str | None = Field(
        default=None, description="Tenant identifier for multi-tenant isolation"
    )
    agent_id: str | None = Field(
        default=None, description="Agent identifier for agent-level isolation in /workspace"
    )
    is_admin: bool = Field(default=False, description="Whether this instance has admin privileges")

    # Custom namespace configurations
    namespaces: list[dict[str, Any]] | None = Field(
        default=None,
        description="Custom namespace configurations (list of dicts with name, readonly, admin_only, requires_tenant)",
    )

    # Remote mode settings (monolithic/distributed)
    url: str | None = Field(default=None, description="Nexus server URL for remote modes")
    api_key: str | None = Field(default=None, description="API key for authentication")
    timeout: float = Field(default=30.0, description="Request timeout in seconds")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Validate deployment mode."""
        allowed = ["embedded", "monolithic", "distributed"]
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}, got {v}")
        return v

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str | None, info: Any) -> str | None:
        """Validate URL is required for remote modes."""
        mode = info.data.get("mode")
        if mode in ["monolithic", "distributed"] and not v:
            # Check if we can get from environment
            env_url = os.getenv("NEXUS_URL")
            if env_url:
                return env_url
            raise ValueError(f"url is required for {mode} mode")
        return v

    class Config:
        """Pydantic config."""

        frozen = False  # Allow modifications after creation


def load_config(
    config: str | Path | dict[str, Any] | NexusConfig | None = None,
) -> NexusConfig:
    """
    Load Nexus configuration from various sources.

    Args:
        config: Configuration source:
            - None: Auto-discover from environment/files
            - str/Path: Path to config file
            - dict: Configuration dictionary
            - NexusConfig: Already loaded config (passthrough)

    Returns:
        Loaded NexusConfig

    Raises:
        FileNotFoundError: If specified config file doesn't exist
        ValueError: If configuration is invalid
    """
    # Passthrough if already a NexusConfig
    if isinstance(config, NexusConfig):
        return config

    # Load from dict
    if isinstance(config, dict):
        return _load_from_dict(config)

    # Load from file path
    if isinstance(config, str | Path):
        return _load_from_file(Path(config))

    # Auto-discover
    return _auto_discover()


def _load_from_dict(config_dict: dict[str, Any]) -> NexusConfig:
    """Load configuration from dictionary."""
    # Merge with environment variables
    merged = _load_from_environment()
    merged_dict = merged.model_dump()
    merged_dict.update(config_dict)
    return NexusConfig(**merged_dict)


def _load_from_file(path: Path) -> NexusConfig:
    """Load configuration from file."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        if path.suffix in [".yaml", ".yml"]:
            config_dict = yaml.safe_load(f)
        else:
            raise ValueError(f"Unsupported config file format: {path.suffix}")

    return _load_from_dict(config_dict)


def _load_from_environment() -> NexusConfig:
    """Load configuration from environment variables."""
    env_config: dict[str, Any] = {}

    # Map environment variables to config fields
    env_mapping = {
        "NEXUS_MODE": "mode",
        "NEXUS_DATA_DIR": "data_dir",
        "NEXUS_CACHE_SIZE_MB": "cache_size_mb",
        "NEXUS_ENABLE_VECTOR_SEARCH": "enable_vector_search",
        "NEXUS_ENABLE_LLM_CACHE": "enable_llm_cache",
        "NEXUS_DB_PATH": "db_path",
        "NEXUS_URL": "url",
        "NEXUS_API_KEY": "api_key",
        "NEXUS_TIMEOUT": "timeout",
    }

    for env_var, config_key in env_mapping.items():
        value = os.getenv(env_var)
        if value is not None:
            # Type conversion for non-string fields
            converted_value: Any
            if config_key in ["cache_size_mb", "timeout"]:
                converted_value = float(value) if config_key == "timeout" else int(value)
            elif config_key in ["enable_vector_search", "enable_llm_cache"]:
                converted_value = value.lower() in ["true", "1", "yes", "on"]
            else:
                converted_value = value
            env_config[config_key] = converted_value

    return NexusConfig(**env_config)


def _auto_discover() -> NexusConfig:
    """
    Auto-discover configuration from standard locations.

    Search order:
    1. ./nexus.yaml
    2. ./nexus.yml
    3. ~/.nexus/config.yaml
    4. Environment variables
    5. Defaults
    """
    # Check current directory
    for filename in ["nexus.yaml", "nexus.yml"]:
        path = Path(filename)
        if path.exists():
            return _load_from_file(path)

    # Check home directory
    home_config = Path.home() / ".nexus" / "config.yaml"
    if home_config.exists():
        return _load_from_file(home_config)

    # Fall back to environment variables and defaults
    return _load_from_environment()
