//! Integration test for `NexusNfs` — exercises the `NFSFileSystem` trait
//! implementation against a mock `KernelHandle` that stores files in an
//! in-memory HashMap.
//!
//! This verifies the full path: NFS method → kernel_callbacks wrapper →
//! C ABI callback → mock storage → response parsing, without needing
//! a real NFS mount (which requires OS-level privileges tested in CI).

#![cfg(target_os = "macos")]

use std::collections::HashMap;
use std::ffi::{c_char, c_void, CStr};
use std::sync::Mutex;

use nexus_plugin_abi::KernelHandle;

use nexus_fuse_plugin::fs_nfs::NexusNfs;
use nfsserve::nfs::{ftype3, nfsstat3};
use nfsserve::vfs::{NFSFileSystem, VFSCapabilities};

/// Helper: compare nfsstat3 variants (the crate doesn't derive PartialEq).
fn is_nfs_err(err: nfsstat3, expected: nfsstat3) -> bool {
    // Both are #[repr(u32)] — compare via discriminant.
    (err as u32) == (expected as u32)
}

/// Helper: compare ftype3 variants.
fn is_ftype(ft: ftype3, expected: ftype3) -> bool {
    (ft as u32) == (expected as u32)
}

// ── Mock in-memory kernel ──────────────────────────────────────────

/// A simple in-memory filesystem for testing.  Entries are either
/// directories (data=None) or files (data=Some(bytes)).
struct MockEntry {
    is_dir: bool,
    data: Vec<u8>,
}

struct MockKernel {
    entries: Mutex<HashMap<String, MockEntry>>,
}

impl MockKernel {
    fn new() -> Self {
        let mut entries = HashMap::new();
        entries.insert(
            "/".to_string(),
            MockEntry {
                is_dir: true,
                data: vec![],
            },
        );
        Self {
            entries: Mutex::new(entries),
        }
    }
}

// Per-test mock kernel.  Each test gets a fresh MockKernel stored in
// a Box, with the raw pointer passed as `kernel_ptr` in KernelHandle.
// The C ABI callbacks cast `kernel_ptr` back to `&MockKernel`.

fn new_mock_box() -> Box<MockKernel> {
    Box::new(MockKernel::new())
}

// ── C ABI callback implementations ────────────────────────────────

/// Helper: allocate a buffer on the heap and write it to out_buf/out_len.
/// The plugin calls `nexus_free` on this — but in tests we leak it
/// (the test process exits anyway). To match the real contract,
/// we use Box::into_raw.
unsafe fn write_out(out_buf: *mut *mut u8, out_len: *mut usize, data: &[u8]) {
    let boxed = data.to_vec().into_boxed_slice();
    *out_len = boxed.len();
    *out_buf = Box::into_raw(boxed) as *mut u8;
}

unsafe extern "C" fn mock_sys_stat(
    kernel: *const c_void,
    path: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let mock = &*(kernel as *const MockKernel);
    let path_str = CStr::from_ptr(path).to_str().unwrap();
    let entries = mock.entries.lock().unwrap();
    match entries.get(path_str) {
        Some(entry) => {
            let entry_type = if entry.is_dir { 1u64 } else { 0u64 };
            let json = format!(
                r#"{{"path":"{}","entry_type":{},"size":{},"zone_id":"root"}}"#,
                path_str,
                entry_type,
                entry.data.len()
            );
            write_out(out_buf, out_len, json.as_bytes());
            0
        }
        None => -1, // NotFound
    }
}

unsafe extern "C" fn mock_sys_read(
    kernel: *const c_void,
    path: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let mock = &*(kernel as *const MockKernel);
    let path_str = CStr::from_ptr(path).to_str().unwrap();
    let entries = mock.entries.lock().unwrap();
    match entries.get(path_str) {
        Some(entry) if !entry.is_dir => {
            write_out(out_buf, out_len, &entry.data);
            0
        }
        _ => -1,
    }
}

unsafe extern "C" fn mock_sys_write(
    kernel: *const c_void,
    path: *const c_char,
    data: *const u8,
    data_len: usize,
) -> i32 {
    let mock = &*(kernel as *const MockKernel);
    let path_str = CStr::from_ptr(path).to_str().unwrap();
    let content = std::slice::from_raw_parts(data, data_len).to_vec();
    let mut entries = mock.entries.lock().unwrap();
    entries.insert(
        path_str.to_string(),
        MockEntry {
            is_dir: false,
            data: content,
        },
    );
    0
}

