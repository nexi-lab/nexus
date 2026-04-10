"""SessionManager — session discovery and lifecycle via VFS.

Manages agent conversation sessions with --continue / --resume / --fork
support. Sessions are stored under the agent's VFS path:

    /{zone}/agents/{id}/sessions/{session-id}/conversation   (CAS-addressed)
    /{zone}/agents/{id}/sessions/{session-id}/metadata.json

All I/O through VFS syscalls — observable via kernel dispatch.

References:
    - docs/architecture/nexus-agent-plan.md §1.7
    - CC's --continue / --resume / --fork-session
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from nexus.contracts.vfs_paths import agent as agent_paths

logger = logging.getLogger(__name__)

SysReadFn = Callable[[str], Awaitable[bytes]]
SysWriteFn = Callable[[str, bytes], Awaitable[Any]]
SysReaddirFn = Callable[..., Awaitable[list[Any]]]


class SessionManager:
    """Session discovery and lifecycle via VFS.

    Supports:
    - create(): new empty session
    - latest(): find most recent session (--continue)
    - load(): load conversation from session (--resume <id>)
    - fork(): copy session with CAS dedup (--fork-session)
    - save(): persist conversation + metadata
    """

    def __init__(
        self,
        *,
        sys_read: SysReadFn,
        sys_write: SysWriteFn,
        sys_readdir: SysReaddirFn,
        zone_id: str,
        agent_id: str,
    ) -> None:
        self._sys_read = sys_read
        self._sys_write = sys_write
        self._sys_readdir = sys_readdir
        self._zone_id = zone_id
        self._agent_id = agent_id

    def _sessions_dir(self) -> str:
        return agent_paths.sessions_dir(self._zone_id, self._agent_id)

    def _conv_path(self, session_id: str) -> str:
        return agent_paths.session_conversation(self._zone_id, self._agent_id, session_id)

    def _meta_path(self, session_id: str) -> str:
        return agent_paths.session_metadata(self._zone_id, self._agent_id, session_id)

    async def create(self) -> str:
        """Create a new empty session. Returns session_id."""
        session_id = uuid.uuid4().hex[:16]

        # Write initial metadata
        metadata = {
            "session_id": session_id,
            "agent_id": self._agent_id,
            "zone_id": self._zone_id,
            "status": "active",
        }
        meta_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        await self._sys_write(self._meta_path(session_id), meta_bytes)

        # Write empty conversation
        await self._sys_write(self._conv_path(session_id), b"[]")

        logger.info("Created session %s for agent %s", session_id, self._agent_id)
        return session_id

    async def latest(self) -> str | None:
        """Find the most recent session (--continue).

        Lists sessions dir, reads metadata for each, returns the one
        with the latest update timestamp. Returns None if no sessions exist.
        """
        try:
            entries = await self._sys_readdir(self._sessions_dir())
        except Exception:
            return None

        if not entries:
            return None

        # entries may be strings (paths) or dicts (with metadata)
        session_ids: list[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                path = entry.get("path", "")
                name = path.rstrip("/").rsplit("/", 1)[-1] if path else ""
            else:
                name = str(entry).rstrip("/").rsplit("/", 1)[-1]
            if name and not name.startswith("."):
                session_ids.append(name)

        if not session_ids:
            return None

        # Find most recent by reading metadata
        best_id: str | None = None
        best_time: float = 0
        for sid in session_ids:
            try:
                meta_bytes = await self._sys_read(self._meta_path(sid))
                meta = json.loads(meta_bytes)
                updated = float(meta.get("updated_at", 0))
                if updated > best_time:
                    best_time = updated
                    best_id = sid
            except Exception:
                # If metadata is missing/corrupt, still consider the session
                if best_id is None:
                    best_id = sid

        return best_id

    async def load(self, session_id: str) -> list[dict[str, Any]]:
        """Load conversation messages from a session (--resume <id>).

        Returns the messages list. Raises if session doesn't exist.
        """
        conv_bytes = await self._sys_read(self._conv_path(session_id))
        messages: list[dict[str, Any]] = json.loads(conv_bytes)
        logger.info(
            "Loaded session %s (%d messages) for agent %s",
            session_id,
            len(messages),
            self._agent_id,
        )
        return messages

    async def fork(self, source_id: str) -> str:
        """Fork a session (--fork-session).

        Creates a new session with a copy of the source conversation.
        CAS dedup: if content is identical, no extra storage used.
        """
        new_id = uuid.uuid4().hex[:16]

        # Copy conversation (CAS dedup = zero cost if identical)
        conv_bytes = await self._sys_read(self._conv_path(source_id))
        await self._sys_write(self._conv_path(new_id), conv_bytes)

        # Create new metadata
        metadata = {
            "session_id": new_id,
            "agent_id": self._agent_id,
            "zone_id": self._zone_id,
            "forked_from": source_id,
            "status": "active",
        }
        meta_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        await self._sys_write(self._meta_path(new_id), meta_bytes)

        logger.info(
            "Forked session %s → %s for agent %s",
            source_id,
            new_id,
            self._agent_id,
        )
        return new_id

    async def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Persist conversation + update metadata timestamp."""
        conv_bytes = json.dumps(messages, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        await self._sys_write(self._conv_path(session_id), conv_bytes)

        # Update metadata timestamp
        import time

        try:
            meta_bytes = await self._sys_read(self._meta_path(session_id))
            meta = json.loads(meta_bytes)
        except Exception:
            meta = {"session_id": session_id, "agent_id": self._agent_id}
        meta["updated_at"] = time.time()
        meta["message_count"] = len(messages)
        meta_bytes = json.dumps(meta, separators=(",", ":")).encode("utf-8")
        await self._sys_write(self._meta_path(session_id), meta_bytes)

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions with metadata."""
        try:
            entries = await self._sys_readdir(self._sessions_dir())
        except Exception:
            return []

        sessions: list[dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, dict):
                name = entry.get("path", "").rstrip("/").rsplit("/", 1)[-1]
            else:
                name = str(entry).rstrip("/").rsplit("/", 1)[-1]
            if not name or name.startswith("."):
                continue
            try:
                meta_bytes = await self._sys_read(self._meta_path(name))
                meta = json.loads(meta_bytes)
                sessions.append(meta)
            except Exception:
                sessions.append({"session_id": name, "status": "unknown"})

        return sessions
