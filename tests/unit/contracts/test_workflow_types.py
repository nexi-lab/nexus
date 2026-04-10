"""Protocol conformance tests for workflow contracts (Issue #2137)."""

from typing import Any

from nexus.contracts.workflow_types import MetadataStoreProtocol, NexusOperationsProtocol


class _FakeNexusOps:
    """Minimal duck-typed implementation for conformance check."""

    async def parse(self, path: str, *, parser: str = "auto") -> Any:
        return {"parsed": True}

    async def add_tag(self, path: str, tag: str) -> None:
        pass

    async def remove_tag(self, path: str, tag: str) -> None:
        pass

    async def rename(self, old_path: str, new_path: str) -> None:
        pass

    def mkdir(self, path: str, *, parents: bool = False) -> None:
        pass

    async def read(self, path: str) -> bytes:
        return b""


class _FakeMetadataStore:
    """Minimal duck-typed implementation for conformance check."""

    async def get_path(self, path: str) -> Any:
        return None

    async def set_file_metadata(self, path_id: Any, key: str, value: str) -> None:
        pass


class TestNexusOperationsConformance:
    def test_runtime_checkable(self) -> None:
        ops = _FakeNexusOps()
        assert isinstance(ops, NexusOperationsProtocol)

    def test_non_conforming_rejected(self) -> None:
        class _Incomplete:
            pass

        assert not isinstance(_Incomplete(), NexusOperationsProtocol)


class TestMetadataStoreConformance:
    def test_runtime_checkable(self) -> None:
        store = _FakeMetadataStore()
        assert isinstance(store, MetadataStoreProtocol)

    def test_non_conforming_rejected(self) -> None:
        class _Incomplete:
            pass

        assert not isinstance(_Incomplete(), MetadataStoreProtocol)


class TestReExportPaths:
    """Ensure protocols are importable from all expected paths."""

    def test_import_from_contracts(self) -> None:
        from nexus.contracts.workflow_types import (
            MetadataStoreProtocol as P1,
        )
        from nexus.contracts.workflow_types import (
            NexusOperationsProtocol as P2,
        )

        assert P1 is MetadataStoreProtocol
        assert P2 is NexusOperationsProtocol

    def test_import_from_services_protocols(self) -> None:
        """services/protocols/workflow re-exports from contracts."""
        from nexus.contracts.protocols.workflow import (
            MetadataStoreProtocol as P1,
        )
        from nexus.contracts.protocols.workflow import (
            NexusOperationsProtocol as P2,
        )

        assert P1 is MetadataStoreProtocol
        assert P2 is NexusOperationsProtocol

    def test_import_from_bricks_workflows(self) -> None:
        """bricks/workflows/protocol re-exports from contracts."""
        from nexus.bricks.workflows.protocol import (
            MetadataStoreProtocol as P1,
        )
        from nexus.bricks.workflows.protocol import (
            NexusOperationsProtocol as P2,
        )

        assert P1 is MetadataStoreProtocol
        assert P2 is NexusOperationsProtocol
