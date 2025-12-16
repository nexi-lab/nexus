"""Configuration system for Nexus."""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Import OAuthConfig - required for OAuth configuration
from nexus.server.auth.oauth_config import OAuthConfig


class DockerImageTemplate(BaseModel):
    """Configuration for a single Docker image template."""

    image: str | None = Field(
        default=None,
        description="Pre-built Docker image name",
    )
    dockerfile: str | None = Field(
        default=None,
        description="Path to Dockerfile (relative to project root)",
    )
    dockerfile_override: str | None = Field(
        default=None,
        description="Inline Dockerfile content to override/customize the base image",
    )
    context: str | None = Field(
        default=".",
        description="Docker build context directory",
    )


class DockerTemplateConfig(BaseModel):
    """Configuration for Docker sandbox images.

    Maps template names to Docker images or Dockerfiles for custom sandbox environments.
    """

    templates: dict[str, DockerImageTemplate] = Field(
        default_factory=dict,
        description="Map of template name to image/dockerfile configuration",
    )
    default_image: str = Field(
        default="nexus-sandbox:latest",
        description="Default image if no template specified",
    )


class FeaturesConfig(BaseModel):
    """Feature flags for optional Nexus functionality."""

    semantic_search: bool = Field(
        default=False,
        description="Enable semantic search (requires vector database)",
    )
    llm_read: bool = Field(
        default=False,
        description="Enable LLM-powered document reading",
    )
    agent_memory: bool = Field(
        default=True,
        description="Enable agent memory API",
    )
    job_system: bool = Field(
        default=False,
        description="Enable asynchronous job system",
    )
    mcp_server: bool = Field(
        default=True,
        description="Enable Model Context Protocol server",
    )

    model_config = ConfigDict(extra="forbid")


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

    # Backend selection
    backend: str = Field(
        default="local",
        description="Storage backend: 'local' for local filesystem, 'gcs' for Google Cloud Storage",
    )

    # Local backend settings
    data_dir: str | None = Field(
        default="./nexus-data", description="Data directory for local backend"
    )

    # GCS backend settings
    gcs_bucket_name: str | None = Field(
        default=None, description="GCS bucket name (required when backend='gcs')"
    )
    gcs_project_id: str | None = Field(
        default=None,
        description="GCP project ID (optional, inferred from credentials if not provided)",
    )
    gcs_credentials_path: str | None = Field(
        default=None,
        description="Path to GCS service account credentials JSON (optional, uses ADC if not provided)",
    )

    # General settings
    cache_size_mb: int = Field(default=100, description="Cache size in megabytes")
    enable_vector_search: bool = Field(default=True, description="Enable vector search")
    enable_llm_cache: bool = Field(default=True, description="Enable LLM KV cache")
    db_path: str | None = Field(
        default=None, description="SQLite database path (auto-generated if None)"
    )

    # In-memory metadata caching settings
    enable_metadata_cache: bool = Field(
        default=True, description="Enable in-memory metadata caching"
    )
    cache_path_size: int = Field(default=512, description="Max entries for path metadata cache")
    cache_list_size: int = Field(default=128, description="Max entries for directory listing cache")
    cache_kv_size: int = Field(default=256, description="Max entries for file metadata KV cache")
    cache_exists_size: int = Field(
        default=1024, description="Max entries for existence check cache"
    )
    cache_ttl_seconds: int | None = Field(
        default=300, description="Cache TTL in seconds (None = no expiry)"
    )

    # v0.5.0: Admin flag for bypassing permission checks
    is_admin: bool = Field(default=False, description="Whether this instance has admin privileges")

    # Custom namespace configurations
    namespaces: list[dict[str, Any]] | None = Field(
        default=None,
        description="Custom namespace configurations (list of dicts with name, readonly, admin_only, requires_tenant)",
    )

    # Parser configurations (v0.2.0)
    parsers: list[dict[str, Any]] | None = Field(
        default=None,
        description="(Deprecated) Custom parser configurations. Use parse_providers instead.",
    )
    auto_parse: bool = Field(
        default=True,
        description="Automatically parse files on upload (default: True)",
    )

    # Parse provider configurations (v0.8.0)
    # Supports multiple providers: unstructured, llamaparse, markitdown
    # Priority determines selection order (higher = preferred)
    # API keys can be set via environment variables or in config
    parse_providers: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Parse provider configurations. Each dict can contain: "
            "name (required), enabled, priority, api_key, api_url, supported_formats. "
            "Providers: unstructured (API), llamaparse (API), markitdown (local fallback)"
        ),
    )

    # Permission enforcement settings (v0.3.0)
    # P0-6: CHANGED DEFAULT TO TRUE FOR SECURITY
    # Production builds MUST enforce permissions
    enforce_permissions: bool = Field(
        default=True,
        description="Enable permission enforcement on file operations (P0-6: default True for security)",
    )

    # Admin bypass setting (P0-4)
    # Default: True for better developer experience - admin keys bypass permission checks
    # Set to False explicitly for stricter production security
    allow_admin_bypass: bool = Field(
        default=True,
        description="Allow admin keys to bypass permission checks (default True for dev experience)",
    )

    # Workspace and Memory registry (v0.7.0)
    workspaces: list[dict[str, Any]] | None = Field(
        default=None,
        description="Workspace registry configurations (list of dicts with path, name, description, created_by, metadata)",
    )
    memories: list[dict[str, Any]] | None = Field(
        default=None,
        description="Memory registry configurations (list of dicts with path, name, description, created_by, metadata)",
    )

    # Workflow automation settings (v0.7.0)
    enable_workflows: bool = Field(
        default=True,
        description="Enable automatic workflow triggering on file operations (default: True)",
    )

    # Multi-backend storage configuration (v0.9.0)
    backends: list[dict[str, Any]] | None = Field(
        default=None,
        description="Multiple backend mount configurations (type, mount_point, config, priority, readonly)",
    )

    # OAuth provider configuration
    oauth: OAuthConfig | None = Field(
        default=None,
        description="OAuth provider configurations (providers list with name, display_name, provider_class, etc.)",
    )

    # Feature flags (v0.9.0+)
    features: FeaturesConfig = Field(
        default_factory=FeaturesConfig,
        description="Feature flags for optional functionality (semantic search, LLM read, etc.)",
    )

    # Remote mode settings (monolithic/distributed)
    url: str | None = Field(default=None, description="Nexus server URL for remote modes")
    api_key: str | None = Field(default=None, description="API key for authentication")
    timeout: float = Field(default=30.0, description="Request timeout in seconds")

    # Identity settings for memory API (v0.4.0)
    tenant_id: str | None = Field(default=None, description="Tenant ID for memory operations")
    user_id: str | None = Field(default=None, description="User ID for memory operations")
    agent_id: str | None = Field(default=None, description="Agent ID for memory operations")

    # Docker sandbox template configuration
    docker: DockerTemplateConfig = Field(
        default_factory=DockerTemplateConfig,
        description="Docker sandbox template configuration",
    )

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Validate deployment mode."""
        allowed = ["embedded", "monolithic", "distributed"]
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}, got {v}")
        return v

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, v: str) -> str:
        """Validate backend type."""
        allowed = ["local", "gcs", "gcs_connector", "s3_connector", "gdrive_connector"]
        if v not in allowed:
            raise ValueError(f"backend must be one of {allowed}, got {v}")
        return v

    @field_validator("gcs_bucket_name")
    @classmethod
    def validate_gcs_bucket(cls, v: str | None, info: Any) -> str | None:
        """Validate GCS bucket name is provided when backend is gcs."""
        backend = info.data.get("backend")
        if backend == "gcs" and not v:
            # Check if we can get from environment
            env_bucket = os.getenv("NEXUS_GCS_BUCKET_NAME")
            if env_bucket:
                return env_bucket
            raise ValueError("gcs_bucket_name is required when backend='gcs'")
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

    model_config = ConfigDict(
        frozen=False,  # Allow modifications after creation
    )


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

    # Convert oauth dict to OAuthConfig if present
    if "oauth" in merged_dict and isinstance(merged_dict["oauth"], dict):
        from nexus.server.auth.oauth_config import OAuthConfig as OAuthConfigType

        merged_dict["oauth"] = OAuthConfigType(**merged_dict["oauth"])

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
        "NEXUS_BACKEND": "backend",
        "NEXUS_DATA_DIR": "data_dir",
        "NEXUS_GCS_BUCKET_NAME": "gcs_bucket_name",
        "NEXUS_GCS_PROJECT_ID": "gcs_project_id",
        "NEXUS_GCS_CREDENTIALS_PATH": "gcs_credentials_path",
        "NEXUS_CACHE_SIZE_MB": "cache_size_mb",
        "NEXUS_ENABLE_VECTOR_SEARCH": "enable_vector_search",
        "NEXUS_ENABLE_LLM_CACHE": "enable_llm_cache",
        "NEXUS_DB_PATH": "db_path",
        "NEXUS_ENABLE_METADATA_CACHE": "enable_metadata_cache",
        "NEXUS_CACHE_PATH_SIZE": "cache_path_size",
        "NEXUS_CACHE_LIST_SIZE": "cache_list_size",
        "NEXUS_CACHE_KV_SIZE": "cache_kv_size",
        "NEXUS_CACHE_EXISTS_SIZE": "cache_exists_size",
        "NEXUS_CACHE_TTL_SECONDS": "cache_ttl_seconds",
        "NEXUS_AUTO_PARSE": "auto_parse",
        "NEXUS_IS_ADMIN": "is_admin",
        "NEXUS_ENFORCE_PERMISSIONS": "enforce_permissions",
        "NEXUS_ALLOW_ADMIN_BYPASS": "allow_admin_bypass",
        "NEXUS_URL": "url",
        "NEXUS_API_KEY": "api_key",
        "NEXUS_TIMEOUT": "timeout",
        "NEXUS_TENANT_ID": "tenant_id",
        "NEXUS_USER_ID": "user_id",
        "NEXUS_AGENT_ID": "agent_id",
    }

    for env_var, config_key in env_mapping.items():
        value = os.getenv(env_var)
        if value is not None:
            # Type conversion for non-string fields
            converted_value: Any
            if config_key in [
                "cache_size_mb",
                "cache_path_size",
                "cache_list_size",
                "cache_kv_size",
                "cache_exists_size",
            ]:
                converted_value = int(value)
            elif config_key == "timeout":
                converted_value = float(value)
            elif config_key == "cache_ttl_seconds":
                converted_value = int(value) if value.lower() != "none" else None
            elif config_key in [
                "enable_vector_search",
                "enable_llm_cache",
                "enable_metadata_cache",
                "auto_parse",
                "is_admin",
                "enforce_permissions",
                "allow_admin_bypass",
            ]:
                converted_value = value.lower() in ["true", "1", "yes", "on"]
            else:
                converted_value = value
            env_config[config_key] = converted_value

    # Handle NEXUS_PARSERS environment variable
    # Format: "module:class:priority,module:class:priority,..."
    # Example: "my_parsers.csv:CSVParser:60,my_parsers.log:LogParser:50"
    parsers_env = os.getenv("NEXUS_PARSERS")
    if parsers_env:
        parsers_list = []
        for parser_spec in parsers_env.split(","):
            parts = parser_spec.strip().split(":")
            if len(parts) >= 2:
                parser_dict: dict[str, Any] = {
                    "module": parts[0],
                    "class": parts[1],
                    "enabled": True,
                }
                if len(parts) >= 3:
                    parser_dict["priority"] = int(parts[2])
                parsers_list.append(parser_dict)
        if parsers_list:
            env_config["parsers"] = parsers_list

    # Auto-discover parse providers from environment variables
    # UNSTRUCTURED_API_KEY, LLAMA_CLOUD_API_KEY enable respective providers
    parse_providers = []

    # Check for Unstructured.io
    unstructured_key = os.getenv("UNSTRUCTURED_API_KEY")
    if unstructured_key:
        parse_providers.append(
            {
                "name": "unstructured",
                "priority": 100,
                "api_key": unstructured_key,
                "api_url": os.getenv(
                    "UNSTRUCTURED_WORKFLOW_ENDPOINT",
                    "https://api.unstructuredapp.io/general/v0/general",
                ),
            }
        )

    # Check for LlamaParse
    llamaparse_key = os.getenv("LLAMA_CLOUD_API_KEY")
    if llamaparse_key:
        parse_providers.append(
            {
                "name": "llamaparse",
                "priority": 90,
                "api_key": llamaparse_key,
            }
        )

    # Always add MarkItDown as fallback (no API key needed)
    parse_providers.append(
        {
            "name": "markitdown",
            "priority": 10,
        }
    )

    if parse_providers:
        env_config["parse_providers"] = parse_providers

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
