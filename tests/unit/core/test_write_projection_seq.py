"""NexusFS.write / sys_write / write_batch return the projection sequence (#4738).

The write observer stashes ``projection_seq`` on the post-hook context; the
write paths must read it back and return it next to content_id / version.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nexus.core.nexus_fs_content import ContentMixin


class _Kernel:
    """Kernel stub whose post-hook dispatch behaves like the audit interceptor."""

    def __init__(self, *, seq: int | None = 11, post_hook_needed: bool = True) -> None:
        self.seq = seq
        self.post_hook_needed = post_hook_needed
        self.dispatched: list[str] = []

    def sys_write(self, path: str, _ctx: object, content: bytes, _offset: int = 0) -> Any:
        return SimpleNamespace(
            hit=True,
            content_id="cid",
            post_hook_needed=self.post_hook_needed,
            version=2,
            gen=2,
            size=len(content),
            is_new=False,
            old_content_id="old",
            old_size=1,
            old_version=1,
            old_modified_at_ms=None,
        )

    def write_batch(self, files: list[tuple[str, bytes]], _ctx: object) -> list[Any]:
        return [
            SimpleNamespace(hit=True, content_id=f"cid-{i}", version=1, gen=1, size=len(c))
            for i, (_p, c) in enumerate(files)
        ]

    def stat_batch(self, paths: list[str], zone_id: str = "root") -> list[Any]:
        return [None for _ in paths]

    def dispatch_pre_hooks(self, _name: str, _ctx: object) -> None:
        return None

    def hook_count(self, _name: str) -> int:
        return 1

    def dispatch_post_hooks(self, name: str, ctx: Any) -> None:
        self.dispatched.append(name)
        if self.seq is None:
            return
        if name == "write_batch":
            ctx.extra["projection_seqs"] = [self.seq + i for i, _ in enumerate(ctx.items)]
        else:
            ctx.extra["projection_seq"] = self.seq


class _Harness(ContentMixin):
    def __init__(self, kernel: _Kernel) -> None:
        self._kernel = kernel
        self._zone_id = "root"
        self.metadata = SimpleNamespace()
        self._driver_coordinator = SimpleNamespace()

    def _parse_context(self, context: object | None) -> object | None:
        return context

    def resolve_write(
        self, path: str, content: bytes, *, context: object | None = None
    ) -> tuple[bool, dict[str, object] | None]:
        return (False, None)

    def _build_rust_ctx(self, context: object | None, is_admin: bool) -> object:
        return object()

    def _get_context_identity(self, context: object | None) -> tuple[str, str | None, bool]:
        return ("root", None, False)

    def _prepare_rust_ctx(
        self, context: object | None = None
    ) -> tuple[str | None, str | None, bool, object]:
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        return zone_id, agent_id, is_admin, self._build_rust_ctx(context, is_admin)

    def _validate_path(self, path: str) -> str:
        return path

    def _batch_permission_check(self, paths: list[str], context: object | None) -> set[str]:
        return set(paths)

    def _dispatch_batch_post_hook(self, name: str, ctx: object) -> None:
        self._kernel.dispatch_post_hooks(name, ctx)


def test_write_returns_projection_seq_from_post_hook_context() -> None:
    kernel = _Kernel(seq=11)
    result = _Harness(kernel).write("/file.txt", b"abc")
    assert result["projection_seq"] == 11
    assert result["content_id"] == "cid" and result["version"] == 2 and result["gen"] == 2
    assert kernel.dispatched == ["write"]


def test_write_projection_seq_is_none_when_no_observer_confirms() -> None:
    result = _Harness(_Kernel(seq=None)).write("/file.txt", b"abc")
    assert "projection_seq" in result and result["projection_seq"] is None


def test_sys_write_returns_projection_seq_when_hooks_run() -> None:
    result = _Harness(_Kernel(seq=5)).sys_write("/file.txt", b"abc")
    assert result["projection_seq"] == 5 and result["bytes_written"] == 3 and "revision" in result


def test_sys_write_projection_seq_none_when_kernel_skips_post_hooks() -> None:
    kernel = _Kernel(seq=5, post_hook_needed=False)
    result = _Harness(kernel).sys_write("/file.txt", b"abc")
    assert result["projection_seq"] is None and kernel.dispatched == []


def test_write_batch_returns_per_item_projection_seq() -> None:
    results = _Harness(_Kernel(seq=20)).write_batch([("/a", b"1"), ("/b", b"22")])
    assert [r["projection_seq"] for r in results] == [20, 21]
    assert [r["content_id"] for r in results] == ["cid-0", "cid-1"]


def test_write_batch_projection_seq_none_without_observer() -> None:
    results = _Harness(_Kernel(seq=None)).write_batch([("/a", b"1")])
    assert results[0]["projection_seq"] is None
