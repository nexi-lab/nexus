#!/usr/bin/env python3
"""CI-side fetcher — extract a plugin signing privkey from the sealed keystore.

Replaces the prior `vault_get_signing_key.py` semantic of "talk to a
running production vault" with the dogfood flow: spin up an ephemeral
vault.dylib + nexusd-cluster on the runner using the org-secret master
key + the committed sealed keystore, GetSecret the requested entry
(vault decrypts under the master key), tear everything down, print
the plaintext privkey to stdout.

The release-plugin-reusable workflow invokes this script when
`signing_key_source == 'vault'`. Output is masked via GitHub Actions'
`::add-mask::` and assigned to `$PLUGIN_SIGNING_PRIVKEY` for
sign_plugin.py to consume.

Env contract:
  VAULT_SIGNING_MASTER_KEY   base64 of 32 raw bytes — the master key
                             that sealed every entry in
                             data/vault-signing/keys.json.

CLI:
  dogfood_keystore_fetch.py
      --key-name <name>
      [--keystore data/vault-signing/keys.json]
      [--namespace signing-keys]
      [--nexusd-cluster <path>]
      [--vault-dylib <path>]

The script prints the privkey value (already base64 of 32 raw
Ed25519 priv bytes — same format `sign_plugin.py` expects) to stdout
with no trailing newline. Refuses to run if the master key env var
is unset or wrong length.
"""

from __future__ import annotations

import argparse
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
    shred_bytes,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extract a privkey from the sealed dogfood keystore.")
    p.add_argument(
        "--key-name",
        required=True,
        help="Key identity to fetch (e.g. kernel-dogfood-v1).",
    )
    p.add_argument(
        "--keystore",
        type=Path,
        default=repo_root() / DEFAULT_KEYSTORE_REL / "keys.json",
        help="Path to the sealed keystore JSON.",
    )
    p.add_argument(
        "--namespace",
        default=SIGNING_NAMESPACE,
        help="Vault namespace the privkey lives under.",
    )
    p.add_argument(
        "--nexusd-cluster",
        type=Path,
        default=default_nexusd_cluster(),
        help="Path to nexusd-cluster binary. Falls back to $NEXUSD_CLUSTER, then $PATH.",
    )
    p.add_argument(
        "--vault-dylib",
        type=Path,
        default=default_vault_dylib(),
        help="Path to the nexus-vault cdylib. Falls back to $VAULT_DYLIB_PATH then cargo target/.",
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

    master_key = read_master_key_from_env()
    store = load_keystore(args.keystore)
    if args.key_name not in store:
        raise SystemExit(
            f"keystore at {args.keystore} has no entry named {args.key_name!r}; "
            f"available: {sorted(store)}"
        )

    with tempfile.TemporaryDirectory(prefix="dogfood-keystore-fetch-") as td:
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

            resp = stub.GetSecret(
                secrets_pb2.GetSecretRequest(namespace=args.namespace, key=args.key_name)
            )

            # Stdout, no newline — workflow assigns to $PLUGIN_SIGNING_PRIVKEY
            # via `PRIVKEY=$(python ...)`; a trailing \n would corrupt the
            # downstream sign_plugin.py base64 decode.
            sys.stdout.write(resp.value)
        finally:
            kill_cluster(proc)

    shred_bytes(master_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
