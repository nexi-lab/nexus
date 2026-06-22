# Local Plugin Signing — dev trust root recipe

**Status:** Active (2026-06-23).  Workaround spec; the long-term fix is the
[sealed-keystore dogfood](./2026-06-13-sealed-keystore-dogfood-design.md)
release pipeline.

**Scope:** How a developer signs locally-built plugin dylibs so the
`PluginLoader` accepts them on their own machine, without going through
the GitHub Actions release pipeline.  Used when (a) the release pipeline
can't emit signed artifacts for a target (e.g. macOS / Windows asset gap)
or (b) the developer needs a same-day turnaround that doesn't fit a tag
+ release cycle.

The kernel-side signing contract is unconditional — there is
[no `--allow-unsigned` escape hatch by design](https://github.com/nexi-lab/nexus-vfs/blob/main/rust/kernel/src/kernel/plugins/loader.rs).
Every loaded plugin must be signed by a key the kernel was compiled to
trust.  This spec is the kernel-team-blessed way to add a dev trust root
to a developer's local kernel build.

---

## TL;DR

```bash
# 1. Generate a fresh Ed25519 keypair (per dev machine, never committed).
python -c "
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
sk = Ed25519PrivateKey.generate()
sk_raw = sk.private_bytes(encoding=serialization.Encoding.Raw, format=serialization.PrivateFormat.Raw, encryption_algorithm=serialization.NoEncryption())
vk_raw = sk.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
print('PRIVKEY=' + base64.b64encode(sk_raw).decode())
print('PUBKEY=' + base64.b64encode(vk_raw).decode())
"

# 2. Drop the pubkey into nexus-vfs as a trusted root.  File path
#    convention: <user>-<host>-dev.pub so it's obvious-on-sight which
#    machine the key lives on and that it's a local-only entry.
cat > nexus-vfs/rust/kernel/trusted_keys/<user>-<host>-dev.pub <<EOF
# LOCAL ONLY — <user>'s <host> dev key for plugin signing.
# Not for commit; release pipeline must produce signed artifacts
# via kernel-dogfood-v1.
<PUBKEY from step 1>
EOF

# 3. Add a matching include_bytes! entry in
#    nexus-vfs/rust/kernel/src/kernel/plugins/loader.rs after the
#    kernel-dogfood-v1.pub block:
#
#    // LOCAL ONLY — <user>'s <host> dev key, see
#    // docs/superpowers/specs/2026-06-23-local-plugin-signing-dev-root.md
#    include_bytes!(concat!(
#        env!("CARGO_MANIFEST_DIR"),
#        "/trusted_keys/<user>-<host>-dev.pub"
#    )),

# 4. Rebuild nexusd-cluster — the trusted-keys list is compile-time
#    embedded, so the new root only takes effect after this step.
cd nexus-vfs && cargo build --release -p nexus-cluster --bin nexusd-cluster

# 5. Sign every dylib that the local nexusd-cluster will load.  The
#    script writes <dylib>.sig (raw 64-byte Ed25519, no header).
PLUGIN_SIGNING_PRIVKEY="<PRIVKEY from step 1>" \
    python nexus/scripts/sign_plugin.py \
    nexus/target/release/<dylib1> \
    nexus/target/release/<dylib2> \
    ...

# 6. Stage <dylib>.{so,dylib,dll} + <dylib>.sig pairs into --plugin-dir.
cp nexus/target/release/{<dylib>,<dylib>.sig} ~/.nexus/plugins/
```

The kernel rebuild in step 4 is mandatory.  Plugin verification reads
the keys via `include_bytes!` at compile time
([`loader.rs:38`](https://github.com/nexi-lab/nexus-vfs/blob/main/rust/kernel/src/kernel/plugins/loader.rs))
so a new `*.pub` file doesn't take effect until the binary that links
the kernel is rebuilt.

---

## Why this is OK to live in a spec

It mutates `trusted_keys/` which is a privileged surface.  The
guardrails that make this a documentable workaround rather than a
loophole:

* **The pubkey list is additive.**  Adding a dev key never weakens
  trust in `kernel-dogfood-v1` or `nexus-team` — it only adds another
  acceptable signer for *this developer's local binary*.
* **The dev key never leaves the dev machine.**  The privkey is
  generated locally, used to sign locally, and discarded (or stored in
  an OS keychain) when done.  No CI surface, no shared infra.
* **The trusted_keys/ edit is uncommitted by convention.**  Both the
  file content and the loader.rs entry carry a `// LOCAL ONLY` marker.
  Any reviewer or future AI auditing the trust roots sees these
  immediately and knows to challenge them.
* **The fix replaces this.**  Once the release pipeline can emit
  signed assets for every target the team uses (the long-term
  [sealed-keystore](./2026-06-13-sealed-keystore-dogfood-design.md)
  work + macOS/Windows asset coverage), this workaround stops being
  needed.  Spec stays as historical context.

---

## Cross-machine considerations

Federation smokes need every node to verify the *peer's* plugin
signatures.  Two shapes work:

* **Same dev key on every node** — copy the pubkey + privkey across
  machines.  Simplest; the privkey is dev-only so the cross-machine
  copy is low-stakes.
* **Different dev key per machine, mutual pubkey trust** — each
  developer adds a `*-dev.pub` file for the *other* developer's key.
  Two `LOCAL ONLY` entries per loader.rs.  Better hygiene (no shared
  privkey), more typing.

Either way the pubkey list is additive — adding more trust roots
doesn't break existing ones.

---

## Lineage / pointers

* Used during the 2026-06-22 Mac↔Win Federation L1 manual smoke when
  PR #4392 had `fuse-v0.3.0` tag → release workflow failure (vault
  rehydrate UNIMPLEMENTED at `dogfood_keystore_fetch.py`) so no signed
  Windows dylib was available from the pipeline.  Win dev key signed
  local builds of `nexus-fuse-plugin` + `nexus-local-connector`;
  rebuilt `nexusd-cluster` embedded the new trust root.  Smoke went
  green; substrate proven for `cc tasks list` cross-machine path.
* Successor: when [sealed-keystore](./2026-06-13-sealed-keystore-dogfood-design.md)
  routing lands + the release pipeline emits all-platform signed
  artifacts, the dev trust root recipe stops applying except for
  truly-local debug work (e.g. running a plugin built from an
  uncommitted experimental branch).
* The kernel-side verifier
  ([nexus-vfs `loader.rs::verify_signature`](https://github.com/nexi-lab/nexus-vfs/blob/main/rust/kernel/src/kernel/plugins/loader.rs))
  reads `<plugin_path>.sig` next to each dylib it loads — same
  64-byte raw Ed25519 layout `sign_plugin.py` produces.
