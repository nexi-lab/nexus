"""``nexusd`` — Nexus node daemon.

Starts a long-running Nexus node process that exposes gRPC/HTTP APIs.
Manages local storage, serves RPC requests, and participates in federation.

Usage::

    nexusd                              # defaults: port 2026, auto profile
    nexusd --port 2026 --host 0.0.0.0
    nexusd --config /etc/nexus/config.yaml
    nexusd --log-level debug

See ``docs/architecture/cli-design.md`` for the full ``nexus``/``nexusd`` split.
"""

from nexus.daemon.main import main

__all__ = ["main"]
