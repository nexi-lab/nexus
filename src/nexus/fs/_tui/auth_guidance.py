"""Shared auth guidance and error formatting for the playground TUI."""

from __future__ import annotations

import os


def auth_guidance(service: str, *, user_email: str | None = None, expired: bool = False) -> str:
    """Return step-by-step auth guidance for a playground-supported service."""
    email = user_email or os.environ.get("NEXUS_FS_USER_EMAIL") or "you@example.com"

    if service == "s3":
        return (
            "For S3: 1. run `nexus-fs auth connect s3 native` or set AWS credentials; "
            "2. run `nexus-fs auth test s3`; 3. reopen the playground and mount `s3://bucket`."
        )
    if service == "gcs":
        return (
            "For GCS: 1. run `nexus-fs auth connect gcs native` or "
            "`gcloud auth application-default login`; 2. run `nexus-fs auth test gcs`; "
            "3. reopen the playground and mount `gcs://project/bucket`."
        )
    if service == "gws":
        if expired:
            return (
                f"Google auth expired. 1. run `nexus-fs auth connect gws oauth --user-email {email}`; "
                "2. approve the requested Google scopes; 3. run `nexus-fs auth test gws`; "
                "4. reopen the playground and retry this Google mount."
            )
        return (
            "For Google Workspace: 1. set NEXUS_OAUTH_GOOGLE_CLIENT_ID and "
            "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET; "
            f"2. run `nexus-fs auth connect gws oauth --user-email {email}`; "
            "3. run `nexus-fs auth test gws`; 4. reopen the playground and mount "
            "`gws://drive`, `gws://docs`, `gws://gmail`, or `gws://calendar`; "
            "5. for `gws://chat`, re-run auth and approve Chat scopes if prompted."
        )
    if service == "local":
        return "Local mounts do not require auth. Mount any absolute path and use `n`, `N`, `d`, and `p` in the TUI."
    return f"No dedicated playground auth guide for {service}. Use `/connectors` to list supported targets."


def service_for_target(target: str) -> str | None:
    """Infer the service from a mount uri or mounted path."""
    if target.startswith(("s3://", "/s3/")):
        return "s3"
    if target.startswith(("gcs://", "/gcs/")):
        return "gcs"
    if target.startswith(("gws://", "/gws/", "gdrive://", "/gdrive/", "gmail://", "/gmail/")):
        return "gws"
    if target.startswith(("local://", "/local/")):
        return "local"
    return None


def is_auth_error(message: str) -> bool:
    """Best-effort detection of auth-related connector failures."""
    lower = message.lower()
    needles = (
        "auth_expired",
        "expired",
        "oauth",
        "credentials",
        "not authenticated",
        "permission denied",
        "insufficient authentication scopes",
        "login required",
        "using keyring backend",
        "approve chat scopes",
    )
    return any(needle in lower for needle in needles)


def format_runtime_error(target: str, exc: Exception) -> str:
    """Render a step-by-step error for live browse/preview failures."""
    message = str(exc).strip()
    service = service_for_target(target)
    if service is None:
        return message

    if service == "local":
        return message

    expired = "auth_expired" in message.lower() or "expired" in message.lower()
    if is_auth_error(message):
        prefix = f"Can't access `{target}`."
        return f"{prefix} {auth_guidance(service, expired=expired)}"
    return message
