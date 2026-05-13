# Issue 4085 VFS Capability Handshake Design

## Context

Issue #4085 asks for a capability-declaring negotiation handshake for the VFS RPC protocol. Nexus currently has a tightly pinned gRPC surface in `proto/nexus/grpc/vfs/vfs.proto`; adding backend-specific behavior usually requires a coordinated proto bump or out-of-band metadata. Asymmetric mounts also fail late with unsupported-operation errors instead of letting clients discover limits before issuing an operation.

The implementation will land full scope in one PR. The PR should still be internally staged so reviewers can evaluate the protocol, server aggregation, Python client, TypeScript client, FUSE behavior, docs, and tests independently.

## Goals

- Add a typed `Initialize` RPC to the VFS gRPC service.
- Return server metadata, negotiated protocol version, per-mount capabilities, command/workspace capabilities, and extension identifiers.
- Reuse the existing backend feature declarations where possible instead of inventing a second backend registry.
- Expose `client.capabilities` in Python remote connections and `RPCTransport.initialize()` for lower-level callers.
- Add TypeScript capability discovery through the existing HTTP API client package without introducing a TS gRPC stack in this PR.
- Let `nexus-fuse` discover capabilities at mount startup and reject obviously unsupported write-like operations locally.
- Document proto3 default semantics: an absent or false capability is not permission to assume support.

## Non-Goals

- This PR will not add a new TypeScript gRPC transport.
- This PR will not replace existing backend runtime errors; server-side enforcement remains authoritative.
- This PR will not implement every future command capability. It will define the schema and fill the capabilities Nexus can determine reliably today.
- This PR will not redesign mount persistence or the Rust `VFSRouter`.

## Protocol

Create `proto/nexus/grpc/initialize.proto` in package `nexus.grpc.vfs`. Import it from `proto/nexus/grpc/vfs/vfs.proto` and add `rpc Initialize(InitializeRequest) returns (InitializeResponse)` to `NexusVFSService`.

`InitializeRequest` contains `client_name`, `client_version`, and `protocol_version`. `InitializeResponse` contains `server_name`, `server_version`, `protocol_version`, and `Capabilities`.

`Capabilities` contains:

- `PosixCapabilities posix`
- `CommandCapabilities commands`
- `WorkspaceCapabilities workspace`
- `map<string, BackendCapabilities> backends`, keyed by user-facing mount path
- `repeated string extensions`

The schema should keep booleans simple for POSIX-like operations: `read`, `readdir`, `stat`, `write`, `unlink`, `mkdir`, `rmdir`, `rename`, and `glob`. Command capability fields can use conservative structured messages where support may be conditional, such as a string filter for grep file types. Backend capability messages include the per-mount POSIX profile, backend name/type when known, feature strings, and extension strings.

Unknown extensions are ignorable. Nexus-owned extensions use `x-nexus:`. Third-party or backend-specific extensions use `x-<vendor>:`. Clients must treat missing fields as "not declared" and must not infer support from proto3 defaults.

## Server Aggregation

The Rust tonic server in `rust/transport/src/grpc.rs` owns the new `Initialize` handler, matching the existing `Ping` authentication behavior. It authenticates the request, then builds a capability response from the Rust kernel and a Python bridge callback.

The Rust side contributes:

- server name/version and protocol version
- canonical mount keys from `Kernel::get_mount_points()`
- route-derived per-mount facts available from the kernel, such as whether a mount has a Rust backend and whether it is external
- conservative POSIX defaults for Rust-native storage backends

The Python bridge contributes:

- Python-side and external connector details that are not visible through the Rust `ObjectStore` trait
- service command capabilities such as grep/glob/workspace support
- mappings from existing `BackendFeature` declarations in `nexus.contracts.backend_features`

Capability aggregation is request-time, not permanently cached server-side. Clients cache the response on connect and may call `initialize()` again after mount topology changes.

## Backend Mapping

The PR should add one shared Python capability-mapping module that translates existing backend feature declarations into protocol/JSON capability fields. This keeps the source of truth in the existing backend declarations while producing a wire-friendly shape.

Initial mapping rules:

