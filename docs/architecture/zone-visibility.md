# Zone visibility on enumeration surfaces

Issue: nexi-lab/nexus#4740 (zone defaults fell open on list/search).
Companions: `consistency-contract.md` (#4737), `rebac.md` (per-file ReBAC).

Reads and writes are path-addressed: the caller has to know a path, and the
RPC layer prefixes it with the caller's `/zone/<id>/` namespace
(`nexus.lib.zone_scoping`). **List and search enumerate**, so the zone
predicate applied to the candidate set *is* the tenant boundary. This document
records what that predicate guarantees.

## 1. The rule

`nexus.lib.zone_visibility.resolve_zone_view(context, all_zones=…)` is the
single source of truth. It returns a `ZoneView` — the set of zones the caller
may enumerate — or refuses.

| Caller | View |
|--------|------|
| `context is None` (kernel / internal caller) | unrestricted |
| the kernel's own init credential (`NexusFS._init_cred`) passed explicitly — the embedded operator, e.g. the MCP tools in sandbox mode; matched by identity, a copy is an ordinary caller | unrestricted, same as `context is None` |
| `is_system` without a zone claim (internal service scans) | unrestricted |
| `is_system` with a zone (ReBAC expansion, zone export) | that zone's namespace, plus root-namespace rows (see §1.1) |
| credential with `zone_perms` | zones granting `r` or `x`; write-only grants see nothing |
| credential with `zone_set` only (legacy) | those zones |
| credential with `zone_id` only | that zone |
| admin without a zone claim | the **root zone**, not every zone |
| admin with `all_zones=true` | unrestricted, **audited** |
| non-admin with `all_zones=true` | refused (403) |
| **non-admin, non-system, no zone claim** | **refused (403)** |

The `zone_id=root` placeholder a multi-zone token carries does not make it a
root-zone caller: only an explicit root grant or claim does.

An entry is visible when the zone embedded in its internal path
(`/zone/<id>/…`, or the legacy `/zones/<id>/…` the ReBAC filter chain also
treats as zone-scoped) is in the view; entries outside a zone namespace fall
back to their `zone_id` column, `None` meaning root. Consequences:

* **Root-tagged entries are visible only to root-zone callers.** Anything
  written without zone scoping (embedded mode, an admin key without a zone
  header, root-zone traffic) is no longer listable by every tenant.
* A root-tagged row written *inside* a tenant namespace (`/zone/ta/x`, zone
  column root) belongs to that tenant's view and is hidden from root.
* Explicit ReBAC cross-zone shares (`shared-viewer` etc.) are re-added by the
  search list pipeline after the zone predicate, as before.

### 1.1 What the kernel stamps

In standalone mode the kernel stamps every metadata row with the **route's**
zone, which is `root` — including rows under `/zone/<id>/…` written by a
zone-scoped context (verified against the pinned kernel: `sys_stat` reports
`zone_id=root` for `/zone/acme/y.txt`). The column therefore carries no
tenant information in standalone deployments; the `/zone/<id>/` prefix that
`scope_params_for_zone` adds to every remote caller's path **is** the tenant
boundary, and the predicate above treats the path zone as authoritative.

Two consequences:

* An embedded (in-process) context with a zone can enumerate only its
  `/zone/<id>/…` namespace. Rows it writes under legacy layouts such as
  `/workspace/<zone>/…` are root-attributed by the kernel and visible only to
  root-zone callers. `tests/integration/core/test_embedded_namespaces_rebac.py`
  documents this.
* Internal service scans that carry a zone (`is_system=True`, e.g. the ReBAC
  directory expander, the Tiger resource-map sync, zone export) keep the
  root-namespace rows they always saw — `ZoneView.system` — while remaining
  scoped to their zone's namespace. Without this, ReBAC inheritance on legacy
  layouts would silently break in standalone deployments.

Making the kernel stamp the caller's zone (`context_zone_id` is already
passed) would let the column carry tenant information everywhere; that is a
nexus-vfs change and the natural follow-up to "require a zone tag on every
write".

