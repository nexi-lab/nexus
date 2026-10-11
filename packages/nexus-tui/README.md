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

## Tests

Use Bun 1.3.14 and tmux 3.6 or newer on Linux. Run `bun test` and
`bun run lint` from this package. Linux CI pins tmux 3.6b and verifies its
release checksum. Terminal tests assert real startup frames and child exit
codes.

The published-image CI also runs `tests/e2e/native-search.test.tsx` against
Rust hosts with TLS, per-file grants, credential revocation and a local
embedding model.
