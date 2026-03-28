"""Google Cloud Storage connector backend with direct path mapping.

Thin subclass of PathAddressingEngine that:
- Creates a GCSTransport for raw GCS I/O (shared with GCSBackend CAS)
- Mixes in CacheConnectorMixin for L1+L2 caching
- Registers as "path_gcs" via @register_connector
- Adds GCS-specific features: signed URLs, versioning, batch version fetch

Storage structure:
    bucket/
    ├── workspace/
    │   ├── file.txt          # Stored at actual path
    │   └── data/
    │       └── output.json

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from nexus.backends.base.backend import FileInfo, HandlerStatusResponse
from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.wrappers.cache_mixin import CacheConnectorMixin
from nexus.contracts.backend_features import BLOB_BACKEND_FEATURES, BackendFeature
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


@register_connector(
    "path_gcs",
    description="Google Cloud Storage with direct path mapping",
    category="storage",
    requires=["google-cloud-storage"],
    service_name="gcs",
)
class PathGCSBackend(PathAddressingEngine, CacheConnectorMixin):
    """Google Cloud Storage connector with direct path mapping and caching.

    Features:
    - Direct path mapping (file.txt → file.txt in GCS)
    - Optional local caching via CacheConnectorMixin
    - Native GCS versioning support
    - Signed URL generation
    - Batch version fetch (single API call for 1000s of files)
    """

    _BACKEND_FEATURES = BLOB_BACKEND_FEATURES | frozenset(
        {
            BackendFeature.CACHE_BULK_READ,
            BackendFeature.CACHE_SYNC,
            BackendFeature.SIGNED_URL,
            BackendFeature.NATIVE_VERSIONING,
            BackendFeature.RESUMABLE_UPLOAD,
        }
    )

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
        "prefix": ConnectionArg(
            type=ArgType.STRING,
            description="Path prefix for all files in bucket",
            required=False,
            default="",
        ),
        "access_token": ConnectionArg(
            type=ArgType.SECRET,
            description="OAuth access token (alternative to credentials_path)",
            required=False,
            secret=True,
        ),
    }

    def __init__(
        self,
        bucket_name: str,
        project_id: str | None = None,
        credentials_path: str | None = None,
        prefix: str = "",
        access_token: str | None = None,
        record_store: "RecordStoreABC | None" = None,
        operation_timeout: float = 60.0,
        upload_timeout: float = 300.0,
    ):
        try:
            from nexus.backends.transports.gcs_transport import GCSTransport

            transport = GCSTransport(
                bucket_name=bucket_name,
                project_id=project_id,
                credentials_path=credentials_path,
                access_token=access_token,
                operation_timeout=operation_timeout,
                upload_timeout=upload_timeout,
            )
            transport.verify_bucket()
            versioning_enabled = transport.check_versioning()

            super().__init__(
                transport,
                backend_name="path_gcs",
                bucket_name=bucket_name,
                prefix=prefix,
                versioning_enabled=versioning_enabled,
            )
            self._gcs_transport = transport
            self._operation_timeout = operation_timeout
            self._upload_timeout = upload_timeout

            # CacheConnectorMixin needs session_factory
            self.session_factory = record_store.session_factory if record_store else None

        except BackendError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to initialize GCS connector backend: {e}",
                backend="path_gcs",
                path=bucket_name,
            ) from e

    def _is_version_id(self, value: str) -> bool:
        """GCS generation numbers are numeric strings."""
        return value.isdigit()

    # === Connection Health ===

    def check_connection(self, context: "OperationContext | None" = None) -> HandlerStatusResponse:
        start = time.perf_counter()
        try:
            exists = self._gcs_transport.bucket.exists()

            if not exists:
                return HandlerStatusResponse(
                    success=False,
                    error_message=f"Bucket '{self.bucket_name}' does not exist",
                    latency_ms=(time.perf_counter() - start) * 1000,
                    details={"backend": self.name, "bucket": self.bucket_name},
                )

            return HandlerStatusResponse(
                success=True,
                latency_ms=(time.perf_counter() - start) * 1000,
                details={
                    "backend": self.name,
                    "bucket": self.bucket_name,
                    "prefix": self.prefix,
                    "versioning_enabled": self.versioning_enabled,
                },
            )

        except Exception as e:
            return HandlerStatusResponse(
                success=False,
                error_message=str(e),
                latency_ms=(time.perf_counter() - start) * 1000,
                details={"backend": self.name, "bucket": self.bucket_name},
            )

    # === Version Support ===

    def get_version(self, path: str, context: "OperationContext | None" = None) -> str | None:
        if context and context.backend_path:
            backend_path = context.backend_path
        else:
            backend_path = path.lstrip("/")

        blob_path = self._get_blob_path(backend_path)
        return self._gcs_transport.get_generation(blob_path)

    def get_file_info(self, path: str, context: "OperationContext | None" = None) -> FileInfo:
        if context and context.backend_path:
            backend_path = context.backend_path
        else:
            backend_path = path.lstrip("/")

        blob_path = self._get_blob_path(backend_path)
        meta = self._gcs_transport.reload_blob_metadata(blob_path)

        return FileInfo(
            size=meta["size"],
            mtime=meta["updated"],
            backend_version=meta["generation"],
            content_hash=None,
        )

    def batch_get_versions(
        self,
        backend_paths: list[str],
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, str | None]:
        """Optimized: single list_blobs() API call for all versions."""
        if not backend_paths:
            return {}

        blob_paths_map = {self._get_blob_path(path): path for path in backend_paths}

        try:
            blobs = self._gcs_transport.bucket.list_blobs(
                prefix=self.prefix if self.prefix else None
            )

            blob_generations: dict[str, int] = {}
            for blob in blobs:
                if blob.name in blob_paths_map:
                    blob_generations[blob.name] = blob.generation

            versions: dict[str, str | None] = {}
            for blob_path, backend_path in blob_paths_map.items():
                generation = blob_generations.get(blob_path)
                versions[backend_path] = str(generation) if generation else None

            return versions

        except Exception as e:
            logger.warning(f"[GCS] Batch version fetch failed: {e}, falling back")
            return super().batch_get_versions(backend_paths, contexts)

    # === Signed URLs ===

    def generate_signed_url(
        self,
        path: str,
        expires_in: int = 3600,
        context: "OperationContext | None" = None,
    ) -> dict[str, str | int]:
        if context and context.backend_path:
            backend_path = context.backend_path
        else:
            backend_path = path.lstrip("/")

        blob_path = self._get_blob_path(backend_path)

        if not self._gcs_transport.blob_exists(blob_path):
            raise NexusFileNotFoundError(path)

        expires_in = min(expires_in, 604800)
        url = self._gcs_transport.generate_signed_url(blob_path, expires_in)

        return {
            "url": url,
            "expires_in": expires_in,
            "method": "GET",
        }

    # === Content Operations with Caching ===

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        if not context or not context.backend_path:
            raise BackendError(
                message="GCS connector requires backend_path in OperationContext.",
                backend="path_gcs",
            )

        cache_path = self._get_cache_path(context) or context.backend_path
        blob_path = self._get_blob_path(context.backend_path)

        # Check cache first
        if self._has_caching():
            try:
                cached = self._read_from_cache(cache_path, original=True)
                if cached and not cached.stale and cached.content_binary:
                    return cached.content_binary
            except Exception as e:
                logger.debug("[CACHE] Cache read failed for %s: %s", cache_path, e)

        # Read from GCS
        version_id = None
        if self.versioning_enabled and content_id and self._is_version_id(content_id):
            version_id = content_id

        content, generation = self._transport.get_blob(blob_path, version_id)

        # Cache the result
        if self._has_caching():
            try:
                zone_id = getattr(context, "zone_id", None)
                self._write_to_cache(
                    path=cache_path,
                    content=content,
                    backend_version=generation,
                    zone_id=zone_id,
                )
            except Exception as e:
                logger.debug("[CACHE] Cache write failed for %s: %s", cache_path, e)

        return content

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        if not context or not context.backend_path:
            raise BackendError(
                message="GCS connector requires backend_path in OperationContext.",
                backend="path_gcs",
            )

        virtual_path = context.backend_path
        if hasattr(context, "virtual_path") and context.virtual_path:
            virtual_path = context.virtual_path

        blob_path = self._get_blob_path(context.backend_path)
        content_type = self._detect_content_type(context.backend_path, content)
        new_version = self._transport.put_blob(blob_path, content, content_type)

        # Update cache
        if self._has_caching():
            try:
                zone_id = getattr(context, "zone_id", None)
                self._write_to_cache(
                    path=virtual_path,
                    content=content,
                    backend_version=new_version,
                    zone_id=zone_id,
                )
            except Exception as e:
                logger.debug("[CACHE] Cache write failed for %s: %s", virtual_path, e)

        content_hash = new_version if new_version else self._compute_hash(content)
        return WriteResult(content_id=content_hash, size=len(content))

    def write_content_with_version_check(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
        expected_version: str | None = None,
    ) -> WriteResult:
        if not context or not context.backend_path:
            raise BackendError(
                message="GCS connector requires backend_path in OperationContext.",
                backend="path_gcs",
            )

        virtual_path = context.backend_path
        if hasattr(context, "virtual_path") and context.virtual_path:
            virtual_path = context.virtual_path

        if expected_version is not None:
            self._check_version(virtual_path, expected_version, context)

        return self.write_content(content, context=context)
