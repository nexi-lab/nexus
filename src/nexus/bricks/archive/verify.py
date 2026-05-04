"""End-to-end archive verifier (#3793).

Reads a ``.nexus`` tar.gz bundle, parses the manifest, verifies
format_version / min_nexus_version compatibility, and for v2 bundles
checks the ed25519 signature stored in ``signatures.json`` against the
embedded ``signer_pubkey_b64``.

Cross-brick note: this module imports ``ArchiveSigner`` and
``canonical_json_bytes`` from ``nexus.bricks.portability.signer``; that
dependency is listed in ``KNOWN_CROSS_BRICK_EXCEPTIONS`` in
``.pre-commit-hooks/check_brick_imports.py`` under the
``("archive", "portability")`` key.
"""

from __future__ import annotations

import json
import tarfile
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from nexus.bricks.archive.errors import (
    ArchiveError,
    ArchiveSignatureError,
    ArchiveVersionIncompatible,
)
from nexus.bricks.portability.signer import ArchiveSigner, canonical_json_bytes


def _current_nexus_version() -> str:
    try:
        return version("nexus-ai-fs")
    except PackageNotFoundError:
        try:
            return str(import_module("nexus").__version__)
        except Exception:
            return "0.0.0"


def _parse_semver(s: str) -> tuple[int, int, int]:
    """Parse a semver string into a (major, minor, patch) integer tuple.

    Pads missing components with zeros, ignores pre-release suffixes.

    Args:
        s: Semver string such as ``"1.2.3"`` or ``"2.0"``.

    Returns:
        Three-element integer tuple ``(major, minor, patch)``.
    """
    parts = s.split(".")
    while len(parts) < 3:
        parts.append("0")
    # Strip pre-release labels from patch component (e.g., "3-alpha" → 3)
    clean = [p.split("-")[0] for p in parts[:3]]
    major, minor, patch = (int(c) for c in clean)
    return major, minor, patch


def verify_archive(file: Path, *, strict: bool = False) -> None:
    """Verify a ``.nexus`` archive bundle.

    Checks (in order):

    1. ``manifest.json`` is present and valid JSON.
    2. ``format_version`` — if *strict*, rejects v1 bundles.
    3. ``min_nexus_version`` — rejects when the archive requires a newer
       nexus than is currently installed.
    4. Ed25519 signature (v2 bundles only) — verifies the payload
       ``canonical_json_bytes(manifest) + merkle_root`` against
       ``signatures.json``.  If *strict* and ``signatures.json`` is absent
       on a v2 bundle, raises :class:`ArchiveSignatureError`.

    Args:
        file: Path to the ``.nexus`` bundle (tar.gz).
        strict: When ``True`` apply tighter rules: reject v1 bundles and
                require ``signatures.json`` on v2 bundles.

    Raises:
        ArchiveError: ``manifest.json`` missing or corrupt, or v1 bundle
            rejected in strict mode.
        ArchiveVersionIncompatible: ``min_nexus_version`` > current nexus.
        ArchiveSignatureError: Signature verification failed.
    """
    with tarfile.open(file, "r:gz") as tar:
        names = tar.getnames()

        if "manifest.json" not in names:
            raise ArchiveError(f"bundle missing manifest.json: {file}")

        manifest_member = tar.extractfile("manifest.json")
        assert manifest_member is not None  # we checked names above
        manifest_bytes = manifest_member.read()

        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError as e:
            raise ArchiveError(f"corrupt manifest: {e}") from e

        # --- format_version check ---
        format_version: str = manifest.get("format_version", "1.0.0")
        if strict and not format_version.startswith("2."):
            raise ArchiveError(
                f"--strict requires a v2 bundle; this bundle is format_version={format_version}"
            )

        # --- min_nexus_version check ---
        min_required: str = manifest.get("min_nexus_version", "0.0.0")
        current = _current_nexus_version()
        if _parse_semver(min_required) > _parse_semver(current):
            raise ArchiveVersionIncompatible(required=min_required, current=current)

        # --- signature check (v2 only) ---
        if format_version.startswith("2."):
            if "signatures.json" in names:
                sig_member = tar.extractfile("signatures.json")
                assert sig_member is not None
                sig_doc = json.loads(sig_member.read())

                merkle_root: str = (manifest.get("checksums") or {}).get("merkle_root") or ""
                # Re-derive the exact same payload that was signed at create time:
                # canonical_json_bytes(manifest_dict) + merkle_root_bytes
                payload = canonical_json_bytes(manifest) + merkle_root.encode()
                ArchiveSigner.verify(
                    payload,
                    sig_doc["signature_b64"],
                    sig_doc["signer_pubkey_b64"],
                )
            elif strict:
                raise ArchiveSignatureError(
                    "v2 bundle is missing signatures.json — pass strict=False to skip"
                )


__all__ = ["_current_nexus_version", "_parse_semver", "verify_archive"]
