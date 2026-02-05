//! Build script for nexus_raft.
//!
//! This script compiles protobuf files from the project-root proto/ directory.
//! All proto files are centralized there for SSOT (Single Source of Truth).
//!
//! Proto structure:
//!   proto/nexus/core/metadata.proto  - FileMetadata (shared with Python)
//!   proto/nexus/raft/transport.proto - Raft gRPC service
//!   proto/nexus/raft/commands.proto  - Raft state machine commands

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Only compile protos when grpc feature is enabled
    #[cfg(feature = "grpc")]
    {
        // Proto files are in project root's proto/ directory (SSOT)
        let proto_root = "../../proto";

        // First compile core/metadata.proto separately
        let core_protos = &[format!("{}/nexus/core/metadata.proto", proto_root)];
        let includes = &[proto_root];

        tonic_build::configure()
            .build_server(false)
            .build_client(false)
            .out_dir(std::env::var("OUT_DIR")?)
            .compile_protos(core_protos, includes)?;

        // Then compile raft protos, mapping nexus.core to the generated module
        let raft_protos = &[
            format!("{}/nexus/raft/transport.proto", proto_root),
            format!("{}/nexus/raft/commands.proto", proto_root),
        ];

        tonic_build::configure()
            .build_server(true)
            .build_client(true)
            // Map nexus.core.FileMetadata to our generated core module
            .extern_path(".nexus.core", "crate::transport::proto::nexus::core")
            .out_dir(std::env::var("OUT_DIR")?)
            .compile_protos(raft_protos, includes)?;

        // Tell cargo to recompile if protos change
        println!(
            "cargo:rerun-if-changed={}/nexus/raft/transport.proto",
            proto_root
        );
        println!(
            "cargo:rerun-if-changed={}/nexus/raft/commands.proto",
            proto_root
        );
        println!(
            "cargo:rerun-if-changed={}/nexus/core/metadata.proto",
            proto_root
        );
    }

    Ok(())
}
