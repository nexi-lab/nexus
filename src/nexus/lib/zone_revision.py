"""Revision token — read-your-writes fence across nodes (Issue #4737).

A mutation returns a *revision token* a later read can wait on::

    <anchor>@<index>

* **Path-anchored** (what writes return today): the anchor is the written
  path and the index is its ``gen`` after the write, e.g.
  ``/ws/a.txt@7``. Raft applies log entries strictly in order, so a node
  whose ``sys_stat`` shows the path at ``gen >= 7`` has applied every
  earlier entry of that zone as well. Fencing on the path therefore also
  fences directory listings, glob, grep and search for that zone — no
  kernel change and no zone-wide counter needed.
* **Zone-anchored** (optional, kernel-stamped): ``root@1234`` where the
  index is the zone's raft ``applied_index`` on the serving node. Emitted
  only when the kernel stamps ``zone_id`` / ``applied_index`` on the
  mutation response; honoured only when the kernel serves the
  ``federation_cluster_info`` Call. A bare integer is a zone token for
  ``root``.

This module is tier-neutral (``lib/``): ``kernel`` arguments are
duck-typed to ``KernelClient`` (``_call``) and ``fs`` arguments to
``NexusFS`` (``sys_stat``). Contract:
``docs/architecture/consistency-contract.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)

REVISION_HEADER = "X-Nexus-Revision"
MIN_REVISION_HEADER = "X-Nexus-Min-Revision"
REVISION_TIMEOUT_HEADER = "X-Nexus-Revision-Timeout-Ms"
MIN_REVISION_PARAM = "min_revision"
REVISION_TIMEOUT_PARAM = "revision_timeout_ms"

DEFAULT_REVISION_TIMEOUT_MS = 5_000
MAX_REVISION_TIMEOUT_MS = 30_000

CLUSTER_INFO_METHOD = "federation_cluster_info"

# Poll cadence for the wait loops (exponential backoff between the bounds).
_INITIAL_POLL_S = 0.02
_MAX_POLL_S = 0.25

_UNAVAILABLE_MARKERS = (
    "unknown call method",
    "method not found",
    "not available in subprocess mode",
    "federation not active",
)


class ZoneRevisionUnavailable(RuntimeError):
    """The serving kernel cannot report ``applied_index`` for a zone."""


@dataclass(frozen=True, slots=True)
class RevisionToken:
    """``<anchor>@<index>``: a path + gen, or a zone + applied_index."""

    anchor: str
    index: int

    @property
    def is_path(self) -> bool:
        return self.anchor.startswith("/")

    def __str__(self) -> str:
        return f"{self.anchor}@{self.index}"

    @classmethod
    def parse(cls, raw: str, *, default_zone: str = ROOT_ZONE_ID) -> RevisionToken:
        """Parse ``anchor@index`` or a bare ``index`` (zone token for ``default_zone``).

        Splits on the *last* ``@`` so path anchors may themselves contain
        ``@``. Raises ``ValueError`` on malformed input.
        """
        text = (raw or "").strip()
        if not text:
            raise ValueError("revision token is empty")
        anchor, sep, index_text = text.rpartition("@")
        if not sep:
            anchor, index_text = default_zone, text
        anchor = anchor.strip()
        if not anchor:
            raise ValueError(f"revision token has an empty anchor: {raw!r}")
        if not anchor.startswith("/") and any(c.isspace() for c in anchor):
            raise ValueError(f"revision token has an invalid zone id: {raw!r}")
        if not index_text.strip().isdigit():
            raise ValueError(f"revision token index must be a non-negative integer: {raw!r}")
        return cls(anchor=anchor, index=int(index_text))


def _field(result: Any, name: str) -> Any:
    if isinstance(result, dict):
        return result.get(name)
    return getattr(result, name, None)


def _positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def revision_from_result(result: Any, *, path: str | None = None) -> RevisionToken | None:
    """Build the token a kernel mutation result should return.

    Prefers a kernel-stamped zone revision (``zone_id`` + ``applied_index``
    on the result). Otherwise, when ``path`` is given and the result
    carries a positive ``gen``, returns the path-anchored token. ``None``
    when neither is available — callers surface ``revision: null`` rather
    than guess.
    """
    if result is None:
        return None
    zone_id = _field(result, "zone_id")
    applied_index = _positive_int(_field(result, "applied_index"))
    if zone_id and applied_index is not None:
        return RevisionToken(anchor=str(zone_id), index=applied_index)
    if path and path.startswith("/"):
        gen = _positive_int(_field(result, "gen"))
        if gen is not None:
            return RevisionToken(anchor=path, index=gen)
    return None


def revision_token(result: Any, *, path: str | None = None) -> str | None:
    """``str(revision_from_result(...))`` or ``None``."""
    rev = revision_from_result(result, path=path)
    return str(rev) if rev is not None else None


def revision_fields(response: Any) -> dict[str, Any]:
    """Normalise the optional kernel-stamped revision fields on a typed response.

    ``zone_id`` / ``applied_index`` are additive proto fields a kernel may
    stamp on mutating responses. Absent (or zero) on the pinned kernel,
    in which case both come back as ``None``.
    """
    zone_id = getattr(response, "zone_id", "") or None
    try:
        applied_index = int(getattr(response, "applied_index", 0) or 0)
    except (TypeError, ValueError):
        applied_index = 0
    return {"zone_id": zone_id, "applied_index": applied_index or None}


# ---------------------------------------------------------------------------
# Path-anchored fence: sys_stat gen on the serving node
# ---------------------------------------------------------------------------


def read_path_gen(fs: Any, path: str, context: Any = None) -> int:
    """Return the ``gen`` of ``path`` as this node currently sees it (0 if absent).

    Goes through ``fs.sys_stat`` so permission hooks apply — the fence must
    not become an existence oracle for paths the caller cannot read.
    """
    meta = fs.sys_stat(path, context=context)
    if inspect.isawaitable(meta):  # pragma: no cover — sync NexusFS in production
        raise TypeError("read_path_gen requires a synchronous sys_stat; use await_path_gen")
    return _gen_of(meta)


async def _stat_async(fs: Any, path: str, context: Any) -> Any:
    meta = await asyncio.to_thread(fs.sys_stat, path, context=context)
    if inspect.isawaitable(meta):
        meta = await meta
    return meta


def _gen_of(meta: Any) -> int:
    if not meta:
        return 0
    gen = _field(meta, "gen")
    if gen is None:
        gen = _field(meta, "version")
    try:
        return max(int(gen or 0), 0)
    except (TypeError, ValueError):
        return 0


async def await_path_gen(
    fs: Any,
    path: str,
    min_gen: int,
    *,
    timeout_s: float,
    context: Any = None,
) -> tuple[bool, int]:
    """Wait until this node's ``sys_stat(path).gen >= min_gen``.

    Returns ``(satisfied, current_gen)``; ``current_gen`` is 0 while the
    path is not visible here. ``timeout_s <= 0`` probes once.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(timeout_s), 0.0)
    interval = _INITIAL_POLL_S
    while True:
        current = _gen_of(await _stat_async(fs, path, context))
        if current >= min_gen:
            return True, current
        now = loop.time()
        if now >= deadline:
            return False, current
        await asyncio.sleep(min(interval, deadline - now))
        interval = min(interval * 2, _MAX_POLL_S)


