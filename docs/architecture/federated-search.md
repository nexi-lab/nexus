# Federated Search

The owning VFS host executes Search and authorizes every returned file. Its
[Rust dispatcher](https://github.com/nexi-lab/nexus-vfs/tree/main/rust/federated-search)
owns cross-zone routing and fusion. Ranking parameters and reusable algorithms
live in [search-common](https://github.com/nexi-lab/nexus-vfs/tree/main/rust/search-common).

## Request path

`POST /v2/search/query` uses the caller's existing bearer credential. An
explicit `zone_id` selects one readable zone. An omitted zone can fan out over
the caller's live ReBAC-readable zones, within the credential's zone ceiling.
Local legs call SearchService.Query. Remote legs use mTLS and short-lived
Search delegation bound to the caller and target zone.

The dispatcher applies cross-zone RRF, preserves per-zone failure information
and returns readable results. It does not substitute a different search engine
when a backend fails. Query parameters are converted to protobuf in one shared
HTTP adapter, including path unions, fusion, expansion, recency and explicit
prefix weights.

`POST /v2/search/query/batch` makes one SearchService.BatchQuery call. Each entry
addresses one zone on the connected host; omission selects the caller's default
zone. The batch has one credential, accepts up to 500 entries, preserves order
and exposes per-query errors. Invalid authorization rejects the entire call.
The request revision fence covers the caller's default zone on this node.

## State ownership

Each executing zone reads its ranking configuration from the owner's
`path_contexts` SQL table. SearchService.PutPathContext, ListPathContexts and
DeletePathContext manage those durable facts. Explicit request weights override
configured defaults for matching prefixes.

Candidate caches, accessible-zone caches, configuration refresh timestamps,
errors and coordination locks remain in process memory. Cached candidates still
receive current host authorization; cached identity cannot restore a revoked key
or file grant.

## Validation

The required VFS workspace CI exercises a real signed Search plugin, cluster
daemon, mTLS, HTTP and gRPC clients, Raft-backed grants, SQL configuration and
restart. It checks zone ceilings, caller isolation, write-only credentials,
ranking, positional batch errors and revocation against warm caches.

Nexus published-image CI additionally runs actual SDK, MCP, CLI and TUI callers
against that host. The scripts preserve the caller's credential through their
existing authenticated transport.
