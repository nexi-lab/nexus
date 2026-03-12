"""Port conflict detection and resolution utilities.

Provides pre-flight port checking for `nexus up` and related commands.
Supports three resolution strategies: auto, prompt, and fail.
"""

from __future__ import annotations

import socket
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Default port assignments for Nexus services
DEFAULT_PORTS: dict[str, int] = {
    "http": 2026,
    "grpc": 2126,
    "postgres": 5432,
    "dragonfly": 6379,
    "zoekt": 6070,
}

# Human-readable labels for port display
PORT_LABELS: dict[str, str] = {
    "http": "Nexus HTTP",
    "grpc": "Nexus gRPC",
    "postgres": "PostgreSQL",
    "dragonfly": "DragonflyDB",
    "zoekt": "Zoekt Search",
}

# Strategies for resolving port conflicts
VALID_STRATEGIES = ("auto", "prompt", "fail")


def check_port_available(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a TCP port is available for binding.

    Args:
        port: Port number to check (1-65535).
        host: Host address to check against.

    Returns:
        True if the port is free, False if occupied.
    """
    if not 1 <= port <= 65535:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            result = sock.connect_ex((host, port))
            # connect_ex returns 0 if connection succeeded (port is in use)
            return result != 0
    except OSError:
        # If we can't even create the socket, treat port as unavailable
        return False


def find_free_port(preferred: int, host: str = "127.0.0.1", max_attempts: int = 100) -> int:
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
    host: str = "127.0.0.1",
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

    for service, port in ports.items():
        # Skip services not in the active set
        if services is not None and service not in services:
            resolved[service] = port
            continue

        if check_port_available(port, host):
            resolved[service] = port
        elif strategy == "fail":
            label = PORT_LABELS.get(service, service)
            from nexus.cli.utils import console

            console.print(f"[red]Error:[/red] Port {port} ({label}) is already in use.")
            console.print(
                "[yellow]Hint:[/yellow] Use --port-strategy auto to auto-select a free port."
            )
            sys.exit(1)
        elif strategy == "prompt":
            label = PORT_LABELS.get(service, service)
            import click

            new_port = click.prompt(
                f"Port {port} ({label}) is in use. Enter alternative port",
                type=int,
                default=find_free_port(port + 1, host),
            )
            resolved[service] = new_port
            messages.append(f"Port {port} ({label}) in use, using {new_port}")
        else:
            # strategy == "auto"
            new_port = find_free_port(port + 1, host)
            label = PORT_LABELS.get(service, service)
            resolved[service] = new_port
            messages.append(f"Port {port} ({label}) in use, selected {new_port}")

    return resolved, messages
