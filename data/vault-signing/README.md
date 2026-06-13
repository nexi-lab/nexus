# Sealed plugin-signing keystore

This directory holds the source-of-truth state for the plugin-signing
dogfood path. Every kernel-signed cdylib plugin (currently:
`nexus-vault`, `nexus-local-connector`, `nexus-fuse-plugin`) is signed
by an Ed25519 privkey stored here in **vault-sealed form** — the
plaintext privkeys never land on disk, in source, or in CI logs.

## Layout

```
data/vault-signing/
├── README.md              # this file
├── keys.json              # sealed (nonce, ciphertext) pairs per key-name
└── pubkeys/
    ├── .gitkeep
    └── <key-name>.pub     # base64(32 raw Ed25519 verifying-key bytes)
```

### `keys.json` schema

```json
{
  "<key-name>": {
    "namespace":   "signing-keys",
    "nonce":       "<base64-12-bytes>",
    "ciphertext":  "<base64-of-aes256gcm-ct-with-tag>",
    "description": "<human-readable>"
  }
}
```

`nonce` + `ciphertext` are emitted verbatim by vault's
`GetSecretSealed` RPC. Vault holds the master key; only an instance
hydrated from the same master key can `GetSecret`-decrypt the
ciphertext back to the plaintext base64-of-privkey-bytes.

## Trust chain

1. The master key — base64 of 32 random bytes — is bootstrapped once
   by `scripts/dogfood_keystore_init.py` and registered as the
   `VAULT_SIGNING_MASTER_KEY` nexi-lab GitHub org secret.
2. `scripts/provision_dogfood_key.py` generates new Ed25519 keypairs,
   stores the privkey in vault (which seals it under the master key),
   captures the sealed bytes, and writes them here.
3. `scripts/dogfood_keystore_fetch.py` reverses the process in CI:
   boots an ephemeral vault with the master key, hydrates state from
   `keys.json`, and reads back the plaintext privkey for one signing
   step. The CI runner pipes it into `$PLUGIN_SIGNING_PRIVKEY` after a
   `::add-mask::` so it never appears in logs.
4. The matching `pubkeys/<key-name>.pub` is committed alongside; the
   nexus-vfs trust root (`rust/kernel/trusted_keys/`) gets the same
   file dropped in so the kernel's plugin loader trusts the signature
   at dlopen time.

## Operator workflow

### First-time setup

```sh
python scripts/dogfood_keystore_init.py
# Take the printed base64 value, register as the nexi-lab GitHub
# org secret VAULT_SIGNING_MASTER_KEY.
git add data/vault-signing/
git commit -m "feat(vault-signing): initialize empty keystore"
```

### Add a new signing key

```sh
export VAULT_SIGNING_MASTER_KEY=...
python scripts/provision_dogfood_key.py --key-name <name>
git add data/vault-signing/keys.json data/vault-signing/pubkeys/<name>.pub
# CODEOWNERS will request kernel-team review.
git commit -m "feat(vault-signing): provision <name>"
```

After the nexus PR merges, drop the same `<name>.pub` into nexus-vfs
`rust/kernel/trusted_keys/` and add an `include_bytes!` entry to
`TRUSTED_KEY_FILES`. Merge the nexus-vfs PR **first** so any cluster
build picking up a `<name>`-signed plugin already trusts the root.

### Rotation

Rotating a master key requires re-sealing every entry — see the
sealed-keystore design doc at
[`docs/superpowers/specs/2026-06-13-sealed-keystore-dogfood-design.md`](../../docs/superpowers/specs/2026-06-13-sealed-keystore-dogfood-design.md).

Rotating a single signing key = mint a new `<name>-v<n+1>`, deprecate
the old name from the trust root in a follow-up nexus-vfs PR, leave
the old sealed entry in place until you're sure no plugin still uses
it.

## What NOT to do

- **Do not commit a plaintext privkey here.** The trust chain breaks
  immediately the moment one lands in `git log`.
- **Do not edit `keys.json` by hand.** The scripts write it
  deterministically (sorted keys, indent 2) so diffs review cleanly;
  hand edits will both round-trip the diff and risk corrupting the
  sealed bytes.
- **Do not rotate `VAULT_SIGNING_MASTER_KEY` without a documented
  re-seal pass.** Every existing entry is bound to the current
  master; rotating without re-sealing locks every plugin out.
