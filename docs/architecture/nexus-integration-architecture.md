# Nexus Integration Architecture

End-state architecture for the sudowork ↔ nexus ↔ sudo-code surface — agent identity,
A2A messaging, audit trace, cross-instance transport.

Cross-references:

- `KERNEL-ARCHITECTURE.md` (peer doc): kernel primitives, syscall surface, dispatch model
- `federation-memo.md` (peer doc): Raft, zone topology, gRPC transport
- sudowork repo (`sudoprivacy/sudowork`) — `OPEN-ITEMS.md`: items not yet implemented; xfail sentinel keeps the list visible in CI

---

## 1. System Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  sudowork (Electron)                       orchestrator                │
│  Renderer (React UI) ←IPC→ Main            copilot-worker topology:    │
│  chat UI · audit viewer · messenger        spawns / cancels workers   │
└─────────────────┬───────────────────────────────────┬─────────────────┘
                  │ gRPC (VFS port)                    │ gRPC (VFS port)
                  ▼                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  sudocode-host  —  the nexus daemon on a host that runs sudocode      │
│                    agents (one process per host)                      │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │  embedded Rust Kernel  (one kernel::Kernel per host)         │     │
│  │  VFSRouter · DCache · Metastore(redb) · LockManager          │     │
│  │  PipeManager(DT_PIPE) · StreamManager(DT_STREAM)            │     │
│  │  FileWatchRegistry(sys_watch) · KernelDispatch(hooks)      │     │
│  │  AuditHook · AgentStatusResolver · AgentRegistry (state SSOT)│     │
│  └───────┬─────────────────────────────────────┬───────────────┘     │
│          │ in-process Rust syscalls (KernelAbi) │ in-process Rust      │
│  ┌───────▼──────────────┐           ┌───────────▼────────────────┐    │
│  │  services rlib       │   spawn    │  sudocode agent tasks       │    │
│  │  ManagedAgentService │──────────► │  N tokio tasks              │    │
│  │  AcpService          │  via DI    │  each: KernelFsBackend<K> + │    │
│  │  SpawnTask<Kernel>   │  seam      │  ConversationRuntime loop,  │    │
│  │   (1 vtable / spawn) │            │  cwd = /proc/{pid}/workspace │    │
│  └──────────────────────┘           └─────────────────────────────┘   │
│                                                                       │
│  gRPC server (nexus `transport` crate, VFS port):                     │
│    NexusVFSService.Call routes managed_agent + ACP methods and        │
│    sys_* reads/writes for external clients (sudowork UI, orchestrator)│
└──────────────────────────────────────────────────────────────────────┘
```

### Constraints

- **One nexus per host.** A host that runs sudocode agents runs one `sudocode-host` process, and its embedded `kernel::Kernel` is that host's nexus. A host without sudocode agents runs the standalone `nexusd-cluster` binary. The `nexus-bootstrap` launcher protocol owns discover-or-launch, so both shapes converge on one kernel per host (§8).
- **Pure-infra nexus.** The kernel and the service-tier crates (`services`, `raft`, `lib`, `contracts`) compile with zero knowledge of any agent runtime; the dependency edge runs `sudocode → nexus`. `sudocode-host` (sudocode repo) is the one binary that links both source trees and monomorphises `SpawnTask<Kernel>` (§2.3).
- **State SSOT.** Agent runtime state (`pid → AgentState`, condvar wakeup, signal semantics, parent/child links, transition validation) lives in `kernel::core::agents::registry::AgentRegistry`. In-process Rust callers (ManagedAgentService, ACP) reach it directly; external callers reach it through the gRPC surface. Profile config and session history live on disk under `/agents/{name}/` (§2.1).
- **gRPC is the external surface.** External clients (sudowork UI, orchestrator) reach the kernel over the VFS gRPC port; the agent↔kernel hot path stays in-process Rust. HTTP is reserved for human-facing dashboards.
- **Cluster profile.** `sudocode-host` and `nexusd-cluster` both run Nexus's cluster profile — bricks: IPC, FEDERATION.
- **Zone = VFS path mount point.** A zone's visibility boundary is its mount path. ReBAC governs sub-path access within a zone.

---

## 2. Agent Identity & Runtime

Two namespaces, the same Linux distinction between an executable on disk and a running process:

| Namespace | Lifetime | Content | Backing store |
|-----------|----------|---------|---------------|
| `/agents/{name}/` | Persistent | Profile config: `config.toml`, `prompts/`, `skills/`, `chat-with-me` | Metastore (DT_FILE / DT_DIR) |
| `/proc/{pid}/` | Ephemeral | Runtime: `status`, `agent` link, `chat-with-me`, `sessions/`, `tasks/`, `workspace/` | In-memory + WAL while pid alive |

`/agents/{name}/` is the stable identity an outsider addresses (other agents, humans on Element). One agent name can spawn many `pid`s — different worktrees, parallel work — and all of them share the same profile.

### 2.1 Agent-name namespace

```
/agents/scode-standard/          ← profile (DT_DIR)
   config.toml                   ← model selection, MCP endpoints, default workspace recipe
   prompts/                      ← system-prompt overrides, per-skill prompts
   skills/                       ← which tool sets are loadable
