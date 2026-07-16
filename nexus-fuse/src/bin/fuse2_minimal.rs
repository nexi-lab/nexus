use fuser::{Filesystem, MountOption};

struct EmptyFs;
impl Filesystem for EmptyFs {}

fn main() {
    let mountpoint = std::env::args().nth(1).unwrap_or_else(|| "/tmp/fuse2-test".to_string());
    eprintln!("Attempting libfuse2 mount at {mountpoint}...");
    match fuser::spawn_mount2(EmptyFs, &mountpoint, &fuser::Config::default()) {
        Ok(session) => {
            eprintln!("MOUNT OK — libfuse2 works with FUSE-T!");
            eprintln!("Sleeping 10s then unmounting...");
            std::thread::sleep(std::time::Duration::from_secs(10));
            drop(session);
            eprintln!("Unmounted.");
        }
        Err(e) => {
            eprintln!("MOUNT FAILED: {e}");
        }
    }
}
