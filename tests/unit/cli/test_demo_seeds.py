"""Tests for demo seed functions — _seed_catalog and _seed_aspects (Issue #2930)."""

import asyncio
import base64
from unittest.mock import MagicMock, patch


class TestSeedFiles:
    """Tests for _seed_files()."""

    def test_uses_batch_write_when_available(self) -> None:
        from nexus.cli.commands.demo import _seed_files
        from nexus.cli.commands.demo_data import DEMO_FILES, HERB_CORPUS

        class BatchClient:
            def __init__(self) -> None:
                self.batch_files: list[tuple[str, bytes]] = []

            def access(self, _path: str) -> bool:
                return False

            def mkdir(self, _path: str, *, parents: bool, exist_ok: bool) -> None:
                assert parents is True
                assert exist_ok is True

            def write_batch(self, files: list[tuple[str, bytes]]) -> list[dict[str, object]]:
                self.batch_files = list(files)
                return [{"path": path, "content_id": "cid"} for path, _content in files]

            def write(self, _path: str, _content: bytes) -> None:
                raise AssertionError("sequential write fallback should not run")

        client = BatchClient()
        manifest: dict = {}

        created = asyncio.run(_seed_files(client, manifest))

        expected_paths = [path for path, _content, _desc in list(DEMO_FILES) + list(HERB_CORPUS)]
        assert created == len(expected_paths)
        assert [path for path, _content in client.batch_files] == expected_paths
        assert manifest["files"] == expected_paths

    @patch("nexus.cli.api_client.NexusApiClient")
    def test_rest_client_batch_write_encodes_payload(self, mock_client_cls) -> None:
        from nexus.cli.commands.demo import _RestApiNexusClient

        mock_client = MagicMock()
        mock_client.post.return_value = {"results": [{"path": "/workspace/demo/a.bin"}]}
        mock_client_cls.return_value = mock_client

        client = _RestApiNexusClient("http://localhost:2026", "test-key")
        result = client.write_batch([("/workspace/demo/a.bin", b"\x00demo")])

        assert result == [{"path": "/workspace/demo/a.bin"}]
        mock_client.post.assert_called_once()
        endpoint = mock_client.post.call_args.args[0]
        payload = mock_client.post.call_args.kwargs["json_body"]
        assert endpoint == "/api/v2/files/batch/write"
        assert payload == {
            "files": [
                {
                    "path": "/workspace/demo/a.bin",
                    "content_base64": base64.b64encode(b"\x00demo").decode("ascii"),
                }
            ]
        }


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
