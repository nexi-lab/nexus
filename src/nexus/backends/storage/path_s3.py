"""AWS S3 connector backend with direct path mapping.

Thin subclass of PathAddressingEngine that:
- Creates an S3Transport for raw S3 I/O
- Mixes in MultipartUpload for chunked uploads
- Registers as "path_s3" via @register_connector
- Adds S3-specific features: presigned URLs, multipart, versioning

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from nexus.backends.base.backend import FileInfo, HandlerStatusResponse
from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.base.runtime_deps import PythonDep
from nexus.backends.engines.multipart import MultipartUpload
from nexus.contracts.backend_features import BLOB_BACKEND_FEATURES, BackendFeature
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


@register_connector(
    "path_s3",
    description="AWS S3 with direct path mapping",
    category="storage",
    runtime_deps=(PythonDep("boto3", extras=("s3",)),),
    service_name="s3",
)
class PathS3Backend(PathAddressingEngine, MultipartUpload):
    """AWS S3 connector with direct path mapping and multipart upload."""

    _BACKEND_FEATURES = BLOB_BACKEND_FEATURES | frozenset(
        {
            BackendFeature.SIGNED_URL,
            BackendFeature.MULTIPART_UPLOAD,
            BackendFeature.NATIVE_VERSIONING,
            BackendFeature.RESUMABLE_UPLOAD,
        }
    )

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "bucket_name": ConnectionArg(
            type=ArgType.STRING,
            description="S3 bucket name",
            required=True,
            config_key="bucket",
        ),
        "region_name": ConnectionArg(
            type=ArgType.STRING,
            description="AWS region (e.g., us-east-1)",
            required=False,
            env_var="AWS_DEFAULT_REGION",
        ),
        "credentials_path": ConnectionArg(
            type=ArgType.PATH,
            description="Path to AWS credentials JSON file",
            required=False,
            secret=True,
        ),
        "prefix": ConnectionArg(
            type=ArgType.STRING,
            description="Path prefix for all files in bucket",
            required=False,
            default="",
        ),
        "access_key_id": ConnectionArg(
            type=ArgType.SECRET,
            description="AWS access key ID",
            required=False,
            secret=True,
            env_var="AWS_ACCESS_KEY_ID",
        ),
        "secret_access_key": ConnectionArg(
            type=ArgType.PASSWORD,
            description="AWS secret access key",
            required=False,
            secret=True,
            env_var="AWS_SECRET_ACCESS_KEY",
        ),
        "session_token": ConnectionArg(
            type=ArgType.SECRET,
            description="AWS session token (for temporary credentials)",
            required=False,
            secret=True,
            env_var="AWS_SESSION_TOKEN",
        ),
    }

    def __init__(
        self,
        bucket_name: str,
        region_name: str | None = None,
        credentials_path: str | None = None,
        prefix: str = "",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        operation_timeout: float = 60.0,
        upload_timeout: float = 300.0,
    ):
        try:
            from nexus.backends.transports.s3_transport import S3Transport

            transport = S3Transport(
                bucket_name=bucket_name,
                region_name=region_name,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                session_token=session_token,
                credentials_path=credentials_path,
                operation_timeout=operation_timeout,
                upload_timeout=upload_timeout,
            )
            transport.verify_bucket()
            versioning_enabled = transport.check_versioning()

            super().__init__(
                transport,
                backend_name="path_s3",
                bucket_name=bucket_name,
                prefix=prefix,
                versioning_enabled=versioning_enabled,
            )
            self._s3_transport = transport

        except BackendError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to initialize S3 connector backend: {e}",
                backend="path_s3",
                path=bucket_name,
            ) from e

    # === Connection Health ===

    def check_connection(self, context: "OperationContext | None" = None) -> HandlerStatusResponse:
        start = time.perf_counter()
        try:
            self._s3_transport.verify_bucket()
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
        backend_path = (
            context.backend_path if context and context.backend_path else path.lstrip("/")
        )
        return self._s3_transport.get_version_id(self._get_key_path(backend_path))

    def get_file_info(self, path: str, context: "OperationContext | None" = None) -> FileInfo:
        backend_path = (
            context.backend_path if context and context.backend_path else path.lstrip("/")
        )
        meta = self._s3_transport.get_object_metadata(self._get_key_path(backend_path))
        # S3 VersionId fallback: use "etag:<etag>" when versioning is off
        version_id = meta["version_id"]
        if not version_id or version_id == "null":
            etag = meta.get("etag", "")
            version_id = f"etag:{etag}" if etag else None
        return FileInfo(
            size=meta["size"],
            mtime=meta["last_modified"],
            backend_version=version_id,
            content_hash=meta.get("etag"),
        )

    # === Presigned URLs ===

    def generate_presigned_url(
        self, path: str, expires_in: int = 3600, context: "OperationContext | None" = None
    ) -> dict[str, str | int]:
        backend_path = (
            context.backend_path if context and context.backend_path else path.lstrip("/")
        )
        blob_path = self._get_key_path(backend_path)
        if not self._s3_transport.exists(blob_path):
            raise NexusFileNotFoundError(path)
        return {
            "url": self._s3_transport.generate_presigned_url(blob_path, expires_in),
            "expires_in": expires_in,
            "method": "GET",
        }

    # === Multipart Upload (MultipartUpload) ===

    def init_multipart(
        self,
        backend_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> str:
        return self._s3_transport.init_multipart(
            self._get_key_path(backend_path), content_type, metadata
        )

    def upload_part(
        self, backend_path: str, upload_id: str, part_number: int, data: bytes
    ) -> dict[str, Any]:
        return self._s3_transport.upload_part(
            self._get_key_path(backend_path), upload_id, part_number, data
        )

    def complete_multipart(
        self, backend_path: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> str:
        blob_path = self._get_key_path(backend_path)
        self._s3_transport.complete_multipart(blob_path, upload_id, parts)
        content, _ = self._s3_transport.fetch(blob_path)
        return self._compute_hash(content)

    def abort_multipart(self, backend_path: str, upload_id: str) -> None:
        self._s3_transport.abort_multipart(self._get_key_path(backend_path), upload_id)

    # === Content Operations ===

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        if not context or not context.backend_path:
            raise BackendError(
                message="S3 connector requires backend_path in OperationContext.",
                backend="path_s3",
            )

        version_id = None
        if self.versioning_enabled and content_id and self._is_version_id(content_id):
            version_id = content_id

        blob_path = self._get_key_path(context.backend_path)
        content, _resp_version = self._transport.fetch(blob_path, version_id)

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
                message="S3 connector requires backend_path in OperationContext.",
                backend="path_s3",
            )

        blob_path = self._get_key_path(context.backend_path)
        content_type = self._detect_content_type(context.backend_path, content)
        new_version = self._transport.store(blob_path, content, content_type)

        content_hash = new_version if new_version else self._compute_hash(content)

        return WriteResult(
            content_id=content_hash,
            size=len(content),
        )
