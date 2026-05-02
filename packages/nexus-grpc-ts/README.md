# @nexus-ai-fs/grpc-client

TypeScript client for the Nexus VFS gRPC service (`:2028`).

Counterpart to the Rust client in `rust/kernel/src/rpc_transport.rs` —
same `.proto`, same wire contract. Built on
[Connect-RPC](https://connectrpc.com/) so the same package works in
Node and (with a different transport) in browsers.

## Install

```sh
npm install @nexus-ai-fs/grpc-client
```

## Generate stubs

The `.proto` files live at the repo root under `proto/`. Stubs are
produced by `buf generate` (config: `buf.gen.yaml` at the repo root)
and land in `src/gen/`.

```sh
# from the repo root
buf generate

# or from this package
npm run generate
```

`src/gen/` is gitignored — regenerate before building.

## Usage (Node / Electron main process)

```ts
import { readFileSync } from "node:fs";
import { createNexusClient } from "@nexus-ai-fs/grpc-client";

const client = createNexusClient({
  baseUrl: "https://nexus:2028",
  authToken: process.env.NEXUS_TOKEN!,
  tls: {
    ca: readFileSync("/etc/nexus/ca.pem"),
    cert: readFileSync("/etc/nexus/client.pem"),
    key: readFileSync("/etc/nexus/client.key"),
  },
});

const resp = await client.read({ path: "/foo", contentId: "" });
console.log(resp.size, resp.contentId);
```

The transport stamps `authToken` onto every request body automatically
(the Nexus server reads auth from the message body, not gRPC metadata)
and retries on transient failures (`UNAVAILABLE`, `DEADLINE_EXCEEDED`)
with the same backoff schedule as the Rust client.

## Architecture for Electron apps

Renderer processes can't speak native HTTP/2 gRPC. The recommended
pattern is to keep this client in the **main** process and expose typed
IPC handlers to the renderer:

```ts
// main.ts
const vfs = createNexusClient({ baseUrl, authToken, tls });
ipcMain.handle("vfs:read", (_e, path: string) =>
  vfs.read({ path, authToken: "", contentId: "" }),
);
```

The renderer never sees the gRPC channel or the mTLS private key.
