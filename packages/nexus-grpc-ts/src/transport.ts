// Thin wrapper around Connect-RPC's gRPC transport for the Nexus VFS service.
//
// Mirrors the Rust `RpcTransport` (rust/kernel/src/rpc_transport.rs):
//   - holds a single channel keyed by base URL
//   - stamps `auth_token` onto every outbound request body (server reads
//     auth from the message body, not gRPC metadata — see
//     rust/transport/src/grpc.rs:125)
//   - retries on transient gRPC failures (UNAVAILABLE / DEADLINE_EXCEEDED)
//     up to 2 times with exponential backoff (200ms, 400ms)
//   - optional mTLS via Node `tls` PEM material
//
// Generated stubs live under `./gen/nexus/grpc/vfs/` after `npm run generate`.

import { Code, ConnectError, createPromiseClient } from "@connectrpc/connect";
import type { Interceptor, PromiseClient } from "@connectrpc/connect";
import { createGrpcTransport } from "@connectrpc/connect-node";

import { NexusVFSService } from "./gen/nexus/grpc/vfs/vfs_connect.js";

export interface TlsConfig {
  /** PEM-encoded CA bundle the server cert is verified against. */
  ca: Buffer | string;
  /** Optional client certificate (mTLS). When set, `key` is required. */
  cert?: Buffer | string;
  /** Optional client private key (mTLS). When set, `cert` is required. */
  key?: Buffer | string;
}

export interface NexusClientOptions {
  /** e.g. "https://nexus:2028" or "http://127.0.0.1:2028". */
  baseUrl: string;
  /** Bearer token forwarded as `auth_token` on every request body. */
  authToken: string;
  /** Optional mTLS material. Omit for plaintext HTTP/2. */
  tls?: TlsConfig;
  /** Per-request deadline in milliseconds. Default: 60s (matches server). */
  timeoutMs?: number;
}

export type NexusClient = PromiseClient<typeof NexusVFSService>;

const DEFAULT_TIMEOUT_MS = 60_000;
const MAX_RETRIES = 2;

/**
 * Create a typed client for the Nexus VFS gRPC service.
 *
 * The returned client exposes one method per RPC declared in
 * `proto/nexus/grpc/vfs/vfs.proto` (`call`, `read`, `write`, `delete`,
 * `ping`). All requests are unary today; if streaming RPCs are added to
 * the proto they appear automatically after regenerating stubs.
 */
export function createNexusClient(opts: NexusClientOptions): NexusClient {
  if (opts.tls?.cert && !opts.tls.key) {
    throw new Error("createNexusClient: tls.cert provided without tls.key");
  }
  if (opts.tls?.key && !opts.tls.cert) {
    throw new Error("createNexusClient: tls.key provided without tls.cert");
  }

  const transport = createGrpcTransport({
    baseUrl: opts.baseUrl,
    httpVersion: "2",
    nodeOptions: opts.tls
      ? { ca: opts.tls.ca, cert: opts.tls.cert, key: opts.tls.key }
      : undefined,
    interceptors: [attachAuthToken(opts.authToken), retryTransient()],
  });

  return createPromiseClient(NexusVFSService, transport);
}

/**
 * Inject `auth_token` into every outbound request body. The Nexus VFS
 * server reads auth from the message body (so static API keys and OIDC
 * tokens flow through the same field), not from gRPC metadata.
 */
function attachAuthToken(token: string): Interceptor {
  return (next) => async (req) => {
    const message = req.message as Record<string, unknown>;
    if (
      message &&
      typeof message === "object" &&
      "authToken" in message &&
      !message["authToken"]
    ) {
      message["authToken"] = token;
    }
    return next(req);
  };
}

/**
 * Retry on transient gRPC failures only — UNAVAILABLE and DEADLINE_EXCEEDED.
 * Backoff matches the Rust client: 200ms, 400ms.
 */
function retryTransient(): Interceptor {
  return (next) => async (req) => {
    let attempt = 0;
    while (true) {
      try {
        return await next(req);
      } catch (err) {
        if (attempt >= MAX_RETRIES || !isRetryable(err)) {
          throw err;
        }
        attempt += 1;
        const delayMs = 100 * (1 << attempt);
        await sleep(delayMs);
      }
    }
  };
}

function isRetryable(err: unknown): boolean {
  if (!(err instanceof ConnectError)) {
    return false;
  }
  return err.code === Code.Unavailable || err.code === Code.DeadlineExceeded;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export { DEFAULT_TIMEOUT_MS };