# ---------------------------------------------------------------------------
# Zone-anchored fence: kernel applied_index (optional, kernel-stamped)
# ---------------------------------------------------------------------------

# Kernels that answered "unknown method" for the cluster-info Call. The
# answer is fixed for the lifetime of a kernel binary, so remember it and
# stop paying a round-trip per fenced read. Keyed weakly so a dropped
# KernelClient does not pin the entry.
_unavailable_kernels: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()


def reset_zone_revision_cache() -> None:
    """Forget cached "kernel lacks cluster-info" verdicts (tests)."""
    _unavailable_kernels.clear()


def _remember_unavailable(kernel: Any, reason: str) -> None:
    # Not weak-referenceable → skip the cache and re-probe next time.
    with contextlib.suppress(TypeError):
        _unavailable_kernels[kernel] = reason


def _cached_unavailable(kernel: Any) -> str | None:
    try:
        return _unavailable_kernels.get(kernel)
    except TypeError:
        return None


def _looks_unavailable(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _UNAVAILABLE_MARKERS)


def read_zone_revision(kernel: Any, zone_id: str) -> int:
    """Return the serving node's ``applied_index`` for ``zone_id``.

    Raises :class:`ZoneRevisionUnavailable` when the kernel does not
    expose ``federation_cluster_info`` (the pinned nexusd-cluster does
    not) or does not host the zone. Transport errors propagate unchanged.
    """
    if kernel is None:
        raise ZoneRevisionUnavailable("no kernel attached to this NexusFS")
    cached = _cached_unavailable(kernel)
    if cached is not None:
        raise ZoneRevisionUnavailable(cached)
    try:
        info = kernel._call(CLUSTER_INFO_METHOD, {"zone_id": zone_id})
    except ZoneRevisionUnavailable:
        raise
    except Exception as exc:
        if _looks_unavailable(exc):
            reason = f"kernel does not expose {CLUSTER_INFO_METHOD}: {exc}"
            _remember_unavailable(kernel, reason)
            logger.info("zone revision unavailable — %s", reason)
            raise ZoneRevisionUnavailable(reason) from exc
        raise
    if not isinstance(info, dict):
        raise ZoneRevisionUnavailable(
            f"{CLUSTER_INFO_METHOD} returned {type(info).__name__}, expected an object"
        )
    if info.get("available") is False:
        # Python-side standalone stub shape (federation_rpc.py) — not a
        # kernel verdict, so do not cache it.
        raise ZoneRevisionUnavailable(
            str(info.get("reason") or f"{CLUSTER_INFO_METHOD} reports federation unavailable")
        )
    if info.get("has_store") is False:
        raise ZoneRevisionUnavailable(f"zone {zone_id!r} is not hosted by this node")
    try:
        return int(info.get("applied_index", 0))
    except (TypeError, ValueError) as exc:
        raise ZoneRevisionUnavailable(
            f"{CLUSTER_INFO_METHOD} returned a non-integer applied_index"
        ) from exc


