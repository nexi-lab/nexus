from __future__ import annotations

import builtins
import json
from typing import Any

import pytest

from nexus.core.dispatch import cat_path


class FakePyKernel:
    def op_metadata_for_path(self, path: str, zone_id: str = "root") -> dict[str, str | None]:
        if path.endswith((".json", ".jsonl", ".ndjson")):
            return {
                "filetype": "json",
                "backend": "local",
                "mime_type": "application/json",
                "backend_name": "local",
            }
        if path.endswith((".parquet", ".pq")):
            return {
                "filetype": "parquet",
                "backend": "local",
                "mime_type": "application/parquet",
                "backend_name": "local",
            }
        return {
            "filetype": "unknown",
            "backend": "local",
            "mime_type": None,
            "backend_name": "local",
        }


class FakeKernel:
    _kernel = FakePyKernel()

    def __init__(self, content: Any) -> None:
        self.content = content

    def sys_read(self, path: str, *, context=None) -> Any:
        return self.content

    def sys_stat(self, path: str, context=None) -> dict[str, object]:
        return {"mime_type": "application/json" if path.endswith(".json") else None}


def test_cat_path_pretty_prints_json() -> None:
    kernel = FakeKernel(b'{"b":2,"a":1}')
    rendered = cat_path(kernel, "/data.json")
    assert json.loads(rendered) == {"a": 1, "b": 2}
    assert rendered.endswith(b"\n")


def test_cat_path_returns_raw_for_unknown_filetype() -> None:
    kernel = FakeKernel(b"raw")
    assert cat_path(kernel, "/data.bin") == b"raw"


def test_cat_path_strict_json_error() -> None:
    kernel = FakeKernel(b"{bad")
    with pytest.raises(json.JSONDecodeError):
        cat_path(kernel, "/data.json", strict=True)


def test_cat_path_permissive_json_falls_back_to_raw() -> None:
    kernel = FakeKernel(b"{bad")
    assert cat_path(kernel, "/data.json", strict=False) == b"{bad"


def test_cat_path_returns_raw_for_json_lines() -> None:
    raw = b'{"a":1}\n{"b":2}\n'
    kernel = FakeKernel(raw)
    assert cat_path(kernel, "/data.jsonl", strict=True) == raw
    assert cat_path(kernel, "/data.ndjson", strict=True) == raw


def test_cat_path_dispatches_dict_data_payload() -> None:
    kernel = FakeKernel({"data": b'{"b":2,"a":1}', "next_offset": 17})
    rendered = cat_path(kernel, "/data.json")
    assert json.loads(rendered) == {"a": 1, "b": 2}
    assert rendered.endswith(b"\n")


def test_cat_path_dispatches_dict_content_payload() -> None:
    kernel = FakeKernel({"content": b'{"b":2,"a":1}'})
    rendered = cat_path(kernel, "/data.json")
    assert json.loads(rendered) == {"a": 1, "b": 2}
    assert rendered.endswith(b"\n")


def test_cat_path_dispatches_bytearray_payload() -> None:
    kernel = FakeKernel(bytearray(b'{"b":2,"a":1}'))
    rendered = cat_path(kernel, "/data.json")
    assert json.loads(rendered) == {"a": 1, "b": 2}


def test_cat_path_rejects_unknown_read_result_shape() -> None:
    kernel = FakeKernel({"next_offset": 12})
    with pytest.raises(TypeError, match="sys_read result dict"):
        cat_path(kernel, "/data.json")


def test_cat_path_parquet_missing_pyarrow_falls_back_to_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("pyarrow unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    kernel = FakeKernel(b"PAR1")
    assert cat_path(kernel, "/data.parquet", strict=True) == b"PAR1"


class MissingMetadataKernel:
    def op_metadata_for_path(self, path: str, zone_id: str = "root") -> dict[str, str | None]:
        raise FileNotFoundError(path)


class KernelWithMissingMetadata(FakeKernel):
    _kernel = MissingMetadataKernel()


def test_cat_path_falls_back_to_stat_when_metadata_misses() -> None:
    kernel = KernelWithMissingMetadata(b'{"b":2,"a":1}')
    rendered = cat_path(kernel, "/data.json")
    assert json.loads(rendered) == {"a": 1, "b": 2}


class TypeErrorReadKernel(FakeKernel):
    def __init__(self) -> None:
        super().__init__(b"unused")
        self.reads = 0

    def sys_read(self, path: str, *, context=None) -> bytes:
        self.reads += 1
        raise TypeError("backend bug")


def test_cat_path_does_not_retry_internal_type_error() -> None:
    kernel = TypeErrorReadKernel()
    with pytest.raises(TypeError, match="backend bug"):
        cat_path(kernel, "/data.json")
    assert kernel.reads == 1
