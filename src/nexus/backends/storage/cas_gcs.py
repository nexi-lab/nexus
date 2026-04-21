"""Google Cloud Storage backend with CAS deduplication.

Thin subclass of CASAddressingEngine that:
- Creates a GCSTransport for raw GCS I/O
- Registers as "cas_gcs" connector via @register_connector
- Declares CONNECTION_ARGS for factory instantiation
- Overrides batch_read_content with GCS-optimized parallel downloads

Authentication (Recommended):
    Use service account credentials for production (no daily re-auth):
    1. Create service account: gcloud iam service-accounts create nexus-storage-sa
    2. Grant permissions: gcloud projects add-iam-policy-binding PROJECT_ID
    3. Download key: gcloud iam service-accounts keys create gcs-credentials.json
    4. Set GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcs-credentials.json

    Alternative (Development Only):
    - gcloud auth application-default login (requires daily re-authentication)
    - Compute Engine/Cloud Run service account (auto-detected)

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.base.runtime_deps import PythonDep
from nexus.contracts.exceptions import BackendError

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


@register_connector(
    "cas_gcs",
    description="Google Cloud Storage with CAS deduplication",
    category="storage",
    runtime_deps=(PythonDep("google.cloud.storage", extras=("gcs",)),),
)
class CASGCSBackend(CASAddressingEngine):
    """Google Cloud Storage backend with CAS deduplication.

    Storage layout:
        bucket/
        ├── cas/<hash[0:2]>/<hash[2:4]>/<hash>       # Content blob
        └── cas/<hash[0:2]>/<hash[2:4]>/<hash>.meta   # Metadata sidecar
    """

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "bucket_name": ConnectionArg(
            type=ArgType.STRING,
            description="GCS bucket name",
            required=True,
            config_key="bucket",
        ),
        "project_id": ConnectionArg(
            type=ArgType.STRING,
            description="GCP project ID (inferred from credentials if not provided)",
            required=False,
            env_var="GCP_PROJECT_ID",
        ),
        "credentials_path": ConnectionArg(
            type=ArgType.PATH,
            description="Path to service account credentials JSON file",
            required=False,
            secret=True,
            env_var="GOOGLE_APPLICATION_CREDENTIALS",
        ),
    }

    # Default: 10 concurrent workers for GCS batch reads.
    # GCS uses HTTP/2 multiplexing, so moderate concurrency is optimal.
    batch_read_workers: int = 10

    def __init__(
        self,
        bucket_name: str,
        project_id: str | None = None,
        credentials_path: str | None = None,
        operation_timeout: float = 60.0,
        upload_timeout: float = 300.0,
    ):
        try:
            from nexus.backends.transports.gcs_transport import GCSTransport

            transport = GCSTransport(
                bucket_name=bucket_name,
                project_id=project_id,
                credentials_path=credentials_path,
                operation_timeout=operation_timeout,
                upload_timeout=upload_timeout,
            )
            transport.verify_bucket()

            super().__init__(transport, backend_name="gcs")
            self._gcs_transport = transport

        except BackendError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to initialize GCS backend: {e}",
                backend="gcs",
                path=bucket_name,
            ) from e

    def batch_read_content(
        self,
        content_hashes: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
        """Optimized batch read for GCS with parallel downloads.

        Uses ThreadPoolExecutor to download multiple CAS objects concurrently.
        The google-cloud-storage Client is thread-safe for independent reads.
        """
        if not content_hashes:
            return {}

        if len(content_hashes) == 1:
            try:
                data = self.read_content(content_hashes[0], context=context)
            except Exception:
                data = None
            return {content_hashes[0]: data}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers = min(self.batch_read_workers, len(content_hashes))

        def read_one(h: str) -> tuple[str, bytes | None]:
            try:
                ctx = contexts.get(h, context) if contexts else context
                return (h, self.read_content(h, context=ctx))
            except Exception as e:
                logger.warning(f"[GCS] batch_read_content failed for {h}: {e}")
                return (h, None)

        result: dict[str, bytes | None] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(read_one, h): h for h in content_hashes}
            for future in as_completed(futures):
                hash_key, file_content = future.result()
                result[hash_key] = file_content

        return result
