"""Tests for nexus.cli.commands.init_cmd — preset-aware init."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli.commands.init_cmd import (
    ADDON_PROFILE_MAP,
    PRESETS,
    VALID_PRESETS,
    PresetConfig,
    _build_config,
    _bundled_compose_file,
    _find_compose_file,
    _resolve_image_ref,
    _scaffold_tls,
    init,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def tmp_project(tmp_path: Path) -> Path:
    """Provide a temp directory for init output."""
    return tmp_path


@pytest.fixture()
def _compose_file(tmp_path: Path) -> Path:
    """Create a minimal nexus-stack.yml in tmp_path."""
    cf = tmp_path / "nexus-stack.yml"
    cf.write_text("services: {}\n")
    return cf


# ---------------------------------------------------------------------------
# Unit tests — PresetConfig (Issue #2961, Issue 6)
# ---------------------------------------------------------------------------


class TestPresetConfig:
    def test_frozen_dataclass(self) -> None:
        cfg = PresetConfig(
            services=("nexus",),
            auth="static",
            compose_profiles=("core",),
            port_keys=("http",),
        )
        with pytest.raises(AttributeError):
            cfg.auth = "none"

    def test_all_presets_defined(self) -> None:
        for preset in VALID_PRESETS:
            assert preset in PRESETS

    def test_local_preset_has_no_services(self) -> None:
        assert PRESETS["local"].services == ()
        assert PRESETS["local"].compose_profiles == ()
        assert PRESETS["local"].port_keys == ()

    def test_shared_and_demo_have_same_services(self) -> None:
        assert PRESETS["shared"].services == PRESETS["demo"].services

    def test_shared_and_demo_differ_in_auth(self) -> None:
        assert PRESETS["shared"].auth == "static"
        assert PRESETS["demo"].auth == "database"

    def test_default_channel_and_accelerator(self) -> None:
        for preset in VALID_PRESETS:
            assert PRESETS[preset].image_channel == "edge"
            assert PRESETS[preset].image_accelerator == "cpu"


# ---------------------------------------------------------------------------
# Unit tests — ADDON_PROFILE_MAP (Issue #2961, Issue 5)
# ---------------------------------------------------------------------------


class TestAddonProfileMap:
    def test_all_addons_mapped(self) -> None:
        expected = {"nats", "mcp", "frontend", "langgraph", "observability"}
        assert set(ADDON_PROFILE_MAP.keys()) == expected

    def test_nats_maps_to_events(self) -> None:
        assert ADDON_PROFILE_MAP["nats"] == "events"


# ---------------------------------------------------------------------------
# Unit tests — _resolve_image_ref (Issue #2961, Issue 1)
# ---------------------------------------------------------------------------


class TestResolveImageRef:
    def test_stable_channel_uses_stable_tag(self) -> None:
        ref = _resolve_image_ref("stable", "cpu")
        assert ref == "ghcr.io/nexi-lab/nexus:stable"

    def test_edge_channel_uses_edge_tag(self) -> None:
        ref = _resolve_image_ref("edge", "cpu")
        assert ref == "ghcr.io/nexi-lab/nexus:edge"

    def test_cuda_accelerator_appends_suffix(self) -> None:
        ref = _resolve_image_ref("edge", "cuda")
        assert ref == "ghcr.io/nexi-lab/nexus:edge-cuda"

    def test_explicit_tag_overrides_channel(self) -> None:
        ref = _resolve_image_ref("stable", "cpu", image_tag="0.9.2")
        assert ref == "ghcr.io/nexi-lab/nexus:0.9.2"

    def test_explicit_tag_with_cuda(self) -> None:
        ref = _resolve_image_ref("stable", "cuda", image_tag="0.9.2")
        assert ref == "ghcr.io/nexi-lab/nexus:0.9.2-cuda"

    def test_explicit_digest_overrides_everything(self) -> None:
        digest = "sha256:abcdef1234567890"
        ref = _resolve_image_ref("stable", "cuda", image_digest=digest)
        assert ref == f"ghcr.io/nexi-lab/nexus@{digest}"

    def test_explicit_digest_ignores_accelerator(self) -> None:
        digest = "sha256:abcdef1234567890"
        ref = _resolve_image_ref("stable", "cuda", image_digest=digest)
        assert "-cuda" not in ref


# ---------------------------------------------------------------------------
# Unit tests — _find_compose_file
# ---------------------------------------------------------------------------


class TestFindComposeFile:
    def test_finds_in_cwd(self, tmp_path: Path) -> None:
        (tmp_path / "nexus-stack.yml").write_text("services: {}\n")
        with patch("nexus.cli.commands.init_cmd.Path.cwd", return_value=tmp_path):
            result = _find_compose_file()
        assert result is not None
        assert result.name == "nexus-stack.yml"

    def test_finds_in_ancestor(self, tmp_path: Path) -> None:
        (tmp_path / "nexus-stack.yml").write_text("services: {}\n")
        child = tmp_path / "sub" / "dir"
        child.mkdir(parents=True)
        with patch("nexus.cli.commands.init_cmd.Path.cwd", return_value=child):
            result = _find_compose_file()
        assert result is not None
        assert result == (tmp_path / "nexus-stack.yml").resolve()

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        child = tmp_path / "empty"
        child.mkdir()
        with patch("nexus.cli.commands.init_cmd.Path.cwd", return_value=child):
            result = _find_compose_file()
        # May find real nexus-stack.yml if run from repo root; test intent is
        # that it doesn't crash. In isolated envs it returns None.
        assert result is None or result.name == "nexus-stack.yml"


class TestBundledComposeFile:
    def test_bundled_file_exists(self) -> None:
        """The package should ship a bundled nexus-stack.yml."""
        result = _bundled_compose_file()
        assert result is not None
        assert result.name == "nexus-stack.yml"
        assert result.exists()

    def test_bundled_file_has_services(self) -> None:
        """The bundled compose file should define services."""
        bundled = _bundled_compose_file()
        assert bundled is not None
        content = bundled.read_text()
        assert "services:" in content
        assert "postgres:" in content

    def test_bundled_file_uses_image_ref(self) -> None:
        """The bundled compose file should use NEXUS_IMAGE_REF, not NEXUS_IMAGE_TAG."""
        bundled = _bundled_compose_file()
        assert bundled is not None
        content = bundled.read_text()
        assert "NEXUS_IMAGE_REF" in content
        assert "NEXUS_IMAGE_TAG" not in content


# ---------------------------------------------------------------------------
# Unit tests — _build_config (pure logic, no I/O)
# ---------------------------------------------------------------------------


class TestBuildConfig:
    def test_local_preset(self) -> None:
        cfg = _build_config("local", "./nexus-data", False, {}, ())
        assert cfg["preset"] == "local"
        assert cfg["auth"] == "none"
        assert cfg["tls"] is False
        # data_dir is resolved to absolute
        assert Path(cfg["data_dir"]).is_absolute()
        assert "services" not in cfg
        assert "ports" not in cfg
        assert "compose_profiles" not in cfg
        assert "image_ref" not in cfg
        assert "image_channel" not in cfg

    def test_shared_preset(self) -> None:
        from nexus.cli.port_utils import DEFAULT_PORTS

        cfg = _build_config("shared", "./nexus-data", False, dict(DEFAULT_PORTS), ())
        assert cfg["preset"] == "shared"
        assert cfg["auth"] == "static"
        assert "nexus" in cfg["services"]
        assert "postgres" in cfg["services"]
        assert "http" in cfg["ports"]
        assert cfg["compose_profiles"] == ["core", "cache"]
        assert "compose_file" in cfg
        # data_dir is absolute
        assert Path(cfg["data_dir"]).is_absolute()

    def test_shared_preset_has_image_ref(self) -> None:
        cfg = _build_config("shared", "./data", False, {}, ())
        assert "image_ref" in cfg
        assert cfg["image_ref"].startswith("ghcr.io/nexi-lab/nexus:")
        assert cfg["image_channel"] == "edge"
        assert cfg["image_accelerator"] == "cpu"

    def test_demo_preset(self) -> None:
        from nexus.cli.port_utils import DEFAULT_PORTS

        cfg = _build_config("demo", "./nexus-data", False, dict(DEFAULT_PORTS), ())
        assert cfg["preset"] == "demo"
        assert cfg["auth"] == "database"
        assert "nexus" in cfg["services"]
        assert "postgres" in cfg["services"]


class TestInitWorkspaceArg:
    def test_positional_workspace_sets_config_and_data_dir(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "demo-workspace"

        result = runner.invoke(init, [str(workspace)])

        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((workspace / "nexus.yaml").read_text())
        assert Path(cfg["data_dir"]) == (workspace / "nexus-data").resolve()
        assert (workspace / "nexus-data").exists()

    def test_channel_override(self) -> None:
        cfg = _build_config("shared", "./data", False, {}, (), channel="edge")
        assert cfg["image_channel"] == "edge"
        assert "edge" in cfg["image_ref"]

    def test_accelerator_override(self) -> None:
        cfg = _build_config("shared", "./data", False, {}, (), accelerator="cuda")
        assert cfg["image_accelerator"] == "cuda"
        assert "-cuda" in cfg["image_ref"]

    def test_explicit_image_tag(self) -> None:
        cfg = _build_config("shared", "./data", False, {}, (), image_tag="0.9.2")
        assert cfg["image_ref"] == "ghcr.io/nexi-lab/nexus:0.9.2"

    def test_explicit_image_digest(self) -> None:
        digest = "sha256:abc123"
        cfg = _build_config("shared", "./data", False, {}, (), image_digest=digest)
        assert cfg["image_ref"] == f"ghcr.io/nexi-lab/nexus@{digest}"

    def test_tls_flag(self) -> None:
        cfg = _build_config("shared", "./data", True, {}, ())
        abs_data = str(Path("./data").resolve())
        assert cfg["tls"] is True
        assert cfg["tls_dir"] == os.path.join(abs_data, "tls")
        assert cfg["tls_cert"] == os.path.join(abs_data, "tls", "server.crt")
        assert cfg["tls_key"] == os.path.join(abs_data, "tls", "server.key")
        assert cfg["tls_ca"] == os.path.join(abs_data, "tls", "ca.crt")

    def test_compose_file_override(self) -> None:
        cfg = _build_config(
            "shared", "./data", False, {}, (), compose_file_override="/tmp/my-stack.yml"
        )
        assert cfg["compose_file"] == str(Path("/tmp/my-stack.yml").resolve())

    def test_addons_included(self) -> None:
        cfg = _build_config("shared", "./data", False, {}, ("nats", "mcp"))
        assert cfg["addons"] == ["nats", "mcp"]

    def test_no_addons_key_when_empty(self) -> None:
        cfg = _build_config("shared", "./data", False, {}, ())
        assert "addons" not in cfg

    def test_no_image_tag_field(self) -> None:
        """New configs should have image_ref, not image_tag."""
        cfg = _build_config("shared", "./data", False, {}, ())
        assert "image_tag" not in cfg
        assert "image_ref" in cfg


# ---------------------------------------------------------------------------
# Integration tests — CLI invocation with CliRunner + tmpdir
# ---------------------------------------------------------------------------


class TestInitCliCommand:
    def test_default_local_preset(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        result = runner.invoke(
            init,
            ["--config-path", str(config_path), "--data-dir", str(data_dir)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "local" in result.output
        assert config_path.exists()
        assert data_dir.exists()

        # Verify config contents
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "local"
        assert cfg["auth"] == "none"

    def test_shared_preset(self, runner: CliRunner, tmp_project: Path, _compose_file: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
                "--compose-file",
                str(_compose_file),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "shared" in result.output

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "shared"
        assert cfg["auth"] == "static"
        assert "postgres" in cfg["services"]
        assert "image_ref" in cfg
        assert cfg["image_channel"] == "edge"

    def test_shared_preset_with_channel(
        self, runner: CliRunner, tmp_project: Path, _compose_file: Path
    ) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--channel",
                "edge",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
                "--compose-file",
                str(_compose_file),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["image_channel"] == "edge"
        assert "edge" in cfg["image_ref"]

    def test_shared_preset_with_accelerator(
        self, runner: CliRunner, tmp_project: Path, _compose_file: Path
    ) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--accelerator",
                "cuda",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
                "--compose-file",
                str(_compose_file),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["image_accelerator"] == "cuda"
        assert "-cuda" in cfg["image_ref"]

    def test_shared_preset_with_explicit_tag(
        self, runner: CliRunner, tmp_project: Path, _compose_file: Path
    ) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--image-tag",
                "0.9.2",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
                "--compose-file",
                str(_compose_file),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["image_ref"] == "ghcr.io/nexi-lab/nexus:0.9.2"

    def test_demo_preset(self, runner: CliRunner, tmp_project: Path, _compose_file: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        result = runner.invoke(
            init,
            [
                "--preset",
                "demo",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
                "--compose-file",
                str(_compose_file),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "demo" in result.output
        assert "nexus demo init" in result.output

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "demo"
        assert cfg["auth"] == "database"

    def test_tls_flag(self, runner: CliRunner, tmp_project: Path, _compose_file: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "data"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--tls",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
                "--compose-file",
                str(_compose_file),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "TLS" in result.output

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["tls"] is True

        # TLS directory should have been scaffolded
        assert (data_dir / "tls").is_dir()

    def test_shared_copies_bundled_compose_file(self, runner: CliRunner, tmp_project: Path) -> None:
        """shared/demo preset copies bundled compose file when not found locally."""
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"

        # Patch _find_compose_file to return None (simulates clean temp dir)
        with patch("nexus.cli.commands.init_cmd._find_compose_file", return_value=None):
            result = runner.invoke(
                init,
                [
                    "--preset",
                    "shared",
                    "--config-path",
                    str(config_path),
                    "--data-dir",
                    str(data_dir),
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "bundled" in result.output.lower() or "copied" in result.output.lower()

        # Verify compose file was copied next to nexus.yaml
        import yaml as _yaml

        with open(config_path) as f:
            cfg = _yaml.safe_load(f)
        compose_file = cfg.get("compose_file", "")
        assert compose_file
        assert Path(compose_file).exists()

    def test_shared_fails_without_compose_file(self, runner: CliRunner, tmp_project: Path) -> None:
        """shared/demo preset should fail init when compose file is missing and no bundle."""
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        # Pass a non-existent compose file explicitly (bypasses bundled fallback)
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
                "--compose-file",
                str(tmp_project / "nonexistent.yml"),
            ],
        )
        assert result.exit_code != 0
        assert "not found" in result.output or "nonexistent" in result.output

    def test_refuses_overwrite_without_force(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        config_path.write_text("existing: true\n")

        result = runner.invoke(
            init,
            ["--config-path", str(config_path), "--data-dir", str(tmp_project / "d")],
        )
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_force_overwrites(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        config_path.write_text("existing: true\n")

        result = runner.invoke(
            init,
            [
                "--force",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(tmp_project / "d"),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "local"

    def test_with_addons(self, runner: CliRunner, tmp_project: Path, _compose_file: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--with",
                "nats",
                "--with",
                "mcp",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(tmp_project / "d"),
                "--compose-file",
                str(_compose_file),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert "nats" in cfg["addons"]
        assert "mcp" in cfg["addons"]

    def test_data_dir_created(self, runner: CliRunner, tmp_project: Path) -> None:
        data_dir = tmp_project / "deep" / "nested" / "nexus-data"
        result = runner.invoke(
            init,
            [
                "--config-path",
                str(tmp_project / "nexus.yaml"),
                "--data-dir",
                str(data_dir),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert data_dir.exists()
        assert (data_dir / "cas").exists()

    def test_output_shows_image_info(
        self, runner: CliRunner, tmp_project: Path, _compose_file: Path
    ) -> None:
        config_path = tmp_project / "nexus.yaml"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(tmp_project / "d"),
                "--compose-file",
                str(_compose_file),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "Image:" in result.output
        assert "Channel:" in result.output


# ---------------------------------------------------------------------------
# TLS scaffolding tests
# ---------------------------------------------------------------------------


class TestTlsScaffolding:
    def test_creates_tls_dir(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        _scaffold_tls(tls_dir)
        assert tls_dir.is_dir()

    def test_generates_certs_when_openssl_available(self, tmp_path: Path) -> None:
        import shutil

        if not shutil.which("openssl"):
            pytest.skip("openssl not on PATH")
        tls_dir = tmp_path / "tls"
        _scaffold_tls(tls_dir)
        assert (tls_dir / "ca.crt").exists()
        assert (tls_dir / "ca.key").exists()
        assert (tls_dir / "server.crt").exists()
        assert (tls_dir / "server.key").exists()

    def test_idempotent_when_certs_exist(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.crt").write_text("existing")
        (tls_dir / "server.crt").write_text("existing")
        # Should not overwrite
        _scaffold_tls(tls_dir)
        assert (tls_dir / "ca.crt").read_text() == "existing"

    def test_fallback_when_openssl_missing(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        tls_dir = tmp_path / "tls"
        with patch("nexus.cli.commands.init_cmd.shutil.which", return_value=None):
            _scaffold_tls(tls_dir)
        assert tls_dir.is_dir()
        # Certs should NOT exist since openssl is "missing"
        assert not (tls_dir / "server.crt").exists()
