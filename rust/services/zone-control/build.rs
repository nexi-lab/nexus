use serde_json::{Map, Value};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

fn read(path: &Path) -> Value {
    println!("cargo:rerun-if-changed={}", path.display());
    serde_json::from_slice(&std::fs::read(path).expect("read canonical contract"))
        .expect("parse canonical contract")
}

fn main() {
    let root = PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap()).join("../../..");
    let manifest = read(&root.join("contracts/manifests/zone-v1.manifest.gen.json"));
    let mut schemas = Map::new();
    for entry in manifest["schemas"]
        .as_array()
        .expect("contract manifest schemas")
    {
        let path = entry["schema_path"]
            .as_str()
            .expect("canonical schema path");
        let key = path
            .strip_prefix("contracts/")
            .expect("owned contract path");
        assert!(schemas
            .insert(key.to_owned(), read(&root.join(path)))
            .is_none());
    }
    let vendor = root.join("contracts/vendor/nexus-vfs.gen");
    println!("cargo:rerun-if-changed={}", vendor.display());
    for entry in std::fs::read_dir(&vendor).expect("vendored owner projections") {
        let path = entry.unwrap().path();
        if path
            .extension()
            .is_some_and(|extension| extension == "json")
        {
            let key = format!(
                "vendor/nexus-vfs.gen/{}",
                path.file_name().unwrap().to_str().unwrap()
            );
            assert!(schemas.insert(key, read(&path)).is_none());
        }
    }
    let mut ids = BTreeSet::new();
    for schema in schemas.values() {
        let id = schema["$id"].as_str().expect("canonical schema identity");
        assert!(ids.insert(id), "duplicate canonical schema identity: {id}");
    }
    let destination =
        PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("zone-contracts.json");
    std::fs::write(destination, serde_json::to_vec(&schemas).unwrap()).expect("embed contracts");
}
