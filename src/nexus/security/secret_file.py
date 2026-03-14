"""Secure file writing for secrets (private keys, credentials, etc.).

Ensures files are created with restrictive permissions atomically,
avoiding the TOCTOU race of write-then-chmod where the file is briefly
world-readable.
"""

import os
from pathlib import Path


def write_secret_file(path: Path, data: str | bytes, *, mode: int = 0o600) -> None:
    """Write data to a file with restrictive permissions, atomically.

    Uses ``os.open`` with explicit mode bits so the file is never
    world-readable, even briefly.

    Args:
        path: Destination file path.
        data: Content to write (str or bytes).
        mode: File permission bits (default ``0o600`` — owner read/write only).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, mode)
    try:
        # Enforce mode even when overwriting an existing file — os.open only
        # applies mode on create; O_TRUNC preserves old permission bits.
        os.fchmod(fd, mode)
        if isinstance(data, str):
            os.write(fd, data.encode())
        else:
            os.write(fd, data)
    finally:
        os.close(fd)
