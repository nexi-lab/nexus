#!/usr/bin/env python3
"""One-time operator script — bootstrap the sealed signing keystore.

Generates a fresh 32-byte AES-256-GCM master key, writes the empty
keystore skeleton at `data/vault-signing/`, and prints the master key
in base64 so the operator can register it as the nexi-lab GitHub org
secret `VAULT_SIGNING_MASTER_KEY`.

After this runs, the keystore looks like:

    data/vault-signing/
        README.md          (operator + key lifecycle reference)
        keys.json          {}   ← empty; provision_dogfood_key.py fills it
        pubkeys/           dir with .gitkeep

Refuses to overwrite an existing `keys.json` — once the master key is
in use, rotating it is a separate, documented procedure (see the
design doc in `docs/superpowers/specs/2026-06-13-sealed-keystore-dogfood-design.md`).

CLI:
  dogfood_keystore_init.py [--keystore-dir PATH]

  --keystore-dir   Override the default `data/vault-signing/` location;
                   only useful for the E2E test that runs in a tempdir.

The script does NOT boot vault — it only sets up files. Provision and
fetch are the entry points that actually exercise the vault gRPC
surface, so a smoke-test there will catch any keystore problems.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

MASTER_KEY_BYTES = 32
DEFAULT_KEYSTORE_REL = Path("data/vault-signing")


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bootstrap the sealed signing keystore.")
    p.add_argument(
        "--keystore-dir",
        type=Path,
        default=_repo_root() / DEFAULT_KEYSTORE_REL,
        help=(
            "Directory holding keys.json + pubkeys/. Defaults to the repo's data/vault-signing/."
        ),
    )
    args = p.parse_args(argv)

    keystore_dir: Path = args.keystore_dir
    keys_json = keystore_dir / "keys.json"
    pubkeys_dir = keystore_dir / "pubkeys"

    if keys_json.exists():
        raise SystemExit(
            f"refusing to overwrite existing keystore: {keys_json}\n"
            "Rotating the master key requires re-sealing every entry — "
            "see docs/superpowers/specs/2026-06-13-sealed-keystore-dogfood-design.md."
        )

    keystore_dir.mkdir(parents=True, exist_ok=True)
    pubkeys_dir.mkdir(parents=True, exist_ok=True)
    (pubkeys_dir / ".gitkeep").touch(exist_ok=True)

    master_key = os.urandom(MASTER_KEY_BYTES)
    b64_master = base64.b64encode(master_key).decode("ascii")

    keys_json.write_text("{}\n", encoding="ascii")
    print(f"wrote empty keystore: {keys_json}", file=sys.stderr)
    print(f"created pubkey dir:   {pubkeys_dir}", file=sys.stderr)

    print(
        "\nVAULT_SIGNING_MASTER_KEY (base64 of 32 raw bytes):\n",
        file=sys.stderr,
    )
    sys.stdout.write(b64_master)
    sys.stdout.write("\n")

    print(
        "\nNext steps (operator):\n"
        "  1. Register the value above as the nexi-lab GitHub org secret\n"
        "     `VAULT_SIGNING_MASTER_KEY`.\n"
        "  2. Commit data/vault-signing/keys.json + pubkeys/.gitkeep.\n"
        "  3. Run scripts/provision_dogfood_key.py to add the first\n"
        "     signing keypair.\n"
        "  4. Optional smoke test: run dogfood_keystore_fetch.py to confirm\n"
        "     CI-side hydration works end-to-end.\n",
        file=sys.stderr,
    )

    # Best-effort scrub of the master key from local memory before exit.
    del master_key
    return 0


if __name__ == "__main__":
    sys.exit(main())
