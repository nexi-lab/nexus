# Plugin Signing Dogfood — encrypted-in-git architecture

> **SUPERSEDED 2026-06-13** by
> [`2026-06-13-sealed-keystore-dogfood-design.md`](2026-06-13-sealed-keystore-dogfood-design.md).
>
> Empirical verification on the para-pc Windows box (2026-06-13) proved
> the "commit `vault-meta.redb`" approach this doc describes is
> unbuildable: redb writes a different byte stream on every open+close,
> so the source tree's committed bytes would diverge from vault state
> on every CI run. The sealed-keystore design replaces it — commit a
> JSON of opaque AES-GCM `(nonce, ciphertext)` tuples instead, vault
> rehydrates them via the new `PutSecretSealed` RPC. Kept for the
> historical record (and so the discovery rationale stays anchored).

**Status:** SUPERSEDED — see the link above.

**Scope:** How plugin signing keys live in vault when there is no always-on vault server. The plumbing (gRPC client scripts, reusable workflow, compliance gate, vault wrapper) shipped in PR #4376 — this doc covers the architecture choice that turns "scripts that talk to a vault somewhere" into "scripts that boot vault in CI from an encrypted DB in this repo".

---

## TL;DR

Vault's redb file lives **committed in the nexus repo** at `data/vault-signing/vault.redb`. Vault's per-entry AES-256-GCM encryption hides the privkey values; the repo is public but values are unreadable without the master key. The master key is one GH org secret: `VAULT_SIGNING_MASTER_KEY`.

Each release CI job that uses `signing_key_source: vault`:

1. Installs `nexusd-cluster` (cargo install from pinned nexus-vfs rev)
2. Downloads the latest signed `libnexus_vault.{so,dylib,dll}` + `.sig` from GH Releases
3. Copies `data/vault-signing/vault.redb` to a runner-local data dir
4. Boots an ephemeral `nexusd-cluster` with `--plugin-dir` pointing at vault, master key in env
5. Calls `scripts/vault_get_signing_key.py` against `localhost:2126` (insecure local channel)
6. Signs the new plugin with the returned privkey
7. Tears down the cluster

Adding/rotating a plugin signing key is a **PR** — kernel-team member runs `scripts/provision_dogfood_key.py` locally against the checked-out DB, encrypted DB delta gets committed, PR is reviewed, merged.

---

## Repository layout

```
data/vault-signing/
  README.md          # how to add/rotate keys; pointer to this doc
  vault.redb         # the redb file vault writes to; AES-256-GCM-encrypted values
  pubkeys/           # base64 pubkey checked in alongside each privkey, for visibility + ops sanity
    kernel-dogfood-v1.pub
```

`vault.redb` is committed in binary form. It's a single file, small (KB range with a handful of keys), and changes only when a key is added or rotated.

Trust roots (`<name>.pub` files) live in `nexus-vfs/rust/kernel/trusted_keys/` per existing pattern — separate repo, separate concern.

## Master key

* **Format:** 32 raw bytes, base64-encoded (same as `PLUGIN_SIGNING_PRIVKEY`).
* **Storage:** GH org secret `VAULT_SIGNING_MASTER_KEY` on the `nexi-lab` org, scoped to the `nexus` repo + its release workflows.
* **Generation:** one-time, by the kernel team lead, using a CSPRNG (e.g. `python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"`). Written to the GH secret via `gh secret set --org`.
* **Rotation:** breaks every committed value's decryption. Procedure when needed:
  1. Locally check out current `vault.redb` + master key (kernel-team lead pulls master key from GH secret CLI).
  2. Decrypt all secrets in memory.
  3. Generate new master key.
  4. Re-encrypt all secrets, write a fresh `vault.redb`.
  5. PR the new `vault.redb`; merge.
  6. Update GH secret with new master key.
  7. The merge commit and the secret update must land in lock-step — there's a brief window where one is updated and the other isn't, in which CI release jobs will fail loudly (decrypt error). Acceptable: kernel team coordinates rotation, doesn't bunch with a release.
* **Recovery from loss:** the master key being lost = every committed secret is unrecoverable. Plugin signing privkeys can be regenerated (provision a new key, update the `.pub` in nexus-vfs trust roots, re-tag). One-time cost: tag two `<plugin>-v<n+1>` releases (one with the lost key never coming back, one with a fresh key). No persistent damage as long as we treat plugin signing keys as rotatable infrastructure.

## CI ephemeral cluster startup

Added to `release-plugin-reusable.yml` when `signing_key_source: vault`. New steps (in order):

