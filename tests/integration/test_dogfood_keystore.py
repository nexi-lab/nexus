"""End-to-end test for the sealed-keystore plugin signing flow.

Real user journey, in order:

  1. `dogfood_keystore_init.py` → writes empty keystore + master key.
  2. `provision_dogfood_key.py --key-name e2e-test` (with the master
     key in env) → spins up vault, stores the new privkey, captures
     sealed bytes, writes keys.json + pubkey.
  3. `dogfood_keystore_fetch.py --key-name e2e-test` (same master
     key) → boots a fresh vault, rehydrates from keys.json,
     GetSecret returns the plaintext base64-of-privkey-bytes.
  4. The pubkey written by step 2 verifies an Ed25519 signature made
     by the privkey returned by step 3.

Each step's output feeds the next. The whole pipeline runs against a
single tempdir so it is reproducible + cleanable.

Skipped when `NEXUSD_CLUSTER` or `VAULT_DYLIB_PATH` aren't set; the
sealed-keystore release CI job (`Plugin Signing Compliance` adjacent)
sets both. Local runs need a pre-built nexusd-cluster (cargo install
nexus-vfs/nexus-cluster) + a built or downloaded libnexus_vault.so.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _have_binaries() -> bool:
    if not os.environ.get("NEXUSD_CLUSTER"):
        return False
    if not os.environ.get("VAULT_DYLIB_PATH"):
        return False
    nx = Path(os.environ["NEXUSD_CLUSTER"])
    vd = Path(os.environ["VAULT_DYLIB_PATH"])
    return nx.is_file() and vd.is_file()


pytestmark = pytest.mark.skipif(
    not _have_binaries(),
    reason="set NEXUSD_CLUSTER + VAULT_DYLIB_PATH to a pre-built cluster + vault.dylib",
)


def _run(
    cmd: list[str], *, env: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        env=full_env,
        check=check,
        capture_output=True,
        text=True,
    )


def test_init_provision_fetch_sign_verify_full_loop(tmp_path: Path) -> None:
    keystore_dir = tmp_path / "vault-signing"

    # ── Step 1: init ──
    init = _run(
        [
            sys.executable,
            str(SCRIPTS / "dogfood_keystore_init.py"),
            "--keystore-dir",
            str(keystore_dir),
        ]
    )
    master_key_b64 = init.stdout.strip()
    assert master_key_b64, "init must print master key on stdout"
    assert len(base64.b64decode(master_key_b64)) == 32

    assert (keystore_dir / "keys.json").is_file()
    assert (keystore_dir / "pubkeys" / ".gitkeep").is_file()
    assert (keystore_dir / "keys.json").read_text(encoding="ascii").strip() == "{}"

    # ── Step 2: provision a fresh signing key ──
    provision_env = {"VAULT_SIGNING_MASTER_KEY": master_key_b64}
    _run(
        [
            sys.executable,
            str(SCRIPTS / "provision_dogfood_key.py"),
            "--key-name",
            "e2e-test",
            "--keystore-dir",
            str(keystore_dir),
            "--description",
            "e2e test key",
        ],
        env=provision_env,
    )
    keys_json = (keystore_dir / "keys.json").read_text(encoding="ascii")
    assert "e2e-test" in keys_json
    pub_path = keystore_dir / "pubkeys" / "e2e-test.pub"
    assert pub_path.is_file()
    raw_pub = base64.b64decode(pub_path.read_text(encoding="ascii").strip())
    assert len(raw_pub) == 32

    # ── Step 3: fetch returns the same plaintext privkey ──
    fetch = _run(
        [
            sys.executable,
            str(SCRIPTS / "dogfood_keystore_fetch.py"),
            "--key-name",
            "e2e-test",
            "--keystore",
            str(keystore_dir / "keys.json"),
        ],
        env=provision_env,
    )
    privkey_b64 = fetch.stdout  # no trailing newline by contract
    assert privkey_b64, "fetch must print the privkey on stdout"
    raw_priv = base64.b64decode(privkey_b64)
    assert len(raw_priv) == 32

    # ── Step 4: sign a sample blob; verify with the pubkey from step 2 ──
    sample_path = tmp_path / "sample.bin"
    sample_path.write_bytes(b"plugin-signing-dogfood-e2e\n")

    sign_env = {"PLUGIN_SIGNING_PRIVKEY": privkey_b64}
    _run(
        [
            sys.executable,
            str(SCRIPTS / "sign_plugin.py"),
            str(sample_path),
        ],
        env=sign_env,
    )
    sig_path = sample_path.with_suffix(".bin.sig")
    if not sig_path.is_file():
        sig_path = Path(str(sample_path) + ".sig")
    assert sig_path.is_file(), "sign_plugin.py must emit <plugin>.sig"
    sig_bytes = sig_path.read_bytes()
    assert len(sig_bytes) == 64, f"signature must be 64 raw bytes, got {len(sig_bytes)}"

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    verifier = Ed25519PublicKey.from_public_bytes(raw_pub)
    try:
        verifier.verify(sig_bytes, sample_path.read_bytes())
    except InvalidSignature as exc:
        pytest.fail(f"signature did not verify against the provisioned pubkey: {exc}")
