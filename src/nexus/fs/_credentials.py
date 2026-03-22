"""Auto-credential discovery for cloud backends.

Discovers credentials from the environment following each cloud provider's
standard chain. No credentials are stored — we just check what's available.

Credential chains:
    AWS (S3):
        1. Environment vars: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
        2. Shared credentials file: ~/.aws/credentials
        3. AWS config file: ~/.aws/config
        4. EC2/ECS instance metadata (boto3 handles this)

    GCP (GCS):
        1. GOOGLE_APPLICATION_CREDENTIALS environment variable
        2. gcloud application-default credentials (~/.config/gcloud/application_default_credentials.json)
        3. Compute Engine metadata service

    Local:
        No credentials needed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from nexus.contracts.exceptions import CloudCredentialError

logger = logging.getLogger(__name__)


def check_aws_credentials() -> dict[str, str]:
    """Check for available AWS credentials.

    Returns:
        Dict with credential source info (e.g., {"source": "env", "profile": "default"}).

    Raises:
        CloudCredentialError: If no AWS credentials are found.
    """
    # 1. Environment variables
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return {"source": "environment", "method": "AWS_ACCESS_KEY_ID"}

    # 2. Shared credentials file
    creds_file = Path(
        os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials")
    ).expanduser()
    if creds_file.exists():
        profile = os.environ.get("AWS_PROFILE", "default")
        return {"source": "credentials_file", "profile": profile, "path": str(creds_file)}

    # 3. AWS config file
    config_file = Path(os.environ.get("AWS_CONFIG_FILE", "~/.aws/config")).expanduser()
    if config_file.exists():
        profile = os.environ.get("AWS_PROFILE", "default")
        return {"source": "config_file", "profile": profile, "path": str(config_file)}

    # 4. Try boto3 session (catches IAM roles, SSO, etc.)
    try:
        import boto3

        session = boto3.Session()
        creds = session.get_credentials()
        if creds is not None:
            return {"source": "boto3_session", "method": type(creds).__name__}
    except ImportError:
        pass
    except Exception:
        pass

    raise CloudCredentialError(
        "s3",
        "AWS credentials not found. Configure with:\n"
        "  - Environment: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY\n"
        "  - AWS CLI: aws configure\n"
        "  - Run `nexus doctor` for detailed diagnostics",
    )


def check_gcs_credentials() -> dict[str, str]:
    """Check for available GCP credentials.

    Returns:
        Dict with credential source info.

    Raises:
        CloudCredentialError: If no GCP credentials are found.
    """
    # 1. GOOGLE_APPLICATION_CREDENTIALS env var
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac and Path(gac).exists():
        return {"source": "service_account", "path": gac}

    # 2. Application Default Credentials (ADC)
    adc_path = Path("~/.config/gcloud/application_default_credentials.json").expanduser()
    if adc_path.exists():
        return {"source": "adc", "path": str(adc_path)}

    # 3. Try google-auth default credentials
    try:
        import google.auth

        credentials, project = google.auth.default()
        if credentials is not None:
            return {"source": "google_auth_default", "project": project or "unknown"}
    except ImportError:
        pass
    except Exception:
        pass

    raise CloudCredentialError(
        "gcs",
        "GCP credentials not found. Configure with:\n"
        "  - Environment: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json\n"
        "  - gcloud CLI: gcloud auth application-default login\n"
        "  - Run `nexus doctor` for detailed diagnostics",
    )


def discover_credentials(scheme: str) -> dict[str, str]:
    """Discover credentials for a given backend scheme.

    Args:
        scheme: URI scheme (s3, gcs, local, gdrive).

    Returns:
        Dict with credential source info.

    Raises:
        CloudCredentialError: If credentials are not found for cloud backends.
    """
    if scheme == "s3":
        return check_aws_credentials()
    elif scheme == "gcs":
        return check_gcs_credentials()
    elif scheme in ("local", "gdrive"):
        # local: no credentials needed
        # gdrive: deferred to explicit auth step
        return {"source": "none", "scheme": scheme}
    else:
        # Connector-based schemes (gws, github, slack, etc.) handle
        # their own auth — CLI tools manage credentials externally.
        return {"source": "connector", "scheme": scheme}
