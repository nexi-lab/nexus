#![cfg(target_os = "linux")]

use nexus_fuse::client::FileMetadata;
use nexus_fuse::passthrough::{
    kernel_release_supports_passthrough, linux_passthrough_supported, OpenAccess,
    PassthroughConfig, PassthroughDecision, PassthroughPolicy,
};
use std::process::Command;

#[test]
fn passthrough_kernel_support_probe_uses_project_parser() {
    let output = Command::new("uname").arg("-r").output().expect("uname");
    assert!(output.status.success());

    let release = String::from_utf8(output.stdout).expect("utf8 uname release");
    assert_eq!(
        linux_passthrough_supported(),
        kernel_release_supports_passthrough(release.trim())
    );
}

#[test]
fn passthrough_policy_handles_optional_and_required_configs_without_fuse() {
    let metadata = FileMetadata {
        size: 1024 * 1024,
        gen: 1,
        etag: Some("etag-1".to_string()),
        modified_at: None,
        is_directory: false,
    };

    for require in [false, true] {
        let policy = PassthroughPolicy::new(PassthroughConfig {
            enabled: true,
            allow_patterns: vec!["/data/**".to_string()],
            deny_patterns: vec![],
            threshold_bytes: 128 * 1024,
            require,
            backing_dir: None,
        })
        .expect("passthrough policy");

        assert_eq!(policy.config().require, require);
        assert_eq!(
            policy.decide("/data/one-gib.bin", &metadata, OpenAccess::ReadOnly),
            PassthroughDecision::Allow
        );
    }
}
