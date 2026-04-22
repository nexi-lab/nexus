"""Google Calendar connector — PathAddressingEngine + CalendarTransport composition.

Architecture (Transport × Addressing):
    PathCalendarBackend(PathAddressingEngine)
        └── CalendarTransport(Transport)
              ├── Calendar API calls (I/O)
              └── OAuth token from OperationContext

Storage structure:
    /
    ├── primary/                    # User's primary calendar
    │   ├── {event_id}.yaml         # Event file
    │   └── _new.yaml               # Write here to create new event
    └── {calendar_id}/              # Other calendars
        └── {event_id}.yaml
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.connectors.base import (
    CheckpointMixin,
    ConfirmLevel,
    OpTraits,
    ReadmeDocMixin,
    Reversibility,
    TraitBasedMixin,
    ValidatedMixin,
    ValidationError,
)
from nexus.backends.connectors.calendar.errors import ERROR_REGISTRY
from nexus.backends.connectors.calendar.schemas import (
    CreateEventSchema,
    DeleteEventSchema,
    UpdateEventSchema,
)
from nexus.backends.connectors.calendar.transport import CalendarTransport
from nexus.backends.connectors.oauth import OAuthConnectorMixin
from nexus.contracts.backend_features import OAUTH_BACKEND_FEATURES, BackendFeature
from nexus.contracts.constants import IMMUTABLE_VERSION
from nexus.contracts.exceptions import AuthenticationError, BackendError, NexusFileNotFoundError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


@register_connector(
    "calendar_connector",
    description="Google Calendar with OAuth 2.0 authentication (full CRUD)",
    category="oauth",
    requires=["google-api-python-client", "google-auth-oauthlib"],
    service_name="google-calendar",
)
class PathCalendarBackend(
    PathAddressingEngine,
    OAuthConnectorMixin,
    ReadmeDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
    CheckpointMixin,
):
    """Google Calendar connector: PathAddressingEngine + CalendarTransport composition.

    Features:
    - Full CRUD (Create, Read, Update, Delete events)
    - OAuth 2.0 authentication (per-user credentials)
    - Agent-friendly validation with self-correcting errors
    - Checkpoint/rollback for reversible operations
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = OAUTH_BACKEND_FEATURES

    # ReadmeDocMixin config
    SKILL_NAME = "gcalendar"
    README_DIR = ".readme"

    NESTED_EXAMPLES: dict[str, list[str]] = {
        "start": ['dateTime: "2024-01-15T09:00:00-08:00"', "timeZone: America/Los_Angeles"],
        "end": ['dateTime: "2024-01-15T09:00:00-08:00"', "timeZone: America/Los_Angeles"],
        "attendees": ["- email: attendee@example.com"],
    }
    FIELD_EXAMPLES: dict[str, str] = {
        "summary": '"Meeting Title"',
        "description": '"Event description"',
        "location": '"Conference Room A"',
        "visibility": "default  # default, public, private, confidential",
        "colorId": '"1"  # 1-11',
        "recurrence": '["RRULE:FREQ=WEEKLY;BYDAY=MO"]',
        "send_notifications": "true",
    }
    EXAMPLES = {
        "create_meeting.yaml": """# agent_intent: User requested to schedule a team meeting
summary: Weekly Team Sync
description: Weekly sync to discuss project progress and blockers
start:
  dateTime: "2024-01-15T10:00:00-08:00"
  timeZone: America/Los_Angeles
end:
  dateTime: "2024-01-15T11:00:00-08:00"
  timeZone: America/Los_Angeles
location: Conference Room B
attendees:
  - email: alice@example.com
  - email: bob@example.com
reminders:
  - method: popup
    minutes: 15
""",
        "recurring_event.yaml": """# agent_intent: User wants to create a recurring standup meeting
summary: Daily Standup
start:
  dateTime: "2024-01-15T09:00:00-08:00"
  timeZone: America/Los_Angeles
end:
  dateTime: "2024-01-15T09:15:00-08:00"
  timeZone: America/Los_Angeles
recurrence:
  - "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
""",
        "update_event.yaml": """# agent_intent: User wants to change the meeting title
summary: Updated Meeting Title
description: Updated description
""",
        "delete_event.yaml": """# agent_intent: User requested to cancel this meeting
# confirm: true
send_notifications: true
""",
    }

    # ValidatedMixin config
    SCHEMAS = {
        "create_event": CreateEventSchema,
        "update_event": UpdateEventSchema,
        "delete_event": DeleteEventSchema,
    }

    # TraitBasedMixin config
    OPERATION_TRAITS = {
        "create_event": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.INTENT,
            checkpoint=True,
            intent_min_length=10,
        ),
        "update_event": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.INTENT,
            checkpoint=True,
            intent_min_length=10,
        ),
        "delete_event": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.EXPLICIT,
            checkpoint=True,
            intent_min_length=10,
        ),
    }

    ERROR_REGISTRY = ERROR_REGISTRY

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
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
            default="gcalendar",
        ),
        "max_events_per_calendar": ConnectionArg(
            type=ArgType.INTEGER,
            description="Maximum number of events to fetch per calendar",
            required=False,
            default=250,
        ),
    }

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "gcalendar",
        record_store: "RecordStoreABC | None" = None,
        max_events_per_calendar: int = 250,
        metadata_store: Any = None,
        encryption_key: str | None = None,
        pool: Any = None,  # CredentialPool | None — see Issue #3723 for migration guide
    ):
        # 1. Initialize OAuth
        self._pool = pool  # stored for future migrate_to_pool() call (Issue #3723)
        self._init_oauth(
            token_manager_db,
            user_email=user_email,
            provider=provider,
            encryption_key=encryption_key,
        )

        # 2. Create CalendarTransport
        cal_transport = CalendarTransport(
            token_manager=self.token_manager,
            provider=provider,
            user_email=user_email,
            max_events_per_calendar=max_events_per_calendar,
        )
        self._cal_transport = cal_transport

        # 3. Initialize PathAddressingEngine
        PathAddressingEngine.__init__(self, transport=cal_transport, backend_name="gcalendar")

        # 4. Cache and metadata
        self.session_factory = record_store.session_factory if record_store else None
        self.metadata_store = metadata_store
        self.max_events_per_calendar = max_events_per_calendar

        # 5. CheckpointMixin state
        self._checkpoints: dict[str, Any] = {}

        # 6. Register OAuth provider
        self._register_oauth_provider()

    # -- Properties --

    @property
    def user_scoped(self) -> bool:
        return True

    @property
    def has_token_manager(self) -> bool:
        return True

    # -- OAuth provider registration (same as OAuthConnectorBase) --

    _PROVIDER_ALIASES: dict[str, list[str]] = {
        "google": ["gmail", "gcalendar", "google-drive", "google-cloud-storage"],
    }

    def _register_oauth_provider(self) -> None:
        try:
            import importlib as _il

            OAuthProviderFactory = _il.import_module(
                "nexus.bricks.auth.oauth.factory"
            ).OAuthProviderFactory

            factory = OAuthProviderFactory()
            candidates = [self.provider]
            backend_name = getattr(self, "name", "")
            if backend_name and backend_name != self.provider:
                candidates.append(backend_name)
            for alias, targets in self._PROVIDER_ALIASES.items():
                if self.provider == alias:
                    candidates.extend(targets)

            for candidate in candidates:
                try:
                    provider_instance = factory.create_provider(name=candidate)
                    self.token_manager.register_provider(self.provider, provider_instance)
                    logger.info(
                        "Registered OAuth provider '%s' (resolved from '%s') for %s backend",
                        candidate,
                        self.provider,
                        self.name,
                    )
                    return
                except ValueError:
                    continue

            logger.warning(
                "OAuth provider '%s' not available (tried: %s). "
                "OAuth flow must be initiated manually via the Integrations page.",
                self.provider,
                ", ".join(candidates),
            )
        except Exception as e:
            logger.error("Failed to register OAuth provider: %s", e)

    # =================================================================
    # Transport context binding
    # =================================================================

    def _bind_transport(self, context: "OperationContext | None") -> None:
        """Bind the transport to the current request context (OAuth token)."""
        self._transport = self._cal_transport.with_context(context)

    # =================================================================
    # Content operations — override for Calendar CRUD
    # =================================================================

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        if not context or not context.backend_path:
            raise BackendError(
                "Calendar connector requires backend_path in OperationContext.",
                backend="gcalendar",
            )

        self._bind_transport(context)

        # Delegate to PathAddressingEngine → transport.fetch
        return super().read_content(content_id, context)

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Handle create/update with validation + checkpoints."""
        if not context or not context.backend_path:
            raise BackendError(
                "Calendar connector requires backend_path in OperationContext.",
                backend="gcalendar",
            )

        self._bind_transport(context)

        path = context.backend_path.strip("/")
        parts = path.split("/")
        if len(parts) != 2:
            raise BackendError(
                f"Invalid path: {path}. Expected: calendar_id/event_id.yaml",
                backend="gcalendar",
            )

        calendar_id = parts[0]
        filename = parts[1]

        # Parse YAML for validation
        data = CalendarTransport._parse_yaml_content(content)

        if filename == "_new.yaml":
            result = self._create_event(calendar_id, data, context, content)
        elif filename.endswith(".yaml"):
            event_id = filename.removesuffix(".yaml")
            result = self._update_event(calendar_id, event_id, data, context, content)
        else:
            raise BackendError(
                f"Invalid filename: {filename}. Use _new.yaml for create or {{event_id}}.yaml for update.",
                backend="gcalendar",
            )
        return WriteResult(content_id=result, version=result, size=len(content))

    def _create_event(
        self,
        calendar_id: str,
        data: dict[str, Any],
        context: "OperationContext | None",
        raw_content: bytes,
    ) -> str:
        """Validate, checkpoint, then delegate to transport.store()."""
        warnings = self.validate_traits("create_event", data)
        for w in warnings:
            logger.warning("Create event warning: %s", w)
        self.validate_schema("create_event", data)

        checkpoint = self.create_checkpoint("create_event", metadata={"calendar_id": calendar_id})

        try:
            blob_path = self._get_key_path(f"{calendar_id}/_new.yaml")
            event_id = self._transport.store(blob_path, raw_content) or ""

            if checkpoint:
                self.complete_checkpoint(
                    checkpoint.checkpoint_id,
                    {"event_id": event_id, "calendar_id": calendar_id},
                )
            logger.info("Created calendar event: %s", event_id)
            return event_id
        except Exception as e:
            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)
            if isinstance(e, (AuthenticationError, BackendError, NexusFileNotFoundError)):
                raise
            raise BackendError(f"Failed to create event: {e}", backend="gcalendar") from e

    def _update_event(
        self,
        calendar_id: str,
        event_id: str,
        data: dict[str, Any],
        context: "OperationContext | None",
        raw_content: bytes,
    ) -> str:
        """Validate, checkpoint with previous state, then delegate to transport.store()."""
        warnings = self.validate_traits("update_event", data)
        for w in warnings:
            logger.warning("Update event warning: %s", w)
        self.validate_schema("update_event", data)

        # Fetch current event for checkpoint
        try:
            blob_path = self._get_key_path(f"{calendar_id}/{event_id}.yaml")
            current_bytes, _ = self._transport.fetch(blob_path)
        except Exception as e:
            raise ValidationError(
                code="EVENT_NOT_FOUND",
                message=f"Event {event_id} not found in calendar {calendar_id}",
                readme_path=self.readme_md_path,
                readme_section="operations",
            ) from e

        import yaml as _yaml

        current_event = _yaml.safe_load(current_bytes) or {}

        checkpoint = self.create_checkpoint(
            "update_event",
            previous_state=current_event,
            metadata={"calendar_id": calendar_id, "event_id": event_id},
        )

        try:
            self._transport.store(blob_path, raw_content)
            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)
            logger.info("Updated calendar event: %s", event_id)
            return "updated"
        except Exception as e:
            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)
            if isinstance(e, (AuthenticationError, BackendError, NexusFileNotFoundError)):
                raise
            raise BackendError(f"Failed to update event: {e}", backend="gcalendar") from e

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        if not context or not context.backend_path:
            raise BackendError(
                "Calendar connector requires backend_path in OperationContext.",
                backend="gcalendar",
            )

        self._bind_transport(context)

        path = context.backend_path.strip("/")
        parts = path.split("/")
        if len(parts) != 2 or not parts[1].endswith(".yaml"):
            raise BackendError(f"Invalid path for delete: {path}", backend="gcalendar")

        calendar_id = parts[0]
        event_id = parts[1].removesuffix(".yaml")

        logger.info("delete_content called for event %s in calendar %s", event_id, calendar_id)

        # Fetch current event for checkpoint
        blob_path = self._get_key_path(f"{calendar_id}/{event_id}.yaml")
        try:
            current_bytes, _ = self._transport.fetch(blob_path)
        except Exception as e:
            raise ValidationError(
                code="EVENT_NOT_FOUND",
                message=f"Event {event_id} not found",
                readme_path=self.readme_md_path,
            ) from e

        import yaml as _yaml

        current_event = _yaml.safe_load(current_bytes) or {}

        checkpoint = self.create_checkpoint(
            "delete_event",
            previous_state=current_event,
            metadata={"calendar_id": calendar_id, "event_id": event_id},
        )

        try:
            self._transport.remove(blob_path)
            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)
            logger.info("Deleted calendar event: %s", event_id)
        except Exception as e:
            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)
            if isinstance(e, (AuthenticationError, BackendError, NexusFileNotFoundError)):
                raise
            raise BackendError(f"Failed to delete event: {e}", backend="gcalendar") from e

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        if not context or not context.backend_path:
            return False
        self._bind_transport(context)
        return super().content_exists(content_id, context)

    def get_content_size(self, content_id: str, context: "OperationContext | None" = None) -> int:
        if not context:
            return 0

        self._bind_transport(context)
        return super().get_content_size(content_id, context)

    # =================================================================
    # Version support
    # =================================================================

    def get_version(self, path: str, context: "OperationContext | None" = None) -> str | None:
        try:
            backend_path = (
                context.backend_path
                if context and hasattr(context, "backend_path") and context.backend_path
                else path.lstrip("/")
            )
            if not backend_path.endswith(".yaml"):
                return None
            if len(backend_path.split("/")) != 2:
                return None
            return IMMUTABLE_VERSION
        except Exception:
            return None

    # =================================================================
    # Directory operations — override for Calendar virtual directories
    # =================================================================

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        path = path.strip("/")
        if not path:
            return True
        return len(path.split("/")) == 1

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        try:
            path = path.strip("/")
            self._bind_transport(context)

            if not path:
                # Root → list calendars
                _keys, prefixes = self._transport.list_keys(prefix="", delimiter="/")
                return prefixes

            # Calendar → list events
            keys, _prefixes = self._transport.list_keys(prefix=path, delimiter="/")
            cal_prefix = f"{path}/"
            files = []
            for key in keys:
                name = key[len(cal_prefix) :] if key.startswith(cal_prefix) else key
                if name:
                    files.append(name)
            # Event filenames are `{YYYY-MM-DD}_{summary}__{eventId}.yaml`,
            # so reverse-lex = reverse-chronological = newest first — matches
            # what users see in every calendar UI.
            return sorted(files, reverse=True)

        except (FileNotFoundError, NotADirectoryError):
            raise
        except AuthenticationError:
            raise
        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="gcalendar",
                path=path,
            ) from e

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "Cannot create calendars via Nexus. Use Google Calendar to create new calendars.",
            backend="gcalendar",
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "Cannot delete calendars via Nexus. Use Google Calendar to manage calendars.",
            backend="gcalendar",
        )


# Backward-compatibility alias: persisted mounts that stored backend_type
# "gcalendar_connector" (the name used before this rename) must still mount.
# Both names resolve to the same class. Remove this alias after one release cycle.
from nexus.backends.base.registry import ConnectorRegistry  # noqa: E402

ConnectorRegistry.register(
    name="gcalendar_connector",
    connector_class=PathCalendarBackend,
    description="Google Calendar (deprecated alias — use calendar_connector)",
    category="oauth",
    requires=["google-api-python-client", "google-auth-oauthlib"],
    service_name="google-calendar",
)
