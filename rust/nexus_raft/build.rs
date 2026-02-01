//! Build script for nexus_raft.
//!
//! This script compiles the protobuf files when the `grpc` feature is enabled.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Only compile protos when grpc feature is enabled
    #[cfg(feature = "grpc")]
    {
        let proto_files = &["proto/raft.proto"];
        let includes = &["proto"];

        // Compile protos to OUT_DIR (standard cargo location)
        tonic_build::configure()
            .build_server(true)
            .build_client(true)
            .compile_protos(proto_files, includes)?;

        // Tell cargo to recompile if protos change
        println!("cargo:rerun-if-changed=proto/raft.proto");
    }

    Ok(())
}
