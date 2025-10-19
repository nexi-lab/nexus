"""Run Nexus HTTP server.

This module provides the entry point for running the Nexus HTTP server
with environment-based configuration for cloud deployments.

For Cloud Run deployments with GCS backend, the SQLite metadata database
is automatically synced to/from GCS to persist across container restarts.
"""

import atexit
import logging
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from nexus import NexusFS
from nexus.server.api import NexusHTTPServer
from nexus.server.auth import SigV4Validator, create_simple_credentials_store

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


class DatabaseSyncManager:
    """Manages SQLite database synchronization with GCS for Cloud Run persistence."""

    def __init__(self, gcs_bucket: str, local_db_path: Path, gcs_db_path: str = "nexus-metadata.db"):
        """Initialize database sync manager.

        Args:
            gcs_bucket: GCS bucket name
            local_db_path: Local path for SQLite database
            gcs_db_path: Path in GCS bucket for database backup
        """
        self.gcs_bucket = gcs_bucket
        self.local_db_path = local_db_path
        self.gcs_db_path = gcs_db_path
        self.sync_interval = 60  # Sync every 60 seconds
        self.running = False
        self.sync_thread = None
        self._storage_client = None

    @property
    def storage_client(self):
        """Lazy initialization of GCS client."""
        if self._storage_client is None:
            from google.cloud import storage
            self._storage_client = storage.Client()
        return self._storage_client

    def download_from_gcs(self) -> bool:
        """Download database from GCS if it exists.

        Returns:
            True if downloaded successfully, False if doesn't exist
        """
        try:
            bucket = self.storage_client.bucket(self.gcs_bucket)
            blob = bucket.blob(self.gcs_db_path)

            if blob.exists():
                logger.info(f"Downloading database from gs://{self.gcs_bucket}/{self.gcs_db_path}")
                self.local_db_path.parent.mkdir(parents=True, exist_ok=True)
                blob.download_to_filename(str(self.local_db_path))
                logger.info(f"Database downloaded successfully to {self.local_db_path}")
                return True
            else:
                logger.info("No existing database found in GCS, starting fresh")
                return False
        except Exception as e:
            logger.error(f"Failed to download database from GCS: {e}")
            return False

    def upload_to_gcs(self) -> None:
        """Upload database to GCS."""
        try:
            if not self.local_db_path.exists():
                logger.warning(f"Local database {self.local_db_path} does not exist, skipping upload")
                return

            bucket = self.storage_client.bucket(self.gcs_bucket)
            blob = bucket.blob(self.gcs_db_path)

            # Create a temporary copy to avoid locking issues
            temp_path = self.local_db_path.with_suffix(".db.tmp")
            shutil.copy2(self.local_db_path, temp_path)

            logger.info(f"Uploading database to gs://{self.gcs_bucket}/{self.gcs_db_path}")
            blob.upload_from_filename(str(temp_path))
            temp_path.unlink()
            logger.info("Database uploaded successfully")
        except Exception as e:
            logger.error(f"Failed to upload database to GCS: {e}")

    def start_periodic_sync(self) -> None:
        """Start background thread for periodic database sync."""
        self.running = True
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()
        logger.info(f"Started periodic database sync (every {self.sync_interval}s)")

    def _sync_loop(self) -> None:
        """Background sync loop."""
        while self.running:
            time.sleep(self.sync_interval)
            if self.running:  # Check again after sleep
                self.upload_to_gcs()

    def stop(self) -> None:
        """Stop periodic sync and perform final upload."""
        logger.info("Stopping database sync manager...")
        self.running = False
        if self.sync_thread:
            self.sync_thread.join(timeout=5)
        # Final upload on shutdown
        self.upload_to_gcs()
        logger.info("Database sync manager stopped")


