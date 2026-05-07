"""S3 connector manifest (extension store discovery).

Imported by ``nexus.extensions.store`` for metadata-only enumeration without
loading boto3 or the S3 backend implementation. Runtime mounting continues to
use ``nexus.backends._manifest.CONNECTOR_MANIFEST`` during the #3964 pilot.
"""

from __future__ import annotations

from nexus.extensions.manifest import ConnectorManifest, RuntimeDep
from nexus.extensions.types import ArgType, ConnectionArg

MANIFEST = ConnectorManifest(
    name="path_s3",
    module="nexus.backends.storage.path_s3",
    factory="PathS3Backend",
    description="AWS S3 with direct path mapping",
    service_name="s3",
    runtime_deps=(
        RuntimeDep(
            kind="python",
            name="boto3",
            extras=("s3",),
            install_hint="pip install nexus-fs[s3]",
        ),
    ),
    import_probes=("boto3",),
    capabilities=frozenset(
        {
            "rename",
            "directory_listing",
            "path_delete",
            "streaming",
            "batch_content",
            "signed_url",
            "multipart_upload",
            "native_versioning",
            "resumable_upload",
        }
    ),
    connection_args={
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
    },
    config_mapping={"bucket": "bucket_name"},
)