unsafe extern "C" fn mock_sys_readdir(
    kernel: *const c_void,
    parent_path: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let mock = &*(kernel as *const MockKernel);
    let parent = CStr::from_ptr(parent_path).to_str().unwrap();
    let entries = mock.entries.lock().unwrap();
    if !entries.contains_key(parent) {
        return -1;
    }
    let prefix = if parent == "/" {
        "/".to_string()
    } else {
        format!("{}/", parent)
    };
    let mut children = Vec::new();
    for (path, entry) in entries.iter() {
        if path == parent {
            continue;
        }
        if let Some(name) = path.strip_prefix(&prefix) {
            if !name.contains('/') {
                let entry_type = if entry.is_dir { 1 } else { 0 };
                children.push(format!(
                    r#"{{"name":"{}","entry_type":{}}}"#,
                    name, entry_type
                ));
            }
        }
    }
    let json = format!("[{}]", children.join(","));
    write_out(out_buf, out_len, json.as_bytes());
    0
}

unsafe extern "C" fn mock_sys_unlink(
    kernel: *const c_void,
    path: *const c_char,
) -> i32 {
    let mock = &*(kernel as *const MockKernel);
    let path_str = CStr::from_ptr(path).to_str().unwrap();
    let mut entries = mock.entries.lock().unwrap();
    match entries.remove(path_str) {
        Some(_) => 0,
        None => -1,
    }
}

unsafe extern "C" fn mock_sys_mkdir(
    kernel: *const c_void,
    path: *const c_char,
) -> i32 {
    let mock = &*(kernel as *const MockKernel);
    let path_str = CStr::from_ptr(path).to_str().unwrap();
    let mut entries = mock.entries.lock().unwrap();
    if entries.contains_key(path_str) {
        return -3; // EEXIST
    }
    entries.insert(
        path_str.to_string(),
        MockEntry {
            is_dir: true,
            data: vec![],
        },
    );
    0
}

unsafe extern "C" fn mock_sys_rmdir(
    kernel: *const c_void,
    path: *const c_char,
) -> i32 {
    let mock = &*(kernel as *const MockKernel);
    let path_str = CStr::from_ptr(path).to_str().unwrap();
    let mut entries = mock.entries.lock().unwrap();
    match entries.remove(path_str) {
        Some(e) if e.is_dir => 0,
        Some(e) => {
            entries.insert(path_str.to_string(), e);
            -2
        }
        None => -1,
    }
}

unsafe extern "C" fn mock_sys_rename(
    kernel: *const c_void,
    old_path: *const c_char,
    new_path: *const c_char,
) -> i32 {
    let mock = &*(kernel as *const MockKernel);
    let old = CStr::from_ptr(old_path).to_str().unwrap();
    let new = CStr::from_ptr(new_path).to_str().unwrap();
    let mut entries = mock.entries.lock().unwrap();
    match entries.remove(old) {
        Some(entry) => {
            entries.insert(new.to_string(), entry);
            0
        }
        None => -1,
    }
}

unsafe extern "C" fn mock_sys_stat_batch(
    _kernel: *const c_void,
    _paths_json: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    // Stub — not used by NFS adapter.
    write_out(out_buf, out_len, b"[]");
    0
}

fn mock_kernel_handle(mock: &MockKernel) -> KernelHandle {
    KernelHandle {
        sys_read: mock_sys_read,
        sys_write: mock_sys_write,
        sys_stat: mock_sys_stat,
        sys_readdir: mock_sys_readdir,
        sys_unlink: mock_sys_unlink,
        sys_mkdir: mock_sys_mkdir,
        sys_rmdir: mock_sys_rmdir,
        sys_rename: mock_sys_rename,
        sys_stat_batch: mock_sys_stat_batch,
        kernel_ptr: mock as *const MockKernel as *const c_void,
    }
}

// ── Test helpers ───────────────────────────────────────────────────

/// Each test gets its own mock + NexusNfs so tests can run in parallel.
/// The Box<MockKernel> must be kept alive for the duration of the test.
fn make_nfs() -> (Box<MockKernel>, NexusNfs) {
    let mock = new_mock_box();
    let handle = mock_kernel_handle(&mock);
    let nfs = NexusNfs::new(handle, "/".to_string());
    (mock, nfs)
}

// ── Tests ──────────────────────────────────────────────────────────

#[tokio::test]
async fn root_dir_returns_1() {
    let (_mock, nfs) = make_nfs();
    assert_eq!(nfs.root_dir(), 1);
}

#[tokio::test]
async fn capabilities_returns_readwrite() {
    let (_mock, nfs) = make_nfs();
    assert!(matches!(nfs.capabilities(), VFSCapabilities::ReadWrite));
}

#[tokio::test]
async fn getattr_root_is_directory() {
    let (_mock, nfs) = make_nfs();
    let attr = nfs.getattr(nfs.root_dir()).await.unwrap();
    assert!(is_ftype(attr.ftype, ftype3::NF3DIR));
    assert_eq!(attr.fileid, 1);
}

