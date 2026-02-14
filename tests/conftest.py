"""Root test configuration.

Provides quarantine marker handling for flaky/timing-dependent tests.
Quarantined tests are skipped by default; pass --run-quarantine to include them.
"""

import pytest
import structlog


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
    from nexus.server.dependencies import _reset_auth_cache

    _reset_auth_cache()


@pytest.fixture(autouse=True)
def _reset_stream_secret_fixture():
    """Reset the HMAC stream signing secret between tests for isolation."""
    yield
    from nexus.server.streaming import _reset_stream_secret

    _reset_stream_secret()
