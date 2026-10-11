"""Filesystem SDK operations over the existing typed VFS transport."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from heapq import heappop, heappush
from itertools import islice
from typing import TYPE_CHECKING, Any

from nexus.contracts.metadata import DT_DIR, DT_MOUNT
from nexus.core.pagination import PaginatedResult
from nexus.lib.zone_revision import revision_fields, revision_token

if TYPE_CHECKING:
    from nexus.remote.rpc_transport import RPCTransport


def stat_response_to_dict(resp: Any) -> dict[str, Any]:
    """Convert typed stat metadata, preserving optional fields and timestamps."""
    from datetime import UTC, datetime

    def _opt(v: str) -> str | None:
        return v or None

    created = resp.created_at_ms if resp.HasField("created_at_ms") else None
    modified = resp.modified_at_ms if resp.HasField("modified_at_ms") else None
    d: dict[str, Any] = {
        "path": resp.path,
        "size": resp.size,
        "content_id": _opt(resp.content_id),
        "mime_type": resp.mime_type,
        "is_directory": resp.is_directory,
        "entry_type": resp.entry_type,
        "mode": resp.mode,
        "version": resp.version,
        "gen": resp.gen,
        "zone_id": _opt(resp.zone_id),
        "created_at_ms": created,
        "modified_at_ms": modified,
        "last_writer_address": _opt(resp.last_writer_address),
        "link_target": _opt(resp.link_target),
        "owner_id": _opt(resp.owner_id),
    }
    if modified is not None:
        d["modified_at"] = datetime.fromtimestamp(modified / 1000.0, UTC).isoformat()
    if created is not None:
        d["created_at"] = datetime.fromtimestamp(created / 1000.0, UTC).isoformat()
    return d


def walk_entries(
    readdir: Callable[[str], list[tuple[str, int]]], prefix: str, recursive: bool
) -> Iterator[tuple[str, int]]:
    """Walk the namespace once, using one authorized Readdir per directory."""
    root = "/" + prefix.strip("/") if prefix else "/"
    pending: list[tuple[str, int]] = []
    seen_dirs: set[str] = set()
    seen_entries: set[str] = set()

    def enqueue(current: str) -> None:
        if current in seen_dirs:
            return
        seen_dirs.add(current)
        for name, entry_type in readdir(current):
            if not name or name == current or name in seen_entries:
                continue
            seen_entries.add(name)
            heappush(pending, (name, entry_type))

    enqueue(root)
    while pending:
        name, entry_type = heappop(pending)
        yield name, entry_type
        if recursive and entry_type in (DT_DIR, DT_MOUNT):
            enqueue(name.rstrip("/") or "/")


class RemoteFilesystemClient:
    """Adapt filesystem signatures to typed RPCs on a borrowed transport.

    The transport credential defines identity. Context arguments preserve the
    SDK signature; they cannot replace the daemon's authenticated identity.
    """

    def __init__(self, transport: RPCTransport) -> None:
        self._transport = transport

    @staticmethod
    def _check_range(count: int | None, offset: int) -> None:
        if offset < 0 or (count is not None and count < 0):
            raise ValueError("count and offset must be non-negative")

    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,  # noqa: ARG002
    ) -> bytes:
        self._check_range(count, offset)
        if count is None and offset == 0:
            return self._transport.read_file(path)
        result = self._transport.batch_read([(path, offset, count)])[0]
        if result.is_error:
            self._transport._handle_typed_error(result.error_payload)
        return bytes(result.content)

    def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        self._check_range(count, offset)
        if offset:
            raise NotImplementedError("The typed VFS Write RPC does not support offset writes")
        content = buf.encode("utf-8") if isinstance(buf, str) else buf
        if count is not None:
            content = content[:count]
        result = self._transport.write_file(path, content)
        return {
            **result,
            "path": path,
            "bytes_written": len(content),
            "revision": revision_token(result, path=path),
        }

    def write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,
        ttl: float | None = None,
    ) -> dict[str, Any]:
        if ttl is not None:
            raise NotImplementedError("The typed VFS Write RPC does not support TTL")
        return self.sys_write(path, buf, count=count, offset=offset, context=context)

    def sys_stat(self, path: str, *, context: Any = None, **_kwargs: Any) -> dict[str, Any] | None:
        del context
        response = self._transport.stat(path)
        return stat_response_to_dict(response) if response is not None else None

    def sys_rename(
        self, old_path: str, new_path: str, *, force: bool = False, **_kwargs: Any
    ) -> dict[str, Any]:
        if force:
            raise NotImplementedError("The typed VFS Rename RPC does not support force")
        self._transport.rename(old_path, new_path)
        return {}

    def sys_unlink(
        self, path: str, *, recursive: bool = False, context: Any = None
    ) -> dict[str, Any]:
        del context
        response = self._transport.delete(path, recursive=recursive)
        return {
            "path": response.path,
            "hit": response.success,
            "entry_type": response.entry_type,
            "content_id": response.content_id or None,
            "size": response.size,
            **revision_fields(response),
        }

    def mkdir(
        self, path: str, parents: bool = True, exist_ok: bool = True, *, context: Any = None
    ) -> dict[str, Any]:
        del context
        response = self._transport.mkdir(path, parents=parents, exist_ok=exist_ok)
        return {"path": path, **revision_fields(response)}

    def rmdir(self, path: str, recursive: bool = True, context: Any = None) -> dict[str, Any]:
        return self.sys_unlink(path, recursive=recursive, context=context)

    def sys_readdir(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        *,
        context: Any = None,
        limit: int | None = None,
        cursor: str | None = None,
        **_kwargs: Any,
    ) -> Any:
        del context
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")

        def readdir(directory: str) -> list[tuple[str, int]]:
            return [(entry.name, entry.entry_type) for entry in self._transport.readdir(directory)]

        entries = walk_entries(readdir, path, recursive)
        if cursor:
            entries = (entry for entry in entries if entry[0] > cursor)
        page = list(entries) if limit is None else list(islice(entries, limit + 1))
        has_more = limit is not None and len(page) > limit
        if has_more:
            page = page[:limit]
        items: list[Any]
        if details:
            responses = self._transport.batch_stat([name for name, _ in page]) if page else []
            items = [stat_response_to_dict(response) for response in responses if response.found]
        else:
            items = [name for name, _ in page]
        if limit is None:
            return items
        return PaginatedResult(
            items=items,
            next_cursor=page[-1][0] if has_more and page else None,
            has_more=has_more,
        )
