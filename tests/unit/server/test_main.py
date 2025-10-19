"""Unit tests for Nexus server __main__ module."""

import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from nexus.server.__main__ import DatabaseSyncManager


class TestDatabaseSyncManager:
    """Test DatabaseSyncManager class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.gcs_bucket = "test-bucket"
        self.local_db_path = Path("/tmp/test-nexus-metadata.db")
        self.gcs_db_path = "metadata/nexus-metadata.db"

    def test_init(self):
        """Test DatabaseSyncManager initialization."""
        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
            gcs_db_path=self.gcs_db_path,
        )

        assert manager.gcs_bucket == self.gcs_bucket
        assert manager.local_db_path == self.local_db_path
        assert manager.gcs_db_path == self.gcs_db_path
        assert manager.sync_interval == 60
        assert manager.running is False
        assert manager.sync_thread is None
        assert manager._storage_client is None

    @patch("google.cloud.storage.Client")
    def test_storage_client_lazy_init(self, mock_storage_client):
        """Test storage_client property lazy initialization."""
        mock_client = Mock()
        mock_storage_client.return_value = mock_client

        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
        )

        # First access should initialize
        client1 = manager.storage_client
        assert client1 == mock_client
        mock_storage_client.assert_called_once()

        # Second access should return cached client
        client2 = manager.storage_client
        assert client2 == mock_client
        mock_storage_client.assert_called_once()  # Still only called once

    @patch("google.cloud.storage.Client")
    def test_download_from_gcs_success(self, mock_storage_client):
        """Test successful database download from GCS."""
        # Setup mocks
        mock_client = Mock()
        mock_bucket = Mock()
        mock_blob = Mock()
        mock_blob.exists.return_value = True

        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_storage_client.return_value = mock_client

        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
        )

        # Test download
        result = manager.download_from_gcs()

        assert result is True
        mock_client.bucket.assert_called_with(self.gcs_bucket)
        mock_blob.download_to_filename.assert_called_once()

    @patch("google.cloud.storage.Client")
    def test_download_from_gcs_not_exists(self, mock_storage_client):
        """Test download when database doesn't exist in GCS."""
        # Setup mocks
        mock_client = Mock()
        mock_bucket = Mock()
        mock_blob = Mock()
        mock_blob.exists.return_value = False

        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_storage_client.return_value = mock_client

        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
        )

        # Test download
        result = manager.download_from_gcs()

        assert result is False
        mock_blob.download_to_filename.assert_not_called()

    @patch("google.cloud.storage.Client")
    def test_download_from_gcs_exception(self, mock_storage_client):
        """Test download handles exceptions gracefully."""
        # Setup mocks to raise exception
        mock_client = Mock()
        mock_client.bucket.side_effect = Exception("GCS error")
        mock_storage_client.return_value = mock_client

        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
        )

        # Test download
        result = manager.download_from_gcs()

        assert result is False

    @patch("nexus.server.__main__.shutil")
    @patch("google.cloud.storage.Client")
    def test_upload_to_gcs_success(self, mock_storage_client, mock_shutil):
        """Test successful database upload to GCS."""
        # Setup mocks
        mock_client = Mock()
        mock_bucket = Mock()
        mock_blob = Mock()

        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_storage_client.return_value = mock_client

        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
        )

        # Mock file existence
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "unlink"):
                manager.upload_to_gcs()

        mock_client.bucket.assert_called_with(self.gcs_bucket)
        mock_shutil.copy2.assert_called_once()
        mock_blob.upload_from_filename.assert_called_once()

    @patch("google.cloud.storage.Client")
    def test_upload_to_gcs_no_file(self, mock_storage_client):
        """Test upload when local database doesn't exist."""
        mock_client = Mock()
        mock_storage_client.return_value = mock_client

        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
        )

        # Mock file doesn't exist
        with patch.object(Path, "exists", return_value=False):
            manager.upload_to_gcs()

        # Should not attempt upload
        mock_client.bucket.assert_not_called()

    @patch("google.cloud.storage.Client")
    def test_start_periodic_sync(self, mock_storage_client):
        """Test starting periodic sync thread."""
        mock_client = Mock()
        mock_storage_client.return_value = mock_client

        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
        )

        manager.start_periodic_sync()

        assert manager.running is True
        assert manager.sync_thread is not None
        assert isinstance(manager.sync_thread, threading.Thread)
        assert manager.sync_thread.daemon is True

        # Clean up
        manager.stop()

    @patch("google.cloud.storage.Client")
    def test_stop_sync(self, mock_storage_client):
        """Test stopping periodic sync."""
        mock_client = Mock()
        mock_bucket = Mock()
        mock_blob = Mock()

        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_storage_client.return_value = mock_client

        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
        )

        # Start sync
        manager.start_periodic_sync()
        assert manager.running is True

        # Mock file existence for final upload
        with patch.object(Path, "exists", return_value=False):
            manager.stop()

        assert manager.running is False

    @patch("google.cloud.storage.Client")
    @patch("nexus.server.__main__.time")
    def test_sync_loop(self, mock_time, mock_storage_client):
        """Test sync loop behavior."""
        mock_client = Mock()
        mock_bucket = Mock()
        mock_blob = Mock()

        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_storage_client.return_value = mock_client

        manager = DatabaseSyncManager(
            gcs_bucket=self.gcs_bucket,
            local_db_path=self.local_db_path,
        )

        # Track how many times sleep is called
        sleep_count = 0

        def mock_sleep(duration):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:  # Stop after 2 iterations
                manager.running = False

        mock_time.sleep.side_effect = mock_sleep

        # Start sync loop in a controlled way
        manager.running = True
        with patch.object(Path, "exists", return_value=False):
            manager._sync_loop()

        # Should have called sleep at least twice
        assert sleep_count >= 2
        mock_time.sleep.assert_called_with(60)  # Default sync interval


