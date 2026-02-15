"""Unit tests for _compat — pool creation and version detection."""

from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor
from unittest.mock import patch

import pytest

from nexus.isolation._compat import SUPPORTS_SUBINTERPRETERS, create_isolation_pool


class TestSupportsSubinterpreters:
    def test_flag_matches_runtime(self) -> None:
        expected = sys.version_info >= (3, 14)
        assert SUPPORTS_SUBINTERPRETERS is expected


class TestCreateIsolationPool:
    def test_force_process_returns_process_pool(self) -> None:
        pool = create_isolation_pool(2, force_process=True)
        try:
            assert isinstance(pool, ProcessPoolExecutor)
        finally:
            pool.shutdown(wait=False)

    def test_below_314_returns_process_pool(self) -> None:
        with patch("nexus.isolation._compat.SUPPORTS_SUBINTERPRETERS", False):
            pool = create_isolation_pool(2)
            try:
                assert isinstance(pool, ProcessPoolExecutor)
            finally:
                pool.shutdown(wait=False)

    @pytest.mark.skipif(
        sys.version_info < (3, 14),
        reason="InterpreterPoolExecutor only available on 3.14+",
    )
    def test_314_returns_interpreter_pool(self) -> None:
        from concurrent.futures import InterpreterPoolExecutor  # type: ignore[attr-defined]

        pool = create_isolation_pool(2)
        try:
            assert isinstance(pool, InterpreterPoolExecutor)
        finally:
            pool.shutdown(wait=False)

    def test_pool_size_respected(self) -> None:
        pool = create_isolation_pool(4, force_process=True)
        try:
            assert pool._max_workers == 4  # type: ignore[attr-defined]
        finally:
            pool.shutdown(wait=False)

    def test_force_process_overrides_314(self) -> None:
        with patch("nexus.isolation._compat.SUPPORTS_SUBINTERPRETERS", True):
            pool = create_isolation_pool(2, force_process=True)
            try:
                assert isinstance(pool, ProcessPoolExecutor)
            finally:
                pool.shutdown(wait=False)
