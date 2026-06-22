# Cross-Machine Federation over Tailscale/Headscale — Complete Runbook

**Status:** verified end-to-end on 2026-06-06, Mac↔Win L1 federation cross-node read passes byte-exact.

> **Doc SSOT.**  This file supersedes nexi-lab/nexus discussion #2596 ("Cross-Machine Federation over Tailscale/Headscale — Complete Runbook").  That discussion is now closed and should be removed; new content for this surface goes here.

---

## What this covers

A 2-node Nexus federation cluster across two machines (typically macOS + Windows) connected via Tailscale VPN, with:

* L1 cross-machine CRUD byte-exact in both directions
* The full operator flow: Tailscale setup → build → bootstrap → share/join → smoke

If you only need the conceptual picture without the operator commands, jump to [Architecture](#architecture) and [Key design decisions](#key-design-decisions).  The [Operator flow](#operator-flow) sections are the concrete commands.

---

## Repo split (since #4259, 2026-06-03)

Kernel-tier crates (`contracts`, `lib`, `transport`, `kernel`, `backends` source, `raft`, `profiles/cluster`, `plugin-abi`, plus `proto/`) live in **[nexi-lab/nexus-vfs](https://github.com/nexi-lab/nexus-vfs)**.  This `nexus` repo holds the service tier (`services`, including service-plugin dylib sub-crates such as `services/vault/`) and the driver-plugin dylib sub-crates under `backends/` (`backends/local-connector/`, …), and consumes the kernel tier as git dependencies in `Cargo.toml`.  Each plugin dylib crate compiles to a cdylib loaded by `nexusd-cluster` via `--plugin-dir`.

Directory pairing tracks the plugin ABI: service-kind plugins (`declare_service_plugin!`) sit under `rust/services/<name>/`; driver-kind plugins (`declare_driver_plugin!`) sit under `rust/backends/<name>/`.  `rust/backends/` is shared with nexus-vfs at the directory level — nexus-vfs owns the source crate at the root, nexus owns the dylib sub-crates underneath.  The boundary gates below enforce that split mechanically.

Two CI gates enforce the split:

| Repo | Workflow | Fails on |
|------|----------|----------|
| nexus | `.github/workflows/repo-boundary.yml` | Re-introducing any of `rust/contracts`, `rust/lib`, `rust/transport`, `rust/kernel`, `rust/backends/Cargo.toml` or `rust/backends/src/` (the kernel-tier source crate), `rust/raft`, `rust/profiles/cluster`, `proto/` |
| nexus-vfs | `.github/workflows/repo-boundary.yml` | Re-introducing any of `rust/services`, `rust/backends/local-connector` |

`nexusd-cluster` is built from **nexus-vfs**.  Federation behaviour fixes land there; this `nexus` repo only updates the git-dep SHA when it needs kernel-tier changes.

---

## Architecture

```
Headscale server (company-managed)
  headscale.sudoprivacy.com
            │
  ┌─────────┼─────────┐
  │  Tailscale WireGuard mesh    │
  │                              │
┌─┴──────┐              ┌────────┴─┐
│Machine A│◄════════════►│ Machine B│
│(founder)│   encrypted │ (joiner) │
│:2126    │    P2P      │ :2126    │
│:2028    │   tunnel    │ :2028    │
└─────────┘             └──────────┘
```

* **Port 2126** — Raft gRPC + ZoneApiService + VfsService.  Hosts inter-node consensus, JoinZone / Propose RPCs, and the kernel's VFS server.  Mounted as the cluster binary's primary listener.
* **Port 2028** — Reserved for future VFS-only client surface; not used by `nexusd-cluster` today.
* **Founder** — First node, brings up the root zone as a 1-voter cluster, then optionally creates federation zones from env (`NEXUS_FEDERATION_ZONES` / `NEXUS_FEDERATION_MOUNTS`).
* **Joiner** — Second node, brings up its own local root, then `nexusd-cluster join` against the founder to subscribe to a federation zone and write a local DT_MOUNT.

---

## Prerequisites

* `nexus-vfs` repo cloned on both machines (for the `nexusd-cluster` build).  This `nexus` repo is **not** required on the cluster machines unless you also want service-tier plugins (vault cdylib, etc.).
* Rust toolchain (stable; the cluster binary builds on the `release` profile).
* Tailscale client installed on both machines.
* Headscale pre-auth key (from IT) if joining the corporate mesh.
* **For `cc tasks list` cross-machine workflow** (Step 3g) — operating-system FUSE userspace:
  * **Linux**: `apt install fuse3 libfuse3-3`.
  * **macOS**: `brew install macfuse-t`.  No kernel extension approval, no reboot — FUSE-T runs the FUSE protocol over a localhost NFS loopback, so the macOS kernel needs nothing beyond its built-in NFS client.  Use FUSE-T instead of macFUSE: macFUSE needs an interactive System Settings → Privacy & Security approval + a reboot per host, which doesn't fit a one-command operator install (the GPL license also blocks bundling it into a closed-source installer).  FUSE-T is MIT-licensed and exposes the same libfuse3 API surface the `fuser` Rust crate consumes, so the plugin source is unchanged.
  * **Windows**: `choco install winfsp -y` (Administrator PowerShell) or `winget install --id WinFsp.WinFsp --silent`.  WinFsp is the Windows kernel-side userspace-filesystem driver `nexus-fuse-plugin` consumes via the `winfsp` Rust crate (different binding from `fuser`, but the same KernelHandle ABI surface).  The driver installs at `C:\Program Files (x86)\WinFsp\bin\winfsp-x64.dll` but the installer does NOT add that dir to system PATH.  Before launching the daemon, prepend it: `$env:PATH = "C:\Program Files (x86)\WinFsp\bin;$env:PATH"`.  Without this, the plugin DLL load fails at `LoadLibraryExW` with error 126 ("module not found") because the static import on `winfsp-x64.dll` can't be resolved.

---

## Operator flow

### Step 1 — Tailscale

**1a. Install Tailscale**

macOS — App Store, then point at Headscale:

```bash
defaults write io.tailscale.ipn.macos ControlURL "https://headscale.sudoprivacy.com"
# Restart Tailscale.app, then menu bar → Log in
```

Windows:

```powershell
winget install --id Tailscale.Tailscale --accept-package-agreements --accept-source-agreements
```

**1b. Pre-auth key from Headscale admin**

```bash
headscale preauthkeys create --user nexus --reusable=false --expiration 24h
```

**1c. Register**

macOS:

```bash
headscale nodes register --user nexus --key <key-from-url>
```

Windows:

```powershell
tailscale up --login-server https://headscale.sudoprivacy.com --authkey tskey-auth-XXXXX
```

**Never use `tailscale up --reset`.**  It wipes the node key and re-registration loops fail until manual cleanup.  Use plain `tailscale up`, or restart the service, to nudge state.

**1d. Verify connectivity**

```bash
tailscale status                # both nodes appear
tailscale ping <other-ip>       # "pong" with latency, ideally "direct" not "via DERP"
```

**1e. Windows — the GUI must be running**

`tailscaled.exe` (the service) alone does **not** hold a connected session on Windows.  The frontend `C:\Program Files\Tailscale\tailscale-ipn.exe` must also run.  After reboot, the GUI does not always auto-start, so `tailscale status` reports `unexpected state: NoState` even though the service is up.  Manually launch the GUI once per boot, or pin it to startup.

**1f. Clash Verge / VPN coexistence**

In the *active* profile's merge file (not just the global `Merge.yaml`), add `route-exclude-address`:

```yaml
tun:
  route-exclude-address:
    - 10.99.0.0/24                 # internal nets you bypass
    - 100.64.0.0/10                # Tailscale CGNAT range
    - 82.157.97.230/32             # Headscale server IP

dns:
  fake-ip-filter:
    - 'headscale.sudoprivacy.com'
    - '*.tailscale.com'
    - '+.headscale.sudoprivacy.com'
```

The path to the active profile's merge file looks like:

```
~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles/<active-uid>.yaml
%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\profiles\<active-uid>.yaml
```

If you only set this in the global Merge template and not the active profile's copy, Clash silently ignores it — that has been the root cause of "Tailscale dropped after Clash restart" multiple times.

Clash Verge cleanup footguns when quitting Clash:

1. System proxy not cleared on exit — clear it manually.
2. DNS not reverted — set DNS back to DHCP/empty.
3. `route-exclude-address` lives in the active-profile merge file, *not* just the global template — verify via the path above.

**1g. Firewall**

Windows (PowerShell as admin):

```powershell
New-NetFirewallRule -DisplayName "Nexus Raft gRPC" -Direction Inbound -Protocol TCP -LocalPort 2126 -Action Allow
```

macOS — usually no action needed; App Store Tailscale uses the Network Extension surface.

### Step 2 — Build

On both machines, from a clone of **nexus-vfs**:

```bash
git fetch origin main && git checkout main && git pull --ff-only
cargo build --release -p nexus-cluster
# binary: target/release/nexusd-cluster (or .exe on Windows)
```

That's it — no Python wheel needed.  The `nexusd-cluster` profile is the canonical entry; the Python runtime is no longer built or tested in this workspace.

Windows extra: open a Developer Command Prompt for VS Build Tools first so `cl.exe` / `link.exe` are on the linker search path.  See [Rust Build Env (Windows)](#) memory note if links fail with `LNK1181`.

### Step 3 — Bootstrap the cluster

Key contract rules:

* **`--bootstrap-mode` is required when federation is in play** (PR #4028).  Pass `static` for env-driven topology (`NEXUS_FEDERATION_*` carry the cluster shape, used across first boot and subsequent container restarts), `restart` for operator-managed resume where persisted ConfState is the source of truth (no env needed), `dynamic` for runtime-API-driven setups.
* **`--peers` lists only the *other* machines.**  Self-listed peers are rejected at parse (PR #4014).
* **`NEXUS_HOSTNAME` should be the node's Tailscale IP** for unambiguous self-detection across machines.
* **`NEXUS_DATA_DIR` and `--data-dir` must point to the same directory.**
* **`--no-tls` is fine for local testing.**  Without it the daemon auto-detects certs in `<data-dir>/tls/`.
* **Windows MSYS / Git Bash operators**: set `MSYS_NO_PATHCONV=1` in the shell *before* exporting `NEXUS_FEDERATION_MOUNTS`, or single-quote the value (`'NEXUS_FEDERATION_MOUNTS=/shared=sharedzone'`).  Without one of these, the shell rewrites the leading `/shared` into `C:/Program Files/Git/shared` before `nexusd-cluster` ever sees the env var; the daemon then boots with `mount_count=0` and the `/shared` namespace stays un-federated.  Post-nexus-vfs#39 the cluster binary refuses to start in this state and names the workaround in the error; on older binaries the symptom is a silent zero-mount federation that surfaces as downstream raft replication failures hours later.

**3a. Founder (machine A, first boot)**

```bash
rm -rf /tmp/nexus-fed-data && mkdir -p /tmp/nexus-fed-data

NEXUS_HOSTNAME=<A_tailscale_ip> \
NEXUS_NO_TLS=true \
NEXUS_FEDERATION_ZONES=sharedzone \
NEXUS_FEDERATION_MOUNTS=/shared=sharedzone \
target/release/nexusd-cluster \
  --bind-addr 0.0.0.0:2126 \
  --data-dir /tmp/nexus-fed-data \
  --no-tls \
  --bootstrap-mode static
```

Wait for:

```
Zone 'root' registered (node_id=<A_node_id>, peers=1)
Zone 'sharedzone' registered ...
install_mount_apply_cb: slot set parent_zone_id=root
install_mount_apply_cb: slot set parent_zone_id=sharedzone
wire_mount: installing distributed locks bound to ROOT zone parent_zone=root mount_path=/shared
Static topology applied: 1 mounts via raft consensus
```

The `Static topology applied` line should land within ~400 ms of `Zone 'root' registered` on a healthy 1-voter founder.  If it takes ~10 s and you see a `Forward to leader failed leader=<A_node_id>` warning in between, you're running a pre-nexus-vfs-#25 binary — rebuild.

**3b. Joiner (machine B, first boot)**

Step B-1: bring up B's own local root.

```bash
rm -rf /tmp/nexus-fed-data && mkdir -p /tmp/nexus-fed-data

NEXUS_HOSTNAME=<B_tailscale_ip> \
NEXUS_NO_TLS=true \
target/release/nexusd-cluster \
  --bind-addr 0.0.0.0:2126 \
  --data-dir /tmp/nexus-fed-data \
  --no-tls \
  --bootstrap-mode static \
  > /tmp/nexus-mac.log 2>&1 &

until grep -q "Zone 'root' registered" /tmp/nexus-mac.log; do sleep 1; done
pkill -f nexusd-cluster
sleep 2
```

Step B-2: join A's `sharedzone` (offline subcommand, daemon stopped).

```bash
target/release/nexusd-cluster join \
  "<A_node_id>@<A_tailscale_ip>:2126" \
  sharedzone \
  /shared \
  --hostname <B_tailscale_ip> \
  --data-dir /tmp/nexus-fed-data \
  --no-tls \
  --as <learner|voter>
```

`--as` picks the membership role on `sharedzone` (default `learner`):

* **`--as learner`** — owner-pattern share.  Joiner gets full replication of `sharedzone` metadata + can write `sys_setattr` / `sys_unlink` metadata via the EC default path (see [Consistency model](#consistency-model) below), but doesn't count toward quorum.  Wipe-rejoin safe — losing or replacing a learner has zero quorum impact, so SSD swap / OS reinstall / device migration can't strand the zone in `not leader` deadlock.  Pick this when one side is the authoritative writer and the other side is mostly read-along (canonical `nexus share` semantics).
* **`--as voter`** — symmetric-peer share.  Joiner counts toward quorum AND can write SC linearizable ops (locks, CAS).  EC-routed `sys_setattr` still works without quorum (so single-peer-online still lets that peer write metadata), but lock acquire / CAS need majority ACK.  Wipe-rejoin risk re-emerges if a voter goes through SSD swap without first transferring its voter slot away.  Pick this for the cc-tasks-share Mac↔Win pattern where both peers write to their own subpath under `/shared` and want equal authority.

Expected last line:

```
Joined remote zone 'sharedzone' as <learner|voter> (via http://<A_tailscale_ip>:2126); mounted at '/shared' inside zone 'root'
```

Each node's local root zone owns its DT_MOUNT routing entries.  `join` writes B's DT_MOUNT into B's local root.  A's local root has its own DT_MOUNT for `/shared` (from the `NEXUS_FEDERATION_MOUNTS` env at A's boot).  They do not need to agree on root-zone membership — the parent zone for each side's DT_MOUNT is *that side's* local root.  See [Key design decisions](#key-design-decisions) for the rationale.

Step B-3: restart B's daemon in restart mode.

```bash
NEXUS_HOSTNAME=<B_tailscale_ip> \
NEXUS_NO_TLS=true \
target/release/nexusd-cluster \
  --bind-addr 0.0.0.0:2126 \
  --data-dir /tmp/nexus-fed-data \
  --no-tls \
  --bootstrap-mode restart \
  > /tmp/nexus-mac.log 2>&1 &
sleep 8
```

Under `restart`, do **not** pass `--peers`, `--bootstrap-new`, `NEXUS_FEDERATION_ZONES`, or `NEXUS_FEDERATION_MOUNTS` — the persisted ConfState and DT_MOUNT entries are the source of truth.

### Step 3e — Expose host paths via LocalConnector

The `local-connector` driver dylib projects a host filesystem subtree (e.g. `~/.claude/tasks/`) into the VFS at an operator-named path.  Reads and writes through the VFS surface flow through to the configured `local_root` on the host fs — the LocalConnector's defining SSOT property.

Drop the dylib into `NEXUS_PLUGIN_DIR` and mount it at boot:

```bash
mkdir -p ./plugins
cp /path/to/libnexus_local_connector.so ./plugins/

target/release/nexusd-cluster \
  ... \
  --plugin-dir ./plugins \
  --mount-driver 'local-connector:root:/tasks:{"local_root":"/home/me/.claude/tasks"}'
```

The second segment names the zone the mount lives in.  `root` is the canonical single-node case (same-canonical routing keeps the mount strictly local).  A separate raft zone is the form operators use when the mount needs to compose with future cross-node operator-mount substrate.  Reads and writes through the VFS gRPC surface go straight to the host fs:

```bash
grpcurl -plaintext \
  -d '{"path":"/tasks/session-foo/1.json","content":"..."}' \
  <addr>:2126 \
  nexus.grpc.vfs.NexusVFSService/Write
```

The CI regression for this surface lives in `tests/e2e/docker/test_cc_tasks_share_e2e.py`.  The `--mount-driver` argument's grammar is documented in `KERNEL-ARCHITECTURE.md §10.4a`.

### Step 3f — Cross-node host-fs sharing (cc-tasks-share-style)

Node A and node B each mount their own LocalConnector under a hostname-namespaced path inside the same federated zone.  Reads on B for a path under A's mount resolve to A's host fs without manual sync — the lazy-observe substrate routes them via Mode B (cold fan-out) on the first read and Mode A (`try_remote_fetch` via `last_writer_address`) on every subsequent read (see `docs/federation-architecture.md §6.7`).

Operator flow (mirrors what `dockerfiles/docker-compose.cc-tasks-share.yml` automates):

```bash
# On A (founder of sharedzone):
NEXUS_FEDERATION_ZONES=sharedzone \
NEXUS_FEDERATION_MOUNTS=/shared=sharedzone \
nexusd-cluster --bootstrap-mode static \
  --plugin-dir ./plugins \
  --mount-driver 'local-connector:sharedzone:/shared/cc-tasks/A:{"local_root":"/home/me/.claude/tasks"}' \
  ...

# On B (joiner):
# 1. First boot — daemon ignores --mount-driver because sharedzone is not yet loaded
#    locally; the substrate explicitly skips operator driver mounts on zones that
#    don't exist yet, preventing a parallel-bootstrap split-brain.
nexusd-cluster --bootstrap-mode static \
  --plugin-dir ./plugins \
  --mount-driver 'local-connector:sharedzone:/shared/cc-tasks/B:{"local_root":"/home/me/.claude/tasks"}' \
  ...

# 2. Offline join (daemon stopped) — seeds sharedzone's ConfState + log into B's data dir.
nexusd-cluster join <A_node_id>@<A_addr> sharedzone /shared --data-dir ./data --hostname B

# 3. Restart — entrypoint auto-detects restart mode; sharedzone replays from disk and
#    --mount-driver runs successfully because the zone is now loaded.
```

After the restart, reads on B for any path under `/shared/cc-tasks/A/...` route through `/sharedzone/shared` (the federation mount), miss locally, fan out to A, and return A's host-fs bytes.  Subsequent reads take Mode A.  Symmetric on A reading `/shared/cc-tasks/B/...`.

This is the substrate the `cc tasks list` cross-machine workflow rides — operator's CC daemon on A drops `~/.claude/tasks/<n>.json` directly to host fs (no Nexus syscall), and a second CC daemon on B reads them through `/shared/cc-tasks/A/<n>.json`.

### Step 3g — Expose the federated VFS as a real OS mount via FUSE

§3f gets the bytes across; `cc tasks list` (which is just `ls ~/.claude/tasks/`) needs the bytes to surface as **real files in the OS filesystem** so plain POSIX tools see them.  The `nexus-fuse-plugin` cdylib does exactly that — it spawns a fuser-backed FUSE event loop in the same process as the kernel and routes POSIX ops through the same `KernelHandle` callbacks the kernel exports to any other plugin.

Drop the signed dylib + `.sig` into `NEXUS_PLUGIN_DIR` (same dir LocalConnector lives in) and set two env vars before launching `nexusd-cluster`:

```bash
mkdir -p ~/.nexus/plugins
# Download the latest fuse-v* release for your platform
gh release download fuse-v0.2.0 \
    --pattern '*linux-x86_64.tar.gz' \
    --dir /tmp/      # → libnexus_fuse_plugin.so + .sig
tar -xzf /tmp/nexus-fuse-plugin-*-linux-x86_64.tar.gz -C ~/.nexus/plugins/

export NEXUS_FUSE_MOUNT_POINT=/mnt/cc-tasks       # absolute path; must exist + be empty
export NEXUS_FUSE_VFS_ROOT=/shared/cc-tasks       # VFS path the mount root maps to

nexusd-cluster \
  --plugin-dir ~/.nexus/plugins \
  --bind-addr 0.0.0.0:2126 \
  --data-dir ./data \
  ... # other args from §3f
```

After the daemon comes up, `mountpoint -q /mnt/cc-tasks` returns 0 and the directory acts as a normal POSIX surface:

```bash
ls /mnt/cc-tasks/                # → A/  B/  (the two host-fs LocalConnector mounts)
ls /mnt/cc-tasks/A/              # → A's ~/.claude/tasks/ contents
echo '{"task":"foo"}' > ~/.claude/tasks/session-x/1.json
ls /mnt/cc-tasks/A/session-x/    # → 1.json (same machine, via LocalConnector)
# And on machine B:
ls /mnt/cc-tasks/A/session-x/    # → 1.json (federation fan-out + LocalConnector on A)
```

The FUSE plugin and LocalConnector compose without coupling — LocalConnector is the *write* surface (host fs is the SSOT, every write goes there bypassing Nexus), FUSE plugin is the *unified read* surface (`ls` sees both local + remote tasks through the federated VFS).  The cc-tasks-share Docker E2E (`tests/e2e/docker/test_cc_tasks_share_e2e.py`) regression-guards the full chain — FUSE op → KernelHandle v3 callback → DT_MOUNT routing → federation fan-out (when crossing nodes) → peer LocalConnector → host fs — as longer cross-layer workflows on a founder + joiner topology.

Platform matrix:

| Platform | FUSE userspace | Status |
|----------|----------------|--------|
| Linux    | `libfuse3` (`fuse3`) | First-cut. |
| macOS    | `FUSE-T` (`fuse-t`)  | First-cut.  Same `fuser` source body — FUSE-T exposes the libfuse3 ABI, so the macOS build path is the cfg-default `fuser` branch with no per-target code.  Chosen over macFUSE for operator UX (no kernel-extension approval, no reboot) and license (MIT, bundleable into a closed-source installer).  macOS NFS attribute cache may surface stale `mtime`/`size` for a few seconds after a remote write; the operator-facing surface today (`cc tasks list` — stat + readdir + read) is unaffected.  See `rust/services/fuse-plugin/README.md` if you need to force-refresh. |
| Windows  | `WinFsp`             | First-cut.  `winfsp` crate, cfg-gated under `target_os = "windows"`; `NEXUS_FUSE_MOUNT_POINT` accepts a drive letter (`Z:`) or directory path. |

The dylib is unsigned-rejected by `PluginLoader::load`; the release pipeline (`.github/workflows/release-fuse-plugin.yml`) signs every dylib it ships against the `kernel-dogfood-v1` key in the sealed in-repo keystore.  See `rust/services/fuse-plugin/README.md` for the operator install + admin RPC surface, and `docs/superpowers/specs/2026-06-13-sealed-keystore-dogfood-design.md` for the signing trust chain.

### Consistency model

The federation write surface routes per-call between EC and SC — same zone, two propose paths, picked at the call site:

| Surface | Path | Cost | When used |
|---------|------|------|-----------|
| `sys_setattr` / `sys_unlink` (the kernel hot path; everything `vfs_write` and `vfs_unlink` drive) | EC — `ZoneConsensus::propose_ec_local` (WAL append + local apply, sync return; async raft replication catches peers up) | ~5–50 µs, no quorum needed | Default for every metadata mutation.  Any node — voter, learner, leader, follower — can write locally.  Read-your-writes preserved on the writer node.  cc-tasks-share Mac↔Win bidirectional write rides this. |
| Lock acquire / release, CAS (`put_if_version`), stream WAL append, control-plane (mount install, ConfChange) | SC — `ZoneConsensus::propose` through raft consensus | ~5–10 ms intra-DC + majority ACK | Operations that need linearizability EC can't provide.  Non-leader callers either forward to the leader or surface `NotLeader`. |

Practical consequence for the §3b `--as` choice:

* The EC default means **a `--as learner` joiner can write `sys_setattr` metadata** — there's no "join as learner means read-only" rule.  The metadata write commits locally; raft replication ships it to peers when they come back online.
* The `--as voter` choice changes quorum participation for SC writes, **not** sys_setattr write capability.  Pick voter when the workflow needs locks / CAS / stream appends to commit despite a peer being offline (and you have ≥majority voters online).  Pick learner when you want wipe-rejoin safety and don't need SC ops.

Conflict resolution for the EC path is last-writer-wins on `modified_at_ms`.  The cc-tasks-share workflow has no contention because each peer writes its own subpath (`/shared/cc-tasks/<host>/...`); workflows where two peers write the same path concurrently need to design around LWW.

See `nexus-vfs` `docs/federation-architecture.md` §4.1 + §5 for the in-kernel architecture, and `docs/superpowers/specs/2026-06-23-federation-write-consistency-surface.md` for the design decision capture.

### Step 4 — Smoke (cross-machine byte-exact read)

On A, write a file into `/shared`:

```bash
cat > /tmp/write-req.json <<'EOF'
{"path":"/shared/win-hello.txt","content":"aGVsbG8gZnJvbSB3aW4="}
EOF

grpcurl -plaintext \
  -import-path <nexus-vfs-checkout>/proto \
  -proto nexus/grpc/vfs/vfs.proto \
  -d @ 127.0.0.1:2126 \
  nexus.grpc.vfs.NexusVFSService/Write < /tmp/write-req.json
```

Expected response:

```json
{
  "contentId": "win-hello.txt",
  "size": "14",
  "gen": "1"
}
```

`contentId` is `"win-hello.txt"` (the path rebased into `sharedzone`) — *not* `"shared/win-hello.txt"`.  Different contentId means the write is still going to the local PathLocalBackend rather than routing through `sharedzone`; that's the symptom that PR #4293 closed.

`Stat` the file to confirm origin tracking:

```bash
grpcurl -plaintext \
  -import-path <nexus-vfs-checkout>/proto \
  -proto nexus/grpc/vfs/vfs.proto \
  -d '{"path":"/shared/win-hello.txt"}' \
  127.0.0.1:2126 \
  nexus.grpc.vfs.NexusVFSService/Stat
```

Expected fields:

```json
{
  "found": true,
  "contentId": "win-hello.txt",
  "zoneId": "sharedzone",
  "lastWriterAddress": "<A_tailscale_ip>:2126"
}
```

`lastWriterAddress` being non-empty is the post-#4294 invariant.  When it's `None`, B's `try_remote_fetch` will fail the origin check before any cross-node RPC fires.

On B, read the same path:

```bash
grpcurl -plaintext \
  -import-path <nexus-vfs-checkout>/proto \
  -proto nexus/grpc/vfs/vfs.proto \
  -d '{"path":"/shared/win-hello.txt"}' \
  127.0.0.1:2126 \
  nexus.grpc.vfs.NexusVFSService/Read
```

Expected:

```json
{
  "contentId": "win-hello.txt",
  "content": "aGVsbG8gZnJvbSB3aW4=",
  "size": "14",
  "gen": "1"
}
```

`base64 -d aGVsbG8gZnJvbSB3aW4=` → `hello from win`.  Byte-exact match with what A wrote = L1 cross-machine federation read passes.

---

## Key design decisions

The choices below are stable invariants of the design.  Each ties back to a specific PR; see the [Relevant PRs](#relevant-prs) table for the original write-up.

* **Node IDs are opaque random `u64`.**  Minted on first boot, persisted to `<data-dir>/.node_id` (PR #3996).  Wipe-rejoin mints a fresh ID; the old ID stays in ConfState as a ghost.  Hostname is *not* part of the identity — that's what lets a single host re-register cleanly after disk wipe.
* **`--peers` lists only the *other* nodes** (PR #4014).  Self-in-peers is a parse error.  Self enters the cluster through `create_zone` (founder path) or AddNode-on-leader (joiner path), never via env.
* **Bootstrap mode is explicit** (PR #4028).  Operator declares `static` / `restart` / `dynamic` at boot.  A validator at startup rejects state × flag combinations that contradict the declared mode (e.g. `restart` on an empty data dir, or `dynamic` on a non-empty one); `static` accepts any data-dir state by design so the same env safely covers both first boot and container restart, with `bootstrap_or_join_zone` resuming from persisted ConfState when it finds one.
* **DT_MOUNT entries live in the *parent* zone's raft state.**  Both `share --mount-at` and `join` write DT_MOUNT via `propose_set_metadata` on the parent zone (PR #4293).  Each node has its own local root zone with its own DT_MOUNT entries — symmetric semantics either side; nodes don't need to share root-zone membership.
* **Federation cross-node read uses a two-Arc round-trip** (PR #4294).
  1. Write side records `last_writer_address = <self_address>` on every entry's metadata.
  2. Reader looks up the entry locally; on backend miss, `Kernel::try_remote_fetch` reads `last_writer_address` and asks `PeerBlobClient` to fetch from that origin.
  3. The origin's raft gRPC server has a `BlobFetcherSlot` populated with a `KernelBlobFetcher` (drained at boot by `blob_fetcher_handler::install`), which routes the fetch back through the origin's `VFSRouter`.

  All four wiring points (`set_self_address`, `stash_blob_fetcher_slot`, `blob_fetcher_handler::install`, `transport::peer_blob::install`) must run at cluster-binary boot.  PR #4294 consolidated the first three into `RaftDistributedCoordinator::install_with_kernel` and kept the fourth in cluster `main` (since `transport` sits above `raft` in the dep graph).
* **`PeerBlobClient` borrows the kernel's runtime via `Handle`, not `Arc<Runtime>`** (PR #4294).  Holding `Arc<Runtime>` extended the runtime's lifetime through the kernel's blob-fetcher capture chain, dropping it from `run_daemon`'s async-context unwind and panicking "Cannot drop a runtime in a context where blocking is not allowed".  With `Handle`, the kernel is the sole runtime owner; `PeerBlobClient` drops are side-effect-free in any context.
* **ZoneManager exposes both sync and async APIs** (nexus-vfs PR #23).  The sync façade (`mount`, `apply_topology`, `bootstrap_static`, `create_zone`, `share_subtree_core`) remains for sync callers — the `DistributedCoordinator` trait impl reached from `Kernel::setattr_mount`, tests, the witness / federation-server founder paths.  Async wrappers (`mount_async`, `apply_topology_async`, etc.) host the `spawn_blocking` hop *once per method* inside `ZoneManager` rather than at every `#[tokio::main]` caller.  No raft contract changed — the wrappers just add async-friendly entry points.
* **`is_leader()` and `leader_id()` share one atomic** (nexus-vfs PR #25).  Both read `cached_leader_id`; `is_leader()` returns `leader_id() == Some(self.config.id)`.  This eliminates the inter-atomic race that previously let a caller observe `(role=Follower, leader=self)` between two `Relaxed` stores in the driver's `update_cached_status`.  Companion fix: `forward_to_leader` returns to a local `submit_to_channel` retry when the resolved leader is self, so 1-voter founder zones never attempt a self-forward via gRPC (which would hairpin on Tailscale).
* **`share` / `join` are operator escape hatches** (PR #4008).  They open the data directory directly and must run with the daemon stopped — redb holds an exclusive file lock.  Day-1 deployment uses static topology env vars at daemon startup (`NEXUS_FEDERATION_ZONES` / `NEXUS_FEDERATION_MOUNTS`); `share` / `join` are for ad-hoc growth after the cluster is up.
* **TLS is opt-in.**  `--no-tls` is fine for testing.  Without it, the daemon auto-detects certs in `<data-dir>/tls/`; the founder generates them on first boot and the joiner gets them via the JoinCluster RPC.
* **3-node cluster recommended for production.**  A 2-node cluster loses quorum on a single failure; a witness node keeps quorum at 2/3 during one-node-down events.  Witness is a slim build with no VFS surface — see `rust/raft/src/bin/witness.rs` in nexus-vfs.

---

## Troubleshooting

### One-shot self-check: `nexusd-cluster doctor`

Before grepping logs, ask the binary:

```bash
pkill -f nexusd-cluster
target/release/nexusd-cluster doctor --data-dir /tmp/nexus-fed-data
```

It walks every zone subdirectory, reads ConfState + HardState + log indices directly from redb, and prints a one-screen per-zone summary plus any alarms (`STORAGE_LOCKED`, `STALE_LOG`, …) with operator recovery hints.  Exit code 2 means at least one zone is wedged.  Available post-nexus-vfs#39.

### Tailscale

| Symptom | Cause | Fix |
|---|---|---|
| `tailscale status` shows "Logged out" | Node key expired or reset | Re-register with Headscale; do **not** `tailscale up --reset` (wipes the node key) |
| `unexpected state: NoState` on Windows; `tailscale up` exits 0 but state never changes | `tailscaled` service running but no GUI driving it | Launch `C:\Program Files\Tailscale\tailscale-ipn.exe` |
| Ping works but TCP `:2126` refused | Firewall blocking, or daemon not running | Check firewall rules + `lsof -i :2126` / `netstat -ano \| findstr :2126` |
| "via DERP" instead of "direct" | NAT traversal failed | Relay still works (higher latency).  Direct comes back when one side opens a UDP hole |
| Clash Verge blocks Tailscale | TUN mode intercepts coordination traffic | Add Headscale IP + `100.64.0.0/10` to `route-exclude-address` in the *active profile's* merge file (not just the global one) |
| `nc -zv` to peer succeeds momentarily, then grpcurl times out | Tailscale dropped between the two; or Clash proxy remnant (`127.0.0.1:7897` enabled with nothing listening) intercepting HTTP/2 | Restart Tailscale; clean Clash proxy state (disable system proxy + clear DNS) |

### Bootstrap mode (PR #4028)

| Symptom | Cause | Fix |
|---|---|---|
| `NEXUS_BOOTSTRAP_MODE is required when bootstrapping federation` | Missing `--bootstrap-mode` flag | Pass `static`, `restart`, or `dynamic` |
| `bootstrap mode = dynamic, but data dir already holds a 'root' zone` | Dynamic mode requires a fresh data dir; persisted state is reserved for static or restart | Pass `--bootstrap-mode restart` to resume persisted state, or wipe the data dir to start over |
| `bootstrap mode = dynamic forbids NEXUS_BOOTSTRAP_NEW / NEXUS_PEERS` | Dynamic mode is runtime-API driven; cluster shape arrives via `share` / `join` RPCs, not env | Drop the env/flag, or switch to `--bootstrap-mode static` |
| `bootstrap mode = restart, but data dir is empty` | First-time boot with `restart` | Use `static` for the first boot |
| `bootstrap mode = restart forbids NEXUS_PEERS / --peers` | Passing peers under `restart` | Drop the flag — persisted ConfState carries the address book |

### Raft

| Symptom | Cause | Fix |
|---|---|---|
| `Zone 'root' registered (peers=1)` then silence | JoinZone went to self or founder unreachable | Check founder is listening on `:2126`; set `NEXUS_HOSTNAME` to the Tailscale IP |
| `peer list contains self ...` | Self listed in `--peers` | Remove your own IP from `--peers` (PR #4014's other-only contract) |
| `to_commit X is out of range [last_index 0]` | Stale raft state from a previous session | Wipe data dir on both sides, restart fresh |
| `not leader, leader hint: None` | Quorum not formed | Ensure both daemons running and connected |
| `Clamping inbound raft commit hint` | Follower has empty log, leader sending heartbeats | Normal during catch-up; wait for InstallSnapshot |
| `Cannot start a runtime from within a runtime` | Old pre-#4011 binary | Rebuild against current main |
| `Cannot drop a runtime in a context where blocking is not allowed` in `run_join` | Pre-nexus-vfs-#23 binary; `zm.mount` called directly from async | Rebuild against current nexus-vfs main |
| `share: did not become leader of '<zone>' within 10 s` (PR #4023) | Running `share` on a follower | Run `share` on the current leader |
| `Forward to leader failed leader=<self_node_id>` warning at every founder boot, `Static topology applied` taking ~10 s | Pre-nexus-vfs-#25 binary; leader-detection race + self-forward hairpin | Rebuild against current nexus-vfs main |
| Mac learner sees `mount: not leader, leader hint: Some(<self>)` on every join | Same as above; the join-CLI's `zm.mount_async` propose hit the self-forward path | Rebuild against current nexus-vfs main |
| Mac learner sees `Read /shared/win-hello.txt` → `IOError("PeerBlobClient not installed")` | Joiner binary pre-#4294 — `transport::peer_blob::install` never ran | Rebuild against current nexus-vfs main |
| Mac learner sees `Read /shared/win-hello.txt` → `NOT_FOUND (-32007)` | Writer binary pre-#4294 — `last_writer_address` is `None` so reader's `try_remote_fetch` fails the origin check | Rebuild against current nexus-vfs main |
| 2-node wipe-rejoin stalls | Old voter in ConfState (F5 deferred) | Use 3+ nodes (witness), or manually `RemoveNode(old_id)` |

### Environment

| Symptom | Cause | Fix |
|---|---|---|
| Another process on `:2126` | Stale `nexusd-cluster` or other daemon squatting | `lsof -i :2126` / `netstat -ano \| findstr :2126` → kill PID |
| TLS certs auto-generated unexpectedly | Old `<data-dir>/tls/*.pem` from previous runs | Delete and re-bootstrap, or set `--no-tls` |
| `NEXUS_DATA_DIR` vs `--data-dir` mismatch | Raft state and operator state in different dirs | Set both to the same path |
| Cluster-binary CI gate fails: `nexusd-cluster-<platform> exceeds size budget` | Adding a callable that drags new code paths into LTO retention | Either factor the dep out, or bump the budget in `.github/workflows/cluster-binary-build.yml` with measurement justification |

---

## Relevant PRs

The bug-class history matters here — the same shape regressed twice during the 2026 push, and the runbook entries above are organised around the specific PRs that closed each class.

### Pre-split (in nexi-lab/nexus)

| PR | What |
|----|------|
| #3453 | Peer address format (`id@host:port`) |
| #3926 | Store-and-forward content fetch (cross-node read primitive) |
| #3955 | Dynamic bootstrap mode (initial) |
| #3996 | Opaque random `node_id` (replace hostname-derived) |
| #4008 | `nexusd-cluster` migrated to opaque-ID + `init_from_env` |
| #4011 | Wrap `bootstrap_or_join_zone` in `spawn_blocking` — nested-tokio panic on founder boot |
| #4014 | `--peers` other-only contract (self-in-peers rejected) |
| #4023 | `share` / `join` leader-wait + AddNode trigger |
| #4028 | `BootstrapMode` enum + boot-time validator |
| #4253 | `nexusd-cluster` outer runtime sized via `recommended_worker_threads` — fixed gRPC accept-path starvation (Win→Mac HTTP/2 SETTINGS stall) |
| #4257 | Raft gRPC client onto `transport_primitives::create_channel` (SSOT consolidation) |
| #4261-#4263 | `ObjectStoreProvider` + driver gate; DT_MOUNT backends over gRPC; `NEXUS_S3_*` config |
| #4293 | `share --mount-at` + DT_MOUNT apply-cb wiring for cluster binary |
| #4294 | Federation cross-node read complete: `self_address` publish, `PeerBlobClient` runtime-Handle refactor, `bootstrap_done` flag, delete dead `init_from_env` |
| #4313 | Repo-boundary CI gate forbidding kernel-tier paths in this repo |
| #4259 | Delete kernel-tier from this repo; SSOT now lives in nexus-vfs |

### Post-split (in nexi-lab/nexus-vfs)

| PR | What |
|----|------|
| nexus-vfs #18 | Catch up kernel-tier state in nexus-vfs to nexus `c18db70dd` snapshot (previous fresh-import had silently fallen ~40 files behind); add reverse repo-boundary CI gate |
| nexus-vfs #23 | ZoneManager async wrappers — `mount_async` / `apply_topology_async` / `create_zone_async` / `share_subtree_core_async` / `bootstrap_static_async`.  Hosts the `spawn_blocking` hop inside the type rather than at every async caller |
| nexus-vfs #25 | F1: `is_leader()` derives from `leader_id()` (single atomic SSOT).  F2: `forward_to_leader` retries `submit_to_channel` locally when leader is self instead of RPC-forwarding via the self-address (which hairpins on Tailscale-on-Windows/macOS).  Eliminates the `Forward to leader failed leader=<self>` warning at every founder boot |

---

## Smoke regression covered in CI

* nexus-vfs `rust/raft/tests/test_zone_manager_under_tokio_main.rs::one_voter_propose_converges_without_self_forward_loop` — pins the convergence behaviour PR #25 restored.  A 2 s upper bound catches both a re-introduction of the cached-role/leader-id race and any restoration of the self-RPC-forward path.
* nexus `tests/e2e/docker/test_federation_runbook.py::TestJoinerCrossNodeReadRunbook` — pins the cross-process L1 milestone (byte-exact cross-node read via the `nexusd-cluster join` CLI flow + `PeerBlobClient` round-trip).  Runs against the 3-voter `docker-compose.federation-runbook.yml` topology on every PR via `.github/workflows/federation-e2e.yml`.  The same test class also catches:
  * pre-#4293 `mount: not leader` from the joiner's `zm.mount_async` propose
  * pre-#4294 `PeerBlobClient not installed` / `NOT_FOUND` (origin attribution missing)
  * pre-nexus-vfs-#23 nested-tokio panic on the join's mount path
  * pre-nexus-vfs-#25 founder self-forward hairpin
* nexus `tests/e2e/docker/test_federation_runbook.py::TestFounderBootstrap` — convergence budget + zero `Forward to leader failed leader=` warnings.
* nexus `tests/e2e/docker/test_federation_runbook.py::TestRestartReplay` — `--bootstrap-mode restart` replays persisted `DT_MOUNT` entries; pre-existing files stay readable post-reboot.
* nexus `tests/e2e/docker/test_federation_runbook.py::TestWitnessQuorumHA` — 3-voter quorum survives founder loss; ConfState recovers to 3 voters on restart.
* nexus `tests/e2e/docker/test_federation_runbook.py::TestRunbookOperatorErgonomics` — `share --mount-at`, `--bootstrap-mode` validator, `--peers <self>` rejection.

The Step-4 byte-exact L1 read across two real machines (Mac↔Win over Tailscale) remains a manual gate for the Tailscale-specific surface; the CI runbook tests cover the cross-process semantics in-network, but do not exercise the Tailscale path itself.
