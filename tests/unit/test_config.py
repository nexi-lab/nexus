"""Unit tests for nexus.config module."""

import os
from pathlib import Path

import pytest
import yaml

from nexus.config import (
    NexusConfig,
    _auto_discover,
    _load_from_dict,
    _load_from_environment,
    _load_from_file,
    load_config,
)


class TestNexusConfig:
    """Tests for NexusConfig model."""

    def test_default_values(self) -> None:
        """Test that default values are set correctly."""
        config = NexusConfig()

        assert config.mode == "embedded"
        assert config.data_dir == "./nexus-data"
        assert config.cache_size_mb == 100
        assert config.enable_vector_search is True
        assert config.enable_llm_cache is True
        assert config.db_path is None
        assert config.url is None
        assert config.api_key is None
        assert config.timeout == 30.0

    def test_custom_values(self) -> None:
        """Test that custom values can be set."""
        config = NexusConfig(
            mode="monolithic",
            data_dir="/tmp/nexus",
            cache_size_mb=200,
            enable_vector_search=False,
            enable_llm_cache=False,
            db_path="/tmp/nexus.db",
            url="http://localhost:8000",
            api_key="test-key",
            timeout=60.0,
        )

        assert config.mode == "monolithic"
        assert config.data_dir == "/tmp/nexus"
        assert config.cache_size_mb == 200
        assert config.enable_vector_search is False
        assert config.enable_llm_cache is False
        assert config.db_path == "/tmp/nexus.db"
        assert config.url == "http://localhost:8000"
        assert config.api_key == "test-key"
        assert config.timeout == 60.0

    def test_mode_validation_embedded(self) -> None:
        """Test mode validation for embedded mode."""
        config = NexusConfig(mode="embedded")
        assert config.mode == "embedded"

    def test_mode_validation_monolithic(self) -> None:
        """Test mode validation for monolithic mode."""
        config = NexusConfig(mode="monolithic", url="http://localhost:8000")
        assert config.mode == "monolithic"

    def test_mode_validation_distributed(self) -> None:
        """Test mode validation for distributed mode."""
        config = NexusConfig(mode="distributed", url="http://localhost:8000")
        assert config.mode == "distributed"

    def test_mode_validation_invalid(self) -> None:
        """Test that invalid mode raises ValueError."""
        with pytest.raises(ValueError, match="mode must be one of"):
            NexusConfig(mode="invalid")

    def test_url_provided_for_monolithic(self) -> None:
        """Test that URL can be provided for monolithic mode."""
        config = NexusConfig(mode="monolithic", url="http://localhost:8000")
        assert config.url == "http://localhost:8000"

    def test_url_provided_for_distributed(self) -> None:
        """Test that URL can be provided for distributed mode."""
        config = NexusConfig(mode="distributed", url="http://localhost:8000")
        assert config.url == "http://localhost:8000"

    def test_url_not_required_for_embedded(self) -> None:
        """Test that URL is not required for embedded mode."""
        config = NexusConfig(mode="embedded")
        assert config.url is None

    def test_config_is_mutable(self) -> None:
        """Test that config can be modified after creation."""
        config = NexusConfig()
        config.mode = "monolithic"
        config.url = "http://localhost:8000"

        assert config.mode == "monolithic"
        assert config.url == "http://localhost:8000"

    def test_backend_validation_local(self) -> None:
        """Test backend validation for local backend."""
        config = NexusConfig(backend="local")
        assert config.backend == "local"

    def test_backend_validation_gcs(self) -> None:
        """Test backend validation for GCS backend."""
        config = NexusConfig(backend="gcs", gcs_bucket_name="my-bucket")
        assert config.backend == "gcs"

    def test_backend_validation_invalid(self) -> None:
        """Test that invalid backend raises ValueError."""
        with pytest.raises(ValueError, match="backend must be one of"):
            NexusConfig(backend="invalid")


