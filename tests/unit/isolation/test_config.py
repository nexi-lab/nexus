"""Unit tests for IsolationConfig."""

from __future__ import annotations

import pickle

import pytest

from nexus.isolation.config import IsolationConfig


class TestIsolationConfigDefaults:
    def test_required_fields(self) -> None:
        cfg = IsolationConfig(backend_module="mod", backend_class="Cls")
        assert cfg.backend_module == "mod"
        assert cfg.backend_class == "Cls"

    def test_sensible_defaults(self) -> None:
        cfg = IsolationConfig(backend_module="mod", backend_class="Cls")
        assert cfg.backend_kwargs == {}
        assert cfg.pool_size == 2
        assert cfg.call_timeout == 30.0
        assert cfg.startup_timeout == 10.0
        assert cfg.force_process is False
        assert cfg.max_consecutive_failures == 5

    def test_custom_values(self) -> None:
        cfg = IsolationConfig(
            backend_module="my.module",
            backend_class="MyBackend",
            backend_kwargs={"key": "value"},
            pool_size=4,
            call_timeout=60.0,
            startup_timeout=20.0,
            force_process=True,
            max_consecutive_failures=10,
        )
        assert cfg.backend_module == "my.module"
        assert cfg.backend_class == "MyBackend"
        assert cfg.backend_kwargs == {"key": "value"}
        assert cfg.pool_size == 4
        assert cfg.call_timeout == 60.0
        assert cfg.startup_timeout == 20.0
        assert cfg.force_process is True
        assert cfg.max_consecutive_failures == 10


class TestIsolationConfigImmutability:
    def test_frozen(self) -> None:
        cfg = IsolationConfig(backend_module="m", backend_class="C")
        with pytest.raises(AttributeError):
            cfg.backend_module = "other"  # type: ignore[misc]

    def test_frozen_pool_size(self) -> None:
        cfg = IsolationConfig(backend_module="m", backend_class="C")
        with pytest.raises(AttributeError):
            cfg.pool_size = 99  # type: ignore[misc]


class TestIsolationConfigValidation:
    def test_empty_module(self) -> None:
        with pytest.raises(ValueError, match="backend_module must not be empty"):
            IsolationConfig(backend_module="", backend_class="C")

    def test_empty_class(self) -> None:
        with pytest.raises(ValueError, match="backend_class must not be empty"):
            IsolationConfig(backend_module="m", backend_class="")

    def test_pool_size_zero(self) -> None:
        with pytest.raises(ValueError, match="pool_size must be >= 1"):
            IsolationConfig(backend_module="m", backend_class="C", pool_size=0)

    def test_pool_size_negative(self) -> None:
        with pytest.raises(ValueError, match="pool_size must be >= 1"):
            IsolationConfig(backend_module="m", backend_class="C", pool_size=-1)

    def test_call_timeout_zero(self) -> None:
        with pytest.raises(ValueError, match="call_timeout must be > 0"):
            IsolationConfig(backend_module="m", backend_class="C", call_timeout=0)

    def test_call_timeout_negative(self) -> None:
        with pytest.raises(ValueError, match="call_timeout must be > 0"):
            IsolationConfig(backend_module="m", backend_class="C", call_timeout=-1.0)

    def test_startup_timeout_zero(self) -> None:
        with pytest.raises(ValueError, match="startup_timeout must be > 0"):
            IsolationConfig(backend_module="m", backend_class="C", startup_timeout=0)

    def test_max_consecutive_failures_zero(self) -> None:
        with pytest.raises(ValueError, match="max_consecutive_failures must be >= 1"):
            IsolationConfig(backend_module="m", backend_class="C", max_consecutive_failures=0)


class TestIsolationConfigKwargsImmutability:
    """Fix #7: backend_kwargs must not be mutatable after creation."""

    def test_kwargs_not_mutatable(self) -> None:
        cfg = IsolationConfig(
            backend_module="m",
            backend_class="C",
            backend_kwargs={"key": "value"},
        )
        with pytest.raises(TypeError):
            cfg.backend_kwargs["key"] = "mutated"  # type: ignore[index]

    def test_original_dict_mutation_does_not_affect_config(self) -> None:
        original = {"key": "value"}
        cfg = IsolationConfig(
            backend_module="m",
            backend_class="C",
            backend_kwargs=original,
        )
        original["key"] = "mutated"
        assert cfg.backend_kwargs["key"] == "value"


class TestIsolationConfigPickle:
    def test_roundtrip(self) -> None:
        cfg = IsolationConfig(
            backend_module="mod",
            backend_class="Cls",
            backend_kwargs={"host": "localhost", "port": 5432},
        )
        restored = pickle.loads(pickle.dumps(cfg))
        assert restored == cfg

    def test_equality(self) -> None:
        a = IsolationConfig(backend_module="m", backend_class="C", pool_size=4)
        b = IsolationConfig(backend_module="m", backend_class="C", pool_size=4)
        assert a == b

    def test_inequality(self) -> None:
        a = IsolationConfig(backend_module="m", backend_class="C", pool_size=2)
        b = IsolationConfig(backend_module="m", backend_class="C", pool_size=4)
        assert a != b
