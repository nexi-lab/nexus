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
    _schemas = {op: MagicMock() for op in ops}
    gen.get_schema.side_effect = _schemas.get
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


class TestRealGeneratorSchemaRead:
    """Round-3 review: ensure schema reads use the public ReadmeDocGenerator API.

    Mock-only tests passed even when ``get_schema()`` did not exist on the real
    generator. Hit the real class so a regression here surfaces immediately.
    """

    def test_schema_yaml_via_real_generator(self) -> None:
        from pydantic import BaseModel, Field

        from nexus.backends.connectors.base import OpTraits
        from nexus.backends.connectors.schema_generator import ReadmeDocGenerator

        class SendEmailIn(BaseModel):
            to: str = Field(description="recipient")
            subject: str = Field(description="subject line")

        gen = ReadmeDocGenerator(
            skill_name="gmail",
            schemas={"send_email": SendEmailIn},
            operation_traits={"send_email": OpTraits()},
            error_registry={},
            examples={},
        )

        class _Backend:
            def generate_readme(self, _mp: str) -> str:
                return "# gmail"

            def get_doc_generator(self) -> ReadmeDocGenerator:
                return gen

        r = ReadmePathResolver()
        r.register_backend("/mnt/gmail", _Backend())
        out = r.try_read("/mnt/gmail/.readme/schemas/send_email.yaml")
        assert out is not None
        assert b"Schema: send_email" in out
        # Missing op → None, not AttributeError.
        assert r.try_read("/mnt/gmail/.readme/schemas/missing.yaml") is None


class TestTryStat:
    def test_stat_on_readme_file(self, mounted: ReadmePathResolver) -> None:
        out = mounted.try_stat("/mnt/gmail/.readme/README.md")
        assert out is not None
        assert out["entry_type"] == 0
        assert out["size"] > 0

    def test_stat_on_readme_dir(self, mounted: ReadmePathResolver) -> None:
        out = mounted.try_stat("/mnt/gmail/.readme")
        assert out is not None
        assert out["entry_type"] == 1

    def test_stat_on_schemas_dir(self, mounted: ReadmePathResolver) -> None:
        out = mounted.try_stat("/mnt/gmail/.readme/schemas")
        assert out is not None
        assert out["entry_type"] == 1

    def test_stat_returns_none_for_unrelated(self, mounted: ReadmePathResolver) -> None:
        assert mounted.try_stat("/mnt/gmail/INBOX/msg.yaml") is None

    def test_stat_returns_none_for_unknown_schema(self, mounted: ReadmePathResolver) -> None:
        assert mounted.try_stat("/mnt/gmail/.readme/schemas/missing.yaml") is None
