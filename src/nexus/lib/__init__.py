"""Tier-neutral library utilities — shared across all layers.

Contains reusable, zero-kernel-dependency modules that were previously
in ``nexus.contracts`` or ``nexus.core`` but are purely implementation
helpers rather than formal Protocol/ABC contracts or kernel logic.

Modules:
    context_utils: Context extraction helpers (zone_id, user identity, db URL)
    path_utils: Cached glob/pattern matching (path_matches_pattern)
    registry: Generic BaseRegistry[T] + BrickRegistry
    rpc_codec: JSON-RPC encode/decode with special-type handling
    rpc_decorator: @rpc_expose decorator for marking RPC-exposed methods
    zone_helpers: ReBAC-based zone group naming and membership checks
"""
