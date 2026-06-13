#!/usr/bin/env python3
"""Add a new Ed25519 signing keypair to the sealed keystore.

Operator-run script. Boots an ephemeral nexusd-cluster + vault.dylib
on localhost using the master key from `$VAULT_SIGNING_MASTER_KEY`,
rehydrates the existing committed keystore via `PutSecretSealed`,
generates a fresh Ed25519 keypair, stores the privkey via `PutSecret`
(vault seals it under the master key), captures the sealed form via
`GetSecretSealed`, writes it back into the keystore JSON for commit,
writes the pubkey alongside, and round-trips a sign/verify self-test
before tearing the cluster down.

Workflow for a new plugin's signing root:

  1. Have `nexusd-cluster` binary + `nexus-vault` cdylib available;
     either pass them via `--nexusd-cluster` / `--vault-dylib` or
     have them on `$PATH` / under the cargo target dir.
  2. `export VAULT_SIGNING_MASTER_KEY=<base64-32-bytes>`
  3. `python scripts/provision_dogfood_key.py --key-name kernel-dogfood-v1`
  4. Commit `data/vault-signing/keys.json` +
     `data/vault-signing/pubkeys/<key-name>.pub`.
  5. Cross-repo: append `<key-name>.pub` to nexus-vfs's
     `TRUSTED_KEY_FILES` so the cluster's plugin loader trusts the
     signature next build.

Re-running with the same key name fails — rotation = mint a new
name. The old pubkey + sealed entry stays in the keystore until the
cross-repo trust-root drop is coordinated.
"""

from __future__ import annotations

import argparse
import base64
import sys
import tempfile
from pathlib import Path

from _dogfood_vault import (
    DEFAULT_KEYSTORE_REL,
    SIGNING_NAMESPACE,
    boot_cluster,
    default_nexusd_cluster,
    default_vault_dylib,
    import_proto_module,
    kill_cluster,
    load_keystore,
    read_master_key_from_env,
    rehydrate,
    repo_root,
    save_keystore,
    shred_bytes,
)

PRIVKEY_LENGTH = 32
PUBKEY_LENGTH = 32
SIGNATURE_LENGTH = 64


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Add a new Ed25519 signing keypair to the sealed keystore."
    )
    p.add_argument(
        "--key-name",
        required=True,
        help="Signing key identity (e.g. kernel-dogfood-v1). Becomes the pubkey filename and the keystore JSON key.",
    )
    p.add_argument(
        "--description",
        default="",
        help="Optional human-readable description stored in the keystore JSON.",
    )
    p.add_argument(
        "--keystore-dir",
        type=Path,
        default=repo_root() / DEFAULT_KEYSTORE_REL,
        help="Directory holding keys.json + pubkeys/.",
    )
    p.add_argument(
        "--nexusd-cluster",
        type=Path,
        default=default_nexusd_cluster(),
        help=(
            "Path to nexusd-cluster binary. Falls back to $NEXUSD_CLUSTER, then "
            "`nexusd-cluster`/`nexusd-cluster.exe` on $PATH."
        ),
    )
    p.add_argument(
        "--vault-dylib",
        type=Path,
        default=default_vault_dylib(),
        help=(
            "Path to the nexus-vault cdylib. Falls back to $VAULT_DYLIB_PATH, "
            "then target/{release,debug}/libnexus_vault.*."
        ),
    )
    args = p.parse_args(argv)

    if args.nexusd_cluster is None or not Path(args.nexusd_cluster).is_file():
        raise SystemExit(
            "nexusd-cluster binary not found; pass --nexusd-cluster or set $NEXUSD_CLUSTER"
        )
    if args.vault_dylib is None or not Path(args.vault_dylib).is_file():
        raise SystemExit(
            "nexus-vault cdylib not found; pass --vault-dylib or set $VAULT_DYLIB_PATH"
        )

    keys_json = args.keystore_dir / "keys.json"
    pub_path = args.keystore_dir / "pubkeys" / f"{args.key_name}.pub"
    if pub_path.exists():
        raise SystemExit(f"refusing to overwrite existing pubkey: {pub_path}")
    pub_path.parent.mkdir(parents=True, exist_ok=True)

    master_key = read_master_key_from_env()
    store = load_keystore(keys_json)
    if args.key_name in store:
        raise SystemExit(
            f"keystore already has an entry named {args.key_name!r}; rotate by minting a new name"
        )

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    privkey = Ed25519PrivateKey.generate()
    raw_priv: bytes = privkey.private_bytes_raw()
    raw_pub: bytes = privkey.public_key().public_bytes_raw()
    assert len(raw_priv) == PRIVKEY_LENGTH
    assert len(raw_pub) == PUBKEY_LENGTH
    b64_priv = base64.b64encode(raw_priv).decode("ascii")
    b64_pub = base64.b64encode(raw_pub).decode("ascii")

    with tempfile.TemporaryDirectory(prefix="provision-dogfood-key-") as td:
        td_path = Path(td)
        proto_dir = td_path / "proto"
        proto_dir.mkdir()
        secrets_pb2, secrets_pb2_grpc = import_proto_module(proto_dir)

        import grpc

        proc, port = boot_cluster(
            nexusd_cluster=args.nexusd_cluster,
            vault_dylib=args.vault_dylib,
            master_key=master_key,
            data_dir=td_path / "cluster",
        )
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{port}")
            stub = secrets_pb2_grpc.GenericSecretsServiceStub(channel)

            rehydrate(stub, secrets_pb2, store)

            stub.PutSecret(
                secrets_pb2.PutSecretRequest(
                    namespace=SIGNING_NAMESPACE,
                    key=args.key_name,
                    value=b64_priv,
                    description=args.description or f"plugin signing privkey ({args.key_name})",
                )
            )

            sealed = stub.GetSecretSealed(
                secrets_pb2.GetSecretRequest(namespace=SIGNING_NAMESPACE, key=args.key_name)
            )

            store[args.key_name] = {
                "namespace": SIGNING_NAMESPACE,
                "nonce": base64.b64encode(sealed.nonce).decode("ascii"),
                "ciphertext": base64.b64encode(sealed.ciphertext).decode("ascii"),
                "description": args.description,
            }
            save_keystore(keys_json, store)
            pub_path.write_text(b64_pub + "\n", encoding="ascii")

            sample = b"plugin-signing-dogfood-self-test"
            sig = privkey.sign(sample)
            if len(sig) != SIGNATURE_LENGTH:
                raise SystemExit(f"unexpected signature length {len(sig)}")
            try:
                Ed25519PublicKey.from_public_bytes(raw_pub).verify(sig, sample)
            except InvalidSignature as exc:
                raise SystemExit("self-test: pubkey did not verify the privkey's sig") from exc
        finally:
            kill_cluster(proc)

    shred_bytes(raw_priv)
    shred_bytes(master_key)

    print(
        f"provisioned: {args.key_name}\n"
        f"  keystore: {keys_json}\n"
        f"  pubkey:   {pub_path}\n"
        f"pubkey (base64): {b64_pub}\n\n"
        "Next steps:\n"
        f"  1. git add {keys_json.relative_to(repo_root())} {pub_path.relative_to(repo_root())}\n"
        "  2. Commit + open PR (CODEOWNERS will request kernel-team review).\n"
        "  3. In nexus-vfs, drop the same pubkey under rust/kernel/trusted_keys/\n"
        "     and add an include_bytes! entry to TRUSTED_KEY_FILES — merge that\n"
        "     BEFORE the nexus pubkey lands so cluster builds trust the new root.\n",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
