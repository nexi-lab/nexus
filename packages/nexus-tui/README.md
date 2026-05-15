# @nexus-ai-fs/tui

Terminal UI for Nexus.

## Usage

Run the published package with Bun:

```bash
bunx @nexus-ai-fs/tui
bunx @nexus-ai-fs/tui --url http://remote:2026 --api-key KEY
```

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