```yaml
- name: Install nexusd-cluster for ephemeral vault
  if: inputs.signing_key_source == 'vault'
  run: |
    cargo install --git https://github.com/nexi-lab/nexus-vfs \
      --rev <pinned> --locked \
      --bin nexusd-cluster nexus-cluster

- name: Download latest signed vault.dylib from GH Release
  if: inputs.signing_key_source == 'vault'
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  run: |
    mkdir -p /tmp/vault-plugins
    gh release download --repo nexi-lab/nexus --pattern 'nexus-vault-linux-x86_64.tar.gz'
    tar -xzf nexus-vault-linux-x86_64.tar.gz -C /tmp/vault-plugins/
    # Asserts both libnexus_vault.so + .sig are present.

- name: Stage vault DB
  if: inputs.signing_key_source == 'vault'
  run: |
    mkdir -p /tmp/vault-data
    cp data/vault-signing/vault.redb /tmp/vault-data/

- name: Start ephemeral nexusd-cluster
  if: inputs.signing_key_source == 'vault'
  env:
    VAULT_MASTER_KEY: ${{ secrets.VAULT_SIGNING_MASTER_KEY }}
  run: |
    nexusd-cluster --no-tls --data-dir /tmp/vault-data \
      --plugin-dir /tmp/vault-plugins --bootstrap-mode static &
    echo "CLUSTER_PID=$!" >> $GITHUB_ENV
    # Block until /tmp/vault-data/cluster.ready (timeout 60s)
    timeout 60 bash -c 'until [ -f /tmp/vault-data/cluster.ready ]; do sleep 1; done'

- name: Fetch signing privkey from vault
  if: inputs.signing_key_source == 'vault'
  env:
    VAULT_ENDPOINT: localhost:2126
    VAULT_TOKEN: bootstrap-local      # any value — auth disabled on local
    VAULT_INSECURE: "1"
  run: |
    PRIVKEY=$(python scripts/vault_get_signing_key.py "${{ inputs.signing_key_name }}")
    echo "::add-mask::$PRIVKEY"
    echo "PLUGIN_SIGNING_PRIVKEY=$PRIVKEY" >> $GITHUB_ENV

- name: Tear down ephemeral cluster
  if: always() && inputs.signing_key_source == 'vault'
  run: |
    if [ -n "$CLUSTER_PID" ]; then kill "$CLUSTER_PID" || true; fi
    rm -rf /tmp/vault-data /tmp/vault-plugins
```

Open questions / decisions:
* **Pinned nexus-vfs rev:** which rev of nexus-vfs do we install? Pinning to `develop` HEAD risks rev drift. Pinning to a tagged release is cleaner but creates a release-tagging cadence dependency. Recommendation: pin to a specific rev in the workflow, refresh quarterly via PR.
* **Local auth:** vault's gRPC auth surface needs to accept a bootstrap token when `--no-tls` and `127.0.0.1` are in play, OR vault needs an "insecure-local" mode. Confirm with [[feedback_admin_merge_self_approve]] in vault's auth code path before implementing the CI step. If neither exists, ship a small `--admin-token` flag on nexusd-cluster.
* **Chicken-and-egg:** the latest signed `libnexus_vault.so` MUST already exist as a GH Release before any non-vault plugin can be signed. Vault's own release runs the github-secret path — no dependency. The first non-vault plugin (localconnect) requires at least one prior vault release. Acceptable; we'll cut a vault `v0.1.3` release as part of the architecture PR.

## Bootstrap script — `scripts/bootstrap_dogfood_vault.py`

One-time tool to create an empty encrypted DB. Steps:

1. Check `data/vault-signing/vault.redb` does NOT exist — refuse if it does (overwriting would obliterate committed keys).
2. Generate a master key via CSPRNG, print to stdout for the operator to register as GH secret.
3. Start vault locally on a tempdir, then immediately stop. Result: an initialized empty `vault.redb`.
4. Move `vault.redb` into `data/vault-signing/`.
5. Print "next steps": `gh secret set VAULT_SIGNING_MASTER_KEY` then commit + push.

Single-purpose: only used to initialize the encrypted DB the first time.

## Provisioning a new plugin signing key — `scripts/provision_dogfood_key.py`

Already shipped in PR #4376 — works against any vault gRPC endpoint. For encrypted-in-git, kernel team runs it locally:

