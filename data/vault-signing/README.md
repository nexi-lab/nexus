# Vault signing keys — encrypted-in-git dogfood

This directory is the persistent home of the dogfood vault DB that holds
every non-vault plugin's signing privkey. The DB is committed in binary
form; vault's AES-256-GCM per-entry encryption hides the privkey values.
The repo is public; values are unreadable without the master key.

Architecture SSOT: `docs/superpowers/specs/2026-06-12-encrypted-in-git-vault-dogfood-design.md`.
Kernel team owns this directory (see `CODEOWNERS`).

## Layout

```
data/vault-signing/
  README.md                  # this file
  vault.redb                 # the redb file vault writes to; encrypted values
  pubkeys/                   # base64 Ed25519 pubkey alongside each privkey
    <key-name>.pub
```

`vault.redb` is generated once by the kernel-team lead via
`scripts/bootstrap_dogfood_vault.py` and never overwritten by routine
edits — only key adds and rotations touch it.

## Bootstrap (one-time, kernel-team lead only)

Required only once per master key. Run on a kernel-team machine with a
local `nexusd-cluster` binary and a built (or downloaded + verified)
`libnexus_vault.so` + `.sig`.

```bash
python scripts/bootstrap_dogfood_vault.py \
  --nexusd-cluster <path-to-nexusd-cluster> \
  --vault-plugin-dir <dir-with-libnexus_vault.so-and-.sig>
```

Output: an initialized empty `data/vault-signing/vault.redb` + the master
key printed to stderr. Then:

1. Register the master key as the GH org secret:
   ```bash
   gh secret set VAULT_SIGNING_MASTER_KEY --org nexi-lab --body '<master-key>'
   ```
2. Stage and commit the DB:
   ```bash
   git add data/vault-signing/vault.redb
   git commit -m 'feat(vault-signing): initial encrypted DB'
   ```

The script refuses to clobber an existing `vault.redb`. Rotation is a
distinct procedure (below).

## Adding a plugin signing key

Each new signing root is one PR. Run locally against the checked-out DB:

```bash
export VAULT_MASTER_KEY=$(gh secret get VAULT_SIGNING_MASTER_KEY --org nexi-lab)
mkdir -p /tmp/local-vault-data
cp data/vault-signing/vault.redb /tmp/local-vault-data/
nexusd-cluster --no-tls --data-dir /tmp/local-vault-data \
  --plugin-dir <local-vault-plugin-dir> &
LOCAL_PID=$!
# Wait for /tmp/local-vault-data/cluster.ready (or sleep ~5s)

python scripts/provision_dogfood_key.py \
  --vault-endpoint localhost:2126 \
  --vault-token bootstrap-local \
  --insecure \
  --key-name <new-key-name>

kill $LOCAL_PID
cp /tmp/local-vault-data/vault.redb data/vault-signing/vault.redb
mv <new-key-name>.pub data/vault-signing/pubkeys/
```

Stage `data/vault-signing/vault.redb` + `data/vault-signing/pubkeys/<new-key-name>.pub`,
open the PR. CODEOWNERS gates kernel-team approval.

After this PR merges, ship a sibling nexus-vfs PR adding
`rust/kernel/trusted_keys/<new-key-name>.pub` + an `include_bytes!` entry
in `TRUSTED_KEY_FILES` (`rust/kernel/src/kernel/plugins/loader.rs`). Merge
nexus-vfs **before** any plugin signed with the new key reaches a
consumer cluster.

## Rotation (master key)

Rotation breaks every committed value's decryption. Procedure:

1. Locally export the current master key from the GH secret and decrypt
   every secret in memory (one-off script — TBD in a follow-up).
2. Generate a new master key via CSPRNG.
3. Re-encrypt all secrets, write a fresh `vault.redb`.
4. PR the new DB. CODEOWNERS gates review.
5. Update the GH org secret in lock-step with the merge — between merge
   and secret update CI release jobs will fail loudly (decrypt error).
   Coordinate; don't bunch with a release.
6. Lost master key = every committed secret is unrecoverable. Plugin
   signing privkeys are designed to be rotatable: re-provision via fresh
   `<key-name>-v<n+1>`, ship the new pubkey through the nexus-vfs trust
   root, re-tag releases.

## Concurrency

Two PRs adding keys race on `vault.redb`. The second PR's redb reflects
state without the first PR's key; merge produces a binary file conflict
git refuses to auto-resolve. The second PR's author re-runs provisioning
against the merged-in first PR's DB. Acceptable friction at our
key-add cadence.

## Threat model

See the design doc — the short version: read access to this repo reveals
the encrypted DB and pubkeys but not privkey values; master-key
compromise = full disclosure of every committed signing privkey, requires
rotating every plugin signing root + shipping cluster trust-root updates.
