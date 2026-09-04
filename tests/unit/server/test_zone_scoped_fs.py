"""ZoneScopedFS — the REST layer's per-request zone view (#4740).

Pure tests against a recording fake filesystem: every path-taking method the
REST routers and the batch executor call must scope its inputs into the
caller's ``/zone/<id>/`` namespace, refuse paths naming another zone with
403, and unscope paths in results.  Root-zone callers get the raw filesystem.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import HTTPException

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import OperationContext
from nexus.core.pagination import PaginatedResult
from nexus.server.api.v2._zone_scoped_fs import (
    ZoneScopedFS,
    scope_rest_path,
    zone_prefix_for,
    zone_scoped_fs,
)


class _FakeSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def list(self, path: str = "/", **kw: Any) -> list[str]:
        self.calls.append(("list", path))
        return [f"{path.rstrip('/')}/x.txt", "/root.txt"]

    def glob(self, pattern: str, path: str = "/", context: Any = None, files: Any = None) -> list:
        self.calls.append(("glob", (pattern, path, files)))
        return [f"{path.rstrip('/')}/{pattern}"]

    async def grep(self, pattern: str, path: str = "/", **kw: Any) -> list[dict[str, Any]]:
        self.calls.append(("grep", (pattern, path, kw.get("files"))))
        return [{"file": f"{path.rstrip('/')}/hit.txt", "line": 1, "content": pattern}]


@dataclass
class _FakeFS:
    """Records every call and echoes paths back the way NexusFS does."""

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]]
    search: _FakeSearch

    def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def sys_stat(self, path: str, **kw: Any) -> dict[str, Any] | None:
        self._rec("sys_stat", path, **kw)
        return {"path": path, "size": 1}

    def sys_read(self, path: str, **kw: Any) -> bytes:
        self._rec("sys_read", path, **kw)
        return b"data"

    def read(self, path: str, **kw: Any) -> Any:
        self._rec("read", path, **kw)
        if kw.get("return_metadata"):
            return {"content": b"data", "path": path}
        return b"data"

    def read_range(self, path: str, start: int, end: int, **kw: Any) -> bytes:
        self._rec("read_range", path, start, end, **kw)
        return b"da"

    def access(self, path: str, **kw: Any) -> bool:
        self._rec("access", path, **kw)
        return True

    def exists(self, path: str, **kw: Any) -> bool:
        self._rec("exists", path, **kw)
        return True

    def get_metadata(self, path: str, **kw: Any) -> FileMetadata:
        self._rec("get_metadata", path, **kw)
        return FileMetadata(path=path, size=3)

    def write(self, path: str, *args: Any, **kw: Any) -> dict[str, Any]:
        self._rec("write", path, *args, **kw)
        return {"path": path, "content_id": "c", "revision": f"{path}@1"}

    def sys_unlink(self, path: str, **kw: Any) -> dict[str, Any]:
        self._rec("sys_unlink", path, **kw)
        return {"deleted": True, "path": path}

    def delete(self, path: str, **kw: Any) -> bool:
        self._rec("delete", path, **kw)
        return True

    def sys_rename(self, old: str, new: str, **kw: Any) -> dict[str, Any]:
        self._rec("sys_rename", old, new, **kw)
        return {"old_path": old, "new_path": new, "revision": f"{new}@2"}

    def mkdir(self, path: str, **kw: Any) -> None:
        self._rec("mkdir", path, **kw)

    def sys_readdir(self, path: str = "/", **kw: Any) -> Any:
        self._rec("sys_readdir", path, **kw)
        base = path.rstrip("/")
        entries: list[Any] = [f"{base}/a.txt", {"path": f"{base}/b.txt", "size": 2}]
        if kw.get("limit") is not None:
            return PaginatedResult(items=entries, next_cursor=f"{base}/b.txt", has_more=True)
        return entries

    def list(self, path: str = "/", **kw: Any) -> list[str]:
        self._rec("list", path, **kw)
        return [f"{path.rstrip('/')}/a.txt"]

    def write_batch(self, files: list[Any], **kw: Any) -> list[dict[str, Any]]:
        self._rec("write_batch", files, **kw)
        return [{"path": p, "revision": f"{p}@1"} for p, _ in files]

    def read_batch(self, paths: list[str], **kw: Any) -> list[dict[str, Any]]:
        self._rec("read_batch", paths, **kw)
        return [{"path": p, "content": b"x"} for p in paths]

    def read_bulk(self, paths: list[str], **kw: Any) -> dict[str, Any]:
        self._rec("read_bulk", paths, **kw)
        return {p: {"content": b"x", "path": p} for p in paths}

    def rename_batch(self, renames: list[Any], **kw: Any) -> dict[str, Any]:
        self._rec("rename_batch", renames, **kw)
        return {src: {"success": True, "new_path": dst} for src, dst in renames}

    def service(self, name: str) -> Any:
        return self.search if name == "search" else object()

    @property
    def _kernel(self) -> str:
        return "kernel-handle"


@pytest.fixture
def raw() -> _FakeFS:
    return _FakeFS(calls=[], search=_FakeSearch())


@pytest.fixture
def view(raw: _FakeFS) -> ZoneScopedFS:
    return ZoneScopedFS(raw, "ta")


def _ctx(zone: str | None) -> OperationContext:
    return OperationContext(user_id="alice", groups=[], zone_id=zone)


class TestFactory:
    def test_root_and_zone_less_callers_get_raw_fs(self, raw: _FakeFS) -> None:
        assert zone_scoped_fs(raw, _ctx(ROOT_ZONE_ID)) is raw
        assert zone_scoped_fs(raw, _ctx(None)) is raw
        assert zone_scoped_fs(raw, None) is raw
        assert zone_prefix_for(_ctx(ROOT_ZONE_ID)) is None

    def test_zone_caller_gets_view_and_is_not_double_wrapped(self, raw: _FakeFS) -> None:
        v = zone_scoped_fs(raw, _ctx("ta"))
        assert isinstance(v, ZoneScopedFS)
        assert v.zone_id == "ta"
        assert zone_scoped_fs(v, _ctx("ta")) is v
        assert zone_prefix_for(_ctx("ta")) == "/zone/ta"

    def test_scope_rest_path_helper(self) -> None:
        assert scope_rest_path("/docs/a.txt", _ctx("ta")) == "/zone/ta/docs/a.txt"
        assert scope_rest_path("/docs/a.txt", _ctx(ROOT_ZONE_ID)) == "/docs/a.txt"
        with pytest.raises(HTTPException) as exc:
            scope_rest_path("/zone/tb/a.txt", _ctx("ta"))
        assert exc.value.status_code == 403


class TestScoping:
    def test_paths_are_prefixed_and_results_unscoped(
        self, view: ZoneScopedFS, raw: _FakeFS
    ) -> None:
        assert view.sys_stat("/docs/a.txt", context="c") == {"path": "/docs/a.txt", "size": 1}
        assert raw.calls[-1] == ("sys_stat", ("/zone/ta/docs/a.txt",), {"context": "c"})
        assert view.sys_read("/a.txt") == b"data"
        assert view.read("/a.txt", return_metadata=True) == {"content": b"data", "path": "/a.txt"}
        assert view.access("/a.txt") is True
        assert raw.calls[-1][1] == ("/zone/ta/a.txt",)

    def test_cross_zone_path_is_403_not_rerooted(self, view: ZoneScopedFS) -> None:
        with pytest.raises(HTTPException) as exc:
            view.sys_read("/zone/tb/secret.txt")
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException):
            view.write("/zone//tb/secret.txt", buf=b"x")

    def test_own_prefix_is_accepted_without_doubling(
        self, view: ZoneScopedFS, raw: _FakeFS
    ) -> None:
        view.sys_stat("/zone/ta/a.txt")
        assert raw.calls[-1][1] == ("/zone/ta/a.txt",)

    def test_write_by_keyword_and_positional(self, view: ZoneScopedFS, raw: _FakeFS) -> None:
        out = view.write(path="/a.txt", buf=b"x", context="c")
        # the fake binds the keyword ``path`` to its positional parameter
        assert raw.calls[-1] == ("write", ("/zone/ta/a.txt",), {"buf": b"x", "context": "c"})
        assert out["path"] == "/a.txt"
        assert out["revision"] == "/a.txt@1"
        out = view.write("/b.txt", buf=b"y")
        assert raw.calls[-1][1] == ("/zone/ta/b.txt",)
        assert out["revision"] == "/b.txt@1"

    def test_rename_delete_mkdir(self, view: ZoneScopedFS, raw: _FakeFS) -> None:
        out = view.sys_rename("/a.txt", "/b.txt", context="c")
        assert raw.calls[-1][1] == ("/zone/ta/a.txt", "/zone/ta/b.txt")
        assert out == {"old_path": "/a.txt", "new_path": "/b.txt", "revision": "/b.txt@2"}
        assert view.sys_unlink("/b.txt") == {"deleted": True, "path": "/b.txt"}
        assert view.delete("/b.txt") is True
        view.mkdir("/dir", parents=True)
        assert raw.calls[-1] == ("mkdir", ("/zone/ta/dir",), {"parents": True})

    def test_get_metadata_dataclass_is_unscoped(self, view: ZoneScopedFS) -> None:
        meta = view.get_metadata("/a.txt")
        assert isinstance(meta, FileMetadata)
        assert meta.path == "/a.txt"


class TestListing:
    def test_readdir_entries_and_dicts_are_unscoped(self, view: ZoneScopedFS, raw: _FakeFS) -> None:
        out = view.sys_readdir("/", recursive=False, details=True)
        # "/" scopes to the namespace root with a trailing slash, exactly as
        # scope_params_for_zone does for the RPC ``list`` path
        assert raw.calls[-1][1] == ("/zone/ta/",)
        assert out == ["/a.txt", {"path": "/b.txt", "size": 2}]

    def test_paginated_result_and_cursor(self, view: ZoneScopedFS, raw: _FakeFS) -> None:
        page = view.sys_readdir("/docs", limit=2, cursor="/docs/a.txt")
        assert raw.calls[-1][2]["cursor"] == "/zone/ta/docs/a.txt"
        assert isinstance(page, PaginatedResult)
        assert page.items == ["/docs/a.txt", {"path": "/docs/b.txt", "size": 2}]
        assert page.next_cursor == "/docs/b.txt"
        assert page.has_more is True

    def test_legacy_list(self, view: ZoneScopedFS) -> None:
        assert view.list("/docs", recursive=False) == ["/docs/a.txt"]

    def test_other_zones_paths_pass_through_unchanged(self, view: ZoneScopedFS) -> None:
        # only the caller's own prefix is stripped — a privileged cross-zone
        # result must stay zone-qualified
        assert view.unscope("/zone/tb/b.txt") == "/zone/tb/b.txt"
        assert view.unscope("/zone/ta") == "/"


class TestBatchOperations:
    def test_write_batch(self, view: ZoneScopedFS, raw: _FakeFS) -> None:
        out = view.write_batch([("/a.txt", b"1"), ("/b.txt", b"2")], context="c")
        assert raw.calls[-1][1] == ([("/zone/ta/a.txt", b"1"), ("/zone/ta/b.txt", b"2")],)
        assert out == [
            {"path": "/a.txt", "revision": "/a.txt@1"},
            {"path": "/b.txt", "revision": "/b.txt@1"},
        ]

    def test_read_batch_and_read_bulk(self, view: ZoneScopedFS, raw: _FakeFS) -> None:
        assert view.read_batch(["/a.txt"]) == [{"path": "/a.txt", "content": b"x"}]
        assert raw.calls[-1][1] == (["/zone/ta/a.txt"],)
        assert view.read_bulk(["/a.txt"]) == {"/a.txt": {"content": b"x", "path": "/a.txt"}}

    def test_rename_batch(self, view: ZoneScopedFS, raw: _FakeFS) -> None:
        out = view.rename_batch([("/a.txt", "/b.txt")])
        assert raw.calls[-1][1] == ([("/zone/ta/a.txt", "/zone/ta/b.txt")],)
        assert out == {"/a.txt": {"success": True, "new_path": "/b.txt"}}

    def test_cross_zone_in_batch_is_403(self, view: ZoneScopedFS) -> None:
        with pytest.raises(HTTPException):
            view.read_batch(["/a.txt", "/zone/tb/b.txt"])


class TestServicesAndPassthrough:
    def test_search_service_is_scoped(self, view: ZoneScopedFS, raw: _FakeFS) -> None:
        search = view.service("search")
        assert search.list("/") == ["/x.txt", "/root.txt"]
        assert raw.search.calls[-1] == ("list", "/zone/ta/")
        assert search.glob("*.txt", "/docs", files=["/docs/a.txt"]) == ["/docs/*.txt"]
        assert raw.search.calls[-1] == ("glob", ("*.txt", "/zone/ta/docs", ["/zone/ta/docs/a.txt"]))
        hits = asyncio.run(search.grep("needle", path="/", files=["/a.txt"]))
        assert hits == [{"file": "/hit.txt", "line": 1, "content": "needle"}]
        assert raw.search.calls[-1] == ("grep", ("needle", "/zone/ta/", ["/zone/ta/a.txt"]))

    def test_other_services_and_attributes_pass_through(
        self, view: ZoneScopedFS, raw: _FakeFS
    ) -> None:
        assert view.service("mount") is not raw.search
        assert view._kernel == "kernel-handle"
        assert view.wrapped_fs is raw
        assert hasattr(view, "service")
