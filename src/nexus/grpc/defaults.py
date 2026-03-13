"""Shared gRPC channel defaults for Nexus.

Centralizes message size limits, keepalive tuning, and base channel options
used by both RPCTransport (file operations) and RaftClient (metadata operations).

Issue #2938: gRPC channel option tuning.
"""

from __future__ import annotations

# Message size limits (bytes).
# Content operations (file read/write) need larger limits than metadata.
MAX_CONTENT_MESSAGE_BYTES = 64 * 1024 * 1024  # 64 MB — RPCTransport (file ops)
MAX_METADATA_MESSAGE_BYTES = 16 * 1024 * 1024  # 16 MB — RaftClient (metadata ops)


def build_channel_options(
    *,
    max_message_bytes: int = MAX_CONTENT_MESSAGE_BYTES,
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
