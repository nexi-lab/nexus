# nexus-search-plugin — Python-parity roadmap

**Status:** Phase 0 (planning). This doc is the SSOT for the multi-PR
project of porting Python `SearchDaemon` (`src/nexus/bricks/search/`,
~25 300 LoC) to the Rust `nexus-search-plugin` cdylib.

**Goal:** Rust plugin matches Python's user-observable feature set AND
performance well enough that the Python search stack can be deleted
(follows [[rust-is-primary]]).

## Non-goals

- Not porting Python's internal test scaffolding — Rust tests are their
  own thing.
- Not preserving Python's private helper names or module layout; only
  the user-observable contract (HTTP / gRPC / MCP surface, index file
  format compatibility not required — plugin re-indexes on first run).
- Not covering federation dispatch inside the plugin — kernel's
  federation driver already routes cross-zone reads; plugin serves
  local zone only (mirrors current Glob + Grep behaviour).

## Architecture decisions (SSOT)

Locked in Aug 2026 discussion. Deviations require re-opening the
discussion, not a silent change:

### D1 — Storage: `tantivy` (FTS) + `hnsw_rs` (vector), plugin-local

Per-node derived state in `~/.nexus/plugins/search/<zone_id>/{fts,ann}/`.
Metadata SSOT — "what was indexed at what generation" — lives in the
kernel MetaStore via `set_file_metadata(path, "search.indexed_gen", …)`
so any node can drop-and-rebuild without losing correctness anchors.
Precedent: `nexus-vfs/rust/raft/src/raft/search_caps.rs`.

**Why tantivy / hnsw_rs and not hand-roll?**
- BM25 hand-rolled on redb: ~1000 LoC, 2-5× slower than tantivy (missing
  skip lists + column compression on postings).
- HNSW hand-rolled: ~1500 LoC + concurrency landmines.
- Both are mature pure-Rust crates with no C deps; they live in the
  plugin cdylib and therefore never touch the cluster binary
  ([[plugin-dylib-isolates-deps]], [[reuse-mature-rust-crates]]).

**Why not sqlite-vec / LanceDB?**
- sqlite-vec: native extension DLL, cross-platform bundling pain.
- LanceDB: young ecosystem, columnar format lock-in, cross-zone
  federation complexity.

### D2 — Embedding: `fastembed-rs` with `ort` load-dynamic

Provider crate: `fastembed = "5"` (2026-07). Backend: `ort` in
`load-dynamic` mode — `onnxruntime.{dll,dylib,so}` shipped in the
plugin dir, `ORT_DYLIB_PATH` set at plugin init. Model weights:
`multilingual-e5-small` INT8 (~120 MB, MTEB 57.9 / MIRACL 60.8; strong
EN + ZH). Shipped as a separate file in the plugin dir — updating the
model does not require re-signing the cdylib.

**Why fastembed and not raw candle / ort?**
- fastembed bundles pooling, normalization, HF-hub, tokenizer wiring,
  and a model catalogue. Raw candle-transformers would need
  hand-rolling of each of those.
- ort backend claims 3-5× perf and 60-80% less memory vs Python
  `optimum`; both are the standard Rust ML runtime substrate.

**Why mE5-small and not BGE-M3?**
- BGE-M3 INT8 is ~600 MB, too heavy for first-run cold-start.
- mE5-small is the smallest MIRACL-competitive multilingual model.
- Upgrading later is a model-file swap — no code churn.

### D3 — Federation: kernel driver, transparent to plugin

Plugin serves local-zone results only. Cross-zone dispatch, ranking
merge across zones, and read-gating live in the kernel /
higher-tier — same model as today's Glob + Grep (`sys_read` transparently
crosses zones).

**Why not plugin-side fan-out?**
- Duplicates work the kernel already does correctly.
- Multi-zone connection pool + auth lifecycle would grow the plugin's
  responsibility surface for no gain.

### D4 — Chunking: pure port of Python `chunking.py`

Tokenizer-driven markdown/code-aware chunking with configurable window
sizes matches Python 1:1. Rust rewrite uses `tiktoken-rs` for BPE tokens
and `pulldown-cmark` for markdown structure. Contextual boundaries
(headings, code fences) preserved exactly so re-indexing produces
equivalent chunks; embeddings stay comparable across the cutover.

### D5 — Read-gate: kernel-tier, not plugin-tier