def main() -> None:
    """Start the Nexus HTTP server."""
    # Get configuration from environment
    host = os.getenv("NEXUS_HOST", "0.0.0.0")
    port = int(os.getenv("NEXUS_PORT", "8080"))
    bucket_name = os.getenv("NEXUS_BUCKET", "nexus")

    # Authentication credentials
    access_key = os.getenv("NEXUS_ACCESS_KEY", "nexus-key")
    secret_key = os.getenv("NEXUS_SECRET_KEY", "nexus-secret")

    # Storage configuration
    storage_backend = os.getenv("NEXUS_STORAGE_BACKEND", "local")
    storage_path = os.getenv("NEXUS_STORAGE_PATH", "/tmp/nexus-data")

    # GCS configuration (if using GCS backend)
    gcs_bucket = os.getenv("NEXUS_GCS_BUCKET")
    gcs_project = os.getenv("NEXUS_GCS_PROJECT")
    gcs_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    # Database sync configuration
    enable_db_sync = os.getenv("NEXUS_DB_SYNC", "true").lower() == "true"
    db_sync_interval = int(os.getenv("NEXUS_DB_SYNC_INTERVAL", "60"))

    logger.info("Starting Nexus HTTP Server")
    logger.info(f"Storage backend: {storage_backend}")

    # Database sync manager (for GCS backend)
    db_sync_manager = None
    local_db_path = Path("/tmp/nexus-metadata.db")

    # Initialize Nexus filesystem
    try:
        if storage_backend == "gcs" and gcs_bucket:
            from nexus.backends.gcs import GCSBackend

            logger.info(f"Using GCS backend: {gcs_bucket}")
            if gcs_credentials:
                logger.info(f"GCS credentials: {gcs_credentials}")

            # Setup database sync to GCS
            if enable_db_sync:
                logger.info("Initializing database sync with GCS")
                db_sync_manager = DatabaseSyncManager(
                    gcs_bucket=gcs_bucket,
                    local_db_path=local_db_path,
                    gcs_db_path="metadata/nexus-metadata.db"
                )
                db_sync_manager.sync_interval = db_sync_interval

                # Download existing database from GCS if available
                db_sync_manager.download_from_gcs()

                # Start periodic sync
                db_sync_manager.start_periodic_sync()

                # Register cleanup handlers
                def cleanup_handler(signum=None, frame=None):
                    logger.info("Received shutdown signal, cleaning up...")
                    if db_sync_manager:
                        db_sync_manager.stop()
                    sys.exit(0)

                signal.signal(signal.SIGTERM, cleanup_handler)
                signal.signal(signal.SIGINT, cleanup_handler)
                atexit.register(cleanup_handler)
            else:
                logger.warning("Database sync disabled - metadata will not persist!")

            backend = GCSBackend(
                bucket_name=gcs_bucket,
                project_id=gcs_project,
                credentials_path=gcs_credentials if gcs_credentials else None,
            )
            nexus_fs = NexusFS(backend=backend, db_path=str(local_db_path))
        else:
            # Default to local filesystem
            logger.info(f"Using local storage: {storage_path}")
            storage_path_obj = Path(storage_path)
            storage_path_obj.mkdir(parents=True, exist_ok=True)
            from nexus.backends.local import LocalBackend
            backend = LocalBackend(root_path=storage_path_obj)
            db_path_local = storage_path_obj / "metadata.db"
            nexus_fs = NexusFS(backend=backend, db_path=str(db_path_local))

    except Exception as e:
        logger.error(f"Failed to initialize Nexus filesystem: {e}")
        if db_sync_manager:
            db_sync_manager.stop()
        sys.exit(1)

    # Create authentication validator
    credentials_store = create_simple_credentials_store(access_key, secret_key)
    auth_validator = SigV4Validator(credentials_store)

    logger.info(f"Access Key: {access_key}")
    logger.info("Secret Key: ****")

    # Create and start server
    try:
        server = NexusHTTPServer(
            nexus_fs=nexus_fs,
            auth_validator=auth_validator,
            host=host,
            port=port,
            bucket_name=bucket_name,
        )
        logger.info(f"Server ready at http://{host}:{port}")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
    finally:
        if db_sync_manager:
            db_sync_manager.stop()


if __name__ == "__main__":
    main()
