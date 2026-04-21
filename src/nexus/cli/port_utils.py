"""Port conflict detection and resolution utilities.

Provides pre-flight port checking for `nexus up` and related commands.
Supports three resolution strategies: auto, prompt, and fail.

Port derivation: when multiple instances run in different directories,
``derive_ports()`` hashes the ``data_dir`` path to produce deterministic,
stable port assignments that don't shift across restarts.
"""

from __future__ import annotations

import hashlib
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Default port assignments for Nexus services
# NOTE: gRPC default must match grpc/server.py client default (2028).
DEFAULT_PORTS: dict[str, int] = {
    "http": 2026,
    "grpc": 2028,
    "postgres": 5432,
    "dragonfly": 6379,
}

# Port range for hash-derived ports (10000–59999 gives 50k usable ports,
# well above the ephemeral range and below common system services).
_DERIVED_PORT_MIN = 10000
_DERIVED_PORT_RANGE = 50000

# Slot spacing — each instance gets 10 consecutive ports so services
# within the same project are contiguous and predictable.
_SLOT_SIZE = 10

# Human-readable labels for port display
PORT_LABELS: dict[str, str] = {
    "http": "Nexus HTTP",
    "grpc": "Nexus gRPC",
    "postgres": "PostgreSQL",
    "dragonfly": "DragonflyDB",
}

# Strategies for resolving port conflicts
VALID_STRATEGIES = ("auto", "prompt", "fail")