```

(`/agents/{name}/chat-with-me` exists only for **human** identities —
e.g. `/agents/human-ethan/chat-with-me` is a real DT_STREAM. For agent
names like `scode-standard` it is intentionally absent; addressing
goes through the pid level. See §3.6.)

`chat-with-me` lives at the pid level — `/proc/{pid}/chat-with-me` is
the canonical address, and `/proc/{pid}/workspace/chat-with-me` is a
DT_LINK to it (stamped at start_session, followed transparently by
VFSRouter on read/write — see §2.2). The agent-name level
(`/agents/{name}/chat-with-me`) reaches the same stream for **human**
identities (long-lived DT_STREAM owned by the user); for managed-agent
names like `scode-standard` the agent-name level holds profile config
only and addressing flows through the pid level. Callers always have a
pid by the time they need to address a managed agent (sudowork gets it
from `ManagedAgentService.start_session_v1`; in-process runtimes have
their own `pid`); requiring the pid keeps the addressing model
unambiguous and avoids the design questions around multi-instance
fan-out / fan-in.

Three kinds of recipient still share the same DT_STREAM-backed surface:

- **Local agent pid** (e.g. `/proc/p_42/chat-with-me` for the active
  scode-standard instance): real DT_STREAM. Writes append; `sys_watch`
  wakes up readers.
- **Remote identity** (e.g. `human-bob` on a stock Matrix client like
  Element): same DT_STREAM under the hood; reach across instances goes
  through the Matrix C-S adapter (§4) which translates Element's HTTP
  REST traffic into nexus VFS reads / writes against this stream.
- **Local persistent identity** (e.g. `/agents/human-ethan/chat-with-me`):
  a long-lived DT_STREAM owned by the user, not a transient pid. The
  sudowork UI reads it for inbox display, writes for outgoing messages.
  This is the one place `/agents/{name}/...` resolves directly to a
  stream, because the "user" agent has no spawn lifecycle.

### 2.2 Runtime namespace

```
/proc/{pid}/
   status                        ← virtual file: AgentStatusResolver renders descriptor JSON
   agent                         ← DT_LINK → /agents/{name}/   (Linux /proc/{pid}/exe analogue)
   chat-with-me                  ← DT_STREAM: this pid's conversation
   sessions/                     ← DT_DIR; sudo-code writes per-session jsonls under here
   tasks/                        ← DT_DIR; reserved for sudo-code task list persistence
   workspace/                    ← DT_DIR (agent cwd)
      chat-with-me               ← DT_LINK → /proc/{pid}/chat-with-me
      project-x/                 ← DT_LINK → host repo path from desc.repos
      project-y/                 ← DT_LINK → host repo path from desc.repos
```

`/proc/{pid}/status` is served by `AgentStatusResolver`, a `PathResolver`
that renders the live `AgentDescriptor` as JSON on each read — content
is a function of the current AgentRegistry snapshot. The DT_LINK rows
under `/proc/{pid}/workspace/` and `/proc/{pid}/agent` are static for
the pid's lifetime, so they live in the metastore as plain DT_LINK
entries stamped at start_session; VFSRouter follows them transparently
on `sys_read` / `sys_write` (single-hop, ELOOP-detected), and the
existing hooks (mailbox stamping, workspace boundary, audit) match on
the link path's suffix so they fire correctly whether the caller writes
to `chat-with-me` directly or through the workspace shortcut. The
descriptor is the SSOT for state, exit code, agent name, parent pid,
timestamps, model, and the workspace mount list (`AgentDescriptor.repos`,
useful PCB metadata for inspection); the metastore's DT_LINK rows are
the SSOT for routing.

`/proc/{pid}/agent` is a kernel-resolved DT_LINK to the agent-name directory. `readlink` returns `/agents/{name}/`; `stat` follows. This is the single SSOT pointer from a runtime back to its profile — no metadata duplication.

### 2.3 Spawn lifecycle

sudo-code is in-process: a Rust crate linked into nexusd, driven as a tokio
task per pid. There is no subprocess and no stdio plumbing — that machinery
exists in `AcpService` only because external ACP agents (claude / codex /
codebuddy / nanobot) run in separate OS processes and the only protocol
those binaries support is JSON-RPC over stdio. sudo-code is our own code in
our own process, so it talks to the kernel through direct Rust syscalls
(`kernel.sys_read`, `kernel.sys_write`, `kernel.sys_watch`, …) and to the
dispatch hooks through the same in-process channel every kernel observer
uses.

```
sudowork (Electron, TS)
   │ gRPC: NexusVFSService.Call(method="managed_agent.start_session_v1",
   │       payload={agent_id:"scode-standard", repos:[…], model, owner_id, zone_id})
   ▼
nexusd:
   tonic Call handler
      │ resolve_rust_dispatch -> ("managed_agent", "start_session_v1")
      │ Kernel::dispatch_rust_call -> ManagedAgentService::dispatch
      │ ManagedAgentService::start_session
      │   → AgentRegistry.register descriptor (model + repos in PCB)
      │   → AgentRegistry.update_state(WARMING_UP)
      │   → register_proc_entry: stamp /proc/{pid}/workspace/ dirent
      ▼
   {session_id=pid, workspace_path="/proc/{pid}/workspace/"} → sudowork
