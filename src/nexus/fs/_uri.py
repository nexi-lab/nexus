"""URI parser for cloud storage mount URIs.

Parses URIs of the form ``<scheme>://<authority>/<path>`` into a
:class:`MountSpec` dataclass and derives filesystem mount points.

Supported schemes
-----------------
- ``s3://``        — Amazon S3
- ``gcs://``       — Google Cloud Storage
- ``local://``     — Local filesystem, passthrough (files visible on disk)
- ``cas-local://`` — Local filesystem, content-addressed (dedup, opt-in)
- ``gdrive://``    — Google Drive

Examples
--------
>>> parse_uri("s3://my-bucket/subdir")
MountSpec(scheme='s3', authority='my-bucket', path='subdir', mount_point='', uri='s3://my-bucket/subdir')

>>> spec = parse_uri("gcs://project/bucket")
>>> derive_mount_point(spec)
'/gcs/bucket'
"""

from __future__ import annotations

import dataclasses
import urllib.parse

from nexus.contracts.exceptions import InvalidPathError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Built-in storage schemes with dedicated backend implementations.
BUILTIN_SCHEMES: frozenset[str] = frozenset({"s3", "gcs", "local", "cas-local", "gdrive"})

# All recognized schemes including connector-based ones.
# This set is extended at runtime when connectors register themselves.
# Using a mutable set so _register_connector_scheme() can add to it.
SUPPORTED_SCHEMES: set[str] = set(BUILTIN_SCHEMES)


def _register_connector_scheme(scheme: str) -> None:
    """Register an additional URI scheme (called by connector discovery)."""
    SUPPORTED_SCHEMES.add(scheme)


RESERVED_PATHS: frozenset[str] = frozenset({"/__sys__", "/__pipes__"})

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class MountSpec:
    """Parsed representation of a cloud-storage mount URI."""

    scheme: str
    authority: str
    path: str
    mount_point: str
    uri: str


def derive_bucket(spec: MountSpec) -> str:
    """Derive the cloud bucket/container name from a MountSpec.

    For GCS (``gcs://project/bucket/sub``), the bucket is the first
    path segment.  For S3 (``s3://bucket/sub``), it is the authority.
    Falls back to authority for all other schemes.
    """
    if spec.scheme == "gcs" and spec.path:
        return spec.path.strip("/").split("/")[0]
    return spec.authority


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_uri(uri: str) -> MountSpec:
    """Parse a cloud-storage URI into a :class:`MountSpec`.

    Parameters
    ----------
    uri:
        A URI string such as ``s3://my-bucket/subdir``.

    Returns
    -------
    MountSpec
        Parsed components.  ``mount_point`` is left empty at this stage —
        call :func:`derive_mount_point` to populate it.

    Raises
    ------
    InvalidPathError
        If the URI is malformed, uses an unsupported scheme, or has an
        empty authority component.
    """
    if not uri:
        raise InvalidPathError("URI must not be empty")

    parsed = urllib.parse.urlparse(uri)

    # --- scheme ----------------------------------------------------------
    scheme = parsed.scheme.lower()
    if not scheme:
        raise InvalidPathError(
            f"Missing scheme in URI '{uri}'. "
            f"Supported schemes: {', '.join(sorted(SUPPORTED_SCHEMES))}"
        )
    # Accept any scheme — built-in storage schemes have dedicated backends,
    # other schemes are resolved via the connector registry at mount time.
    # Only reject truly empty schemes (handled above).

    # --- authority -------------------------------------------------------
    authority = parsed.netloc
    if not authority:
        # urlparse puts everything into path when authority is empty.
        # This happens with local:///tmp/nexus and gws://sheets (where
        # "sheets" is treated as netloc by urlparse if the URI has //).
        # Treat the first path segment as the authority stand-in.
        if parsed.path:
            authority, _, remainder = parsed.path.lstrip("/").partition("/")
            if not authority:
                raise InvalidPathError(
                    f"Empty authority in URI '{uri}'. "
                    "Provide a host, bucket, or path component after the scheme."
                )
            path = remainder.rstrip("/")
            return MountSpec(
                scheme=scheme,
                authority=authority,
                path=path,
                mount_point="",
                uri=uri,
            )
        raise InvalidPathError(
            f"Empty authority in URI '{uri}'. "
            "Provide a host, bucket, or path component after the scheme."
        )

    # --- path ------------------------------------------------------------
    path = parsed.path.strip("/")

    return MountSpec(
        scheme=scheme,
        authority=authority,
        path=path,
        mount_point="",
        uri=uri,
    )


def derive_mount_point(spec: MountSpec, at: str | None = None) -> str:
    """Derive the filesystem mount point for a parsed URI.

    Mount-point logic per scheme:

    * **s3**  — ``/s3/<authority>/``
    * **gcs** — ``/gcs/<last-authority-or-path-segment>/``
      (``gcs://project/bucket`` → ``/gcs/bucket``)
    * **local** — ``/local/<sanitised-path>/``
    * **gdrive** — ``/gdrive/<authority>/``

    Parameters
    ----------
    spec:
        A :class:`MountSpec` returned by :func:`parse_uri`.
    at:
        Optional explicit mount-point override.  When provided the
        derivation logic is skipped entirely.

    Returns
    -------
    str
        Absolute mount-point path (always starts with ``/``).

    Raises
    ------
    InvalidPathError
        If the resulting mount point collides with a reserved path.
    """
    if at is not None:
        mount = at if at.startswith("/") else f"/{at}"
    elif spec.scheme == "s3":
        mount = f"/s3/{spec.authority}"
    elif spec.scheme == "gcs":
        # gcs://project/bucket → mount at /gcs/bucket
        # gcs://bucket          → mount at /gcs/bucket
        mount = f"/gcs/{derive_bucket(spec)}"
    elif spec.scheme in ("local", "cas-local"):
        # Sanitise: replace slashes and dots so the mount name is a single
        # clean segment.  e.g. /tmp/nexus → tmp-nexus, ./data → data.
        # cas-local and local share the same mount-point shape; they only
        # differ in how bytes are stored under the root.
        raw = spec.path if spec.path else spec.authority
        sanitised = raw.strip(".").strip("/").replace("/", "-")
        prefix = "cas-local" if spec.scheme == "cas-local" else "local"
        mount = f"/{prefix}/{sanitised}"
    elif spec.scheme == "gdrive":
        mount = f"/gdrive/{spec.authority}"
    else:
        # Generic: /<scheme>/<authority> — works for any connector scheme
        mount = f"/{spec.scheme}/{spec.authority}"

    # Strip trailing slash for consistent comparisons, but keep root slash.
    mount = mount.rstrip("/") or "/"

    _check_reserved(mount)
    return mount


def validate_mount_collision(mount_point: str, existing_mounts: set[str]) -> None:
    """Raise if *mount_point* conflicts with an existing mount.

    Parameters
    ----------
    mount_point:
        The candidate mount point to check.
    existing_mounts:
        Set of mount points that are already in use.

    Raises
    ------
    InvalidPathError
        If *mount_point* is already present in *existing_mounts*.
    """
    if mount_point in existing_mounts:
        raise InvalidPathError(
            f"Mount point '{mount_point}' is already mounted. "
            "Each scheme+authority combination must be unique."
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_reserved(mount_point: str) -> None:
    """Raise if *mount_point* falls under a reserved path prefix."""
    normalised = mount_point.rstrip("/")
    for reserved in RESERVED_PATHS:
        if normalised == reserved or normalised.startswith(reserved + "/"):
            raise InvalidPathError(
                f"Mount point '{mount_point}' conflicts with reserved path '{reserved}/'."
            )
