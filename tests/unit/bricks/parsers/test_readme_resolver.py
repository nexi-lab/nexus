"""Tests for ReadmePathResolver — VFSPathResolver for /<mount>/.readme/** paths."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.bricks.parsers.readme_resolver import ReadmePathResolver


def _make_backend(skill_name: str = "testskill", ops: list[str] | None = None) -> MagicMock:
    ops = ops or ["send_email", "delete_email"]
    backend = MagicMock()
    backend.SKILL_NAME = skill_name
    backend.generate_readme.return_value = f"# {skill_name} readme"
    gen = MagicMock()
    gen._schemas = {op: MagicMock() for op in ops}
    gen.generate_schema_yaml.return_value = f"# schema: {skill_name}"
    backend.get_doc_generator.return_value = gen
    return backend


@pytest.fixture
def resolver() -> ReadmePathResolver:
    return ReadmePathResolver()


@pytest.fixture
def mounted(resolver: ReadmePathResolver) -> ReadmePathResolver:
    backend = _make_backend("gmail", ["send_email", "create_draft"])
    resolver.register_backend("/mnt/gmail", backend)
    return resolver


class TestTryRead:
    def test_returns_none_for_unrelated_path(self, resolver: ReadmePathResolver) -> None:
        assert resolver.try_read("/mnt/gmail/INBOX/msg.yaml") is None

    def test_returns_none_when_no_backends_registered(self, resolver: ReadmePathResolver) -> None:
        assert resolver.try_read("/mnt/gmail/.readme/README.md") is None

    def test_returns_readme_bytes(self, mounted: ReadmePathResolver) -> None:
        result = mounted.try_read("/mnt/gmail/.readme/README.md")
        assert result is not None
        assert b"gmail readme" in result

    def test_readme_called_with_mount_point(self, mounted: ReadmePathResolver) -> None:
        mounted.try_read("/mnt/gmail/.readme/README.md")
        backend = mounted._backends["/mnt/gmail"]
        backend.generate_readme.assert_called_once_with("/mnt/gmail")

    def test_returns_schema_yaml(self, mounted: ReadmePathResolver) -> None:
        result = mounted.try_read("/mnt/gmail/.readme/schemas/send_email.yaml")
        assert result is not None
        assert b"schema: gmail" in result

    def test_returns_none_for_unknown_schema(self, mounted: ReadmePathResolver) -> None:
        result = mounted.try_read("/mnt/gmail/.readme/schemas/nonexistent.yaml")
        assert result is None

    def test_returns_none_for_unimplemented_subpath(self, mounted: ReadmePathResolver) -> None:
        assert mounted.try_read("/mnt/gmail/.readme/examples/foo.yaml") is None

    def test_longer_mount_wins(self) -> None:
        r = ReadmePathResolver()
        backend_short = _make_backend("short")
        backend_short.generate_readme.return_value = "short readme"
        backend_long = _make_backend("long")
        backend_long.generate_readme.return_value = "long readme"
        r.register_backend("/mnt", backend_short)
        r.register_backend("/mnt/gmail", backend_long)
        result = r.try_read("/mnt/gmail/.readme/README.md")
        assert result is not None
        assert b"long readme" in result


class TestTryWrite:
    def test_rejects_readme_write(self, mounted: ReadmePathResolver) -> None:
        with pytest.raises(PermissionError):
            mounted.try_write("/mnt/gmail/.readme/README.md", b"data")

    def test_returns_none_for_non_readme_write(self, mounted: ReadmePathResolver) -> None:
        assert mounted.try_write("/mnt/gmail/INBOX/msg.yaml", b"data") is None


class TestTryDelete:
    def test_rejects_readme_delete(self, mounted: ReadmePathResolver) -> None:
        with pytest.raises(PermissionError):
            mounted.try_delete("/mnt/gmail/.readme/README.md")

    def test_returns_none_for_non_readme_delete(self, mounted: ReadmePathResolver) -> None:
        assert mounted.try_delete("/mnt/gmail/INBOX/msg.yaml") is None


class TestRegisterUnregister:
    def test_register_then_unregister(self, resolver: ReadmePathResolver) -> None:
        backend = _make_backend()
        resolver.register_backend("/mnt/x", backend)
        assert resolver.try_read("/mnt/x/.readme/README.md") is not None
        resolver.unregister_backend("/mnt/x")
        assert resolver.try_read("/mnt/x/.readme/README.md") is None

    def test_unregister_nonexistent_is_noop(self, resolver: ReadmePathResolver) -> None:
        resolver.unregister_backend("/mnt/does_not_exist")  # must not raise

    def test_register_normalizes_trailing_slash(self, resolver: ReadmePathResolver) -> None:
        backend = _make_backend()
        resolver.register_backend("/mnt/x/", backend)
        assert resolver.try_read("/mnt/x/.readme/README.md") is not None


class TestHookSpec:
    def test_hook_spec_includes_self_as_resolver(self, resolver: ReadmePathResolver) -> None:
        from nexus.contracts.protocols.service_hooks import HookSpec

        spec = resolver.hook_spec()
        assert isinstance(spec, HookSpec)
        assert resolver in spec.resolvers
