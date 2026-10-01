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
import sys
from pathlib import Path

import pytest

# Ensure local src is in path for worktree development
_src_path = Path(__file__).parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))

# ---------------------------------------------------------------------------
# Issue #3712: auto-rebuild stale nexus_runtime binary before test runs.
# Activated only when NEXUS_RUST_EDITABLE=1 (opt-in for local dev).
# CI pre-builds the binary from source, so the hook is not needed there.
# ---------------------------------------------------------------------------
if os.environ.get("NEXUS_RUST_EDITABLE") == "1":
    try:
        pass  # maturin-import-hook removed

        # kernel runs as nexus-cluster binary
    except ImportError:
        pass  # maturin-import-hook not installed — skip (warn below)

# ---------------------------------------------------------------------------
# #4738: run the suite against the production-default write observer.
# Issue #3399 forced the synchronous observer here because the debounced
# piped observer spawned a background consumer per NexusFS instance and made
# projection rows visible only after a timer.  The write-through observer
# commits before a write returns and starts its two daemon threads lazily,
# so tests see the same observer production runs.  Set
# NEXUS_ENABLE_WRITE_BUFFER=false to exercise the legacy synchronous one.
# ---------------------------------------------------------------------------
os.environ.setdefault("NEXUS_ENABLE_WRITE_BUFFER", "true")

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


def __getattr__(name: str):
    if name == "make_test_nexus":
        from tests.testkit import make_test_nexus

        return make_test_nexus
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        suppress_health_check=[HealthCheck.too_slow],
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


def pytest_configure(config):
    """Register custom markers.

    ``wedge_watchdog`` is our systematic answer to the "test hangs
    forever on a wedged filesystem syscall" failure class.  Any test
    that touches a live FUSE/NFS/kernel-fs mount can hit an infinite
    kernel-level block if the backend is stalled — Python cannot
    interrupt an in-progress ``stat()`` / ``open()`` syscall.  Without
    a watchdog the ONLY recovery is the wall-clock workflow timeout,
    which is silent, uninformative, and burns runner minutes.

    Usage:

        @pytest.mark.wedge_watchdog                 # default 60s
        def test_something(mount): ...

        @pytest.mark.wedge_watchdog(seconds=120)    # custom budget
        def test_slower(mount): ...

    Implementation: the ``pytest_collection_modifyitems`` hook below
    translates a ``wedge_watchdog`` marker into pytest-timeout's
    ``timeout`` marker with ``method="thread"``.  ``thread`` method
    calls ``pytest.exit`` from a watchdog thread that hard-fails the
    test with a Python-side traceback — visible in the CI log,
    unlike a workflow-level kill.  Tests can override the seconds
    kwarg per-test; sub-directories can auto-apply via a local
    ``conftest.py`` that adds the marker in
    ``pytest_collection_modifyitems``.
    """
    config.addinivalue_line(
        "markers",
        "wedge_watchdog(seconds=60): watchdog-timeout the test if it hangs "
        "on a kernel-level filesystem syscall.  Backed by pytest-timeout "
        "method=thread so wedged syscalls surface with diagnostic tracebacks "
        "instead of silent workflow-timeout kills.",
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

    # ``wedge_watchdog`` marker → pytest-timeout translation.
    #
    # We intentionally do NOT set a default timeout in pyproject
    # (see the "No per-test timeout" comment there — profile+optimize,
    # don't kill).  But fs-vulnerable tests have a unique failure mode
    # (kernel-level syscall hang) that profiling can't fix and only a
    # watchdog can surface.  Applying via a marker keeps the escape
    # hatch scoped: only tests that opt in get the watchdog.
    for item in items:
        watchdog = item.get_closest_marker("wedge_watchdog")
        if watchdog is None:
            continue
        seconds = watchdog.kwargs.get("seconds")
        if seconds is None and watchdog.args:
            seconds = watchdog.args[0]
        if seconds is None:
            seconds = 60  # sane default; individual tests can override
        # pytest-timeout method="thread" instead of the default signal
        # method: signal is SIGALRM which is unavailable in worker
        # threads (Python raises "signal only works in main thread"),
        # and pytest-xdist runs tests in threads by default.  Thread
        # method spawns a watchdog thread that calls pytest.exit —
        # cross-platform, xdist-safe, and produces a visible traceback.
        item.add_marker(pytest.mark.timeout(seconds, method="thread"))


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


# ---------------------------------------------------------------------------
# Issue #4667: name what refuses to die, at session end.
#
# The `Test Python 3.14 on ubuntu-latest` flake is a HANG, not a failure: a gw0
# worker crashes, the run reaches ~98%, and then nothing happens until the job
# timeout kills it. A rerun goes green, so the cause is never in the log — the
# issue says as much ("hasn't yet been captured in a durability report").
#
# Same reasoning as the `wedge_watchdog` marker above: a wall-clock timeout is
# silent and uninformative, so the answer is to make the process say what it is
# waiting on before the clock runs out. A leaked child process (#4777 left a
# ~30-thread `nexus-cluster` orphan per CLI run) or a live non-daemon thread is
# exactly what keeps a worker from exiting, and both are cheap to enumerate.
#
# This runs at `sessionfinish`, BEFORE interpreter shutdown, so the report lands
# in the log even when the shutdown itself is what hangs.
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 — pytest hook signature
    """Report child processes and non-daemon threads still alive.

    Always prints one line, including when clean. Silence would be
    indistinguishable from "the hook never ran", which is the mistake this kind of
    check exists to prevent.
    """
    import threading

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")

    lingering_threads = [
        t
        for t in threading.enumerate()
        if t is not threading.main_thread() and t.is_alive() and not t.daemon
    ]

    children: list[str] = []
    children_note = ""
    try:
        import psutil

        for child in psutil.Process().children(recursive=True):
            try:
                cmd = " ".join(child.cmdline())[:120] or child.name()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                cmd = "<unreadable>"
            children.append(f"pid={child.pid} {cmd}")
    except ImportError:
        children_note = " (psutil absent — child processes not checked)"
    except Exception as exc:  # a diagnostic must never fail the run
        children_note = f" (child scan failed: {exc})"

    if not lingering_threads and not children:
        print(f"[leak-check {worker}] clean{children_note}", file=sys.stderr)
        return

    print(f"[leak-check {worker}] SOMETHING IS STILL ALIVE{children_note}", file=sys.stderr)
    for t in lingering_threads:
        print(f"[leak-check {worker}]   non-daemon thread: {t.name}", file=sys.stderr)
    for c in children:
        print(f"[leak-check {worker}]   child process: {c}", file=sys.stderr)
    print(
        f"[leak-check {worker}] any of these can keep this worker from exiting; "
        "see nexi-lab/nexus#4667",
        file=sys.stderr,
    )
