from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from prometheus_client import REGISTRY

from nexus.core.nexus_fs_content import ContentMixin

_OVERSIZED_BATCH_LENGTH = 100 * 1024 * 1024 + 1


def _sample(name: str, **labels: str) -> float:
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


class _Kernel:
    def __init__(self) -> None:
        self.sys_read_calls = 0
        self.sys_write_calls = 0

    def sys_read(self, path: str, _ctx: object, _timeout_ms: int, _offset: int = 0) -> Any:
        self.sys_read_calls += 1
        return SimpleNamespace(
            data=b"abc",
            post_hook_needed=False,
            content_id="cid",
            gen=1,
            entry_type=1,
            stream_next_offset=None,
        )

    def sys_write(self, path: str, _ctx: object, content: bytes, _offset: int = 0) -> Any:
        self.sys_write_calls += 1
        return SimpleNamespace(
            hit=True,
            content_id="cid",
            post_hook_needed=False,
            version=1,
            gen=1,
            size=len(content),
            is_new=True,
            old_content_id=None,
            old_size=None,
            old_version=None,
            old_modified_at_ms=None,
        )

    def stat_batch(self, paths: list[str], zone_id: str = "root") -> list[Any]:
        return [
            {
                "size": 3,
                "content_id": f"cid-{index}",
                "version": 1,
                "gen": 1,
                "modified_at": None,
            }
            for index, _path in enumerate(paths)
        ]

    def sys_read_batch(self, paths: list[str], _ctx: object) -> list[Any]:
        return [
            SimpleNamespace(
                data=f"b{index}".encode(),
                content_id=f"cid-{index}",
                gen=index + 1,
            )
            for index, _path in enumerate(paths)
        ]

    def sys_write_batch(self, files: list[tuple[str, bytes]], _ctx: object) -> list[Any]:
        return [
            SimpleNamespace(
                hit=True,
                content_id=f"cid-{index}",
                version=1,
                gen=1,
                size=len(content),
            )
            for index, (_path, content) in enumerate(files)
        ]

    def dispatch_pre_hooks(self, _name: str, _ctx: object) -> None:
        return None

    def hook_count(self, _name: str) -> int:
        return 0

    def dispatch_post_hooks(self, _name: str, _ctx: object) -> None:
        return None


class _ErrorKernel(_Kernel):
    def sys_read(self, path: str, _ctx: object, _timeout_ms: int, _offset: int = 0) -> Any:
        self.sys_read_calls += 1
        raise RuntimeError("boom")


class _FallbackKernel(_Kernel):
    def sys_read_batch(self, paths: list[str], _ctx: object) -> list[Any]:
        return [
            SimpleNamespace(
                data=None,
                content_id=f"cid-{index}",
                gen=index + 1,
            )
            for index, _path in enumerate(paths)
        ]


class _MutatingHookKernel(_Kernel):
    def hook_count(self, name: str) -> int:
        return 1 if name == "read" else 0

    def dispatch_post_hooks(self, name: str, ctx: object) -> None:
        if name == "read":
            ctx.content = b"expanded"


class _OversizedBytes(bytes):
    def __new__(cls) -> _OversizedBytes:
        return super().__new__(cls, b"x")

    def __len__(self) -> int:
        return _OVERSIZED_BATCH_LENGTH


class _Harness(ContentMixin):
    def __init__(self) -> None:
        self._kernel = _Kernel()
        self._zone_id = "root"
        self.metadata = SimpleNamespace()
        self._driver_coordinator = SimpleNamespace()

    def _parse_context(self, context: object | None) -> object | None:
        return context

    def resolve_read(
        self, path: str, *, context: object | None = None
    ) -> tuple[bool, bytes | None]:
        return (False, None)

    def resolve_write(
        self, path: str, content: bytes, *, context: object | None = None
    ) -> tuple[bool, dict[str, object] | None]:
        return (False, None)

    def _build_rust_ctx(self, context: object | None, is_admin: bool) -> object:
        return object()

    def _get_context_identity(self, context: object | None) -> tuple[str, str | None, bool]:
        return ("root", None, False)

    def _validate_path(self, path: str) -> str:
        return path

    def _batch_permission_check(self, paths: list[str], context: object | None) -> set[str]:
        return set(paths)

    def _dispatch_batch_post_hook(self, _name: str, _ctx: object) -> None:
        return None


class _VirtualHarness(_Harness):
    def resolve_read(
        self, path: str, *, context: object | None = None
    ) -> tuple[bool, bytes | None]:
        return (True, b"virtual")


class _ErrorHarness(_Harness):
    def __init__(self) -> None:
        super().__init__()
        self._kernel = _ErrorKernel()


class _FallbackHarness(_Harness):
    def __init__(self) -> None:
        super().__init__()
        self._kernel = _FallbackKernel()

    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: object | None = None,
    ) -> bytes | dict[str, object]:
        return b"fallback"


class _InstrumentedFallbackHarness(_Harness):
    def __init__(self) -> None:
        super().__init__()
        self._kernel = _FallbackKernel()


class _OversizedFallbackHarness(_FallbackHarness):
    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: object | None = None,
    ) -> bytes | dict[str, object]:
        return _OversizedBytes()


class _StreamFallbackHarness(_FallbackHarness):
    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: object | None = None,
    ) -> bytes | dict[str, object]:
        return {"data": b"streamed", "next_offset": 8}


class _MutatingHookHarness(_Harness):
    def __init__(self) -> None:
        super().__init__()
        self._kernel = _MutatingHookKernel()


