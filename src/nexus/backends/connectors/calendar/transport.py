"""Google Calendar Transport — raw key→bytes I/O over the Calendar API.

Implements the Transport protocol for Google Calendar, mapping:
- fetch(key) → events.get → YAML bytes
- store(key, data) → events.insert / events.update
- remove(key) → events.delete
- list_keys(prefix) → calendarList.list / events.list

Auth: CalendarTransport carries a TokenManager + provider.  Before each
request the caller must bind an OperationContext via ``with_context()``.

Key schema:
    "primary/eventId.yaml"   → calendar=primary, event=eventId
    "primary/_new.yaml"      → create new event in primary
    list_keys("")            → common_prefixes = ["primary/", ...]
    list_keys("primary/")    → event keys in primary calendar
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from copy import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

from nexus.backends.connectors.cli.display_path import sanitize_filename
from nexus.contracts.exceptions import AuthenticationError, BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Suppress noisy discovery-cache warnings.
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def _resolve_first_valid_zone(*candidates: str | None) -> Any | None:
    """Return the first parseable ZoneInfo from the candidate sequence.

    Returning ``None`` when every candidate is missing or invalid lets
    callers decide how to degrade (date-only, UTC, etc.) instead of
    silently locking onto the first entry regardless of validity.  This
    is what lets the event→calendar-default fallback chain work: if
    ``start.timeZone`` is present but garbage, the next resort is the
    calendar-level ``events_result.timeZone``.
    """
    from zoneinfo import ZoneInfo

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    return None


def _log_sort_fallback(date_raw: str, *candidates: str | None) -> None:
    """Log a warning when the UTC-sort-prefix path degrades to coarser
    keys.  Without this, silent fallback hides provider drift (new
    invalid ``timeZone`` values, unexpected ``dateTime`` shapes) and
    lets newest-first ordering instability slip through undetected."""
    hint_pair = [c for c in candidates if c]
    logger.warning(
        "calendar: falling back to coarse sort prefix for date=%r timezone_hints=%r",
        date_raw,
        hint_pair,
    )


def _utc_sort_prefix(
    date_raw: str,
    *,
    timezone_hint: str | None = None,
    fallback_timezone: str | None = None,
) -> str:
    """Render a UTC-normalized, lex-sortable date prefix for event keys.

    For dateTime inputs returns ``YYYY-MM-DDTHH:MM:SSZ``.  For all-day
    (``date``) inputs, anchors to midnight in the first valid of
    ``timezone_hint`` (the event's ``start.timeZone``) then
    ``fallback_timezone`` (the calendar-level default).  A Tokyo
    all-day event otherwise sorts before a Tokyo 23:00 timed event on
    the same local day, inverting real chronological order.  Falls
    back to raw ``YYYY-MM-DD`` when every candidate zone is missing or
    unparseable, and empty string for unparseable input.
    """
    if not date_raw:
        return ""
    # All-day case: ``start.date`` gives exactly ``YYYY-MM-DD``.  Anchor
    # to 00:00 in the event's own timezone (or the calendar default) so
    # timed / all-day events on the same local day order consistently
    # in UTC.  A bad ``start.timeZone`` must not silently mask a valid
    # calendar-level default — resolve through the chain.
    if len(date_raw) == 10 and date_raw[4] == "-" and date_raw[7] == "-":
        tz = _resolve_first_valid_zone(timezone_hint, fallback_timezone)
        if tz is not None:
            try:
                midnight_local = datetime.fromisoformat(date_raw).replace(tzinfo=tz)
                return midnight_local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
        # All-day events without a usable zone fall back to date-only.
        # That's a coarser sort key than UTC-anchored events carry, so
        # log so ops can catch drift in provider defaults or malformed
        # zone strings instead of silently losing precision.
        _log_sort_fallback(date_raw, timezone_hint, fallback_timezone)
        return date_raw
    candidate = date_raw.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate.replace(" ", "T", 1))
    except ValueError:
        # Unparseable dateTime degrades to date-only / empty — log so
        # provider payload drift is visible rather than silently eroding
        # sort stability.
        _log_sort_fallback(date_raw, timezone_hint, fallback_timezone)
        if len(date_raw) >= 10 and date_raw[4] == "-" and date_raw[7] == "-":
            return date_raw[:10]
        return ""
    if dt.tzinfo is None:
        # Calendar payloads can carry a naive ``dateTime`` alongside a
        # separate ``timeZone`` field (common for recurring events).  We
        # must apply the hint first — reading "09:00" in LA as UTC would
        # misdate it by 7–8 hours.  Fall through event→calendar→UTC so
        # one bad event zone doesn't bypass a good calendar default.
        tz = _resolve_first_valid_zone(timezone_hint, fallback_timezone)
        if tz is None:
            # UTC fallback for naive input is a real ordering risk — a
            # non-UTC event can shift by hours.  Log so ops can catch
            # drift before it silently corrupts newest-first results.
            _log_sort_fallback(date_raw, timezone_hint, fallback_timezone)
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class CalendarTransport:
    """Google Calendar API transport implementing the Transport protocol."""

    transport_name: str = "gcalendar"

    def __init__(
        self,
        token_manager: Any,
        provider: str = "gcalendar",
        user_email: str | None = None,
        max_events_per_calendar: int = 250,
    ) -> None:
        self._token_manager = token_manager
        self._provider = provider
        self._user_email = user_email
        self._max_events_per_calendar = max_events_per_calendar
        self._context: OperationContext | None = None

    # ------------------------------------------------------------------
    # Context binding
    # ------------------------------------------------------------------

    def with_context(self, context: OperationContext | None) -> CalendarTransport:
        """Return a shallow copy bound to *context* (for OAuth token resolution)."""
        clone = copy(self)
        clone._context = context
        return clone

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_calendar_service(self) -> Resource:
        """Build an authenticated Calendar ``Resource``."""
        try:
            from googleapiclient.discovery import build
        except ImportError:
            raise BackendError(
                "google-api-python-client not installed. "
                "Install with: pip install google-api-python-client",
                backend="gcalendar",
            ) from None

        from nexus.backends.connectors.oauth_base import resolve_oauth_access_token
        from nexus.contracts.exceptions import AuthenticationError

        user_email: str | None = self._user_email
        nexus_user_id: str | None = (
            self._context.user_id if self._context and self._context.user_id else None
        )
        zone_id = (
            self._context.zone_id
            if self._context and hasattr(self._context, "zone_id") and self._context.zone_id
            else "root"
        )
        try:
            access_token = resolve_oauth_access_token(
                self._token_manager,
                connector_name="gcalendar_connector",
                provider=self._provider,
                user_email=user_email,
                zone_id=zone_id,
                nexus_user_id=nexus_user_id,
            )
        except AuthenticationError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get valid OAuth token for user {user_email}: {e}",
                backend="gcalendar",
            ) from e

        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        return build("calendar", "v3", credentials=creds)

    @staticmethod
    def _parse_key(key: str) -> tuple[str, str]:
        """Parse ``"calendar_id/event_id.yaml"`` → ``(calendar_id, event_id)``.

        Accepted filename forms:
        - ``"eventId.yaml"``                       (legacy)
        - ``"{date}_{summary}__eventId.yaml"``     (readable — same convention
          as gmail: trailing ``__<id>`` anchor is authoritative)

        Returns ``("", "")`` for root or directory-only keys.
        """
        key = key.strip("/")
        parts = key.split("/")
        if len(parts) == 2 and parts[1].endswith(".yaml"):
            base = parts[1].removesuffix(".yaml")
            if "__" in base:
                base = base.rsplit("__", 1)[-1]
            return parts[0], base
        if len(parts) == 1:
            return parts[0], ""
        return "", ""

    @staticmethod
    def _format_event_as_yaml(event: dict[str, Any]) -> bytes:
        """Format a Calendar API event dict as YAML bytes."""
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
        yaml_data = {k: v for k, v in yaml_data.items() if v is not None}
        yaml_output: str = yaml.dump(
            yaml_data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        return yaml_output.encode("utf-8")

    @staticmethod
    def _parse_yaml_content(data: bytes) -> dict[str, Any]:
        """Parse YAML bytes, extracting ``agent_intent`` / ``confirm`` from comments."""
        text = data.decode("utf-8")
        result: dict[str, Any] = {}

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("# agent_intent:"):
                result["agent_intent"] = line.replace("# agent_intent:", "").strip()
            elif line.startswith("# confirm:"):
                result["confirm"] = line.replace("# confirm:", "").strip().lower() == "true"
            elif line.startswith("# user_confirmed:"):
                result["user_confirmed"] = (
                    line.replace("# user_confirmed:", "").strip().lower() == "true"
                )

        yaml_content = yaml.safe_load(text) or {}
        if isinstance(yaml_content, dict):
            result.update(yaml_content)
        return result

    @staticmethod
    def _build_event_body(
        data: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build Google Calendar API event body from parsed data."""
        body: dict[str, Any] = existing.copy() if existing else {}

        if hasattr(data, "model_dump"):
            data = data.model_dump(exclude_unset=True, exclude_none=True)

        data.pop("agent_intent", None)
        data.pop("confirm", None)
        data.pop("user_confirmed", None)

        field_map = [
            "summary",
            "description",
            "location",
            "visibility",
            "colorId",
            "recurrence",
        ]
        for field in field_map:
            if field in data:
                body[field] = data[field]

        for dt_field in ("start", "end"):
            if dt_field in data:
                val = data[dt_field]
                body[dt_field] = val.model_dump() if hasattr(val, "model_dump") else val

        if "attendees" in data:
            body["attendees"] = [
                a.model_dump() if hasattr(a, "model_dump") else a for a in data["attendees"]
            ]
        return body

    # ------------------------------------------------------------------
    # Transport protocol methods
    # ------------------------------------------------------------------

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        """Create or update a calendar event.

        - Key ending ``_new.yaml`` → create (events.insert)
        - Key ending ``{event_id}.yaml`` → update (events.update)

        Returns the event ID (create) or ``"updated"`` (update).
        """
        calendar_id, event_id = self._parse_key(key)
        if not calendar_id:
            raise BackendError(f"Invalid calendar key: {key}", backend="gcalendar")

        parsed = self._parse_yaml_content(data)
        service = self._get_calendar_service()

        if event_id == "_new":
            event_body = self._build_event_body(parsed)
            created = service.events().insert(calendarId=calendar_id, body=event_body).execute()
            return str(created.get("id", ""))
        else:
            # Update: fetch existing event first
            try:
                current = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
            except Exception as e:
                raise NexusFileNotFoundError(key) from e
            event_body = self._build_event_body(parsed, current)
            service.events().update(
                calendarId=calendar_id, eventId=event_id, body=event_body
            ).execute()
            return "updated"

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Fetch a single event as YAML bytes."""
        calendar_id, event_id = self._parse_key(key)
        if not calendar_id or not event_id or event_id.startswith("_"):
            raise NexusFileNotFoundError(key)

        service = self._get_calendar_service()
        try:
            event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as e:
            raise NexusFileNotFoundError(key) from e

        content = self._format_event_as_yaml(event)
        etag = event.get("etag")
        return content, etag

    def remove(self, key: str) -> None:
        """Delete a calendar event."""
        calendar_id, event_id = self._parse_key(key)
        if not calendar_id or not event_id:
            raise BackendError(f"Invalid key for delete: {key}", backend="gcalendar")

        service = self._get_calendar_service()
        try:
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as e:
            raise BackendError(f"Failed to delete event: {e}", backend="gcalendar") from e

    def exists(self, key: str) -> bool:
        """Check whether a calendar event exists."""
        calendar_id, event_id = self._parse_key(key)
        if not event_id:
            # Directory check
            return calendar_id != "" or key.strip("/") == ""
        try:
            service = self._get_calendar_service()
            service.events().get(calendarId=calendar_id, eventId=event_id).execute()
            return True
        except AuthenticationError:
            raise
        except Exception:
            return False

    def get_size(self, key: str) -> int:
        """Return event content size (fetch → len)."""
        content, _ = self.fetch(key)
        return len(content)

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """List calendar or event keys.

        - ``list_keys("")`` → ``([], ["primary/", ...])``
        - ``list_keys("primary/")`` → ``(["primary/eventId.yaml", ...], [])``
        """
        prefix = prefix.strip("/")

        if not prefix:
            # Root → list calendars as common prefixes
            service = self._get_calendar_service()
            try:
                result = service.calendarList().list().execute()
            except Exception as e:
                raise BackendError(f"Failed to list calendars: {e}", backend="gcalendar") from e

            prefixes = []
            user_id = self._context.user_id if self._context else None
            for cal in result.get("items", []):
                cal_id = cal.get("id", "")
                if cal_id == user_id:
                    prefixes.append("primary/")
                else:
                    prefixes.append(f"{cal_id}/")

            if "primary/" not in prefixes:
                prefixes.insert(0, "primary/")
            return [], sorted(set(prefixes))

        # Calendar → list events
        service = self._get_calendar_service()
        now = datetime.now(UTC).isoformat()
        try:
            events_result = (
                service.events()
                .list(
                    calendarId=prefix,
                    timeMin=now,
                    maxResults=self._max_events_per_calendar,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except Exception as e:
            raise BackendError(
                f"Failed to list events in calendar {prefix}: {e}", backend="gcalendar"
            ) from e

        keys = []
        # Calendar-level default timezone — falls back when an event's
        # own ``start.timeZone`` is missing (especially common for all-
        # day events, where Calendar often omits the field and expects
        # the calendar's own zone to apply).  Without this fallback,
        # all-day events in non-UTC calendars would still render as
        # raw ``YYYY-MM-DD`` and mis-sort against timed events.
        default_tz = events_result.get("timeZone")
        for event in events_result.get("items", []):
            eid = event.get("id")
            if not eid:
                continue
            start = event.get("start", {}) or {}
            # For all-day events, render the start as midnight in the
            # event's own timezone (or calendar default) so its UTC
            # instant compares correctly against timed events on the
            # same day.  Without this, an all-day event in Asia/Tokyo
            # starting "2026-04-21" would compare as 2026-04-21T00:00Z
            # against a 23:00 Tokyo-time event that resolves to
            # 2026-04-20T14:00Z — inverting their real order.
            date_prefix = _utc_sort_prefix(
                start.get("dateTime") or start.get("date") or "",
                timezone_hint=start.get("timeZone"),
                fallback_timezone=default_tz,
            )
            summary = (event.get("summary") or "").strip()
            parts: list[str] = []
            if date_prefix:
                parts.append(date_prefix)
            if summary:
                parts.append(sanitize_filename(summary, max_len=60))
            if parts:
                keys.append(f"{prefix}/{'_'.join(parts)}__{eid}.yaml")
            else:
                keys.append(f"{prefix}/{eid}.yaml")
        return sorted(keys), []

    def copy_key(self, src_key: str, dst_key: str) -> None:
        raise BackendError("Calendar transport does not support copy.", backend="gcalendar")

    def create_dir(self, key: str) -> None:
        raise BackendError(
            "Cannot create calendars via Nexus. Use Google Calendar.",
            backend="gcalendar",
        )

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        data, _ = self.fetch(key, version_id)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def store_chunked(
        self,
        key: str,
        chunks: Iterator[bytes],
        content_type: str = "",
    ) -> str | None:
        data = b"".join(chunks)
        return self.store(key, data, content_type)