class TestLoadConfig:
    """Tests for load_config function."""

    def test_passthrough_nexus_config(self) -> None:
        """Test that NexusConfig is passed through unchanged."""
        original = NexusConfig(mode="embedded", cache_size_mb=200)
        result = load_config(original)

        assert result is original
        assert result.mode == "embedded"
        assert result.cache_size_mb == 200

    def test_load_from_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading from dictionary."""
        # Clear environment to avoid interference
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        config_dict = {"mode": "embedded", "cache_size_mb": 150}
        result = load_config(config_dict)

        assert isinstance(result, NexusConfig)
        assert result.mode == "embedded"
        assert result.cache_size_mb == 150

    def test_load_from_string_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading from string path."""
        # Clear environment to avoid interference
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_dict = {"mode": "embedded", "cache_size_mb": 175}

        with open(config_file, "w") as f:
            yaml.dump(config_dict, f)

        result = load_config(str(config_file))

        assert isinstance(result, NexusConfig)
        assert result.mode == "embedded"
        assert result.cache_size_mb == 175

    def test_load_from_path_object(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading from Path object."""
        # Clear environment to avoid interference
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_dict = {"mode": "embedded", "cache_size_mb": 125}

        with open(config_file, "w") as f:
            yaml.dump(config_dict, f)

        result = load_config(config_file)

        assert isinstance(result, NexusConfig)
        assert result.mode == "embedded"
        assert result.cache_size_mb == 125

    def test_load_auto_discover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test auto-discovery when no config provided."""
        # Set environment variable for testing
        monkeypatch.setenv("NEXUS_MODE", "embedded")
        monkeypatch.setenv("NEXUS_CACHE_SIZE_MB", "180")

        result = load_config(None)

        assert isinstance(result, NexusConfig)
        assert result.mode == "embedded"
        assert result.cache_size_mb == 180


class TestLoadFromDict:
    """Tests for _load_from_dict function."""

    def test_simple_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading from simple dictionary."""
        # Clear environment
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        config_dict = {"mode": "embedded", "cache_size_mb": 250}
        result = _load_from_dict(config_dict)

        assert result.mode == "embedded"
        assert result.cache_size_mb == 250

    def test_merge_with_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that dict values override environment values."""
        monkeypatch.setenv("NEXUS_MODE", "embedded")
        monkeypatch.setenv("NEXUS_CACHE_SIZE_MB", "100")

        config_dict = {"cache_size_mb": 300}
        result = _load_from_dict(config_dict)

        # Dict value should override environment
        assert result.cache_size_mb == 300
        # Environment value should be used for non-overridden fields
        assert result.mode == "embedded"

    def test_empty_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading from empty dictionary."""
        # Clear environment
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        result = _load_from_dict({})

        # Should use defaults
        assert result.mode == "embedded"
        assert result.cache_size_mb == 100


class TestLoadFromFile:
    """Tests for _load_from_file function."""

    def test_load_yaml_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading from YAML file."""
        # Clear environment
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_dict = {"mode": "embedded", "cache_size_mb": 400, "enable_vector_search": False}

        with open(config_file, "w") as f:
            yaml.dump(config_dict, f)

        result = _load_from_file(config_file)

        assert result.mode == "embedded"
        assert result.cache_size_mb == 400
        assert result.enable_vector_search is False

    def test_load_yml_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading from YML file."""
        # Clear environment
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yml"
        config_dict = {"mode": "embedded", "cache_size_mb": 350}

        with open(config_file, "w") as f:
            yaml.dump(config_dict, f)

        result = _load_from_file(config_file)

        assert result.mode == "embedded"
        assert result.cache_size_mb == 350

    def test_file_not_found(self) -> None:
        """Test that FileNotFoundError is raised for missing file."""
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            _load_from_file(Path("/nonexistent/config.yaml"))

    def test_unsupported_format(self, tmp_path: Path) -> None:
        """Test that ValueError is raised for unsupported file format."""
        config_file = tmp_path / "config.json"
        config_file.write_text("{}")

        with pytest.raises(ValueError, match="Unsupported config file format"):
            _load_from_file(config_file)


class TestLoadFromEnvironment:
    """Tests for _load_from_environment function."""

    def test_mode_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading mode from environment."""
        monkeypatch.setenv("NEXUS_MODE", "monolithic")
        result = _load_from_environment()
        assert result.mode == "monolithic"

    def test_data_dir_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading data_dir from environment."""
        monkeypatch.setenv("NEXUS_DATA_DIR", "/tmp/test-data")
        result = _load_from_environment()
        assert result.data_dir == "/tmp/test-data"

    def test_cache_size_mb_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading cache_size_mb from environment."""
        monkeypatch.setenv("NEXUS_CACHE_SIZE_MB", "500")
        result = _load_from_environment()
        assert result.cache_size_mb == 500

    def test_enable_vector_search_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading enable_vector_search as true from environment."""
        for value in ["true", "True", "TRUE", "1", "yes", "on"]:
            monkeypatch.setenv("NEXUS_ENABLE_VECTOR_SEARCH", value)
            result = _load_from_environment()
            assert result.enable_vector_search is True, f"Failed for value: {value}"

    def test_enable_vector_search_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading enable_vector_search as false from environment."""
        for value in ["false", "False", "FALSE", "0", "no", "off"]:
            monkeypatch.setenv("NEXUS_ENABLE_VECTOR_SEARCH", value)
            result = _load_from_environment()
            assert result.enable_vector_search is False, f"Failed for value: {value}"

    def test_enable_llm_cache_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading enable_llm_cache as true from environment."""
        monkeypatch.setenv("NEXUS_ENABLE_LLM_CACHE", "true")
        result = _load_from_environment()
        assert result.enable_llm_cache is True

    def test_enable_llm_cache_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading enable_llm_cache as false from environment."""
        monkeypatch.setenv("NEXUS_ENABLE_LLM_CACHE", "false")
        result = _load_from_environment()
        assert result.enable_llm_cache is False

    def test_db_path_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading db_path from environment."""
        monkeypatch.setenv("NEXUS_DB_PATH", "/tmp/test.db")
        result = _load_from_environment()
        assert result.db_path == "/tmp/test.db"

    def test_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading url from environment."""
        monkeypatch.setenv("NEXUS_URL", "http://localhost:8000")
        result = _load_from_environment()
        assert result.url == "http://localhost:8000"

    def test_api_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading api_key from environment."""
        monkeypatch.setenv("NEXUS_API_KEY", "secret-key-123")
        result = _load_from_environment()
        assert result.api_key == "secret-key-123"

    def test_timeout_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading timeout from environment."""
        monkeypatch.setenv("NEXUS_TIMEOUT", "45.5")
        result = _load_from_environment()
        assert result.timeout == 45.5

    def test_all_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading all environment variables at once."""
        monkeypatch.setenv("NEXUS_MODE", "distributed")
        monkeypatch.setenv("NEXUS_DATA_DIR", "/data")
        monkeypatch.setenv("NEXUS_CACHE_SIZE_MB", "512")
        monkeypatch.setenv("NEXUS_ENABLE_VECTOR_SEARCH", "false")
        monkeypatch.setenv("NEXUS_ENABLE_LLM_CACHE", "false")
        monkeypatch.setenv("NEXUS_DB_PATH", "/data/db.sqlite")
        monkeypatch.setenv("NEXUS_URL", "http://nexus:9000")
        monkeypatch.setenv("NEXUS_API_KEY", "test-key")
        monkeypatch.setenv("NEXUS_TIMEOUT", "60.0")

        result = _load_from_environment()

        assert result.mode == "distributed"
        assert result.data_dir == "/data"
        assert result.cache_size_mb == 512
        assert result.enable_vector_search is False
        assert result.enable_llm_cache is False
        assert result.db_path == "/data/db.sqlite"
        assert result.url == "http://nexus:9000"
        assert result.api_key == "test-key"
        assert result.timeout == 60.0

    def test_empty_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading with no environment variables set."""
        # Clear all NEXUS_* environment variables
        for key in list(os.environ.keys()):
            if key.startswith("NEXUS_"):
                monkeypatch.delenv(key, raising=False)

        result = _load_from_environment()

        # Should use defaults
        assert result.mode == "embedded"
        assert result.data_dir == "./nexus-data"
        assert result.cache_size_mb == 100
        assert result.enable_vector_search is True
        assert result.enable_llm_cache is True


class TestAutoDiscover:
    """Tests for _auto_discover function."""

    def test_discover_nexus_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test discovery of nexus.yaml in current directory."""
        # Clear environment
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        config_file = tmp_path / "nexus.yaml"
        config_dict = {"mode": "embedded", "cache_size_mb": 600}

        with open(config_file, "w") as f:
            yaml.dump(config_dict, f)

        result = _auto_discover()

        assert result.mode == "embedded"
        assert result.cache_size_mb == 600

    def test_discover_nexus_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test discovery of nexus.yml in current directory."""
        # Clear environment
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        config_file = tmp_path / "nexus.yml"
        config_dict = {"mode": "embedded", "cache_size_mb": 650}

        with open(config_file, "w") as f:
            yaml.dump(config_dict, f)

        result = _auto_discover()

        assert result.mode == "embedded"
        assert result.cache_size_mb == 650

    def test_discover_home_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test discovery of ~/.nexus/config.yaml."""
        # Clear environment
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        # Change to temp directory without config files
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        monkeypatch.chdir(work_dir)

        # Mock home directory
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        nexus_dir = home_dir / ".nexus"
        nexus_dir.mkdir()

        monkeypatch.setattr(Path, "home", lambda: home_dir)

        config_file = nexus_dir / "config.yaml"
        config_dict = {"mode": "embedded", "cache_size_mb": 700}

        with open(config_file, "w") as f:
            yaml.dump(config_dict, f)

        result = _auto_discover()

        assert result.mode == "embedded"
        assert result.cache_size_mb == 700

    def test_discover_priority_nexus_yaml_over_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that nexus.yaml takes priority over nexus.yml."""
        # Clear environment
        for key in ["NEXUS_MODE", "NEXUS_DATA_DIR", "NEXUS_URL"]:
            monkeypatch.delenv(key, raising=False)

        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        # Create both files
        yaml_file = tmp_path / "nexus.yaml"
        yml_file = tmp_path / "nexus.yml"

        with open(yaml_file, "w") as f:
            yaml.dump({"cache_size_mb": 800}, f)

        with open(yml_file, "w") as f:
            yaml.dump({"cache_size_mb": 900}, f)

        result = _auto_discover()

        # Should use nexus.yaml (800, not 900)
        assert result.cache_size_mb == 800

    def test_fallback_to_environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test fallback to environment when no config files found."""
        # Change to temp directory without config files
        monkeypatch.chdir(tmp_path)

        # Mock home directory without config
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home_dir)

        # Set environment variables
        monkeypatch.setenv("NEXUS_MODE", "embedded")
        monkeypatch.setenv("NEXUS_CACHE_SIZE_MB", "999")

        result = _auto_discover()

        assert result.mode == "embedded"
        assert result.cache_size_mb == 999

    def test_fallback_to_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test fallback to defaults when no config files or env vars found."""
        # Clear environment
        for key in list(os.environ.keys()):
            if key.startswith("NEXUS_"):
                monkeypatch.delenv(key, raising=False)

        # Change to temp directory without config files
        monkeypatch.chdir(tmp_path)

        # Mock home directory without config
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home_dir)

        result = _auto_discover()

        # Should use all defaults
        assert result.mode == "embedded"
        assert result.data_dir == "./nexus-data"
        assert result.cache_size_mb == 100
        assert result.enable_vector_search is True
        assert result.enable_llm_cache is True


