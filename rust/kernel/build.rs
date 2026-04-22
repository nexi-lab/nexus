fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Point tonic_build (→ prost-build) at the vendored protoc binary so the
    // crate builds without a system-wide protobuf-compiler. Respect an
    // externally-set PROTOC if the caller already chose one.
    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }

    // Compile vfs.proto → client for inter-node ReadBlob (federation remote fetch).
    tonic_build::configure()
        .build_server(false)
        .compile_protos(&["../../proto/nexus/grpc/vfs/vfs.proto"], &["../../proto"])?;
    Ok(())
}
