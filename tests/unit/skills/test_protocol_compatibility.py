"""Test that Skills Protocol matches Core ABC exactly.

This test ensures that nexus.skills.protocols.NexusFilesystem stays in sync
with nexus.core.filesystem.NexusFilesystem (ABC).

If these tests fail, it means the Protocol and ABC have drifted and need
to be synchronized.
"""

import inspect

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.filesystem import NexusFilesystem as NexusFilesystemABC
from nexus.skills.protocols import NexusFilesystem as NexusFilesystemProtocol


def test_protocol_has_all_abc_methods() -> None:
    """Verify Protocol defines all ABC public methods.

    This catches when ABC adds new methods that haven't been added to Protocol.
    """
    # Get all public abstract methods from ABC
    abc_methods = {
        name
        for name, method in inspect.getmembers(NexusFilesystemABC, predicate=inspect.isfunction)
        if not name.startswith("_") or name in ("__enter__", "__exit__")
    }

    # Get all public methods from Protocol
    # Protocols define methods as attributes, so we check the annotations
    protocol_methods = set()
    for name in dir(NexusFilesystemProtocol):
        if name.startswith("_") and name not in ("__enter__", "__exit__"):
            continue
        attr = getattr(NexusFilesystemProtocol, name, None)
        if attr is not None and (callable(attr) or name in ("__enter__", "__exit__")):
            protocol_methods.add(name)

    missing = abc_methods - protocol_methods
    extra = protocol_methods - abc_methods

    assert not missing, f"Protocol missing methods from ABC: {sorted(missing)}"
    assert not extra, f"Protocol has extra methods not in ABC: {sorted(extra)}"


def test_protocol_method_signatures_match() -> None:
    """Verify Protocol method signatures match ABC exactly.

    This catches signature changes (parameters, types, defaults).
    """
    # Get all public methods from ABC
    abc_methods = {
        name: method
        for name, method in inspect.getmembers(NexusFilesystemABC, predicate=inspect.isfunction)
        if not name.startswith("_") or name in ("__enter__", "__exit__")
    }

    for method_name, abc_method in abc_methods.items():
        protocol_method = getattr(NexusFilesystemProtocol, method_name, None)

        assert protocol_method is not None, f"Protocol missing method: {method_name}"

        # Get signatures
        abc_sig = inspect.signature(abc_method)
        protocol_sig = inspect.signature(protocol_method)

        # Compare parameter names and order
        abc_params = list(abc_sig.parameters.keys())
        protocol_params = list(protocol_sig.parameters.keys())

        assert abc_params == protocol_params, (
            f"Method {method_name} parameter mismatch:\n"
            f"  ABC:      {abc_params}\n"
            f"  Protocol: {protocol_params}"
        )

        # Compare parameter types and defaults
        for param_name in abc_params:
            abc_param = abc_sig.parameters[param_name]
            protocol_param = protocol_sig.parameters[param_name]

            # Check annotation
            if abc_param.annotation != inspect.Parameter.empty:
                assert protocol_param.annotation == abc_param.annotation, (
                    f"Method {method_name} parameter '{param_name}' type mismatch:\n"
                    f"  ABC:      {abc_param.annotation}\n"
                    f"  Protocol: {protocol_param.annotation}"
                )

            # Check default value
            if abc_param.default != inspect.Parameter.empty:
                assert protocol_param.default == abc_param.default, (
                    f"Method {method_name} parameter '{param_name}' default mismatch:\n"
                    f"  ABC:      {abc_param.default}\n"
                    f"  Protocol: {protocol_param.default}"
                )

        # Compare return types
        abc_return = abc_sig.return_annotation
        protocol_return = protocol_sig.return_annotation

        if abc_return != inspect.Signature.empty:
            assert protocol_return == abc_return, (
                f"Method {method_name} return type mismatch:\n"
                f"  ABC:      {abc_return}\n"
                f"  Protocol: {protocol_return}"
            )