```

The session identifier IS the AgentRegistry pid — there is no second id.
The descriptor (`AgentDescriptor`) is the per-pid SSOT for spawn-time
surface info: `agent_id` (static profile name) lands in `desc.name`,
`model` in `desc.labels["model"]`, workspace mount list in
`desc.repos`.  ManagedAgentService plants the descriptor, stamps the
per-pid procfs entries, and hands off to sudo-code:

  - `/proc/{pid}/` (DT_DIR) — process root.
  - `/proc/{pid}/agent` (DT_LINK → `/agents/{desc.name}/`) — Linux
    `/proc/{pid}/exe` analogue; readlink returns the static profile dir.
  - `/proc/{pid}/chat-with-me` (DT_STREAM) — the canonical mailbox; A2A
    writes append here, sudo-code's loop `sys_watch`es it for prompts.
  - `/proc/{pid}/sessions/` (DT_DIR) — sudo-code writes per-session jsonl
    transcripts under this prefix.
  - `/proc/{pid}/tasks/` (DT_DIR) — reserved for sudo-code task list
    persistence.
  - `/proc/{pid}/workspace/` (DT_DIR) — agent cwd; per-repo mounts and
    the chat-with-me shortcut hang under here.
  - `/proc/{pid}/workspace/chat-with-me` (DT_LINK → `/proc/{pid}/chat-with-me`)
    — workspace shortcut so the agent can write `chat-with-me` relative
    to its cwd.  Resolved by VFSRouter's standard DT_LINK follow.
  - `/proc/{pid}/workspace/{alias}` (DT_LINK → `desc.repos[alias].mount_path`)
    — one per `WorkspaceRepo` in the spawn request.

Out-of-band termination (SIGTERM / SIGKILL / orphan reap) flows through
`AgentRegistry::on_terminate`, which reaps every entry under
`/proc/{pid}/`.  `cancel(Session)` calls `AgentRegistry::kill(pid, 0)`
for the same outcome — orphan auto-reap removes the descriptor, the
observer reaps the procfs subtree.

After the procfs entries are in place, ManagedAgentService hands off
to the runtime crate through the `SpawnTask<K: KernelAbi>` DI seam.
The seam is one indirect call per session start — the binary edge
(the `nexus-cluster` binary) constructs
a concrete `SpawnTask<Kernel>` adapter wrapping
`sudocode_runtime::spawn_task` and registers it via
`install_managed_agent_with_spawn(kernel, Arc::new(adapter))`.
`start_session` then calls `provider.spawn(kernel, desc, observer)`
through `Arc<dyn SpawnTask<K>>`: exactly one vtable dispatch per
session. The returned `Box<dyn SpawnHandle>` lands in the service's
`spawn_handles` sidecar so the on_terminate observer can abort the
worker on session reap.

Inside the spawn body the call surface is plain monomorphic Rust.
`sudocode_runtime::spawn_task::spawn_task::<K: KernelAbi + Send +
Sync + 'static>` (and its inner `run_loop<K, C, T, F>` at
`rust/crates/runtime/src/spawn_task.rs` in the
[sudocode repo](https://github.com/sudoprivacy/sudocode)) is generic
over `K`; binary-edge
monomorphisation specialises it against the concrete `Kernel`. Every
`kernel.sys_read`, `kernel.sys_write`, and `kernel.sys_watch` in the
mailbox poll loop is an inline direct call — no per-syscall vtable
cost. Tokio supplies the I/O-concurrency model (LLM HTTP round-trips
run seconds; many pids share a small worker pool) on the kernel's
shared runtime.

The services rlib stays runtime-agnostic: it depends on the
`SpawnTask` trait only, not on `sudocode_runtime` or any other
concrete runtime crate. Pure-Rust slim builds that ship managed-agent
without a runtime body call `install_managed_agent` instead of
`install_managed_agent_with_spawn`; `start_session` then leaves
`spawn_provider == None` and no per-pid task is launched.

After spawn, prompts and responses flow through the chat-with-me VFS
surface — same A2A primitive every other agent uses (§3). sudowork
writes prompts to `/proc/{pid}/chat-with-me`; the kernel rewrites the
envelope's `from` field to sudowork's caller identity (§3.3). The
sudo-code task in nexusd `sys_watch`es its own `/proc/{pid}/chat-with-me`
for incoming prompts and writes responses to `/agents/{user}/chat-with-me`.
sudowork's UI `sys_watch`es the user's chat-with-me for those responses.

**ManagedAgentService surface is intentionally narrow** — only spawn /
cancel / liveness, exposed over `NexusVFSService.Call`:

- `start_session_v1` — payload `{agent_id, repos, model, owner_id, zone_id}` →
  `{session_id, workspace_path}`. `agent_id` names the static profile
  (`/agents/{agent_id}/`); `session_id` is the runtime pid.
- `cancel_v1` — payload `{session_id, mode}` → `{cancelled}`.
  `mode ∈ {turn, session}` — turn aborts the current generation,
  session reaps the pid.
- `get_session_v1` — payload `{session_id}` →
  `{session_id, agent_id, workspace_path, model, state}`. `agent_id`
  echoes the static profile; `state` mirrors `AgentDescriptor.state`.

The dotted form (`managed_agent.start_session_v1`) is canonical;
flat-name fallback (`managed_agent_start_session_v1`) is wired for
backward compat in the gRPC `Call` handler (KERNEL-ARCHITECTURE §8.1).
Prompt / event flow uses the existing `NexusVFSService` gRPC
(`sys_write`, `sys_watch`, `sys_read`) over the chat-with-me paths.
There is no `SendPrompt` or `SubscribeEvents` gRPC — those would
duplicate the A2A surface the rest of the system uses.

`AgentState` lifecycle: `REGISTERED → WARMING_UP → READY ↔ BUSY → SUSPENDED → TERMINATED`.
`kernel.agent_wait(pid, target_state, timeout_ms)` parks the calling
thread on the per-pid condvar — supervisors get an event-driven
blocking wait instead of polling.

`AgentRegistry` is the SSOT for `AgentState`.
`AgentRegistry::update_state(&pid, new_state)` at
`rust/kernel/src/core/agents/registry.rs:579` is the only writer in
the runtime path — it enforces the FSM via `can_transition_to`
(`registry.rs:177`), updates `updated_at_ms`, and fires the
`on_terminate` observers when a session transitions to `TERMINATED`.

The runtime-path callsite is a state-observer closure constructed by
`ManagedAgentService::start_session`. The closure captures
`Arc<AgentRegistry>` and the new session's pid, maps the runtime
crate's `AgentLoopState` (`WarmingUp` / `Ready` / `Busy`) onto
`AgentState`, and calls `registry.update_state(&pid, target)` — any
`AgentError::InvalidTransition` is logged so a runtime FSM bug is
visible without aborting the worker. The `SpawnTask<K>::spawn` DI
seam takes this observer as a parameter; the binary-edge adapter
forwards it through to
the runtime crate's `state_callback` parameter and never touches
`AgentRegistry` itself. The adapter is a pure runtime-wrapper; the
service owns the SSOT writes.

### 2.4 sudo-code state placement

sudo-code's `runtime` crate already organises persistence around two
configurable surfaces — `SessionStore` (which builds session jsonl
paths) and `ConfigLoader` (which discovers config files via env +
project-local lookup). The integration redirects those surfaces at
nexus VFS paths and lets `file_ops.rs` workspace-bounded helpers
(`read_file_in_workspace` / `write_file_in_workspace` / etc.) issue
kernel syscalls instead of `std::fs`. sudo-code's static prompt
sections + AGENTS.md scan stay as-is on the read path.

| sudo-code surface | Current on-disk shape | nexus VFS path | Adaptation |
|---|---|---|---|
| `SessionStore::from_data_dir(data_dir, workspace_root)` — conversation jsonls | `<data_dir>/sessions/<workspace_hash>/<session_id>.jsonl` (FNV-1a 64-bit hex of canonical workspace path) | `/proc/{pid}/sessions/<workspace_hash>/<session_id>.jsonl` | **READS UNCHANGED** — call `from_data_dir(data_dir="/proc/{pid}/sessions", workspace_root="/proc/{pid}/workspace")`; replace `std::fs` usage in `session.rs` with kernel syscalls |
| `task_registry` — sub-agent task lifecycle | In-memory only (no disk) today | `/proc/{pid}/tasks/<task_list_name>.json` once persistence lands in sudo-code | **NEEDS PATCH** in upstream sudo-code (forked) — `tasks/` dirent is reserved by nexus; persistence is a sudo-code-side change driven by sudo-code's own roadmap, not by this integration |
| `prompt.rs` — static prompt sections | Embedded `&'static str` constants returned by `get_simple_*_section()` | n/a (no IO) | **READS UNCHANGED** — embedded strings pass through |
| `prompt.rs` — project AGENTS.md scan | Walks parent dirs from cwd looking for `AGENTS.md` and `.nexus/sudocode/AGENTS.md` | walks `/proc/{pid}/workspace/{repo}/...AGENTS.md` and `.nexus/sudocode/AGENTS.md` (DT_FILE under the repo's mount) | **READS UNCHANGED** — sudo-code already takes cwd as input; cwd = `/proc/{pid}/workspace/`; replace `std::fs` with kernel syscalls in the scanner |
| `ConfigLoader` — runtime settings | `$SUDO_CODE_CONFIG_HOME` (default `$HOME/.nexus/sudocode/`) for user-level; `./.scode.json` or `./.nexus/sudocode/settings.json` for project-level | `/agents/{name}/config/` for user-level; `/proc/{pid}/workspace/{repo}/.nexus/sudocode/settings.json` for project-level | **READS UNCHANGED** — set `SUDO_CODE_CONFIG_HOME` to a VFS-mapped path; replace `std::fs` with kernel syscalls in the loader |
| Agent profile config | (no equivalent on disk today) | `/agents/{name}/config.toml` (DT_FILE) | **NEW** — user-global agent settings written by sudowork's profile UI; sudo-code reads it through ConfigLoader's user-level slot |

`workspace_hash` matches sudo-code's existing FNV-1a 64-bit fingerprint
of the canonical workspace path (16-char hex), so a single profile
talking to multiple repos still partitions sessions per-repo on disk.

User-global agent settings live at `/agents/{name}/config.toml` inside
nexus VFS, sharing the same SSOT as the rest of agent identity.

---

## 3. A2A Communication

A2A, H2A, and A2H share one primitive: write a message to the recipient's `chat-with-me`.

### 3.1 Mailbox

`/agents/{name}/chat-with-me` and `/proc/{pid}/chat-with-me` are append-only message streams. They are normal DT_STREAMs that any caller can write to and the owner can read with `sys_watch`. Federation Raft replicates them across zone members; reach to clients outside the federation (e.g. Element on a stock Matrix server) goes through the Matrix C-S adapter (§4).

### 3.2 The chat-with-me link inside a workspace

Every workspace exposes a sibling `chat-with-me` entry that resolves to the owning pid's chat:

```
/proc/{pid}/workspace/chat-with-me  →  /proc/{pid}/chat-with-me
```

So an agent inside another's workspace — say agent A is staged at `/proc/p_other/workspace/projects/nexus/` and wants to talk to whoever owns this nexus repo — writes to `chat-with-me` relative to wherever it stands; resolution follows back to the workspace owner's stream.

The link is a plain DT_LINK row in the metastore, stamped at start_session
(§2.2). VFSRouter follows it transparently on `sys_read` / `sys_write`
(single-hop, ELOOP-detected); hooks match on the link path's `/chat-with-me`
suffix so audit, sender stamping, and boundary checks behave identically
to a direct write to `/proc/{pid}/chat-with-me`.

### 3.3 Sender identity

Mailbox envelope stamping rewrites the message envelope's `from` field
to the caller's authenticated `agent_id` before the write reaches the
backend. The on-disk envelope's `from` always reflects the kernel's
authenticated identity; the LLM-supplied envelope contributes message
body and metadata only.

The rewrite is implemented as a registered `NativeInterceptHook`
(`MailboxStampingHook`) that delegates the actual envelope policy to
`mailbox_stamping_policy::maybe_stamp_chat_envelope`. Both live under
`rust/services/src/managed_agent/` — owned by `ManagedAgentService`
(the chat-with-me mailbox is a managed-agent concern, not a generic
agent-table concern). The hook struct owns "how to be a hook"
(dispatch wiring + content-clone bypass); the policy module owns
"what to rewrite" (envelope schema, identity guarantee). The hook
trait was widened to
support content rewriting — `on_pre` returns
`Result<HookOutcome, String>` where `HookOutcome::Replace(bytes)` is
the new variant that substitutes write content. Accept/reject hooks
(audit, permission, workspace boundary) all return `HookOutcome::Pass`.

To keep the hot path allocation-free for the writes that don't need
rewriting, hooks declare a `mutating_path_suffix` and the dispatcher
uses it as a double bypass:

- **Layer 1 (no mutating hooks registered)**: empty-Vec check, dispatcher
  goes straight to `WriteHookCtx::content = vec![]` — identical to the
  pre-widening cost.
- **Layer 2 (mutating hook registered, write path doesn't match)**:
  suffix scan returns false, dispatcher still passes `vec![]`. Only
  writes whose path ends in a registered suffix (`*/chat-with-me`)
  pay the content clone.

```
agent A writes envelope { to: "scode-standard", body: "ping" }
   │
   ▼
sys_write
   has_mutating_hook_match(path) → true (suffix matches "/chat-with-me")
   clone content into WriteHookCtx
   │
   ▼
dispatch_native_pre → MailboxStampingHook.on_pre
   reads ctx.agent_id = "human-ethan"
   delegates to maybe_stamp_chat_envelope
   returns HookOutcome::Replace({ from:"human-ethan", to:"scode-standard", … })
   │
   ▼
DT_STREAM append (the per-pid stream — `/proc/{pid}/chat-with-me`,
                  possibly reached via the workspace DT_LINK shortcut)
```

### 3.4 Boundary teaching UX

`WorkspaceBoundaryHook` is registered as an `INTERCEPT pre-write` hook scoped to `/proc/{pid}/workspace/{...}`. It compares the caller's `agent_id` to the workspace owner derived from the path (`pid → AgentRegistry.lookup(pid).name`). On mismatch the hook returns `Err(EPERM)` with a structured payload:

```
EPERM at /proc/p_scode/workspace/projects/nexus/src/main.rs:
  This workspace is owned by agent 'scode-standard' (pid p_scode).
  You are 'human-ethan'. To send a message about this workspace, write to:
     /proc/p_scode/workspace/chat-with-me
  (DT_LINK to /proc/p_scode/chat-with-me.)
```

The error is intentionally instructive. LLMs that hit it once learn the convention without memory or system-prompt edits — the path layout itself is the SSOT for permissions.

### 3.5 Same primitive across humans and agents

`/agents/human-ethan/chat-with-me` is the canonical Ethan address —
"human" identities have no spawn lifecycle so the path resolves
directly to a long-lived DT_STREAM, no pid indirection needed. From
sudowork's UI Ethan sends through gRPC writes to other agents'
`/proc/{pid}/chat-with-me`; he reads his own through `sys_watch` over
`/agents/human-ethan/chat-with-me`. Other humans (Bob on Element)
reach the same DT_STREAM through the Matrix C-S adapter (§4); the
adapter speaks Matrix REST at the edge and nexus VFS underneath, so
the recipient's transport is invisible to the sender.

### 3.6 Addressing non-human agents

Non-human agent names (`scode-standard`, `claude`, etc.) can map to
zero, one, or many running pids in parallel — different worktrees,
sessions, supervisors. The chat-with-me surface for these agents is
per-pid:

- `/proc/{pid}/chat-with-me` — direct DT_STREAM at the per-pid path.
- `/proc/{pid}/workspace/chat-with-me` — DT_LINK to the same stream so
  callers inside the workspace tree can write `chat-with-me` relative
  to their cwd; VFSRouter follows the link transparently.

Callers reach the pid through the lifecycle surface: sudowork from
`managed_agent.start_session_v1` (§2.3); in-process runtimes from
`self.pid`. Per-pid addressing keeps the routing model unambiguous
for the supervised, parallel-worktree workflows this integration
runs.

---

## 4. Cross-instance Transport

Two layers compose. **Within** a federation, raft replicates the
chat-with-me DT_STREAM across every nexus instance voted into the
zone — recipients on any peer node read through their local kernel
just like a same-host write. **Outside** the federation, a Matrix
Client-Server adapter exposes the same DT_STREAMs over Matrix REST so
unmodified third-party clients (Element, FluffyChat, Cinny) can join
conversations without nexus needing a bespoke client.

### 4.1 Federation-internal — raft replication

Every chat-with-me DT_STREAM lives inside its zone's raft cluster. The
write path is `sys_write` → `WalStreamCore` → `Command::AppendStreamEntry`
→ raft commit → state-machine apply on every voter, including remote
peers. Cross-instance reach is the same `sys_watch` wake-up that a
same-host caller sees. Read § 6 for the broader DT_STREAM /
WalStreamBackend contract — there is no separate transport for the
in-federation case.

### 4.2 Federation-external — Matrix C-S adapter

The Matrix C-S adapter is a nexus services-tier component
(`services::matrix_adapter`) that hosts the Matrix Client-Server REST
surface at the edge and translates each call into nexus kernel
syscalls underneath. Element opens a TCP socket to the adapter's
`/_matrix/...` HTTP endpoints; the adapter walks the room state and
DT_STREAM contents through `sys_read` / `sys_write` / `stream_read_batch`.

```
Element   ──HTTP REST + JSON──►  services::matrix_adapter  ──in-process──►  nexus kernel
                                  /_matrix/client/v3/sync                    sys_read /
                                  /_matrix/client/v3/rooms/.../send          sys_write /
                                  /_matrix/media/v3/...                      stream_read_batch
                                                                              on chat-with-me
                                                                              DT_STREAMs
```

#### Endpoint scope

The adapter implements the minimal Client-Server v3 surface needed
for stock chat clients (Element, FluffyChat, Cinny) to participate
in nexus chat-with-me streams.  Scope is the C-S surface those
clients use — server-to-server federation, admin API, application
service, spaces, and end-to-end encryption are layers nexus already
covers (raft / AuthService / ReBAC) or layers the v1 surface does
not need.  ~20 endpoints organised in five groups:

| Group | Endpoints | Backed by |
|-------|-----------|-----------|
| **Auth** | `POST /_matrix/client/v3/login`, `POST /_matrix/client/v3/logout`, `GET /_matrix/client/v3/account/whoami` | `AuthService`; Matrix access token = AuthService session token, stamped into `OperationContext` per call |
| **Sync** | `GET /_matrix/client/v3/sync` (long-poll, since-token paging) | `sys_watch` on the user's joined chat-with-me streams; since-token is `(stream_path → offset)` map |
| **Rooms — read state** | `GET /_matrix/client/v3/rooms/{rid}/state`, `GET /_matrix/client/v3/rooms/{rid}/state/{event_type}/{state_key}`, `GET /_matrix/client/v3/rooms/{rid}/messages` (back-paginate), `GET /_matrix/client/v3/rooms/{rid}/joined_members` | `stream_read_batch` over the chat-with-me DT_STREAM; room state synthesised from ReBAC membership + envelope metadata |
| **Rooms — write** | `PUT /_matrix/client/v3/rooms/{rid}/send/{event_type}/{txn_id}`, `POST /_matrix/client/v3/rooms/{rid}/leave`, `POST /_matrix/client/v3/rooms/{rid}/join`, `POST /_matrix/client/v3/createRoom` | `sys_write` (envelope assembled from PDU body); join/leave write ReBAC mutations; createRoom binds a new `/agents/{name}/chat-with-me` |
| **Media** | `GET /_matrix/media/v3/download/{server}/{media_id}`, `POST /_matrix/media/v3/upload`, `GET /_matrix/media/v3/thumbnail/{server}/{media_id}` | DT_FILE under `/media/{media_id}`; CAS storage for content, raft for the metastore entry |

#### Room ↔ chat-with-me mapping

A Matrix room id maps 1:1 to a chat-with-me DT_STREAM path.  Stable
encoding so cross-restart the same room id resolves to the same
stream:

```
!{base32(stream_path)}:nexus.local
  e.g. /agents/human-bob/chat-with-me
       ↔ !MFRGS43FORZS6ZTPMVQHIYLUMRZA:nexus.local
```

The `:nexus.local` suffix is the Matrix server-name; it's a stable
constant per nexus deployment, configured at adapter boot.

#### PDU envelope ↔ chat envelope

Matrix PDUs (Persistent Data Units) and the nexus chat envelope
(§3.3) are both JSON; the adapter translates field-by-field on the
hot path:

| PDU field | Chat envelope field | Notes |
|-----------|---------------------|-------|
| `sender` | `from` | Stamped by `MailboxStampingHook` from `OperationContext` — adapter cannot forge |
| `content.body` | `body` | `m.room.message` text; pass-through |
| `content.msgtype` | `msgtype` | `m.text`, `m.image`, etc. |
| `origin_server_ts` | `ts_ms` | Unix ms |
| `event_id` | derived from DT_STREAM offset | `$offset_{n}:nexus.local` so it's stable |
| `room_id` | derived from path | See above |
| `unsigned.age` | computed at send time | Standard Matrix |

Adapter scope is the C-S surface stock chat clients use; nexus raft
provides cross-instance replication (§4.1), so PDU signing, state DAG
resolution, and depth/prev_events tracking belong to layers nexus
already owns — the DT_STREAM linear order is the SSOT for
ordering.

#### Storage

Adapter is **stateless** — every read/write goes through the kernel.
The kernel's metastore + WalStreamBackend hold the SSOT; the adapter
keeps three things in-memory, all rebuilt on restart:

  - Active `/sync` long-poll registrations (channel per pinned client)
  - Access-token → user-id map (mirrors AuthService; cache miss falls
    back to AuthService lookup)
  - Room id → stream path decoding (pure function of room id)

#### Identity & permissions

Matrix `/login` calls AuthService with the same credential schemes
sudowork uses.  AuthService returns the session token; adapter
returns it as Matrix `access_token`.  Subsequent calls validate the
token once per request (cached) and stamp the resolved user id into
`OperationContext` before issuing kernel syscalls.  ReBAC then
governs read/write — Matrix room membership IS the ReBAC `read` /
`write` predicate on the chat-with-me path.  No second permission
model.

#### Reference reading

The Matrix protocol mechanics (PDU canonical JSON, `/sync` semantics,
media repo) come from the [Matrix C-S spec v1.10+](https://spec.matrix.org/v1.10/client-server-api/);
upstream Rust implementations to read for patterns: Tuwunel
(Conduit fork, Apache-2.0).  The adapter is a fresh Rust crate that
reuses no upstream storage or S2S code.

### 4.3 Properties retained across both layers

Both layers preserve the kernel-level surfaces that make the
chat-with-me primitive uniform:

- **Permissions** — ReBAC on the DT_STREAM path governs who may write
  / read. Matrix room membership is derived from the same ReBAC
  decision on each `/send` call.
- **Audit** — Every Matrix-originated write reaches the kernel through
  `sys_write`, so `AuditHook` captures it like any other VFS write
  with no Matrix-specific bookkeeping.
- **Single SSOT** — The DT_STREAM at `/agents/human-bob/chat-with-me`
  is authoritative. The Matrix room view, the sudowork chat UI, and a
  federation peer's `sys_watch` all resolve to the same byte sequence;
  the adapter does not maintain a parallel store.

Native sudowork clients keep talking gRPC directly to nexus; only
external Matrix clients touch the adapter.

---

## 5. Audit Trace

### 5.1 Surface

| Concern | What | Source |
|---------|------|--------|
| VFS operation trace | Every read/write/delete/rename through nexus kernel, including chat-with-me writes | Rust `AuditHook` on kernel dispatch (POST phase) |
| Exchange audit | Agent economic transactions | Python exchange service |

Both write to **DT_STREAM with WalStreamBackend** — ordered, durable, Raft-replicated.

### 5.2 AuditHook pipeline

```
Kernel dispatch (Rust)
      │
      │  on_post_write / on_post_read / on_post_delete (POST hook)
      ▼
AuditHook  (impl NativeInterceptHook — pure Rust)
      │
      │  mpsc::SyncSender::try_send()  ← ~10–50ns, non-blocking
      ▼
audit-flush  (background Rust thread)
      │
      │  serialise AuditRecord → JSON → WalStreamCore::write_nowait
      ▼
WalStreamCore  (Command::AppendStreamEntry → zone Raft cluster)
      │
      └─► registered with StreamManager at /__sys__/audit/traces/
```

### 5.3 Auto-wiring on zone mount

Zone creation is a raft-layer operation, not a syscall — voters in a
zone's raft cluster agree on the zone's existence through
`DistributedCoordinator::create_zone`. The kernel-facing entry point
for an existing zone showing up on the local kernel is
`kernel.sys_setattr(mount_path, DT_MOUNT, …, zone_id, …)`, which
registers the mount with `VFSRouter` and dispatches a
`FileEvent { event_type: FileEventType::Mount, path, zone_id, … }`
(`rust/kernel/src/core/dispatch/mod.rs:54` for the enum variant,
`:288` for the `MutationObserver` trait).

`services::audit::install_root` (called once at boot) installs a
`MutationObserver` that filters on `FileEventType::Mount.bit()`. Each
fired event maps to a per-zone `install_for_zone(zone_id)` call,
guarded by a `Mutex<HashSet<String>>` so a re-mount is a harmless
no-op. New zones therefore wire their AuditHook + DT_STREAM the
moment they mount on this kernel, with no zone-creation API
duplication; the install path is purely syscall-driven. See §8 for
the dispatch lifecycle the observer hooks into.

### 5.4 Central audit zone

Each production node shares a 1:1 zone with the audit-node. `AuditHook` writes formatted `AuditRecord` entries to `/__sys__/audit/traces/` in that shared zone; the audit-node reads and gathers them locally.

```
Production nexusd (node A)
    │  AuditHook → /__sys__/audit/traces/  (auto-wired by zone_create(audit=true))
    │
    └──► Shared zone: zone-A-audit
              Raft cluster: [node-A voter, audit-node learner]

Production nexusd (node B)
    │  AuditHook → /__sys__/audit/traces/
    │
    └──► Shared zone: zone-B-audit
              Raft cluster: [node-B voter, audit-node learner]

Audit nexusd (audit-node)
    ├── learner of zone-A-audit  ← receives only node-A's audit stream
    ├── learner of zone-B-audit  ← receives only node-B's audit stream
    └── local collect/gather: reads all /__sys__/audit/traces/ streams, aggregates
```

The 1:1 zone holds `AuditRecord` only — formatted by `AuditHook`, with no production-zone metadata or lock commands. audit-node joins via `zone_join(zone_id, as_learner=true, audit=true)` so the production zone's voter quorum is unaffected; audit loss is preferable to blocking production writes.

### 5.5 AuditRecord schema

```json
{
  "v": 1,
  "ts": "2026-04-26T10:00:00.123Z",
  "trace_id": "req_a1b2c3d4",
  "agent_id": "agent:sudo-code",
  "op": "write",
  "path": "/proc/p_scode/workspace/projects/nexus/src/main.rs",
  "zone_id": "root",
  "size_bytes": 1024,
  "status": "ok",
  "duration_us": 42
}
```

---

## 6. Data Replication Mechanisms

Replication composes two channels — metadata via raft and content
via CAS — and dispatches by entry type:

| Entry type | Used for | Metadata channel | Content channel |
|-----------|----------|------------------|-----------------|
| **DT_FILE** | sudo-code sessions, profile configs, task lists | Raft (intra-zone, strongly consistent) | Local CAS (`cas_local` backend) + on-miss lazy fetch from a peer voter via `PeerBlobClient` |
| **DT_STREAM** (via `WalStreamBackend`) | `chat-with-me`, `/__sys__/audit/traces/` | Raft (intra-zone) | Same raft log — `WalStreamBackend` writes content as `Command::AppendStreamEntry` so total order across voters is the same channel as metadata |

For `DT_FILE`, the file's `FileMetadata` (entry type, content hash,
size, last-writer address, etc.) commits through raft so every voter
agrees on the namespace. The bytes themselves stay local-first in the
zone's CAS engine; a voter that misses the chunk on read pulls it on
demand through `PeerBlobClient` from the recorded `last_writer_address`.

For `DT_STREAM`, ordering is the load-bearing property — every voter
applies the same sequence of `AppendStreamEntry` commands, so a
`stream_read_batch` at offset N returns the same bytes on every node.
Append throughput is bounded by raft commit latency; `chat-with-me`
and audit traffic stay within that envelope by design.

The Matrix C-S adapter (§4.2) is an edge surface that calls
`sys_read` / `sys_write` / `stream_read_batch` against these same
mechanisms; the adapter is stateless. Stock Matrix clients see the
chat-with-me DT_STREAM, not a parallel store.

Primitive contracts (`DT_FILE` / `DT_STREAM` / `WalStreamBackend` /
`PeerBlobClient` / CAS engine) live in `KERNEL-ARCHITECTURE.md` §3
and §4. This doc captures only how the integration layer uses them.

---

## 7. Messenger Surface

Three clients sit over the same chat-with-me DT_STREAMs:

- **sudowork chat UI** — talks gRPC directly to nexus. Reads each
  `/agents/{name}/chat-with-me` and `/proc/{pid}/chat-with-me` through
  `stream_read_batch` + `sys_watch`; writes via `sys_write`.
- **Stock Matrix clients** (Element, FluffyChat, Cinny) — connect to
  the Matrix C-S adapter (§4.2) over HTTP. Each Matrix room id maps
  1:1 to a chat-with-me path; `m.room.message` events serialize into
  the same envelope schema sudowork's UI emits.
- **In-process agent runtimes** — read / write `chat-with-me` directly
  through the kernel like any other VFS surface; no gateway involved.

The chat-with-me DT_STREAM is the SSOT. None of the three clients
maintain a parallel inbox; identity, ordering, and audit all derive
from the kernel.

---

## 8. Appendix A: Kernel Dispatch Hook Lifecycle

```
syscall (sys_write, sys_read, …)
    │
    ├─► [CLONE GATE — sys_write only] mutating-suffix bypass
    │         has_mutating_hook_match(path)
    │         → false: WriteHookCtx.content = vec![] (no clone)
    │         → true:  WriteHookCtx.content = clone(content)
    │
    ├─► [PRE] NativeInterceptHook chain
    │         on_pre(ctx) → Result<HookOutcome, String>
    │         → Err to abort  (PermissionHook, WorkspaceBoundaryHook)
    │         → HookOutcome::Pass to proceed unchanged
    │         → HookOutcome::Replace(bytes) to rewrite write content
    │           (MailboxStampingHook on */chat-with-me)
    │
    ├─► [EXECUTE] backend write with replacement.unwrap_or(content)
    │             (redb / CAS / MemoryStreamBackend / WalStreamBackend / …)
    │
    ├─► [POST] NativeInterceptHook chain
    │         on_post(ctx)  ← AuditHook fires here
    │         → fire-and-forget, non-blocking (mpsc try_send)
    │
    └─► [OBSERVE] MutationObserver::on_mutation(FileEvent)
              → StreamEventObserver writes to DT_STREAM (sys_watch wakeup)
              → FileWatcher wakes sys_watch subscribers
              → AuditZoneAutoWire (filter FileEventType::Mount) installs
                AuditHook for the newly-mounted zone (§5.3)
```

The clone gate keeps the hot path allocation-free for writes that
do not need rewriting. Each hook declares a `mutating_path_suffix`
at registration; the dispatcher scans the registered suffixes
against the write path and only clones content into `WriteHookCtx`
when one matches. `MailboxStampingHook` declares `/chat-with-me`;
accept/reject hooks (audit, permission, workspace boundary) declare
`None` and the dispatcher passes them an empty content vec. When
multiple mutating hooks register, the chain semantics are
last-write-wins on `HookOutcome::Replace`.

---

## 9. Appendix B: Raft Command Taxonomy

All commands in a zone's Raft cluster share a single `Command` enum (`state_machine.rs`):

```
Command::SetMetadata           — VFS file/dir metadata (path, size, etag, …)
Command::DeleteMetadata        — VFS delete
Command::AcquireLock           — distributed lock
Command::ReleaseLock           — lock release
Command::AppendStreamEntry{…}  — WalStreamBackend stream data (chat, audit)
Command::DeleteStreamEntry     — stream cleanup
… (others)
```

In the audit 1:1 zone the only `AppendStreamEntry` traffic comes from `AuditHook` writes to `/__sys__/audit/traces/`; the audit-node learner applies them in order and exposes the aggregated stream to its local `collect/gather` consumer.

In a chat zone the `AppendStreamEntry` traffic is the conversation itself — every envelope (with its `from` field rewritten by `MailboxStampingHook` on `*/chat-with-me`, see §3.3) replicates to every voter and learner in the zone.
