fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto = "../proto/nexus/accounts/v1/accounts.proto";
    println!("cargo:rerun-if-changed={proto}");
    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }
    tonic_prost_build::configure()
        .type_attribute(".", "#[derive(serde::Serialize, serde::Deserialize)]")
        .type_attribute(".", "#[serde(deny_unknown_fields)]")
        .field_attribute(
            "nexus.accounts.v1.StartGoogleLoginResponse.browser_binding",
            "#[serde(skip)]",
        )
        .field_attribute(
            "nexus.accounts.v1.FinishGoogleLoginRequest.browser_binding",
            "#[serde(skip)]",
        )
        .trait_attribute("AccountsService", "#[allow(clippy::double_must_use)]")
        .compile_protos(&[proto], &["../proto"])?;
    Ok(())
}
