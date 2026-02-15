"""Tests for nexus.server._version (Issue #761)."""

from __future__ import annotations

from unittest.mock import patch

from nexus.server._version import get_nexus_version


class TestGetNexusVersion:
    """Tests for the shared get_nexus_version() utility."""

    def test_returns_version_string(self) -> None:
        result = get_nexus_version()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_unknown_on_error(self) -> None:
        with patch(
            "importlib.metadata.version",
            side_effect=Exception("package not found"),
        ):
            result = get_nexus_version()
        assert result == "unknown"

    def test_returns_unknown_when_package_missing(self) -> None:
        from importlib.metadata import PackageNotFoundError

        with patch(
            "importlib.metadata.version",
            side_effect=PackageNotFoundError("nexus-ai-fs"),
        ):
            result = get_nexus_version()
        assert result == "unknown"

    def test_returns_actual_version_when_installed(self) -> None:
        with patch("importlib.metadata.version", return_value="1.2.3"):
            result = get_nexus_version()
        assert result == "1.2.3"