class TestDockerImageTemplate:
    """Tests for DockerImageTemplate model."""

    def test_with_image(self) -> None:
        """Test template with pre-built image."""
        from nexus.config import DockerImageTemplate

        template = DockerImageTemplate(image="python:3.11")
        assert template.image == "python:3.11"
        assert template.dockerfile is None
        assert template.context == "."

    def test_with_dockerfile(self) -> None:
        """Test template with Dockerfile."""
        from nexus.config import DockerImageTemplate

        template = DockerImageTemplate(dockerfile="docker/Dockerfile.custom", context="docker")
        assert template.dockerfile == "docker/Dockerfile.custom"
        assert template.context == "docker"
        assert template.image is None

    def test_with_dockerfile_override(self) -> None:
        """Test template with inline Dockerfile override."""
        from nexus.config import DockerImageTemplate

        override = "FROM python:3.11\nRUN pip install numpy"
        template = DockerImageTemplate(image="python:3.11", dockerfile_override=override)
        assert template.image == "python:3.11"
        assert template.dockerfile_override == override


class TestDockerTemplateConfig:
    """Tests for DockerTemplateConfig model."""

    def test_default_values(self) -> None:
        """Test default template configuration."""
        from nexus.config import DockerTemplateConfig

        config = DockerTemplateConfig()
        assert config.templates == {}
        assert config.default_image == "nexus-sandbox:latest"

    def test_with_templates(self) -> None:
        """Test template configuration with custom templates."""
        from nexus.config import DockerImageTemplate, DockerTemplateConfig

        templates = {
            "python": DockerImageTemplate(image="python:3.11"),
            "node": DockerImageTemplate(image="node:20"),
        }
        config = DockerTemplateConfig(templates=templates)
        assert len(config.templates) == 2
        assert config.templates["python"].image == "python:3.11"
        assert config.templates["node"].image == "node:20"

    def test_custom_default_image(self) -> None:
        """Test custom default image."""
        from nexus.config import DockerTemplateConfig

        config = DockerTemplateConfig(default_image="custom:latest")
        assert config.default_image == "custom:latest"


