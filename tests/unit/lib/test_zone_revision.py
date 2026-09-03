"""Unit tests for the revision token (Issue #4737)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus.contracts.exceptions import NexusError
from nexus.lib.zone_revision import (
    CLUSTER_INFO_METHOD,
    RevisionToken,
    ZoneRevisionUnavailable,
    await_path_gen,
    await_zone_revision,
    read_path_gen,
    read_zone_revision,
    reset_zone_revision_cache,
    revision_fields,
    revision_from_result,
    revision_token,
    wait_for_zone_revision,
)


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    reset_zone_revision_cache()


# ---------------------------------------------------------------------------
# Token parsing / formatting
# ---------------------------------------------------------------------------


class TestRevisionTokenParse:
    def test_path_token_round_trips(self) -> None:
        tok = RevisionToken.parse("/ws/a.txt@7")
        assert tok == RevisionToken(anchor="/ws/a.txt", index=7)
        assert tok.is_path
        assert str(tok) == "/ws/a.txt@7"

    def test_path_anchor_may_contain_at_and_spaces(self) -> None:
        tok = RevisionToken.parse("/inbox/me@example.com/my file.txt@3")
        assert tok.anchor == "/inbox/me@example.com/my file.txt"
        assert tok.index == 3

    def test_zone_token_round_trips(self) -> None:
        tok = RevisionToken.parse("corp-eng@1234")
        assert tok == RevisionToken(anchor="corp-eng", index=1234)
        assert not tok.is_path

    def test_bare_index_is_a_root_zone_token(self) -> None:
        assert RevisionToken.parse("42") == RevisionToken(anchor="root", index=42)
        assert RevisionToken.parse(" 7 ", default_zone="eng") == RevisionToken("eng", 7)

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "root@", "@5", "/ws/a.txt@", "root@x", "root@-1", "ro ot@5", "root@1.5"],
    )
    def test_malformed_tokens_raise(self, raw: str) -> None:
        with pytest.raises(ValueError):
            RevisionToken.parse(raw)


class TestRevisionFromResult:
    def test_path_anchored_from_gen(self) -> None:
        result = SimpleNamespace(content_id="abc", gen=7)
        assert revision_token(result, path="/ws/a.txt") == "/ws/a.txt@7"

    def test_kernel_stamped_zone_revision_wins_over_path(self) -> None:
        result = SimpleNamespace(gen=7, zone_id="root", applied_index=1234)
        assert revision_token(result, path="/ws/a.txt") == "root@1234"

    def test_dict_results(self) -> None:
        assert revision_token({"gen": "9"}, path="/a") == "/a@9"
        assert revision_token({"zone_id": "eng", "applied_index": "5"}) == "eng@5"

    @pytest.mark.parametrize(
        ("result", "path"),
        [
            (None, "/a"),
            ({}, "/a"),
            ({"gen": 0}, "/a"),
            ({"gen": None}, "/a"),
            ({"gen": "nope"}, "/a"),
            ({"gen": 3}, None),
            ({"gen": 3}, "relative/path"),
            ({"zone_id": "root", "applied_index": 0}, None),
            ({"zone_id": "", "applied_index": 5}, None),
            (SimpleNamespace(content_id="abc"), "/a"),
        ],
    )
    def test_no_token_when_nothing_to_anchor_on(self, result: Any, path: str | None) -> None:
        assert revision_from_result(result, path=path) is None
        assert revision_token(result, path=path) is None


class TestRevisionFields:
    def test_stamped_response(self) -> None:
        resp = SimpleNamespace(zone_id="root", applied_index=31)
        assert revision_fields(resp) == {"zone_id": "root", "applied_index": 31}

    def test_pinned_kernel_response_without_fields(self) -> None:
        resp = SimpleNamespace(content_id="x", gen=3)
        assert revision_fields(resp) == {"zone_id": None, "applied_index": None}

    def test_zero_values_normalise_to_none(self) -> None:
        resp = SimpleNamespace(zone_id="", applied_index=0)
        assert revision_fields(resp) == {"zone_id": None, "applied_index": None}


# ---------------------------------------------------------------------------
# Path-anchored fence (sys_stat gen)
# ---------------------------------------------------------------------------


class FakeFS:
    """Duck-typed NexusFS: sys_stat answers from a script of gens (None = absent)."""

    def __init__(self, gens: list[int | None]) -> None:
        self._gens = list(gens)
        self.calls: list[tuple[str, Any]] = []

    def sys_stat(self, path: str, context: Any = None) -> dict[str, Any] | None:
        self.calls.append((path, context))
        gen = self._gens[0] if len(self._gens) == 1 else self._gens.pop(0)
        if gen is None:
            return None
        return {"path": path, "gen": gen, "content_id": "abc"}


class TestPathGen:
    def test_read_path_gen_goes_through_permission_checked_stat(self) -> None:
        fs = FakeFS([4])
        ctx = object()
        assert read_path_gen(fs, "/a", ctx) == 4
        assert fs.calls == [("/a", ctx)]

    def test_absent_path_is_gen_zero(self) -> None:
        assert read_path_gen(FakeFS([None]), "/a") == 0

    def test_falls_back_to_version_when_gen_missing(self) -> None:
        class VersionOnlyFS:
            def sys_stat(self, path: str, context: Any = None) -> dict[str, Any]:
                return {"path": path, "version": 3}

        assert read_path_gen(VersionOnlyFS(), "/a") == 3

    async def test_await_satisfied_after_replication(self) -> None:
        fs = FakeFS([None, 2, 7])
        ok, current = await await_path_gen(fs, "/a", 7, timeout_s=2.0)
        assert (ok, current) == (True, 7)
        assert len(fs.calls) == 3

    async def test_await_times_out_with_current_gen(self) -> None:
        ok, current = await await_path_gen(FakeFS([3]), "/a", 9, timeout_s=0.05)
        assert (ok, current) == (False, 3)

    async def test_zero_timeout_probes_once(self) -> None:
        fs = FakeFS([None])
        ok, current = await await_path_gen(fs, "/a", 1, timeout_s=0)
        assert (ok, current) == (False, 0)
        assert len(fs.calls) == 1

    async def test_async_sys_stat_is_awaited(self) -> None:
        class AsyncFS:
            async def sys_stat(self, path: str, context: Any = None) -> dict[str, Any]:
                return {"gen": 5}

        assert await await_path_gen(AsyncFS(), "/a", 5, timeout_s=0) == (True, 5)


# ---------------------------------------------------------------------------
# Zone-anchored fence (kernel applied_index) — optional, kernel-stamped
# ---------------------------------------------------------------------------


class FakeKernel:
    def __init__(self, applied: list[int] | int) -> None:
        self._applied = list(applied) if isinstance(applied, list) else [applied]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        assert method == CLUSTER_INFO_METHOD
        current = self._applied[0] if len(self._applied) == 1 else self._applied.pop(0)
        return {"zone_id": params["zone_id"], "has_store": True, "applied_index": current}


class FailingKernel:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls += 1
        raise self.exc


class TestReadZoneRevision:
    def test_returns_applied_index(self) -> None:
        kernel = FakeKernel(42)
        assert read_zone_revision(kernel, "root") == 42
        assert kernel.calls == [(CLUSTER_INFO_METHOD, {"zone_id": "root"})]

    def test_none_kernel_is_unavailable(self) -> None:
        with pytest.raises(ZoneRevisionUnavailable):
            read_zone_revision(None, "root")

    def test_unknown_call_method_is_unavailable_and_cached(self) -> None:
        kernel = FailingKernel(
            NexusError("RPC error [-32603]: unknown Call method: federation_cluster_info")
        )
        for _ in range(2):
            with pytest.raises(ZoneRevisionUnavailable):
                read_zone_revision(kernel, "root")
        assert kernel.calls == 1  # second probe answered from the cache

    def test_transport_errors_propagate_and_are_not_cached(self) -> None:
        kernel = FailingKernel(RuntimeError("connection refused"))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                read_zone_revision(kernel, "root")
        assert kernel.calls == 2

    def test_python_standalone_stub_is_unavailable_but_not_cached(self) -> None:
        class StubKernel:
            calls = 0

            def _call(self, method: str, params: dict[str, Any]) -> Any:
                self.calls += 1
                return {"zone_id": "root", "applied_index": 0, "available": False}

        kernel = StubKernel()
        for _ in range(2):
            with pytest.raises(ZoneRevisionUnavailable):
                read_zone_revision(kernel, "root")
        assert kernel.calls == 2

    def test_zone_not_hosted_is_unavailable(self) -> None:
        class NoStoreKernel:
            def _call(self, method: str, params: dict[str, Any]) -> Any:
                return {"zone_id": params["zone_id"], "has_store": False, "applied_index": 0}

        with pytest.raises(ZoneRevisionUnavailable, match="not hosted"):
            read_zone_revision(NoStoreKernel(), "ghost")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestWaitForZoneRevision:
    def test_satisfied_after_polls_with_backoff(self) -> None:
        clock = _Clock()
        kernel = FakeKernel([1, 3, 7])
        ok, current = wait_for_zone_revision(
            kernel, "root", 7, timeout_s=5.0, sleep=clock.sleep, clock=clock
        )
        assert (ok, current) == (True, 7)
        assert clock.sleeps == [0.02, 0.04]

    def test_timeout_returns_current_index(self) -> None:
        clock = _Clock()
        ok, current = wait_for_zone_revision(
            FakeKernel(3), "root", 99, timeout_s=0.5, sleep=clock.sleep, clock=clock
        )
        assert (ok, current) == (False, 3)
        assert all(s <= 0.25 for s in clock.sleeps)

    async def test_async_twin(self) -> None:
        ok, current = await await_zone_revision(FakeKernel([0, 5]), "root", 5, timeout_s=2.0)
        assert (ok, current) == (True, 5)
