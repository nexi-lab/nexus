//! Build script for `services`.
//!
//! Per-service proto codegen, gated by the same feature flags that
//! gate the corresponding `pub mod` in `lib.rs`. Default builds (no
//! service features) skip codegen entirely.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(feature = "service-password-vault")]
    compile_password_vault_proto()?;

    Ok(())
}

#[cfg(feature = "service-password-vault")]
fn compile_password_vault_proto() -> Result<(), Box<dyn std::error::Error>> {
    let proto = "../../proto/nexus/password_vault/v1/password_vault.proto";
    println!("cargo:rerun-if-changed={}", proto);

    // Vendored protoc — no system-wide protobuf-compiler required.
    // Mirrors the kernel + raft build.rs convention.
    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }

    tonic_prost_build::configure()
        .build_server(true)
        .build_client(false) // server-only; clients live in password-agent (Python) and sudowork (TS)
        .compile_protos(&[proto], &["../../proto"])?;

    Ok(())
}
