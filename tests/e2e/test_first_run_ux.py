"""End-to-end test for the first-run UX workflow (Issue #2915).

Validates the full journey:
    nexus init --preset demo → nexus up → nexus demo init → verify → nexus down

This test requires Docker and is gated behind the ``e2e`` and ``docker``
pytest markers.  It is skipped by default and only runs in CI or when
explicitly requested with ``pytest -m e2e``.

Timeout: 5 minutes (containers need startup time).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

# Gate behind markers
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
]


def _docker_available() -> bool:
    """Check if Docker daemon is running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(autouse=True)
def _skip_without_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker is not available")


class TestFirstRunInit:
    """Test nexus init for various presets."""

    @pytest.fixture()
    def project_dir(self, tmp_path: Path) -> Path:
        """Create a temporary project directory."""
        return tmp_path

    def test_init_creates_config(self, project_dir: Path) -> None:
        """nexus init --preset demo writes nexus.yaml and data dirs."""
        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"

        result = subprocess.run(
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
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"init failed: {result.stderr}"
        assert config_path.exists(), "nexus.yaml not created"
        assert data_dir.exists(), "data directory not created"

        # Verify config structure
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "demo"
        assert cfg["auth"] == "database"
        assert "postgres" in cfg["services"]
        assert "dragonfly" in cfg["services"]
        assert cfg["ports"]["http"] == 2026

    def test_init_shared_with_tls(self, project_dir: Path) -> None:
        """nexus init --preset shared --tls enables TLS in config."""
        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--tls",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["tls"] is True
        assert "tls_dir" in cfg

    def test_init_with_addons(self, project_dir: Path) -> None:
        """nexus init --preset shared --with nats includes add-on."""
        config_path = project_dir / "nexus.yaml"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--with",
                "nats",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(project_dir / "data"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert "nats" in cfg.get("addons", [])

    def test_init_portable_outside_repo_root(self, project_dir: Path) -> None:
        """nexus init --preset demo succeeds in a clean temp dir.

        The bundled nexus-stack.yml should be copied to the project
        directory when no local compose file is found.
        """
        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"

        result = subprocess.run(
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
            capture_output=True,
            text=True,
            timeout=30,
            # Run from project_dir, NOT the repo root
            cwd=str(project_dir),
        )

        assert result.returncode == 0, (
            f"init failed outside repo root: {result.stderr}\n{result.stdout}"
        )
        assert config_path.exists()

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        # compose_file should be set and the file should exist
        compose_file = cfg.get("compose_file", "")
        assert compose_file, "compose_file not set in config"
        assert Path(compose_file).exists(), f"compose file not found: {compose_file}"

        # image_ref should be set to the resolved prebuilt image
        image_ref = cfg.get("image_ref", "")
        assert image_ref, "image_ref not set in config for portable path"
        assert image_ref.startswith("ghcr.io/nexi-lab/nexus:"), (
            f"image_ref should be a full GHCR reference, got: {image_ref}"
        )
        assert cfg.get("image_channel") == "stable", "default channel should be stable"
        assert cfg.get("image_accelerator") == "cpu", "default accelerator should be cpu"
        # Deprecated image_tag should not be present in new configs
        assert "image_tag" not in cfg, "new configs should use image_ref, not image_tag"

    def test_init_with_channel_edge(self, project_dir: Path) -> None:
        """nexus init --preset shared --channel edge pins to edge image."""
        config_path = project_dir / "nexus.yaml"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--channel",
                "edge",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(project_dir / "data"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["image_channel"] == "edge"
        assert "edge" in cfg["image_ref"]

    def test_init_with_explicit_image_tag(self, project_dir: Path) -> None:
        """nexus init --preset shared --image-tag 0.9.2 pins to exact tag."""
        config_path = project_dir / "nexus.yaml"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--image-tag",
                "0.9.2",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(project_dir / "data"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["image_ref"] == "ghcr.io/nexi-lab/nexus:0.9.2"

    def test_init_with_cuda_accelerator(self, project_dir: Path) -> None:
        """nexus init --preset shared --accelerator cuda appends -cuda suffix."""
        config_path = project_dir / "nexus.yaml"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--accelerator",
                "cuda",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(project_dir / "data"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["image_accelerator"] == "cuda"
        assert "-cuda" in cfg["image_ref"]


class TestFullWorkflow:
    """Full init → up → demo init → verify → down cycle.

    These tests exercise the complete first-run UX and require Docker
    to build/pull images and start containers.
    """

    @pytest.fixture()
    def project_dir(self, tmp_path: Path) -> Path:
        return tmp_path

    @pytest.fixture()
    def initialized_project(self, project_dir: Path) -> Path:
        """Run nexus init --preset demo and return the project dir.

        Uses the repo-root nexus-stack.yml with NEXUS_DOCKERFILE pointing
        to the lightweight nexus-demo.Dockerfile. The build context is the
        repo root, so ``COPY . /tmp/nexus-build/`` picks up pyproject.toml
        and src/, giving the container the PR's code (Python-only, no Rust).
        """
        # Find the repo root (where nexus-stack.yml lives)
        repo_root = Path(__file__).resolve().parents[2]
        compose_file = repo_root / "nexus-stack.yml"
        assert compose_file.exists(), f"repo-root compose file not found: {compose_file}"

        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "demo",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
                "--compose-file",
                str(compose_file),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_dir),
        )
        assert result.returncode == 0, f"init failed: {result.stderr}"
        return project_dir

    def test_up_starts_services(self, initialized_project: Path) -> None:
        """nexus up should start Docker Compose services."""
        config_path = initialized_project / "nexus.yaml"

        # Verify compose file exists before starting
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        compose_file = cfg.get("compose_file", "")
        assert Path(compose_file).exists(), f"compose file missing: {compose_file}"

        # Run nexus up --build so Docker Compose uses the build: directive
        # from the repo-root compose file (local branch code), instead of
        # pulling the pinned GHCR image which wouldn't reflect PR changes.
        result = subprocess.run(
            ["nexus", "up", "--build"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(initialized_project),
        )

        try:
            assert result.returncode == 0, (
                f"nexus up failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )

            # Re-read config — nexus up may have resolved port conflicts
            # and persisted new ports back to nexus.yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f)

            # Verify health endpoint is reachable
            import urllib.request

            health_port = cfg.get("ports", {}).get("http", 2026)
            resp = urllib.request.urlopen(f"http://localhost:{health_port}/health", timeout=10)
            assert resp.status == 200, f"health check returned {resp.status}"

        finally:
            # Always clean up — run nexus down
            down_result = subprocess.run(
                ["nexus", "down"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(initialized_project),
            )
            assert down_result.returncode == 0, f"nexus down failed: {down_result.stderr}"

    def test_full_init_up_demo_down(self, initialized_project: Path) -> None:
        """Complete first-run workflow: init → up → demo init → down.

        This is the golden path from issue #2915.
        """
        config_path = initialized_project / "nexus.yaml"

        # Step 1: nexus up --build (build from branch code, not GHCR image)
        up_result = subprocess.run(
            ["nexus", "up", "--build"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(initialized_project),
        )
        assert up_result.returncode == 0, (
            f"nexus up failed:\nstdout: {up_result.stdout}\nstderr: {up_result.stderr}"
        )

        # Re-read config after up — ports may have been reassigned
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        try:
            # Wait a moment for services to stabilize
            time.sleep(2)

            # Step 2: nexus demo init
            demo_result = subprocess.run(
                ["nexus", "demo", "init"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(initialized_project),
            )
            assert demo_result.returncode == 0, (
                f"demo init failed: {demo_result.stderr}\n{demo_result.stdout}"
            )
            assert "Seeding" in demo_result.stdout or "Files" in demo_result.stdout

            # Verify printed commands use correct CLI syntax
            # grep takes path as positional arg (not --path)
            assert "--path" not in demo_result.stdout, (
                "grep command should use positional path, not --path"
            )
            # For shared/demo presets, should print all required env vars
            assert "NEXUS_URL" in demo_result.stdout, (
                "demo init should print NEXUS_URL export for shared/demo presets"
            )
            assert "NEXUS_API_KEY" in demo_result.stdout, (
                "demo init should print NEXUS_API_KEY export for shared/demo presets"
            )
            assert "NEXUS_GRPC_PORT" in demo_result.stdout, (
                "demo init should print NEXUS_GRPC_PORT export for shared/demo presets"
            )

            # Verify manifest was created, files were seeded, and permissions set
            data_dir = cfg.get("data_dir", str(initialized_project / "nexus-data"))
            manifest_path = Path(data_dir) / ".demo-manifest.json"
            assert manifest_path.exists(), "demo manifest not created"

            with open(manifest_path) as f:
                manifest = json.loads(f.read())

            # Critical: demo files must actually be created (not silently 0)
            demo_files = manifest.get("files", [])
            assert len(demo_files) >= 10, (
                f"only {len(demo_files)} demo files in manifest, expected >= 10 — "
                "sys_write likely crashed (e.g. missing auto_parse attribute on remote NexusFS)"
            )

            assert manifest.get("permissions_seeded") is True, "permissions not seeded"
            assert manifest.get("permissions_count", 0) > 0, (
                f"permissions_count is {manifest.get('permissions_count', 0)}, "
                "expected > 0 — docker exec or RPC seeding failed"
            )

            # Step 2b: Verify grep/ls work against the running stack.
            # Set NEXUS_URL + NEXUS_API_KEY + NEXUS_GRPC_PORT env vars
            # (as printed by demo init).
            http_port = cfg.get("ports", {}).get("http", 2026)
            grpc_port = cfg.get("ports", {}).get("grpc", 2028)
            api_key = cfg.get("api_key", "")
            stack_env = {
                **os.environ,
                "NEXUS_URL": f"http://localhost:{http_port}",
                "NEXUS_API_KEY": api_key,
                "NEXUS_GRPC_PORT": str(grpc_port),
            }

            grep_result = subprocess.run(
                ["nexus", "grep", "vector index", "/workspace/demo"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(initialized_project),
                env=stack_env,
            )
            assert grep_result.returncode == 0, (
                f"nexus grep failed against live stack:\n"
                f"stdout: {grep_result.stdout}\nstderr: {grep_result.stderr}"
            )
            # Should find at least one match in the demo corpus
            assert grep_result.stdout.strip(), (
                "grep returned no output — demo file corpus not searchable"
            )

            # Step 2c: Verify ls works against the running stack
            ls_result = subprocess.run(
                ["nexus", "ls", "/workspace/demo"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(initialized_project),
                env=stack_env,
            )
            assert ls_result.returncode == 0, (
                f"nexus ls failed against live stack:\n"
                f"stdout: {ls_result.stdout}\nstderr: {ls_result.stderr}"
            )

            # Step 2d: Verify semantic search was initialized by demo init
            assert manifest.get("semantic_ready") is True, (
                "semantic search not initialized — demo init should call "
                "initialize_semantic_search + semantic_search_index"
            )

            # Step 2e: Verify semantic search query works against the live stack
            search_result = subprocess.run(
                [
                    "nexus",
                    "search",
                    "query",
                    "How does the demo authentication flow work?",
                    "--path",
                    "/workspace/demo",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(initialized_project),
                env=stack_env,
            )
            assert search_result.returncode == 0, (
                f"nexus search query failed against live stack:\n"
                f"stdout: {search_result.stdout}\nstderr: {search_result.stderr}"
            )

            # ----------------------------------------------------------
            # Step 2f: Knowledge platform — catalog schemas (Issue #2930)
            # ----------------------------------------------------------
            # Verify demo init output mentions catalog/aspects
            assert (
                "catalog" in demo_result.stdout.lower() or "schema" in demo_result.stdout.lower()
            ), "demo init output should mention catalog or schema seeding"

            # Verify manifest tracks knowledge platform seeding
            with open(manifest_path) as f:
                manifest = json.loads(f.read())
            assert manifest.get("write_mode_used") == "ec", (
                "manifest should track write_mode_used as 'ec'"
            )
            # schemas_extracted and aspects_created may be True or False
            # depending on whether the REST API was reachable, but the
            # keys should exist in the manifest
            assert "schemas_extracted" in manifest, "manifest should track schemas_extracted"
            assert "aspects_created" in manifest, "manifest should track aspects_created"

            # Verify nexus catalog schema works against the live stack
            catalog_schema_result = subprocess.run(
                [
                    "nexus",
                    "catalog",
                    "schema",
                    "/workspace/demo/data/sales.csv",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(initialized_project),
                env=stack_env,
            )
            # Command should succeed (rc=0) or fail gracefully (rc=1)
            # with a clear error — it should never crash
            assert catalog_schema_result.returncode in (0, 1), (
                f"nexus catalog schema crashed:\n"
                f"stdout: {catalog_schema_result.stdout}\n"
                f"stderr: {catalog_schema_result.stderr}"
            )
            if catalog_schema_result.returncode == 1:
                assert "Traceback" not in catalog_schema_result.stderr, (
                    f"nexus catalog schema crashed with traceback:\n{catalog_schema_result.stderr}"
                )
            if catalog_schema_result.returncode == 0:
                # If the REST API is available, output should mention
                # columns or schema details
                output = catalog_schema_result.stdout.lower()
                assert "schema" in output or "column" in output or "no schema" in output, (
                    f"catalog schema output unexpected: {catalog_schema_result.stdout}"
                )

            # ----------------------------------------------------------
            # Step 2g: Knowledge platform — catalog column search
            # ----------------------------------------------------------
            catalog_search_result = subprocess.run(
                [
                    "nexus",
                    "catalog",
                    "search",
                    "--column",
                    "amount",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(initialized_project),
                env=stack_env,
            )
            assert catalog_search_result.returncode in (0, 1), (
                f"nexus catalog search crashed:\n"
                f"stdout: {catalog_search_result.stdout}\n"
                f"stderr: {catalog_search_result.stderr}"
            )
            if catalog_search_result.returncode == 1:
                assert "Traceback" not in catalog_search_result.stderr, (
                    f"nexus catalog search crashed with traceback:\n{catalog_search_result.stderr}"
                )

            # ----------------------------------------------------------
            # Step 2h: Knowledge platform — aspects list
            # ----------------------------------------------------------
            aspects_list_result = subprocess.run(
                [
                    "nexus",
                    "aspects",
                    "list",
                    "/workspace/demo/restricted/internal.md",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(initialized_project),
                env=stack_env,
            )
            assert aspects_list_result.returncode in (0, 1), (
                f"nexus aspects list crashed:\n"
                f"stdout: {aspects_list_result.stdout}\n"
                f"stderr: {aspects_list_result.stderr}"
            )
            if aspects_list_result.returncode == 1:
                assert "Traceback" not in aspects_list_result.stderr, (
                    f"nexus aspects list crashed with traceback:\n{aspects_list_result.stderr}"
                )

            # ----------------------------------------------------------
            # Step 2i: Knowledge platform — ops replay
            # ----------------------------------------------------------
            ops_replay_result = subprocess.run(
                ["nexus", "ops", "replay", "--limit", "5"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(initialized_project),
                env=stack_env,
            )
            assert ops_replay_result.returncode in (0, 1), (
                f"nexus ops replay crashed:\n"
                f"stdout: {ops_replay_result.stdout}\n"
                f"stderr: {ops_replay_result.stderr}"
            )
            if ops_replay_result.returncode == 1:
                assert "Traceback" not in ops_replay_result.stderr, (
                    f"nexus ops replay crashed with traceback:\n{ops_replay_result.stderr}"
                )

            # ----------------------------------------------------------
            # Step 2j: Knowledge platform — reindex dry-run
            # ----------------------------------------------------------
            reindex_result = subprocess.run(
                [
                    "nexus",
                    "reindex",
                    "--target",
                    "search",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(initialized_project),
                env=stack_env,
            )
            # reindex uses local RecordStore access, may not work via
            # remote preset — accept both success and graceful failure
            assert reindex_result.returncode in (0, 1), (
                f"nexus reindex crashed:\n"
                f"stdout: {reindex_result.stdout}\n"
                f"stderr: {reindex_result.stderr}"
            )
            if reindex_result.returncode == 1:
                assert "Traceback" not in reindex_result.stderr, (
                    f"nexus reindex crashed with traceback:\n{reindex_result.stderr}"
                )

            # Step 3: nexus demo reset (verify cleanup works)
            reset_result = subprocess.run(
                ["nexus", "demo", "reset"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(initialized_project),
            )
            assert reset_result.returncode == 0, f"demo reset failed: {reset_result.stderr}"
            assert not manifest_path.exists(), "manifest should be removed after reset"

        finally:
            # Step 4: nexus down — must succeed and actually remove the stack
            down_result = subprocess.run(
                ["nexus", "down"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(initialized_project),
            )
            assert down_result.returncode == 0, f"nexus down failed: {down_result.stderr}"
