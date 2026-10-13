# Search plugin deployment (post-P12)

The **Rust `nexus-search-plugin` cdylib**, hosted by
`nexusd-cluster` and reached over gRPC, is the **sole search backend**
for the Nexus server. A server deployment that does not run the plugin
host logs the failed Search probe during SDK startup.

This page is the deployment contract: which processes you run, how
they are wired, and what changed for corpora indexed under the
pre-P12 stack.

## HTTP and gRPC boundaries

The cluster serves typed VFS and `nexus.search.v1.SearchService` RPCs over
gRPC. Its optional Rust HTTP listener serves `/v2/search/*`,
`/v2/documents/*`, `/v2/auth/keys`, and `/v2/rebac/tuples`. HTTP search
forwards the caller's bearer credential through the daemon's gRPC listener;
both transports share the daemon's API-key and ReBAC stores.

`POST /v2/search/query` accepts the SearchService query settings for both
single-zone and federated requests: query type, hybrid fusion, page pooling,
context expansion, recency, path-prefix weights and path filters. Local and
remote legs use the same protobuf encoder. Local legs carry the caller's
bearer; remote legs carry scoped delegation over the daemon's authenticated
channel. An explicit `zone_id` selects one zone. An omitted zone lets a
multi-zone credential search its authorized zones. Cross-zone result fusion
uses the dispatcher's configuration; the request's fusion settings apply
within each zone.

