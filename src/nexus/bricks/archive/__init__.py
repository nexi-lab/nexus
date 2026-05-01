"""Archive brick — signed, credential-stripped zone snapshots (Issue #3793).

Layers signing, credential stripping, and scheduled retention on top of
the existing `bricks/portability/` zone export brick.

Public API:
    ArchiveError                  - Base class for all archive errors
    ArchiveSignatureError         - ed25519 signature mismatch
    ArchiveMerkleMismatch         - Merkle root verification failed
    ArchiveFileHashMismatch       - Per-file SHA256 mismatch
    ArchiveVersionIncompatible    - min_nexus_version > current
    ArchivePlaceholderNotInjected - restore guard tripped
    ArchiveEmbeddingDimMismatch   - dim differs and --rebuild-embeddings not set
    ArchiveCredentialLeakDetected - regex backstop matched during create (warning)
    ArchiveUntrustedSigner        - --require-trusted and signer unseen
    ArchiveTargetNotEmpty         - target has zones and --force not set
"""

from nexus.bricks.archive.errors import (
    ArchiveCredentialLeakDetected,
    ArchiveEmbeddingDimMismatch,
    ArchiveError,
    ArchiveFileHashMismatch,
    ArchiveMerkleMismatch,
    ArchivePlaceholderNotInjected,
    ArchiveSignatureError,
    ArchiveTargetNotEmpty,
    ArchiveUntrustedSigner,
    ArchiveVersionIncompatible,
)

__all__ = [
    "ArchiveError",
    "ArchiveSignatureError",
    "ArchiveMerkleMismatch",
    "ArchiveFileHashMismatch",
    "ArchiveVersionIncompatible",
    "ArchivePlaceholderNotInjected",
    "ArchiveEmbeddingDimMismatch",
    "ArchiveCredentialLeakDetected",
    "ArchiveUntrustedSigner",
    "ArchiveTargetNotEmpty",
]
