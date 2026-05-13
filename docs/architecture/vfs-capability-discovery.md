# VFS Capability Discovery

Nexus VFS clients discover server and mount capabilities with an `Initialize`
handshake. gRPC clients call `NexusVFSService.Initialize`; HTTP clients call
`GET /api/vfs/initialize`.

Clients should call initialize before issuing VFS operations and cache the
response for the lifetime of the connection. A client may call initialize again
after mount topology changes.

Capability declarations are advisory. They help clients choose behavior and
avoid calls that are known to fail, but server-side authentication,
authorization, and backend checks remain authoritative.

## Entry Points

The gRPC request carries client metadata and the auth token used by the VFS RPC
transport:

```proto
message InitializeRequest {
  string client_name = 1;
  string client_version = 2;
  string protocol_version = 3;
  string auth_token = 4;
}
```

The HTTP endpoint uses the normal Nexus HTTP authentication headers and returns
the same response shape as gRPC, encoded as JSON:

```json
{
  "server_name": "nexus",
  "server_version": "0.10.0",
  "protocol_version": "0.1.0",
  "capabilities": {
    "posix": {
      "read": true,
      "readdir": true,
      "stat": true,
      "write": false
    },
    "commands": {
      "grep": { "supported": true, "filetype": { "allow": [], "deny": [] } },
      "glob": { "supported": true, "filetype": { "allow": [], "deny": [] } }
    },
    "workspace": { "snapshot": false, "restore": false, "watch": false },
    "backends": {},
    "extensions": ["x-nexus:versioning"]
  }
}
```

## Semantics

POSIX fields use presence-aware booleans. `true` means the server declares the
capability. `false` means the server declares it unsupported. A missing field is
unknown, not support.

Clients must not infer support from proto3 defaults. This matters for older
servers, partial backend declarations, and future capability fields. When a
capability response is unavailable, clients should keep their legacy behavior
and let the server remain the source of truth.

## Per-Mount Backends

The top-level `capabilities.posix` describes the root or default capability
set. `capabilities.backends` contains per-mount overrides keyed by user-facing
mount path:

```json
{
  "capabilities": {
    "posix": { "read": true, "write": true },
    "backends": {
      "/mail": {
        "backend_name": "gmail",
        "backend_type": "external",
        "posix": { "read": true, "write": false, "unlink": false },
        "features": ["directory_listing"],
        "extensions": [],
        "rust_native": false,
        "external": true
      }
    }
  }
}
```

For a path such as `/mail/inbox/thread.yaml`, clients should choose the longest
matching mount prefix. If a matching backend does not declare a specific POSIX
field, that field is unknown for that path.

## Extensions

Extensions are opaque strings. Nexus-owned extensions use the `x-nexus:` prefix,
for example `x-nexus:versioning`. Backend or vendor-specific extensions use
`x-<vendor>:`. Unknown extensions are ignorable and must not break clients.

## Python

Remote Python connections call initialize during connection setup when the
server supports it. The discovered POSIX capability map is exposed on the
returned filesystem object:

```python
# skip-test
import nexus

nx = nexus.connect(
    config={
        "profile": "remote",
        "url": "http://localhost:2026",
        "api_key": "sk-test",
    }
)

caps = getattr(nx, "capabilities", None)
if caps and caps.get("posix", {}).get("write") is True:
    nx.sys_write("/notes.txt", b"hello")
```

Lower-level remote callers can invoke the handshake directly:

```python
# skip-test
from nexus.remote.rpc_transport import RPCTransport

transport = RPCTransport("localhost:2126", auth_token="sk-test")
payload = transport.initialize(client_name="my-client")
```

## TypeScript

The TypeScript HTTP client exposes the HTTP endpoint through
`FetchClient.initialize()`. By default the client transforms JSON keys to
camelCase:

```ts
import { FetchClient } from "@nexus-ai-fs/api-client";

const client = new FetchClient({
  apiKey: "sk-test",
  baseUrl: "http://localhost:2026",
});

const init = await client.initialize();

if (init.capabilities.posix.write === true) {
  await client.post("/api/v2/files/write", {
    path: "/notes.txt",
    content: "hello",
  });
}
```

Because POSIX booleans are optional, TypeScript callers should check for
`true` or `false` explicitly when the difference between unsupported and
unknown matters.
