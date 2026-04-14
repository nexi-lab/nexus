"""Benchmark tests for service delegation overhead (Issue #1287).

Measures the cost of NexusFS -> service delegation patterns:
- Direct vs delegated file operations
- Gateway method delegation overhead
- Service instantiation time
- Parameter transformation cost (zone_id -> _zone_id renaming)
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
def delegation_loop():
    """Dedicated event loop for service delegation benchmarks."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_nexus_fs():
    """Create NexusFS with mock services for delegation benchmarks.

    Bypasses __init__ so no Raft is required. Services return
    pre-defined values to isolate delegation overhead.
    """
    fs = object.__new__(NexusFS)
    fs.metadata = MagicMock()
    fs.metadata.list = MagicMock(return_value=[])
    fs.version_service = MagicMock()
    fs.version_service.get_version = AsyncMock(return_value=b"benchmark")
    fs.version_service.list_versions = AsyncMock(return_value=[{"v": 1}])
    fs.version_service.diff_versions = AsyncMock(return_value={"changed": False})
    mock_rebac_svc = MagicMock()
    mock_rebac_svc.rebac_check = AsyncMock(return_value=True)
    mock_rebac_svc.rebac_create = AsyncMock(return_value={"tuple_id": "t1"})
    mock_rebac_svc.rebac_list_tuples = AsyncMock(return_value=[])
    mock_rebac_svc.rebac_expand = AsyncMock(return_value=[])
    mock_mcp_svc = MagicMock()
    mock_mcp_svc.mcp_list_mounts = AsyncMock(return_value=[])
    fs.skill_service = MagicMock()
    fs.skill_service.share = MagicMock(return_value="tuple-abc")
    fs.skill_service.discover = MagicMock(return_value=[])
    fs.skill_service.get_prompt_context = MagicMock(
        return_value=MagicMock(to_dict=MagicMock(return_value={}))
    )
    mock_oauth_svc = MagicMock()
    mock_oauth_svc.oauth_list_providers = AsyncMock(return_value=[])
    mock_search_svc = MagicMock()
    mock_search_svc.list = MagicMock(return_value=[])
    mock_search_svc.glob = MagicMock(return_value=[])
    mock_search_svc.grep = MagicMock(return_value=[])
    mock_search_svc.semantic_search = AsyncMock(return_value=[])
    mock_share_link_svc = MagicMock()
    mock_share_link_svc.create_share_link = AsyncMock(return_value=MagicMock())
    mock_mount_svc = MagicMock()
    mock_mount_svc.list_mounts = AsyncMock(return_value=[])
    fs._kernel = MagicMock()
    fs._kernel.service_lookup = MagicMock(
        side_effect=lambda name: {
            "rebac": mock_rebac_svc,
            "mcp": mock_mcp_svc,
            "oauth": mock_oauth_svc,
            "search": mock_search_svc,
            "share_link": mock_share_link_svc,
            "mount": mock_mount_svc,
        }.get(name)
    )
    fs._hook_specs = {}
    return fs


@pytest.fixture
def mock_gateway():
    """Create a NexusFSGateway with mock NexusFS for gateway benchmarks.

    Gateway sys_* methods are async and await the underlying NexusFS methods,
    so the mock NexusFS methods must be AsyncMock.
    """
    mock_fs = MagicMock()
    mock_fs.sys_read = AsyncMock(return_value=b"data")
    mock_fs.sys_write = AsyncMock()
    mock_fs.write = AsyncMock()
    mock_fs.mkdir = AsyncMock()
    mock_fs.sys_readdir = MagicMock(return_value=["a.txt", "b.txt"])
    mock_fs.access = AsyncMock(return_value=True)
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
    mock_fs._get_context_identity = MagicMock(return_value=("z1", "a1", False))
    mock_fs._has_descendant_access = MagicMock(return_value=True)
    mock_fs._get_backend_directory_entries = MagicMock(return_value=set())
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
# NexusFS -> Service Async Delegation Overhead
# =============================================================================


