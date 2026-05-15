use crate::client::{FileMetadata, NexusClient, ReadToWriterResponse};
use anyhow::{anyhow, Context};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::io::{AsRawFd, RawFd};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_TEMP_ID: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BackingKey {
    pub digest: String,
    pub server_url: String,
    pub path: String,
}

impl BackingKey {
    pub fn new(server_url: &str, path: &str, version_token: &str, size: u64) -> Self {
        let mut hasher = Sha256::new();
        hasher.update(server_url.as_bytes());
        hasher.update(b"\0");
        hasher.update(path.as_bytes());
        hasher.update(b"\0");
        hasher.update(version_token.as_bytes());
        hasher.update(b"\0");
        hasher.update(size.to_le_bytes());

        Self {
            digest: hex::encode(hasher.finalize()),
            server_url: server_url.to_string(),
            path: path.to_string(),
        }
    }

    pub fn filename(&self) -> String {
        format!("{}.backing", self.digest)
    }

    fn marker_prefix(&self) -> String {
        marker_prefix(&self.server_url, &self.path)
    }
}

pub struct MaterializedBacking {
    pub path: PathBuf,
    pub key: BackingKey,
    file: File,
}

impl MaterializedBacking {
    pub fn file(&self) -> &File {
        &self.file
    }

    pub fn raw_fd(&self) -> RawFd {
        self.file.as_raw_fd()
    }
}

pub struct BackingStore {
    root: PathBuf,
}

impl BackingStore {
    pub fn new(root: impl AsRef<Path>) -> anyhow::Result<Self> {
        fs::create_dir_all(root.as_ref()).with_context(|| {
            format!(
                "failed to create passthrough backing root {}",
                root.as_ref().display()
            )
        })?;

        Ok(Self {
            root: root.as_ref().to_path_buf(),
        })
    }

    pub fn path_for_key(&self, key: &BackingKey) -> PathBuf {
        self.root.join(key.filename())
    }

    pub fn materialize(
        &self,
        server_url: &str,
        path: &str,
        client: &NexusClient,
        metadata: &FileMetadata,
    ) -> anyhow::Result<MaterializedBacking> {
        let version_token = backing_version_token(metadata).ok_or_else(|| {
            anyhow!("cannot materialize passthrough backing without an etag or generation")
        })?;
        let key = BackingKey::new(server_url, path, &version_token, metadata.size);
        let final_path = self.path_for_key(&key);

        if final_path.exists() {
            if self.is_reusable_backing(&final_path, &key, metadata.size)? {
                return self.open_materialized(final_path, key);
            }
            self.remove_invalid_backing(&final_path, &key)?;
        }

        let (temp_path, mut temp_file) = self.create_temp_file(&key)?;
        let bytes_written = match client.read_with_etag_to_writer(path, None, &mut temp_file) {
            Ok(ReadToWriterResponse::Content { bytes_written, .. }) => bytes_written,
            Ok(ReadToWriterResponse::NotModified) => {
                let _ = fs::remove_file(&temp_path);
                return Err(anyhow!(
                    "cannot materialize passthrough backing from not-modified response"
                ));
            }
            Err(err) => {
                let _ = fs::remove_file(&temp_path);
                return Err(err.into());
            }
        };

        if bytes_written != metadata.size {
            let _ = fs::remove_file(&temp_path);
            return Err(anyhow!(
                "passthrough backing size mismatch for {}: metadata={}, read={}",
                path,
                metadata.size,
                bytes_written
            ));
        }

        if let Err(err) = temp_file.sync_all() {
            let _ = fs::remove_file(&temp_path);
            return Err(err)
                .with_context(|| format!("failed to sync temp backing {}", temp_path.display()));
        }
        drop(temp_file);

        if let Err(err) = fs::rename(&temp_path, &final_path) {
            let _ = fs::remove_file(&temp_path);
            return Err(err).with_context(|| {
                format!(
                    "failed to publish passthrough backing {}",
                    final_path.display()
                )
            });
        }
        if let Err(err) = self.write_marker(&key) {
            let _ = fs::remove_file(&final_path);
            return Err(err);
        }

        self.open_materialized(final_path, key)
    }

