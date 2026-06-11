#!/usr/bin/env python3
"""Detach-sign a plugin binary with the kernel team's Ed25519 key.

Half 2 of the cross-repo plugin-signing 0->1. The verifier side lives in
the nexus-vfs repository, at `rust/kernel/src/kernel/plugins/loader.rs`
+ `rust/kernel/trusted_keys/nexus-team.pub`.

Format contract is held by `nexus-plugin-abi`:
    SIGNATURE_FILE_SUFFIX = ".sig"
    SIGNATURE_LENGTH      = 64   # raw Ed25519 signature, no header
    PUBKEY_LENGTH         = 32   # raw Ed25519 pubkey, base64 in .pub file
Constants here are hardcoded to match. If you ever bump one, bump the
other side in the same coordinated PR — drift fails verify silently
across every existing signed plugin.

Private key source: `PLUGIN_SIGNING_PRIVKEY` env var, base64 of exactly
32 raw bytes. Set as a GitHub Actions secret on `nexi-lab/nexus`. The
private key is never written to disk or echoed; it stays in process
memory only for the duration of the sign call.

Output: `<plugin>` is left untouched; `<plugin>.sig` is written
alongside it with exactly 64 raw bytes (no base64, no PEM, no minisign
frame). The kernel-side verifier reads `<plugin>` and `<plugin>.sig`
in tandem and Ed25519-verifies one against the other.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Pinned format constants — must match `nexus-plugin-abi::signing` in nexus-vfs.
SIGNATURE_FILE_SUFFIX = ".sig"
SIGNATURE_LENGTH = 64
PUBKEY_LENGTH = 32
PRIVKEY_LENGTH = 32

PRIVKEY_ENV = "PLUGIN_SIGNING_PRIVKEY"


def load_privkey_from_env() -> Ed25519PrivateKey:
    """Pull the signing key from `PLUGIN_SIGNING_PRIVKEY` and validate it."""
    raw_b64 = os.environ.get(PRIVKEY_ENV)
    if not raw_b64:
        raise SystemExit(
            f"environment variable {PRIVKEY_ENV} is empty or unset — "
            f"set it to base64 of {PRIVKEY_LENGTH} raw Ed25519 private bytes"
        )
    try:
        raw = base64.b64decode(raw_b64.strip(), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise SystemExit(f"{PRIVKEY_ENV}: not valid base64: {exc}") from exc
    if len(raw) != PRIVKEY_LENGTH:
        raise SystemExit(f"{PRIVKEY_ENV}: decoded length {len(raw)} != expected {PRIVKEY_LENGTH}")
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign_one(privkey: Ed25519PrivateKey, plugin: Path) -> Path:
    """Sign one plugin file in place and return the path of the `.sig` written."""
    if not plugin.is_file():
        raise SystemExit(f"plugin not found or not a file: {plugin}")
    payload = plugin.read_bytes()
    signature = privkey.sign(payload)
    if len(signature) != SIGNATURE_LENGTH:
        # Defence in depth: cryptography always returns 64 for Ed25519, but
        # an SSOT mismatch (someone bumped the constant on one side and not
        # the other) would silently emit a sig the verifier can't read.
        raise SystemExit(f"signature length {len(signature)} != expected {SIGNATURE_LENGTH}")

    sig_path = plugin.with_name(plugin.name + SIGNATURE_FILE_SUFFIX)
    sig_path.write_bytes(signature)

    # Self-check the just-written sig against the in-memory pubkey so an IO
    # truncation, encoding accident, or wrong-keypair pairing is caught
    # before we ship — much cheaper to fail here than in the cluster log.
    pubkey: Ed25519PublicKey = privkey.public_key()
    try:
        pubkey.verify(sig_path.read_bytes(), payload)
    except InvalidSignature as exc:
        raise SystemExit(f"self-verify failed for {sig_path} — signing pipeline is broken") from exc

    return sig_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Ed25519 detach-sign one or more plugin binaries.",
    )
    p.add_argument(
        "plugins",
        nargs="+",
        type=Path,
        help="plugin binaries to sign (`.so` / `.dylib` / `.dll`)",
    )
    args = p.parse_args(argv)

    privkey = load_privkey_from_env()
    for plugin in args.plugins:
        sig_path = sign_one(privkey, plugin)
        sha = base64.b64encode(privkey.public_key().public_bytes_raw()).decode()
        print(f"signed {plugin} -> {sig_path} (pubkey {sha[:8]}...)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