def derive_ports(data_dir: str | Path) -> dict[str, int]:
    """Derive deterministic port assignments from *data_dir*.

    Hashes the absolute path of *data_dir* to produce a stable offset
    into the 10000–59999 range.  The same directory always maps to the
    same ports, regardless of start order or how many other instances
    are running.

    The port layout within a slot (10 consecutive ports):
        +0  http
        +1  grpc
        +2  postgres
        +3  dragonfly
        +4…+9  reserved for future services
    """
    abs_path = str(Path(data_dir).resolve())
    digest = hashlib.sha256(abs_path.encode()).hexdigest()
    # Use first 8 hex chars → 0..4294967295, mod by available slots
    slot = int(digest[:8], 16) % (_DERIVED_PORT_RANGE // _SLOT_SIZE)
    base = _DERIVED_PORT_MIN + slot * _SLOT_SIZE

    return {
        "http": base,
        "grpc": base + 1,
        "postgres": base + 2,
        "dragonfly": base + 3,
    }


def check_port_available(port: int, host: str = "0.0.0.0") -> bool:
    """Check if a TCP port is available for binding.

    Attempts an actual bind on the port (both 0.0.0.0 and 127.0.0.1) to
    reliably detect ports held by Docker or other services.  A simple
    ``connect_ex`` probe misses ports that are *allocated* (e.g. by
    ``docker-proxy``) but not yet *listening*.

    Args:
        port: Port number to check (1-65535).
        host: Host address to bind against.

    Returns:
        True if the port is free, False if occupied.
    """
    if not 1 <= port <= 65535:
        return False

    # Try binding on the requested host AND 127.0.0.1 — Docker binds on
    # 0.0.0.0, so a port may appear free on 127.0.0.1 while Docker holds it.
    hosts_to_check = {host, "0.0.0.0", "127.0.0.1"}
    for bind_host in hosts_to_check:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                # Do NOT set SO_REUSEADDR — we want the bind to fail if
                # anything already holds this port.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                sock.bind((bind_host, port))
        except OSError:
            return False
    return True


def find_free_port(preferred: int, host: str = "0.0.0.0", max_attempts: int = 100) -> int:
    """Find a free port starting from the preferred port.

    Tries the preferred port first, then increments until a free port is found.

    Args:
        preferred: The preferred port number.
        host: Host address to check against.
        max_attempts: Maximum number of ports to try.

    Returns:
        A free port number.

    Raises:
        RuntimeError: If no free port is found within max_attempts.
    """
    for offset in range(max_attempts):
        candidate = preferred + offset
        if candidate > 65535:
            break
        if check_port_available(candidate, host):
            return candidate
    msg = f"No free port found starting from {preferred} (tried {max_attempts} ports)"
    raise RuntimeError(msg)


def resolve_ports(
    ports: dict[str, int],
    strategy: str = "auto",
    host: str = "0.0.0.0",
    services: Sequence[str] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Resolve port conflicts for a set of service ports.

    Args:
        ports: Mapping of service name to desired port.
        strategy: Resolution strategy — "auto", "prompt", or "fail".
        host: Host address to check against.
        services: If provided, only check ports for these services.
            Other services are passed through unchanged.

    Returns:
        Tuple of (resolved_ports, messages).
        resolved_ports: Mapping of service name to resolved port.
        messages: Human-readable messages about port changes.

    Raises:
        SystemExit: If strategy is "fail" and a port is occupied.
        ValueError: If strategy is invalid.
    """
    if strategy not in VALID_STRATEGIES:
        msg = f"Invalid port strategy: {strategy!r}. Must be one of {VALID_STRATEGIES}"
        raise ValueError(msg)

    resolved: dict[str, int] = {}
    messages: list[str] = []
    # Track ports already claimed in this pass to avoid self-conflicts
    # (e.g. http and grpc both resolving to the same free port).
    claimed: set[int] = set()

    for service, port in ports.items():
        # Skip services not in the active set
        if services is not None and service not in services:
            resolved[service] = port
            claimed.add(port)
            continue

        if check_port_available(port, host) and port not in claimed:
            resolved[service] = port
            claimed.add(port)
        elif strategy == "fail":
            label = PORT_LABELS.get(service, service)
            from nexus.cli.theme import console

            console.print(
                f"[nexus.error]Error:[/nexus.error] Port {port} ({label}) is already in use."
            )
            console.print(
                "[nexus.warning]Hint:[/nexus.warning] Use --port-strategy auto to auto-select a free port."
            )
            sys.exit(1)
        elif strategy == "prompt":
            label = PORT_LABELS.get(service, service)
            default_free = _find_free_unclaimed(port + 1, host, claimed)

            # In non-interactive contexts (CI, piped stdin), fall back to
            # "fail" behaviour instead of blocking on a prompt that nobody
            # will answer.  Matches the Create-React-App / Next.js pattern.
            if not sys.stdin.isatty():
                from nexus.cli.theme import console

                console.print(
                    f"[nexus.error]Error:[/nexus.error] Port {port} ({label}) is already in use "
                    f"(non-interactive terminal — cannot prompt)."
                )
                console.print(
                    f"[nexus.warning]Hint:[/nexus.warning] Use --port-strategy auto to auto-select "
                    f"a free port, or set the port explicitly (suggested: {default_free})."
                )
                sys.exit(1)

            import click

            new_port = click.prompt(
                f"Port {port} ({label}) is in use. Enter alternative port",
                type=int,
                default=default_free,
            )
            resolved[service] = new_port
            claimed.add(new_port)
            messages.append(f"Port {port} ({label}) in use, using {new_port}")
        else:
            # strategy == "auto"
            new_port = _find_free_unclaimed(port + 1, host, claimed)
            label = PORT_LABELS.get(service, service)
            resolved[service] = new_port
            claimed.add(new_port)
            messages.append(f"Port {port} ({label}) in use, selected {new_port}")

    return resolved, messages


def _find_free_unclaimed(
    preferred: int,
    host: str,
    claimed: set[int],
    max_attempts: int = 100,
) -> int:
    """Find a free port that is also not already claimed in this resolution pass."""
    for offset in range(max_attempts):
        candidate = preferred + offset
        if candidate > 65535:
            break
        if candidate not in claimed and check_port_available(candidate, host):
            return candidate
    msg = f"No free port found starting from {preferred} (tried {max_attempts} ports)"
    raise RuntimeError(msg)
