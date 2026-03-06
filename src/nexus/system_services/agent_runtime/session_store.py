"""Session store — checkpoint/restore conversation history via CAS.

Serializes conversation messages to JSONL format and persists them
through NexusFS (which stores content in CAS via ObjectStore).

Design doc: docs/design/AGENT-PROCESS-ARCHITECTURE.md §10 (step 5).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from nexus.contracts.llm_types import Message

if TYPE_CHECKING:
    from nexus.contracts.protocols.agent_vfs import AgentVFSProtocol
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Session file within the agent's home directory
_SESSION_FILENAME = "sessions/latest.jsonl"


class SessionStore:
    """Checkpoint conversation history to/from CAS as JSONL.

    Each line in the JSONL file is a serialized Message dict.
    Content is stored in NexusFS which deduplicates via CAS
    (content-addressed storage) automatically.
    """

    def __init__(self, vfs: AgentVFSProtocol) -> None:
        self._vfs = vfs
        self._line_cache: dict[str, list[str]] = {}  # pid → already-serialized lines

    async def save(
        self,
        pid: str,
        messages: list[Message],
        ctx: OperationContext,
        *,
        cwd: str = "/",
    ) -> str:
        """Serialize messages to JSONL and write to agent's session directory.

        Uses incremental serialization: only new messages (beyond what was
        already cached) are serialized, reducing CPU and allocation overhead
        on long conversations.

        Args:
            pid: Agent process ID (for logging).
            messages: Conversation history to checkpoint.
            ctx: Operation context for VFS permission checks.
            cwd: Agent's home directory.

        Returns:
            Path to the saved session file.
        """
        session_path = _resolve_session_path(cwd)

        # Incremental serialization: only serialize new messages
        cached = self._line_cache.get(pid, [])
        new_lines = [self._serialize_one(m) for m in messages[len(cached) :]]
        all_lines = [*cached, *new_lines]
        self._line_cache[pid] = all_lines

        content = "\n".join(all_lines)
        self._vfs.sys_write(session_path, content.encode("utf-8"), context=ctx)

        logger.debug(
            "Checkpoint saved: pid=%s, messages=%d (new=%d), path=%s",
            pid,
            len(messages),
            len(new_lines),
            session_path,
        )
        return session_path

    def clear_cache(self, pid: str) -> None:
        """Drop cached serialization for a process (e.g. on terminate)."""
        self._line_cache.pop(pid, None)

    @staticmethod
    def _serialize_one(msg: Message) -> str:
        """Serialize a single Message to a compact JSON line."""
        d = msg.model_dump()
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        return json.dumps(d, separators=(",", ":"))

    async def load(
        self,
        pid: str,
        ctx: OperationContext,
        *,
        cwd: str = "/",
    ) -> list[Message]:
        """Load messages from the agent's session JSONL file.

        Args:
            pid: Agent process ID (for logging).
            ctx: Operation context for VFS permission checks.
            cwd: Agent's home directory.

        Returns:
            List of deserialized Message objects. Empty list if no checkpoint.
        """
        session_path = _resolve_session_path(cwd)

        # Check if session file exists
        if not self._vfs.sys_access(session_path, context=ctx):
            logger.debug("No checkpoint found for pid=%s at %s", pid, session_path)
            return []

        # Read JSONL content
        raw = self._vfs.sys_read(session_path, context=ctx)
        if isinstance(raw, dict):
            raw = raw.get("content", b"")
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

        if not text.strip():
            return []

        # Deserialize each line
        messages: list[Message] = []
        for line_num, line in enumerate(text.strip().split("\n"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                messages.append(Message.from_dict(d))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning(
                    "Skipping invalid message at line %d in %s: %s",
                    line_num,
                    session_path,
                    exc,
                )

        logger.debug(
            "Checkpoint loaded: pid=%s, messages=%d, path=%s",
            pid,
            len(messages),
            session_path,
        )
        return messages


def _resolve_session_path(cwd: str) -> str:
    """Build the session file path from agent cwd."""
    if cwd.endswith("/"):
        return cwd + _SESSION_FILENAME
    return cwd + "/" + _SESSION_FILENAME
