"""ZonePhase — string enum for the ``ZoneModel.phase`` column.

The values mirror the strings stored in the ``zones.phase`` column (a
``VARCHAR(12) NOT NULL DEFAULT 'Active'`` per
``storage.schema_invariants`` + ``storage.models.auth.ZoneModel``); using a
``StrEnum`` lets call sites compare and assign without retyping the
literal at every callsite.

This module is intentionally small so it can be imported by both
boundary layers (HTTP handlers, lifecycle code) and the storage tier
without dragging in protocol / dataclass definitions.  It used to live in
``contracts.protocols.zone_lifecycle`` alongside the K8s-finalizer
protocol; that file is removed as part of the zone-teardown
simplification (PR 7b), so the enum was promoted here.
"""

from __future__ import annotations

from enum import StrEnum


class ZonePhase(StrEnum):
    """Zone lifecycle phase as recorded on ``ZoneModel.phase``.

    Two values are observable in normal operation:

    - ``ACTIVE``: the zone is in service.
    - ``TERMINATED``: the zone has been torn down (DELETE handler ran).

    A third value, ``TERMINATING``, used to mark the in-flight destroy
    window for the now-removed K8s-finalizer pattern.  It is kept so
    existing rows that may still carry the value compare equal, but new
    transitions go straight from ``ACTIVE`` to ``TERMINATED``.
    """

    ACTIVE = "Active"
    TERMINATING = "Terminating"
    TERMINATED = "Terminated"