@pytest.mark.benchmark_ci
class TestAsyncDelegationOverhead:
    """Benchmark async delegation: NexusFS method -> await service.method().

    Measures the cost of the delegation wrapper around an async mock.
    """

    def test_version_get_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark version_service.get_version (direct brick-source call)."""

        def run():
            asyncio.run(mock_nexus_fs.version_service.get_version("/file.txt", 1, context))

        benchmark(run)

    def test_rebac_check_delegation(self, benchmark, mock_nexus_fs):
        """Benchmark rebac_check via rebac_service direct call."""

        def run():
            asyncio.run(
                mock_nexus_fs.service("rebac").rebac_check(
                    subject=("user", "alice"),
                    permission="read",
                    object=("file", "/doc.txt"),
                    zone_id="z1",
                )
            )

        benchmark(run)

    def test_rebac_list_tuples_with_param_rename(self, benchmark, mock_nexus_fs):
        """Benchmark rebac_list_tuples via rebac_service direct call."""

        def run():
            asyncio.run(
                mock_nexus_fs.service("rebac").rebac_list_tuples(
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
            asyncio.run(mock_nexus_fs.service("mcp").mcp_list_mounts(_context=context))

        benchmark(run)

    def test_oauth_list_providers_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark oauth_list_providers via oauth_service direct call."""

        def run():
            asyncio.run(mock_nexus_fs.service("oauth").oauth_list_providers(_context=context))

        benchmark(run)


# =============================================================================
# NexusFS -> Service Sync Delegation Overhead
# =============================================================================


@pytest.mark.benchmark_ci
class TestSyncDelegationOverhead:
    """Benchmark sync delegation: NexusFS method -> service.method().

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

    def test_search_list_delegation(self, benchmark, mock_nexus_fs, context, delegation_loop):
        """Benchmark sys_readdir() delegation to SearchService."""

        def run():
            mock_nexus_fs.sys_readdir(
                path="/data",
                recursive=True,
                details=False,
                show_parsed=True,
                context=context,
            )

        benchmark(run)

    def test_search_glob_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark glob() delegation to SearchService."""
        benchmark(mock_nexus_fs.service("search").glob, "*.py", "/src", context)

    def test_search_grep_delegation(self, benchmark, mock_nexus_fs, context):
        """Benchmark grep() delegation to SearchService."""
        benchmark(
            mock_nexus_fs.service("search").grep,
            "import os",
            "/src",
            None,
            False,
            100,
            "auto",
            context,
        )


# =============================================================================
# Gateway Delegation Overhead
# =============================================================================


@pytest.mark.benchmark_ci
class TestGatewayDelegationOverhead:
    """Benchmark NexusFSGateway method delegation to NexusFS.

    Gateway sys_* methods are async, so we use a dedicated event loop
    with run_until_complete() to drive them from sync benchmark functions.
    """

    def test_gateway_read(self, benchmark, mock_gateway, context, delegation_loop):
        """Benchmark gateway.sys_read() delegation."""

        def run():
            delegation_loop.run_until_complete(
                mock_gateway.sys_read("/test/file.txt", context=context)
            )

        benchmark(run)

    def test_gateway_write_bytes(self, benchmark, mock_gateway, context, delegation_loop):
        """Benchmark gateway.write() delegation with bytes."""

        def run():
            delegation_loop.run_until_complete(
                mock_gateway.write("/test/file.txt", b"content", context=context)
            )

        benchmark(run)

    def test_gateway_write_str_conversion(self, benchmark, mock_gateway, context, delegation_loop):
        """Benchmark gateway.write() with str->bytes conversion."""

        def run():
            delegation_loop.run_until_complete(
                mock_gateway.write("/test/file.txt", "text content", context=context)
            )

        benchmark(run)

    def test_gateway_exists(self, benchmark, mock_gateway, context, delegation_loop):
        """Benchmark gateway.access() delegation."""

        def run():
            delegation_loop.run_until_complete(
                mock_gateway.access("/test/file.txt", context=context)
            )

        benchmark(run)

    def test_gateway_list(self, benchmark, mock_gateway, context, delegation_loop):
        """Benchmark gateway.sys_readdir() delegation."""

        def run():
            mock_gateway.sys_readdir("/test", context=context)

        benchmark(run)

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
        from nexus.bricks.share_link.share_link_service import ShareLinkService

        mock_gw = MagicMock()
        benchmark(ShareLinkService, gateway=mock_gw, enforce_permissions=True)

    def test_version_service_construction(self, benchmark):
        """Benchmark VersionService construction."""
        from nexus.bricks.versioning.version_service import VersionService

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
        from nexus.bricks.share_link.share_link_service import ShareLinkService

        benchmark(ShareLinkService._extract_context_info, context)

    def test_extract_context_info_none(self, benchmark):
        """Benchmark _extract_context_info with None context."""
        from nexus.bricks.share_link.share_link_service import ShareLinkService

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
