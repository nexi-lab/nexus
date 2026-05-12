use globset::{Glob, GlobSet, GlobSetBuilder};
use std::path::PathBuf;

pub const DEFAULT_THRESHOLD_BYTES: u64 = 128 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PassthroughConfig {
    pub enabled: bool,
    pub allow_patterns: Vec<String>,
    pub deny_patterns: Vec<String>,
    pub threshold_bytes: u64,
    pub require: bool,
    pub backing_dir: Option<PathBuf>,
}

impl Default for PassthroughConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            allow_patterns: Vec::new(),
            deny_patterns: Vec::new(),
            threshold_bytes: DEFAULT_THRESHOLD_BYTES,
            require: false,
            backing_dir: None,
        }
    }
}

impl PassthroughConfig {
    pub fn disabled() -> Self {
        Self::default()
    }

    pub fn pattern_set(&self) -> Result<PatternSet, globset::Error> {
        PatternSet::new(self.allow_patterns.clone(), self.deny_patterns.clone())
    }
}

pub struct PatternSet {
    allow: GlobSet,
    deny: GlobSet,
    has_allow_patterns: bool,
}

impl PatternSet {
    pub fn new(
        allow_patterns: Vec<String>,
        deny_patterns: Vec<String>,
    ) -> Result<Self, globset::Error> {
        let has_allow_patterns = !allow_patterns.is_empty();
        Ok(Self {
            allow: build_glob_set(&allow_patterns)?,
            deny: build_glob_set(&deny_patterns)?,
            has_allow_patterns,
        })
    }

    pub fn allows(&self, path: &str) -> bool {
        !self.deny.is_match(path) && (!self.has_allow_patterns || self.allow.is_match(path))
    }
}

fn build_glob_set(patterns: &[String]) -> Result<GlobSet, globset::Error> {
    let mut builder = GlobSetBuilder::new();
    for pattern in patterns {
        builder.add(Glob::new(pattern)?);
    }
    builder.build()
}

pub fn parse_pattern_env(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(str::trim)
        .filter(|pattern| !pattern.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}

pub fn kernel_release_supports_passthrough(release: &str) -> bool {
    let mut parts = release.split('.');
    let Some(major) = parts.next().and_then(|part| part.parse::<u64>().ok()) else {
        return false;
    };
    let Some(minor) = parts.next().and_then(parse_leading_number) else {
        return false;
    };

    major > 6 || (major == 6 && minor >= 9)
}

pub fn linux_passthrough_supported() -> bool {
    #[cfg(target_os = "linux")]
    {
        std::fs::read_to_string("/proc/sys/kernel/osrelease")
            .map(|release| kernel_release_supports_passthrough(release.trim()))
            .unwrap_or(false)
    }

    #[cfg(not(target_os = "linux"))]
    {
        false
    }
}

fn parse_leading_number(value: &str) -> Option<u64> {
    let digits: String = value
        .chars()
        .take_while(|character| character.is_ascii_digit())
        .collect();

    if digits.is_empty() {
        None
    } else {
        digits.parse().ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_is_disabled_with_expected_limits() {
        let config = PassthroughConfig::default();

        assert!(!config.enabled);
        assert_eq!(config.threshold_bytes, DEFAULT_THRESHOLD_BYTES);
        assert!(!config.require);
        assert!(config.allow_patterns.is_empty());
        assert!(config.deny_patterns.is_empty());
        assert!(config.backing_dir.is_none());
        assert_eq!(PassthroughConfig::disabled(), config);
    }

    #[test]
    fn parse_pattern_env_trims_empty_segments() {
        let patterns = parse_pattern_env(" cache/**, ,*.bin,, /mnt/data/** ");

        assert_eq!(patterns, vec!["cache/**", "*.bin", "/mnt/data/**"]);
    }

    #[test]
    fn empty_allow_pattern_set_allows_any_path_unless_denied() {
        let patterns = PatternSet::new(Vec::new(), vec!["/secret/**".to_string()]).unwrap();

        assert!(patterns.allows("/public/file.bin"));
        assert!(!patterns.allows("/secret/file.bin"));
    }

    #[test]
    fn allow_and_deny_globs_control_path_eligibility() {
        let config = PassthroughConfig {
            allow_patterns: vec!["/cache/**".to_string(), "*.iso".to_string()],
            deny_patterns: vec!["/cache/private/**".to_string()],
            ..Default::default()
        };
        let patterns = config.pattern_set().unwrap();

        assert!(patterns.allows("/cache/image.bin"));
        assert!(patterns.allows("disk.iso"));
        assert!(!patterns.allows("/cache/private/key.bin"));
        assert!(!patterns.allows("/other/file.txt"));
    }

    #[test]
    fn kernel_release_supports_linux_passthrough_from_6_9() {
        assert!(kernel_release_supports_passthrough("6.9.0"));
        assert!(kernel_release_supports_passthrough("6.10.1-custom"));
        assert!(!kernel_release_supports_passthrough("6.8.12"));
        assert!(!kernel_release_supports_passthrough("5.15.0"));
        assert!(!kernel_release_supports_passthrough("not-a-version"));
    }
}
