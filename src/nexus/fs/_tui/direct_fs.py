"""Direct filesystem adapters for the playground TUI.

Bypass the NexusFS kernel and talk to backends directly so users see
real files immediately — no API seeding required.

    nexus-fs playground local:///some/dir       → LocalDirectFS (pathlib)
    nexus-fs playground s3://my-bucket          → S3DirectFS (boto3)
    nexus-fs playground s3://bucket local://dir → MultiDirectFS (combines both)
"""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Local filesystem (pathlib)
# ---------------------------------------------------------------------------


class LocalDirectFS:
    """Direct local filesystem — reads real files on disk via pathlib."""

    def __init__(self, root: Path, mount_point: str) -> None:
        self._root = root.resolve()
        self._mount_point = mount_point

    def _to_real(self, virtual_path: str) -> Path:
        rel = virtual_path
        if rel.startswith(self._mount_point):
            rel = rel[len(self._mount_point) :]
        rel = rel.lstrip("/")
        return self._root / rel

    def _to_virtual(self, real_path: Path) -> str:
        try:
            rel = real_path.resolve().relative_to(self._root)
            return f"{self._mount_point}/{rel}" if str(rel) != "." else self._mount_point
        except ValueError:
            return self._mount_point

    async def read(self, path: str) -> bytes:
        return self._to_real(path).read_bytes()

    async def read_range(self, path: str, start: int, end: int) -> bytes:
        with open(self._to_real(path), "rb") as f:
            f.seek(start)
            return f.read(end - start)

    async def write(self, path: str, content: bytes) -> dict[str, Any]:
        real = self._to_real(path)
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(content)
        return {"path": path, "size": len(content)}

    async def ls(
        self,
        path: str = "/",
        detail: bool = False,
        recursive: bool = False,
    ) -> list[str] | list[dict[str, Any]]:
        real = self._to_real(path)
        if not real.is_dir():
            return []
        items = sorted(real.rglob("*")) if recursive else sorted(real.iterdir())
        entries = [self._stat_entry(p) for p in items]
        return entries if detail else [e["path"] for e in entries]

    async def stat(self, path: str) -> dict[str, Any] | None:
        real = self._to_real(path)
        return self._stat_entry(real) if real.exists() else None

    async def mkdir(self, path: str, parents: bool = True) -> None:
        self._to_real(path).mkdir(parents=parents, exist_ok=True)

    async def rmdir(self, path: str, recursive: bool = False) -> None:
        real = self._to_real(path)
        if recursive:
            import shutil

            shutil.rmtree(real)
        else:
            real.rmdir()

    async def delete(self, path: str) -> None:
        self._to_real(path).unlink()

    async def rename(self, old_path: str, new_path: str) -> None:
        self._to_real(old_path).rename(self._to_real(new_path))

    async def exists(self, path: str) -> bool:
        return self._to_real(path).exists()

    async def copy(self, src: str, dst: str) -> dict[str, Any]:
        import shutil

        src_real = self._to_real(src)
        dst_real = self._to_real(dst)
        dst_real.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_real, dst_real)
        st = dst_real.stat()
        return {"path": dst, "size": st.st_size, "etag": None}

    def list_mounts(self) -> list[str]:
        return [self._mount_point]

    async def close(self) -> None:
        pass

    def _stat_entry(self, real_path: Path) -> dict[str, Any]:
        st = real_path.stat()
        is_dir = real_path.is_dir()
        mime, _ = mimetypes.guess_type(str(real_path))
        return {
            "path": self._to_virtual(real_path),
            "size": 4096 if is_dir else st.st_size,
            "is_directory": is_dir,
            "etag": None,
            "mime_type": mime or ("inode/directory" if is_dir else "application/octet-stream"),
            "created_at": datetime.fromtimestamp(st.st_ctime, tz=UTC).isoformat(),
            "modified_at": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
            "version": 0,
            "zone_id": "root",
            "entry_type": 1 if is_dir else 0,
        }


