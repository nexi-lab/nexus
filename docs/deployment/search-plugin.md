# Search plugin deployment (post-P12)

Since the P12 pivot (#4598), the Python in-process search daemon is
gone. The **Rust `nexus-search-plugin` cdylib**, hosted by
`nexusd-cluster` and reached over gRPC, is the **sole search backend**
for the Nexus server. A server deployment that does not run the plugin
host boots with search disabled — the boot probe fail-softs (logs a
warning, `/api/v2/search/*` returns 503) rather than failing the boot.

This page is the deployment contract: which processes you run, how
they are wired, and what changed for corpora indexed under the
pre-P12 stack.

## Required processes

| Process | Image | Role |
|---|---|---|
| Nexus server | `ghcr.io/nexi-lab/nexus` | FastAPI/gRPC server; proxies search calls to the plugin |
| Plugin host | `ghcr.io/nexi-lab/nexusd-cluster` | `nexusd-cluster` with the signed `nexus-search-plugin` cdylib in `/plugins`; owns FTS + ANN indices and embeddings |

Both images are published from the **same commit** with the **same
tag set** (`edge` on develop/main, `stable`/`latest`/semver on version
tags, plus the commit SHA), so a deployable pairing is pinnable by
digest. Publishing is done by `.github/workflows/docker-publish.yml`
(#4613); the `e2e-edge` smoke job runs the exact published plugin-host
image, not an in-CI build.

The plugin-host image is reproducible locally from a nexus + nexus-vfs
checkout (the nexus-vfs rev is pinned in the workspace `Cargo.toml`):

```bash
docker build \
  -f dockerfiles/Dockerfile.nexusd-cluster-plugins \
  --build-context nexus-vfs-src=../nexus-vfs \
  --secret id=plugin_signing_key,env=PLUGIN_SIGNING_PRIVKEY \
  -t nexusd-cluster:latest .
```

## Wiring

1. Start the plugin host with the plugin dir enabled:

   ```bash
   docker run -d --name nexus-cluster \
     -p 2126:2126 \
     -v nexus-workspace:/workspace \
     ghcr.io/nexi-lab/nexusd-cluster:stable \
     --bind-addr 0.0.0.0:2126 \
     --data-dir /app/data \
     --plugin-dir /plugins
   ```

   (`--no-tls --insecure-no-auth` are available for trusted loopback
   / compose topologies; production should keep TLS + auth on.)

2. Point the server at it:

   ```bash
   -e NEXUS_SEARCH_PLUGIN_TARGET=<cluster-host>:2126
   ```

   The server's boot probe (`server/lifespan/search.py`) health-checks
   the target with a 5 s timeout. Unreachable ⇒ search disabled for
   the whole server lifetime with a loud warning; fix the wiring and
   restart the server.

3. **Shared filesystem**: on `NotifyFileChange` / `Index` the plugin
   reads files **by absolute path through its own VFS/filesystem** —
   the server and the plugin host must see the same bytes at the same
   paths (shared volume in Docker, same host mount, or the kernel VFS
   in federated topologies).

## Environment reference (plugin host)

Set these on the `nexusd-cluster` process — the plugin reads process
env directly.

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_PLUGIN_DIR` | `/plugins` (in image) | `--plugin-dir` scan target |
| `NEXUS_SEARCH_MODEL_DIR` | `<data>/plugins/search/models` | Local embedding model directory (see below) |
| `ORT_DYLIB_PATH` | *(unset)* | Absolute path to `onnxruntime.{so,dylib,dll}`; **required** for local semantic search |
| `NEXUS_SEARCH_BATCH_CONCURRENCY` | `4` (clamp 1..=16) | BatchQuery inner-query concurrency (#4610) |
| `NEXUS_SEARCH_EMBED_API_URL` | *(unset)* | OpenAI-compatible `/v1/embeddings` endpoint; presence selects the remote embedder (#4614) |
| `NEXUS_SEARCH_EMBED_MODEL` | — | Remote model name (required with URL) |
| `NEXUS_SEARCH_EMBED_DIM` | — | Remote embedding dim (required with URL) |
| `NEXUS_SEARCH_EMBED_API_KEY` | *(unset)* | Bearer token for the endpoint |
| `NEXUS_SEARCH_EMBED_TIMEOUT_SECONDS` | `30` | Per-request timeout |

And on the **server**:

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_SEARCH_PLUGIN_TARGET` | `127.0.0.1:2126` | gRPC dial target for the plugin |
| `NEXUS_SEARCH_DAEMON` | auto (on when a DB URL is present) | `true`/`false` force search proxy on/off |
| `NEXUS_SEARCH_PLUGIN_TLS` | *(unset)* | `true` ⇒ TLS channel to the plugin host |
| `NEXUS_SEARCH_PLUGIN_TLS_CA` | *(system roots)* | CA bundle path for server verification |
| `NEXUS_SEARCH_PLUGIN_TLS_CERT` / `_KEY` | *(unset)* | Client cert+key pair for mTLS |
| `NEXUS_SEARCH_PLUGIN_ALLOW_INSECURE` | *(unset)* | Explicit opt-in for **plaintext to a non-loopback** target (trusted network only) — without it the server refuses the channel and boots with search disabled |

**Transport security**: plaintext is only accepted to same-machine
targets — loopback addresses and Docker's `host.docker.internal`
alias (which by construction resolves to the machine running the
container). A cross-host deployment must either terminate TLS on the
plugin host (pair with `NEXUS_SEARCH_PLUGIN_TLS*` on the server) or
explicitly set `NEXUS_SEARCH_PLUGIN_ALLOW_INSECURE=true` and accept
that anyone on the network path can read queries and index content.
There is NO blanket default for the opt-in: changing
`NEXUS_SEARCH_PLUGIN_TARGET` to a remote host fails closed (search
disabled with a loud boot warning) until you choose TLS or plaintext
deliberately.

## Plugin signing

The kernel **refuses unsigned plugins** — `PluginLoader` verifies an
Ed25519 detached signature (`<plugin>.so.sig` next to the dylib)
against compile-time trusted keys. The published image ships the
signed pair; if you build your own, sign with
`scripts/sign_plugin.py` and a key the kernel trusts (CI uses the
`PLUGIN_SIGNING_PRIVKEY` secret). An unsigned or foreign-signed
plugin is skipped at `--plugin-dir` scan time and search stays down.

## Embedding backends and model provisioning

Keyword (BM25/tantivy) search works out of the box. The **semantic /
hybrid lane needs an embedder**, one of:

### Local ONNX (default)

The plugin loads `multilingual-e5-small` (384-dim) from
`NEXUS_SEARCH_MODEL_DIR`. Provision the directory with the layout a
`huggingface-cli download intfloat/multilingual-e5-small` produces:

```text
<model-dir>/
  model.onnx
  tokenizer.json
  config.json
  special_tokens_map.json
  tokenizer_config.json
```

and set `ORT_DYLIB_PATH` to a matching ONNX Runtime library (1.19+
for the pinned ort 2.0 rc). Neither the model nor the ORT dylib is
baked into the published image (size + licensing); mount them and set
the two env vars. Missing model/dylib degrades gracefully: semantic
queries return a typed "unavailable" error, keyword search keeps
working.

### Remote / API (#4614)

Set `NEXUS_SEARCH_EMBED_API_URL` + `_MODEL` + `_DIM` (+ `_API_KEY`)
and the plugin embeds via any OpenAI-compatible endpoint instead of
local ONNX — no model files or ORT dylib needed. This is the
migration path for deployments whose corpus quality was tuned on API
embeddings (e.g. 1536-dim models) under the pre-P12 stack. A
configured remote endpoint wins over the local model; a partially
configured one (URL without model/dim) fails loudly rather than
silently falling back.

## Migrating a pre-P12 corpus

The pre-P12 stack embedded via API models into pgvector; the post-P12
plugin owns its own tantivy (FTS) + HNSW (ANN) indices under
`<data>/plugins/search/<zone>/`. There is **no index migration** — a
corpus indexed under the previous stack must be **fully reindexed**
after the upgrade (`nexus reindex --target search`, then
`nexus search index <path>` or your indexing pipeline).

Choose the embedding backend **before** reindexing:

- **Local mE5-small (384-dim)**: zero external dependencies, but a
  different (generally weaker) vector space than 1536-dim API models —
  re-validate retrieval quality and fusion tuning (`alpha`, etc.) on
  your own eval set.
- **Remote/API embedder pointed at your previous model**: preserves
  the vector space your tuning was measured on. The ANN index is
  still rebuilt (different storage engine), but embedding quality is
  unchanged.

The ANN directory tag encodes the embedder (`ann-<tag>-v<n>`, e.g.
`ann-mE5-small-v1-v2` or `ann-api-text-embedding-3-small-1536-v2`), so
switching embedders later re-tags and rebuilds alongside the old
directory instead of corrupting it.
