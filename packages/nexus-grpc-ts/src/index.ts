export {
  createNexusClient,
  DEFAULT_TIMEOUT_MS,
} from "./transport.js";
export type {
  NexusClient,
  NexusClientOptions,
  TlsConfig,
} from "./transport.js";

// Re-export the generated service descriptor and message types so
// consumers can construct requests / inspect schemas without reaching
// into the `gen/` tree directly.
export { NexusVFSService } from "./gen/nexus/grpc/vfs/vfs_connect.js";
export {
  CallRequest,
  CallResponse,
  DeleteRequest,
  DeleteResponse,
  PingRequest,
  PingResponse,
  ReadRequest,
  ReadResponse,
  WriteRequest,
  WriteResponse,
} from "./gen/nexus/grpc/vfs/vfs_pb.js";