class TestFeaturesConfig:
    """Tests for FeaturesConfig model."""

    def test_default_values(self) -> None:
        """Test default feature flags."""
        from nexus.config import FeaturesConfig

        config = FeaturesConfig()
        assert config.semantic_search is False
        assert config.llm_read is False
        assert config.agent_memory is True
        assert config.job_system is False
        assert config.mcp_server is True

    def test_custom_values(self) -> None:
        """Test custom feature flags."""
        from nexus.config import FeaturesConfig

        config = FeaturesConfig(
            semantic_search=True,
            llm_read=True,
            agent_memory=False,
            job_system=True,
            mcp_server=False,
        )
        assert config.semantic_search is True
        assert config.llm_read is True
        assert config.agent_memory is False
        assert config.job_system is True
        assert config.mcp_server is False

    def test_extra_fields_forbidden(self) -> None:
        """Test that extra fields raise validation error."""
        from pydantic import ValidationError

        from nexus.config import FeaturesConfig

        with pytest.raises(ValidationError):
            FeaturesConfig(unknown_feature=True)


class TestNexusConfigAdvanced:
    """Advanced tests for NexusConfig with nested models."""

    def test_with_docker_config(self) -> None:
        """Test NexusConfig with Docker templates."""
        from nexus.config import DockerImageTemplate, DockerTemplateConfig

        docker_config = DockerTemplateConfig(
            templates={"python": DockerImageTemplate(image="python:3.11")},
            default_image="custom:latest",
        )
        config = NexusConfig(docker=docker_config)
        assert config.docker.default_image == "custom:latest"
        assert "python" in config.docker.templates

    def test_with_features_config(self) -> None:
        """Test NexusConfig with feature flags."""
        from nexus.config import FeaturesConfig

        features = FeaturesConfig(semantic_search=True, llm_read=True)
        config = NexusConfig(features=features)
        assert config.features.semantic_search is True
        assert config.features.llm_read is True

    def test_gcs_backend_with_bucket(self) -> None:
        """Test that GCS backend works with bucket provided."""
        config = NexusConfig(backend="gcs", gcs_bucket_name="my-bucket")
        assert config.backend == "gcs"
        assert config.gcs_bucket_name == "my-bucket"

    def test_monolithic_mode_with_url(self) -> None:
        """Test that monolithic mode works with URL provided."""
        config = NexusConfig(mode="monolithic", url="http://localhost:8000")
        assert config.mode == "monolithic"
        assert config.url == "http://localhost:8000"

    def test_distributed_mode_with_url(self) -> None:
        """Test that distributed mode works with URL provided."""
        config = NexusConfig(mode="distributed", url="http://localhost:9000")
        assert config.mode == "distributed"
        assert config.url == "http://localhost:9000"

    def test_parsers_list(self) -> None:
        """Test NexusConfig with parsers list."""
        parsers = [{"module": "my_parsers", "class": "CSVParser", "priority": 60, "enabled": True}]
        config = NexusConfig(parsers=parsers)
        assert config.parsers == parsers

    def test_namespaces_list(self) -> None:
        """Test NexusConfig with namespaces list."""
        namespaces = [{"name": "private", "readonly": False, "admin_only": True}]
        config = NexusConfig(namespaces=namespaces)
        assert config.namespaces == namespaces

    def test_workspaces_and_memories(self) -> None:
        """Test NexusConfig with workspaces and memories."""
        workspaces = [{"path": "/workspace", "name": "Main", "created_by": "admin"}]
        memories = [{"path": "/memory", "name": "Context", "created_by": "agent"}]
        config = NexusConfig(workspaces=workspaces, memories=memories)
        assert config.workspaces == workspaces
        assert config.memories == memories

    def test_backends_list(self) -> None:
        """Test NexusConfig with multiple backends."""
        backends = [
            {"type": "local", "mount_point": "/local", "priority": 1},
            {"type": "gcs", "mount_point": "/cloud", "priority": 2, "readonly": True},
        ]
        config = NexusConfig(backends=backends)
        assert config.backends == backends

    def test_identity_settings(self) -> None:
        """Test NexusConfig with identity settings."""
        config = NexusConfig(tenant_id="tenant-123", user_id="user-456", agent_id="agent-789")
        assert config.tenant_id == "tenant-123"
        assert config.user_id == "user-456"
        assert config.agent_id == "agent-789"

    def test_permission_settings(self) -> None:
        """Test NexusConfig with permission settings."""
        config = NexusConfig(
            enforce_permissions=False,
            allow_admin_bypass=False,
            is_admin=True,
        )
        assert config.enforce_permissions is False
        assert config.allow_admin_bypass is False
        assert config.is_admin is True

    def test_cache_settings(self) -> None:
        """Test NexusConfig with cache settings."""
        config = NexusConfig(
            enable_metadata_cache=True,
            cache_path_size=1024,
            cache_list_size=256,
            cache_kv_size=512,
            cache_exists_size=2048,
            cache_ttl_seconds=600,
        )
        assert config.enable_metadata_cache is True
        assert config.cache_path_size == 1024
        assert config.cache_list_size == 256
        assert config.cache_kv_size == 512
        assert config.cache_exists_size == 2048
        assert config.cache_ttl_seconds == 600

    def test_cache_ttl_none(self) -> None:
        """Test NexusConfig with no cache TTL."""
        config = NexusConfig(cache_ttl_seconds=None)
        assert config.cache_ttl_seconds is None

    def test_workflow_settings(self) -> None:
        """Test NexusConfig with workflow settings."""
        config = NexusConfig(enable_workflows=False)
        assert config.enable_workflows is False

    def test_auto_parse_setting(self) -> None:
        """Test NexusConfig with auto_parse setting."""
        config = NexusConfig(auto_parse=False)
        assert config.auto_parse is False

    def test_gcs_settings(self) -> None:
        """Test NexusConfig with GCS settings."""
        config = NexusConfig(
            backend="gcs",
            gcs_bucket_name="my-bucket",
            gcs_project_id="my-project",
            gcs_credentials_path="/path/to/creds.json",
        )
        assert config.backend == "gcs"
        assert config.gcs_bucket_name == "my-bucket"
        assert config.gcs_project_id == "my-project"
        assert config.gcs_credentials_path == "/path/to/creds.json"


