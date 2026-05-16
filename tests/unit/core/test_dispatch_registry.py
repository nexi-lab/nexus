from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from nexus.core.dispatch import (
    BackendKind,
    FileType,
    OperationRequest,
    OpKey,
    OpsRegistry,
    get_global_registry,
    grep_path,
    normalize_backend,
    normalize_filetype,
    register_backend_ops,
    register_default_ops,
    register_parser_ops,
    reset_global_registry_for_tests,
)


def test_registry_resolution_order() -> None:
    registry = OpsRegistry()
    registry.register(OpKey("cat", None, None), lambda req: b"default")
    registry.register(OpKey("cat", FileType.JSON, None), lambda req: b"json")
    registry.register(OpKey("cat", None, BackendKind.GITHUB), lambda req: b"github")
    registry.register(OpKey("cat", FileType.JSON, BackendKind.GITHUB), lambda req: b"exact")

    req = OperationRequest(
        op="cat",
        path="/repo/data.json",
        filetype=FileType.JSON,
        backend=BackendKind.GITHUB,
        content=b"{}",
    )
    assert registry.resolve(req.op, req.filetype, req.backend)(req) == b"exact"
    assert registry.resolve("cat", FileType.UNKNOWN, BackendKind.GITHUB)(req) == b"github"
    assert registry.resolve("cat", FileType.JSON, BackendKind.LOCAL)(req) == b"json"
    assert registry.resolve("cat", FileType.UNKNOWN, BackendKind.LOCAL)(req) == b"default"


def test_duplicate_register_rejects_and_replace_updates() -> None:
    registry = OpsRegistry()
    key = OpKey("cat", None, None)
    registry.register(key, lambda req: b"one")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(key, lambda req: b"two")
    registry.replace(key, lambda req: b"two")
    req = OperationRequest(
        op="cat",
        path="/a",
        filetype=FileType.UNKNOWN,
        backend=BackendKind.UNKNOWN,
        content=b"",
    )
    assert registry.resolve("cat", FileType.UNKNOWN, BackendKind.UNKNOWN)(req) == b"two"


def test_normalizers_cover_requested_types() -> None:
    assert normalize_filetype("/tmp/data.json", None) == FileType.JSON
    assert normalize_filetype("/tmp/data.parquet", None) == FileType.PARQUET
    assert normalize_filetype("/tmp/data", "application/json") == FileType.JSON
    assert normalize_filetype("/tmp/data.bin", None) == FileType.UNKNOWN
    assert normalize_backend("path_s3") == BackendKind.S3
    assert normalize_backend("slack_connector") == BackendKind.SLACK
    assert normalize_backend("github_connector") == BackendKind.GITHUB
    assert normalize_backend("path_local") == BackendKind.LOCAL
    assert normalize_backend("unknown_backend") == BackendKind.UNKNOWN


def test_default_and_parser_registration() -> None:
    registry = OpsRegistry()
    register_default_ops(registry)
    register_parser_ops(registry)
    req = OperationRequest(
        op="cat",
        path="/data.json",
        filetype=FileType.JSON,
        backend=BackendKind.LOCAL,
        content=b'{"b":2,"a":1}',
        strict=True,
    )
    rendered = registry.resolve("cat", FileType.JSON, BackendKind.LOCAL)(req)
    assert json.loads(rendered) == {"a": 1, "b": 2}
    assert rendered.endswith(b"\n")


def test_backend_registration_adds_requested_overrides() -> None:
    registry = OpsRegistry()
    register_default_ops(registry)
    register_backend_ops(registry)
    assert registry.resolve("grep", FileType.UNKNOWN, BackendKind.SLACK) is not None
    assert registry.resolve("raw_read", FileType.UNKNOWN, BackendKind.GITHUB) is not None
    assert registry.resolve("fingerprint", FileType.UNKNOWN, BackendKind.S3) is not None


