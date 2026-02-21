"""Cross-zone storage driver for IPC message routing via DT_MOUNT.

Wraps an ``IPCStorageDriver`` with zone-aware routing, implementing the
same protocol (Recursive Wrapping — LEGO Architecture §4.3 Mechanism 2).

When a message is written to ``/agents/{recipient}/inbox/...``, this driver:

1. Resolves the recipient's zone via ``AgentRegistryProtocol``
2. Checks cross-zone permissions via ``PermissionProtocol`` (optional)
3. Delegates the write to the inner driver with the resolved ``zone_id``
4. Publishes a NATS notification to the target zone (optional)

Non-write operations (read, list_dir, exists, etc.) delegate directly to
the inner driver — only writes need zone routing.

Issue: #1727
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING

from nexus.bricks.ipc.conventions import dead_letter_path
from nexus.bricks.ipc.exceptions import CrossZoneDeliveryError, DLQReason

if TYPE_CHECKING:
    from nexus.bricks.ipc.protocols import (
        AgentLookupProtocol,
        HotPathPublisher,
        PermissionCheckProtocol,
    )
    from nexus.bricks.ipc.storage.protocol import IPCStorageDriver

logger = logging.getLogger(__name__)

# Regex to extract agent_id from inbox write paths:
# /agents/{agent_id}/inbox/{filename}
_INBOX_PATH_RE = re.compile(r"^/agents/([^/]+)/inbox/")

# Regex to extract agent_id from agent directory paths:
# /agents/{agent_id} or /agents/{agent_id}/inbox etc.
_AGENT_PATH_RE = re.compile(r"^/agents/([^/]+)(?:/|$)")


class CrossZoneStorageDriver:
    """Wraps IPCStorageDriver with zone-aware routing.

    Implements the ``IPCStorageDriver`` protocol (same-Protocol wrapping,
    LEGO §4.3 Mechanism 2).  Resolves recipient's zone via
    ``AgentRegistryProtocol`` before delegating writes.

    Args:
        inner: The wrapped storage driver for actual I/O.
        agent_registry: Registry to resolve agent → zone mapping.
        local_zone_id: This node's zone ID.
        permission_checker: Optional ReBAC checker for cross-zone auth.
        hot_publisher: Optional NATS publisher for target-zone notifications.
        cache_ttl_seconds: TTL for the agent-zone LRU cache.
    """

    def __init__(
        self,
        inner: IPCStorageDriver,
        agent_registry: AgentLookupProtocol,
        local_zone_id: str,
        permission_checker: PermissionCheckProtocol | None = None,
        hot_publisher: HotPathPublisher | None = None,
        cache_ttl_seconds: int = 30,
    ) -> None:
        self._inner = inner
        self._agent_registry = agent_registry
        self._local_zone_id = local_zone_id
        self._permission_checker = permission_checker
        self._hot_publisher = hot_publisher
        self._cache_ttl = cache_ttl_seconds
        # Simple TTL cache: agent_id → (zone_id, expiry_time)
        self._zone_cache: dict[str, str] = {}
        self._zone_cache_expiry: dict[str, float] = {}

    # ------------------------------------------------------------------
    # IPCStorageDriver protocol — delegated methods (no zone routing)
    # ------------------------------------------------------------------

    async def read(self, path: str, zone_id: str) -> bytes:
        return await self._inner.read(path, zone_id)

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        return await self._inner.list_dir(path, zone_id)

    async def count_dir(self, path: str, zone_id: str) -> int:
        resolved_zone = await self._resolve_zone_for_path(path, zone_id)
        return await self._inner.count_dir(path, resolved_zone)

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        return await self._inner.rename(src, dst, zone_id)

    async def mkdir(self, path: str, zone_id: str) -> None:
        return await self._inner.mkdir(path, zone_id)

    async def exists(self, path: str, zone_id: str) -> bool:
        resolved_zone = await self._resolve_zone_for_path(path, zone_id)
        return await self._inner.exists(path, resolved_zone)

    # ------------------------------------------------------------------
    # Zone resolution for agent paths
    # ------------------------------------------------------------------

    async def _resolve_zone_for_path(self, path: str, default_zone: str) -> str:
        """Resolve the correct zone_id for an agent path.

        If the path matches ``/agents/{agent_id}/...``, looks up the
        agent's zone.  Falls back to *default_zone* for non-agent paths
        or when the agent is local / not found in the registry.
        """
        match = _AGENT_PATH_RE.match(path)
        if not match:
            return default_zone

        agent_id = match.group(1)
        try:
            return await self._resolve_agent_zone(agent_id)
        except CrossZoneDeliveryError:
            # Agent not in registry — fall back to caller's zone
            return default_zone

    # ------------------------------------------------------------------
    # IPCStorageDriver.write — zone-aware routing
    # ------------------------------------------------------------------

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        """Write data, resolving cross-zone routing for inbox writes.

        If the path matches ``/agents/{agent_id}/inbox/...``, resolves
        the agent's zone and routes the write accordingly.  Non-inbox
        writes delegate directly to the inner driver.
        """
        match = _INBOX_PATH_RE.match(path)
        if not match:
            # Not an inbox write — delegate directly
            await self._inner.write(path, data, zone_id)
            return

        recipient_id = match.group(1)
        target_zone = await self._resolve_agent_zone(recipient_id)

        # Same zone — delegate directly (no cross-zone overhead)
        if target_zone == self._local_zone_id:
            await self._inner.write(path, data, zone_id)
            return

        # Cross-zone delivery
        logger.info(
            "Cross-zone IPC: routing write for %s from %s → %s",
            recipient_id,
            self._local_zone_id,
            target_zone,
        )

        # Permission check
        await self._check_permission(self._local_zone_id, target_zone, recipient_id)

        # Verify target zone inbox exists
        from nexus.bricks.ipc.conventions import inbox_path

        target_inbox = inbox_path(recipient_id)
        if not await self._inner.exists(target_inbox, target_zone):
            raise CrossZoneDeliveryError(
                reason=DLQReason.ZONE_UNREACHABLE,
                detail=f"Inbox for agent '{recipient_id}' not found in zone '{target_zone}'",
                source_zone=self._local_zone_id,
                target_zone=target_zone,
                agent_id=recipient_id,
            )

        # Write to target zone
        await self._inner.write(path, data, target_zone)

        # Write routing metadata sidecar (uses .routing extension, not .json,
        # so MessageProcessor won't try to parse it as a message envelope)
        routing_meta = {
            "source_zone": self._local_zone_id,
            "target_zone": target_zone,
            "hop_count": 1,
            "max_hops": 3,
            "recipient": recipient_id,
        }
        routing_path = path + ".routing"
        await self._inner.write(
            routing_path,
            json.dumps(routing_meta, indent=2).encode("utf-8"),
            target_zone,
        )

        # NATS notification to target zone (best-effort)
        await self._notify_target_zone(target_zone, recipient_id, data)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _resolve_agent_zone(self, agent_id: str) -> str:
        """Resolve agent_id → zone_id via registry with TTL cache.

        Raises:
            CrossZoneDeliveryError: If agent not found (MOUNT_NOT_FOUND).
        """
        now = time.monotonic()

        # Check cache
        if agent_id in self._zone_cache:
            expiry = self._zone_cache_expiry.get(agent_id, 0)
            if now < expiry:
                return self._zone_cache[agent_id]
            # Expired — remove
            del self._zone_cache[agent_id]
            del self._zone_cache_expiry[agent_id]

        # Registry lookup
        info = await self._agent_registry.get(agent_id)
        if info is None or info.zone_id is None:
            raise CrossZoneDeliveryError(
                reason=DLQReason.MOUNT_NOT_FOUND,
                detail=f"Agent '{agent_id}' not found in registry or has no zone_id",
                source_zone=self._local_zone_id,
                agent_id=agent_id,
            )

        # Cache result
        self._zone_cache[agent_id] = info.zone_id
        self._zone_cache_expiry[agent_id] = now + self._cache_ttl

        return info.zone_id

    async def _check_permission(self, sender_zone: str, target_zone: str, agent_id: str) -> None:
        """Check ReBAC permission for cross-zone delivery.

        Raises:
            CrossZoneDeliveryError: If permission denied (PERMISSION_DENIED).
        """
        if self._permission_checker is None:
            return  # No permission checker — allow all

        allowed = await self._permission_checker.rebac_check(
            subject=("zone", sender_zone),
            permission="ipc:deliver",
            object=("zone", target_zone),
        )

        if not allowed:
            # Write DLQ reason file (best-effort)
            await self._write_dlq_reason(
                agent_id=agent_id,
                zone_id=sender_zone,
                reason=DLQReason.PERMISSION_DENIED,
                detail=f"Zone '{sender_zone}' denied cross-zone delivery to '{target_zone}'",
            )
            raise CrossZoneDeliveryError(
                reason=DLQReason.PERMISSION_DENIED,
                detail=f"Zone '{sender_zone}' not permitted to deliver to zone '{target_zone}'",
                source_zone=sender_zone,
                target_zone=target_zone,
                agent_id=agent_id,
            )

    async def _notify_target_zone(self, target_zone: str, recipient: str, msg_data: bytes) -> None:
        """Publish NATS notification to the target zone's IPC subject."""
        if self._hot_publisher is None:
            return

        subject = f"{target_zone}.ipc.inbox.{recipient}"
        try:
            await self._hot_publisher.publish(subject, msg_data)
            logger.debug(
                "Cross-zone NATS notification sent: %s",
                subject,
            )
        except Exception:
            logger.warning(
                "Cross-zone NATS notification failed for %s (message IS written)",
                subject,
                exc_info=True,
            )

    async def _write_dlq_reason(
        self,
        agent_id: str,
        zone_id: str,
        reason: DLQReason,
        detail: str,
    ) -> None:
        """Write a structured .reason.json file to the agent's dead_letter dir."""
        try:
            dlq_dir = dead_letter_path(agent_id)
            # Ensure dead_letter dir exists
            if await self._inner.exists(dlq_dir, zone_id):
                import time as _time

                reason_filename = f"{int(_time.time())}_{reason.value}.reason.json"
                reason_data = json.dumps(
                    {
                        "reason": reason.value,
                        "detail": detail,
                        "source_zone": self._local_zone_id,
                        "agent_id": agent_id,
                    },
                    indent=2,
                ).encode("utf-8")
                await self._inner.write(
                    f"{dlq_dir}/{reason_filename}",
                    reason_data,
                    zone_id,
                )
        except Exception:
            logger.warning(
                "Failed to write DLQ reason file for agent %s",
                agent_id,
                exc_info=True,
            )
