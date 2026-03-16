"""Shared gRPC channel defaults for Nexus.

Centralizes message size limits, keepalive tuning, and base channel options
used by both RPCTransport (file operations) and RaftClient (metadata operations).

Issue #2938: gRPC channel option tuning.
"""

from __future__ import annotations

# Maximum gRPC message size (bytes) for all channels.
# 64 MB accommodates large file reads and unbounded list_metadata() responses.
# A per-channel split (e.g. 16 MB for metadata) is unsafe until list_metadata()
# enforces server-side pagination — see client.py:list_metadata(limit=0).
MAX_GRPC_MESSAGE_BYTES = 64 * 1024 * 1024  # 64 MB


def build_channel_options(
    *,
    max_message_bytes: int = MAX_GRPC_MESSAGE_BYTES,
    keepalive_time_ms: int = 30_000,
    keepalive_timeout_ms: int = 10_000,
) -> list[tuple[str, int]]:
    """Build gRPC channel options with message size and keepalive settings.

    Args:
        max_message_bytes: Max send/receive message size in bytes.
        keepalive_time_ms: Interval between keepalive pings (ms).
        keepalive_timeout_ms: Timeout waiting for keepalive response (ms).

    Returns:
        List of (option_name, value) tuples for grpc channel creation.
    """
    return [
        ("grpc.max_send_message_length", max_message_bytes),
        ("grpc.max_receive_message_length", max_message_bytes),
        ("grpc.keepalive_time_ms", keepalive_time_ms),
        ("grpc.keepalive_timeout_ms", keepalive_timeout_ms),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
    ]
