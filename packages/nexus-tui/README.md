# @nexus-ai-fs/tui

Terminal UI for Nexus.

## Usage

Run the published package with Bun:

```bash
bunx @nexus-ai-fs/tui
bunx @nexus-ai-fs/tui --url http://remote:2026 --api-key KEY
```

Search uses the owning Rust host's `/v2/search/*` APIs. For a separate Search
listener, set `NEXUS_SEARCH_URL`, `search_url` in `nexus.yaml`, or
`--search-url <url>`. Use its TLS proxy for a remote connection. The configured
Search host and file server must share workspace ownership and authorization.
The API worker forwards the existing credential to that host.

The Search panel displays native chunks and zone attribution. File explorer
search supports `g:` glob, `r:` grep and indexed queries, and shows access
errors and discovery result limits.

The installed binary name is:

```bash
nexus-tui
```

## Local Development

```bash
cd packages/nexus-api-client
npm install
npm run build

cd packages/nexus-tui
bun install
bun run src/index.tsx
```
