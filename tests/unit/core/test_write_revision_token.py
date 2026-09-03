"""NexusFS write paths return the path-anchored revision token (Issue #4737)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nexus.core.nexus_fs_content import ContentMixin


class _Kernel:
    """Minimal kernel stand-in: every write lands at gen 3, batch items at gen i+1."""

    def sys_write(self, path: str, _ctx: object, content: bytes, _offset: int = 0) -> Any:
        return SimpleNamespace(
            hit=True,
            content_id="cid",
            post_hook_needed=False,
            version=1,
            gen=3,
            size=len(content),
            is_new=True,
            old_content_id=None,
            old_size=None,
            old_version=None,
            old_modified_at_ms=None,
        )

    def write_batch(self, files: list[tuple[str, bytes]], _ctx: object) -> list[Any]:
        return [
            SimpleNamespace(hit=True, content_id=f"cid-{i}", version=1, gen=i + 1, size=len(c))
            for i, (_p, c) in enumerate(files)
        ]

    def stat_batch(self, paths: list[str], zone_id: str = "root") -> list[Any]:
        return [None for _ in paths]

    def dispatch_pre_hooks(self, _name: str, _ctx: object) -> None:
        return None

    def dispatch_post_hooks(self, _name: str, _ctx: object) -> None:
        return None

    def hook_count(self, _name: str) -> int:
        return 0


class _StampingKernel(_Kernel):
    """A future kernel that stamps zone revisions on the response."""

    def sys_write(self, path: str, _ctx: object, content: bytes, _offset: int = 0) -> Any:
        result = super().sys_write(path, _ctx, content, _offset)
        result.zone_id = "root"
        result.applied_index = 1234
        return result


class _Harness(ContentMixin):
    def __init__(self, kernel: _Kernel | None = None) -> None:
        self._kernel = kernel or _Kernel()
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

    def _dispatch_batch_post_hook(self, _name: str, _ctx: object) -> None:
        return None


def test_write_returns_path_anchored_revision() -> None:
    result = _Harness().write("/ws/a.txt", b"abc")

    assert result["gen"] == 3
    assert result["revision"] == "/ws/a.txt@3"


def test_sys_write_returns_path_anchored_revision() -> None:
    result = _Harness().sys_write("/ws/a.txt", b"abc")

    assert result["bytes_written"] == 3
    assert result["revision"] == "/ws/a.txt@3"


def test_write_batch_returns_per_item_revision() -> None:
    results = _Harness().write_batch([("/ws/a.txt", b"abc"), ("/ws/b.txt", b"defg")])

    assert [r["revision"] for r in results] == ["/ws/a.txt@1", "/ws/b.txt@2"]


def test_kernel_stamped_zone_revision_is_preferred() -> None:
    result = _Harness(_StampingKernel()).write("/ws/a.txt", b"abc")

    assert result["revision"] == "root@1234"
