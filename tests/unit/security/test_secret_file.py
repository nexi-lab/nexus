"""Regression tests for write_secret_file — Issue #2960 C4+H2.

Ensures private keys and other secrets are written with restrictive
permissions atomically, never briefly world-readable.
"""

import os
import stat
from pathlib import Path

from nexus.security.secret_file import write_secret_file


class TestWriteSecretFile:
    """Regression: C4 (WireGuard key world-readable) + H2 (TLS key TOCTOU)."""

    def test_creates_file_with_0o600_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.key"
        write_secret_file(path, "supersecretkey")

        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_writes_string_content(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.txt"
        write_secret_file(path, "hello world")
        assert path.read_text() == "hello world"

    def test_writes_bytes_content(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.bin"
        data = b"\x00\x01\x02\xff"
        write_secret_file(path, data)
        assert path.read_bytes() == data

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "secret.key"
        write_secret_file(path, "key data")
        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.key"
        write_secret_file(path, "old data")
        write_secret_file(path, "new data")
        assert path.read_text() == "new data"
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_tightens_permissions_on_existing_loose_file(self, tmp_path: Path) -> None:
        """Regression: overwriting a pre-existing 0o644 file must tighten to 0o600."""
        path = tmp_path / "secret.key"
        path.write_text("old data")  # Creates with default umask (typically 0o644)
        os.chmod(path, 0o644)  # Ensure it's world-readable
        assert stat.S_IMODE(path.stat().st_mode) == 0o644

        write_secret_file(path, "new secret")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"Overwriting a 0o644 file must tighten to 0o600, got {oct(mode)}"

    def test_custom_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.key"
        write_secret_file(path, "data", mode=0o400)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o400

    def test_file_never_world_readable(self, tmp_path: Path) -> None:
        """The core regression: file must be created with restrictive mode,
        never via write-then-chmod which leaves a brief window."""
        path = tmp_path / "secret.key"
        write_secret_file(path, "private key data")

        mode = stat.S_IMODE(path.stat().st_mode)
        # No group or other bits should be set
        assert not (mode & stat.S_IRGRP), "Group read bit must not be set"
        assert not (mode & stat.S_IWGRP), "Group write bit must not be set"
        assert not (mode & stat.S_IROTH), "Other read bit must not be set"
        assert not (mode & stat.S_IWOTH), "Other write bit must not be set"
