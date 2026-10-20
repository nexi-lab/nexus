"""Native filesystem SDK with one authenticated channel to the data owner."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import suppress
from functools import partial
from types import SimpleNamespace
from typing import Any, Self, cast

from nexus.contracts.exceptions import (
    AuthenticationError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    RemoteConnectionError,
    RemoteTimeoutError,
)
from nexus.contracts.metadata import DT_DIR, DT_MOUNT
from nexus.grpc.vfs import vfs_pb2
from nexus.remote.rpc_transport import RPCTransport
from nexus.remote.service_proxy import RemoteServiceProxy
from nexus.remote.vfs_client import RemoteFilesystemClient, stat_response_to_dict


class RemoteNexusFS(RemoteFilesystemClient):
    """Own the remote connection. All file facts and authority stay in the daemon."""

    def __init__(self, transport: RPCTransport, *, zone_id: str | None = None) -> None:
        super().__init__(transport)
        self._closed = False
        self.zone_id = zone_id
        self._search = RemoteServiceProxy(
            transport.call_rpc,
            service_name="search",
            default_context=SimpleNamespace(zone_id=zone_id),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def call_rpc(self, method: str, params: dict[str, Any] | None = None, **options: Any) -> Any:
        """Call an existing native registry or plugin method on the owned channel."""
        return self._transport.call_rpc(method, params, **options)

    def service(self, name: str) -> RemoteServiceProxy | None:
        return self._search if name == "search" else None

    def read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,
        return_metadata: bool = False,
    ) -> bytes:
        if return_metadata:
            raise NotImplementedError("The native Read RPC has no metadata snapshot; use sys_stat")
        return self.sys_read(path, count=count, offset=offset, context=context)

    def stat(self, path: str, *, context: Any = None) -> dict[str, Any]:
        result = self.sys_stat(path, context=context)
        if result is None:
            raise NexusFileNotFoundError(path)
        return result

    def exists(self, path: str, *, context: Any = None) -> bool:
        return self.sys_stat(path, context=context) is not None

    def access(self, path: str, *, context: Any = None) -> bool:
        try:
            return self.exists(path, context=context)
        except NexusPermissionError:
            return False

    def is_directory(self, path: str, *, context: Any = None) -> bool:
        result = self.sys_stat(path, context=context)
        return result is not None and result["entry_type"] in (DT_DIR, DT_MOUNT)

    def stat_bulk(self, paths: list[str], *, context: Any = None) -> dict[str, Any]:
        del context
        results = self._transport.batch_stat(paths)
        return {
            path: stat_response_to_dict(result) if result.found else None
            for path, result in zip(paths, results, strict=True)
        }

    def metadata_batch(self, paths: list[str], *, context: Any = None) -> dict[str, Any]:
        return self.stat_bulk(paths, context=context)

    def read_batch(
        self, paths: list[str], *, partial: bool = False, context: Any = None
    ) -> list[dict[str, Any]]:
        del context
        results = self._transport.batch_read([(path, 0, None) for path in paths])
        items = []
        for path, result in zip(paths, results, strict=True):
            if result.is_error:
                if not partial:
                    self._transport._handle_typed_error(result.error_payload)
                items.append({"path": path, "content": None, "error": bytes(result.error_payload)})
            else:
                items.append({"path": path, "content": bytes(result.content)})
        return items

    def read_bulk(self, paths: list[str], *, context: Any = None) -> dict[str, bytes | None]:
        results = self.read_batch(paths, partial=True, context=context)
        output = {}
        for item in results:
            if "error" in item:
                with suppress(NexusFileNotFoundError, NexusPermissionError):
                    self._transport._handle_typed_error(item["error"])
            output[item["path"]] = item["content"]
        return output

    def write_batch(
        self, files: list[tuple[str, bytes]], *, context: Any = None
    ) -> list[dict[str, Any]]:
        del context
        return [
            {
                "path": path,
                "content_id": result.content_id,
                "size": result.size,
                "gen": result.gen,
                "version": result.version,
            }
            for (path, _), result in zip(files, self._transport.batch_write(files), strict=True)
        ]

    def sys_copy(self, src_path: str, dst_path: str, *, context: Any = None) -> dict[str, Any]:
        del context
        response = self._transport.copy(src_path, dst_path)
        return {field.name: value for field, value in response.ListFields()}

    def sys_setattr(self, path: str, *, context: Any = None, **attrs: Any) -> dict[str, Any]:
        del context
        allowed = set(vfs_pb2.SetattrRequest.DESCRIPTOR.fields_by_name) - {"path", "auth_token"}
        unknown = attrs.keys() - allowed
        if unknown:
            raise ValueError(f"Unsupported native file attributes: {', '.join(sorted(unknown))}")
        response = self._transport.setattr(path, **attrs)
        return {field.name: value for field, value in response.ListFields()}

    @staticmethod
    def _independent(
        operations: list[tuple[str, Callable[[], dict[str, Any]]]],
    ) -> dict[str, dict[str, Any]]:
        results = {}
        if len({path for path, _ in operations}) != len(operations):
            raise ValueError("Batch paths must be unique")
        for path, operation in operations:
            try:
                results[path] = operation()
            except (AuthenticationError, RemoteConnectionError, RemoteTimeoutError):
                raise
            except (NexusError, OSError, ValueError) as exc:
                results[path] = {"success": False, "error": str(exc)}
        return results

    def rename_batch(self, renames: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
        def rename(source: str, destination: str) -> dict[str, Any]:
            return {
                **self.sys_rename(source, destination),
                "success": True,
                "new_path": destination,
            }

        return self._independent(
            [(source, partial(rename, source, destination)) for source, destination in renames]
        )

    def delete_batch(
        self, paths: list[str], *, recursive: bool = False
    ) -> dict[str, dict[str, Any]]:
        def delete(path: str) -> dict[str, Any]:
            receipt = self.sys_unlink(path, recursive=recursive)
            return {**receipt, "success": receipt["hit"]}

        return self._independent([(path, partial(delete, path)) for path in paths])

    def exists_batch(self, paths: list[str], *, context: Any = None) -> dict[str, bool]:
        return {
            path: result is not None
            for path, result in self.stat_bulk(paths, context=context).items()
        }

    def read_range(self, path: str, start: int, end: int, *, context: Any = None) -> bytes:
        if start < 0 or end < start:
            raise ValueError("A range must have non-negative bounds with end >= start")
        return self.sys_read(path, count=end - start, offset=start, context=context)

    def stream(self, path: str, chunk_size: int = 65536, *, context: Any = None) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        offset = 0
        while True:
            chunk = self.sys_read(path, count=chunk_size, offset=offset, context=context)
            if not chunk:
                return
            yield chunk
            offset += len(chunk)
            if len(chunk) < chunk_size:
                return

    def stream_range(
        self, path: str, start: int, end: int, chunk_size: int = 65536, *, context: Any = None
    ) -> Iterator[bytes]:
        if start < 0 or end < start or chunk_size <= 0:
            raise ValueError("A range must have non-negative bounds and a positive chunk size")
        offset = start
        while offset < end:
            chunk = self.sys_read(
                path, count=min(chunk_size, end - offset), offset=offset, context=context
            )
            if not chunk:
                return
            yield chunk
            offset += len(chunk)

    def glob(
        self,
        pattern: str,
        path: str = "/",
        context: Any = None,
        *,
        files: list[str] | None = None,
        max_results: int = 10_000,
    ) -> list[str]:
        return cast(
            list[str],
            self._search.glob(
                pattern=pattern, path=path, context=context, files=files, max_results=max_results
            ),
        )

    def grep(self, pattern: str, path: str = "/", **options: Any) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._search.grep(pattern=pattern, path=path, **options))

    def semantic_search(self, query: str, **options: Any) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._search.semantic_search(query=query, **options))

    def semantic_search_index(self, path: str = "/", **options: Any) -> dict[str, Any]:
        return cast(dict[str, Any], self._search.semantic_search_index(path=path, **options))

    def semantic_search_stats(self, **options: Any) -> dict[str, Any]:
        return cast(dict[str, Any], self._search.semantic_search_stats(**options))
