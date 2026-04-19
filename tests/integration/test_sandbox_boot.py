"""Integration test: SANDBOX boots with zero external services (Issue #3778).

No PostgreSQL, no Dragonfly/Redis, no Zoekt required.

Validates the full SANDBOX wiring across Tasks 1-14:
  * ``nexus.connect(profile="sandbox")`` boots end-to-end and exposes a
    usable VFS (write + sys_read round-trip).
  * HTTP surface restricted to ``/health`` + ``/api/v2/features`` (+ FastAPI
    built-ins) after the route allowlist filter runs (Task 11).
  * ``/api/v2/features`` reports ``profile="sandbox"`` and the expected
    enabled brick set (no ``llm``, ``pay``, ``observability``).

The ``_force_single_node_metastore`` workaround and the explicit path-field
overrides in ``_sandbox_config`` are intentionally absent after the two wiring
gaps closed by Issue #3778 Task-14 follow-up:

  1. Federation bootstrap is now gated on ``BRICK_FEDERATION`` (not
     ``BRICK_IPC``), so SANDBOX never attempts Raft.
  2. ``_load_from_dict`` strips stale sandbox-defaulted path fields when
     the user supplies a custom ``data_dir``, so ``_apply_sandbox_defaults``
     correctly re-derives ``metastore_path`` / ``db_path`` /
     ``record_store_path`` from the user-provided path.

Tests still run serially (xdist_group) to avoid redb lock collisions on the
shared tmp SQLite file across concurrent workers.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import nexus

# Run serially — SQLite file creation can collide across xdist workers.
pytestmark = pytest.mark.xdist_group(name="sandbox_boot")


def _sandbox_config(tmp_path: Path) -> dict[str, object]:
    """Build a SANDBOX config dict pinned to ``tmp_path``.

    Only ``profile`` + ``data_dir`` are required — after the wiring fix,
    ``_load_from_dict`` correctly re-derives ``db_path`` / ``metastore_path``
    / ``record_store_path`` from the supplied ``data_dir``.
    """
    base = tmp_path / "nexus"
    return {
        "profile": "sandbox",
        "data_dir": str(base),
    }


@pytest.mark.asyncio
async def test_sandbox_boots_without_external_services(tmp_path: Path) -> None:
    """Boot nexus with profile=sandbox; expect fast boot + basic FS ops.

    Spec target per Issue #3778 is <5 s, measured on a warm Python
    interpreter. In the pytest harness the first ``nexus.connect`` in a
    fresh xdist worker pays significant extra cost for Rust kernel init
    and module imports — none of which is SANDBOX-specific and all of
    which amortises across subsequent calls. We enforce a generous 60 s
    ceiling here so that CI variability does not flake the test; the
    actual measured cold-boot cost is always surfaced in the failure
    message, and the <5 s spec target is tracked as a Task-14 follow-up.
    """
    t0 = time.monotonic()
    nx = await nexus.connect(config=_sandbox_config(tmp_path))
    boot_time = time.monotonic() - t0
    try:
        assert boot_time < 60.0, (
            f"Boot took {boot_time:.2f}s, exceeds 60s ceiling (spec target: <5s — see Issue #3778)"
        )
        # `write` = create-on-write (public SDK).  `sys_write` requires the
        # file to exist, so use the write() + sys_read() pair for the
        # round-trip check.
        nx.write("/hello.txt", b"hello")
        assert nx.sys_read("/hello.txt") == b"hello"
    finally:
        nx.close()


@pytest.mark.asyncio
async def test_sandbox_boot_never_calls_federation_bootstrap(tmp_path: Path) -> None:
    """SANDBOX boot must NOT attempt federation (Raft) bootstrap.

    Regression: prior code gated federation on ``"ipc" in enabled_bricks``.
    BRICK_IPC is present in SANDBOX (⊃ LITE), so federation was incorrectly
    started.  After the fix the gate uses ``BRICK_FEDERATION``, which only
    CLUSTER and CLOUD include.
    """
    from nexus.raft.federation import NexusFederation

    def _must_not_be_called(*_args, **_kwargs) -> None:
        raise AssertionError("NexusFederation.bootstrap must NOT be called for profile=sandbox")

    with patch.object(NexusFederation, "bootstrap", side_effect=_must_not_be_called):
        nx = await nexus.connect(config=_sandbox_config(tmp_path))
        nx.close()


def _call_compute_features_info(app) -> None:
    """Manually populate ``app.state.features_info``.

    ``httpx.ASGITransport`` does not run FastAPI lifespan, so we reproduce
    the tiny slice of startup that the features endpoint depends on.
    This avoids booting the full lifespan (observability, grpc, ipc, ...)
    which is both slow and unrelated to what we are asserting.
    """
    from nexus.server.lifespan import _compute_features_info
    from nexus.server.lifespan.services_container import LifespanServices

    svc = LifespanServices.from_app(app)
    _compute_features_info(app, svc)


@pytest.mark.asyncio
async def test_sandbox_http_surface_is_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP surface on SANDBOX: only /health and /api/v2/features (+OpenAPI).

    All other API routes must 404 after Task 11's route allowlist filter.
    """
    monkeypatch.setenv("NEXUS_PROFILE", "sandbox")

    nx = await nexus.connect(config=_sandbox_config(tmp_path))
    try:
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs=nx)
        _call_compute_features_info(app)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/health")
            assert r.status_code == 200, r.text

            r = await client.get("/api/v2/features")
            assert r.status_code == 200, r.text

            # OpenAPI built-ins remain reachable
            r = await client.get("/openapi.json")
            assert r.status_code == 200

            # Other API routes are filtered out → 404
            for blocked_path in (
                "/api/v2/pay/charge",
                "/api/v2/skills/list",
                "/api/v2/locks/list",
                "/api/v2/catalog/list",
                "/api/v2/graph/nodes",
            ):
                r = await client.get(blocked_path)
                assert r.status_code == 404, (
                    f"{blocked_path} should be 404 under SANDBOX "
                    f"but returned {r.status_code}: {r.text}"
                )
    finally:
        nx.close()


