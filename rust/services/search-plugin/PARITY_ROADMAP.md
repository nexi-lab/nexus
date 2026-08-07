# nexus-search-plugin — Python-parity roadmap

**Status:** P0-P7 landed. P8-P12 revised post-audit — see the
"Cutover strategy" section for why P8 is no longer "delete Python".

**Goal:** Rust plugin matches Python's user-observable feature set
AND performance well enough that the Python search stack can be
deleted (follows [[rust-is-primary]]).

## Architecture decisions (SSOT)

Locked in early 2026 discussion. Deviations require re-opening the
discussion, not a silent change.

- **D1 Storage**: `tantivy` (FTS) + `hnsw_rs` (vector), plugin-local
  under `<data-root>/plugins/search/<zone>/{fts, ann-<model>-v2}/`.
- **D2 Embedding**: `fastembed-rs` (`ort` load-dynamic) with
  `multilingual-e5-small` INT8. Graceful degradation when the model
  is absent — keyword still works.
- **D3 Federation**: plugin serves the local zone only; kernel
  driver handles cross-zone dispatch (mirrors Glob + Grep).
- **D4 Chunking**: semantic-equivalent port of Python `chunking.py`
  (heading + code-fence + budget-aware).
- **D5 Read-gate**: zone-perm filtering stays kernel-tier; plugin
  receives `zone_id` scope only, never subject identity.

## Landed phases

| # | Scope | PR |
|---|-------|----|
| P0 | Roadmap SSOT (this doc, first version) | — |
| P1 | tantivy + `Query` keyword-only | #4575 |
| P1r | Retrofits: NEXUS_DATA_DIR + Index E2E + Docker E2E | #4576 |
| P2 | fastembed + hnsw + SemanticQuery | #4577 |
| P3 | Hybrid fusion (RRF / weighted / RRF-weighted) + pooling | #4578 |
| P3+P4 | Audit retrofits + chunker + expand=macro + storage-key v2 | #4587 |
| P5 | Incremental Refresh (mtime cache + stale-sweep) | #4588 |
| P6 | Recency decay + path-prefix boost | #4589 |
| P7 | Zone-scoped query cache | #4590 |
| P5-P7r | Audit retrofits (cache E2E, delete-through-Refresh E2E, builder) | #4591 |

## Cutover strategy (revised post-P7 audit)

The original plan had **P8 = "flip default + delete Python"**. Audit
found this would break production: Python's search router exposes
endpoints the Rust plugin doesn't cover yet (list below). Deleting
Python before those are ported crashes ShareOne.

**Revised strategy: cutover in phases, delete last.**

- **P8** — coexist-mode. Add `SEARCH_BACKEND=rust|python` env flag
  to the Python router; when set to `rust`, the three
  already-parity endpoints (`/query`, `/index`, `/refresh`) forward
  to the Rust plugin. Every other endpoint keeps its Python impl.
  Default stays `python`; a per-zone override can flip specific
  zones early for testing.
- **P9-P11** — port the remaining endpoints one PR per group,
  cutover under the same flag. See "Endpoint gap list" below.
- **P12** — with every endpoint on Rust + one full release cycle
  in ShareOne dogfood at green, flip default to `rust`, delete
  the Python search stack (bricks + tests + contracts).

Under this strategy the Python code stays live and receives bug
fixes until P12 — no premature retirement, no dogfood outage.

## Endpoint gap list (Rust plugin needs before P12)

Sourced from `src/nexus/server/api/v2/routers/search.py` + its
sub-routers. Anything checkmarked already forwards under P8's flag.

- ☑ `GET /query` — keyword / semantic / hybrid (P1 / P2 / P3)
- ☑ `POST /index` — full walk indexing (P1 / P4)
- ☑ `POST /refresh` — incremental (P5)
- ☑ `POST /expand` (as `expand=macro` param on `/query`) (P4)
- ☐ `POST /query/batch` — batched multi-query (P9 target)
- ☐ `POST /locate` — path-only lookup, no scoring (P9 target)
- ☐ `GET /parked` + `POST /parked/retry` + `POST /parked/discard`
  — indexing-failure queue mgmt (P10 target)
- ☐ `POST /consumers/{name}/skip-to` — projection cursor mgmt
  (P10 target)
- ☐ `GET /index-dirs/*` — indexed-dirs metadata surface, 5
  sub-endpoints (P11 target)

Total new Rust surface for P9-P11 estimated at ~2 000 LoC (each
endpoint is 100-400 LoC on top of existing FtsIndex + AnnIndex
primitives).

## Success criteria for P12 (deletion gate)

- Every endpoint above has a Rust counterpart and its docker E2E
  passing on `SEARCH_BACKEND=rust`.
- One full release cycle in ShareOne dogfood on `SEARCH_BACKEND=rust`
  with no user-visible regressions.
- Latency: p95 hybrid query within 1.5× Python on 100k-chunk corpus.
- Recall: hybrid Recall@10 within 5% of Python on the eval set.

## Success criteria for individual endpoint cutovers (P9-P11)

Each endpoint PR ships with:

- Rust impl behind the plugin's existing service dispatch.
- Docker E2E covering the endpoint's shipping surface.
- The Python router's forwarding gate updated so
  `SEARCH_BACKEND=rust` routes to the Rust impl.
- Python router endpoint stays intact but marked
  `# forwarded to Rust under SEARCH_BACKEND=rust; delete at P12`
  so the removal path is documented at the callsite.

## Related memories

- [[search-plugin-python-parity-mirror]] — STANDING policy
- [[plugin-dylib-isolates-deps]] — heavy deps live in cdylib
- [[reuse-mature-rust-crates]] — don't hand-roll subsystems
- [[rust-is-primary]] — Python search stack scheduled for deletion
