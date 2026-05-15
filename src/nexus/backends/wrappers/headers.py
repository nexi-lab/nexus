"""Centralized magic header registry for wrapper content detection (#2362).

Prevents header collision between wrappers. Each wrapper registers its
magic bytes at import time; collision raises ImportError immediately.

All wrapper headers follow the format: 4-byte ASCII tag + 1-byte version.

Usage:
    from nexus.backends.wrappers.headers import COMPRESSED_HEADER, ENCRYPTED_HEADER

    # Register a new header for a future wrapper:
    MY_HEADER = register_header("my_wrapper", b"NEXM\\x01")
"""

WRAPPER_HEADERS: dict[str, bytes] = {}


def register_header(name: str, header: bytes) -> bytes:
    """Register a magic header for a wrapper, checking for prefix collisions.

    Must be called at module import time (protected by Python's import lock).
    Do not call at runtime from multiple threads.

    Args:
        name: Human-readable wrapper name (e.g., "compressed").
        header: Magic bytes to register.

    Returns:
        The registered header bytes (for assignment to module constants).

    Raises:
        ImportError: If the header overlaps (prefix-wise) with an existing one.
    """
    for existing_name, existing_header in WRAPPER_HEADERS.items():
        if header.startswith(existing_header) or existing_header.startswith(header):
            raise ImportError(
                f"Wrapper header collision: {name!r} ({header!r}) "
                f"overlaps with {existing_name!r} ({existing_header!r})"
            )
    WRAPPER_HEADERS[name] = header
    return header


COMPRESSED_HEADER = register_header("compressed", b"NEXZ\x01")
ENCRYPTED_HEADER = register_header("encrypted", b"NEXE\x01")
