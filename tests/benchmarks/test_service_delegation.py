"""Benchmark tests for service delegation overhead (Issue #1287).

Measures the cost of NexusFS → service delegation patterns:
- Direct vs delegated file operations
- Gateway method delegation overhead
- Service instantiation time
- Parameter transformation cost (zone_id → _zone_id renaming)
- Result wrapping cost (SkillService dict construction)

Run with: pytest tests/benchmarks/test_service_delegation.py -v --benchmark-only
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.types import OperationContext
from nexus.core.nexus_fs import NexusFS
from nexus.services.gateway import NexusFSGateway

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_nexus_fs():
    """Create NexusFS with mock services for delegation benchmarks.

    Bypasses __init__ so no Raft is required. Services return
    pre-defined values to isolate delegation overhead.
    """
    fs = object.__new__(NexusFS)
    fs.version_service = MagicMock()
    fs.version_service.get_version = AsyncMock(return_value=b"benchmark")
    fs.version_service.list_versions = AsyncMock(return_value=[{"v": 1}])
    fs.version_service.diff_versions = AsyncMock(return_value={"changed": False})
    fs.rebac_service = MagicMock()
    fs.rebac_service.rebac_check = AsyncMock(return_value=True)
    fs.rebac_service.rebac_create = AsyncMock(return_value={"tuple_id": "t1"})
    fs.rebac_service.rebac_list_tuples = AsyncMock(return_value=[])
    fs.rebac_service.rebac_expand = AsyncMock(return_value=[])
    fs.mcp_service = MagicMock()
    fs.mcp_service.mcp_list_mounts = AsyncMock(return_value=[])
    fs.skill_service = MagicMock()
    fs.skill_service.share = MagicMock(return_value="tuple-abc")
    fs.skill_service.discover = MagicMock(return_value=[])
    fs.skill_service.get_prompt_context = MagicMock(
        return_value=MagicMock(to_dict=MagicMock(return_value={}))
    )
    fs.llm_service = MagicMock()
    fs.llm_service.create_llm_reader = MagicMock()
    fs.oauth_service = MagicMock()
    fs.oauth_service.oauth_list_providers = AsyncMock(return_value=[])
    fs.search_service = MagicMock()
    fs.search_service.list = MagicMock(return_value=[])
    fs.search_service.glob = MagicMock(return_value=[])
    fs.search_service.grep = MagicMock(return_value=[])
    fs.search_service.semantic_search = AsyncMock(return_value=[])
    fs.share_link_service = MagicMock()
    fs.share_link_service.create_share_link = AsyncMock(return_value=MagicMock())
    fs.mount_service = MagicMock()
    fs.mount_service.list_mounts = AsyncMock(return_value=[])
    return fs


@pytest.fixture
def mock_gateway():
    """Create a NexusFSGateway with mock NexusFS for gateway benchmarks."""
    mock_fs = MagicMock()
    mock_fs.sys_read = MagicMock(return_value=b"data")
    mock_fs.sys_write = MagicMock()
    mock_fs.sys_mkdir = MagicMock()
    mock_fs.sys_readdir = MagicMock(return_value=["a.txt", "b.txt"])
    mock_fs.sys_access = MagicMock(return_value=True)
    mock_fs.metadata = MagicMock()
    mock_fs.metadata.get = MagicMock(return_value=MagicMock())
    mock_fs.metadata.list = MagicMock(return_value=[])
    mock_fs.rebac_check = MagicMock(return_value=True)
    mock_fs.rebac_create = MagicMock(return_value={"tuple_id": "t1"})
    mock_fs.rebac_list_tuples = MagicMock(return_value=[])
    mock_fs._rebac_manager = MagicMock()
    mock_fs._hierarchy_manager = MagicMock()
    mock_fs._hierarchy_manager.enable_inheritance = True
    mock_fs.router = MagicMock()
    mock_fs.backend = MagicMock()
    mock_fs._get_routing_params = MagicMock(return_value=("z1", "a1", False))
    mock_fs._has_descendant_access = MagicMock(return_value=True)
    mock_fs._get_backend_directory_entries = MagicMock(return_value=set())
    mock_fs._record_read_if_tracking = MagicMock()
    mock_fs.read_bulk = MagicMock(return_value={})
    return NexusFSGateway(mock_fs)


@pytest.fixture
def context():
    """Standard operation context for benchmarks."""
    return OperationContext(
        user_id="bench_user",
        groups=["bench_group"],
        zone_id="bench_zone",
        is_system=False,
        is_admin=False,
    )


# =============================================================================
# NexusFS → Service Async Delegation Overhead
# =============================================================================


@pytest.mark.benchmark_ci
class TestAsyncDelegationOverhead:
    """Benchmark async delegation: NexusFS method → await service.method().

    Measures the cost of the delegation wrapper around an async mock.
    """

    def test_version_get_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark version_service.get_version (direct brick-source call)."""

        def run():
            asyncio.run(mock_nexus_fs.version_service.get_version("/file.txt", 1, context))

        benchmark(run)

    def test_rebac_check_delegation(self, benchmark, mock_nexus_fs):
        """Benchmark arebac_check delegation overhead."""

        def run():
            asyncio.run(
                mock_nexus_fs.arebac_check(
                    subject=("user", "alice"),
                    permission="read",
                    object=("file", "/doc.txt"),
                    zone_id="z1",
                )
            )

        benchmark(run)

    def test_rebac_list_tuples_with_param_rename(self, benchmark, mock_nexus_fs):
        """Benchmark arebac_list_tuples with zone_id→_zone_id transformation."""

        def run():
            asyncio.run(
                mock_nexus_fs.arebac_list_tuples(
                    subject=("user", "alice"),
                    zone_id="z1",
                    limit=100,
                    offset=0,
                )
            )

        benchmark(run)

    def test_mcp_list_mounts_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark mcp_list_mounts via mcp_service direct call."""

        def run():
            asyncio.run(mock_nexus_fs.mcp_service.mcp_list_mounts(_context=context))

        benchmark(run)

    def test_oauth_list_providers_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark oauth_list_providers via oauth_service direct call."""

        def run():
            asyncio.run(mock_nexus_fs.oauth_service.oauth_list_providers(_context=context))

        benchmark(run)