def test_sys_read_records_backend_latency_and_bytes() -> None:
    harness = _Harness()
    before_bytes = _sample("nexus_read_bytes_total", tier="backend")
    before_count = _sample("nexus_read_latency_seconds_count", tier="backend")

    assert harness.sys_read("/file.txt") == b"abc"

    assert _sample("nexus_read_bytes_total", tier="backend") == before_bytes + 3
    assert _sample("nexus_read_latency_seconds_count", tier="backend") == before_count + 1


def test_sys_read_records_virtual_resolver_reads() -> None:
    harness = _VirtualHarness()
    before_bytes = _sample("nexus_read_bytes_total", tier="virtual")

    assert harness.sys_read("/__sys__/virtual") == b"virtual"

    assert _sample("nexus_read_bytes_total", tier="virtual") == before_bytes + 7


def test_sys_read_records_error_tier_metrics() -> None:
    harness = _ErrorHarness()
    before_bytes = _sample("nexus_read_bytes_total", tier="error")
    before_count = _sample("nexus_read_latency_seconds_count", tier="error")

    try:
        harness.sys_read("/file.txt")
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("sys_read should have raised")

    assert _sample("nexus_read_bytes_total", tier="error") == before_bytes
    assert _sample("nexus_read_latency_seconds_count", tier="error") == before_count + 1


def test_sys_write_records_backend_rpc_when_kernel_hit() -> None:
    harness = _Harness()
    before = _sample("nexus_write_backend_rpc_total")

    harness.sys_write("/file.txt", b"abc")

    assert _sample("nexus_write_backend_rpc_total") == before + 1


def test_write_locked_records_backend_rpc_when_kernel_hit() -> None:
    harness = _Harness()
    before = _sample("nexus_write_backend_rpc_total")

    harness._write_locked("/file.txt", b"abc")

    assert _sample("nexus_write_backend_rpc_total") == before + 1


def test_write_batch_records_backend_rpc_for_rust_hits() -> None:
    harness = _Harness()
    before = _sample("nexus_write_backend_rpc_total")

    results = harness.write_batch([("/a.txt", b"abc"), ("/b.txt", b"defg")])

    assert [item["size"] for item in results] == [3, 4]
    assert _sample("nexus_write_backend_rpc_total") == before + 2


def test_read_batch_records_batch_size_and_batch_bytes() -> None:
    harness = _Harness()
    before_size = _sample("nexus_read_batch_size_count")
    before_bytes = _sample("nexus_read_bytes_total", tier="batch")
    before_latency_count = _sample("nexus_read_latency_seconds_count", tier="batch")

    results = harness.read_batch(["/a.txt", "/b.txt"])

    assert [item["content"] for item in results] == [b"b0", b"b1"]
    assert _sample("nexus_read_batch_size_count") == before_size + 1
    assert _sample("nexus_read_bytes_total", tier="batch") == before_bytes + 4
    assert _sample("nexus_read_latency_seconds_count", tier="batch") == before_latency_count


def test_read_batch_empty_records_batch_size() -> None:
    harness = _Harness()
    before_size = _sample("nexus_read_batch_size_count")

    assert harness.read_batch([]) == []

    assert _sample("nexus_read_batch_size_count") == before_size + 1


def test_read_batch_fallback_does_not_record_synthetic_batch_metrics() -> None:
    harness = _FallbackHarness()
    before_bytes = _sample("nexus_read_bytes_total", tier="batch")
    before_latency_count = _sample("nexus_read_latency_seconds_count", tier="batch")

    results = harness.read_batch(["/fallback.txt"])

    assert results[0]["content"] == b"fallback"
    assert _sample("nexus_read_bytes_total", tier="batch") == before_bytes
    assert _sample("nexus_read_latency_seconds_count", tier="batch") == before_latency_count


def test_read_batch_fallback_does_not_double_count_single_read_metrics() -> None:
    harness = _InstrumentedFallbackHarness()
    before_backend_bytes = _sample("nexus_read_bytes_total", tier="backend")
    before_batch_bytes = _sample("nexus_read_bytes_total", tier="batch")

    results = harness.read_batch(["/fallback.txt"])

    assert results[0]["content"] == b"abc"
    assert _sample("nexus_read_bytes_total", tier="backend") == before_backend_bytes + 3
    assert _sample("nexus_read_bytes_total", tier="batch") == before_batch_bytes


def test_read_batch_fallback_does_not_record_bytes_when_size_guard_rejects() -> None:
    harness = _OversizedFallbackHarness()
    before_bytes = _sample("nexus_read_bytes_total", tier="batch")

    try:
        harness.read_batch(["/too-large.txt"])
    except ValueError as exc:
        assert "Batch read aggregate size exceeded" in str(exc)
    else:
        raise AssertionError("read_batch should have raised")

    assert _sample("nexus_read_bytes_total", tier="batch") == before_bytes


def test_read_batch_fallback_stream_dict_records_payload_bytes() -> None:
    harness = _StreamFallbackHarness()
    before_bytes = _sample("nexus_read_bytes_total", tier="batch")

    results = harness.read_batch(["/stream"])

    assert results[0]["content"] == {"data": b"streamed", "next_offset": 8}
    assert results[0]["size"] == 8
    assert _sample("nexus_read_bytes_total", tier="batch") == before_bytes


def test_read_batch_records_bytes_after_read_hook_mutates_content() -> None:
    harness = _MutatingHookHarness()
    before_bytes = _sample("nexus_read_bytes_total", tier="batch")

    results = harness.read_batch(["/hooked.txt"])

    assert results[0]["content"] == b"expanded"
    assert _sample("nexus_read_bytes_total", tier="batch") == before_bytes + 8
