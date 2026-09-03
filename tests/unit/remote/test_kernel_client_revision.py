"""KernelClient passes the kernel's zone revision fields through (Issue #4737)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nexus.lib.zone_revision import revision_token
from nexus.remote.kernel_client import KernelClient


class _ProtoLike(SimpleNamespace):
    """Stands in for a generated protobuf message (HasField + attributes)."""

    def HasField(self, name: str) -> bool:  # noqa: N802 — protobuf API name
        return getattr(self, name, None) is not None


def _client(transport: Any) -> KernelClient:
    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = transport
    return client


def test_sys_write_carries_zone_revision_when_kernel_stamps_it() -> None:
    class Transport:
        def write_file(self, path: str, data: bytes, **_: Any) -> dict[str, Any]:
            return {
                "content_id": "abc",
                "size": 3,
                "gen": 4,
                "zone_id": "corp-eng",
                "applied_index": 57,
            }

    result = _client(Transport()).sys_write("/corp/eng/a.txt", None, b"abc")

    assert result.zone_id == "corp-eng"
    assert result.applied_index == 57
    assert revision_token(result) == "corp-eng@57"


def test_sys_write_without_stamp_yields_no_revision() -> None:
    class Transport:
        def write_file(self, path: str, data: bytes, **_: Any) -> dict[str, Any]:
            return {"content_id": "abc", "size": 3, "gen": 4}

    result = _client(Transport()).sys_write("/a.txt", None, b"abc")

    assert result.zone_id is None
    assert result.applied_index is None
    assert revision_token(result) is None


def test_sys_unlink_and_rename_pass_revision_fields_through() -> None:
    class Transport:
        def delete(self, path: str, recursive: bool) -> _ProtoLike:
            return _ProtoLike(
                success=True,
                entry_type=1,
                path=path,
                content_id="abc",
                size=3,
                zone_id="root",
                applied_index=12,
            )

        def rename(self, path: str, new_path: str) -> _ProtoLike:
            return _ProtoLike(
                hit=True,
                success=True,
                is_directory=False,
                old_content_id=None,
                old_size=None,
                old_version=None,
                old_modified_at_ms=None,
                zone_id="root",
                applied_index=13,
            )

    client = _client(Transport())

    assert revision_token(client.sys_unlink("/a.txt")) == "root@12"
    assert revision_token(client.sys_rename("/a.txt", "/b.txt")) == "root@13"


def test_pre_contract_typed_responses_yield_no_revision() -> None:
    class Transport:
        def delete(self, path: str, recursive: bool) -> _ProtoLike:
            return _ProtoLike(success=True, entry_type=1, path=path, content_id="", size=0)

        def batch_write(self, files: list[tuple[str, bytes]]) -> list[_ProtoLike]:
            return [_ProtoLike(content_id="abc", size=1, gen=1, version=1) for _ in files]

    client = _client(Transport())

    assert revision_token(client.sys_unlink("/a.txt")) is None
    items = client.write_batch([("/a", b"x")])
    assert items[0].zone_id is None and items[0].applied_index is None


def test_write_batch_carries_per_item_revision() -> None:
    class Transport:
        def batch_write(self, files: list[tuple[str, bytes]]) -> list[_ProtoLike]:
            return [
                _ProtoLike(
                    content_id="abc", size=1, gen=1, version=1, zone_id="root", applied_index=i + 1
                )
                for i, _ in enumerate(files)
            ]

    items = _client(Transport()).write_batch([("/a", b"x"), ("/b", b"y")])

    assert [revision_token(i) for i in items] == ["root@1", "root@2"]
