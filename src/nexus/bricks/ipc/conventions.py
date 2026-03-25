"""Path conventions for filesystem-as-IPC.

Defines the directory layout for agent communication:

    /agents/{agent_id}/
        AGENT.json          # Agent card (capabilities, status)
        inbox/              # Incoming messages
        outbox/             # Sent messages (audit trail)
        processed/          # Successfully processed messages
        dead_letter/        # Failed messages
        tasks/              # Task persistence

All functions are pure — they compose path strings with no I/O.
"""

import re
from datetime import datetime

AGENTS_ROOT = "/agents"

# Agent ID validation pattern and limits
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9:_\-\.]+$")
MAX_AGENT_ID_LENGTH = 255

# Subdirectory names within each agent's directory
INBOX_DIR = "inbox"
OUTBOX_DIR = "outbox"
PROCESSED_DIR = "processed"
DEAD_LETTER_DIR = "dead_letter"
TASKS_DIR = "tasks"
NOTIFY_PIPE_NAME = "notify"
AGENT_CARD_FILENAME = "AGENT.json"

# All directories auto-provisioned for each agent
AGENT_SUBDIRS: tuple[str, ...] = (
    INBOX_DIR,
    OUTBOX_DIR,
    PROCESSED_DIR,
    DEAD_LETTER_DIR,
    TASKS_DIR,
)


def validate_agent_id(agent_id: str) -> str:
    """Validate and normalize agent ID per IPC conventions.

    Validates that agent_id:
    - Is non-empty after stripping whitespace
    - Does not exceed MAX_AGENT_ID_LENGTH (255 chars)
    - Matches AGENT_ID_PATTERN (alphanumeric, colon, underscore, hyphen, dot)
    - Does not contain path separators (prevents path traversal)

    Args:
        agent_id: The agent ID to validate.

    Returns:
        The normalized (stripped) agent ID.

    Raises:
        ValueError: If agent_id is invalid.

    Examples:
        >>> validate_agent_id("agent_123")
        'agent_123'
        >>> validate_agent_id("  agent:foo  ")
        'agent:foo'
        >>> validate_agent_id("")
        Traceback (most recent call last):
        ValueError: Agent ID must be non-empty
        >>> validate_agent_id("agent/../../etc/passwd")
        Traceback (most recent call last):
        ValueError: Agent ID must not contain path separators: 'agent/../../etc/passwd'
    """
    if not agent_id or not agent_id.strip():
        raise ValueError("Agent ID must be non-empty")

    agent_id = agent_id.strip()

    if len(agent_id) > MAX_AGENT_ID_LENGTH:
        raise ValueError(f"Agent ID exceeds {MAX_AGENT_ID_LENGTH} chars: {len(agent_id)}")

    if "/" in agent_id or "\\" in agent_id:
        raise ValueError(f"Agent ID must not contain path separators: {agent_id!r}")

    if not AGENT_ID_PATTERN.match(agent_id):
        raise ValueError(f"Agent ID contains invalid characters: {agent_id!r}")

    return agent_id


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


def notify_pipe_path(agent_id: str) -> str:
    """Notification pipe path: ``/agents/{agent_id}/notify``."""
    return f"{AGENTS_ROOT}/{agent_id}/{NOTIFY_PIPE_NAME}"


def tasks_path(agent_id: str) -> str:
    """Tasks directory: ``/agents/{agent_id}/tasks``."""
    return f"{AGENTS_ROOT}/{agent_id}/{TASKS_DIR}"


def task_file_path(agent_id: str, task_id: str, timestamp: datetime) -> str:
    """Full path for a task file in an agent's tasks directory.

    Format: ``/agents/{agent_id}/tasks/{ISO_timestamp}_{task_id}.json``

    The timestamp prefix ensures ``ls --sort=name`` gives chronological
    ordering.  The task_id suffix ensures uniqueness.
    """
    ts = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    return f"{tasks_path(agent_id)}/{ts}_{task_id}.json"


def task_dead_letter_path(agent_id: str) -> str:
    """Dead letter directory for deleted tasks: ``/agents/{agent_id}/tasks/_dead_letter``."""
    return f"{tasks_path(agent_id)}/_dead_letter"


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