    pub fn invalidate_path(&self, server_url: &str, path: &str) -> anyhow::Result<()> {
        let prefix = marker_prefix(server_url, path);

        for entry in fs::read_dir(&self.root)? {
            let entry = entry?;
            let marker_path = entry.path();
            let Some(name) = marker_path.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            if !name.starts_with(&prefix) || !name.ends_with(".marker") {
                continue;
            }

            let backing_filename = fs::read_to_string(&marker_path)?;
            let backing_filename = backing_filename.trim();
            if !is_valid_backing_filename(backing_filename) {
                continue;
            }

            let backing_path = self.root.join(backing_filename);
            match fs::remove_file(&backing_path) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) => return Err(err.into()),
            }
            match fs::remove_file(&marker_path) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) => return Err(err.into()),
            }
        }

        Ok(())
    }

    fn open_materialized(
        &self,
        path: PathBuf,
        key: BackingKey,
    ) -> anyhow::Result<MaterializedBacking> {
        let file = File::open(&path)
            .with_context(|| format!("failed to open backing file {}", path.display()))?;
        Ok(MaterializedBacking { path, key, file })
    }

    fn write_marker(&self, key: &BackingKey) -> anyhow::Result<()> {
        let marker_path = self.marker_path_for(key);
        let mut marker = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&marker_path)
            .with_context(|| format!("failed to create marker {}", marker_path.display()))?;
        marker.write_all(key.filename().as_bytes())?;
        marker.sync_all()?;
        Ok(())
    }

    fn is_reusable_backing(
        &self,
        path: &Path,
        key: &BackingKey,
        expected_size: u64,
    ) -> anyhow::Result<bool> {
        let metadata = fs::metadata(path)
            .with_context(|| format!("failed to stat backing file {}", path.display()))?;
        if !metadata.is_file() || metadata.len() != expected_size {
            return Ok(false);
        }

        let marker_path = self.marker_path_for(key);
        let marker_contents = match fs::read_to_string(&marker_path) {
            Ok(contents) => contents,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(err) => {
                return Err(err).with_context(|| {
                    format!("failed to read backing marker {}", marker_path.display())
                });
            }
        };

        Ok(marker_contents.trim() == key.filename())
    }

    fn remove_invalid_backing(&self, path: &Path, key: &BackingKey) -> anyhow::Result<()> {
        match fs::remove_file(path) {
            Ok(()) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => {
                return Err(err).with_context(|| {
                    format!("failed to remove invalid backing {}", path.display())
                });
            }
        }

        let marker_path = self.marker_path_for(key);
        match fs::remove_file(&marker_path) {
            Ok(()) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => {
                return Err(err).with_context(|| {
                    format!(
                        "failed to remove invalid backing marker {}",
                        marker_path.display()
                    )
                });
            }
        }
        Ok(())
    }

    fn marker_path_for(&self, key: &BackingKey) -> PathBuf {
        self.root
            .join(format!("{}.{}.marker", key.marker_prefix(), key.digest))
    }

    fn create_temp_file(&self, key: &BackingKey) -> anyhow::Result<(PathBuf, File)> {
        for _ in 0..1024 {
            let temp_path = self.temp_path_for(key);
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temp_path)
            {
                Ok(file) => return Ok((temp_path, file)),
                Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(err) => {
                    return Err(err).with_context(|| {
                        format!("failed to create temp backing {}", temp_path.display())
                    });
                }
            }
        }

        Err(anyhow!(
            "failed to create unique temp backing for {} after retries",
            key.filename()
        ))
    }

    fn temp_path_for(&self, key: &BackingKey) -> PathBuf {
        let sequence = NEXT_TEMP_ID.fetch_add(1, Ordering::Relaxed);
        self.root.join(format!(
            ".{}.{}.{}.tmp",
            key.digest,
            std::process::id(),
            sequence
        ))
    }
}

fn marker_prefix(server_url: &str, path: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(server_url.as_bytes());
    hasher.update(b"\0");
    hasher.update(path.as_bytes());
    format!("marker-{}", hex::encode(hasher.finalize()))
}

fn backing_version_token(metadata: &FileMetadata) -> Option<String> {
    if let Some(etag) = metadata.etag.as_deref().filter(|etag| !etag.is_empty()) {
        return Some(format!("etag:{etag}"));
    }
    if metadata.gen != 0 {
        return Some(format!("gen:{}", metadata.gen));
    }
    None
}

