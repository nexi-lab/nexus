"""Tests for demo seed functions — _seed_catalog and _seed_aspects (Issue #2930)."""

from unittest.mock import MagicMock, patch


class TestSeedCatalog:
    """Tests for _seed_catalog()."""

    def test_skips_if_already_seeded(self) -> None:
        from nexus.cli.commands.demo import _seed_catalog

        manifest = {"schemas_extracted": True}
        result = _seed_catalog(MagicMock(), {"ports": {"http": 2026}}, manifest)
        assert result == 0

    @patch("nexus.cli.api_client.NexusApiClient")
    def test_extracts_schemas_for_data_files(self, mock_client_cls) -> None:
        from nexus.cli.commands.demo import _seed_catalog

        mock_client = MagicMock()
        mock_client.get.return_value = {"schema": {"columns": []}}
        mock_client_cls.return_value = mock_client

        manifest: dict = {}
        config = {"ports": {"http": 2026}, "api_key": "test-key"}
        result = _seed_catalog(MagicMock(), config, manifest)

        assert result == 3  # 3 data files
        assert manifest["schemas_extracted"] is True
        assert mock_client.get.call_count == 3

    @patch("nexus.cli.api_client.NexusApiClient")
    def test_handles_api_errors_gracefully(self, mock_client_cls) -> None:
        from nexus.cli.commands.demo import _seed_catalog

        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("Connection refused")
        mock_client_cls.return_value = mock_client

        manifest: dict = {}
        config = {"ports": {"http": 2026}, "api_key": "test-key"}
        result = _seed_catalog(MagicMock(), config, manifest)

        assert result == 0
        assert manifest["schemas_extracted"] is False


class TestSeedAspects:
    """Tests for _seed_aspects()."""

    def test_skips_if_already_seeded(self) -> None:
        from nexus.cli.commands.demo import _seed_aspects

        manifest = {"aspects_created": True}
        result = _seed_aspects(MagicMock(), {"ports": {"http": 2026}}, manifest)
        assert result == 0

    @patch("nexus.cli.api_client.NexusApiClient")
    def test_creates_governance_aspect(self, mock_client_cls) -> None:
        from nexus.cli.commands.demo import _seed_aspects

        mock_client = MagicMock()
        mock_client.put.return_value = {
            "entity_urn": "test",
            "aspect_name": "governance.classification",
        }
        mock_client_cls.return_value = mock_client

        manifest: dict = {}
        config = {"ports": {"http": 2026}, "api_key": "test-key"}
        result = _seed_aspects(MagicMock(), config, manifest)

        assert result == 1
        assert manifest["aspects_created"] is True
        mock_client.put.assert_called_once()

    @patch("nexus.cli.api_client.NexusApiClient")
    def test_handles_api_errors_gracefully(self, mock_client_cls) -> None:
        from nexus.cli.commands.demo import _seed_aspects

        mock_client = MagicMock()
        mock_client.put.side_effect = Exception("Connection refused")
        mock_client_cls.return_value = mock_client

        manifest: dict = {}
        config = {"ports": {"http": 2026}, "api_key": "test-key"}
        result = _seed_aspects(MagicMock(), config, manifest)

        assert result == 0
        assert manifest["aspects_created"] is False