class TestMainFunction:
    """Test main() function."""

    @patch("nexus.server.__main__.SigV4Validator")
    @patch("nexus.server.__main__.create_simple_credentials_store")
    @patch("nexus.backends.local.LocalBackend")
    @patch("nexus.server.__main__.NexusHTTPServer")
    @patch("nexus.server.__main__.NexusFS")
    @patch("nexus.server.__main__.os.getenv")
    def test_main_local_backend(
        self,
        mock_getenv,
        mock_nexus_fs,
        mock_server,
        mock_backend,
        mock_cred_store,
        mock_validator,
    ):
        """Test main() with local backend."""
        # Setup environment variables
        env_vars = {
            "NEXUS_HOST": "127.0.0.1",
            "NEXUS_PORT": "9000",
            "NEXUS_BUCKET": "test-bucket",
            "NEXUS_ACCESS_KEY": "test-key",
            "NEXUS_SECRET_KEY": "test-secret",
            "NEXUS_STORAGE_BACKEND": "local",
            "NEXUS_STORAGE_PATH": "/tmp/test-storage",
        }
        mock_getenv.side_effect = lambda key, default=None: env_vars.get(key, default)

        # Mock filesystem and server
        mock_fs_instance = Mock()
        mock_nexus_fs.return_value = mock_fs_instance
        mock_server_instance = Mock()
        mock_server.return_value = mock_server_instance
        mock_backend_instance = Mock()
        mock_backend.return_value = mock_backend_instance

        # Mock auth components
        mock_cred_store.return_value = Mock()
        mock_validator.return_value = Mock()

        # Mock serve_forever to avoid infinite loop
        mock_server_instance.serve_forever.side_effect = KeyboardInterrupt()

        # Patch Path.mkdir to avoid actual directory creation
        with patch.object(Path, "mkdir"):
            # Run main
            from nexus.server.__main__ import main

            # Main catches KeyboardInterrupt, so we just run it
            main()

            # Verify backend was created
            mock_backend.assert_called_once()
            # Verify NexusFS was created
            mock_nexus_fs.assert_called_once()
            # Verify server was created
            mock_server.assert_called_once()

    @patch("nexus.server.__main__.SigV4Validator")
    @patch("nexus.server.__main__.create_simple_credentials_store")
    @patch("nexus.backends.gcs.GCSBackend")
    @patch("nexus.server.__main__.DatabaseSyncManager")
    @patch("nexus.server.__main__.NexusHTTPServer")
    @patch("nexus.server.__main__.NexusFS")
    @patch("nexus.server.__main__.os.getenv")
    def test_main_gcs_backend_with_sync(
        self,
        mock_getenv,
        mock_nexus_fs,
        mock_server,
        mock_db_sync,
        mock_backend,
        mock_cred_store,
        mock_validator,
    ):
        """Test main() with GCS backend and database sync enabled."""
        # Setup environment variables
        env_vars = {
            "NEXUS_HOST": "0.0.0.0",
            "NEXUS_PORT": "8080",
            "NEXUS_BUCKET": "nexus",
            "NEXUS_ACCESS_KEY": "gcs-key",
            "NEXUS_SECRET_KEY": "gcs-secret",
            "NEXUS_STORAGE_BACKEND": "gcs",
            "NEXUS_GCS_BUCKET": "my-gcs-bucket",
            "NEXUS_GCS_PROJECT": "my-project",
            "NEXUS_DB_SYNC": "true",
            "NEXUS_DB_SYNC_INTERVAL": "30",
        }
        mock_getenv.side_effect = lambda key, default=None: env_vars.get(key, default)

        # Mock components
        mock_fs_instance = Mock()
        mock_nexus_fs.return_value = mock_fs_instance
        mock_server_instance = Mock()
        mock_server.return_value = mock_server_instance
        mock_sync_instance = Mock()
        mock_db_sync.return_value = mock_sync_instance
        mock_backend_instance = Mock()
        mock_backend.return_value = mock_backend_instance

        # Mock auth components
        mock_cred_store.return_value = Mock()
        mock_validator.return_value = Mock()

        # Mock serve_forever to avoid infinite loop
        mock_server_instance.serve_forever.side_effect = KeyboardInterrupt()

        # Run main
        from nexus.server.__main__ import main

        # Main catches KeyboardInterrupt, so we just run it
        main()

        # Verify database sync manager was created and started
        mock_db_sync.assert_called_once()
        mock_sync_instance.download_from_gcs.assert_called_once()
        mock_sync_instance.start_periodic_sync.assert_called_once()

        # Verify GCS backend was created
        mock_backend.assert_called_once()
        # Verify NexusFS was created
        mock_nexus_fs.assert_called_once()
        # Verify server was created
        mock_server.assert_called_once()
