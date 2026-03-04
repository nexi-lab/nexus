"""BackendFactory._accepted_params and kwarg filtering tests (Issue #1601).

Tests for the introspection helper and the extra-kwargs filtering logic
that ensures only constructor-compatible kwargs are forwarded.
"""

from typing import Any

from nexus.backends.base.factory import BackendFactory

# ---------------------------------------------------------------------------
# Helper classes with known signatures
# ---------------------------------------------------------------------------


class _SimpleInit:
    def __init__(self, x: int, y: str) -> None: ...


class _VarKwInit:
    def __init__(self, x: int, **kwargs: Any) -> None: ...


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAcceptedParams:
    """Tests for BackendFactory._accepted_params()."""

    def setup_method(self) -> None:
        BackendFactory._accepted_params.cache_clear()

    def test_accepted_params_simple_class(self) -> None:
        """Returns frozenset of param names, excluding 'self'."""
        params, accepts_var_kw = BackendFactory._accepted_params(_SimpleInit)

        assert params == frozenset({"x", "y"})
        assert accepts_var_kw is False

    def test_accepted_params_var_keyword(self) -> None:
        """Detects **kwargs and returns accepts_var_kw=True."""
        params, accepts_var_kw = BackendFactory._accepted_params(_VarKwInit)

        assert "x" in params
        assert "kwargs" in params
        assert accepts_var_kw is True

    def test_accepted_params_cached(self) -> None:
        """lru_cache returns the same object on second call."""
        BackendFactory._accepted_params.cache_clear()

        r1 = BackendFactory._accepted_params(_SimpleInit)
        r2 = BackendFactory._accepted_params(_SimpleInit)

        assert r1 is r2
        assert BackendFactory._accepted_params.cache_info().hits == 1


class TestExtraKwargsFiltering:
    """Tests for extra-kwargs filtering in BackendFactory.create()."""

    def test_extra_kwargs_filtered_by_signature(self, tmp_path: Any) -> None:
        """Extra kwargs not in constructor signature are silently dropped."""
        # LocalBackend.__init__ does NOT accept 'session_factory'
        backend = BackendFactory.create(
            "local",
            {"data_dir": str(tmp_path / "data")},
            session_factory="should_be_dropped",
        )
        # Backend created successfully -- unknown kwarg did not cause TypeError
        assert backend.name == "local"
        assert not hasattr(backend, "session_factory")

    def test_accepted_extra_kwargs_are_forwarded(self, tmp_path: Any) -> None:
        """Extra kwargs that match constructor params are forwarded."""
        # 'read_only' is accepted by LocalBackend's constructor
        backend = BackendFactory.create(
            "local",
            {"data_dir": str(tmp_path / "data")},
            read_only=True,
        )
        assert backend.name == "local"
