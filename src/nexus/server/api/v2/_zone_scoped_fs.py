"""Per-request zone view of NexusFS for the REST v2 routes (#4740).

The RPC (``/api/nfs/*``) and gRPC paths prefix every caller path with the
caller's ``/zone/<id>/`` namespace (``nexus.lib.zone_scoping``) and strip it
again on the way out (``_kernel_syscall_dispatch._apply_result_adapter``).
The REST routes never did.  A zone-scoped REST caller therefore read and
wrote the ROOT namespace: two tenants shared ``/x.txt`` (ReBAC permitting),
and under the #4740 visibility predicate a tenant's own REST writes were
root-attributed and invisible to its own listing.

:class:`ZoneScopedFS` gives the REST layer the RPC contract:

* every path in a request is scoped with ``scope_single_path`` — a path that
  names another zone is refused with HTTP 403, never silently re-rooted;
* every path in a result (list entries, stat dicts, batch results, revision
  tokens) is unscoped back to the caller's view;
* root-zone callers and multi-zone tokens (``zone_id == root`` placeholder)
  get the underlying filesystem unchanged, exactly as the RPC layer does.

Unknown attributes are forwarded to the wrapped filesystem unchanged; every
path-taking method the REST routers and the batch executor call is proxied
explicitly below.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.zone_scoping import ZoneScopingError, scope_single_path

#: Dict keys whose string values are VFS paths in NexusFS results.
_PATH_KEYS = (
    "path",
    "virtual_path",
    "file",
    "file_path",
    "old_path",
    "new_path",
    "source",
    "destination",
)


def zone_prefix_for(context: Any) -> str | None:
    """``/zone/<id>`` for a context scoped to a concrete non-root zone, else None."""
    zone = getattr(context, "zone_id", None)
    if not isinstance(zone, str) or not zone or zone == ROOT_ZONE_ID:
        return None
    return f"/zone/{zone}"


def scope_rest_path(path: Any, context: Any) -> Any:
    """Scope one caller-supplied path for *context*; 403 when it names another zone."""
    prefix = zone_prefix_for(context)
    if prefix is None or not isinstance(path, str):
        return path
    try:
        return scope_single_path(path, prefix, context.zone_id)
    except ZoneScopingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def zone_scoped_fs(fs: Any, context: Any) -> Any:
    """Return *fs* wrapped in a :class:`ZoneScopedFS` when *context* has a zone."""
    if fs is None or zone_prefix_for(context) is None:
        return fs
    if isinstance(fs, ZoneScopedFS):
        return fs
    return ZoneScopedFS(fs, context.zone_id)


class ZoneScopedFS:
    """Zone-namespaced view over a NexusFS instance (see module docstring)."""

    __slots__ = ("_fs", "_prefix", "_zone")

    def __init__(self, fs: Any, zone_id: str) -> None:
        self._fs = fs
        self._zone = zone_id
        self._prefix = f"/zone/{zone_id}"

    # ── identity ───────────────────────────────────────────────────────

    @property
    def wrapped_fs(self) -> Any:
        return self._fs

    @property
    def zone_id(self) -> str:
        return self._zone

    # ── path translation ────────────────────────────────────────────────

    def scope(self, path: Any) -> Any:
        """Caller path → internal path; 403 when the path names another zone."""
        if not isinstance(path, str):
            return path
        try:
            return scope_single_path(path, self._prefix, self._zone)
        except ZoneScopingError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def unscope(self, path: Any) -> Any:
        """Internal path → caller path (only this caller's own prefix is stripped)."""
        if not isinstance(path, str):
            return path
        if path == self._prefix:
            return "/"
        if path.startswith(self._prefix + "/"):
            return path[len(self._prefix) :]
        return path

    def _unscope_revision(self, token: Any) -> Any:
        if not isinstance(token, str) or "@" not in token:
            return token
        anchor, _, index = token.rpartition("@")
        return f"{self.unscope(anchor)}@{index}"

    def _unscope_dict(self, value: Any) -> Any:
        if not isinstance(value, dict):
            return self._unscope_object(value)
        out = dict(value)
        for key in _PATH_KEYS:
            if isinstance(out.get(key), str):
                out[key] = self.unscope(out[key])
        if "revision" in out:
            out["revision"] = self._unscope_revision(out["revision"])
        return out

    def _unscope_object(self, value: Any) -> Any:
        """Dataclass results (FileMetadata) carry ``.path``; rebuild with it unscoped."""
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            path = getattr(value, "path", None)
            if isinstance(path, str):
                try:
                    return dataclasses.replace(value, path=self.unscope(path))
                except (TypeError, ValueError):
                    return value
        return value

    def _unscope_entries(self, entries: Any) -> Any:
        if isinstance(entries, list):
            return [
                self.unscope(e) if isinstance(e, str) else self._unscope_dict(e) for e in entries
            ]
        return entries

    def _unscope_readdir(self, result: Any) -> Any:
        """``sys_readdir`` returns a list or a PaginatedResult (items + path cursor)."""
        if isinstance(result, list):
            return self._unscope_entries(result)
        if (
            dataclasses.is_dataclass(result)
            and not isinstance(result, type)
            and hasattr(result, "items")
        ):
            updates: dict[str, Any] = {"items": self._unscope_entries(list(result.items))}
            cursor = getattr(result, "next_cursor", None)
            if isinstance(cursor, str):
                updates["next_cursor"] = self.unscope(cursor)
            return dataclasses.replace(result, **updates)
        return result

    def _unscope_keyed(self, result: Any) -> Any:
        """Results keyed by path (``read_bulk``, ``rename_batch``)."""
        if not isinstance(result, dict):
            return result
        return {self.unscope(k): self._unscope_dict(v) for k, v in result.items()}

    def _scope_paths(self, paths: Iterable[Any]) -> list[Any]:
        return [self.scope(p) for p in paths]

    # ── single-path operations ──────────────────────────────────────────

    def sys_stat(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_dict(self._fs.sys_stat(self.scope(path), *args, **kwargs))

    def sys_read(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._fs.sys_read(self.scope(path), *args, **kwargs)

    def read(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_dict(self._fs.read(self.scope(path), *args, **kwargs))

    def read_range(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._fs.read_range(self.scope(path), *args, **kwargs)

    def stream(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._fs.stream(self.scope(path), *args, **kwargs)

    def access(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._fs.access(self.scope(path), *args, **kwargs)

    def exists(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._fs.exists(self.scope(path), *args, **kwargs)

    def get_metadata(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_object(self._fs.get_metadata(self.scope(path), *args, **kwargs))

    def get_content_id(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._fs.get_content_id(self.scope(path), *args, **kwargs)

    def write(self, *args: Any, **kwargs: Any) -> Any:
        # Callers pass ``path`` both positionally (copy) and by keyword (the
        # write route, the batch executor); forward it the way it came so
        # keyword-only implementations keep working.
        if "path" in kwargs:
            kwargs["path"] = self.scope(kwargs["path"])
        elif args:
            args = (self.scope(args[0]), *args[1:])
        return self._unscope_dict(self._fs.write(*args, **kwargs))

    def sys_write(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_dict(self._fs.sys_write(self.scope(path), *args, **kwargs))

    def write_stream(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_dict(self._fs.write_stream(self.scope(path), *args, **kwargs))

    def sys_unlink(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_dict(self._fs.sys_unlink(self.scope(path), *args, **kwargs))

    def delete(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_dict(self._fs.delete(self.scope(path), *args, **kwargs))

    def sys_rename(self, old_path: str, new_path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_dict(
            self._fs.sys_rename(self.scope(old_path), self.scope(new_path), *args, **kwargs)
        )

    def mkdir(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_dict(self._fs.mkdir(self.scope(path), *args, **kwargs))

    def rmdir(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._unscope_dict(self._fs.rmdir(self.scope(path), *args, **kwargs))

    def is_directory(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._fs.is_directory(self.scope(path), *args, **kwargs)

    # ── listing ────────────────────────────────────────────────────────

    def sys_readdir(self, path: str = "/", *args: Any, **kwargs: Any) -> Any:
        if isinstance(kwargs.get("cursor"), str):
            kwargs["cursor"] = self.scope(kwargs["cursor"])
        return self._unscope_readdir(self._fs.sys_readdir(self.scope(path), *args, **kwargs))

    def list(self, path: str = "/", *args: Any, **kwargs: Any) -> Any:
        if isinstance(kwargs.get("cursor"), str):
            kwargs["cursor"] = self.scope(kwargs["cursor"])
        return self._unscope_readdir(self._fs.list(self.scope(path), *args, **kwargs))

    # ── batch operations ───────────────────────────────────────────────

    def write_batch(self, files: Iterable[Any], *args: Any, **kwargs: Any) -> Any:
        scoped = [
            (self.scope(item[0]), *item[1:]) if isinstance(item, (list, tuple)) and item else item
            for item in files
        ]
        return self._unscope_entries(self._fs.write_batch(scoped, *args, **kwargs))

    def read_batch(self, paths: Iterable[Any], *args: Any, **kwargs: Any) -> Any:
        return self._unscope_entries(self._fs.read_batch(self._scope_paths(paths), *args, **kwargs))

    def read_bulk(self, paths: Iterable[Any], *args: Any, **kwargs: Any) -> Any:
        return self._unscope_keyed(self._fs.read_bulk(self._scope_paths(paths), *args, **kwargs))

    def rename_batch(self, renames: Iterable[Any], *args: Any, **kwargs: Any) -> Any:
        scoped = [
            (self.scope(item[0]), self.scope(item[1]), *item[2:])
            if isinstance(item, (list, tuple)) and len(item) >= 2
            else item
            for item in renames
        ]
        return self._unscope_keyed(self._fs.rename_batch(scoped, *args, **kwargs))

    # ── services ───────────────────────────────────────────────────────

    @property
    def service(self) -> Any:
        # A property so ``hasattr(fs, "service")`` stays faithful to the
        # wrapped filesystem (test fakes and minimal backends may lack it).
        inner = getattr(self._fs, "service", None)
        if inner is None:
            raise AttributeError("service")

        def _service(name: str) -> Any:
            svc = inner(name)
            if name == "search" and svc is not None:
                return _ZoneScopedSearch(svc, self)
            return svc

        return _service

    def __getattr__(self, name: str) -> Any:
        # Everything not proxied above (kernel handle, service accessors,
        # capability probes) is forwarded unchanged.
        return getattr(self._fs, name)


class _ZoneScopedSearch:
    """Path-scoping proxy for the search service handed out by :meth:`ZoneScopedFS.service`."""

    __slots__ = ("_svc", "_view")

    def __init__(self, svc: Any, view: ZoneScopedFS) -> None:
        self._svc = svc
        self._view = view

    def _scoped_files(self, files: Any) -> Any:
        if files is None:
            return None
        return [self._view.scope(f) for f in files]

    def list(self, path: str = "/", *args: Any, **kwargs: Any) -> Any:
        if isinstance(kwargs.get("cursor"), str):
            kwargs["cursor"] = self._view.scope(kwargs["cursor"])
        return self._view._unscope_readdir(self._svc.list(self._view.scope(path), *args, **kwargs))

    def glob(
        self, pattern: str, path: str = "/", *args: Any, files: Any = None, **kwargs: Any
    ) -> Any:
        result = self._svc.glob(
            pattern, self._view.scope(path), *args, files=self._scoped_files(files), **kwargs
        )
        return self._view._unscope_entries(result)

    async def grep(
        self, pattern: str, path: str = "/", *args: Any, files: Any = None, **kwargs: Any
    ) -> Any:
        result = await self._svc.grep(
            pattern, self._view.scope(path), *args, files=self._scoped_files(files), **kwargs
        )
        return self._view._unscope_entries(list(result)) if result is not None else result

    def resolve_physical_path(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._svc.resolve_physical_path(self._view.scope(path), *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._svc, name)


__all__ = [
    "ZoneScopedFS",
    "scope_rest_path",
    "zone_prefix_for",
    "zone_scoped_fs",
]
