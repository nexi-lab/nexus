"""Shared helpers for the `nexus hub` CLI command group.

Kept separate from `hub.py` so that the subcommand file stays focused on
Click wiring and the helpers can be unit-tested in isolation.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

_DURATION_RE = re.compile(r"^(\d+)([dhm])$")


def get_session_factory() -> Callable[[], Session]:
    """Build a SQLAlchemy session factory from NEXUS_DATABASE_URL.

    Hub CLI talks to the DB directly; it is expected to run on the same
    host as the nexus server (see spec §Token/admin model — bootstrap).
    """
    db_url = os.environ.get("NEXUS_DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "NEXUS_DATABASE_URL is not set — `nexus hub` must run on the hub host "
            "with access to the nexus database."
        )
    engine = create_engine(db_url, future=True)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def parse_duration(text: str) -> timedelta:
    """Parse '90d', '6h', '30m' style durations used by `--expires`."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise ValueError(f"invalid duration {text!r}: expected Nd / Nh / Nm (e.g. 90d)")
    value, unit = int(match.group(1)), match.group(2)
    return {
        "d": timedelta(days=value),
        "h": timedelta(hours=value),
        "m": timedelta(minutes=value),
    }[unit]


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a simple fixed-width text table. Used for human output."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)
