"""Process lifecycle types — kernel-level process management (Issue #1509).

Pure value objects for the kernel ProcessTable. Zero runtime dependencies
on kernel/services/bricks — only stdlib + contracts.

    contracts/process_types.py = include/linux/sched.h (task_struct fields)

Defines:
    ProcessState     — finite state machine (CREATED → RUNNING → … → ZOMBIE)
    ProcessSignal    — POSIX-like signals (SIGTERM, SIGSTOP, SIGCONT, SIGKILL, SIGUSR1)
    ProcessKind      — MANAGED (nexusd-spawned) vs UNMANAGED (self-managed via gRPC)
    ProcessDescriptor — frozen PCB (Process Control Block)
    ExternalProcessInfo — connection metadata for external agents

See: core/process_table.py for the ProcessTable implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ProcessState(StrEnum):
    """Process lifecycle states — mirrors Linux task_struct.state."""

    CREATED = "created"
    RUNNING = "running"
    SLEEPING = "sleeping"
    STOPPED = "stopped"
    ZOMBIE = "zombie"


VALID_PROCESS_TRANSITIONS: dict[ProcessState, frozenset[ProcessState]] = {
    ProcessState.CREATED: frozenset({ProcessState.RUNNING}),
    ProcessState.RUNNING: frozenset(
        {ProcessState.SLEEPING, ProcessState.STOPPED, ProcessState.ZOMBIE}
    ),
    ProcessState.SLEEPING: frozenset(
        {ProcessState.RUNNING, ProcessState.STOPPED, ProcessState.ZOMBIE}
    ),
    ProcessState.STOPPED: frozenset({ProcessState.SLEEPING, ProcessState.ZOMBIE}),
    ProcessState.ZOMBIE: frozenset(),  # terminal
}


class ProcessSignal(StrEnum):
    """POSIX-like process signals."""

    SIGTERM = "SIGTERM"  # Graceful shutdown → ZOMBIE
    SIGSTOP = "SIGSTOP"  # Suspend → STOPPED
    SIGCONT = "SIGCONT"  # Resume → SLEEPING
    SIGKILL = "SIGKILL"  # Immediate kill + reap
    SIGUSR1 = "SIGUSR1"  # User-defined (agent steering)


class ProcessKind(StrEnum):
    """Process kind — who controls the lifecycle."""

    MANAGED = "managed"  # nexusd spawns + owns lifecycle (spawn/kill/signal)
    UNMANAGED = "unmanaged"  # external agent connects, self-managed (register/heartbeat)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProcessError(Exception):
    """Base exception for process operations."""


class ProcessNotFoundError(ProcessError):
    """Raised when a PID does not exist in the process table."""


class InvalidTransitionError(ProcessError):
    """Raised when a state transition is not allowed."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExternalProcessInfo:
    """Connection metadata for external (gRPC/MCP) processes."""

    connection_id: str
    host_pid: int | None = None
    remote_addr: str | None = None
    protocol: str = "grpc"  # "grpc" | "mcp" | "stdio"
    last_heartbeat: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProcessDescriptor:
    """Frozen PCB (Process Control Block) — kernel process descriptor.

    Immutable. Use ``dataclasses.replace()`` for state transitions.
    """

    # Identity
    pid: str
    ppid: str | None
    name: str
    owner_id: str
    zone_id: str
    kind: ProcessKind

    # Lifecycle
    state: ProcessState
    exit_code: int | None = None

    # Filesystem
    cwd: str = "/"
    root: str = "/"

    # Sub-processes
    children: tuple[str, ...] = ()

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # External-only metadata
    external_info: ExternalProcessInfo | None = None

    # Opaque extension — Kubernetes-style labels
    labels: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe dict."""
        d: dict[str, Any] = {
            "pid": self.pid,
            "ppid": self.ppid,
            "name": self.name,
            "owner_id": self.owner_id,
            "zone_id": self.zone_id,
            "kind": str(self.kind),
            "state": str(self.state),
            "exit_code": self.exit_code,
            "cwd": self.cwd,
            "root": self.root,
            "children": list(self.children),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "labels": dict(self.labels),
        }
        if self.external_info is not None:
            d["external_info"] = {
                "connection_id": self.external_info.connection_id,
                "host_pid": self.external_info.host_pid,
                "remote_addr": self.external_info.remote_addr,
                "protocol": self.external_info.protocol,
                "last_heartbeat": (
                    self.external_info.last_heartbeat.isoformat()
                    if self.external_info.last_heartbeat
                    else None
                ),
            }
        else:
            d["external_info"] = None
        return d

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProcessDescriptor:
        """Deserialize from dict."""
        ext_raw = d.get("external_info")
        ext_info = None
        if ext_raw is not None:
            hb = ext_raw.get("last_heartbeat")
            ext_info = ExternalProcessInfo(
                connection_id=ext_raw["connection_id"],
                host_pid=ext_raw.get("host_pid"),
                remote_addr=ext_raw.get("remote_addr"),
                protocol=ext_raw.get("protocol", "grpc"),
                last_heartbeat=datetime.fromisoformat(hb) if hb else None,
            )
        return cls(
            pid=d["pid"],
            ppid=d.get("ppid"),
            name=d["name"],
            owner_id=d["owner_id"],
            zone_id=d["zone_id"],
            kind=ProcessKind(d["kind"]),
            state=ProcessState(d["state"]),
            exit_code=d.get("exit_code"),
            cwd=d.get("cwd", "/"),
            root=d.get("root", "/"),
            children=tuple(d.get("children", ())),
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
            external_info=ext_info,
            labels=d.get("labels", {}),
        )

    @classmethod
    def from_json(cls, s: str) -> ProcessDescriptor:
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(s))