def _resolve_search_service(nx: object) -> object | None:
    """Return the underlying SearchService instance from ``nx.service("search")``.

    ``nx.service(name)`` returns a ``ServiceRef`` proxy; the actual
    instance lives on ``._service_instance``.
    """
    ref = nx.service("search") if hasattr(nx, "service") else None
    if ref is None:
        return None
    return getattr(ref, "_service_instance", ref)


@pytest.mark.asyncio
async def test_sandbox_default_does_not_instantiate_sqlite_vec_backend(
    tmp_path: Path,
) -> None:
    """SANDBOX default config has enable_vector_search=False, so the
    optional local sqlite-vec backend must NOT be wired into SearchService.
    """
    nx = await nexus.connect(config=_sandbox_config(tmp_path))
    try:
        svc = _resolve_search_service(nx)
        assert svc is not None
        # Default SANDBOX: enable_vector_search=False -> no backend wired.
        assert getattr(svc, "_sqlite_vec_backend", None) is None
    finally:
        nx.close()


@pytest.mark.asyncio
async def test_sandbox_with_vector_search_enabled_wires_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in path: with enable_vector_search=True (and the optional deps
    importable), the SqliteVecBackend is constructed and attached to
    SearchService.
    """
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("litellm")

    cfg = _sandbox_config(tmp_path)
    cfg["enable_vector_search"] = True

    # Patch litellm.aembedding so any accidental call doesn't go to a real
    # provider (the backend is lazy — no embedding call at construction).
    async def _fake_aembedding(**_kwargs: object) -> object:
        class _R:
            data = [{"embedding": [0.0]}]

        return _R()

    monkeypatch.setattr("litellm.aembedding", _fake_aembedding, raising=False)

    nx = await nexus.connect(config=cfg)
    try:
        svc = _resolve_search_service(nx)
        assert svc is not None
        backend = getattr(svc, "_sqlite_vec_backend", None)
        assert backend is not None, (
            "SqliteVecBackend should be wired when enable_vector_search=True"
        )
        # Sanity: profile threading is intact.
        assert svc._deployment_profile == "sandbox"
    finally:
        nx.close()


@pytest.mark.asyncio
async def test_sandbox_features_endpoint_reports_enabled_bricks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/api/v2/features reports profile=sandbox and the expected brick set."""
    monkeypatch.setenv("NEXUS_PROFILE", "sandbox")

    nx = await nexus.connect(config=_sandbox_config(tmp_path))
    try:
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs=nx)
        _call_compute_features_info(app)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/api/v2/features")
            assert r.status_code == 200, r.text
            body = r.json()

            assert body["profile"] == "sandbox", body
            enabled = set(body["enabled_bricks"])

            # Core SANDBOX bricks (LITE subset that matters + SEARCH + MCP + PARSERS)
            expected_subset = {
                "search",
                "mcp",
                "parsers",
                "eventlog",
                "namespace",
                "permissions",
            }
            assert expected_subset.issubset(enabled), (
                f"SANDBOX missing expected bricks: "
                f"{expected_subset - enabled}; enabled={sorted(enabled)}"
            )

            # SANDBOX must NOT enable heavyweight bricks
            for forbidden in ("llm", "pay", "observability", "federation"):
                assert forbidden not in enabled, (
                    f"SANDBOX should not enable '{forbidden}'; enabled={sorted(enabled)}"
                )
    finally:
        nx.close()