#[tokio::test]
async fn lookup_nonexistent_returns_noent() {
    let (_mock, nfs) = make_nfs();
    let err = nfs
        .lookup(nfs.root_dir(), &"nonexistent".as_bytes().into())
        .await
        .unwrap_err();
    assert!(is_nfs_err(err, nfsstat3::NFS3ERR_NOENT));
}

#[tokio::test]
async fn create_and_read_file() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();
    let filename: nfsserve::nfs::filename3 = "hello.txt".as_bytes().into();

    // Create.
    let (fid, attr) = nfs
        .create(root, &filename, Default::default())
        .await
        .unwrap();
    assert!(fid > 0);
    assert!(is_ftype(attr.ftype, ftype3::NF3REG));
    assert_eq!(attr.size, 0); // empty file

    // Write content.
    let content = b"hello world";
    let write_attr = nfs.write(fid, 0, content).await.unwrap();
    assert_eq!(write_attr.size, content.len() as u64);

    // Read back.
    let (data, eof) = nfs.read(fid, 0, 1024).await.unwrap();
    assert_eq!(data, content);
    assert!(eof);

    // Partial read.
    let (data, eof) = nfs.read(fid, 6, 5).await.unwrap();
    assert_eq!(data, b"world");
    assert!(eof);
}

#[tokio::test]
async fn mkdir_and_readdir() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();

    // Create a subdirectory.
    let dirname: nfsserve::nfs::filename3 = "subdir".as_bytes().into();
    let (dir_id, dir_attr) = nfs.mkdir(root, &dirname).await.unwrap();
    assert!(dir_id > 0);
    assert!(is_ftype(dir_attr.ftype, ftype3::NF3DIR));

    // Create a file in root.
    let filename: nfsserve::nfs::filename3 = "file.txt".as_bytes().into();
    nfs.create(root, &filename, Default::default())
        .await
        .unwrap();

    // Readdir root — should contain both.
    let result = nfs.readdir(root, 0, 100).await.unwrap();
    let names: Vec<String> = result
        .entries
        .iter()
        .map(|e| String::from_utf8_lossy(&e.name).to_string())
        .collect();
    assert!(names.contains(&"subdir".to_string()), "missing subdir in {names:?}");
    assert!(names.contains(&"file.txt".to_string()), "missing file.txt in {names:?}");
    assert!(result.end);
}

#[tokio::test]
async fn lookup_returns_stable_inode() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();
    let filename: nfsserve::nfs::filename3 = "stable.txt".as_bytes().into();

    nfs.create(root, &filename, Default::default())
        .await
        .unwrap();

    let id1 = nfs.lookup(root, &filename).await.unwrap();
    let id2 = nfs.lookup(root, &filename).await.unwrap();
    assert_eq!(id1, id2, "lookup must return stable inode for same path");
}

#[tokio::test]
async fn rename_preserves_content() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();

    let old_name: nfsserve::nfs::filename3 = "old.txt".as_bytes().into();
    let new_name: nfsserve::nfs::filename3 = "new.txt".as_bytes().into();

    // Create and write.
    let (fid, _) = nfs
        .create(root, &old_name, Default::default())
        .await
        .unwrap();
    let content = b"rename me";
    nfs.write(fid, 0, content).await.unwrap();

    // Rename.
    nfs.rename(root, &old_name, root, &new_name).await.unwrap();

    // Old is gone.
    let err = nfs.lookup(root, &old_name).await.unwrap_err();
    assert!(is_nfs_err(err, nfsstat3::NFS3ERR_NOENT));

    // New has the content.
    let new_id = nfs.lookup(root, &new_name).await.unwrap();
    let (data, _) = nfs.read(new_id, 0, 1024).await.unwrap();
    assert_eq!(data, content);
}

#[tokio::test]
async fn remove_deletes_file() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();
    let filename: nfsserve::nfs::filename3 = "doomed.txt".as_bytes().into();

    nfs.create(root, &filename, Default::default())
        .await
        .unwrap();

    // File exists.
    nfs.lookup(root, &filename).await.unwrap();

    // Remove.
    nfs.remove(root, &filename).await.unwrap();

    // Gone.
    let err = nfs.lookup(root, &filename).await.unwrap_err();
    assert!(is_nfs_err(err, nfsstat3::NFS3ERR_NOENT));
}

#[tokio::test]
async fn create_exclusive_fails_on_existing() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();
    let filename: nfsserve::nfs::filename3 = "exists.txt".as_bytes().into();

    nfs.create(root, &filename, Default::default())
        .await
        .unwrap();

    let err = nfs.create_exclusive(root, &filename).await.unwrap_err();
    assert!(is_nfs_err(err, nfsstat3::NFS3ERR_EXIST));
}

