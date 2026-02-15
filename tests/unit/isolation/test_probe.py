"""Unit tests for _probe — sub-interpreter compatibility probing."""

from __future__ import annotations

import sys
from unittest.mock import patch

from nexus.isolation._probe import probe_subinterpreter_compat


class TestProbeSubinterpreterCompat:
    def test_below_314_always_true(self) -> None:
        """On Python < 3.14 we use ProcessPool, so probe always returns True."""
        with patch("nexus.isolation._probe.sys") as mock_sys:
            mock_sys.version_info = (3, 12, 0)
            assert probe_subinterpreter_compat("os") is True

    def test_stdlib_module_compatible(self) -> None:
        """stdlib modules like ``json`` are sub-interpreter safe on any Python."""
        # On < 3.14 this returns True immediately; on 3.14+ it does the real check.
        assert probe_subinterpreter_compat("json") is True

    def test_nonexistent_module(self) -> None:
        """A nonexistent module should return False on 3.14+ or True on < 3.14."""
        result = probe_subinterpreter_compat("__no_such_module_xyz__")
        if sys.version_info >= (3, 14):
            assert result is False
        else:
            assert result is True  # short-circuits before import attempt

    def test_returns_bool(self) -> None:
        result = probe_subinterpreter_compat("os")
        assert isinstance(result, bool)
