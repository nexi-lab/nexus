# @nexus-ai-fs/api-client

Shared HTTP client for Nexus APIs.

## Search on the owning Rust host

```typescript
import { FetchClient, SearchClient, resolveConfig } from "@nexus-ai-fs/api-client";

const config = resolveConfig();
const search = new SearchClient(new FetchClient(config), {
  url: config.searchUrl,
  zoneId: config.zoneId,
});
const response = await search.query("needle", "keyword");
const batch = await search.batchQuery([{ q: "needle", query_type: "keyword" }, { q: "other" }]);
```

`NEXUS_SEARCH_URL`, `search_url` in `nexus.yaml`, or the `searchUrl` override
selects the Rust HTTP listener or its TLS proxy. When omitted, Search uses
`baseUrl`. Files, Search and ReBAC must refer to the same owning workspace;
the transport sends its existing bearer credential to the selected host.

Query replies contain `results` and an optional `error`, including partial
federation failures. Glob returns `paths`; grep returns `matches` with
`path`, `line_number` and `line`. Discovery also reports `truncated`.
The client preserves these wire fields regardless of the transport's key
transformation default. `files: []` searches an empty working set.

Batch replies contain `responses` in request order, each with `results` and
an optional `error`. A batch uses one existing credential and up to 500 queries;
each entry addresses one zone on the connected host. Omitting its `zone_id`
uses the configured connection zone or the caller's default zone.

## Local Development

```bash
cd packages/nexus-api-client
npm install
npm run build
npm test
```