The remote filesystem client's migration from HTTP/JSON-RPC to gRPC did
not remove these HTTP routes. R10 ([#4674](https://github.com/nexi-lab/nexus/issues/4674))
moves the server implementation into Rust while retaining an HTTP entry point.
The Python server still serves `/api/v2/*` for mixed deployments. Its
remaining callers must migrate before that server can be removed.

Indexed CLI commands (`query`, `index`, `stats`) use the Rust HTTP listener;
see [CLI setup](#cli-on-a-rust-cluster). Filesystem SDK and CLI `glob`/`grep`
calls use typed SearchService RPCs on the filesystem's existing gRPC channel.
The Python facade only translates parameters and results. The Rust plugin
reads current workspace bytes, prunes ignored subtrees during discovery and
applies Markdown block/section selections before result limits.

Load Search into the Kernel that owns the workspace mounts. An agent pod's
workspace belongs to its own Kernel; a VM broker does not gain access to that
workspace by hosting Search. Preserve the existing agent or delegated user
identity and pass its credential at each request boundary.

Remote SDK files and Search share the filesystem's authenticated gRPC channel.
File operations use typed VFS RPCs; range reads use BatchRead. Directory pages
walk only as far as the page window plus one entry and fetch detailed metadata
in one BatchStat call. Their `total_count` remains unknown rather than requiring
a complete namespace scan.

MCP Search tools support both local async services and the remote SDK's sync
proxy. Sync calls run outside the MCP event loop with the request credential
preserved. Set `zone_id` in `nexus.connect()` configuration to select the
default indexed Search zone. An explicit zone selector takes precedence;
the daemon still determines identity and permissions from the credential.

Directory enumeration requires read permission on the directory and each
returned child. Stat and BatchStat use the same installed file policy; a mixed
BatchStat request is rejected before reading metadata. Xattr reads follow that
policy too; xattr support still depends on the selected metastore. Xattr writes,
locks and IPC close operations require write permission. Force unlock and
closing all pipes require administrative authority. A wildcard watch needs
read permission on its fixed parent, and every returned event is checked
again after the wait.

The current typed Write contract represents complete file replacement. Offset
writes and TTL requests return `NotImplementedError` before writing. The typed
Rename contract also rejects `force=True` before changing either path.

Certificate agents use SearchService on the same mTLS channel as VFS. Their
certificate establishes identity without a zone grant; the Kernel's installed
file policy, including containment and ReBAC, determines access. A requested
Search zone does not grant access to its files. User and service API keys still
require a read grant for that zone. An explicit invalid bearer token fails
authentication even when the channel carries a valid agent certificate.

An omitted `files` selection permits directory traversal and requires root
read access. A supplied selection is bounded, validated and authorized per
file; `files=[]` returns no results. Search rechecks live permissions on the
host, and SDK clients reject hosts that do not acknowledge requested filters.

HTTP and MCP discovery paginate the host's authorized results. Grep requests the
page window plus one match to detect `has_more`. Responses report observed
counts; HTTP also reports latency. The host does not expose a per-file denial rate.

Rust HTTP discovery uses the canonical VFS `root_path` to select its mount
and zone. An optional `zone_id` must match that mount; a mismatch returns
HTTP 400. The revision fence uses the same zone as the gRPC host.

Native HTTP query and batch routes forward the caller's credential and
requested limits to SearchService. The host authorizes returned candidates.
These reads do not require a Python
record store. Path-context ranking weights remain optional durable configuration.
The Rust Search plugin loads the existing `path_contexts` SQL table for every
entry point, including direct gRPC, batch, and each executing federated zone.
Set `DATABASE_URL` or `NEXUS_DATABASE_URL` to the same PostgreSQL or SQLite
database that holds those rows. SQLAlchemy `postgresql+asyncpg` and
`sqlite+aiosqlite` URL spellings are accepted. Apply the existing database
migrations before enabling configuration; Search does not create tables.
Use SQLAlchemy's SQLite path convention: `sqlite:///config.db` is relative
and `sqlite:////data/config.db` is absolute. For a remotely executed zone,
connect configuration CRUD to its Search owner and use that owner's database.
`PUT`, `GET`, and `DELETE /v2/search/path-contexts` forward the original bearer
to `PutPathContext`, `ListPathContexts`, and `DeletePathContext` on SearchService.
Writes require administrator authority; ordinary listing requires a zone read
grant. Omitting the zone lists all zones for administrators and the caller zone
for other callers. Omitting `weight` on PUT clears the existing multiplier.
The bounded memory cache refreshes after `NEXUS_PATH_CONTEXT_FRESHNESS_SECONDS`
(default 1 second); native writes invalidate immediately. External writes are
reloaded even when row counts and timestamps stay unchanged. Explicit query
`path_prefix_boosts` override configured defaults at matching prefixes. Without
a database, Search uses request weights; database failures leave Search available
without configured weights and report a warning. CRUD reports HTTP 503.
Responses report observed timings; permission-denial statistics are not exposed.
The query limit bounds the host's ranked candidate window. Live authorization
and VFS checks can leave fewer returned hits than that limit.

The Rust host also authorizes index mutations, directory/mode management and
statistics. These operations require management authority; ordinary callers
receive HTTP 403. Invalid or revoked caller credentials return HTTP 401.
Native HTTP handlers forward the original credential and preserve the host's
RPC error category. The document refresh adapter reads workspace bytes using
the caller's VFS context before submitting them for indexing.

`POST /v2/search/locate` accepts `{"path": "/docs/file.md", "zone_id": "team"}`
(`zone_id` defaults to the caller's zone) and returns `indexed`, `chunk_count`,
`mtime_ms` and `zone_id`. It queries one path's index status.
The host checks current read permission and VFS existence on each call.
`X-Nexus-Min-Revision` uses the same revision fence as indexed Query.

The current Search host reports `has_graph=false`. Query and BatchQuery support
keyword, semantic and hybrid modes. Unknown HTTP request fields are rejected.

`GET /v2/search/health` requires management authority and returns the host's
`status`, `detail` and writer
liveness fields: `fts_writer_faults`, `fts_writer_unavailable`,
`last_verified_commit_age_ms` and `dispatch_panics`.

### Index configuration

Directory registration and indexing modes are independent RPCs. All require
management authority and keep their durable facts in the Search plugin's store.

| HTTP operation | Request | Response |
|---|---|---|
| `POST /v2/documents/directories` | JSON `path`, optional `zone_id` | `added`, optional `error` |
| `DELETE /v2/documents/directories` | Query `path`, optional `zone_id` | `removed`, optional `error` |
| `GET /v2/documents/directories` | Optional query `zone_id` | `directories` with `path`, `zone_id`, `added_at_ms`; optional `error` |
| `PUT /v2/documents/indexing-modes` | JSON `mode`, optional `zone_id` | Optional `error` |
| `GET /v2/documents/indexing-modes` | No body | `modes` with `zone_id`, `mode`; optional `error` |

Registration and removal are idempotent: an existing or absent record returns
`added=false` or `removed=false`. Modes are `on`, `off` and `sandbox`; an empty
mode resets the host default. An omitted directory zone selects the host's root
zone. The modes list contains the host's configured zone modes.
HTTP adds no directory cache or readiness state.
Configuration writes serialize their read/modify/save transaction and flush the
committed file before acknowledging it. Unsupported versions and corrupt facts
return an error and leave the stored file intact.

## Index storage and read operations

Indexed Search zone IDs must be single directory components. An omitted zone
still resolves to `root`; existing logical zone names keep their meaning.
Separators, parent paths, absolute paths, drive prefixes, NUL and trailing
spaces or dots are rejected with `INVALID_ARGUMENT` before index work. The
suffix check prevents Windows path normalization from aliasing zone directories.
Authentication runs before this admission check. Batch query and document indexing validate the entire request,
including document zone overrides, before starting work.

Query, Locate, Stats and metadata lists load existing state. An unindexed zone
returns empty results or zero counts without creating an index, writer or zone
directory. Missing corpora are not cached, so subsequent explicit indexing is
visible. Committed FTS and ANN corpora reload from disk after daemon restart.
Unsupported semantic modes still report their availability error.

## Runtime capability discovery

The peer `ZoneApiService.GetSearchCapabilities` RPC queries the currently
loaded Search plugin. It reports configured search modes and the embedding
vector-space tag and dimension without opening indices, loading a model or
calling an embedding endpoint. A local model's dimension remains zero until
its first inference probe; remote configuration supplies an explicit dimension.
Use Search `Health` to check readiness separately.

The host checks its zone catalog without materializing a Raft replica. An
unknown zone returns `NOT_FOUND`; an absent Search plugin returns
`UNIMPLEMENTED`. An embedding dimension beyond the existing RPC's int32
range returns `FAILED_PRECONDITION`. Incomplete or invalid remote embedding
configuration advertises keyword search only.

Capabilities are runtime state and have no persistent store.
Python zone routes start with unknown capabilities; failed peer discovery
clears any previous snapshot and propagates the error.

## Published images and acceptance

| Process | Image | Role |
|---|---|---|
| SDK and compatibility APIs | `ghcr.io/nexi-lab/nexus` | Python SDK/CLI and API adapters that send requests to the workspace daemon |
| Workspace daemon | `ghcr.io/nexi-lab/nexusd-cluster` | Typed VFS, Rust HTTP and the signed Search plugin; owns workspace metadata, authorization, indices and embeddings |

Both images are published from the **same commit** with the **same
tag set** (`edge` on develop/main, `stable`/`latest`/semver on version
tags, plus the commit SHA), so a deployable pairing is pinnable by
digest. Publishing is done by `.github/workflows/docker-publish.yml`
(#4613). The `e2e-edge` smoke runs the published SDK image as a client of the
published workspace daemon. Internal PRs run this acceptance before merge;
mutable release tags require the smoke to pass. It covers file operations,
caller credentials, ReBAC grants and revocation, MCP, indexing visibility,
deletion and the HERB hybrid retrieval gate with the local ONNX model.

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

3. **Workspace ownership**: route file writes and Search requests to the
   same owning VFS. `IndexFile`, `Index` and `Refresh` read files through that VFS;
   `IndexDocuments` accepts inline text. Query and Locate check current
   VFS existence and permissions for each indexed path, including cached
   hits. Register workspace mounts in this namespace. A shared filesystem
   volume supplies bytes; the owning VFS supplies the path metadata and
   authorization facts. Compatibility API writes must reach this same owner.

### Tencent Cloud rollout proposal — awaiting confirmation

The deployment has two ownership domains: the environment cluster on the
Moss VM, and agent clusters in gVisor pods alongside sudocode. Install Search
on the Kernel serving each domain's mounts. Agents using scode send Search
requests to their workspace's owning Kernel. A Moss frontend hosting a scode
session uses that session's owner and identity too.

Before changing either deployment:

1. Finish code migration, required CI and the published-image E2E tests.
2. Read the existing deployment configuration: binary/image digests, plugin
   signatures, mounts and zones, persistent volumes, authentication settings,
   ReBAC storage, callers, TLS and Raft voter topology. Record the exact target
   changes and rollback artifacts for review. Keep credentials out of the plan.
3. Confirm the target agent pod and test namespace, maintenance window and
   rollback thresholds with the operator. Deployment approval covers the
   reviewed changes and test writes.

After confirmation, upgrade one selected agent pod's owning daemon and its
signed Search plugin before enabling callers of the new `IndexFile` RPC.
Use artifacts from the same verified VFS revision. Preserve identity, keys,
certificates, relationship data and VFS storage. Search index files and commit
receipts need persistent storage across pod recreation. Keep the existing
AuthN and AuthZ providers; HTTP forwards the caller's bearer, and gRPC uses
the existing authenticated channel. HTTP listens on loopback behind the
existing authenticated TLS ingress where required.
Queries retain the agent's identity and grants. Index mutations follow the
existing administrator/node management policy. Record the indexing trigger
and its management credential during inventory so agent writes reach the
owning index through that authorized path.

Verify write → index → query, overwrite, binary/empty outcomes, index eviction,
permission grant/revocation, isolation between two identities and zones,
restart recovery, cancellation and overload. Run these checks from scode/MCP
and the actual HTTP caller. Compare resource usage and query/index latency
against the recorded baseline, including the gVisor sandbox. Agree numerical
thresholds from that baseline before rollout.

Expand agent pods only after the selected pod passes. Upgrade the Moss VM
cluster in a separate step and repeat the same acceptance checks for its own
mounts. If either cluster has multiple Raft voters, restart one node at a time
and wait for readiness while preserving quorum and membership. Reindex only
the affected owning namespace when its corpus requires it.

Rollback restores the recorded daemon/plugin artifacts and caller routing,
while retaining VFS and authorization facts. Snapshot the node's Search store
before upgrade so a format incompatibility can restore its matching index and
receipt together. Do not remove the Python server from these deployments
until its remaining APIs have their own migration and acceptance completed.

## Write-to-searchable contract (#4736)

**A write does not index.** The server relays nothing to the plugin
on a write: the plugin's `NotifyFileChange` carries no text, so a
`create` / `update` event would be a pure ack (it answers `skipped` and
mutates nothing), and the server's VFS hooks only send `delete` for
deleted and renamed paths. The plugin has no background poller or
scheduler: text enters the index only through an explicit indexing
call. A document written with a bare `POST /api/v2/files/write` is
therefore **never** searchable, however long you wait — pair the write
with one of the calls below. The same holds for **overwrites**: writing
new bytes to an already-indexed path leaves the *previous* text
searchable until the path is re-indexed, so pair every write of an
indexed file with `index: true` or a `refresh`.

All indexing calls are **synchronous**: the response returns after the
plugin has committed, so there is no "queued" state to poll for.

Single-file indexing and eviction resolve the zone from the file's VFS mount.
An optional `zone_id` asserts that ownership; a mismatch is rejected.
Inline batches use the requested zone and optional per-document zones.
The host requires management authority for index mutations and diagnostics.

| Call | What it indexes | Returns |
|---|---|---|
| `POST /api/v2/files/write` with `"index": true` or `"index": {"text": "…"}` | The file just written, in the same request | `index.status`: `indexed` + `index.index_seq`, `skipped` + `reason`, or `error` |
| `POST /api/v2/files/batch/write` with request-level `"index": true` and/or per-file `index` | Every requested file, one plugin round-trip after the writes land | per-result `index` verdict |
| `POST /v2/documents/file` `{"path": "P"}` | Current bytes of one VFS file or stream, read by its owning Kernel | 200 `status: indexed`, `index_seq`, `zone_id`; 404 missing; 409 `status: skipped` with `reason` and the actual receipt |
| `DELETE /v2/documents/file?path=P` | Evicts one path's index entries; retains the VFS file | 200 `status: deleted`, `index_seq`, `zone_id` |
| `POST /v2/documents/batch` `{"documents": [{"path", "text"}]}` | Caller-supplied text, any number of documents | `indexed_count`, `skipped_count`, `skipped_paths`, `parked_paths`, `index_seq`; parked work returns 409; storage failure returns 500 |
| `nexus search index <dir>` / plugin `Index` + `Refresh` RPCs | A directory walk through the plugin host's VFS | counts |
| `POST /api/v2/admin/reindex` with `target` `all` or `search` (admin) | Every path in the replayed operation log: deletes evict, the rest are read server-side and indexed in bounded batches (non-UTF-8 or > 2 MiB files are `skipped`) | `search_paths_indexed` / `_deleted` / `_skipped` (+ reason histogram), `search_index_errors` + failed paths, `search_index_seq` |

`index: true` on a write and `/v2/documents/file` decode the written bytes
as UTF-8. A binary payload is reported `skipped` with a reason instead
of being silently acknowledged — pass `index: {"text": "<extracted
text>"}` to index it. Empty or whitespace-only content is `skipped`
too (409 on single-file indexing). A binary skip commits nothing and returns
`index_seq: 0`; empty text removes previous chunks and returns that commit's
sequence. The TypeScript `IndexingClient.indexFile()` returns these skips as
typed results, including through the TUI worker. Other HTTP errors retain
their status, body and retry information.

When `index` is requested and the search plugin is unreachable, the
write fails with **503 before anything is written**, so a write+index
request never half-succeeds. A plugin failure *after* the write landed
is reported as `index.status = "error"` (HTTP 200: the write itself
succeeded); retry indexing with `/v2/documents/file` or `/v2/documents/batch`.

### Confirming a document is served

Explicit document batches, write+index and per-file refresh return the plugin's
**`index_seq`**: a
plugin-wide, persisted, monotonic sequence stamped *after* the commit
that made the batch visible. `GET /v2/documents/stats` reports:

| Field | Meaning |
|---|---|
| `last_index_seq` | Sequence of the last committed index mutation. `last_index_seq >= <your index_seq>` means your batch is served. |
| `pending` | Documents accepted by in-flight `IndexDocuments` / `IndexFile` work, including after caller cancellation. |
| `last_successful_index_at_ms` | Epoch milliseconds of the last committed mutation (`0` = never). The Python SDK also exposes ISO-8601 `last_successful_index_at` and float epoch seconds `last_index_refresh`. |
| `indexing_in_progress` | In-flight `Index`, `IndexDocuments`, `IndexFile`, `Refresh` and mutating `NotifyFileChange` operations. |

Stall signature — the #4725 class, index acknowledged but nothing
served: `pending > 0` or `indexing_in_progress > 0` for longer than one
indexing call should take, while `last_successful_index_at_ms` does not
advance. Alert on that instead of running a synthetic write probe.

The sequence lives in `<data>/plugins/search/index_seq.json` and
survives plugin-host restarts, so a seq captured before a restart still
compares correctly afterwards.

## Environment reference (plugin host)

Set these on the `nexusd-cluster` process — the plugin reads process
env directly.

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_PLUGIN_DIR` | `/plugins` (in image) | `--plugin-dir` scan target |
| `NEXUS_SEARCH_MODEL_DIR` | `<data>/plugins/search/models` | Local embedding model directory (see below) |
| `ORT_DYLIB_PATH` | *(unset)* | Absolute path to `onnxruntime.{so,dylib,dll}`; **required** for local semantic search |
| `NEXUS_SEARCH_BATCH_CONCURRENCY` | `4` (clamp 1..=16) | BatchQuery inner-query concurrency (#4610) |
| `NEXUS_SEARCH_INDEX_MAX_INFLIGHT` | `8` | Shared Rust Search service cap for `Index`, `Refresh`, `IndexDocuments`, `IndexFile` and mutating `NotifyFileChange` across HTTP, gRPC and SDK clients. Create/update notifications commit nothing and consume no slot. Read once at startup. Overflow returns gRPC `RESOURCE_EXHAUSTED`, or HTTP `429` with `Retry-After: 5`. Cancelled callers retain their slot until accepted work finishes. `0` disables the cap; invalid or negative values use the default. |
| `NEXUS_SEARCH_EMBED_API_URL` | *(unset)* | OpenAI-compatible `/v1/embeddings` endpoint; presence selects the remote embedder (#4614) |
| `NEXUS_SEARCH_EMBED_MODEL` | — | Remote model name (required with URL) |
| `NEXUS_SEARCH_EMBED_DIM` | — | Remote embedding dim (required with URL) |
| `NEXUS_SEARCH_EMBED_API_KEY` | *(unset)* | Bearer token for the endpoint |
| `NEXUS_SEARCH_EMBED_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `NEXUS_SEARCH_EMBED_CONCURRENCY` | `4` | Max concurrent embedding calls across `IndexDocuments` batches (#4777). Embedding runs OUTSIDE the per-zone write lock, so concurrent batches overlap their provider round-trips instead of serialising. `0` = unlimited. |
| `NEXUS_SEARCH_ANN_FLUSH_SECONDS` | `30` | While sibling `IndexDocuments` batches are in flight for a zone, the full HNSW dump is left to the last one, and while siblings are queued on the zone lock the tantivy commit is coalesced into the last queued batch too; this is the fallback delay after which a deferred dump (and any coalesced commit) is forced to disk if the stream never pauses (#4777). `0` disables both (every batch commits and dumps, pre-#4777 behaviour). Deferred vectors are served from memory immediately and recorded as retry-me until the dump lands, so a crash costs a re-embed, never a hole. |
| `NEXUS_SEARCH_ANN_FLUSH_IDLE_SECONDS` | `5` | After `NEXUS_SEARCH_ANN_FLUSH_SECONDS`, the fallback flusher waits until the zone has had no `IndexDocuments` batch in flight, queued or deferred for this long before dumping. The dump holds the zone write lock for the whole graph rewrite (seconds at 100k+ chunks), so landing it in a pause keeps it off the request path; a fixed 30 s cadence stalled about one index call per window for 2–11 s in production. Semantic queries are never blocked by a dump. `0` = no idle gate. |
| `NEXUS_SEARCH_ANN_FLUSH_MAX_SECONDS` | `300` | Upper bound on how long the idle gate may postpone a deferred dump from the first deferral of its window — caps the crash re-embed window when indexing never pauses. |
| `NEXUS_SEARCH_ANN_DEFER_MIN_CHUNKS` | `10000` | Zones with at least this many live ANN chunks defer their HNSW dump even when no sibling batch is in flight, so a large index is rewritten at most once per `NEXUS_SEARCH_ANN_FLUSH_SECONDS` rather than once per `IndexDocuments` call (a 210k-chunk index is a ~1.5 GB dump; inline it stalled every co-located `files/write` for seconds). Smaller zones keep the inline dump. `0` = always defer. |
| `NEXUS_SEARCH_ANN_EXACT_MAX_CHUNKS` | `4096` | A `path=`-scoped semantic or hybrid query whose subtree holds at most this many live chunks is answered by exact cosine scoring over those chunks' own vectors: no graph traversal, no fetch ceiling, a few milliseconds, and exact rather than approximate results. Larger subtrees fall back to the widening fetch below. `0` disables exact scoring. The index keeps an `id → point` lookup for this, rebuilt on every commit (O(chunks), tens of ms at a few hundred thousand chunks). |
| `NEXUS_SEARCH_ANN_FILTER_MAX_FETCH` | `4096` | Ceiling on the ANN candidates a `path=`-scoped semantic or hybrid query pulls. The path filter is applied after the vector top-k, so the fetch starts at 4×`limit` and widens geometrically until the response is full, the subtree's live chunks are all found, the graph is exhausted, or this ceiling is hit. A subtree with no live chunks answers without any vector search. Before this, a small subtree in a 220k-chunk corpus returned nothing for any query whose best subtree match ranked outside the global top 4×`limit` (#4777 follow-up). Unfiltered queries never widen. |
| `NEXUS_SEARCH_PHASE_TRACE` | *(unset)* | Set to any value to print a per-batch phase trace for `IndexDocuments` (lock acquired / FTS committed or coalesced / embedded / dump deferred) on the plugin host's stderr. The plugin's `tracing` events do not reach the host log, so this is the way to see where a batch spends its time live. |

And on the **server**:

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_SEARCH_PLUGIN_TARGET` | `127.0.0.1:2126` | gRPC dial target for the plugin |
| `NEXUS_SEARCH_DAEMON` | auto (on when a DB URL is present) | `true`/`false` force search proxy on/off |
| `NEXUS_SEARCH_PLUGIN_TLS` | *(unset)* | `true` ⇒ TLS channel to the plugin host |
| `NEXUS_SEARCH_PLUGIN_TLS_CA` | *(system roots)* | CA bundle path for server verification |
| `NEXUS_SEARCH_PLUGIN_TLS_CERT` / `_KEY` | *(unset)* | Client cert+key pair for mTLS |
| `NEXUS_SEARCH_PLUGIN_ALLOW_INSECURE` | *(unset)* | Explicit opt-in for **plaintext to a non-loopback** target (trusted network only) — without it the server refuses the channel and boots with search disabled |
| `NEXUS_SLOW_REQUEST_MS` | `10000` | Any HTTP request slower than this logs `request_completed` at WARNING with `slow_request=true`, regardless of status. `0` disables. |
| `NEXUS_AUTH_CACHE_DEBUG` | *(unset)* | `1` logs every auth-cache get/set outcome (`[AUTH-CACHE] get hit/miss`, `set ok`, store class, payload size) at INFO — one line per request, for diagnosing a live miss rate. Cache backend errors are always fail-open (logged at WARNING, request re-authenticates against the provider). |
| `NEXUS_AUTH_CACHE_MAX_ENTRIES` | `10000` | Size of the process-local auth-result cache used when no shared cache store (Dragonfly) is configured. Auth results are cached 15 min; key revocation flushes the cache. |

**Backpressure**: HTTP `429` from an indexing operation carries
`Retry-After: 5`; direct gRPC callers receive `RESOURCE_EXHAUSTED`.
Clients should back off and use `/v2/documents/stats`
(`indexing_in_progress`, `pending`) to pace submissions. The shared limit
belongs to the Search host, so configure it on `nexusd-cluster`.

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
`ann-mE5-small-v2-v2` or `ann-api-text-embedding-3-small-1536-v2`), so
switching embedders later re-tags and rebuilds alongside the old
directory instead of corrupting it.

The local tag moved from `mE5-small-v1` to `mE5-small-v2` when the
local embedder switched to mean pooling and the e5 `query: ` /
`passage: ` input prefixes (the `v1` vectors were CLS-pooled and
unprefixed). Walked directories re-embed on the next refresh; corpora
indexed through `POST /v2/documents/batch` need `nexus reindex
--target search` (or a replay of their index calls) before semantic
results return. The old `ann-mE5-small-v1-*` directory can be deleted
once the new one is populated.

## CLI on a Rust cluster

`nexus search query`, `index`, and `stats` use the cluster HTTP listener.
Set `NEXUS_URL` to that listener, or select a named CLI profile containing
its URL and API key. `--zone-id` (or `NEXUS_ZONE_ID`) overrides the profile's
zone; query results always remain subject to the key's zone and file permissions.

```bash
export NEXUS_URL=http://localhost:2027
export NEXUS_API_KEY=<cluster-api-key>
nexus search index /docs --zone-id sharedzone
nexus search query constellation --mode keyword --path /docs --zone-id sharedzone --json
nexus search stats --zone-id sharedzone
```

These commands call `POST /v2/documents/index`, `POST /v2/search/query`,
and `GET /v2/documents/stats`. Query `--json` emits an array of results.
Index and stats require an administrative key. A query key needs read access
to its zone and the matching files. Backend errors and rejected credentials
produce a nonzero exit code.

Stats show the Rust counters separately: indexed files (`fts_path_count`),
keyword chunks (`fts_doc_count`), vector chunks (`ann_chunk_count`), and
pending documents (`pending`).

## TUI on a Rust Search host

The Search panel and file explorer use the Rust HTTP query, glob and grep
routes. `--search-url`, `NEXUS_SEARCH_URL`, or `search_url` in `nexus.yaml`
selects the owning host's listener or TLS proxy; omission uses the configured
server URL. File access and Search must share workspace ownership and the
credential's authorization source. The existing API worker carries the bearer
credential through both listeners.

The terminal displays native chunk text, zone attribution, discovery limits
and request errors. An explicit configured zone pins the query; otherwise
the Rust host selects readable zones. A directory walk requires access to
its root. An explicit discovery working set is checked per file.
