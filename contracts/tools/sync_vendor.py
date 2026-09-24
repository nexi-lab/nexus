#!/usr/bin/env python3
"""Materialize the derived contract artifacts: vendored nexus-vfs projections,
source-lock, and the per-kind manifest.

The vendor directory is a materialization, not a second editable source: the
bytes are fetched from the exact nexus-vfs revision pinned by the workspace
Cargo.toml and recorded with their SHA-256 digests in source-lock.gen.json.
Schemas that need zone-id/zone-path semantics $ref these files by their stable
$nexus-vfs.dev $id instead of restating the rules.

The manifest (zone-v1.manifest.gen.json) merges hand-maintained metadata
(manifests/zone-v1.meta.json) with digests computed from the actual schema
files, so digests can never drift from reality.

  python contracts/tools/sync_vendor.py          fetch, write, report
  python contracts/tools/sync_vendor.py --check  fail if any derived file drifted

The revision is read from the workspace Cargo.toml (never hardcoded), so the
lock can never become a second human-maintained pin.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CARGO_TOML = REPO_ROOT / "Cargo.toml"
HTTP_API_CARGO_TOML = REPO_ROOT / "rust" / "services" / "http-api" / "Cargo.toml"
VENDOR_DIR = REPO_ROOT / "contracts" / "vendor" / "nexus-vfs.gen"
SOURCE_LOCK = REPO_ROOT / "contracts" / "source-lock.gen.json"
MANIFEST_META = REPO_ROOT / "contracts" / "manifests" / "zone-v1.meta.json"
MANIFEST_GEN = REPO_ROOT / "contracts" / "manifests" / "zone-v1.manifest.gen.json"
RAW_BASE = "https://raw.githubusercontent.com/nexi-lab/nexus-vfs"

# The owner projections this repo's product schemas reference. Paths live in
# the nexus-vfs repo; names mirror them for traceability.
PROJECTIONS = [
    "contracts/zone-id/tenant-zone-id-create.schema.gen.json",
    "contracts/zone-id/system-zone-id.schema.gen.json",
    "contracts/zone-id/existing-zone-id-ref.schema.gen.json",
    "contracts/zone-id/remote-learned-zone-id.schema.gen.json",
    "contracts/zone-path/zone-path.schema.gen.json",
]

# This repo's own product schemas; the manifest covers exactly these kinds.
OWNED_SCHEMAS = [
    "common/v1/principal-ref.schema.json",
    "common/v1/resource-ref.schema.json",
    "auth/v1/zone.schema.json",
    "auth/v1/zone-create-request.schema.json",
    "auth/v1/zone-patch-request.schema.json",
    "auth/v1/zone-grant.schema.json",
    "auth/v1/zone-grant-create-request.schema.json",
    "auth/v1/zone-operation.schema.json",
    "auth/v1/zone-delegation-scope-rule.schema.json",
    "auth/v1/zone-delegation-issue-request.schema.json",
    "auth/v1/zone-delegation.schema.json",
    "runtime/v2/runtime-resource-scope.schema.json",
]

CHECK = "--check" in sys.argv


def pinned_rev() -> str:
    """Extract the one revision used by both direct dependency surfaces."""
    revs: set[str] = set()
    for cargo_toml in (CARGO_TOML, HTTP_API_CARGO_TOML):
        matches = set(
            re.findall(
                r'git\s*=\s*"https://github\.com/nexi-lab/nexus-vfs"\s*,\s*rev\s*=\s*"([0-9a-f]{40})"',
                cargo_toml.read_text(encoding="utf-8"),
            )
        )
        if not matches:
            raise SystemExit(f"no nexus-vfs revision found in {cargo_toml}")
        revs.update(matches)
    if len(revs) != 1:
        raise SystemExit(
            f"expected one nexus-vfs rev across both Cargo manifests, found {sorted(revs)}; "
            "the source lock derives from the Cargo pin, so this must stay uniform"
        )
    return revs.pop()


def fetch(url: str) -> bytes:
    # urllib honors HTTPS_PROXY/HTTP_PROXY via getproxies() by default.
    request = urllib.request.Request(url, headers={"User-Agent": "nexus-contract-sync/1"})
    last_error: OSError | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return resp.read()
        except OSError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest() -> bytes:
    meta = json.loads(MANIFEST_META.read_text(encoding="utf-8"))
    kinds = []
    for rel in OWNED_SCHEMAS:
        path = REPO_ROOT / "contracts" / rel
        doc = json.loads(path.read_text(encoding="utf-8"))
        id_tail = doc["$id"].rsplit("/schemas/", 1)[1]
        family, major = id_tail.split("/")[0], id_tail.split("/")[1]  # e.g. common/v1/...
        # Referenced sub-objects (PrincipalRef) carry no api_version property;
        # the family major in the $id is authoritative either way.
        api_version = (
            doc.get("properties", {})
            .get("api_version", {})
            .get("const", f"{family}.sudo.dev/{major}")
        )
        fm = meta["family_meta"].get(api_version)
        if fm is None:
            raise SystemExit(f"family_meta missing for {rel} (api_version={api_version})")
        km = meta["kind_meta"].get(rel)
        if km is None:
            raise SystemExit(f"kind_meta missing for {rel}")
        kinds.append(
            {
                "family": family,
                "api_version": api_version,
                "kind": doc["title"],
                "schema_id": doc["$id"],
                "schema_path": f"contracts/{rel}",
                "schema_digest": f"sha256:{sha256(path.read_bytes())}",
                "semantic_adr_refs": ["sudostack/docs/adr/ADR-002-zone-and-tenancy-model.md"],
                **fm,
                "secrets_allowed": False,
                "compatibility": km["compatibility"],
            }
        )
    kinds.sort(key=lambda k: k["schema_path"])
    manifest = {
        "$comment": [
            "Generated by contracts/tools/sync_vendor.py from zone-v1.meta.json + schema digests — do not edit.",
        ],
        "upstream": {
            "source": "https://github.com/nexi-lab/nexus-vfs",
            "referenced_projections": "contracts/source-lock.gen.json",
        },
        "schemas": kinds,
        "amendments": meta.get("amendments", []),
    }
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main() -> int:
    rev = pinned_rev()
    entries = []
    contents: dict[Path, bytes] = {}
    for rel in PROJECTIONS:
        url = f"{RAW_BASE}/{rev}/{rel}"
        try:
            data = fetch(url)
        except OSError as exc:
            raise SystemExit(f"cannot fetch {url}: {exc}") from exc
        dest = VENDOR_DIR / Path(rel).name
        contents[dest] = data
        entries.append(
            {"path": str(dest.relative_to(REPO_ROOT)).replace("\\", "/"), "sha256": sha256(data)}
        )

    entries.sort(key=lambda e: e["path"])
    lock = {
        "$comment": [
            "Generated by contracts/tools/sync_vendor.py — do not edit.",
            "Derived from the nexus-vfs revision pinned in Cargo.toml; a second",
            "hand-maintained pin would defeat the point.",
        ],
        "source": "https://github.com/nexi-lab/nexus-vfs",
        "rev": rev,
        "files": entries,
    }
    lock_bytes = (json.dumps(lock, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    stale: list[str] = []
    for dest, data in contents.items():
        current = dest.read_bytes() if dest.exists() else None
        if current == data:
            continue
        stale.append(dest.name)
        if CHECK:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(f"wrote {dest.relative_to(REPO_ROOT)}")

    lock_current = SOURCE_LOCK.read_bytes() if SOURCE_LOCK.exists() else None
    if lock_current != lock_bytes:
        stale.append(SOURCE_LOCK.name)
        if not CHECK:
            SOURCE_LOCK.parent.mkdir(parents=True, exist_ok=True)
            SOURCE_LOCK.write_bytes(lock_bytes)
            print(f"wrote {SOURCE_LOCK.relative_to(REPO_ROOT)}")

    manifest_bytes = build_manifest()
    manifest_current = MANIFEST_GEN.read_bytes() if MANIFEST_GEN.exists() else None
    if manifest_current != manifest_bytes:
        stale.append(MANIFEST_GEN.name)
        if not CHECK:
            MANIFEST_GEN.parent.mkdir(parents=True, exist_ok=True)
            MANIFEST_GEN.write_bytes(manifest_bytes)
            print(f"wrote {MANIFEST_GEN.relative_to(REPO_ROOT)}")

    if CHECK and stale:
        print(f"stale: {', '.join(sorted(set(stale)))}", file=sys.stderr)
        print("Run `python contracts/tools/sync_vendor.py` and commit the result.", file=sys.stderr)
        return 1
    if not stale:
        print("up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
