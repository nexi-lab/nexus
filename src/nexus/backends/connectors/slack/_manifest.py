"""Slack connector manifest (extension store discovery).

Imported by ``nexus.extensions.store`` for metadata-only enumeration without
loading slack_sdk or the Slack connector implementation. Runtime mounting
continues to use ``nexus.backends._manifest.CONNECTOR_MANIFEST`` during the
#3964 pilot.
"""

from __future__ import annotations

from nexus.extensions.manifest import ConnectorManifest, RuntimeDep
from nexus.extensions.types import ArgType, ConnectionArg

MANIFEST = ConnectorManifest(
    name="slack_connector",
    module="nexus.backends.connectors.slack.connector",
    factory="PathSlackBackend",
    description="Slack workspace with OAuth 2.0 authentication",
    service_name="slack",
    runtime_deps=(
        RuntimeDep(
            kind="python",
            name="slack-sdk",
            extras=("slack",),
            install_hint="pip install nexus-fs[slack]",
        ),
        RuntimeDep(kind="service", name="token_manager"),
    ),
    import_probes=("slack_sdk",),
    capabilities=frozenset({"user_scoped", "token_manager", "oauth", "readme_doc"}),
    connection_args={
        "token_manager_db": ConnectionArg(
            type=ArgType.PATH,
            description="Path to TokenManager database or database URL",
            required=True,
        ),
        "user_email": ConnectionArg(
            type=ArgType.STRING,
            description="User email for OAuth lookup (None for multi-user from context)",
            required=False,
        ),
        "provider": ConnectionArg(
            type=ArgType.STRING,
            description="OAuth provider name from config",
            required=False,
            default="slack",
        ),
        "max_messages_per_channel": ConnectionArg(
            type=ArgType.INTEGER,
            description="Maximum number of messages to fetch per channel",
            required=False,
            default=100,
        ),
    },
    user_scoped=True,
)