Zone-perm filtering (`readable_zone_filter` / `token_zone_filter_from_auth`,
#4557) stays in the router / kernel. Plugin never sees subject identity;
it receives a `zone_id` scope and returns everything in that zone. The
caller does the filter before returning to the user.

## Phased delivery

Each phase is a single PR unless noted. Ships behind a feature flag
(`SEARCH_BACKEND=rust|python`, default `python`) until P8 cutover.

| # | Scope | Rust LoC | New deps | Verify |
|---|---|---:|---|---|
| **P0** | This doc + `PARITY_ROADMAP.md` review | 0 | — | user sign-off |
| **P1** | `tantivy` wired; `Query` gRPC RPC = keyword-only (matches Python `search_type=keyword`); per-zone index open/create at startup; incremental add-doc on `sys_watch`; unit tests | ~1500 | tantivy | E2E: index 100 docs, keyword query returns expected top-K |
| **P2** | `fastembed-rs` + `hnsw_rs`; `SemanticQuery` RPC; index build path adds embedding batch; recall/latency benchmark vs Python | ~1500 | fastembed, ort (dylib), hnsw_rs | Recall@10 within 5% of Python on eval set |
| **P3** | Hybrid fusion (`search_type=hybrid`): RRF + weighted-alpha (#4541), final-list pooling `chunks_per_page` (#4542), fusion params `alpha` / `fusion_method` / `rrf_k` | ~800 | — | Parity test: hybrid ordering matches Python within score tolerance |
| **P4** | Chunking port (`chunking.py` 1570 → Rust); macro-chunk read-side expansion (#4398) | ~2000 | tiktoken-rs, pulldown-cmark | Snapshot tests: chunk boundaries byte-identical to Python on 50 sample docs |
| **P5** | Indexing pipeline: `sys_watch` subscriber → incremental re-index; MetaStore `search.indexed_gen` checkpoint; bounded projection wait (#4566) so write-then-index returns a coherent count | ~1200 | — | E2E: `POST /search/index` after `sys_write` returns count > 0 |
| **P6** | Recency decay #4543 (spirit-port; already have `sort_recency` for glob/grep), path-context boost #4544, skeleton title arm #4545 | ~600 | — | Param-echo tests + eval-set recency-weighted ordering |
| **P7** | Adaptive strategy selection (sequential / parallel / trigram-index for grep), result cache with zone-scoped keys (adversarial-review hardening from #4559 folded in) | ~800 | — | Latency benchmark shows strategy switch at documented thresholds |
| **P8** | Cutover: HTTP router points at Rust plugin behind `SEARCH_BACKEND=rust`; Python `SearchDaemon` marked deprecated; delete after one release cycle at green | — | — | Prod-shape E2E — full ShareOne search flow on Rust backend |

**Total Rust new code:** ~8500 LoC (vs Python 25 300 — port drops dead
code, testing scaffolding, and legacy hybrid pathways).

**Cadence expectation:** each phase merges independently, small enough
for a normal review. Ordering allows early phases to ship a usable Rust
search (P3 = hybrid works) before P4-P7 add polish.

## Success criteria for "usable in production" (P8 gate)

- E2E: ShareOne search-plugin E2E test suite passes on `SEARCH_BACKEND=rust`.
- Latency: p95 hybrid query within 1.5× Python on 100k-chunk corpus.
- Recall: hybrid Recall@10 within 5% of Python on the eval set.
- Cutover: one full-release cycle in dogfood at green before Python
  deletion.

## Open questions (resolve during phase execution, not up-front)

- **Q1:** Where does the ONNX runtime .dylib get shipped from? Options:
  (a) COS-hosted release artefact per plugin version, (b) HF-hub
  download on first run, (c) sudowork/moss release bundles the file.
  Deferred to P2.
- **Q2:** Model swap policy — do we pin one model per plugin version,
  or negotiate at query time? Deferred to P2. Default is version-pinned.
- **Q3:** Do we ship a slim variant with keyword-only (no `ort` / model)
  for lite profiles? Deferred to P8.

## Related memories

- [[search-plugin-python-parity-mirror]] — the STANDING policy that
  triggered this roadmap
- [[plugin-dylib-isolates-deps]] — dep isolation via cdylib
- [[reuse-mature-rust-crates]] — don't hand-roll subsystems
- [[rust-is-primary]] — Python search stack scheduled for deletion
- [[distributed-ssot]] — new fields (e.g. `search.indexed_gen`) live in
  replicated metadata
