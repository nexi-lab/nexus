"""Unit tests for MCPProviderRegistry."""

from pathlib import Path

import pytest
import yaml

# Import directly from the module to avoid nexus.mcp.__init__
# which requires fastmcp (not always installed).
from nexus.mcp.provider_registry import (
    BackendConfig,
    MCPConfig,
    MCPProviderRegistry,
    OAuthConfig,
    ProviderConfig,
    ProviderType,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> MCPProviderRegistry:
    return MCPProviderRegistry()


@pytest.fixture()
def sample_providers() -> dict[str, ProviderConfig]:
    return {
        "github": ProviderConfig(
            name="github",
            type=ProviderType.KLAVIS,
            display_name="GitHub",
            description="GitHub repos",
            klavis_name="github",
            default_scopes=["repo"],
        ),
        "local_tool": ProviderConfig(
            name="local_tool",
            type=ProviderType.LOCAL,
            display_name="Local Tool",
            description="A local MCP tool",
            mcp=MCPConfig(command="node", args=["server.js"]),
        ),
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestInit:
    def test_empty_init(self, registry: MCPProviderRegistry) -> None:
        assert len(registry) == 0
        assert registry.list_providers() == []

    def test_init_with_providers(self, sample_providers: dict) -> None:
        reg = MCPProviderRegistry(providers=sample_providers)
        assert len(reg) == 2
        assert reg.get("github") is not None
        assert reg.get("local_tool") is not None


# ---------------------------------------------------------------------------
# get / add / remove
# ---------------------------------------------------------------------------


class TestGetAddRemove:
    def test_get_existing(self, sample_providers: dict) -> None:
        reg = MCPProviderRegistry(providers=sample_providers)
        gh = reg.get("github")
        assert gh is not None
        assert gh.display_name == "GitHub"

    def test_get_missing(self, registry: MCPProviderRegistry) -> None:
        assert registry.get("nope") is None

    def test_add_provider(self, registry: MCPProviderRegistry) -> None:
        cfg = ProviderConfig(name="test", type=ProviderType.LOCAL, display_name="Test")
        registry.add_provider(cfg)
        assert registry.get("test") is cfg

    def test_remove_provider(self, sample_providers: dict) -> None:
        reg = MCPProviderRegistry(providers=sample_providers)
        assert reg.remove_provider("github") is True
        assert reg.get("github") is None
        assert len(reg) == 1

    def test_remove_missing(self, registry: MCPProviderRegistry) -> None:
        assert registry.remove_provider("nope") is False


# ---------------------------------------------------------------------------
# Listing / filtering
# ---------------------------------------------------------------------------


class TestListing:
    def test_list_providers(self, sample_providers: dict) -> None:
        reg = MCPProviderRegistry(providers=sample_providers)
        items = reg.list_providers()
        names = [name for name, _ in items]
        assert "github" in names
        assert "local_tool" in names

    def test_list_klavis_providers(self, sample_providers: dict) -> None:
        reg = MCPProviderRegistry(providers=sample_providers)
        klavis = reg.list_klavis_providers()
        assert len(klavis) == 1
        assert klavis[0][0] == "github"

    def test_list_local_providers(self, sample_providers: dict) -> None:
        reg = MCPProviderRegistry(providers=sample_providers)
        local = reg.list_local_providers()
        assert len(local) == 1
        assert local[0][0] == "local_tool"


# ---------------------------------------------------------------------------
# Builtin defaults
# ---------------------------------------------------------------------------


class TestBuiltinDefaults:
    def test_with_builtin_defaults(self) -> None:
        reg = MCPProviderRegistry.with_builtin_defaults()
        names = [n for n, _ in reg.list_providers()]
        assert "github" in names
        assert "slack" in names
        assert "notion" in names
        assert "linear" in names
        assert len(names) == 4


# ---------------------------------------------------------------------------
# YAML round-trip
# ---------------------------------------------------------------------------


class TestYaml:
    def test_from_yaml(self, tmp_path: Path) -> None:
        yaml_content = {
            "providers": {
                "test_provider": {
                    "type": "klavis",
                    "display_name": "Test",
                    "description": "Test provider",
                    "klavis_name": "test",
                    "default_scopes": ["read"],
                }
            }
        }
        yaml_file = tmp_path / "providers.yaml"
        yaml_file.write_text(yaml.dump(yaml_content))

        reg = MCPProviderRegistry.from_yaml(yaml_file)
        p = reg.get("test_provider")
        assert p is not None
        assert p.type == ProviderType.KLAVIS
        assert p.display_name == "Test"

    def test_from_yaml_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            MCPProviderRegistry.from_yaml("/nonexistent/path.yaml")

    def test_to_yaml_round_trip(self, sample_providers: dict, tmp_path: Path) -> None:
        reg = MCPProviderRegistry(providers=sample_providers)
        out_path = tmp_path / "out.yaml"
        reg.to_yaml(out_path)

        reg2 = MCPProviderRegistry.from_yaml(out_path)
        assert len(reg2) == len(reg)
        gh = reg2.get("github")
        assert gh is not None
        assert gh.klavis_name == "github"

    def test_from_yaml_empty_file(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")
        reg = MCPProviderRegistry.from_yaml(yaml_file)
        assert len(reg) == 0

    def test_from_yaml_with_oauth(self, tmp_path: Path) -> None:
        yaml_content = {
            "providers": {
                "gdrive": {
                    "type": "local",
                    "display_name": "Google Drive",
                    "oauth": {
                        "provider_class": "test.GoogleOAuth",
                        "client_id_env": "GOOGLE_ID",
                        "client_secret_env": "GOOGLE_SECRET",
                        "scopes": ["drive.readonly"],
                    },
                }
            }
        }
        yaml_file = tmp_path / "oauth.yaml"
        yaml_file.write_text(yaml.dump(yaml_content))

        reg = MCPProviderRegistry.from_yaml(yaml_file)
        p = reg.get("gdrive")
        assert p is not None
        assert p.oauth is not None
        assert p.oauth.client_id_env == "GOOGLE_ID"
        assert p.oauth.scopes == ["drive.readonly"]


# ---------------------------------------------------------------------------
# load_default fallback chain
# ---------------------------------------------------------------------------


class TestLoadDefault:
    def test_load_default_returns_registry(self) -> None:
        reg = MCPProviderRegistry.load_default()
        # Should return some registry (either from yaml or builtin defaults)
        assert isinstance(reg, MCPProviderRegistry)

    def test_load_default_env_var(self, tmp_path: Path) -> None:
        yaml_content = {
            "providers": {
                "env_test": {
                    "type": "klavis",
                    "display_name": "Env Test",
                }
            }
        }
        yaml_file = tmp_path / "env-providers.yaml"
        yaml_file.write_text(yaml.dump(yaml_content))

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("NEXUS_MCP_PROVIDERS_PATH", str(yaml_file))
            reg = MCPProviderRegistry.load_default()
            assert reg.get("env_test") is not None


# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_provider_config_from_dict(self) -> None:
        data = {
            "type": "klavis",
            "display_name": "Test",
            "description": "desc",
            "klavis_name": "test",
            "default_scopes": ["r"],
        }
        cfg = ProviderConfig.from_dict("test", data)
        assert cfg.name == "test"
        assert cfg.type == ProviderType.KLAVIS
        assert cfg.default_scopes == ["r"]

    def test_provider_config_to_dict(self) -> None:
        cfg = ProviderConfig(
            name="x",
            type=ProviderType.KLAVIS,
            display_name="X",
            description="desc",
            klavis_name="x_name",
        )
        d = cfg.to_dict()
        assert d["type"] == "klavis"
        assert d["klavis_name"] == "x_name"

    def test_oauth_config(self) -> None:
        oauth = OAuthConfig(
            provider_class="test.OAuth",
            client_id_env="ID_ENV",
            client_secret_env="SECRET_ENV",
            scopes=["read"],
        )
        assert oauth.provider_class == "test.OAuth"
        assert oauth.requires_pkce is False

    def test_mcp_config(self) -> None:
        mcp = MCPConfig(command="node", args=["server.js"])
        assert mcp.transport == "stdio"
        assert mcp.command == "node"

    def test_backend_config(self) -> None:
        bc = BackendConfig(type="gdrive_connector", config_template={"key": "val"})
        assert bc.type == "gdrive_connector"