```bash
# Pull master key from GH secret
export VAULT_MASTER_KEY=$(gh secret get VAULT_SIGNING_MASTER_KEY --org nexi-lab)

# Start a local ephemeral vault pointed at the checked-out DB
mkdir -p /tmp/local-vault-data
cp data/vault-signing/vault.redb /tmp/local-vault-data/
nexusd-cluster --no-tls --data-dir /tmp/local-vault-data \
  --plugin-dir <local vault.dylib> &
LOCAL_VAULT_PID=$!

# Provision the key
python scripts/provision_dogfood_key.py \
  --vault-endpoint localhost:2126 \
  --vault-token bootstrap-local \
  --vault-insecure \
  --key-name <new-key-name>

# Stop vault, copy back the updated DB
kill $LOCAL_VAULT_PID
cp /tmp/local-vault-data/vault.redb data/vault-signing/vault.redb

# Stage the new pubkey, the updated DB, and the trust-root entry
git add data/vault-signing/vault.redb \
        data/vault-signing/pubkeys/<new-key-name>.pub
# In nexus-vfs:
git add rust/kernel/trusted_keys/<new-key-name>.pub
# Edit TRUSTED_KEY_FILES in nexus-vfs/rust/kernel/src/kernel/plugins/loader.rs
```

A wrapper script `scripts/add_dogfood_key.sh` could automate the above. Optional convenience; not required.

## Kernel-team workflow for adding a key (operator view)

1. `git checkout -b add-signing-key/<plugin-name>` on nexus.
2. `./scripts/add_dogfood_key.sh <plugin-name>` (wrapper around the above).
3. Inspect `git status` — should be `data/vault-signing/vault.redb` + `data/vault-signing/pubkeys/<name>.pub`.
4. Commit, push, open PR. PR review checks: pubkey file matches the redb's metadata for that key (script verifies); kernel-team approval required (CODEOWNERS).
5. In nexus-vfs: separate PR adds the `.pub` + the `include_bytes!` entry in `TRUSTED_KEY_FILES`. Merge nexus-vfs first.
6. Bump nexus-vfs pin in `Cargo.toml` (or rely on the next pin bump).
7. Merge the nexus PR.

## Threat model

* **Adversary: read access to the public repo.** Can see `vault.redb` and pubkeys. Cannot decrypt values without master key. No leak.
* **Adversary: stolen master key only.** Can decrypt every committed value. Has every plugin signing privkey. Game over — must rotate every signing key + ship cluster updates with new trust roots. Mitigation: master key only ever in GH org secret (org-admin only) + kernel-team-lead local machine when rotating. Treat as crown jewels.
* **Adversary: write access to the public repo.** Can replace `vault.redb` with one they encrypted with a different master key. Their malicious values would fail decrypt in CI → loud failure. Cannot inject a wrong privkey silently. Mitigation: branch protection requires kernel-team review on `data/vault-signing/**`.
* **Adversary: compromised CI runner during signing.** Sees the privkey for the plugin being signed (a few seconds). Could exfiltrate. Mitigation: minimize runner window, use trusted GH-hosted runners only, audit any self-hosted runner additions.
* **Concurrency: two PRs adding keys.** Second PR's `vault.redb` reflects state without the first PR's key. Merge produces a binary file conflict — git refuses auto-merge, kernel team resolves by re-running the second add against the merged-in first add. Acceptable friction at our key-add cadence.

## Review checklist (next PR)

* [ ] `scripts/bootstrap_dogfood_vault.py` written and self-tested locally (creates an empty `vault.redb`).
* [ ] `data/vault-signing/vault.redb` committed (pre-provisioned with `signing-keys/kernel-dogfood-v1`).
* [ ] `data/vault-signing/pubkeys/kernel-dogfood-v1.pub` committed.
* [ ] Master key generated, registered as `VAULT_SIGNING_MASTER_KEY` GH org secret.
* [ ] `release-plugin-reusable.yml` extended with ephemeral-vault startup steps when `signing_key_source: vault`.
* [ ] `scripts/vault_get_signing_key.py` accepts `VAULT_INSECURE=1` for localhost gRPC.
* [ ] `scripts/provision_dogfood_key.py` same.
* [ ] `release-local-connector-plugin.yml` secrets updated: drop `VAULT_SIGNING_ENDPOINT/TOKEN`, add `VAULT_SIGNING_MASTER_KEY`.
* [ ] `data/vault-signing/README.md` written: rotation procedure, key-add workflow, pointer to this doc.
* [ ] Kernel-team CODEOWNERS entry for `data/vault-signing/**`.
* [ ] Compliance gate flipped to required on `develop` branch protection.
* [ ] Vault `v0.1.3` (or current) tag pushed to ensure a signed dylib exists for ephemeral cluster's `--plugin-dir`.

## Out of scope

* Plugin-author self-service for key addition (PR-based is the SSOT).
* Hardware-backed master key (HSM / TPM) — possible future hardening.
* Per-key access control (today: anyone with master key can read everything in `signing-keys/`).
* Vault DB compaction / archive of rotated keys.
