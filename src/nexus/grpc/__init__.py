# gRPC transport bindings for Nexus.
#
# Subpackages:
#   nexus.grpc.vfs      — proto-generated stubs (DO NOT EDIT pb2 files)
#
# Modules:
#   nexus.grpc.servicer  — VFSCallDispatcher (sync bridge invoked by the
#                          Rust tonic server for the `Call` RPC; the
#                          typed Read/Write/Delete/Ping handlers are
#                          pure-Rust in nexus_runtime::grpc_server).
#   nexus.grpc.server    — Lifespan glue: boots `nexus_runtime.start_vfs_grpc_server`
#                          and stores the handle for FastAPI shutdown.
