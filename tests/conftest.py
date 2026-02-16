"""Root test configuration.

Provides quarantine marker handling for flaky/timing-dependent tests.
Quarantined tests are skipped by default; pass --run-quarantine to include them.

Hypothesis profiles (Issue #1303):
  - dev:      10 examples, 500ms deadline — fast local iteration
  - ci:       1000 examples, no deadline, derandomize — thorough PR checks
  - thorough: 100K examples, no deadline — periodic full proofs (nightly/weekly)

Usage:
  HYPOTHESIS_PROFILE=ci pytest ...
  pytest --hypothesis-profile ci ...
"""

import os

import pytest

# ---------------------------------------------------------------------------
# Hypothesis profiles (Issue #1303)
# ---------------------------------------------------------------------------

try:
    from hypothesis import HealthCheck, Phase
    from hypothesis import settings as hypothesis_settings

    hypothesis_settings.register_profile(
        "dev",
        max_examples=10,
        deadline=500,
    )

    hypothesis_settings.register_profile(
        "ci",
        max_examples=1000,
        deadline=None,
        derandomize=True,
        print_blob=True,
        suppress_health_check=[HealthCheck.too_slow],
    )

    hypothesis_settings.register_profile(
        "thorough",
        max_examples=100_000,
        deadline=None,
        derandomize=True,
        print_blob=True,
        suppress_health_check=[HealthCheck.too_slow],
        phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink],
    )

    hypothesis_settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))
except ImportError:
    pass  # hypothesis not installed — property-based tests will be skipped

try:
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:
    _HAS_STRUCTLOG = False


def pytest_addoption(parser):
    parser.addoption(
        "--run-quarantine",
        action="store_true",
        default=False,
        help="Run quarantined flaky tests",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-quarantine"):
        skip_quarantine = pytest.mark.skip(reason="Quarantined: use --run-quarantine to run")
        for item in items:
            if "quarantine" in item.keywords:
                item.add_marker(skip_quarantine)


# ---------------------------------------------------------------------------
# Autouse fixtures for test isolation (reset module-level singletons)
# ---------------------------------------------------------------------------


if _HAS_STRUCTLOG:

    @pytest.fixture(autouse=True)
    def _reset_structlog_context():
        """Reset structlog contextvars between tests for isolation."""
        structlog.contextvars.clear_contextvars()
        yield
        structlog.contextvars.clear_contextvars()


def make_test_nexus(
    tmp_path,
    *,
    permissions=None,
    parsing=None,
    cache=None,
    memory=None,
    distributed=None,
    services=None,
    is_admin=False,
    record_store=None,
    use_raft=False,
    backend=None,
    metadata_store=None,
):
    """Create a NexusFS instance for testing with sensible defaults.

    Defaults: permissions off, no auto-parse, no distributed features.
    Avoids heavy I/O (event bus, lock manager, workflows) for fast tests.

    Args:
        tmp_path: pytest tmp_path fixture for backend/metadata storage.
        permissions: PermissionConfig override. Default: enforce=False.
        parsing: ParseConfig override. Default: auto_parse=False.
        cache: CacheConfig override.
        memory: MemoryConfig override.
        distributed: DistributedConfig override. Default: all disabled.
        services: KernelServices override.
        is_admin: Admin flag.
        record_store: Optional RecordStoreABC.
        use_raft: Use RaftMetadataStore (requires Python 3.13).
        backend: Override backend. Default: LocalBackend(tmp_path / "data").
        metadata_store: Override metadata store. Default: InMemory or Raft.

    Returns:
        NexusFS instance ready for testing.
    """
    from nexus.core.config import (
        DistributedConfig,
        ParseConfig,
        PermissionConfig,
    )
    from nexus.core.nexus_fs import NexusFS

    if permissions is None:
        permissions = PermissionConfig(enforce=False, audit_strict_mode=False)
    if parsing is None:
        parsing = ParseConfig(auto_parse=False)
    if distributed is None:
        distributed = DistributedConfig(
            enable_events=False,
            enable_locks=False,
            enable_workflows=False,
        )

    if backend is None:
        from nexus.backends.local import LocalBackend

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        backend = LocalBackend(root_path=data_dir)

    if metadata_store is None:
        if use_raft:
            from nexus.storage.raft_metadata_store import RaftMetadataStore

            metadata_store = RaftMetadataStore.embedded(str(tmp_path / "raft"))
        else:
            from tests.helpers.in_memory_metadata_store import InMemoryFileMetadataStore

            metadata_store = InMemoryFileMetadataStore()

    return NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        is_admin=is_admin,
        permissions=permissions,
        parsing=parsing,
        cache=cache,
        memory=memory,
        distributed=distributed,
        services=services,
    )


@pytest.fixture(autouse=True)
def _reset_auth_cache_fixture():
    """Reset the TTLCache auth cache between tests for isolation."""
    yield
    try:
        from nexus.server.dependencies import _reset_auth_cache

        _reset_auth_cache()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _reset_stream_secret_fixture():
    """Reset the HMAC stream signing secret between tests for isolation."""
    yield
    try:
        from nexus.server.streaming import _reset_stream_secret

        _reset_stream_secret()
    except ImportError:
        pass
