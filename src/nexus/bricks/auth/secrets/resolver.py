"""Secret resolver — scans configs for nexus-secret:NAME patterns and injects values.

Used by PluginRegistry and agent config loading to resolve secret references
into actual decrypted values before execution.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Pattern: nexus-secret:SECRET_NAME
SECRET_PATTERN = re.compile(r"nexus-secret:([A-Za-z0-9_.\-]+)")


class SecretResolver:
    """Resolves nexus-secret:NAME references in configuration dicts/strings.

    Args:
        secrets_service: UserSecretsService instance for value lookups.
        user_id: The user whose secrets to resolve.
        zone_id: The zone scope for secret lookups.
    """

    def __init__(
        self,
        secrets_service: Any,
        user_id: str,
        zone_id: str | None = None,
    ) -> None:
        from nexus.contracts.constants import ROOT_ZONE_ID

        self._service = secrets_service
        self._user_id = user_id
        self._zone_id = zone_id or ROOT_ZONE_ID

    def resolve_string(self, value: str) -> str:
        """Replace all nexus-secret:NAME patterns in a string with decrypted values.

        If a secret is not found, the pattern is left unchanged and a warning is logged.
        """

        def _replacer(match: re.Match[str]) -> str:
            secret_name = match.group(1)
            resolved = self._service.get_secret_value(
                user_id=self._user_id,
                name=secret_name,
                zone_id=self._zone_id,
            )
            if resolved is None:
                logger.warning(
                    "Secret %r not found for user=%s zone=%s",
                    secret_name,
                    self._user_id,
                    self._zone_id,
                )
                return str(match.group(0))  # leave unresolved
            return str(resolved)

        return SECRET_PATTERN.sub(_replacer, value)

    def resolve_config(self, config: Any) -> Any:
        """Recursively resolve nexus-secret:NAME patterns in a config structure.

        Handles dicts, lists, and string values. Non-string leaves are returned as-is.
        """
        if isinstance(config, str):
            if SECRET_PATTERN.search(config):
                return self.resolve_string(config)
            return config
        elif isinstance(config, dict):
            return {k: self.resolve_config(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self.resolve_config(item) for item in config]
        return config

    def has_secrets(self, config: Any) -> bool:
        """Check if a config structure contains any nexus-secret:NAME references."""
        if isinstance(config, str):
            return bool(SECRET_PATTERN.search(config))
        elif isinstance(config, dict):
            return any(self.has_secrets(v) for v in config.values())
        elif isinstance(config, list):
            return any(self.has_secrets(item) for item in config)
        return False
