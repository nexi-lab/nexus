"""Google Calendar connector backend with OAuth 2.0 authentication.

This connector provides full CRUD access to Google Calendar events,
organizing them as YAML files in a virtual filesystem.

Use case: Access and manage Google Calendar events through Nexus mount.

Storage structure:
    /
    ├── primary/                    # User's primary calendar
    │   ├── {event_id}.yaml         # Event file
    │   └── _new.yaml               # Write here to create new event
    └── {calendar_id}/              # Other calendars
        └── {event_id}.yaml

Key features:
- OAuth 2.0 authentication (user-scoped)
- Full CRUD operations (Create, Read, Update, Delete)
- Agent-friendly validation with self-correcting errors
- Checkpoint/rollback support for reversible operations
- Auto-generated SKILL.md documentation
- Database-backed caching via CacheConnectorMixin

Authentication:
    Uses OAuth 2.0 flow via TokenManager:
    - User authorizes via browser
    - Tokens stored encrypted in database
    - Automatic refresh when expired
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

from nexus.backends.backend import Backend
from nexus.backends.cache_mixin import IMMUTABLE_VERSION, CacheConnectorMixin
from nexus.connectors.base import (
    CheckpointMixin,
    ConfirmLevel,
    OpTraits,
    Reversibility,
    SkillDocMixin,
    TraitBasedMixin,
    ValidatedMixin,
    ValidationError,
)
from nexus.connectors.calendar.errors import ERROR_REGISTRY
from nexus.connectors.calendar.schemas import (
    CreateEventSchema,
    DeleteEventSchema,
    UpdateEventSchema,
)
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.response import HandlerResponse

# Suppress annoying googleapiclient discovery cache warnings
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class GoogleCalendarConnectorBackend(
    Backend,
    CacheConnectorMixin,
    SkillDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
    CheckpointMixin,
):
    """Google Calendar connector backend with full CRUD support.

    This backend syncs events from Google Calendar API and organizes them
    as YAML files. Supports create, read, update, and delete operations
    with agent-friendly validation.

    Features:
    - OAuth 2.0 authentication (per-user credentials)
    - Event syncing from calendars
    - CRUD operations with validation
    - Checkpoint/rollback for reversible operations
    - Auto-generated SKILL.md documentation

    Folder Structure:
    - / - Root directory (lists calendars)
    - /primary/ - User's primary calendar
    - /{calendar_id}/ - Other calendars
    - /{calendar_id}/{event_id}.yaml - Event files
    - /{calendar_id}/_new.yaml - Write here to create new event
    """

    # =========================================================================
    # Mixin Configuration
    # =========================================================================

    # SkillDocMixin config
    SKILL_NAME = "gcalendar"
    SKILL_DIR = ".skill"  # Will be at <mount_path>/.skill/

    # Example YAML files for agents
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
            reversibility=Reversibility.FULL,  # Can restore from trash
            confirm=ConfirmLevel.EXPLICIT,  # Requires confirm: true
            checkpoint=True,
            intent_min_length=10,
        ),
    }

    # Error registry for self-correcting messages
    ERROR_REGISTRY = ERROR_REGISTRY

    # Enable metadata-based listing for fast database queries
    use_metadata_listing = True

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "gcalendar",
        session_factory: Any = None,
        max_events_per_calendar: int = 250,
        metadata_store: Any = None,
    ):
        """Initialize Google Calendar connector backend.

        Args:
            token_manager_db: Path to TokenManager database (e.g., ~/.nexus/nexus.db)
            user_email: Optional user email for OAuth lookup. If None, uses authenticated
                       user from OperationContext (recommended for multi-user scenarios)
            provider: OAuth provider name from config (default: "gcalendar")
            session_factory: SQLAlchemy session factory for content caching (optional).
                           If provided, enables persistent caching for fast grep/search.
            max_events_per_calendar: Maximum number of events to fetch per calendar (default: 250).
            metadata_store: FileMetadataProtocol instance for writing to file_paths table (optional).

        Note:
            For single-user scenarios (demos), set user_email explicitly.
            For multi-user production, leave user_email=None to auto-detect from context.
        """
        from nexus.server.auth.token_manager import TokenManager

        # Store original token_manager_db for config updates
        self.token_manager_db = token_manager_db

        # Resolve database URL using base class method
        resolved_db = self.resolve_database_url(token_manager_db)

        # Support both file paths and database URLs
        if resolved_db.startswith(("postgresql://", "sqlite://", "mysql://")):
            self.token_manager = TokenManager(db_url=resolved_db)
        else:
            self.token_manager = TokenManager(db_path=resolved_db)

        self.user_email = user_email
        self.provider = provider
        self.session_factory = session_factory
        self.max_events_per_calendar = max_events_per_calendar
        self.metadata_store = metadata_store

        # Register OAuth provider
        self._register_oauth_provider()

    def _register_oauth_provider(self) -> None:
        """Register OAuth provider with TokenManager using OAuthProviderFactory."""
        try:
            from nexus.server.auth.oauth_factory import OAuthProviderFactory

            factory = OAuthProviderFactory()

            try:
                provider_instance = factory.create_provider(name=self.provider)
                self.token_manager.register_provider(self.provider, provider_instance)
                logger.info(f"Registered OAuth provider '{self.provider}' for Calendar backend")
            except ValueError as e:
                logger.warning(
                    f"OAuth provider '{self.provider}' not available: {e}. "
                    "OAuth flow must be initiated manually via the Integrations page."
                )
        except Exception as e:
            logger.error(f"Failed to register OAuth provider: {e}")

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "gcalendar"

    @property
    def user_scoped(self) -> bool:
        """This backend requires per-user OAuth credentials."""
        return True

    # --- Capability flags ---

    @property
    def has_token_manager(self) -> bool:
        """GCalendar connector manages OAuth tokens."""
        return True

    # =========================================================================
    # OAuth / Service
    # =========================================================================

    def _get_calendar_service(self, context: OperationContext | None = None) -> Resource:
        """Get Google Calendar service with user's OAuth credentials.

        Args:
            context: Operation context (provides user_id if user_email not configured)

        Returns:
            Calendar service instance

        Raises:
            BackendError: If credentials not found or user not authenticated
        """
        try:
            from googleapiclient.discovery import build
        except ImportError:
            raise BackendError(
                "google-api-python-client not installed. "
                "Install with: pip install google-api-python-client",
                backend="gcalendar",
            ) from None

        # Determine which user's tokens to use
        if self.user_email:
            user_email = self.user_email
        elif context and context.user_id:
            user_email = context.user_id
        else:
            raise BackendError(
                "Calendar backend requires either configured user_email "
                "or authenticated user in OperationContext",
                backend="gcalendar",
            )

        # Get valid access token from TokenManager
        from nexus.core.sync_bridge import run_sync

        try:
            zone_id = (
                context.zone_id
                if context and hasattr(context, "zone_id") and context.zone_id
                else "default"
            )

            access_token = run_sync(
                self.token_manager.get_valid_token(
                    provider=self.provider,
                    user_email=user_email,
                    zone_id=zone_id,
                )
            )
        except Exception as e:
            raise BackendError(
                f"Failed to get valid OAuth token for user {user_email}: {e}",
                backend="gcalendar",
            ) from e

        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        return build("calendar", "v3", credentials=creds)

    # =========================================================================
    # YAML Formatting
    # =========================================================================

    def _format_event_as_yaml(self, event: dict[str, Any]) -> bytes:
        """Format calendar event as YAML bytes.

        Args:
            event: Event data from Google Calendar API

        Returns:
            Formatted YAML as bytes
        """
        # Extract relevant fields
        yaml_data = {
            "id": event.get("id"),
            "summary": event.get("summary", "(No title)"),
            "description": event.get("description"),
            "location": event.get("location"),
            "start": event.get("start"),
            "end": event.get("end"),
            "created": event.get("created"),
            "updated": event.get("updated"),
            "status": event.get("status"),
            "organizer": event.get("organizer"),
            "attendees": event.get("attendees"),
            "recurrence": event.get("recurrence"),
            "recurringEventId": event.get("recurringEventId"),
            "htmlLink": event.get("htmlLink"),
        }

        # Remove None values
        yaml_data = {k: v for k, v in yaml_data.items() if v is not None}

        yaml_output: str = yaml.dump(
            yaml_data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        return yaml_output.encode("utf-8")

    def _parse_yaml_content(self, content: bytes) -> dict[str, Any]:
        """Parse YAML content from agent request.

        Extracts agent_intent and confirm from comments.

        Args:
            content: YAML content as bytes

        Returns:
            Parsed data dict including agent_intent and confirm
        """
        text = content.decode("utf-8")
        lines = text.split("\n")

        data: dict[str, Any] = {}

        # Extract comments (agent_intent, confirm, user_confirmed)
        for line in lines:
            line = line.strip()
            if line.startswith("# agent_intent:"):
                data["agent_intent"] = line.replace("# agent_intent:", "").strip()
            elif line.startswith("# confirm:"):
                value = line.replace("# confirm:", "").strip().lower()
                data["confirm"] = value == "true"
            elif line.startswith("# user_confirmed:"):
                value = line.replace("# user_confirmed:", "").strip().lower()
                data["user_confirmed"] = value == "true"

        # Parse YAML body
        yaml_content = yaml.safe_load(text) or {}
        if isinstance(yaml_content, dict):
            data.update(yaml_content)

        return data

    # =========================================================================
    # Backend Interface - Read Operations
    # =========================================================================

    def read_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bytes]:
        """Read event content from cache or Google Calendar API.

        Args:
            content_hash: Ignored for connector backends
            context: Operation context with backend_path

        Returns:
            HandlerResponse with event content as YAML bytes

        Raises:
            NexusFileNotFoundError: If event doesn't exist
            BackendError: If read operation fails
        """
        _ = content_hash  # Unused for connector backends
        if not context or not context.backend_path:
            raise BackendError(
                "Calendar connector requires backend_path in OperationContext.",
                backend="gcalendar",
            )

        path = context.backend_path.strip("/")
        parts = path.split("/")

        # Must be calendar_id/event_id.yaml
        if len(parts) != 2 or not parts[1].endswith(".yaml"):
            raise NexusFileNotFoundError(context.backend_path)

        calendar_id = parts[0]
        event_id = parts[1].replace(".yaml", "")

        # Skip special files
        if event_id.startswith("_"):
            raise NexusFileNotFoundError(context.backend_path)

        # Check cache first
        cache_path = self._get_cache_path(context) or context.backend_path
        if self._has_caching():
            cached = self._read_from_cache(cache_path, original=True)
            if cached and not cached.stale and cached.content_binary:
                return HandlerResponse.ok(
                    data=cached.content_binary,
                    backend_name="gcalendar",
                    path=context.backend_path,
                )

        # Fetch from Google Calendar API
        try:
            service = self._get_calendar_service(context)
            event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as e:
            raise NexusFileNotFoundError(context.backend_path) from e

        content = self._format_event_as_yaml(event)

        # Cache the result
        if self._has_caching():
            with suppress(Exception):
                zone_id = getattr(context, "zone_id", None)
                self._write_to_cache(
                    path=cache_path,
                    content=content,
                    backend_version=event.get("etag", IMMUTABLE_VERSION),
                    zone_id=zone_id,
                )

        return HandlerResponse.ok(
            data=content,
            backend_name="gcalendar",
            path=context.backend_path,
        )

    # =========================================================================
    # Backend Interface - Write Operations (CUD)
    # =========================================================================

    def write_content(
        self, content: bytes, context: OperationContext | None = None
    ) -> HandlerResponse[str]:
        """Write event content - handles create and update.

        For create: Write to /{calendar_id}/_new.yaml
        For update: Write to /{calendar_id}/{event_id}.yaml

        Args:
            content: YAML content with event data
            context: Operation context with backend_path

        Returns:
            HandlerResponse with Event ID (for create) or "updated" (for update)

        Raises:
            ValidationError: If validation fails
            BackendError: If operation fails
        """
        if not context or not context.backend_path:
            raise BackendError(
                "Calendar connector requires backend_path in OperationContext.",
                backend="gcalendar",
            )

        path = context.backend_path.strip("/")
        parts = path.split("/")

        if len(parts) != 2:
            raise BackendError(
                f"Invalid path: {path}. Expected: calendar_id/event_id.yaml",
                backend="gcalendar",
            )

        calendar_id = parts[0]
        filename = parts[1]

        # Parse content
        data = self._parse_yaml_content(content)

        # Determine operation
        if filename == "_new.yaml":
            result = self._create_event(calendar_id, data, context)
        elif filename.endswith(".yaml"):
            event_id = filename.replace(".yaml", "")
            result = self._update_event(calendar_id, event_id, data, context)
        else:
            raise BackendError(
                f"Invalid filename: {filename}. Use _new.yaml for create or {{event_id}}.yaml for update.",
                backend="gcalendar",
            )

        return HandlerResponse.ok(
            data=result,
            backend_name="gcalendar",
            path=context.backend_path,
        )

    def _create_event(
        self, calendar_id: str, data: dict[str, Any], context: OperationContext | None
    ) -> str:
        """Create a new calendar event.

        Args:
            calendar_id: Calendar ID
            data: Event data
            context: Operation context

        Returns:
            Created event ID

        Raises:
            ValidationError: If validation fails
        """
        # Validate traits
        warnings = self.validate_traits("create_event", data)
        for warning in warnings:
            logger.warning(f"Create event warning: {warning}")

        # Validate schema
        validated = self.validate_schema("create_event", data)

        # Create checkpoint
        checkpoint = self.create_checkpoint("create_event", metadata={"calendar_id": calendar_id})

        # Build event body for API
        event_body = self._build_event_body(validated)

        try:
            service = self._get_calendar_service(context)
            created_event = (
                service.events().insert(calendarId=calendar_id, body=event_body).execute()
            )

            event_id: str = created_event.get("id", "")

            # Complete checkpoint with created state
            if checkpoint:
                self.complete_checkpoint(
                    checkpoint.checkpoint_id,
                    {"event_id": event_id, "calendar_id": calendar_id},
                )

            logger.info(f"Created calendar event: {event_id}")
            return event_id

        except Exception as e:
            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)
            raise BackendError(f"Failed to create event: {e}", backend="gcalendar") from e

    def _update_event(
        self,
        calendar_id: str,
        event_id: str,
        data: dict[str, Any],
        context: OperationContext | None,
    ) -> str:
        """Update an existing calendar event.

        Args:
            calendar_id: Calendar ID
            event_id: Event ID
            data: Update data
            context: Operation context

        Returns:
            "updated"

        Raises:
            ValidationError: If validation fails
        """
        # Validate traits
        warnings = self.validate_traits("update_event", data)
        for warning in warnings:
            logger.warning(f"Update event warning: {warning}")

        # Validate schema
        validated = self.validate_schema("update_event", data)

        service = self._get_calendar_service(context)

        # Get current event for checkpoint
        try:
            current_event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as e:
            raise ValidationError(
                code="EVENT_NOT_FOUND",
                message=f"Event {event_id} not found in calendar {calendar_id}",
                skill_path=self.skill_md_path,
                skill_section="operations",
            ) from e

        # Create checkpoint with previous state
        checkpoint = self.create_checkpoint(
            "update_event",
            previous_state=current_event,
            metadata={"calendar_id": calendar_id, "event_id": event_id},
        )

        # Build update body (merge with existing)
        event_body = self._build_event_body(validated, current_event)

        try:
            service.events().update(
                calendarId=calendar_id, eventId=event_id, body=event_body
            ).execute()

            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)

            logger.info(f"Updated calendar event: {event_id}")
            return "updated"

        except Exception as e:
            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)
            raise BackendError(f"Failed to update event: {e}", backend="gcalendar") from e

    def delete_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[None]:
        """Delete a calendar event.

        Args:
            content_hash: Ignored
            context: Operation context with backend_path

        Returns:
            HandlerResponse indicating success

        Raises:
            ValidationError: If validation fails
            BackendError: If delete fails
        """
        _ = content_hash  # Unused for connector backends
        if not context or not context.backend_path:
            raise BackendError(
                "Calendar connector requires backend_path in OperationContext.",
                backend="gcalendar",
            )

        path = context.backend_path.strip("/")
        parts = path.split("/")

        if len(parts) != 2 or not parts[1].endswith(".yaml"):
            raise BackendError(f"Invalid path for delete: {path}", backend="gcalendar")

        calendar_id = parts[0]
        event_id = parts[1].replace(".yaml", "")

        # For delete, we need to read the delete request content
        # This should be passed through context or as a separate call
        # For now, require explicit confirmation via a different mechanism
        data = {"agent_intent": "Delete requested", "confirm": True}

        # Validate traits (requires confirm: true)
        self.validate_traits("delete_event", data)

        service = self._get_calendar_service(context)

        # Get current event for checkpoint
        try:
            current_event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as e:
            raise ValidationError(
                code="EVENT_NOT_FOUND",
                message=f"Event {event_id} not found",
                skill_path=self.skill_md_path,
            ) from e

        # Create checkpoint
        checkpoint = self.create_checkpoint(
            "delete_event",
            previous_state=current_event,
            metadata={"calendar_id": calendar_id, "event_id": event_id},
        )

        try:
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute()

            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)

            logger.info(f"Deleted calendar event: {event_id}")

            return HandlerResponse.ok(
                data=None,
                backend_name="gcalendar",
                path=context.backend_path,
            )

        except Exception as e:
            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)
            raise BackendError(f"Failed to delete event: {e}", backend="gcalendar") from e

    def _build_event_body(
        self, validated: Any, existing: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Build Google Calendar API event body from validated data.

        Args:
            validated: Validated Pydantic model
            existing: Existing event data (for updates)

        Returns:
            Event body dict for Google Calendar API
        """
        body: dict[str, Any] = existing.copy() if existing else {}

        # Handle Pydantic model or dict
        if hasattr(validated, "model_dump"):
            data = validated.model_dump(exclude_unset=True, exclude_none=True)
        else:
            data = validated

        # Remove our custom fields
        data.pop("agent_intent", None)
        data.pop("confirm", None)
        data.pop("user_confirmed", None)

        # Map fields to Google Calendar API format
        if "summary" in data:
            body["summary"] = data["summary"]
        if "description" in data:
            body["description"] = data["description"]
        if "location" in data:
            body["location"] = data["location"]
        if "start" in data:
            start = data["start"]
            if hasattr(start, "model_dump"):
                start = start.model_dump()
            body["start"] = start
        if "end" in data:
            end = data["end"]
            if hasattr(end, "model_dump"):
                end = end.model_dump()
            body["end"] = end
        if "attendees" in data:
            attendees = data["attendees"]
            body["attendees"] = [
                a.model_dump() if hasattr(a, "model_dump") else a for a in attendees
            ]
        if "recurrence" in data:
            body["recurrence"] = data["recurrence"]
        if "visibility" in data:
            body["visibility"] = data["visibility"]
        if "colorId" in data:
            body["colorId"] = data["colorId"]

        return body

    # =========================================================================
    # Backend Interface - Directory Operations
    # =========================================================================

    def content_exists(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        """Check if event exists."""
        _ = content_hash  # Unused for connector backends
        if not context or not context.backend_path:
            return HandlerResponse.ok(data=False, backend_name="gcalendar")

        try:
            self.read_content("", context)
            return HandlerResponse.ok(
                data=True, backend_name="gcalendar", path=context.backend_path
            )
        except Exception:
            return HandlerResponse.ok(
                data=False, backend_name="gcalendar", path=context.backend_path
            )

    def get_content_size(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        """Get event content size."""
        if not context:
            return HandlerResponse.ok(data=0, backend_name="gcalendar")

        # Check cache first
        if hasattr(context, "virtual_path") and context.virtual_path:
            cached_size = self._get_size_from_cache(context.virtual_path)
            if cached_size is not None:
                return HandlerResponse.ok(data=cached_size, backend_name="gcalendar")

        # Read content to get size
        response = self.read_content(content_hash, context)
        return HandlerResponse.ok(data=len(response.unwrap()), backend_name="gcalendar")

    def get_ref_count(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        """Get reference count (always 1 for connector backends)."""
        _, _ = content_hash, context  # Unused
        return HandlerResponse.ok(data=1, backend_name="gcalendar")

    def get_version(self, path: str, context: OperationContext | None = None) -> str | None:
        """Get version for a calendar event file."""
        try:
            if context and hasattr(context, "backend_path") and context.backend_path:
                backend_path = context.backend_path
            else:
                backend_path = path.lstrip("/")

            if not backend_path.endswith(".yaml"):
                return None

            parts = backend_path.split("/")
            if len(parts) != 2:
                return None

            # For events, return etag from API or cached version
            return IMMUTABLE_VERSION

        except Exception:
            return None

    def is_directory(
        self, path: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        """Check if path is a directory."""
        _ = context  # Unused
        path = path.strip("/")
        if not path:
            return HandlerResponse.ok(
                data=True, backend_name="gcalendar"
            )  # Root is always a directory

        parts = path.split("/")
        # Calendar IDs (single part) are directories, event files are not
        return HandlerResponse.ok(data=len(parts) == 1, backend_name="gcalendar")

    def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
        """List directory contents.

        Args:
            path: Directory path to list
            context: Operation context for authentication

        Returns:
            List of entry names
        """
        path = path.strip("/")

        # Root directory - list calendars
        if not path:
            return self._list_calendars(context)

        # Calendar directory - list events
        return self._list_events(path, context)

    def _list_calendars(self, context: OperationContext | None) -> list[str]:
        """List available calendars."""
        try:
            service = self._get_calendar_service(context)
            calendars_result = service.calendarList().list().execute()
            calendars = calendars_result.get("items", [])

            entries = []
            for cal in calendars:
                cal_id = cal.get("id", "")
                if cal_id == context.user_id if context else False:
                    entries.append("primary/")
                else:
                    # Use summary as folder name if available, otherwise ID
                    entries.append(f"{cal_id}/")

            # Always include primary
            if "primary/" not in entries:
                entries.insert(0, "primary/")

            return sorted(set(entries))

        except Exception as e:
            raise BackendError(f"Failed to list calendars: {e}", backend="gcalendar") from e

    def _list_events(self, calendar_id: str, context: OperationContext | None) -> list[str]:
        """List events in a calendar."""
        try:
            service = self._get_calendar_service(context)

            # Get events from now onwards
            now = datetime.now(UTC).isoformat()

            events_result = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=now,
                    maxResults=self.max_events_per_calendar,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            events = events_result.get("items", [])

            entries = []
            for event in events:
                event_id = event.get("id")
                if event_id:
                    entries.append(f"{event_id}.yaml")

            return sorted(entries)

        except Exception as e:
            raise BackendError(
                f"Failed to list events in calendar {calendar_id}: {e}",
                backend="gcalendar",
            ) from e

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Create directory (not supported - calendars are created via Google)."""
        _, _, _, _ = path, parents, exist_ok, context  # Unused
        raise BackendError(
            "Cannot create calendars via Nexus. Use Google Calendar to create new calendars.",
            backend="gcalendar",
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Remove directory (not supported)."""
        _, _, _ = path, recursive, context  # Unused
        raise BackendError(
            "Cannot delete calendars via Nexus. Use Google Calendar to manage calendars.",
            backend="gcalendar",
        )
