//! Compile SearchService proto (`../proto/nexus/search/v1/search.proto`)
//! into `OUT_DIR` for the plugin to include! at build time.
//!
//! Same shape as the sibling `nexus-vault` and `nexus-fuse-plugin`
//! per-crate build scripts — vendored `protoc` so the plugin builds
//! on hosts without a system-wide protobuf-compiler install.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto = "../proto/nexus/search/v1/search.proto";
    println!("cargo:rerun-if-changed={}", proto);

    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }

    tonic_prost_build::configure()
        .build_server(true)
        // Client stubs so the crate's own dylib E2E tests can dial
        // themselves through the same tonic surface real cluster
        // callers use.  Dead code in production plugin builds — the
        // linker strips it via the cdylib LTO pass.
        .build_client(true)
        .compile_protos(&[proto], &["../proto"])?;

    Ok(())
}
