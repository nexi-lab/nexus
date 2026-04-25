fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Point tonic_build (→ prost-build) at the vendored protoc binary so the
    // crate builds without a system-wide protobuf-compiler. Respect an
    // externally-set PROTOC if the caller already chose one.
    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }

    // Compile vfs.proto for both client (inter-node ReadBlob, REMOTE profile)
    // and server (Rust-native VfsGrpcServer that owns :2028 — replaces the
    // Python `grpc.aio.server` for the typed Read/Write/Delete/Ping path).
    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&["../../proto/nexus/grpc/vfs/vfs.proto"], &["../../proto"])?;
    Ok(())
}
