"""YAML config loader with mtime-based cache invalidation.

Loads validators.yaml files from sandbox workspaces and caches parsed
configurations to avoid repeated YAML parsing overhead.
"""

import logging
import os
from hashlib import sha256
from typing import Any

from nexus.bricks.parsers.validation.models import ValidationPipelineConfig, ValidatorConfig

logger = logging.getLogger(__name__)


def _parse_yaml_content(content: str) -> dict[str, Any]:
    """Parse YAML content string into a dict.

    Uses PyYAML safe_load. Returns empty dict on parse failure.
    """
    try:
        import yaml

        result = yaml.safe_load(content)
        return result if isinstance(result, dict) else {}
    except Exception as e:
        logger.warning("Failed to parse YAML content: %s", e)
        return {}


class ValidatorConfigLoader:
    """Loads validators.yaml with mtime-based cache invalidation."""

    def __init__(self) -> None:
        self._cache: dict[str, ValidationPipelineConfig] = {}
        self._content_hashes: dict[str, str] = {}
        self._mtimes: dict[str, float] = {}

    def load_from_string(self, content: str, cache_key: str = "") -> ValidationPipelineConfig:
        """Parse a YAML string into a ValidationPipelineConfig.

        Args:
            content: Raw YAML content.
            cache_key: Optional key for caching. If provided, the result
                is cached under this key for subsequent lookups.

        Returns:
            Parsed pipeline configuration.
        """
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        if (
            cache_key
            and cache_key in self._cache
            and self._content_hashes.get(cache_key) == content_hash
        ):
            return self._cache[cache_key]

        data = _parse_yaml_content(content)
        config = self._build_config(data)

        if cache_key:
            self._cache[cache_key] = config
            self._content_hashes[cache_key] = content_hash

        return config

    def load_from_file(self, config_path: str) -> ValidationPipelineConfig:
        """Load config from a local file path with mtime caching.

        Args:
            config_path: Absolute path to validators.yaml.

        Returns:
            Parsed pipeline configuration.
        """
        if not os.path.isfile(config_path):
            return ValidationPipelineConfig()

        current_mtime = os.path.getmtime(config_path)

        if config_path in self._cache and self._mtimes.get(config_path) == current_mtime:
            return self._cache[config_path]

        with open(config_path) as f:
            content = f.read()

        config = self.load_from_string(content, cache_key=config_path)
        self._cache[config_path] = config
        self._mtimes[config_path] = current_mtime
        return config

    def invalidate(self, config_path: str | None = None) -> None:
        """Invalidate cached config.

        Args:
            config_path: Specific path to invalidate, or None for all.
        """
        if config_path is None:
            self._cache.clear()
            self._content_hashes.clear()
            self._mtimes.clear()
        else:
            self._cache.pop(config_path, None)
            self._content_hashes.pop(config_path, None)
            self._mtimes.pop(config_path, None)

    def _build_config(self, data: dict[str, Any]) -> ValidationPipelineConfig:
        """Build a ValidationPipelineConfig from parsed YAML data."""
        validators_data = data.get("validators", [])
        if not isinstance(validators_data, list):
            logger.warning("'validators' key is not a list, ignoring")
            return ValidationPipelineConfig()

        validators: list[ValidatorConfig] = []
        for v in validators_data:
            if not isinstance(v, dict):
                continue
            if "name" not in v or "command" not in v:
                logger.warning("Validator entry missing 'name' or 'command': %s", v)
                continue
            try:
                validators.append(ValidatorConfig(**v))
            except Exception as e:
                logger.warning("Invalid validator config %s: %s", v, e)

        return ValidationPipelineConfig(
            validators=validators,
            auto_run=data.get("auto_run", True),
            max_total_timeout=data.get("max_total_timeout", 30),
        )
