use crate::client::FileMetadata;
use crate::passthrough::config::{PassthroughConfig, PatternSet};
use fuser::{OpenAccMode, OpenFlags};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OpenAccess {
    ReadOnly,
    WriteOnly,
    ReadWrite,
}

impl OpenAccess {
    pub fn from_open_flags(flags: OpenFlags) -> Self {
        match flags.acc_mode() {
            OpenAccMode::O_RDONLY => Self::ReadOnly,
            OpenAccMode::O_WRONLY => Self::WriteOnly,
            OpenAccMode::O_RDWR => Self::ReadWrite,
        }
    }

    fn is_read_only(self) -> bool {
        matches!(self, Self::ReadOnly)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DenyReason {
    Disabled,
    NotNegotiated,
    Pattern,
    Directory,
    BelowThreshold,
    NotReadOnly,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PassthroughDecision {
    Allow,
    Deny(DenyReason),
}

pub struct PassthroughPolicy {
    config: PassthroughConfig,
    patterns: PatternSet,
}

impl PassthroughPolicy {
    pub fn new(config: PassthroughConfig) -> Result<Self, globset::Error> {
        let patterns = config.pattern_set()?;
        Ok(Self { config, patterns })
    }

    pub fn config(&self) -> &PassthroughConfig {
        &self.config
    }

    pub fn decide(
        &self,
        path: &str,
        metadata: &FileMetadata,
        access: OpenAccess,
    ) -> PassthroughDecision {
        if !self.config.enabled {
            return PassthroughDecision::Deny(DenyReason::Disabled);
        }

        if metadata.is_directory {
            return PassthroughDecision::Deny(DenyReason::Directory);
        }

        if !access.is_read_only() {
            return PassthroughDecision::Deny(DenyReason::NotReadOnly);
        }

        if metadata.size < self.config.threshold_bytes {
            return PassthroughDecision::Deny(DenyReason::BelowThreshold);
        }

        if !self.patterns.allows(path) {
            return PassthroughDecision::Deny(DenyReason::Pattern);
        }

        PassthroughDecision::Allow
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::FileMetadata;
    use crate::passthrough::config::PassthroughConfig;
    use fuser::OpenFlags;

    #[test]
    fn disabled_config_denies_passthrough() {
        let policy = PassthroughPolicy::new(PassthroughConfig::disabled()).unwrap();

        assert_eq!(
            policy.decide(
                "/cache/file.bin",
                &metadata(false, 256 * 1024),
                OpenAccess::ReadOnly,
            ),
            PassthroughDecision::Deny(DenyReason::Disabled)
        );
    }

    #[test]
    fn large_read_only_file_matching_allow_pattern_is_allowed() {
        let config = PassthroughConfig {
            enabled: true,
            allow_patterns: vec!["/cache/**".to_string()],
            ..Default::default()
        };
        let policy = PassthroughPolicy::new(config).unwrap();

        assert!(policy.config().enabled);
        assert_eq!(
            policy.decide(
                "/cache/file.bin",
                &metadata(false, 256 * 1024),
                OpenAccess::ReadOnly,
            ),
            PassthroughDecision::Allow
        );
    }

    #[test]
    fn pattern_mismatch_is_denied() {
        let policy = PassthroughPolicy::new(enabled_config()).unwrap();

        assert_eq!(
            policy.decide(
                "/other/file.bin",
                &metadata(false, 256 * 1024),
                OpenAccess::ReadOnly,
            ),
            PassthroughDecision::Deny(DenyReason::Pattern)
        );
    }

    #[test]
    fn directories_are_denied() {
        let policy = PassthroughPolicy::new(enabled_config()).unwrap();

        assert_eq!(
            policy.decide("/cache", &metadata(true, 256 * 1024), OpenAccess::ReadOnly,),
            PassthroughDecision::Deny(DenyReason::Directory)
        );
    }

    #[test]
    fn files_below_threshold_are_denied() {
        let policy = PassthroughPolicy::new(enabled_config()).unwrap();

        assert_eq!(
            policy.decide(
                "/cache/small.bin",
                &metadata(false, 127 * 1024),
                OpenAccess::ReadOnly,
            ),
            PassthroughDecision::Deny(DenyReason::BelowThreshold)
        );
    }

    #[test]
    fn write_opens_are_denied() {
        let policy = PassthroughPolicy::new(enabled_config()).unwrap();

        assert_eq!(
            policy.decide(
                "/cache/file.bin",
                &metadata(false, 256 * 1024),
                OpenAccess::from_open_flags(OpenFlags(libc::O_RDWR)),
            ),
            PassthroughDecision::Deny(DenyReason::NotReadOnly)
        );
    }

    #[test]
    fn open_access_maps_fuser_access_modes() {
        assert_eq!(
            OpenAccess::from_open_flags(OpenFlags(libc::O_RDONLY)),
            OpenAccess::ReadOnly
        );
        assert_eq!(
            OpenAccess::from_open_flags(OpenFlags(libc::O_WRONLY)),
            OpenAccess::WriteOnly
        );
        assert_eq!(
            OpenAccess::from_open_flags(OpenFlags(libc::O_RDWR)),
            OpenAccess::ReadWrite
        );
    }

    fn enabled_config() -> PassthroughConfig {
        PassthroughConfig {
            enabled: true,
            allow_patterns: vec!["/cache/**".to_string()],
            ..Default::default()
        }
    }

    fn metadata(is_directory: bool, size: u64) -> FileMetadata {
        FileMetadata {
            size,
            gen: 1,
            etag: None,
            modified_at: None,
            is_directory,
        }
    }
}