- Rust/Python local path and CAS-like storage declare `read`, `stat`, `readdir`, `write`, `unlink`, `mkdir`, `rmdir`, and `rename` where the backend supports those methods.
- `BackendFeature.DIRECTORY_LISTING` maps to `readdir=true`.
- `BackendFeature.PATH_DELETE` maps to `unlink=true` and `rmdir=true` only when directory delete is supported.
- `BackendFeature.RENAME` maps to `rename=true`.
- Blob/versioned backends with `BackendFeature.NATIVE_VERSIONING` declare `x-nexus:versioning`.
- Read-oriented or uninspectable external connectors should be conservative. If write support cannot be verified, `write=false`.

The response can include a root-level POSIX union for the server and per-mount overrides under `backends`. Clients should prefer the most specific per-mount declaration when a path can be routed to a mount; otherwise they fall back to root-level capabilities or current behavior when no capability response exists.

## Python SDK And Transport

`src/nexus/remote/rpc_transport.py` gains `initialize()` and stores the most recent response in a typed or dict-shaped `capabilities` attribute. `nexus.connect(profile="remote")` calls initialize after the `RPCTransport` is constructed and before returning the `NexusFS` instance. The returned `NexusFS` gets `capabilities` attached for user code.

Remote syscall overrides in `src/nexus/factory/_remote.py` should honor known negative capabilities before issuing network calls:

- `write` and `sys_write` check `write`
- `sys_unlink` or delete wrappers check `unlink`
- `mkdir` checks `mkdir`
- `rmdir` checks `rmdir`
- `sys_rename` checks `rename`

If initialize fails because the server is older and lacks the RPC, remote connections continue with current behavior and no local gating. Other initialize failures that indicate authentication or server errors should propagate like `Ping`.

## HTTP And TypeScript

The TypeScript package in this repo is HTTP-focused, so the one-PR scope should expose the same capability response through a small HTTP endpoint rather than adding TS gRPC dependencies.

Add an HTTP route that returns the same JSON shape as `InitializeResponse.capabilities` plus server/protocol metadata. The route should share the Python aggregation logic used by the gRPC bridge to avoid divergent behavior.

`packages/nexus-api-client` gains:

- capability TypeScript interfaces in `src/types.ts`
- `FetchClient.initialize()` that calls the new endpoint
- exports for the new types
- unit tests covering request path, key transformation behavior, and response typing

## nexus-fuse

`nexus-fuse` uses the HTTP client in `nexus-fuse/src/client.rs`, so it should call the new HTTP capability endpoint during mount startup. Add Rust structs matching the JSON capability response and expose `NexusClient::capabilities()`.

`nexus-fuse/src/fs.rs` should gate obvious unsupported operations before making network requests:

- `write` and `create` return a write-prohibited FUSE error when `write=false`
- `mkdir` returns an operation-not-supported error when `mkdir=false`
- `unlink` returns an operation-not-supported error when `unlink=false`
- `rename` returns an operation-not-supported error when `rename=false`

If the server does not expose capability discovery, FUSE keeps current behavior for backward compatibility.

## Documentation

Add a docs page covering:

- initialize request/response shape
- client-first-call behavior
- per-mount granularity
- proto3 default semantics and forward compatibility
- `x-<vendor>:` extension handling
- examples for Python and TypeScript clients

The docs should call out that capability declarations are advisory for clients and that server-side permission and backend checks remain authoritative.

## Testing

The PR should include focused tests for each surface:

- proto/codegen imports for the new messages and service method
- Rust tonic `Initialize` response shape and auth behavior
- Python `RPCTransport.initialize()` success, old-server fallback, and cached capability exposure
- `nexus.connect(profile="remote")` attaches `capabilities`
- Python capability mapping for at least local writable and read-only/external-style fixtures
- HTTP endpoint returns the shared shape
- TypeScript `FetchClient.initialize()` and exported types
- FUSE HTTP client parsing and operation gating
- docs link/import smoke where existing docs tests require it

## Risks And Mitigations

The largest risk is scope size. The implementation should keep each slice independent and testable: proto/codegen first, server response second, Python client third, HTTP/TS fourth, FUSE fifth, docs last.

The second risk is overclaiming backend support. The mapping must be conservative: false or absent is safer than declaring support that still fails for common operations.

The third risk is duplicated logic between gRPC and HTTP. The Python aggregation module should produce the canonical dict representation, and gRPC should convert that dict into protobuf messages. HTTP should return the same dict shape.

The fourth risk is backward compatibility. Clients must continue to work against older servers that do not implement `Initialize`; local gating applies only when a capability response is present.
