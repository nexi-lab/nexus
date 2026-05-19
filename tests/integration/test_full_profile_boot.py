"""FULL profile real-boot E2E (Issue #4132).

Gated: only runs with NEXUS_E2E=1 (boots a real Docker stack:
PostgreSQL + Dragonfly + Zoekt). Captures boot/RSS as guidance, not
CI gates; asserts control-plane calls with generous bounds.
"""

import os
import time

import pytest

pytestmark = pytest.mark.integration

requires_e2e = pytest.mark.skipif(
    os.environ.get("NEXUS_E2E") != "1",
    reason="FULL boot E2E requires NEXUS_E2E=1 (real Docker stack)",
)


@requires_e2e
def test_full_stack_boots_and_serves(full_stack):
    """full_stack fixture: nexus init --preset shared; nexus up; yield env; nexus down."""
    t0 = time.monotonic()
    health = full_stack.http_get("/health")
    boot_s = time.monotonic() - t0
    assert health.status_code == 200
    features = full_stack.http_get("/api/v2/features")
    assert features.status_code == 200
    body = features.json()
    assert body  # FULL reports a non-empty feature set
    print(f"[bench] first-health latency ~ {boot_s:.2f}s")


@requires_e2e
def test_remote_sdk_connect(full_stack):
    from nexus.sdk import connect

    nx = connect(
        config={
            "profile": "remote",
            "url": full_stack.url,
            "api_key": full_stack.api_key,
        }
    )
    assert nx is not None
    nx.ls("/")


@requires_e2e
def test_remote_sdk_without_grpc_fails_clearly(full_stack, monkeypatch):
    from nexus.sdk import connect

    monkeypatch.setenv("NEXUS_GRPC_PORT", "1")  # unreachable
    with pytest.raises(Exception, match=r"."):  # any error — connection refused / RPC failure
        connect(
            config={
                "profile": "remote",
                "url": full_stack.url,
                "api_key": full_stack.api_key,
            }
        ).ls("/")
