"""Isolation configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class IsolationConfig:
    """Immutable configuration for IsolatedBackend.

    Attributes:
        backend_module: Dotted import path (e.g. ``"nexus.backends.gdrive_connector"``).
        backend_class: Class name inside *backend_module*.
        backend_kwargs: Keyword arguments forwarded to the backend constructor.
            All values must be picklable.  Stored internally as a read-only
            ``MappingProxyType`` to enforce true immutability.
        pool_size: Number of workers in the executor pool.
        call_timeout: Per-call timeout in seconds.
        startup_timeout: Backend initialisation timeout in seconds.
        force_process: Force ProcessPoolExecutor even on Python 3.14+.
        max_consecutive_failures: Failures before the pool is automatically restarted.
    """

    backend_module: str
    backend_class: str
    backend_kwargs: dict[str, Any] | MappingProxyType[str, Any] = field(default_factory=dict)
    pool_size: int = 2
    call_timeout: float = 30.0
    startup_timeout: float = 10.0
    force_process: bool = False
    max_consecutive_failures: int = 5

    def __post_init__(self) -> None:
        # Defensive copy → read-only view to enforce true immutability
        object.__setattr__(self, "backend_kwargs", MappingProxyType(dict(self.backend_kwargs)))
        if not self.backend_module:
            raise ValueError("backend_module must not be empty")
        if not self.backend_class:
            raise ValueError("backend_class must not be empty")
        if self.pool_size < 1:
            raise ValueError(f"pool_size must be >= 1, got {self.pool_size}")
        if self.call_timeout <= 0:
            raise ValueError(f"call_timeout must be > 0, got {self.call_timeout}")
        if self.startup_timeout <= 0:
            raise ValueError(f"startup_timeout must be > 0, got {self.startup_timeout}")
        if self.max_consecutive_failures < 1:
            raise ValueError(
                f"max_consecutive_failures must be >= 1, got {self.max_consecutive_failures}"
            )

    def __getstate__(self) -> dict[str, Any]:
        """Support pickle by converting MappingProxyType back to dict."""
        state = {f.name: getattr(self, f.name) for f in self.__dataclass_fields__.values()}
        state["backend_kwargs"] = dict(self.backend_kwargs)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore from pickle, re-wrapping kwargs as MappingProxyType."""
        state["backend_kwargs"] = MappingProxyType(dict(state["backend_kwargs"]))
        for key, value in state.items():
            object.__setattr__(self, key, value)
