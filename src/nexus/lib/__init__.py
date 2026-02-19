"""Tier-neutral library utilities — shared across all layers.

Contains reusable, zero-kernel-dependency modules that were previously
in ``nexus.contracts`` but are purely implementation helpers rather than
formal Protocol/ABC contracts.

Modules:
    registry: Generic BaseRegistry[T] + BrickRegistry
    rpc_codec: JSON-RPC encode/decode with special-type handling
"""
