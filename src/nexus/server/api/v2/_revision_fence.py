"""Read-your-writes fence for HTTP reads (Issue #4737).

A caller that holds the ``revision`` a mutation returned (``/ws/a.txt@7``)
can ask any read — ``/files/read``, ``/files/metadata``, ``/files/list``,
``/files/glob``, ``/search/query``, ``/search/grep``, … — to observe at
least that revision by sending::

    X-Nexus-Min-Revision: /ws/a.txt@7        (or ?min_revision=/ws/a.txt@7)
    X-Nexus-Revision-Timeout-Ms: 5000        (or ?revision_timeout_ms=5000)

The fence waits until the serving node's ``sys_stat`` of the anchor path
shows ``gen >= 7`` (bounded by the timeout, default 5 s, max 30 s), then
lets the read run and stamps ``X-Nexus-Revision`` with what it observed.
Because raft applies entries in order, a node that shows the path at that
gen has applied every earlier entry of the zone, so listings, glob, grep
and search for that zone include the write too. If the deadline passes
first the request fails with ``412 Precondition Failed`` carrying the
current revision — never a stale or ``metadata: None`` answer dressed up
as success.

Zone-anchored tokens (``root@1234``, kernel ``applied_index``) are
accepted for kernels that stamp them; on the pinned kernel they answer
``501 Not Implemented`` so the caller learns the guarantee is absent.
Unfenced reads are untouched: no header, no extra round-trip.

Contract: ``docs/architecture/consistency-contract.md``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Header, HTTPException, Query
from fastapi.responses import Response

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.zone_revision import (
    DEFAULT_REVISION_TIMEOUT_MS,
    MAX_REVISION_TIMEOUT_MS,
    MIN_REVISION_HEADER,
    MIN_REVISION_PARAM,
    REVISION_HEADER,
    REVISION_TIMEOUT_HEADER,
    REVISION_TIMEOUT_PARAM,
    RevisionToken,
    ZoneRevisionUnavailable,
    await_path_gen,
    await_zone_revision,
)

logger = logging.getLogger(__name__)


@dataclass
class RevisionFence:
    """Per-request fence state — built by :func:`get_revision_fence`."""

    required: RevisionToken | None = None
    timeout_ms: int = DEFAULT_REVISION_TIMEOUT_MS
    #: Revision observed on the serving node once :meth:`enforce` ran.
    observed: RevisionToken | None = None

    @property
    def active(self) -> bool:
        return self.required is not None

    async def enforce(self, fs: Any, context: Any = None) -> None:
        """Wait for the required revision or fail with 412 / 501 / 503.

        No-op when the request did not ask for a fence. Call it inside the
        route's work function *before* the read so the read observes the
        applied state. ``context`` is the caller's ``OperationContext``;
        path-anchored fences stat through it so permission hooks apply.
        """
        required = self.required
        if required is None:
            return
        if fs is None:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")
        started = time.monotonic()
        timeout_s = self.timeout_ms / 1000.0
        if required.is_path:
            satisfied, current = await await_path_gen(
                fs, required.anchor, required.index, timeout_s=timeout_s, context=context
            )
        else:
            satisfied, current = await self._await_zone(fs, required, timeout_s)

        self.observed = RevisionToken(anchor=required.anchor, index=current)
        if not satisfied:
            waited_ms = int((time.monotonic() - started) * 1000)
            what = "path" if required.is_path else "zone"
            raise HTTPException(
                status_code=412,
                detail={
                    "error": "revision_not_applied",
                    "message": (
                        f"This node sees {what} {required.anchor!r} at revision {current}; "
                        f"{required.index} was required. Waited {waited_ms} ms."
                    ),
                    "min_revision": str(required),
                    "current_revision": str(self.observed),
                    "waited_ms": waited_ms,
                },
                headers={REVISION_HEADER: str(self.observed)},
            )

    @staticmethod
    async def _await_zone(fs: Any, required: RevisionToken, timeout_s: float) -> tuple[bool, int]:
        kernel = getattr(fs, "_kernel", None)
        try:
            return await await_zone_revision(
                kernel, required.anchor, required.index, timeout_s=timeout_s
            )
        except ZoneRevisionUnavailable as exc:
            raise HTTPException(
                status_code=501,
                detail={
                    "error": "zone_revision_unavailable",
                    "message": (
                        "This server cannot fence reads on a zone revision: "
                        f"{exc}. Use the path-anchored revision a write returns."
                    ),
                    "min_revision": str(required),
                },
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("zone revision probe failed for %s: %s", required, exc)
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "zone_revision_probe_failed",
                    "message": f"Could not read the zone revision from the kernel: {exc}",
                    "min_revision": str(required),
                },
            ) from exc

    def stamp(self, response: Response | None) -> None:
        """Set ``X-Nexus-Revision`` on ``response`` if a fence ran."""
        if response is None or self.observed is None:
            return
        response.headers[REVISION_HEADER] = str(self.observed)


def _parse_min_revision(raw: str | None, source: str) -> RevisionToken | None:
    if raw is None or not raw.strip():
        return None
    try:
        return RevisionToken.parse(raw, default_zone=ROOT_ZONE_ID)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {source}: {exc}. Expected '<path>@<gen>' as returned by a write.",
        ) from exc


def _parse_timeout(raw: str | None, source: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid {source}: must be an integer number of ms"
        ) from exc
    if value < 0 or value > MAX_REVISION_TIMEOUT_MS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {source}: must be between 0 and {MAX_REVISION_TIMEOUT_MS} ms",
        )
    return value


def get_revision_fence(
    min_revision: str | None = Query(
        None,
        alias=MIN_REVISION_PARAM,
        description=(
            "Read-your-writes fence: wait until this node has applied the given "
            "revision ('<path>@<gen>', as returned in the 'revision' field / "
            "X-Nexus-Revision header of a mutation) before serving the read. "
            "412 with the current revision if it is not applied within the timeout."
        ),
    ),
    min_revision_header: str | None = Header(
        None,
        alias=MIN_REVISION_HEADER,
        description="Header form of min_revision (takes precedence).",
    ),
    revision_timeout_ms: str | None = Query(
        None,
        alias=REVISION_TIMEOUT_PARAM,
        description=(
            f"How long to wait for min_revision (default {DEFAULT_REVISION_TIMEOUT_MS}, "
            f"max {MAX_REVISION_TIMEOUT_MS} ms; 0 probes once)."
        ),
    ),
    revision_timeout_header: str | None = Header(
        None,
        alias=REVISION_TIMEOUT_HEADER,
        description="Header form of revision_timeout_ms (takes precedence).",
    ),
) -> RevisionFence:
    """FastAPI dependency: parse the fence headers / query params."""
    required = _parse_min_revision(min_revision_header, MIN_REVISION_HEADER)
    if required is None:
        required = _parse_min_revision(min_revision, MIN_REVISION_PARAM)
    timeout = _parse_timeout(revision_timeout_header, REVISION_TIMEOUT_HEADER)
    if timeout is None:
        timeout = _parse_timeout(revision_timeout_ms, REVISION_TIMEOUT_PARAM)
    return RevisionFence(
        required=required,
        timeout_ms=DEFAULT_REVISION_TIMEOUT_MS if timeout is None else timeout,
    )


def stamp_revision(response: Response, token: str | None) -> None:
    """Set ``X-Nexus-Revision: <token>`` on a mutation response, if known."""
    if token:
        response.headers[REVISION_HEADER] = token


__all__ = [
    "REVISION_HEADER",
    "RevisionFence",
    "get_revision_fence",
    "stamp_revision",
]
