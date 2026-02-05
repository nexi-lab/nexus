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
        let core_proto = format!("{}/nexus/core/metadata.proto", proto_root);

        // Skip proto compilation if proto files don't exist yet (Issue #1159)
        if !std::path::Path::new(&core_proto).exists() {
            println!("cargo:warning=Proto files not found at {}, skipping gRPC codegen. See Issue #1159.", proto_root);
            // Create empty stub files so include!() macros in transport/mod.rs don't fail
            let out_dir = std::env::var("OUT_DIR")?;
            std::fs::write(format!("{}/nexus.core.rs", out_dir), "// Proto stub - proto files not yet committed (Issue #1159)\n")?;
            std::fs::write(format!("{}/nexus.raft.rs", out_dir), "// Proto stub - proto files not yet committed (Issue #1159)\n")?;
            // Do NOT set has_protos cfg - transport client/server won't compile
            return Ok(());
        }

        // Signal that proto codegen succeeded - enables full transport module
        println!("cargo:rustc-cfg=has_protos");

        // First compile core/metadata.proto separately
        let core_protos = &[core_proto];
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
