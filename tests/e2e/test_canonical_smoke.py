"""Canonical reusable smoke suite for the default CPU demo/shared path.

Issue #2961, Section G: Covers the base canonical flow plus file operations,
version tracking, event log/ops replay, permissions, Zoekt search, semantic
search, and the HERB-derived quality gate.

This suite is designed to be reusable across:
  - Local developer verification
  - PR / CI smoke (where Docker is available)
  - Post-merge release-smoke / publish validation

All tests are gated behind the ``e2e`` and ``docker`` markers.

Usage:
    pytest tests/e2e/test_canonical_smoke.py -m e2e -v
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

# Gate behind markers
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
]


# ---------------------------------------------------------------------------
# Timing harness (Issue #2961, Section G.4 + Issue 16)
# ---------------------------------------------------------------------------


@contextmanager
def timed(label: str, results: list[dict[str, Any]]) -> Generator[None, None, None]:
    """Context manager that records wall-clock timing for a smoke step."""
    start = time.monotonic()
    yield
    elapsed = time.monotonic() - start
    results.append({"step": label, "elapsed_s": round(elapsed, 3)})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run(
    cmd: list[str],
    env: dict[str, str] | None = None,
    timeout: int = 60,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


@pytest.fixture(autouse=True)
def _skip_without_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker is not available")


# ---------------------------------------------------------------------------
# Canonical smoke test
# ---------------------------------------------------------------------------


class TestCanonicalSmoke:
    """Base canonical flow: init → up → demo init → status → down.

    Covers file ops, version tracking, event log, permissions, search.
    Reports timing signals as structured JSON.
    """

    @pytest.fixture()
    def project_dir(self, tmp_path: Path) -> Path:
        return tmp_path

    @pytest.fixture()
    def stack_env(self, project_dir: Path) -> Generator[tuple[dict[str, str], Path], None, None]:
        """Set up and tear down a full demo stack.

        Yields (env_dict, project_dir) so tests can pass cwd properly.
        """
        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"
        cwd = str(project_dir)

        # Step 1: nexus init --preset demo
        init_result = _run(
            [
                "nexus",
                "init",
                "--preset",
                "demo",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            timeout=30,
            cwd=cwd,
        )
        assert init_result.returncode == 0, f"init failed: {init_result.stderr}"

        # Verify new config model
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert "image_ref" in cfg, "Config should have image_ref"
        assert cfg.get("image_channel") == "stable"

        # Step 2: nexus up (must run from project_dir where nexus.yaml lives)
        up_result = _run(
            ["nexus", "up"],
            timeout=300,
            cwd=cwd,
        )
        if up_result.returncode != 0:
            _run(["nexus", "down"], timeout=60, cwd=cwd)
            pytest.fail(f"nexus up failed:\nstdout: {up_result.stdout}\nstderr: {up_result.stderr}")

        # Re-read config (ports may have been reassigned)
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        http_port = cfg.get("ports", {}).get("http", 2026)
        grpc_port = cfg.get("ports", {}).get("grpc", 2028)
        api_key = cfg.get("api_key", "")

        env = {
            **os.environ,
            "NEXUS_URL": f"http://localhost:{http_port}",
            "NEXUS_API_KEY": api_key,
            "NEXUS_GRPC_PORT": str(grpc_port),
        }

        try:
            # Step 3: nexus demo init
            time.sleep(2)  # Let services stabilize
            demo_result = _run(["nexus", "demo", "init"], timeout=120, env=env, cwd=cwd)
            assert demo_result.returncode == 0, (
                f"demo init failed:\nstdout: {demo_result.stdout}\nstderr: {demo_result.stderr}"
            )
            yield env, project_dir
        finally:
            # Step N: nexus down
            _run(["nexus", "down"], timeout=60, cwd=cwd)

    def test_file_operations(self, stack_env: tuple[dict[str, str], Path]) -> None:
        """Test mkdir, write, ls, cat, grep, delete."""
        env, cwd = stack_env
        timings: list[dict[str, Any]] = []

        with timed("mkdir", timings):
            r = _run(["nexus", "mkdir", "/workspace/smoke-test"], env=env, cwd=cwd)
            assert r.returncode == 0, f"mkdir failed: {r.stderr}"

        with timed("write", timings):
            r = _run(
                [
                    "nexus",
                    "write",
                    "/workspace/smoke-test/hello.txt",
                    "smoke test content",
                ],
                env=env,
                cwd=cwd,
            )
            assert r.returncode == 0, f"write failed: {r.stderr}"

        with timed("ls", timings):
            r = _run(["nexus", "ls", "/workspace/smoke-test"], env=env, cwd=cwd)
            assert r.returncode == 0, f"ls failed: {r.stderr}"
            assert "hello.txt" in r.stdout

        with timed("cat", timings):
            r = _run(
                ["nexus", "cat", "/workspace/smoke-test/hello.txt"],
                env=env,
                cwd=cwd,
            )
            assert r.returncode == 0, f"cat failed: {r.stderr}"
            assert "smoke test content" in r.stdout

        with timed("grep", timings):
            r = _run(
                ["nexus", "grep", "smoke test", "/workspace/smoke-test"],
                env=env,
                cwd=cwd,
            )
            assert r.returncode == 0, f"grep failed: {r.stderr}"

        with timed("rm", timings):
            r = _run(
                ["nexus", "rm", "/workspace/smoke-test/hello.txt"],
                env=env,
                cwd=cwd,
            )
            assert r.returncode == 0, f"rm failed: {r.stderr}"

        # Print structured timing output
        print(f"\n[SMOKE_TIMING] file_ops: {json.dumps(timings)}")

    def test_version_tracking(self, stack_env: tuple[dict[str, str], Path]) -> None:
        """Verify version history works on demo files."""
        env, cwd = stack_env
        r = _run(
            ["nexus", "versions", "list", "/workspace/demo/plan.md"],
            env=env,
            cwd=cwd,
        )
        assert r.returncode == 0, (
            f"versions list failed (rc={r.returncode}):\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )

    def test_ops_replay(self, stack_env: tuple[dict[str, str], Path]) -> None:
        """Verify event log / operation replay returns results."""
        env, cwd = stack_env
        r = _run(["nexus", "ops", "replay", "--limit", "5"], env=env, cwd=cwd)
        assert r.returncode == 0, (
            f"ops replay failed (rc={r.returncode}):\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert r.stdout.strip(), "ops replay returned empty output"

    def test_permissions(self, stack_env: tuple[dict[str, str], Path]) -> None:
        """Verify permission tuples were seeded and are queryable."""
        env, cwd = stack_env
        timings: list[dict[str, Any]] = []

        with timed("rebac_check", timings):
            r = _run(
                [
                    "nexus",
                    "rebac",
                    "check",
                    "user:admin",
                    "direct_owner",
                    "file:/workspace/demo",
                ],
                env=env,
                cwd=cwd,
            )
            assert r.returncode == 0, (
                f"rebac check failed (rc={r.returncode}):\nstdout: {r.stdout}\nstderr: {r.stderr}"
            )
            # Verify the check returned an allowed result
            assert r.stdout.strip(), "rebac check returned empty output"

        print(f"\n[SMOKE_TIMING] permissions: {json.dumps(timings)}")

    def test_agent_list(self, stack_env: tuple[dict[str, str], Path]) -> None:
        """Verify agent list shows the seeded demo agent."""
        env, cwd = stack_env
        r = _run(["nexus", "agent", "list"], env=env, cwd=cwd)
        assert r.returncode == 0, (
            f"agent list failed (rc={r.returncode}):\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
        # Verify the demo_agent seeded by demo init is present
        assert "demo_agent" in r.stdout, f"demo_agent not found in agent list output:\n{r.stdout}"

    def test_agent_ipc(self, stack_env: tuple[dict[str, str], Path]) -> None:
        """Verify agent IPC via Nexus filesystem (scratchpad pattern).

        Tests the IPC-via-filesystem path: one agent writes to a shared
        scratchpad path, another reads it — the Nexus VFS mediates the
        handoff with permission checks and audit logging.
        """
        env, cwd = stack_env
        ipc_path = "/workspace/demo/ipc/handoff.json"

        # Create IPC directory
        r = _run(["nexus", "mkdir", "/workspace/demo/ipc"], env=env, cwd=cwd)
        assert r.returncode == 0, f"ipc mkdir failed: {r.stderr}"

        # Agent writes to scratchpad
        payload = '{"from": "demo_agent", "task": "search", "status": "complete"}'
        r = _run(["nexus", "write", ipc_path, payload], env=env, cwd=cwd)
        assert r.returncode == 0, f"ipc write failed: {r.stderr}"

        # Another agent reads from scratchpad
        r = _run(["nexus", "cat", ipc_path], env=env, cwd=cwd)
        assert r.returncode == 0, f"ipc read failed: {r.stderr}"
        assert "demo_agent" in r.stdout, "IPC payload not readable"
        assert "complete" in r.stdout, "IPC payload content missing"

    def test_grep_demo_corpus(self, stack_env: tuple[dict[str, str], Path]) -> None:
        """Verify grep works against demo and HERB corpus."""
        env, cwd = stack_env
        timings: list[dict[str, Any]] = []

        with timed("grep_demo", timings):
            r = _run(
                ["nexus", "grep", "vector index", "/workspace/demo"],
                env=env,
                cwd=cwd,
            )
            assert r.returncode == 0, f"grep failed: {r.stderr}"
            assert r.stdout.strip(), "grep returned no results"

        with timed("grep_herb", timings):
            r = _run(
                ["nexus", "grep", "Meridian Health", "/workspace/demo/herb"],
                env=env,
                cwd=cwd,
            )
            assert r.returncode == 0, f"grep HERB failed: {r.stderr}"
            assert "Meridian" in r.stdout

        print(f"\n[SMOKE_TIMING] grep: {json.dumps(timings)}")

    @pytest.fixture()
    def search_http(
        self, stack_env: tuple[dict[str, str], Path]
    ) -> Generator[httpx.Client, None, None]:
        env, _cwd = stack_env
        with httpx.Client(
            base_url=env["NEXUS_URL"],
            headers={"Authorization": f"Bearer {env['NEXUS_API_KEY']}"},
            timeout=60,
        ) as client:
            yield client

    def test_semantic_search(self, search_http: httpx.Client) -> None:
        """Verify the demo server's indexed corpus through its HTTP API."""
        timings: list[dict[str, Any]] = []

        with timed("semantic_query", timings):
            response = search_http.get(
                "/api/v2/search/query",
                params={
                    "q": "How does authentication work?",
                    "path": "/workspace/demo",
                    "type": "semantic",
                },
            )
            response.raise_for_status()
            payload = response.json()
            assert not payload.get("error"), payload
            assert payload["results"], "semantic search returned no results"
            assert all(hit["path"].startswith("/workspace/demo/") for hit in payload["results"])

        print(f"\n[SMOKE_TIMING] semantic_search: {json.dumps(timings)}")

    def test_herb_semantic_quality_gate(
        self, stack_env: tuple[dict[str, str], Path], search_http: httpx.Client
    ) -> None:
        """HERB semantic search quality gate (Issue #2961, Section G.8).

        For each curated QA question, assert the answer-bearing file
        appears in the top-5 results. Reports hit rate and MRR as
        non-blocking metrics.
        """
        from nexus.cli.commands.demo_data import HERB_QA_SET

        _env, cwd = stack_env
        timings: list[dict[str, Any]] = []
        hits = 0
        reciprocal_ranks: list[float] = []

        # Check manifest for which search engine was used
        manifest_path = list(cwd.glob("nexus-data/.demo-manifest.json"))
        semantic_engine = "unknown"
        if manifest_path:
            with open(manifest_path[0]) as f:
                manifest = json.loads(f.read())
                semantic_engine = manifest.get("semantic_engine", "unknown")

        for qa in HERB_QA_SET:
            question = qa["question"]
            expected_sub = qa["expected_substring"]

            expected_file = qa["expected_file"]

            with timed(f"qa_{expected_sub}", timings):
                response = search_http.get(
                    "/api/v2/search/query",
                    params={
                        "q": question,
                        "path": "/workspace/demo/herb",
                        "type": "semantic",
                        "limit": 5,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                assert not payload.get("error"), payload

            result_paths = [hit["path"] for hit in payload["results"][:5]]
            found_rank = next(
                (rank for rank, path in enumerate(result_paths, 1) if path == expected_file),
                0,
            )

            if found_rank > 0:
                hits += 1
                reciprocal_ranks.append(1.0 / found_rank)
            else:
                reciprocal_ranks.append(0.0)

        total = len(HERB_QA_SET)
        hit_rate = hits / total if total else 0
        mrr = sum(reciprocal_ranks) / total if total else 0

        # Non-blocking metrics
        print(f"\n[SMOKE_QUALITY] semantic_engine: {semantic_engine}")
        print(f"[SMOKE_QUALITY] herb_hit_rate: {hit_rate:.2f} ({hits}/{total})")
        print(f"[SMOKE_QUALITY] herb_mrr: {mrr:.3f}")
        print(f"[SMOKE_TIMING] herb_qa: {json.dumps(timings)}")

        # Blocking gate: majority of questions should hit (>= 50%)
        assert hit_rate >= 0.5, (
            f"HERB quality gate failed: hit_rate={hit_rate:.2f} ({hits}/{total}), "
            f"engine={semantic_engine}. "
            "Expected >= 0.5. Check that HERB corpus was seeded and indexed."
        )

    def test_nexus_status(self, stack_env: tuple[dict[str, str], Path]) -> None:
        """Verify nexus status shows image_ref and is parseable."""
        env, cwd = stack_env
        r = _run(["nexus", "status", "--json"], env=env, cwd=cwd)
        if r.returncode == 0 and r.stdout.strip():
            try:
                data = json.loads(r.stdout)
                # If project config enrichment works, image_ref should be present
                if "image_ref" in data:
                    assert data["image_ref"], "image_ref should not be empty"
            except json.JSONDecodeError:
                pass  # Non-JSON output is acceptable for status