class TestLoadFromEnvironmentAdvanced:
    """Advanced tests for _load_from_environment with more fields."""

    def test_parsers_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading parsers from environment variable."""
        monkeypatch.setenv(
            "NEXUS_PARSERS", "my_parsers.csv:CSVParser:60,my_parsers.log:LogParser:50"
        )
        result = _load_from_environment()

        assert result.parsers is not None
        assert len(result.parsers) == 2
        assert result.parsers[0]["module"] == "my_parsers.csv"
        assert result.parsers[0]["class"] == "CSVParser"
        assert result.parsers[0]["priority"] == 60
        assert result.parsers[1]["module"] == "my_parsers.log"
        assert result.parsers[1]["class"] == "LogParser"
        assert result.parsers[1]["priority"] == 50

    def test_parsers_from_env_no_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading parsers without priority from environment."""
        monkeypatch.setenv("NEXUS_PARSERS", "my_parsers.csv:CSVParser")
        result = _load_from_environment()

        assert result.parsers is not None
        assert len(result.parsers) == 1
        assert result.parsers[0]["module"] == "my_parsers.csv"
        assert result.parsers[0]["class"] == "CSVParser"
        assert "priority" not in result.parsers[0]

    def test_cache_ttl_none_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading cache_ttl_seconds as None from environment."""
        monkeypatch.setenv("NEXUS_CACHE_TTL_SECONDS", "none")
        result = _load_from_environment()
        assert result.cache_ttl_seconds is None

    def test_cache_settings_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading cache settings from environment."""
        monkeypatch.setenv("NEXUS_ENABLE_METADATA_CACHE", "true")
        monkeypatch.setenv("NEXUS_CACHE_PATH_SIZE", "1024")
        monkeypatch.setenv("NEXUS_CACHE_LIST_SIZE", "256")
        monkeypatch.setenv("NEXUS_CACHE_KV_SIZE", "512")
        monkeypatch.setenv("NEXUS_CACHE_EXISTS_SIZE", "2048")
        monkeypatch.setenv("NEXUS_CACHE_TTL_SECONDS", "600")

        result = _load_from_environment()

        assert result.enable_metadata_cache is True
        assert result.cache_path_size == 1024
        assert result.cache_list_size == 256
        assert result.cache_kv_size == 512
        assert result.cache_exists_size == 2048
        assert result.cache_ttl_seconds == 600

    def test_permission_flags_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading permission flags from environment."""
        monkeypatch.setenv("NEXUS_IS_ADMIN", "true")
        monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
        monkeypatch.setenv("NEXUS_ALLOW_ADMIN_BYPASS", "false")

        result = _load_from_environment()

        assert result.is_admin is True
        assert result.enforce_permissions is False
        assert result.allow_admin_bypass is False

    def test_identity_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading identity settings from environment."""
        monkeypatch.setenv("NEXUS_TENANT_ID", "tenant-123")
        monkeypatch.setenv("NEXUS_USER_ID", "user-456")
        monkeypatch.setenv("NEXUS_AGENT_ID", "agent-789")

        result = _load_from_environment()

        assert result.tenant_id == "tenant-123"
        assert result.user_id == "user-456"
        assert result.agent_id == "agent-789"

    def test_auto_parse_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading auto_parse from environment."""
        monkeypatch.setenv("NEXUS_AUTO_PARSE", "false")
        result = _load_from_environment()
        assert result.auto_parse is False

    def test_gcs_settings_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading GCS settings from environment."""
        monkeypatch.setenv("NEXUS_GCS_BUCKET_NAME", "my-bucket")
        monkeypatch.setenv("NEXUS_GCS_PROJECT_ID", "my-project")
        monkeypatch.setenv("NEXUS_GCS_CREDENTIALS_PATH", "/creds.json")

        result = _load_from_environment()

        assert result.gcs_bucket_name == "my-bucket"
        assert result.gcs_project_id == "my-project"
        assert result.gcs_credentials_path == "/creds.json"
