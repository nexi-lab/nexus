//! Build script for `services`.
//!
//! Per-service proto codegen, gated by the same feature flags that
//! gate the corresponding `pub mod` in `lib.rs`. Default builds (no
//! service features) skip codegen entirely.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(feature = "service-password-vault")]
    compile_password_vault_proto()?;

    #[cfg(feature = "service-generic-secrets")]
    compile_secrets_proto()?;

    Ok(())
}

#[cfg(feature = "service-password-vault")]
fn compile_password_vault_proto() -> Result<(), Box<dyn std::error::Error>> {
    let proto = "proto/nexus/password_vault/v1/password_vault.proto";
    println!("cargo:rerun-if-changed={}", proto);

    // Vendored protoc — no system-wide protobuf-compiler required.
    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }

    tonic_prost_build::configure()
        .build_server(true)
        // Rust clients are required by `vault`'s gRPC E2E test
        // (`rust/services/vault/tests/grpc_e2e.rs`). Production
        // callers remain TS / Python in sudowork + password-agent;
        // the Rust client stubs are dead code in any normal build.
        .build_client(true)
        .compile_protos(&[proto], &["proto"])?;

    Ok(())
}

#[cfg(feature = "service-generic-secrets")]
fn compile_secrets_proto() -> Result<(), Box<dyn std::error::Error>> {
    let proto = "proto/nexus/secrets/v1/secrets.proto";
    println!("cargo:rerun-if-changed={}", proto);

    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }

    tonic_prost_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&[proto], &["proto"])?;

    Ok(())
}
