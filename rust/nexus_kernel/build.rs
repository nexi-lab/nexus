fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Compile object_store.proto → Rust types + gRPC client/server stubs
    tonic_build::configure()
        .build_server(false) // Only client needed in Rust kernel
        .compile_protos(
            &["../../proto/nexus/storage/object_store.proto"],
            &["../../proto"],
        )?;
    Ok(())
}
