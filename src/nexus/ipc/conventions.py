"""Path conventions for filesystem-as-IPC.

Defines the directory layout for agent communication:

    /agents/{agent_id}/
        AGENT.json          # Agent card (capabilities, status)
        inbox/              # Incoming messages
        outbox/             # Sent messages (audit trail)
        processed/          # Successfully processed messages
        dead_letter/        # Failed messages

All functions are pure — they compose path strings with no I/O.
"""

from __future__ import annotations

from datetime import datetime

AGENTS_ROOT = "/agents"

# Subdirectory names within each agent's directory
INBOX_DIR = "inbox"
OUTBOX_DIR = "outbox"
PROCESSED_DIR = "processed"
DEAD_LETTER_DIR = "dead_letter"
AGENT_CARD_FILENAME = "AGENT.json"

# All directories auto-provisioned for each agent
AGENT_SUBDIRS: tuple[str, ...] = (
    INBOX_DIR,
    OUTBOX_DIR,
    PROCESSED_DIR,
    DEAD_LETTER_DIR,
)


def agent_dir(agent_id: str) -> str:
    """Root directory for an agent: ``/agents/{agent_id}``."""
    return f"{AGENTS_ROOT}/{agent_id}"


def inbox_path(agent_id: str) -> str:
    """Inbox directory: ``/agents/{agent_id}/inbox``."""
    return f"{AGENTS_ROOT}/{agent_id}/{INBOX_DIR}"


def outbox_path(agent_id: str) -> str:
    """Outbox directory: ``/agents/{agent_id}/outbox``."""
    return f"{AGENTS_ROOT}/{agent_id}/{OUTBOX_DIR}"


def processed_path(agent_id: str) -> str:
    """Processed directory: ``/agents/{agent_id}/processed``."""
    return f"{AGENTS_ROOT}/{agent_id}/{PROCESSED_DIR}"


def dead_letter_path(agent_id: str) -> str:
    """Dead letter directory: ``/agents/{agent_id}/dead_letter``."""
    return f"{AGENTS_ROOT}/{agent_id}/{DEAD_LETTER_DIR}"


def agent_card_path(agent_id: str) -> str:
    """Agent card file: ``/agents/{agent_id}/AGENT.json``."""
    return f"{AGENTS_ROOT}/{agent_id}/{AGENT_CARD_FILENAME}"


def message_filename(msg_id: str, timestamp: datetime) -> str:
    """Generate a sortable, unique message filename.

    Format: ``{ISO_timestamp}_{msg_id}.json``

    The timestamp prefix ensures ``ls --sort=name`` gives chronological
    ordering. The msg_id suffix ensures uniqueness.

    Args:
        msg_id: Unique message identifier (e.g. ``"msg_7f3a9b2c"``).
        timestamp: Message creation timestamp.

    Returns:
        Filename string like ``"20260212T100000_msg_7f3a9b2c.json"``.
    """
    ts = timestamp.strftime("%Y%m%dT%H%M%S")
    return f"{ts}_{msg_id}.json"


def message_path_in_inbox(agent_id: str, msg_id: str, timestamp: datetime) -> str:
    """Full path for a message in an agent's inbox."""
    return f"{inbox_path(agent_id)}/{message_filename(msg_id, timestamp)}"


def message_path_in_outbox(agent_id: str, msg_id: str, timestamp: datetime) -> str:
    """Full path for a message in an agent's outbox."""
    return f"{outbox_path(agent_id)}/{message_filename(msg_id, timestamp)}"


def message_path_in_processed(agent_id: str, msg_id: str, timestamp: datetime) -> str:
    """Full path for a message in an agent's processed directory."""
    return f"{processed_path(agent_id)}/{message_filename(msg_id, timestamp)}"


def message_path_in_dead_letter(agent_id: str, msg_id: str, timestamp: datetime) -> str:
    """Full path for a message in an agent's dead letter directory."""
    return f"{dead_letter_path(agent_id)}/{message_filename(msg_id, timestamp)}"
