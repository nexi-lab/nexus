"""Tests for Python-level path utility behavior."""

import pytest

from nexus.contracts.exceptions import InvalidPathError
from nexus.core.path_utils import validate_path


class TestValidatePath:
    def test_allows_dotdot_inside_filename_components(self) -> None:
        assert validate_path("/workspace/file..txt") == "/workspace/file..txt"
        assert validate_path("/workspace/my..file.txt") == "/workspace/my..file.txt"
        assert validate_path("/workspace/..hidden/file") == "/workspace/..hidden/file"

    def test_rejects_dotdot_path_component(self) -> None:
        with pytest.raises(InvalidPathError, match="\\.\\."):
            validate_path("/workspace/../etc/passwd")

    def test_rejects_dot_path_component(self) -> None:
        with pytest.raises(InvalidPathError, match="Path contains"):
            validate_path("/workspace/./file.txt")
