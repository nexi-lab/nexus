"""Unit tests for OperationResult and OperationWarning."""

from nexus.contracts.operation_result import OperationResult, OperationWarning


class TestOperationWarning:
    def test_frozen(self):
        w = OperationWarning("degraded", "cache", "miss")
        assert w.severity == "degraded"
        assert w.component == "cache"
        assert w.message == "miss"

    def test_cosmetic_severity(self):
        w = OperationWarning("cosmetic", "logging", "log write failed")
        assert w.severity == "cosmetic"


class TestOperationResult:
    def test_ok_result(self):
        r: OperationResult[int] = OperationResult(value=42)
        assert r.value == 42
        assert r.ok
        assert not r.degraded
        assert r.warnings == ()

    def test_degraded_result(self):
        r = OperationResult(
            value={"content_id": "abc"},
            warnings=(OperationWarning("degraded", "tiger_cache", "update failed"),),
        )
        assert r.degraded
        assert not r.ok
        assert len(r.warnings) == 1

    def test_cosmetic_only_not_degraded(self):
        r = OperationResult(
            value="ok",
            warnings=(OperationWarning("cosmetic", "log", "skip"),),
        )
        assert not r.degraded
        assert not r.ok  # has warnings, so not ok

    def test_with_warning_returns_new_result(self):
        r1 = OperationResult(value=1)
        r2 = r1.with_warning("degraded", "rebac", "tuple create failed")
        # Original unchanged (immutable)
        assert r1.ok
        assert r2.degraded
        assert r2.value == 1
        assert len(r2.warnings) == 1

    def test_with_warning_accumulates(self):
        r = OperationResult(value="x")
        r = r.with_warning("cosmetic", "a", "msg1")
        r = r.with_warning("degraded", "b", "msg2")
        assert len(r.warnings) == 2
        assert r.degraded

    def test_merge_warnings(self):
        r1 = OperationResult(value=1).with_warning("cosmetic", "a", "w1")
        r2 = OperationResult(value=2).with_warning("degraded", "b", "w2")
        merged = r1.merge_warnings(r2)
        assert merged.value == 1  # keeps r1's value
        assert len(merged.warnings) == 2
        assert merged.degraded

    def test_frozen_immutability(self):
        r = OperationResult(value=42)
        import pytest

        with pytest.raises(AttributeError):
            r.value = 99  # type: ignore[misc]
