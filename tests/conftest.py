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