## 2. Where it is enforced

| Surface | Enforcement |
|---------|-------------|
| `NexusFS.sys_readdir` (RPC `list`/`sys_readdir`, gRPC `Call`, REST `/api/v2/files/list`) | view resolved first; recursive/detail/paginated branches post-filter the metastore scan; the non-recursive kernel fast path fetches each child's zone with one `stat_batch` and filters |
| `SearchService.list` (and `glob`, `grep`, `glob_batch`, which build on it) | view resolved first; the scan uses the caller's `/zone/<id>/…` path as scoped (the former "flat standalone rows" prefix strip is gone; it made every enforced tenant list/glob/grep scan the root namespace); candidate rows filtered before permission filtering; paginated pages filtered per page |
| REST `/api/v2/search/query`, `/query/batch` | `token_zone_filter_from_auth` fails closed (empty set → 403) for a non-admin credential with no zone claim |
| REST `/api/v2/files/*`, `/api/v2/batch`, `/api/v2/search/glob|grep` | Zone-scoped callers get a `ZoneScopedFS` view (`server/api/v2/_zone_scoped_fs.py`): every request path is prefixed with `/zone/<id>/` (a path naming another zone is 403), every result path is unscoped. Same contract as the RPC/gRPC `scope_params_for_zone` |
| Rust `http-api` `/v2/search/*`, `/v2/documents/*` | `rust/services/http-api/src/zone.rs`: zone derived from the authenticated `OperationContext`, not the request body; zone-less non-admin → 403; `root_path` scoped and results unscoped; admins may name a zone explicitly |
| Server boundary (`get_operation_context`) | a missing zone claim is no longer coerced to root for non-admins; the context stays zone-less and the surfaces above refuse it. Admins keep root as their default zone |
| `context_for_target_zone` | returns a copy; the caller's context is never mutated when an admin is retargeted to `/zone/<id>/` |

Providers that legitimately mean "the default zone" now claim root
explicitly instead of relying on the server's coercion: open-access
(loopback, no auth configured), `DatabaseLocalAuth` users without an assigned
zone, and OIDC when a zone claim is not required. `StaticAPIKeyAuth` and
`LocalAuth` keys configured without a `zone_id` are zone-less on purpose and
are refused on list/search — add `zone_id: root` to the key's config if the
root view is intended. `DatabaseAPIKeyAuth` already refuses zone-less
non-admin keys at authentication time (#3871).

## 3. Admin cross-zone listing

`all_zones=true` is available on `sys_readdir` (RPC `list` param, REST
`?all_zones=true`) and `SearchService.list`. Each request emits one
`zone.all_zones_enumeration` audit event (`nexus.lib.events.emit_audit_event`)
carrying the operation, path, subject, zone and `request_id`, plus an INFO log
line. Nested calls within the same request (pagination, explicit child routes)
are recorded once.

## 4. Known gaps (unchanged by #4740)

* **Migration note for zone-scoped REST clients.** Before #4740 the REST
  routes never prefixed paths, so a zone-scoped key's REST writes landed in
  the root namespace (`/x.txt`, stamped root). Those rows are now outside
  the key's namespace: they stay readable by root-zone callers and can be
  moved into place by an admin (`rename /x.txt → /zone/<id>/x.txt`).
* The recursive / detail / paginated `sys_readdir` branch is a Python walk
  over the kernel's per-directory `readdir` plus `stat_batch`
  (`kernel_client.metastore_list_paginated`); whether it reaches
  federation-zone entries behind their own mounted metastore was not
  verified here.
* The Rust `http-api` handlers have no file-level ReBAC post-filter yet;
  that is the R10 epic's step (#4674). Zone scoping there is done.
* Search-index routing: `/search/query` scopes to the credential's zone. A
  zone-less credential is refused; it is not redirected to the root index.
