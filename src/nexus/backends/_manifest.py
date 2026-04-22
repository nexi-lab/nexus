"""Static declaration of every built-in connector (Issue #3830 sub-project A.3).

This manifest is read BEFORE any connector module is imported. Each entry
becomes a placeholder ``ConnectorInfo`` in the registry. If the connector
module then imports successfully, its ``@register_connector`` call binds
the real class into the placeholder, preserving manifest metadata. If the
module fails to import, the placeholder stays — and
``BackendFactory.create()`` raises ``MissingDependencyError`` with the
manifest's install hints instead of the pre-A.3 ``RuntimeError("Unsupported
backend type")``.

Adding a new built-in connector:
    1. Add an entry to ``CONNECTOR_MANIFEST`` here.
    2. Write the connector class; decorate with ``@register_connector("name")``
       — name only, no metadata kwargs (those come from this manifest).

External plugins (entry-point based) are NOT listed here; they register
via the regular decorator path and go through the no-existing-entry branch
of ``ConnectorRegistry.register()``.
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus.backends.base.runtime_deps import (
    BinaryDep,
    PythonDep,
    RuntimeDep,
    ServiceDep,
)


@dataclass(frozen=True, slots=True)
class ConnectorManifestEntry:
    """Static declaration of one built-in connector."""

    name: str
    """Registry key (what users mount with, e.g. ``"path_gcs"``)."""

    module_path: str
    """Dotted import path for the connector module."""

    class_name: str
    """Class name exposed from the module (for legacy ``__getattr__`` lookup)."""

    description: str
    """One-line description shown in ``nexus connectors list``."""

    category: str
    """Grouping key (``"storage"``, ``"oauth"``, ``"cli"``, ``"compute"``, ...)."""

    runtime_deps: tuple[RuntimeDep, ...] = ()
    """Typed deps checked at mount time."""

    service_name: str | None = None
    """Unified service name for ``service_map.py`` integration."""


_GWS_RUNTIME_DEPS: tuple[RuntimeDep, ...] = (
    BinaryDep("gws", "brew install nexi-lab/tap/gws"),
    ServiceDep("token_manager"),
)

_GH_RUNTIME_DEPS: tuple[RuntimeDep, ...] = (
    BinaryDep("gh", "brew install gh"),
    ServiceDep("token_manager"),
)

CONNECTOR_MANIFEST: tuple[ConnectorManifestEntry, ...] = (
    # --- Storage ---
    ConnectorManifestEntry(
        name="path_gcs",
        module_path="nexus.backends.storage.path_gcs",
        class_name="PathGCSBackend",
        description="Google Cloud Storage with direct path mapping",
        category="storage",
        runtime_deps=(PythonDep("google.cloud.storage", extras=("gcs",)),),
        service_name="gcs",
    ),
    ConnectorManifestEntry(
        name="cas_gcs",
        module_path="nexus.backends.storage.cas_gcs",
        class_name="CASGCSBackend",
        description="Google Cloud Storage with CAS deduplication",
        category="storage",
        runtime_deps=(PythonDep("google.cloud.storage", extras=("gcs",)),),
    ),
    ConnectorManifestEntry(
        name="path_s3",
        module_path="nexus.backends.storage.path_s3",
        class_name="PathS3Backend",
        description="AWS S3 with direct path mapping",
        category="storage",
        runtime_deps=(PythonDep("boto3", extras=("s3",)),),
        service_name="s3",
    ),
    ConnectorManifestEntry(
        name="path_local",
        module_path="nexus.backends.storage.path_local",
        class_name="PathLocalBackend",
        description="Local filesystem with direct path mapping (no CAS)",
        category="storage",
    ),
    ConnectorManifestEntry(
        name="cas_local",
        module_path="nexus.backends.storage.cas_local",
        class_name="CASLocalBackend",
        description="Local filesystem with CAS deduplication (new architecture)",
        category="storage",
    ),
    ConnectorManifestEntry(
        name="local_connector",
        module_path="nexus.backends.storage.local_connector",
        class_name="LocalConnectorBackend",
        description="Mount local folder into Nexus (reference mode, no copy)",
        category="storage",
    ),
    # --- OAuth / API connectors ---
    # Every OAuth connector uses OAuthConnectorMixin / OAuthConnectorBase,
    # which lazily imports ``nexus.bricks.auth.oauth.factory`` at
    # instantiation time (see src/nexus/backends/connectors/oauth_base.py).
    # ``nexus.bricks`` is excluded from the slim wheel, so declaring
    # ``ServiceDep("token_manager")`` here gates the mount with a clean
    # ``MissingDependencyError`` on slim instead of leaking a raw
    # ModuleNotFoundError from deep inside the OAuth factory call.
    ConnectorManifestEntry(
        name="gdrive_connector",
        module_path="nexus.backends.connectors.gdrive.connector",
        class_name="PathGDriveBackend",
        description="Google Drive with OAuth 2.0 authentication",
        category="oauth",
        runtime_deps=(
            PythonDep("googleapiclient", extras=("gdrive",)),
            PythonDep("google_auth_oauthlib", extras=("gdrive",)),
            ServiceDep("token_manager"),
        ),
        service_name="google-drive",
    ),
    ConnectorManifestEntry(
        name="gmail_connector",
        module_path="nexus.backends.connectors.gmail.connector",
        class_name="PathGmailBackend",
        description="Gmail with OAuth 2.0 authentication (send, reply, forward, draft, trash)",
        category="oauth",
        runtime_deps=(
            PythonDep("googleapiclient", extras=("gmail",)),
            PythonDep("google_auth_oauthlib", extras=("gmail",)),
            ServiceDep("token_manager"),
        ),
        service_name="gmail",
    ),
    ConnectorManifestEntry(
        name="calendar_connector",
        module_path="nexus.backends.connectors.calendar.connector",
        class_name="PathCalendarBackend",
        description="Google Calendar with OAuth 2.0 authentication (full CRUD)",
        category="oauth",
        runtime_deps=(
            PythonDep("googleapiclient", extras=("gcalendar",)),
            PythonDep("google_auth_oauthlib", extras=("gcalendar",)),
            ServiceDep("token_manager"),
        ),
        service_name="google-calendar",
    ),
    ConnectorManifestEntry(
        name="gcalendar_connector",
        module_path="nexus.backends.connectors.calendar.connector",
        class_name="PathCalendarBackend",
        description="Google Calendar (deprecated alias — use calendar_connector)",
        category="oauth",
        runtime_deps=(
            PythonDep("googleapiclient", extras=("gcalendar",)),
            PythonDep("google_auth_oauthlib", extras=("gcalendar",)),
            ServiceDep("token_manager"),
        ),
        service_name="google-calendar",
    ),
    ConnectorManifestEntry(
        name="x_connector",
        module_path="nexus.backends.connectors.x.connector",
        class_name="PathXBackend",
        description="X (Twitter) API with OAuth 2.0 PKCE",
        category="api",
        runtime_deps=(
            PythonDep("requests_oauthlib", extras=("x",)),
            ServiceDep("token_manager"),
        ),
        service_name="x",
    ),
    ConnectorManifestEntry(
        name="slack_connector",
        module_path="nexus.backends.connectors.slack.connector",
        class_name="PathSlackBackend",
        description="Slack workspace with OAuth 2.0 authentication",
        category="oauth",
        runtime_deps=(
            PythonDep("slack_sdk", extras=("slack",)),
            ServiceDep("token_manager"),
        ),
        service_name="slack",
    ),
    ConnectorManifestEntry(
        name="hn_connector",
        module_path="nexus.backends.connectors.hn.connector",
        class_name="PathHNBackend",
        description="HackerNews API (read-only)",
        category="api",
        service_name="hackernews",
    ),
    # --- Compute ---
    ConnectorManifestEntry(
        name="anthropic_native",
        module_path="nexus.backends.compute.anthropic_native",
        class_name="CASAnthropicBackend",
        description="Native Anthropic Claude API (direct SDK, no translation)",
        category="compute",
        runtime_deps=(PythonDep("anthropic", extras=("anthropic",)),),
    ),
    ConnectorManifestEntry(
        name="openai_compatible",
        module_path="nexus.backends.compute.openai_compatible",
        class_name="CASOpenAIBackend",
        description="OpenAI-compatible LLM API (OpenAI, SudoRouter, OpenRouter, Ollama)",
        category="compute",
        runtime_deps=(PythonDep("openai", extras=("openai",)),),
    ),
    # --- CLI-backed connectors (gws family) ---
    ConnectorManifestEntry(
        name="gws_gmail",
        module_path="nexus.backends.connectors.gws.connector",
        class_name="GmailConnector",
        description="Gmail via gws CLI",
        category="cli",
        runtime_deps=_GWS_RUNTIME_DEPS,
        service_name="gws",
    ),
    ConnectorManifestEntry(
        name="gws_calendar",
        module_path="nexus.backends.connectors.gws.connector",
        class_name="CalendarConnector",
        description="Google Calendar via gws CLI",
        category="cli",
        runtime_deps=_GWS_RUNTIME_DEPS,
        service_name="gws",
    ),
    ConnectorManifestEntry(
        name="gws_sheets",
        module_path="nexus.backends.connectors.gws.connector",
        class_name="SheetsConnector",
        description="Google Sheets via gws CLI",
        category="cli",
        runtime_deps=_GWS_RUNTIME_DEPS,
        service_name="gws",
    ),
    ConnectorManifestEntry(
        name="gws_docs",
        module_path="nexus.backends.connectors.gws.connector",
        class_name="DocsConnector",
        description="Google Docs via gws CLI",
        category="cli",
        runtime_deps=_GWS_RUNTIME_DEPS,
        service_name="gws",
    ),
    ConnectorManifestEntry(
        name="gws_chat",
        module_path="nexus.backends.connectors.gws.connector",
        class_name="ChatConnector",
        description="Google Chat via gws CLI",
        category="cli",
        runtime_deps=_GWS_RUNTIME_DEPS,
        service_name="gws",
    ),
    ConnectorManifestEntry(
        name="gws_drive",
        module_path="nexus.backends.connectors.gws.connector",
        class_name="DriveConnector",
        description="Google Drive via gws CLI",
        category="cli",
        runtime_deps=_GWS_RUNTIME_DEPS,
        service_name="gws",
    ),
    # --- GitHub CLI ---
    ConnectorManifestEntry(
        name="github_connector",
        module_path="nexus.backends.connectors.github.connector",
        class_name="GitHubConnector",
        description="GitHub via gh CLI",
        category="cli",
        runtime_deps=_GH_RUNTIME_DEPS,
    ),
    ConnectorManifestEntry(
        name="gws_github",
        module_path="nexus.backends.connectors.github.connector",
        class_name="GitHubConnector",
        description="GitHub via gh CLI (deprecated alias, use github_connector)",
        category="cli",
        runtime_deps=_GH_RUNTIME_DEPS,
    ),
)


__all__ = ["CONNECTOR_MANIFEST", "ConnectorManifestEntry"]