def test_nexus_fs_satisfies_protocol() -> None:
    """Verify NexusFS implementation satisfies the Protocol.

    This ensures the actual implementation (NexusFS) works with code
    expecting the Protocol interface.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        nx = NexusFS(backend=LocalBackend(tmpdir))

        # Verify nx satisfies the Protocol
        def accepts_protocol(fs: NexusFilesystemProtocol) -> None:
            """Function that requires Protocol interface."""
            # Check all methods exist and are callable
            assert callable(getattr(fs, "read", None))
            assert callable(getattr(fs, "write", None))
            assert callable(getattr(fs, "delete", None))
            assert callable(getattr(fs, "exists", None))
            assert callable(getattr(fs, "list", None))
            assert callable(getattr(fs, "glob", None))
            assert callable(getattr(fs, "grep", None))
            assert callable(getattr(fs, "mkdir", None))
            assert callable(getattr(fs, "rmdir", None))
            assert callable(getattr(fs, "is_directory", None))
            assert callable(getattr(fs, "add_mount", None))
            assert callable(getattr(fs, "remove_mount", None))
            assert callable(getattr(fs, "list_mounts", None))
            assert callable(getattr(fs, "get_mount", None))
            assert callable(getattr(fs, "close", None))

        # This should pass without errors
        accepts_protocol(nx)

        # Also verify isinstance check works with @runtime_checkable
        assert isinstance(nx, NexusFilesystemProtocol)


def test_protocol_runtime_checkable() -> None:
    """Verify Protocol is runtime_checkable and works with isinstance().

    This allows runtime validation of filesystem-like objects.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        nx = NexusFS(backend=LocalBackend(tmpdir))

        # Protocol should support isinstance() check
        assert isinstance(nx, NexusFilesystemProtocol)

        # Mock object with required methods should also pass
        class MockFilesystem:
            _agent_id: str | None = None
            _tenant_id: str | None = None

            @property
            def agent_id(self) -> str | None:
                return self._agent_id

            @property
            def tenant_id(self) -> str | None:
                return self._tenant_id

            def read(self, path: str) -> bytes:
                return b""

            def write(self, path: str, content: bytes) -> None:
                pass

            def write_batch(self, files, context=None):
                return []

            def delete(self, path: str) -> None:
                pass

            def rename(self, old_path: str, new_path: str) -> None:
                pass

            def exists(self, path: str) -> bool:
                return True

            def list(
                self,
                path: str = "/",
                recursive: bool = True,
                details: bool = False,
                prefix: str | None = None,
                show_parsed: bool = True,
            ):
                return []

            def glob(self, pattern: str, path: str = "/"):
                return []

            def grep(
                self,
                pattern: str,
                path: str = "/",
                file_pattern: str | None = None,
                ignore_case: bool = False,
                max_results: int = 1000,
                search_mode: str = "auto",
            ):
                return []

            def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
                pass

            def rmdir(self, path: str, recursive: bool = False) -> None:
                pass

            def is_directory(self, path: str) -> bool:
                return False

            def get_available_namespaces(self):
                return []

            def get_version(self, path: str, version: int) -> bytes:
                return b""

            def list_versions(self, path: str):
                return []

            def rollback(self, path: str, version: int, context=None) -> None:
                pass

            def diff_versions(self, path: str, v1: int, v2: int, mode: str = "metadata"):
                return {} if mode == "metadata" else ""

            def workspace_snapshot(
                self, agent_id: str | None = None, description: str | None = None, tags=None
            ):
                return {}

            def workspace_restore(self, snapshot_number: int, agent_id: str | None = None):
                return {}

            def workspace_log(self, agent_id: str | None = None, limit: int = 100):
                return []

            def workspace_diff(self, snapshot_1: int, snapshot_2: int, agent_id: str | None = None):
                return {}

            def register_workspace(
                self, path, name=None, description=None, created_by=None, tags=None, metadata=None
            ):
                return {}

            def unregister_workspace(self, path):
                return True

            def list_workspaces(self):
                return []

            def get_workspace_info(self, path):
                return None

            def register_memory(
                self, path, name=None, description=None, created_by=None, tags=None, metadata=None
            ):
                return {}

            def unregister_memory(self, path):
                return True

            def list_memories(self):
                return []

            def get_memory_info(self, path):
                return None

            def append(self, path, content, context=None, if_match=None, force=False):
                return {}

            def sandbox_create(
                self, name, ttl_minutes=10, provider="e2b", template_id=None, context=None
            ):
                return {}

            def sandbox_get_or_create(
                self,
                name,
                ttl_minutes=10,
                provider=None,
                template_id=None,
                verify_status=True,
                context=None,
            ):
                return {}

            def sandbox_run(self, sandbox_id, language, code, timeout=30, context=None):
                return {}

            def sandbox_pause(self, sandbox_id, context=None):
                return {}

            def sandbox_resume(self, sandbox_id, context=None):
                return {}

            def sandbox_stop(self, sandbox_id, context=None):
                return {}

            def sandbox_list(
                self, context=None, verify_status=False, user_id=None, tenant_id=None, agent_id=None
            ):
                return {}

            def sandbox_status(self, sandbox_id, context=None):
                return {}

            def sandbox_connect(
                self, sandbox_id, source_path, mount_path, read_only=False, context=None
            ):
                return {}

            def sandbox_disconnect(self, sandbox_id, mount_path, context=None):
                return {}

            def add_mount(
                self, mount_point, backend_type, backend_config, priority=0, readonly=False
            ):
                return "mount_id"

            def remove_mount(self, mount_point):
                return True

            def list_mounts(self):
                return []

            def get_mount(self, mount_point):
                return None

            def close(self) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

        mock = MockFilesystem()
        assert isinstance(mock, NexusFilesystemProtocol)


def test_protocol_documentation() -> None:
    """Verify Protocol has proper documentation and warnings."""
    doc = NexusFilesystemProtocol.__doc__

    assert doc is not None, "Protocol should have docstring"
    assert "sync" in doc.lower(), "Documentation should mention synchronization requirement"
    assert "abc" in doc.lower(), "Documentation should reference the ABC"


if __name__ == "__main__":
    # Allow running this test file directly for quick verification
    pytest.main([__file__, "-v"])