def test_backend_handlers_forward_to_backend_instances() -> None:
    @dataclass
    class FakeContext:
        backend_path: str | None = None

    class FakeSlack:
        def grep_messages(
            self,
            pattern: str,
            *,
            context: object,
            max_results: int,
            ignore_case: bool,
            backend_path: str,
            mount_path: str,
        ) -> list[dict[str, Any]]:
            assert pattern == "error"
            assert isinstance(context, FakeContext)
            assert max_results == 2
            assert ignore_case is True
            assert backend_path == "channels/general.yaml"
            assert mount_path == "/slack"
            return [{"file": "/slack/channels/general.yaml"}]

    class FakeGitHub:
        def raw_read(self, path: str, *, context: object) -> bytes:
            assert path == "owner/repo/main/README.md"
            assert getattr(context, "backend_path", None) == "owner/repo/main/README.md"
            return b"readme"

    class FakeS3:
        def fingerprint(self, path: str, *, context: object) -> str:
            assert path == "bucket/key"
            assert getattr(context, "backend_path", None) == "bucket/key"
            return "etag:abc"

    registry = OpsRegistry()
    register_backend_ops(registry)
    ctx = FakeContext()

    grep = registry.resolve("grep", FileType.UNKNOWN, BackendKind.SLACK)
    assert grep is not None
    assert grep(
        OperationRequest(
            op="grep",
            path="/slack",
            filetype=FileType.UNKNOWN,
            backend=BackendKind.SLACK,
            context=ctx,
            pattern="error",
            ignore_case=True,
            max_results=2,
            metadata={
                "backend_instance": FakeSlack(),
                "backend_path": "channels/general.yaml",
                "mount_path": "/slack",
            },
        )
    ) == [{"file": "/slack/channels/general.yaml"}]

    raw_read = registry.resolve("raw_read", FileType.UNKNOWN, BackendKind.GITHUB)
    assert raw_read is not None
    assert (
        raw_read(
            OperationRequest(
                op="raw_read",
                path="/repo/README.md",
                filetype=FileType.UNKNOWN,
                backend=BackendKind.GITHUB,
                context=ctx,
                metadata={
                    "backend_instance": FakeGitHub(),
                    "backend_path": "owner/repo/main/README.md",
                },
            )
        )
        == b"readme"
    )

    fingerprint = registry.resolve("fingerprint", FileType.UNKNOWN, BackendKind.S3)
    assert fingerprint is not None
    assert (
        fingerprint(
            OperationRequest(
                op="fingerprint",
                path="/bucket/key",
                filetype=FileType.UNKNOWN,
                backend=BackendKind.S3,
                context=ctx,
                metadata={"backend_instance": FakeS3(), "backend_path": "bucket/key"},
            )
        )
        == "etag:abc"
    )


def test_grep_path_uses_mounted_backend_instance() -> None:
    class FakeSlack:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, str, str]] = []

        def grep_messages(
            self,
            pattern: str,
            *,
            context: object,
            max_results: int,
            ignore_case: bool,
            backend_path: str,
            mount_path: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((pattern, max_results, backend_path, mount_path))
            return [{"file": "/slack/channels/general.yaml"}]

    class FakePyKernel:
        def op_metadata_for_path(self, path: str, zone_id: str = "root") -> dict[str, Any]:
            return {
                "filetype": "unknown",
                "backend": "unknown",
                "backend_name": "",
                "mime_type": None,
            }

    slack = FakeSlack()

    class FakeKernel:
        _kernel = FakePyKernel()

    FakeKernel._mounted_backend_instances = {"/slack": slack}

    assert grep_path(FakeKernel(), "error", "/slack/channels/general.yaml", max_results=3) == [
        {"file": "/slack/channels/general.yaml"}
    ]
    assert slack.calls == [("error", 3, "channels/general.yaml", "/slack")]


def test_grep_path_preserves_empty_backend_path_for_mount_root() -> None:
    class FakeSlack:
        def __init__(self) -> None:
            self.backend_paths: list[str] = []

        def grep_messages(
            self,
            pattern: str,
            *,
            context: object,
            max_results: int,
            ignore_case: bool,
            backend_path: str,
            mount_path: str,
        ) -> list[dict[str, Any]]:
            self.backend_paths.append(backend_path)
            return [{"file": "/slack/channels/general.yaml"}]

    class FakePyKernel:
        def op_metadata_for_path(self, path: str, zone_id: str = "root") -> dict[str, Any]:
            return {
                "filetype": "unknown",
                "backend": "unknown",
                "backend_name": "",
                "mime_type": None,
            }

    slack = FakeSlack()

    class FakeKernel:
        _kernel = FakePyKernel()

    FakeKernel._mounted_backend_instances = {"/slack": slack}

    assert grep_path(FakeKernel(), "error", "/slack") == [{"file": "/slack/channels/general.yaml"}]
    assert slack.backend_paths == [""]


def test_grep_path_falls_back_when_backend_instance_is_unavailable() -> None:
    class FakePyKernel:
        def op_metadata_for_path(self, path: str, zone_id: str = "root") -> dict[str, Any]:
            return {
                "filetype": "unknown",
                "backend": "slack",
                "backend_name": "slack",
                "mime_type": None,
            }

    class FakeKernel:
        _kernel = FakePyKernel()

    assert grep_path(FakeKernel(), "error", "/slack") is None


def test_grep_path_returns_none_without_backend_override() -> None:
    class FakePyKernel:
        def op_metadata_for_path(self, path: str, zone_id: str = "root") -> dict[str, Any]:
            return {
                "filetype": "unknown",
                "backend": "local",
                "backend_name": "local",
                "mime_type": None,
            }

    class FakeKernel:
        _kernel = FakePyKernel()

    assert grep_path(FakeKernel(), "error", "/data") is None


def test_global_registry_bootstrap_is_idempotent() -> None:
    reset_global_registry_for_tests()
    first = get_global_registry()
    second = get_global_registry()
    assert first is second
    assert first.resolve("cat", FileType.JSON, BackendKind.LOCAL) is not None
