//! Compile the workspace's `nexus.search.v1` proto SSOT
//! (`../proto/nexus/search/v1/search.proto`) into `OUT_DIR` as a
//! CLIENT stub for `SearchServiceClient`.  The sibling
//! `nexus-search-plugin` crate compiles the same proto with
//! `build_server(true) + build_client(true)` for its cdylib; this
//! crate only needs the client side (we call INTO search-plugin,
//! we do not implement the service).
//!
//! Same tool stack the sibling `nexus-search-plugin/build.rs` uses.
//! Vendored `protoc` so the build stays hermetic on hosts without a
//! system-wide protobuf-compiler install.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto = "../proto/nexus/search/v1/search.proto";
    println!("cargo:rerun-if-changed={}", proto);

    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }

    // Both server + client stubs are generated: production code
    // (`handlers::search`) only calls the CLIENT to dial upstream
    // `nexus-search-plugin`, but the crate's integration tests
    // (`tests/glob_e2e.rs`) implement a mock SearchService using the
    // server stub so the full axum→tonic-client→grpc-server chain is
    // exercised end-to-end.  The server code is dead-stripped from
    // release builds by LTO — same pattern the sibling
    // `nexus-search-plugin` build.rs uses (which needs the server
    // for production and the client for its dylib self-tests).
    tonic_prost_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&[proto], &["../proto"])?;

    Ok(())
}
