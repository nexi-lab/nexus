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
# Issue #3712: auto-rebuild stale nexus_kernel binary before test runs.
# Activated only when NEXUS_RUST_EDITABLE=1 (opt-in for local dev).
# CI pre-builds the binary from source, so the hook is not needed there.
# ---------------------------------------------------------------------------
if os.environ.get("NEXUS_RUST_EDITABLE") == "1":
    try:
        import maturin_import_hook

        maturin_import_hook.install()
    except ImportError:
        pass  # maturin-import-hook not installed — skip (warn below)

# ---------------------------------------------------------------------------
# Issue #3399: default to sync write observer in tests.
# The piped observer spawns a background consumer task per NexusFS instance;
# with 10K+ tests this adds significant startup/shutdown overhead and can
# cause CI timeouts.  Tests that specifically need the piped observer create
# it directly (e.g. test_piped_write_observer_flush.py).
# ---------------------------------------------------------------------------
os.environ.setdefault("NEXUS_ENABLE_WRITE_BUFFER", "false")

# ---------------------------------------------------------------------------
# OAuthCrypto: allow ephemeral keys in the test suite.
# Tests do not persist secrets across process restarts, so the production
# fail-loud default (which prevents silent data loss on the next boot) is
# overly strict for test fixtures that call ``OAuthCrypto()`` with no
# wired settings_store or explicit key. Tests that specifically exercise
# the fail-loud contract use ``monkeypatch.delenv`` to remove this flag.
# See ``tests/unit/lib/oauth/test_crypto_fail_loud.py``.
# ---------------------------------------------------------------------------
os.environ.setdefault("NEXUS_ALLOW_EPHEMERAL_OAUTH_KEY", "1")

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

    # sandbox_memory tests are skipped by default (RSS sampling is flaky on
    # shared CI runners).  Only run when explicitly selected: -m sandbox_memory
    marker_expr = config.option.markexpr if hasattr(config.option, "markexpr") else ""
    if "sandbox_memory" not in marker_expr:
        skip_mem = pytest.mark.skip(reason="SANDBOX memory benchmark: run with -m sandbox_memory")
        for item in items:
            if "sandbox_memory" in item.keywords:
                item.add_marker(skip_mem)


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


async def make_test_nexus(
    tmp_path,
    *,
    backend=None,
    permissions=None,
    parsing=None,
    cache=None,
    memory=None,
    distributed=None,
    is_admin=False,
    record_store=None,
    use_raft=False,
    metadata_store=None,
    context=None,
):
    """Create a NexusFS instance for testing via factory (Issue #1801).

    Uses ``create_nexus_fs()`` — the same boot path as production.
    Defaults: permissions off, no auto-parse, no distributed features,
    no bricks (SLIM profile).

    Args:
        tmp_path: pytest tmp_path fixture for backend/metadata storage.
        backend: Backend to mount at ``/``. Default: PathLocalBackend(tmp_path / "data").
        permissions: PermissionConfig override. Default: enforce=False.
        parsing: ParseConfig override. Default: auto_parse=False.
        cache: CacheConfig override.
        memory: MemoryConfig override.
        distributed: DistributedConfig override. Default: all disabled.
        is_admin: Admin flag for init_cred.
        record_store: Optional RecordStoreABC.
        use_raft: Use RaftMetadataStore (requires Python 3.13).
        metadata_store: Override metadata store. Default: DictMetastore or Raft.
        context: Override init_cred identity.

    Returns:
        NexusFS instance ready for testing.
    """
    from nexus.core.config import (
        DistributedConfig,
        ParseConfig,
        PermissionConfig,
    )
    from nexus.factory import create_nexus_fs

    if permissions is None:
        permissions = PermissionConfig(enforce=False)
    if parsing is None:
        parsing = ParseConfig(auto_parse=False)
    if distributed is None:
        distributed = DistributedConfig(
            enable_events=False,
            enable_workflows=False,
        )

    if metadata_store is None:
        # F2 C4 routes writes through ``kernel.sys_write``; DictMetastore is a
        # Python-only dict so the kernel can't persist through it. Default to
        # the same path production uses — RustMetastoreProxy wired to a fresh
        # kernel + redb file under tmp_path. ``use_raft`` is now redundant
        # but kept for API compatibility.
        del use_raft
        from nexus_kernel import Kernel as _Kernel

        from nexus.core.metastore import RustMetastoreProxy

        _kernel = _Kernel()
        metadata_store = RustMetastoreProxy(_kernel, str(tmp_path / "metastore.redb"))

    if backend is None:
        from pathlib import Path

        from nexus.backends.storage.path_local import PathLocalBackend

        data_dir = Path(tmp_path) / "data"
        data_dir.mkdir(exist_ok=True)
        backend = PathLocalBackend(root_path=str(data_dir))

    # Issue #1801: unified boot path — all NexusFS goes through factory
    from tests.helpers.test_context import TEST_ADMIN_CONTEXT, TEST_CONTEXT

    _init_cred = (
        context if context is not None else (TEST_ADMIN_CONTEXT if is_admin else TEST_CONTEXT)
    )

    return await create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=permissions,
        parsing=parsing,
        cache=cache,
        memory=memory,
        distributed=distributed,
        is_admin=is_admin,
        enabled_bricks=frozenset(),  # SLIM profile for fast tests
        init_cred=_init_cred,
    )


@pytest.fixture(autouse=True)
def _reset_auth_cache_fixture():
    """No-op: auth cache is now CacheStoreABC-based (instance-level, not module-level).

    Tests that need auth caching create their own InMemoryCacheStore,
    so no global state needs resetting.
    """
    yield


@pytest.fixture(autouse=True)
def _reset_stream_secret_fixture():
    """Reset the HMAC stream signing secret between tests for isolation."""
    yield
    try:
        from nexus.server.streaming import _reset_stream_secret

        _reset_stream_secret()
    except ImportError:
        pass
