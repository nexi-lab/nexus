# Plugin Signing Dogfood — sealed-keystore architecture

**Status:** Active (2026-06-13). Lands in the PR titled
`feat: sealed-keystore dogfood signing` against `develop`. Supersedes
[`2026-06-12-encrypted-in-git-vault-dogfood-design.md`](2026-06-12-encrypted-in-git-vault-dogfood-design-SUPERSEDED.md).

**Scope:** How plugin signing keys live in vault when there is no
always-on vault server. The plumbing (compliance gate, reusable
release workflow, wrappers for vault/local-connector/fuse-plugin)
shipped in PR #4376. Phase P (nexus-vfs#57, merged 2026-06-13) added
the plugin-as-gRPC-service routing path that vault.dylib needs to
actually serve external `PutSecret` / `GetSecret` calls. Phase E'
(PR #4381) shipped `vault-v0.1.3` with the routing opt-in + the new
sealed `Get`/`Put` RPCs. This doc covers the architecture choice
that turns "scripts that talk to a vault somewhere" into "scripts
that boot vault in CI from a JSON of sealed bytes committed in this
repo".

---

## TL;DR

The committed source-tree state for the plugin-signing root is a
JSON file of opaque AES-GCM ciphertexts:

```
data/vault-signing/
├── README.md
├── keys.json          {"<name>": {"namespace", "nonce", "ciphertext", "description"}, ...}
└── pubkeys/
    └── <name>.pub     base64(32 raw Ed25519 verifying-key bytes)
```

The master key — 32 random bytes, base64-encoded — is the
`VAULT_SIGNING_MASTER_KEY` nexi-lab GitHub org secret. Vault holds
it briefly when it runs; the source tree never sees plaintext.

Each release CI job with `signing_key_source: vault`:

1. Reads `VAULT_SIGNING_MASTER_KEY` from env.
2. `cargo install`s `nexusd-cluster` from the workspace-pinned
   `nexus-vfs` rev.
3. Downloads the latest `vault-v*` GH release's Linux dylib.
4. Spawns an ephemeral tempdir, writes `master.key`, copies the dylib
   under `plugins/`, runs
   `nexusd-cluster --plugin-dir <tempdir>/plugins --no-tls --bind 127.0.0.1:0`,
   polls the chosen port.
5. Calls `scripts/dogfood_keystore_fetch.py --key-name <name>` which:
   a. compiles `secrets.proto` in process,
   b. `PutSecretSealed(nonce, ciphertext)` for every entry in
      `keys.json` (rehydrate),
   c. `GetSecret(namespace="signing-keys", key=<name>)` returns the
      plaintext base64-of-privkey-bytes,
   d. tears down cluster, shreds master key.
6. The runner `::add-mask::`s the value, writes it to
   `$PLUGIN_SIGNING_PRIVKEY`, then `sign_plugin.py` signs the new
   dylib exactly the same way the `github-secret` path does.

That's it. Same downstream signer, same trust contract on the
verifier side (nexus-vfs `TRUSTED_KEY_FILES`).

---

## Why sealed-keystore (G3) over the alternatives

The 2026-06-13 verification considered four architectures. Empirical
findings drove the choice:

| Option | Idea | Why dropped (or chosen) |
|---|---|---|
| **A\*** | Commit vault's internal `vault-meta.redb` + `content/` files to git. | DEAD. Measured: redb dirties `vault-meta.redb` bytes on bare `open()` + `close()` with no writes (548864 → 294912 bytes, different SHA-256). The source tree's committed bytes would diverge from vault state on every CI run. |
| **E** | New vault `Export(master_key) → bytes` / `Import(bytes)` RPC pair producing an opaque archive. | Heavier vault Rust delta, binary archive harder to PR-review, no other consumer. Tracked in the Backlog of the implementation plan; not blocking. |
| **D** | Python AES-256-GCM seal/open in `dogfood_keystore_fetch.py` directly — skip vault entirely. | Drops dogfood: vault's signing path stops exercising vault.dylib in CI, defeating the whole point. Also unblocks nothing for sudowork `pwd_login` which needs the real vault gRPC path. |
| **G3 (chosen)** | Add `GetSecretSealed` / `PutSecretSealed` to vault's `GenericSecretsService`. Source tree commits JSON of `(nonce, ciphertext)`. Vault re-seals/un-seals via its existing master-key path. | Smallest vault delta, JSON-friendly diff, fixes the root cause (redb non-determinism), unblocks sudowork `pwd_login` as a side-effect (also a real-vault external-gRPC consumer). |

---

## What runs where

```
Operator's laptop                                Source tree              CI runner
─────────────────────────                        ───────────────          ─────────────────────────────
dogfood_keystore_init.py
  os.urandom(32) ────► master key ─────────────► (out-of-band:
                                                  set as GH org secret
                                                  VAULT_SIGNING_MASTER_KEY)

  data/vault-signing/keys.json := "{}"  ◄──── git commit + PR ──►
  data/vault-signing/pubkeys/.gitkeep

provision_dogfood_key.py
  ┌────────────────────────────────────────┐
  │ tempdir/                               │
  │   vault/master.key (read from env)     │
  │   plugins/libnexus_vault.so            │
  │                                        │
  │ spawn nexusd-cluster ◄── gRPC ─────────│► PutSecretSealed(rehydrate)
  │                                        │  PutSecret(plaintext privkey)
  │                                        │  GetSecretSealed   ──► (nonce, ct)
  │                                        │
  │ kill cluster                           │
  └────────────────────────────────────────┘
  data/vault-signing/keys.json[new-name] = …       ◄── git commit + PR ──►
  data/vault-signing/pubkeys/new-name.pub
  (drop matching .pub into nexus-vfs trust root in a sibling PR)

                                                                          dogfood_keystore_fetch.py
                                                                            ┌─────────────────────────┐
                                                                            │ tempdir/                │
                                                                            │   vault/master.key      │
                                                                            │     (read from env)     │
                                                                            │   plugins/...so         │
                                                                            │     (gh release dl)     │
                                                                            │                         │
                                                                            │ spawn nexusd-cluster    │
                                                                            │      ◄── gRPC ──────────│► PutSecretSealed (rehydrate)
                                                                            │                         │  GetSecret ──► plaintext
                                                                            │ kill cluster            │
                                                                            └─────────────────────────┘
                                                                            ::add-mask:: ──► $PLUGIN_SIGNING_PRIVKEY
                                                                            sign_plugin.py picks it up from env
```

---

## Master-key lifecycle

### Bootstrap (one-time)

```sh
python scripts/dogfood_keystore_init.py
```

- `os.urandom(32)` → master key.
- Writes `data/vault-signing/keys.json = {}` and `pubkeys/.gitkeep`.
- Prints base64 master key to stdout.
- Operator registers as the `VAULT_SIGNING_MASTER_KEY` GitHub org
  secret on `nexi-lab`.
- Operator commits the empty keystore.

### Steady state

The master key lives in two places, both controlled:

- **GH org secret** — read by CI workflows via `secrets.VAULT_SIGNING_MASTER_KEY`.
- **Operator's local env** when running `provision_dogfood_key.py`.

It never lands on disk in the repo, never appears in logs (the
fetcher `::add-mask::`s its output anyway), and shreds itself from
process memory at end of each run.

### Rotation

Rotating the master key requires re-sealing every existing entry,
because each `(nonce, ciphertext)` pair is bound to the current
master. Procedure:

1. Mint a new candidate master key locally:
   `python -c 'import os,base64; print(base64.b64encode(os.urandom(32)).decode())'`
2. With the OLD `VAULT_SIGNING_MASTER_KEY` env, run
   `dogfood_keystore_fetch.py` for every entry, capture plaintexts in
   memory (not on disk).
3. With the NEW master key in env, run a one-off script that
   `PutSecret`s each plaintext, then captures `GetSecretSealed` and
   writes the rotated `keys.json`.
4. Update the GH org secret to the new value.
5. Open a PR with the rotated `keys.json`; CODEOWNERS gates kernel-
   team review.
6. Merge; verify the next plugin release CI run signs successfully.
7. Until step 5 lands, the workflow runs against the OLD master + OLD
   sealed entries — no window of brokenness.

We don't currently ship the rotation script — it's a future
follow-up. The conceptual scope is small but deserves its own
review.

---

## Threat model

| Threat | Mitigation |
|---|---|
| Source-tree leak (clone, archive, etc.) reveals `keys.json` plaintext. | `keys.json` only carries AES-256-GCM ciphertexts. Without the master key the bytes are opaque. |
| `keys.json` corruption (accidental edit, merge mishap). | CODEOWNERS forces kernel-team review on any diff. Scripts write deterministically (sorted keys, `indent=2`) so corruption stands out in PR review. |
| Master key compromise (GH org secret leak). | All dogfood-signed plugins must be re-signed under a new key root. Mitigations: rotate master immediately, mint fresh signing keys, drop old pubkeys from nexus-vfs trust root, force re-build of every cdylib. Worst case = window of trust from leak to drop. |
| Master key shipped to disk by accident (e.g. a script that writes env to a file). | Scripts only `os.urandom(32)` or `read_master_key_from_env`; never write it. CODEOWNERS gating slows accidental introductions. |
| Tampered sealed entry (replace one `keys.json` entry with another). | A swap would either decrypt to a wrong plaintext (GCM tag mismatch → ciphertext failure → fetcher errors), or to a different but valid signing key. Defense in depth: nexus-vfs only trusts `<name>.pub` files dropped explicitly into `TRUSTED_KEY_FILES`; swapping `keys.json[name]` for a different keypair would mean a different signing key signs but the kernel only trusts the original pubkey, so the plugin signature fails dlopen. |
| Cluster spawned in CI binds non-loopback. | `dogfood_keystore_fetch.py` passes `--bind 127.0.0.1:0` to `nexusd-cluster`. Sealed RPC handlers also reject non-loopback peers at the gRPC layer (Phase E3's `require_localhost_caller`). Belt-and-braces. |
| Master key replayed to the wrong vault instance. | Each ephemeral cluster is spawned in a fresh tempdir per CI job. There is no other vault instance that could be talking to the same master key — by design, the production vault path is dead. |

---

## CODEOWNERS gating

Kernel-team approval is required for any change to:

- `data/vault-signing/` — sealed keystore + pubkeys.
- `scripts/_dogfood_vault.py` — shared cluster helper.
- `scripts/dogfood_keystore_init.py` — bootstrap.
- `scripts/dogfood_keystore_fetch.py` — CI fetcher.
- `scripts/provision_dogfood_key.py` — operator writer.
- `scripts/vault_get_signing_key.py` — legacy generic client.
- `scripts/sign_plugin.py` — signer.
- `scripts/check_plugin_signing_compliance.py` — compliance gate.
- `.github/workflows/release-plugin-reusable.yml` — fetch + sign pipeline.
- `.github/workflows/release-{vault,local-connector,fuse}-plugin.yml`
  — per-plugin wrappers.

Bytes-equal mirrors live in `nexus-vfs/rust/kernel/trusted_keys/`;
that mirror's drift is policed by
`scripts/check_plugin_signing_compliance.py` (extended cross-repo
check is a follow-up in the implementation plan).

---

## Open follow-ups

- Rotation script: one-off helper that re-seals every entry under a
  new master key. Conceptually small, separate PR.
- `Export(master_key) / Import(bytes)` vault RPCs (Option E) — useful
  for future bulk migrations or backups; not blocking dogfood.
- Cross-repo trust-root mirror check in
  `check_plugin_signing_compliance.py` — verify every
  `data/vault-signing/pubkeys/*.pub` has a matching entry at the
  pinned nexus-vfs rev's `TRUSTED_KEY_FILES`. Tracked in the
  implementation plan as Phase B2c'.