fn is_valid_backing_filename(filename: &str) -> bool {
    filename.ends_with(".backing") && !filename.contains('/') && !filename.contains('\\')
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::{engine::general_purpose::STANDARD, Engine};
    use mockito::Server;
    use std::fs;
    use std::thread;

    #[test]
    fn backing_key_uses_stable_backing_filename() {
        let key = BackingKey::new("https://nexus.example", "/data/file.bin", "etag-1", 42);
        let same = BackingKey::new("https://nexus.example", "/data/file.bin", "etag-1", 42);
        let changed_etag = BackingKey::new("https://nexus.example", "/data/file.bin", "etag-2", 42);

        assert_eq!(key.filename(), same.filename());
        assert_ne!(key.filename(), changed_etag.filename());
        assert!(key.filename().ends_with(".backing"));
    }

    #[test]
    fn materialize_writes_backing_file_and_marker() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();
        let mut server = Server::new();
        let _mock = read_mock(&mut server, "/data/file.bin", b"content", 200);
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let metadata = metadata("etag-1", 7);

        let materialized = store
            .materialize(&server.url(), "/data/file.bin", &client, &metadata)
            .unwrap();

        assert_eq!(fs::read(&materialized.path).unwrap(), b"content");
        assert_eq!(
            fs::read_to_string(store.marker_path_for(&materialized.key)).unwrap(),
            materialized.key.filename()
        );
        assert_eq!(materialized.file().metadata().unwrap().len(), 7);
        assert!(materialized.raw_fd() >= 0);
    }

    #[test]
    fn materialize_reuses_valid_existing_backing_file() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();
        let mut server = Server::new();
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let metadata = metadata("etag-1", 7);
        let key = BackingKey::new(&server.url(), "/data/file.bin", "etag:etag-1", 7);
        let backing_path = store.path_for_key(&key);
        fs::write(&backing_path, b"cached!").unwrap();
        write_marker(&store, &key, &key.filename());
        let _unused_mock = server
            .mock("POST", "/api/nfs/read")
            .expect(0)
            .with_status(500)
            .create();

        let materialized = store
            .materialize(&server.url(), "/data/file.bin", &client, &metadata)
            .unwrap();

        assert_eq!(materialized.path, backing_path);
        assert_eq!(fs::read(&materialized.path).unwrap(), b"cached!");
    }

    #[test]
    fn materialize_replaces_existing_backing_with_wrong_size() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();
        let mut server = Server::new();
        let _mock = read_mock(&mut server, "/data/file.bin", b"correct", 200);
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let metadata = metadata("etag-1", 7);
        let key = BackingKey::new(&server.url(), "/data/file.bin", "etag:etag-1", 7);
        let backing_path = store.path_for_key(&key);
        fs::write(&backing_path, b"stale").unwrap();
        write_marker(&store, &key, &key.filename());

        let materialized = store
            .materialize(&server.url(), "/data/file.bin", &client, &metadata)
            .unwrap();

        assert_eq!(materialized.path, backing_path);
        assert_eq!(fs::read(&materialized.path).unwrap(), b"correct");
    }

    #[test]
    fn materialize_recovers_existing_backing_without_marker() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();
        let mut server = Server::new();
        let _mock = read_mock(&mut server, "/data/file.bin", b"fresh", 200);
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let metadata = metadata("etag-1", 5);
        let key = BackingKey::new(&server.url(), "/data/file.bin", "etag:etag-1", 5);
        fs::write(store.path_for_key(&key), b"stale").unwrap();

        let materialized = store
            .materialize(&server.url(), "/data/file.bin", &client, &metadata)
            .unwrap();

        assert_eq!(fs::read(&materialized.path).unwrap(), b"fresh");
        assert_eq!(
            fs::read_to_string(store.marker_path_for(&key)).unwrap(),
            key.filename()
        );
    }

    #[test]
    fn materialize_uses_generation_when_etag_is_missing() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();
        let mut server = Server::new();
        let _mock = read_mock(&mut server, "/data/no-etag.bin", b"gen-data", 200);
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let mut metadata = metadata("ignored", 8);
        metadata.etag = None;
        metadata.gen = 42;

        let materialized = store
            .materialize(&server.url(), "/data/no-etag.bin", &client, &metadata)
            .unwrap();

        let expected_key = BackingKey::new(&server.url(), "/data/no-etag.bin", "gen:42", 8);
        assert_eq!(materialized.path, store.path_for_key(&expected_key));
        assert_eq!(fs::read(&materialized.path).unwrap(), b"gen-data");
        assert_eq!(
            fs::read_to_string(store.marker_path_for(&expected_key)).unwrap(),
            expected_key.filename()
        );
    }

    #[test]
    fn materialize_rejects_missing_version_token() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();
        let server = Server::new();
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let mut metadata = metadata("ignored", 5);
        metadata.etag = None;
        metadata.gen = 0;

        let err = materialize_err(
            &store,
            &server.url(),
            "/data/no-version.bin",
            &client,
            &metadata,
        );

        assert!(err.to_string().contains("without an etag or generation"));
    }

    #[test]
    fn materialize_rejects_not_modified_response() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();
        let mut server = Server::new();
        let _mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(304)
            .create();
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();

        let err = materialize_err(
            &store,
            &server.url(),
            "/data/file.bin",
            &client,
            &metadata("etag-1", 5),
        );

        assert!(err.to_string().contains("not-modified"));
    }

    #[test]
    fn materialize_rejects_size_mismatch() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();
        let mut server = Server::new();
        let _mock = read_mock(&mut server, "/data/file.bin", b"short", 200);
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();

        let err = materialize_err(
            &store,
            &server.url(),
            "/data/file.bin",
            &client,
            &metadata("etag-1", 99),
        );

        assert!(err.to_string().contains("size mismatch"));
    }

    #[test]
    fn materialize_rolls_back_final_backing_when_marker_creation_fails() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();
        let mut server = Server::new();
        let _mock = read_mock(&mut server, "/data/file.bin", b"content", 200);
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let key = BackingKey::new(&server.url(), "/data/file.bin", "etag:etag-1", 7);
        fs::create_dir(store.marker_path_for(&key)).unwrap();

        let err = materialize_err(
            &store,
            &server.url(),
            "/data/file.bin",
            &client,
            &metadata("etag-1", 7),
        );

        assert!(err.to_string().contains("marker"));
        assert!(!store.path_for_key(&key).exists());
    }

    #[test]
    fn temp_paths_are_unique_across_threads() {
        let temp = tempfile::tempdir().unwrap();
        let store = std::sync::Arc::new(BackingStore::new(temp.path()).unwrap());
        let key = BackingKey::new("https://nexus.example", "/data/file.bin", "etag-1", 42);

        let mut handles = Vec::new();
        for _ in 0..32 {
            let store = store.clone();
            let key = key.clone();
            handles.push(thread::spawn(move || store.temp_path_for(&key)));
        }
        let mut paths = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect::<Vec<_>>();
        paths.sort();
        paths.dedup();

        assert_eq!(paths.len(), 32);
    }

    #[test]
    fn invalidate_path_removes_only_matching_path_backings() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();

        let key = BackingKey::new("https://nexus.example", "/data/file.bin", "etag-1", 42);
        let other_path_key =
            BackingKey::new("https://nexus.example", "/data/other.bin", "etag-1", 42);

        let backing_path = store.path_for_key(&key);
        let other_path = store.path_for_key(&other_path_key);
        fs::write(&backing_path, b"file").unwrap();
        fs::write(&other_path, b"other").unwrap();
        write_marker(&store, &key, &key.filename());
        write_marker(&store, &other_path_key, &other_path_key.filename());

        store
            .invalidate_path("https://nexus.example", "/data/file.bin")
            .unwrap();

        assert!(!backing_path.exists());
        assert!(other_path.exists());
    }

    #[test]
    fn invalidate_path_ignores_prefix_named_non_marker_files() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();

        let key = BackingKey::new("https://nexus.example", "/data/file.bin", "etag-1", 42);
        let backing_path = store.path_for_key(&key);
        fs::write(&backing_path, b"file").unwrap();
        fs::write(
            temp.path()
                .join(format!("{}.not-a-marker", key.marker_prefix())),
            key.filename(),
        )
        .unwrap();

        store
            .invalidate_path("https://nexus.example", "/data/file.bin")
            .unwrap();

        assert!(backing_path.exists());
    }

    #[test]
    fn invalidate_path_ignores_marker_contents_that_are_not_backing_filenames() {
        let temp = tempfile::tempdir().unwrap();
        let store = BackingStore::new(temp.path()).unwrap();

        let key = BackingKey::new("https://nexus.example", "/data/file.bin", "etag-1", 42);
        let arbitrary_path = temp.path().join("arbitrary.txt");
        fs::write(&arbitrary_path, b"keep").unwrap();
        write_marker(&store, &key, "arbitrary.txt");

        store
            .invalidate_path("https://nexus.example", "/data/file.bin")
            .unwrap();

        assert!(arbitrary_path.exists());
    }

    fn write_marker(store: &BackingStore, key: &BackingKey, contents: &str) {
        fs::write(store.marker_path_for(key), contents).unwrap();
    }

    fn materialize_err(
        store: &BackingStore,
        server_url: &str,
        path: &str,
        client: &NexusClient,
        metadata: &FileMetadata,
    ) -> anyhow::Error {
        match store.materialize(server_url, path, client, metadata) {
            Ok(_) => panic!("expected materialize to fail"),
            Err(err) => err,
        }
    }

    fn metadata(etag: &str, size: u64) -> FileMetadata {
        FileMetadata {
            size,
            gen: 1,
            etag: Some(etag.to_string()),
            modified_at: None,
            is_directory: false,
        }
    }

    fn read_mock(
        server: &mut Server,
        path: &'static str,
        content: &[u8],
        status: usize,
    ) -> mockito::Mock {
        let body = format!(
            r#"{{"jsonrpc":"2.0","id":1,"result":{{"__type__":"bytes","data":"{}"}}}}"#,
            STANDARD.encode(content)
        );
        server
            .mock("POST", "/api/nfs/read")
            .match_body(mockito::Matcher::Regex(format!(
                r#""path"\s*:\s*"{}""#,
                path
            )))
            .with_status(status)
            .with_header("content-type", "application/json")
            .with_body(body)
            .create()
    }
}