# =============================================================================
# NexusFS → Service Sync Delegation Overhead
# =============================================================================


@pytest.mark.benchmark_ci
class TestSyncDelegationOverhead:
    """Benchmark sync delegation: NexusFS method → service.method().

    Measures pure Python call overhead for sync delegation.
    """

    def test_skills_share_via_brick_service(self, benchmark, mock_nexus_fs, context):
        """Benchmark skills_share: brick service RPC method."""
        mock_nexus_fs.skill_service.rpc_share = MagicMock(
            return_value={"success": True, "tuple_id": "t-1"}
        )
        benchmark(
            mock_nexus_fs.skill_service.rpc_share,
            "/skills/test.py",
            "user:bob",
            context,
        )

    def test_skills_discover_via_brick_service(self, benchmark, mock_nexus_fs, context):
        """Benchmark skills_discover: brick service RPC method."""
        mock_nexus_fs.skill_service.rpc_discover = MagicMock(
            return_value={"skills": [], "count": 0}
        )
        benchmark(mock_nexus_fs.skill_service.rpc_discover, "all", context)

    def test_skills_get_prompt_context_via_brick_service(self, benchmark, mock_nexus_fs, context):
        """Benchmark skills_get_prompt_context: brick service RPC method."""
        mock_nexus_fs.skill_service.rpc_get_prompt_context = MagicMock(
            return_value={"skills": [], "count": 0}
        )
        benchmark(mock_nexus_fs.skill_service.rpc_get_prompt_context, 50, context)

    def test_search_list_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark list() delegation to SearchService."""
        benchmark(
            mock_nexus_fs.list,
            "/data",
            True,
            False,
            None,
            True,
            context,
        )

    def test_search_glob_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark glob() delegation to SearchService."""
        benchmark(mock_nexus_fs.glob, "*.py", "/src", context)

    def test_search_grep_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark grep() delegation to SearchService."""
        benchmark(
            mock_nexus_fs.grep,
            "import os",
            "/src",
            None,
            False,
            100,
            "auto",
            context,
        )

    def test_create_llm_reader_delegation(self, benchmark, mock_nexus_fs):
        """Benchmark create_llm_reader sync delegation via llm_service."""
        benchmark(mock_nexus_fs.llm_service.create_llm_reader)


# =============================================================================
# Gateway Delegation Overhead
# =============================================================================


@pytest.mark.benchmark_ci
class TestGatewayDelegationOverhead:
    """Benchmark NexusFSGateway method delegation to NexusFS.

    Measures the gateway facade's overhead for common operations.
    """

    def test_gateway_read(self, benchmark, mock_gateway, context):
        """Benchmark gateway.sys_read() delegation."""
        benchmark(mock_gateway.sys_read, "/test/file.txt", context=context)

    def test_gateway_write_bytes(self, benchmark, mock_gateway, context):
        """Benchmark gateway.sys_write() delegation with bytes."""
        benchmark(mock_gateway.sys_write, "/test/file.txt", b"content", context=context)

    def test_gateway_write_str_conversion(self, benchmark, mock_gateway, context):
        """Benchmark gateway.sys_write() with str→bytes conversion."""
        benchmark(mock_gateway.sys_write, "/test/file.txt", "text content", context=context)

    def test_gateway_exists(self, benchmark, mock_gateway, context):
        """Benchmark gateway.sys_access() delegation."""
        benchmark(mock_gateway.sys_access, "/test/file.txt", context=context)

    def test_gateway_list(self, benchmark, mock_gateway, context):
        """Benchmark gateway.sys_readdir() delegation."""
        benchmark(mock_gateway.sys_readdir, "/test", context=context)

    def test_gateway_metadata_get(self, benchmark, mock_gateway):
        """Benchmark gateway.metadata_get() delegation."""
        benchmark(mock_gateway.metadata_get, "/test/file.txt")

    def test_gateway_rebac_check(self, benchmark, mock_gateway):
        """Benchmark gateway.rebac_check() delegation."""
        benchmark(
            mock_gateway.rebac_check,
            subject=("user", "alice"),
            permission="read",
            object=("file", "/test"),
            zone_id="z1",
        )


# =============================================================================
# Service Instantiation
# =============================================================================


class TestServiceInstantiation:
    """Benchmark service construction time."""

    def test_gateway_construction(self, benchmark):
        """Benchmark NexusFSGateway construction."""
        mock_fs = MagicMock()

        benchmark(NexusFSGateway, mock_fs)

    def test_share_link_service_construction(self, benchmark):
        """Benchmark ShareLinkService construction."""
        from nexus.services.share_link.share_link_service import ShareLinkService

        mock_gw = MagicMock()
        benchmark(ShareLinkService, gateway=mock_gw, enforce_permissions=True)

    def test_events_service_construction(self, benchmark):
        """Benchmark EventsService construction."""
        from nexus.system_services.lifecycle.events_service import EventsService

        mock_backend = MagicMock()
        mock_backend.is_passthrough = False
        benchmark(EventsService, backend=mock_backend)

    def test_version_service_construction(self, benchmark):
        """Benchmark VersionService construction."""
        from nexus.services.versioning.version_service import VersionService

        mock_metadata = MagicMock()
        mock_cas = MagicMock()
        benchmark(
            VersionService,
            metadata_store=mock_metadata,
            cas_store=mock_cas,
            enforce_permissions=False,
        )


# =============================================================================
# Context Extraction
# =============================================================================


class TestContextExtractionOverhead:
    """Benchmark context extraction helpers used in delegation."""

    def test_extract_context_info(self, benchmark, context):
        """Benchmark ShareLinkService._extract_context_info."""
        from nexus.services.share_link.share_link_service import ShareLinkService

        benchmark(ShareLinkService._extract_context_info, context)

    def test_extract_context_info_none(self, benchmark):
        """Benchmark _extract_context_info with None context."""
        from nexus.services.share_link.share_link_service import ShareLinkService

        benchmark(ShareLinkService._extract_context_info, None)

    def test_operation_context_construction(self, benchmark):
        """Benchmark OperationContext creation."""
        benchmark(
            OperationContext,
            user_id="bench_user",
            groups=["group1", "group2"],
            zone_id="zone1",
            is_system=False,
            is_admin=False,
        )
