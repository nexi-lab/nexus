//! `dlopen` regression test for `nexus-search-plugin`.
//!
//! Cargo builds the crate as an rlib for the test binary, but the
//! kernel loader dlopens the cdylib.  This test builds the cdylib
//! explicitly, opens it via `libloading`, resolves every plugin-ABI
//! manifest symbol the loader needs, and validates:
//!
//!   * `nexus_plugin_api_version` returns `PLUGIN_API_VERSION`
//!   * `nexus_plugin_kind` reports `PluginKind::Service`
//!   * `nexus_plugin_name` reports the literal `"search"`
//!   * `nexus_service_{create,dispatch,destroy}` are exported
//!   * `nexus_plugin_grpc_services` (Phase P opt-in) returns the
//!     `["nexus.search.v1.SearchService"]` JSON contract
//!
//! Mirrors `nexus-vault/tests/dlopen_self.rs`; both symbol lists
//! and both file layouts are the plugin-ABI's stable surface.
//! Adding a new required symbol on the loader side without updating
//! this test would surface at PR time on all three
//! `libloading`-supported platforms.

use std::ffi::CStr;
use std::path::PathBuf;
use std::process::Command;

use nexus_plugin_abi::{symbols, PluginKind, PLUGIN_API_VERSION};

#[cfg(target_os = "linux")]
const PLUGIN_FILE: &str = "libnexus_search_plugin.so";
#[cfg(target_os = "macos")]
const PLUGIN_FILE: &str = "libnexus_search_plugin.dylib";
#[cfg(target_os = "windows")]
const PLUGIN_FILE: &str = "nexus_search_plugin.dll";

fn plugin_path() -> PathBuf {
    // `current_exe()` returns the test binary path:
    //   target/{debug,release}/deps/dlopen_self-<hash>(.exe)
    // The cdylib lives next to the profile dir:
    //   target/{debug,release}/<PLUGIN_FILE>
    let exe = std::env::current_exe().expect("current_exe");
    let target_profile_dir = exe
        .parent()
        .and_then(|p| p.parent())
        .unwrap_or_else(|| panic!("unexpected test binary layout: {}", exe.display()));
    target_profile_dir.join(PLUGIN_FILE)
}

/// `cargo test` builds the test binary against the rlib; workspace-
/// wide runs don't necessarily emit the cdylib.  Force the build so
/// the test is self-contained regardless of invocation shape.
fn ensure_cdylib_built(plugin: &std::path::Path) {
    if plugin.exists() {
        return;
    }
    let mut cmd = Command::new(env!("CARGO"));
    cmd.args(["build", "-p", "nexus-search-plugin"]);
    if !cfg!(debug_assertions) {
        cmd.arg("--release");
    }
    let status = cmd.status().expect("invoke cargo");
    assert!(
        status.success(),
        "cargo build -p nexus-search-plugin failed"
    );
    assert!(
        plugin.exists(),
        "cdylib still not at {} after cargo build — check [lib] crate-type includes \"cdylib\"",
        plugin.display()
    );
}

#[test]
fn search_cdylib_dlopens_with_required_symbols() {
    let plugin = plugin_path();
    ensure_cdylib_built(&plugin);

    let lib = unsafe { libloading::Library::new(&plugin) }
        .unwrap_or_else(|e| panic!("dlopen({}) failed: {e}", plugin.display()));

    unsafe {
        // API version — a version drift would mean the plugin
        // compiled against a nexus-vfs whose ABI the running
        // cluster no longer accepts.
        let api_version: libloading::Symbol<unsafe extern "C" fn() -> u32> = lib
            .get(symbols::API_VERSION.as_bytes())
            .unwrap_or_else(|e| panic!("resolve {}: {e}", symbols::API_VERSION));
        assert_eq!(
            api_version(),
            PLUGIN_API_VERSION,
            "plugin reports api_version != PLUGIN_API_VERSION ({PLUGIN_API_VERSION})",
        );

        // Kind — must be Service (search is not a Driver).
        let kind: libloading::Symbol<unsafe extern "C" fn() -> u32> = lib
            .get(symbols::KIND.as_bytes())
            .unwrap_or_else(|e| panic!("resolve {}: {e}", symbols::KIND));
        assert_eq!(
            PluginKind::from_raw(kind()),
            Some(PluginKind::Service),
            "search-plugin should declare PluginKind::Service",
        );

        // Name — must match what `declare_service_plugin!` was
        // given.  Loader indexes plugins by this string, so a
        // silent rename would strand mount specs pointing at the
        // old name.
        let name_fn: libloading::Symbol<unsafe extern "C" fn() -> *const std::ffi::c_char> = lib
            .get(symbols::NAME.as_bytes())
            .unwrap_or_else(|e| panic!("resolve {}: {e}", symbols::NAME));
        let name = CStr::from_ptr(name_fn())
            .to_str()
            .expect("plugin name UTF-8");
        assert_eq!(name, "search");

        // Service lifecycle triplet — a missing entry surfaces as
        // "plugin found but rejected at load" (the SEV shape from
        // nexus-vfs#45).
        for sym in [
            symbols::SERVICE_CREATE,
            symbols::SERVICE_DISPATCH,
            symbols::SERVICE_DESTROY,
        ] {
            let _: libloading::Symbol<unsafe extern "C" fn()> = lib
                .get(sym.as_bytes())
                .unwrap_or_else(|e| panic!("resolve {sym}: {e}"));
        }

        // Phase P opt-in symbol — cluster loader dlsym's this to
        // learn which gRPC service full names to route into the
        // plugin's `nexus_service_dispatch`.  Return contract: a
        // null-terminated UTF-8 JSON array of service full names.
        let grpc_services: libloading::Symbol<unsafe extern "C" fn() -> *const std::ffi::c_char> =
            lib.get(symbols::SERVICE_GRPC_SERVICES.as_bytes())
                .unwrap_or_else(|e| panic!("resolve {}: {e}", symbols::SERVICE_GRPC_SERVICES));
        let services_json = CStr::from_ptr(grpc_services())
            .to_str()
            .expect("grpc_services JSON UTF-8");
        // Parse the JSON so a future refactor that ships malformed
        // JSON (missing quote, trailing comma) fails at PR time
        // instead of at cluster load time.
        let services: Vec<String> = serde_json::from_str(services_json)
            .unwrap_or_else(|e| panic!("grpc_services JSON parse: {e} — got {services_json:?}"));
        assert_eq!(
            services,
            vec!["nexus.search.v1.SearchService".to_string()],
            "plugin must advertise exactly the SearchService full name",
        );
    }
}
