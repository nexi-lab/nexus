"""Access manifest service — CRUD + ReBAC integration (Issue #1754).

Manages manifest lifecycle: create, evaluate, revoke, list.
Generates ReBAC tuples on creation and deletes them on revocation.
Uses in-process TTLCache (60s) for hot-path evaluations.
"""

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from nexus.bricks.access_manifest.evaluator import ManifestEvaluator
from nexus.contracts.access_manifest_types import (
    AccessManifest,
    EvaluationTrace,
    ManifestEntry,
    ToolPermission,
)
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models.access_manifest import AccessManifestModel

# Inlined from nexus.mcp.profiles to avoid cross-brick import (LEGO compliance)
TOOL_PATH_PREFIX = "/tools/"

# Manifest status constants (avoid coupling to credential_types.CredentialStatus dataclass)
_STATUS_ACTIVE = "active"
_STATUS_REVOKED = "revoked"

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.orm import Session

    from nexus.services.protocols.rebac import ReBACBrickProtocol
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple TTL cache (in-process, no external deps)
# ---------------------------------------------------------------------------


class _TTLCache:
    """Minimal thread-safe TTL cache for manifest entries."""

    def __init__(self, ttl_seconds: int = 60) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], tuple[float, tuple[ManifestEntry, ...]]] = {}

    def get(self, key: tuple[str, str]) -> tuple[ManifestEntry, ...] | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            ts, entries = item
            if datetime.now(UTC).timestamp() - ts > self._ttl:
                del self._data[key]
                return None
            return entries

    def set(self, key: tuple[str, str], entries: tuple[ManifestEntry, ...]) -> None:
        with self._lock:
            self._data[key] = (datetime.now(UTC).timestamp(), entries)

    def invalidate(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._data.pop(key, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    """Return current UTC time as a naive datetime (SQLite-compatible)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _entries_to_json(entries: tuple[ManifestEntry, ...]) -> str:
    """Serialize ManifestEntry tuple to JSON."""
    return json.dumps(
        [
            {
                "tool_pattern": e.tool_pattern,
                "permission": e.permission.value,
                "max_calls_per_minute": e.max_calls_per_minute,
            }
            for e in entries
        ]
    )


def _json_to_entries(raw: str) -> tuple[ManifestEntry, ...]:
    """Deserialize JSON to ManifestEntry tuple."""
    data = json.loads(raw)
    return tuple(
        ManifestEntry(
            tool_pattern=d["tool_pattern"],
            permission=ToolPermission(d["permission"]),
            max_calls_per_minute=d.get("max_calls_per_minute"),
        )
        for d in data
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AccessManifestService:
    """Manages access manifests with ReBAC integration.

    Creates ReBAC tuples when manifests are created, deletes them on
    revocation. Uses TTLCache for hot-path evaluation.
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        rebac_manager: "ReBACBrickProtocol",
        cache_ttl: int = 60,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._rebac = rebac_manager
        self._cache = _TTLCache(ttl_seconds=cache_ttl)
        self._evaluator = ManifestEvaluator()

    @contextmanager
    def _get_session(self) -> "Generator[Session, None, None]":
        """Create a session with auto-commit/rollback."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_manifest(
        self,
        agent_id: str,
        name: str,
        entries: tuple[ManifestEntry, ...],
        zone_id: str = ROOT_ZONE_ID,
        created_by: str = "",
        valid_hours: int = 720,
        credential_id: str | None = None,
    ) -> AccessManifest:
        """Create a manifest and generate ReBAC tuples.

        Args:
            agent_id: Agent this manifest applies to.
            name: Human-readable name.
            entries: Ordered access rules.
            zone_id: Zone scope.
            created_by: Creator identifier.
            valid_hours: Validity period in hours.
            credential_id: Optional backing VC.

        Returns:
            The created AccessManifest.
        """
        manifest_id = str(uuid.uuid4())
        now = _utcnow_naive()
        valid_until = now + timedelta(hours=valid_hours)

        # Generate ReBAC tuples for ALLOW entries
        tuple_ids: list[str] = []
        for entry in entries:
            if entry.permission == ToolPermission.ALLOW and not _is_glob_pattern(
                entry.tool_pattern
            ):
                tool_path = f"{TOOL_PATH_PREFIX}{entry.tool_pattern}"
                result = self._rebac.rebac_write(
                    subject=("agent", agent_id),
                    relation="direct_viewer",
                    object=("file", tool_path),
                    zone_id=zone_id,
                )
                if hasattr(result, "tuple_id") and result.tuple_id:
                    tuple_ids.append(result.tuple_id)

        # Persist
        model = AccessManifestModel(
            manifest_id=manifest_id,
            agent_id=agent_id,
            zone_id=zone_id,
            name=name,
            entries_json=_entries_to_json(entries),
            status=_STATUS_ACTIVE,
            valid_from=now,
            valid_until=valid_until,
            credential_id=credential_id,
            created_by=created_by,
            created_at=now,
            tuple_ids_json=json.dumps(tuple_ids) if tuple_ids else None,
        )

        with self._get_session() as session:
            session.add(model)
            session.flush()

        # Invalidate cache
        self._cache.invalidate((agent_id, zone_id))

        logger.info(
            "[MANIFEST] Created manifest %s for agent %s (%d entries, %d tuples)",
            manifest_id,
            agent_id,
            len(entries),
            len(tuple_ids),
        )

        return AccessManifest(
            id=manifest_id,
            agent_id=agent_id,
            zone_id=zone_id,
            name=name,
            entries=entries,
            status=_STATUS_ACTIVE,
            valid_from=now.isoformat(),
            valid_until=valid_until.isoformat(),
            created_by=created_by,
            credential_id=credential_id,
        )

    def evaluate(
        self, agent_id: str, tool_name: str, zone_id: str = ROOT_ZONE_ID
    ) -> ToolPermission:
        """Evaluate a tool access request for an agent.

        Uses cache first, falls back to DB lookup.

        Args:
            agent_id: Agent requesting access.
            tool_name: Tool to evaluate.
            zone_id: Zone scope.

        Returns:
            ToolPermission.ALLOW or ToolPermission.DENY.
        """
        entries = self._get_entries_cached(agent_id, zone_id)
        if entries is None:
            return ToolPermission.DENY
        return self._evaluator.evaluate(entries, tool_name)

    def evaluate_with_trace(
        self, agent_id: str, tool_name: str, zone_id: str = ROOT_ZONE_ID
    ) -> EvaluationTrace:
        """Evaluate a tool with a full decision trace (proof tree).

        Uses cache first, falls back to DB lookup. Returns the complete
        evaluation trace showing which entries were checked and matched.

        Args:
            agent_id: Agent requesting access.
            tool_name: Tool to evaluate.
            zone_id: Zone scope.

        Returns:
            EvaluationTrace with decision and per-entry trace.
        """
        entries = self._get_entries_cached(agent_id, zone_id)
        if entries is None:
            # No manifest found — return trace with default deny, no entries
            return EvaluationTrace(
                tool_name=tool_name,
                decision=ToolPermission.DENY,
                matched_index=-1,
                entries=(),
                default_applied=True,
            )
        return self._evaluator.evaluate_with_trace(entries, tool_name)

    def filter_tools(
        self, agent_id: str, tool_names: frozenset[str], zone_id: str = ROOT_ZONE_ID
    ) -> frozenset[str]:
        """Batch filter tools for an agent.

        Args:
            agent_id: Agent requesting access.
            tool_names: Set of tool names.
            zone_id: Zone scope.

        Returns:
            Frozenset of allowed tool names.
        """
        entries = self._get_entries_cached(agent_id, zone_id)
        if entries is None:
            return frozenset()
        return self._evaluator.filter_tools(entries, tool_names)

    def revoke_manifest(self, manifest_id: str) -> bool:
        """Revoke a manifest and delete its ReBAC tuples.

        Args:
            manifest_id: UUID of the manifest.

        Returns:
            True if revoked, False if not found.
        """
        now = _utcnow_naive()

        with self._get_session() as session:
            model = session.execute(
                select(AccessManifestModel).where(AccessManifestModel.manifest_id == manifest_id)
            ).scalar_one_or_none()

            if model is None:
                return False

            agent_id = model.agent_id
            zone_id = model.zone_id

            # Delete ReBAC tuples
            if model.tuple_ids_json:
                tuple_ids = json.loads(model.tuple_ids_json)
                for tid in tuple_ids:
                    self._rebac.rebac_delete(tid)

            session.execute(
                update(AccessManifestModel)
                .where(AccessManifestModel.manifest_id == manifest_id)
                .values(status=_STATUS_REVOKED, revoked_at=now)
            )

        self._cache.invalidate((agent_id, zone_id))
        logger.info("[MANIFEST] Revoked manifest %s", manifest_id)
        return True

    def get_manifest(self, manifest_id: str) -> AccessManifest | None:
        """Get a single manifest by ID.

        Args:
            manifest_id: UUID of the manifest.

        Returns:
            AccessManifest or None.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AccessManifestModel).where(AccessManifestModel.manifest_id == manifest_id)
            ).scalar_one_or_none()

            if model is None:
                return None

            return self._model_to_manifest(model)

    def list_manifests(
        self,
        agent_id: str | None = None,
        zone_id: str | None = None,
        active_only: bool = False,
        offset: int = 0,
        limit: int = 50,
    ) -> list[AccessManifest]:
        """List manifests with optional filters.

        Args:
            agent_id: Filter by agent.
            zone_id: Filter by zone.
            active_only: Only return active manifests.
            offset: Pagination offset.
            limit: Max results (capped at 100).

        Returns:
            List of AccessManifest objects.
        """
        limit = min(limit, 100)

        with self._get_session() as session:
            query = select(AccessManifestModel)
            if agent_id is not None:
                query = query.where(AccessManifestModel.agent_id == agent_id)
            if zone_id is not None:
                query = query.where(AccessManifestModel.zone_id == zone_id)
            if active_only:
                query = query.where(AccessManifestModel.status == _STATUS_ACTIVE)
            query = query.order_by(AccessManifestModel.created_at.desc())
            query = query.offset(offset).limit(limit)

            models = list(session.execute(query).scalars().all())
            return [self._model_to_manifest(m) for m in models]

    # --- Private helpers ---

    def _get_entries_cached(self, agent_id: str, zone_id: str) -> tuple[ManifestEntry, ...] | None:
        """Get entries from cache or DB."""
        cache_key = (agent_id, zone_id)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        with self._get_session() as session:
            models = list(
                session.execute(
                    select(AccessManifestModel)
                    .where(AccessManifestModel.agent_id == agent_id)
                    .where(AccessManifestModel.zone_id == zone_id)
                    .where(AccessManifestModel.status == _STATUS_ACTIVE)
                    .order_by(AccessManifestModel.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .all()
            )

        if not models:
            return None

        entries = _json_to_entries(models[0].entries_json)
        self._cache.set(cache_key, entries)
        return entries

    @staticmethod
    def _model_to_manifest(model: AccessManifestModel) -> AccessManifest:
        """Convert ORM model to AccessManifest."""
        return AccessManifest(
            id=model.manifest_id,
            agent_id=model.agent_id,
            zone_id=model.zone_id,
            name=model.name,
            entries=_json_to_entries(model.entries_json),
            status=model.status,
            valid_from=model.valid_from.isoformat() if model.valid_from else "",
            valid_until=model.valid_until.isoformat() if model.valid_until else None,
            created_by=model.created_by,
            credential_id=model.credential_id,
        )


def _is_glob_pattern(pattern: str) -> bool:
    """Check if a pattern contains glob wildcards."""
    return "*" in pattern or "?" in pattern or "[" in pattern
