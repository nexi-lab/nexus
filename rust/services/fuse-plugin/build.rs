fn main() {
    let target_os =
        std::env::var("CARGO_CFG_TARGET_OS").expect("CARGO_CFG_TARGET_OS should be set");

    // Embed rpath entries so the dylib can find libfuse3 at runtime on
    // macOS regardless of whether FUSE-T was installed via .pkg (ships
    // to /Library/Application Support/fuse-t/lib/) or brew (ships to
    // /usr/local/lib/).  Both paths are harmless when the other is the
    // actual install — dyld silently skips non-existent rpath entries.
    if target_os == "macos" {
        println!("cargo:rustc-cdylib-link-arg=-Wl,-rpath,/Library/Application Support/fuse-t/lib");
        println!("cargo:rustc-cdylib-link-arg=-Wl,-rpath,/usr/local/lib");
    }
}
