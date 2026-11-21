//! Linux dlopen regression test for SEV nexus-vfs#45.
//!
//! Builds in the same workflow as the cdylib (`cargo test --release`
//! after `cargo build --release`), uses `libloading` to `dlopen` the
//! just-built shared object, resolves every manifest symbol the kernel
//! loader needs, and validates the plugin contract:
//!
//! - `nexus_plugin_api_version` returns `PLUGIN_API_VERSION`
//! - `nexus_plugin_kind` returns `PluginKind::Service`
//! - `nexus_plugin_name` returns the literal `"password-vault"`
//! - All three `nexus_service_{create,dispatch,destroy}` symbols resolve
//!
//! The existing `vault-plugin-build.yml` workflow only `nm`-checks symbol
//! presence in the binary — that catches a missing `#[no_mangle]` but
//! cannot catch a real runtime `dlopen` regression (missing transitive
//! library, ABI version mismatch, symbol visibility drift). This test
//! closes that gap so a Linux load break surfaces at PR time on all
//! three platforms `libloading` runs on.

use std::ffi::CStr;
use std::path::PathBuf;
use std::process::Command;

use nexus_plugin_abi::{symbols, PluginKind, PLUGIN_API_VERSION};

#[cfg(target_os = "linux")]
const PLUGIN_FILE: &str = "libnexus_vault.so";
#[cfg(target_os = "macos")]
const PLUGIN_FILE: &str = "libnexus_vault.dylib";
#[cfg(target_os = "windows")]
const PLUGIN_FILE: &str = "nexus_vault.dll";

fn build_plugin() -> PathBuf {
    let mut cmd = Command::new(env!("CARGO"));
    cmd.args([
        "build",
        "--locked",
        "-p",
        "nexus-vault",
        "--lib",
        "--message-format=json",
    ]);
    if !cfg!(debug_assertions) {
        cmd.arg("--release");
    }
    let output = cmd.output().expect("build vault plugin");
    assert!(
        output.status.success(),
        "cargo build failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    for line in String::from_utf8(output.stdout)
        .expect("Cargo JSON output")
        .lines()
    {
        let event: serde_json::Value = serde_json::from_str(line).expect("Cargo build event");
        if event["reason"] == "compiler-artifact" && event["target"]["name"] == "nexus_vault" {
            for file in event["filenames"].as_array().expect("artifact filenames") {
                let path = PathBuf::from(file.as_str().expect("artifact path"));
                if path.ends_with(PLUGIN_FILE) {
                    return path;
                }
            }
        }
    }
    panic!("cargo build did not report the vault cdylib");
}

#[test]
fn vault_cdylib_dlopens_with_required_symbols() {
    let plugin = build_plugin();

    let lib = unsafe { libloading::Library::new(&plugin) }
        .unwrap_or_else(|e| panic!("dlopen({}) failed: {e}", plugin.display()));

    unsafe {
        let api_version: libloading::Symbol<unsafe extern "C" fn() -> u32> = lib
            .get(symbols::API_VERSION.as_bytes())
            .unwrap_or_else(|e| panic!("resolve {}: {e}", symbols::API_VERSION));
        assert_eq!(
            api_version(),
            PLUGIN_API_VERSION,
            "plugin reports api_version != PLUGIN_API_VERSION ({PLUGIN_API_VERSION})"
        );

        let kind: libloading::Symbol<unsafe extern "C" fn() -> u32> = lib
            .get(symbols::KIND.as_bytes())
            .unwrap_or_else(|e| panic!("resolve {}: {e}", symbols::KIND));
        assert_eq!(
            PluginKind::from_raw(kind()),
            Some(PluginKind::Service),
            "vault should declare PluginKind::Service"
        );

        let name_fn: libloading::Symbol<unsafe extern "C" fn() -> *const std::ffi::c_char> = lib
            .get(symbols::NAME.as_bytes())
            .unwrap_or_else(|e| panic!("resolve {}: {e}", symbols::NAME));
        let name = CStr::from_ptr(name_fn())
            .to_str()
            .expect("plugin name UTF-8");
        assert_eq!(name, "password-vault");

        // Lifecycle symbols the kernel loader resolves at registration
        // time. A missing one would surface as the SEV symptom (plugin
        // "found" by extension scan but rejected at load).
        for sym in [
            symbols::SERVICE_CREATE,
            symbols::SERVICE_DISPATCH,
            symbols::SERVICE_GRPC_DISPATCH,
            symbols::SERVICE_DESTROY,
        ] {
            let _: libloading::Symbol<unsafe extern "C" fn()> = lib
                .get(sym.as_bytes())
                .unwrap_or_else(|e| panic!("resolve {sym}: {e}"));
        }
    }
}
