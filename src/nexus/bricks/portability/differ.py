"""Diff two .nexus bundles by content-addressed blob set."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nexus.bricks.portability.bundle import BundleReader


@dataclass
class BundleDiff:
    added: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    unchanged: set[str] = field(default_factory=set)
    embedding_model_a: str | None = None
    embedding_model_b: str | None = None

    def summary(self) -> str:
        embed_note = (
            "embedding_model: same"
            if self.embedding_model_a == self.embedding_model_b
            else f"embedding_model: {self.embedding_model_a} -> {self.embedding_model_b}"
        )
        return (
            f"+{len(self.added)} docs, -{len(self.removed)} docs, "
            f"={len(self.unchanged)} docs unchanged, {embed_note}"
        )


def _content_blob_hashes(reader: BundleReader) -> set[str]:
    """Return the set of CAS blob hashes in this bundle.

    Checks both the manifest checksums index (for blobs recorded at export
    time) and the actual tar entries, returning their union so that bundles
    built by either pathway are handled correctly.
    """
    out: set[str] = set()

    # Primary source: manifest checksums index (populated at export time)
    try:
        manifest = reader.get_manifest()
        for path in manifest.checksums.files:
            if path.startswith("content/cas/") and len(path) > len("content/cas/xx/"):
                out.add(path.rsplit("/", 1)[-1])
    except Exception:
        pass

    # Secondary source: actual tar entries (in case checksums index is absent)
    for path in reader.list_contents():
        if path.startswith("content/cas/") and len(path) > len("content/cas/xx/"):
            out.add(path.rsplit("/", 1)[-1])

    return out


def diff_bundles(a_path: Path, b_path: Path) -> BundleDiff:
    with BundleReader(a_path) as a, BundleReader(b_path) as b:
        manifest_a = a.get_manifest()
        manifest_b = b.get_manifest()
        ha = _content_blob_hashes(a)
        hb = _content_blob_hashes(b)
    return BundleDiff(
        added=hb - ha,
        removed=ha - hb,
        unchanged=ha & hb,
        embedding_model_a=getattr(manifest_a, "embedding_model", None),
        embedding_model_b=getattr(manifest_b, "embedding_model", None),
    )


__all__ = ["BundleDiff", "diff_bundles"]