def wait_for_zone_revision(
    kernel: Any,
    zone_id: str,
    min_index: int,
    *,
    timeout_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, int]:
    """Block until the node's ``applied_index`` for ``zone_id`` reaches ``min_index``.

    Returns ``(satisfied, current_index)``. ``timeout_s <= 0`` probes once.
    Propagates :class:`ZoneRevisionUnavailable` from the first probe.
    """
    deadline = clock() + max(float(timeout_s), 0.0)
    interval = _INITIAL_POLL_S
    while True:
        current = read_zone_revision(kernel, zone_id)
        if current >= min_index:
            return True, current
        now = clock()
        if now >= deadline:
            return False, current
        sleep(min(interval, deadline - now))
        interval = min(interval * 2, _MAX_POLL_S)


async def await_zone_revision(
    kernel: Any,
    zone_id: str,
    min_index: int,
    *,
    timeout_s: float,
) -> tuple[bool, int]:
    """Async twin of :func:`wait_for_zone_revision` (probes on a worker thread)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(timeout_s), 0.0)
    interval = _INITIAL_POLL_S
    while True:
        current = await asyncio.to_thread(read_zone_revision, kernel, zone_id)
        if current >= min_index:
            return True, current
        now = loop.time()
        if now >= deadline:
            return False, current
        await asyncio.sleep(min(interval, deadline - now))
        interval = min(interval * 2, _MAX_POLL_S)


__all__ = [
    "CLUSTER_INFO_METHOD",
    "DEFAULT_REVISION_TIMEOUT_MS",
    "MAX_REVISION_TIMEOUT_MS",
    "MIN_REVISION_HEADER",
    "MIN_REVISION_PARAM",
    "REVISION_HEADER",
    "REVISION_TIMEOUT_HEADER",
    "REVISION_TIMEOUT_PARAM",
    "RevisionToken",
    "ZoneRevisionUnavailable",
    "await_path_gen",
    "await_zone_revision",
    "read_path_gen",
    "read_zone_revision",
    "reset_zone_revision_cache",
    "revision_fields",
    "revision_from_result",
    "revision_token",
    "wait_for_zone_revision",
]