# ---------------------------------------------------------------------------
# S3 (boto3)
# ---------------------------------------------------------------------------


class S3DirectFS:
    """Direct S3 access — lists and reads objects via boto3."""

    def __init__(self, bucket: str, prefix: str = "", mount_point: str = "") -> None:
        import boto3
        from botocore.config import Config

        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._mount_point = mount_point or f"/s3/{bucket}"
        self._s3 = boto3.client("s3", config=Config(connect_timeout=5, read_timeout=10))

    def _to_key(self, virtual_path: str) -> str:
        """Virtual path → S3 key."""
        rel = virtual_path
        if rel.startswith(self._mount_point):
            rel = rel[len(self._mount_point) :]
        rel = rel.strip("/")
        if self._prefix:
            return f"{self._prefix}/{rel}" if rel else self._prefix
        return rel

    def _to_virtual(self, key: str) -> str:
        """S3 key → virtual path."""
        rel = key
        if self._prefix and rel.startswith(self._prefix):
            rel = rel[len(self._prefix) :].lstrip("/")
        return f"{self._mount_point}/{rel}" if rel else self._mount_point

    async def read(self, path: str) -> bytes:
        key = self._to_key(path)
        resp = self._s3.get_object(Bucket=self._bucket, Key=key)
        data: bytes = resp["Body"].read()
        return data

    async def read_range(self, path: str, start: int, end: int) -> bytes:
        key = self._to_key(path)
        resp = self._s3.get_object(Bucket=self._bucket, Key=key, Range=f"bytes={start}-{end - 1}")
        data: bytes = resp["Body"].read()
        return data

    async def write(self, path: str, content: bytes) -> dict[str, Any]:
        key = self._to_key(path)
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=content)
        return {"path": path, "size": len(content)}

    async def ls(
        self,
        path: str = "/",
        detail: bool = False,
        recursive: bool = False,
    ) -> list[str] | list[dict[str, Any]]:
        prefix = self._to_key(path)
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        # Root of mount
        if prefix == "/":
            prefix = ""

        entries: list[dict[str, Any]] = []
        dirs_seen: set[str] = set()

        paginator = self._s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=self._bucket,
            Prefix=prefix,
            **({} if recursive else {"Delimiter": "/"}),
        )

        for page in pages:
            # Common prefixes = directories
            for cp in page.get("CommonPrefixes", []):
                dir_key = cp["Prefix"].rstrip("/")
                if dir_key not in dirs_seen:
                    dirs_seen.add(dir_key)
                    entries.append(
                        {
                            "path": self._to_virtual(dir_key),
                            "size": 4096,
                            "is_directory": True,
                            "etag": None,
                            "mime_type": "inode/directory",
                            "created_at": None,
                            "modified_at": None,
                            "version": 0,
                            "zone_id": "root",
                            "entry_type": 1,
                        }
                    )

            # Objects = files
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue  # Skip directory markers
                mime, _ = mimetypes.guess_type(key)
                entries.append(
                    {
                        "path": self._to_virtual(key),
                        "size": obj.get("Size", 0),
                        "is_directory": False,
                        "etag": obj.get("ETag", "").strip('"'),
                        "mime_type": mime or "application/octet-stream",
                        "created_at": None,
                        "modified_at": obj["LastModified"].isoformat()
                        if obj.get("LastModified")
                        else None,
                        "version": 0,
                        "zone_id": "root",
                        "entry_type": 0,
                    }
                )

        return entries if detail else [e["path"] for e in entries]

    async def stat(self, path: str) -> dict[str, Any] | None:
        key = self._to_key(path)
        try:
            resp = self._s3.head_object(Bucket=self._bucket, Key=key)
            mime, _ = mimetypes.guess_type(key)
            return {
                "path": path,
                "size": resp.get("ContentLength", 0),
                "is_directory": False,
                "etag": resp.get("ETag", "").strip('"'),
                "mime_type": mime or "application/octet-stream",
                "created_at": None,
                "modified_at": resp["LastModified"].isoformat()
                if resp.get("LastModified")
                else None,
                "version": 0,
                "zone_id": "root",
                "entry_type": 0,
            }
        except Exception:
            return None

    async def mkdir(self, path: str, parents: bool = True) -> None:
        pass  # S3 doesn't have real directories

    async def rmdir(self, path: str, recursive: bool = False) -> None:
        pass

    async def delete(self, path: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=self._to_key(path))

    async def rename(self, old_path: str, new_path: str) -> None:
        old_key = self._to_key(old_path)
        new_key = self._to_key(new_path)
        # Server-side copy
        self._s3.copy(
            {"Bucket": self._bucket, "Key": old_key},
            self._bucket,
            new_key,
        )
        # Verify destination before deleting source
        self._s3.head_object(Bucket=self._bucket, Key=new_key)
        self._s3.delete_object(Bucket=self._bucket, Key=old_key)

    async def exists(self, path: str) -> bool:
        return (await self.stat(path)) is not None

    async def copy(self, src: str, dst: str) -> dict[str, Any]:
        src_key = self._to_key(src)
        dst_key = self._to_key(dst)
        self._s3.copy(
            {"Bucket": self._bucket, "Key": src_key},
            self._bucket,
            dst_key,
        )
        head = self._s3.head_object(Bucket=self._bucket, Key=dst_key)
        return {"path": dst, "size": head.get("ContentLength", 0), "etag": head.get("ETag")}

    def list_mounts(self) -> list[str]:
        return [self._mount_point]

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Multi-mount combiner
# ---------------------------------------------------------------------------