#[tokio::test]
async fn readdir_pagination_with_start_after() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();

    // Create 5 files.
    for i in 0..5 {
        let name: nfsserve::nfs::filename3 =
            format!("file-{i}.txt").as_bytes().into();
        nfs.create(root, &name, Default::default()).await.unwrap();
    }

    // Read all.
    let all = nfs.readdir(root, 0, 100).await.unwrap();
    assert_eq!(all.entries.len(), 5);

    // Paginate: first 2.
    let page1 = nfs.readdir(root, 0, 2).await.unwrap();
    assert_eq!(page1.entries.len(), 2);
    assert!(!page1.end);

    // Next page starting after last entry of page1.
    let last_id = page1.entries.last().unwrap().fileid;
    let page2 = nfs.readdir(root, last_id, 100).await.unwrap();
    assert_eq!(page2.entries.len(), 3);
    assert!(page2.end);
}

#[tokio::test]
async fn symlink_and_readlink_unsupported() {
    let (_mock, nfs) = make_nfs();
    let err = nfs
        .symlink(
            nfs.root_dir(),
            &"link".as_bytes().into(),
            &"/target".as_bytes().into(),
            &Default::default(),
        )
        .await
        .unwrap_err();
    assert!(is_nfs_err(err, nfsstat3::NFS3ERR_NOENT));

    let err = nfs.readlink(999).await.unwrap_err();
    assert!(is_nfs_err(err, nfsstat3::NFS3ERR_NOENT));
}

#[tokio::test]
async fn write_at_nonzero_offset_returns_io() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();
    let filename: nfsserve::nfs::filename3 = "offset.txt".as_bytes().into();

    let (fid, _) = nfs
        .create(root, &filename, Default::default())
        .await
        .unwrap();

    let err = nfs.write(fid, 10, b"data").await.unwrap_err();
    assert!(is_nfs_err(err, nfsstat3::NFS3ERR_IO));
}

#[tokio::test]
async fn read_past_eof_returns_empty() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();
    let filename: nfsserve::nfs::filename3 = "small.txt".as_bytes().into();

    let (fid, _) = nfs
        .create(root, &filename, Default::default())
        .await
        .unwrap();
    nfs.write(fid, 0, b"hi").await.unwrap();

    let (data, eof) = nfs.read(fid, 100, 50).await.unwrap();
    assert!(data.is_empty());
    assert!(eof);
}

/// Workflow test: full session lifecycle mirroring the pytest E2E.
/// mkdir → write N files → readdir → read each → unlink each → rmdir.
#[tokio::test]
async fn workflow_session_lifecycle() {
    let (_mock, nfs) = make_nfs();
    let root = nfs.root_dir();

    // Step 1: mkdir.
    let sess_name: nfsserve::nfs::filename3 = "session".as_bytes().into();
    let (sess_id, _) = nfs.mkdir(root, &sess_name).await.unwrap();

    // Step 2: write 3 task files.
    let tasks = vec![
        ("task-001.json", r#"{"id":1,"title":"PR"}"#),
        ("task-002.json", r#"{"id":2,"title":"review"}"#),
        ("task-003.json", r#"{"id":3,"title":"smoke"}"#),
    ];
    let mut task_ids = Vec::new();
    for (name, payload) in &tasks {
        let fname: nfsserve::nfs::filename3 = name.as_bytes().into();
        let (fid, _) = nfs.create(sess_id, &fname, Default::default()).await.unwrap();
        nfs.write(fid, 0, payload.as_bytes()).await.unwrap();
        task_ids.push(fid);
    }

    // Step 3: readdir session — all 3 present.
    let listing = nfs.readdir(sess_id, 0, 100).await.unwrap();
    assert_eq!(listing.entries.len(), 3);

    // Step 4: read each back — byte exact.
    for (i, (_, payload)) in tasks.iter().enumerate() {
        let (data, _) = nfs.read(task_ids[i], 0, 4096).await.unwrap();
        assert_eq!(
            String::from_utf8_lossy(&data),
            *payload,
            "file {i} content mismatch"
        );
    }

    // Step 5: unlink each.
    for (name, _) in &tasks {
        let fname: nfsserve::nfs::filename3 = name.as_bytes().into();
        nfs.remove(sess_id, &fname).await.unwrap();
    }

    // Step 6: rmdir session.
    nfs.remove(root, &"session".as_bytes().into()).await.unwrap();
    let err = nfs.lookup(root, &sess_name).await.unwrap_err();
    assert!(is_nfs_err(err, nfsstat3::NFS3ERR_NOENT));
}
