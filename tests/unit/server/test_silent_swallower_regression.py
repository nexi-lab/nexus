"""Regression tests for silent swallower fixes (Issue #1254, Phase 3).

Verifies that formerly-silent exception handlers now log errors
instead of silently swallowing them.
"""

import ast
import inspect
import logging
from unittest.mock import MagicMock

import pytest


def _has_silent_swallower(source: str) -> tuple[bool, int]:
    """Check if source has any `except Exception: pass` patterns.

    Returns:
        Tuple of (found, line_number). line_number is 0 if not found.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ExceptHandler)
            and node.type
            and isinstance(node.type, ast.Name)
            and node.type.id == "Exception"
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Pass)
        ):
            return True, node.lineno
    return False, 0


class TestAuthHelperLogging:
    """Verify auth helper errors are logged, not silently swallowed."""

    def test_get_user_zones_logs_on_db_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """get_user_zones should log warning when rebac_list_tuples fails."""
        from nexus.lib.zone_helpers import get_user_zones

        mock_rebac = MagicMock()
        mock_rebac.rebac_list_tuples.side_effect = RuntimeError("DB connection lost")

        with caplog.at_level(logging.WARNING, logger="nexus.lib.zone_helpers"):
            result = get_user_zones(mock_rebac, "user-123")

        assert result == []
        assert "Failed to fetch zone IDs" in caplog.text


class TestOAuthCleanupLogging:
    """Verify OAuth cleanup errors are logged with specific types."""

    def test_oauth_service_uses_specific_exceptions(self) -> None:
        """oauth_service.py should not have except Exception: pass."""
        from nexus.services.oauth import oauth_service

        found, line = _has_silent_swallower(inspect.getsource(oauth_service))
        assert not found, f"Silent swallower found at line {line}"


class TestPermissionFilterLogging:
    """Verify permission filter chain errors are logged."""

    def test_no_silent_swallowers_in_filter_chain(self) -> None:
        """permission_filter_chain.py should not have except Exception: pass."""
        from nexus.bricks.rebac import permission_filter_chain

        found, line = _has_silent_swallower(inspect.getsource(permission_filter_chain))
        assert not found, f"Silent swallower found at line {line}"


class TestCacheWarmerLogging:
    """Verify cache warmer errors are logged."""

    def test_no_silent_swallowers_in_warmer(self) -> None:
        """warmer.py should not have except Exception: pass."""
        from nexus.server import cache_warmer as warmer

        found, line = _has_silent_swallower(inspect.getsource(warmer))
        assert not found, f"Silent swallower found at line {line}"


class TestFastAPIServerLogging:
    """Verify FastAPI server silent swallowers are fixed."""

    def test_no_silent_swallowers_in_fastapi_server(self) -> None:
        """fastapi_server.py should not have except Exception: pass."""
        from nexus.server import fastapi_server

        found, line = _has_silent_swallower(inspect.getsource(fastapi_server))
        assert not found, f"Silent swallower found at line {line}"


class TestNexusFSCoreLogging:
    """Verify nexus_fs.py silent swallowers are fixed."""

    def test_no_silent_swallowers_in_nexus_fs(self) -> None:
        """nexus_fs.py should not have except Exception: pass."""
        from nexus.core import nexus_fs

        found, line = _has_silent_swallower(inspect.getsource(nexus_fs))
        assert not found, f"Silent swallower found at line {line}"