class MultiDirectFS:
    """Combines multiple DirectFS instances under one interface."""

    def __init__(self, backends: list[Any]) -> None:
        self._backends = backends
        self._mount_map: dict[str, Any] = {}
        for b in backends:
            for mp in b.list_mounts():
                self._mount_map[mp] = b

    def _resolve(self, path: str) -> Any:
        """Find the backend that owns this path."""
        for mp, backend in self._mount_map.items():
            if path == mp or path.startswith(mp + "/"):
                return backend
        # Fallback to first backend
        return self._backends[0] if self._backends else None

    def read(self, path: str) -> bytes:
        result: bytes = self._resolve(path).read(path)
        return result

    def read_range(self, path: str, start: int, end: int) -> bytes:
        result: bytes = self._resolve(path).read_range(path, start, end)
        return result

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        result: dict[str, Any] = self._resolve(path).write(path, content)
        return result

    async def ls(
        self,
        path: str = "/",
        detail: bool = False,
        recursive: bool = False,
    ) -> list[str] | list[dict[str, Any]]:
        result: list[str] | list[dict[str, Any]] = await self._resolve(path).ls(
            path, detail=detail, recursive=recursive
        )
        return result

    async def stat(self, path: str) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self._resolve(path).stat(path)
        return result

    async def mkdir(self, path: str, parents: bool = True) -> None:
        self._resolve(path).mkdir(path, parents=parents)

    async def rmdir(self, path: str, recursive: bool = False) -> None:
        self._resolve(path).rmdir(path, recursive=recursive)

    async def delete(self, path: str) -> None:
        await self._resolve(path).delete(path)

    async def rename(self, old_path: str, new_path: str) -> None:
        await self._resolve(old_path).rename(old_path, new_path)

    async def exists(self, path: str) -> bool:
        result: bool = await self._resolve(path).exists(path)
        return result

    async def copy(self, src: str, dst: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._resolve(src).copy(src, dst)
        return result

    def list_mounts(self) -> list[str]:
        return list(self._mount_map.keys())

    async def close(self) -> None:
        for b in self._backends:
            await b.close()
