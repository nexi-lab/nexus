"""FULL profile real-boot E2E (Issue #4132).

Gated: only runs with NEXUS_E2E=1 (boots a real Docker stack:
PostgreSQL + Dragonfly + the Nexus server). Captures boot/RSS as
guidance, not CI gates; asserts control-plane calls with generous
bounds.

KNOWN-BLOCKED by "Bug B": on the current product `nexus up --preset
shared` returns rc=1 because its health gate waits for `zoekt` even
though the shared preset does not start it (a pre-existing `nexus up`
health-gate defect, out of #4132's docs/test scope; see
docs/superpowers/specs/2026-05-18-issue-4132-full-profile-design.md
"Bug B"). For that ONE precise signature the fixture
(`tests/integration/conftest.py`) does NOT blind-xfail: it first
PROVES the hub actually serves by hitting the running container's
``/health`` (200) and ``/api/v2/features`` (non-empty) directly. Only
if the hub is verifiably serving is it ``pytest.xfail``ed (so it
neither masquerades as green nor hard-reds CI, and XPASSes once Bug B
is fixed). If the hub does NOT serve, it hard-FAILS — a real
FULL-stack regression hiding behind a zoekt mention cannot pass. Every
OTHER `nexus up`/health/gRPC failure also hard-FAILS, so this gate
keeps full blocking value (no blanket module-level xfail). Empirical
#4132 verification was also done directly against a live hub (recorded
in the spec), independent of this gate.
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
def test_remote_sdk_connect(full_stack, monkeypatch):
    from nexus.sdk import connect

    # Pin the SDK to the fixture's *actual* resolved gRPC port. Without
    # this, connect() falls back to ambient/default (2028) and would
    # fail on conflict-remapped ports — or, worse, silently pass against
    # an unrelated service on 2028 instead of the booted fixture.
    monkeypatch.setenv("NEXUS_GRPC_PORT", str(full_stack.grpc_port))

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
