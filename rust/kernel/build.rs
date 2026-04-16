fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Compile vfs.proto → client for inter-node ReadBlob (federation remote fetch).
    tonic_build::configure()
        .build_server(false)
        .compile_protos(&["../../proto/nexus/grpc/vfs/vfs.proto"], &["../../proto"])?;
    Ok(())
}
