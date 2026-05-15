"""Archive domain errors (#3793).

Each error has a stable `code` for CLI exit + structured logging.
Codes map: 10s=integrity, 20s=restore guards, 30s=warnings, 40s=trust, 50s=target.
"""

from __future__ import annotations


class ArchiveError(Exception):
    """Base class for all archive errors."""

    code: int = 1


class ArchiveSignatureError(ArchiveError):
    """Ed25519 signature does not verify against embedded pubkey."""

    code = 10

    def __init__(self, message: str, manifest_sha: str | None = None) -> None:
        super().__init__(message)
        self.manifest_sha = manifest_sha


class ArchiveMerkleMismatch(ArchiveError):
    """Computed Merkle root does not match manifest root."""

    code = 11

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"Merkle root mismatch: expected {expected[:16]}…, got {actual[:16]}…")
        self.expected = expected
        self.actual = actual


class ArchiveFileHashMismatch(ArchiveError):
    """A file in the archive does not match its manifest checksum."""

    code = 12

    def __init__(self, path: str, expected: str, actual: str) -> None:
        super().__init__(
            f"File hash mismatch at {path}: expected {expected[:16]}…, got {actual[:16]}…"
        )
        self.path = path
        self.expected = expected
        self.actual = actual


class ArchiveVersionIncompatible(ArchiveError):
    """Archive's min_nexus_version exceeds current nexus version."""

    code = 13

    def __init__(self, required: str, current: str) -> None:
        super().__init__(f"Archive requires nexus >= {required}, current is {current}")
        self.required = required
        self.current = current


class ArchivePlaceholderNotInjected(ArchiveError):
    """One or more credential placeholders were not re-injected before restore."""

    code = 20

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"Restore blocked — missing --inject for: {', '.join(missing)}")
        self.missing = missing


class ArchiveEmbeddingDimMismatch(ArchiveError):
    """Embedding model/dim differs from current nexus configuration."""

    code = 21

    def __init__(
        self, archive_model: str, archive_dim: int, current_model: str, current_dim: int
    ) -> None:
        super().__init__(
            f"Embedding mismatch: archive uses {archive_model} (dim={archive_dim}), "
            f"current is {current_model} (dim={current_dim}). "
            "Pass --rebuild-embeddings to re-embed shipped documents."
        )
        self.archive_model = archive_model
        self.archive_dim = archive_dim
        self.current_model = current_model
        self.current_dim = current_dim


class ArchiveCredentialLeakDetected(ArchiveError):
    """Regex backstop matched a known credential pattern during create.

    This is a warning, not a fatal — the secret is redacted in the bundle
    but the operator should investigate why the secret was in free-text.
    """

    code = 30

    def __init__(self, pattern_name: str, location: str) -> None:
        super().__init__(
            f"Credential pattern {pattern_name!r} matched at {location}; redacted in bundle"
        )
        self.pattern_name = pattern_name
        self.location = location


class ArchiveUntrustedSigner(ArchiveError):
    """--require-trusted is set and signer pubkey is not in trust store."""

    code = 40

    def __init__(self, pubkey_b64: str) -> None:
        super().__init__(
            f"Signer {pubkey_b64[:24]}… is not in ~/.nexus/trusted_signers.json. "
            "Add via: nexus archive keys trust <pubkey> --label <name>"
        )
        self.pubkey_b64 = pubkey_b64


class ArchiveTargetNotEmpty(ArchiveError):
    """Restore target already has zones and --force was not passed."""

    code = 50

    def __init__(self, existing_zones: list[str]) -> None:
        super().__init__(
            f"Target nexus already has zones: {', '.join(existing_zones)}. "
            "Pass --force to overwrite (DESTRUCTIVE)."
        )
        self.existing_zones = existing_zones
