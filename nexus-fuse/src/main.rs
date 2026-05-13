//! Nexus FUSE Client - High-performance FUSE mount for Nexus filesystem
//!
//! This is a Rust implementation of the Nexus FUSE client, designed for
//! fast startup time (<100ms vs ~10s for Python version).

use clap::{Parser, Subcommand};
use fuser::{Config, MountOption, SessionACL};
use log::{error, info, warn};
use nexus_fuse::{cache, client, daemon, fs, metrics, passthrough};
use std::path::PathBuf;
use std::sync::Arc;

#[derive(Parser)]
#[command(name = "nexus-fuse")]
#[command(about = "High-performance FUSE client for Nexus filesystem")]
#[command(version)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Mount Nexus filesystem
    Mount {
        /// Mount point path
        #[arg(value_name = "MOUNT_POINT")]
        mount_point: PathBuf,

        /// Nexus server URL
        #[arg(long, env = "NEXUS_URL")]
        url: String,

        /// Nexus API key (DEPRECATED: use --api-key-file instead)
        #[arg(long, env = "NEXUS_API_KEY")]
        api_key: Option<String>,

        /// Path to a file containing the Nexus API key
        #[arg(long)]
        api_key_file: Option<PathBuf>,

        /// Allow other users to access the mount
        #[arg(long, default_value = "false")]
        allow_other: bool,

        /// Run in foreground (don't daemonize)
        #[arg(long, short = 'f', default_value = "false")]
        foreground: bool,

        /// Agent ID for file attribution
        #[arg(long, env = "NEXUS_AGENT_ID")]
        agent_id: Option<String>,

        /// Foyer DRAM cache size in MiB
        #[arg(long, env = "NEXUS_FUSE_CACHE_MEMORY_MB", default_value_t = 256)]
        cache_memory_mb: usize,

        /// Foyer filesystem cache size in GiB
        #[arg(long, env = "NEXUS_FUSE_CACHE_DISK_GB", default_value_t = 10)]
        cache_disk_gb: usize,

        /// Override cache root directory
        #[arg(long, env = "NEXUS_FUSE_CACHE_DIR")]
        cache_dir: Option<PathBuf>,

        /// Enable Linux FUSE passthrough for eligible large reads. Can also be set with NEXUS_FUSE_PASSTHROUGH.
        #[arg(long, default_value_t = false)]
        passthrough: bool,

        /// Glob allow pattern for passthrough. Repeat for multiple patterns. Appends to NEXUS_FUSE_PASSTHROUGH_PATTERNS.
        #[arg(long = "passthrough-pattern", value_delimiter = ',')]
        passthrough_patterns: Vec<String>,

        /// Glob deny pattern for passthrough. Repeat for multiple patterns. Appends to NEXUS_FUSE_PASSTHROUGH_DENY_PATTERNS.
        #[arg(long = "passthrough-deny-pattern", value_delimiter = ',')]
        passthrough_deny_patterns: Vec<String>,

        /// Minimum file size for passthrough eligibility.
        #[arg(
            long,
            env = "NEXUS_FUSE_PASSTHROUGH_THRESHOLD_BYTES",
            default_value_t = passthrough::DEFAULT_THRESHOLD_BYTES
        )]
        passthrough_threshold_bytes: u64,

        /// Fail the mount instead of falling back when passthrough is unavailable. Can also be set with NEXUS_FUSE_PASSTHROUGH_REQUIRE.
        #[arg(long, default_value_t = false)]
        passthrough_require: bool,

        /// Directory for immutable passthrough backing files.
        #[arg(long, env = "NEXUS_FUSE_PASSTHROUGH_BACKING_DIR")]
        passthrough_backing_dir: Option<PathBuf>,

        /// Prometheus metrics bind address, for example 127.0.0.1:9464
        #[arg(long, env = "NEXUS_FUSE_METRICS_ADDR")]
        metrics_addr: Option<String>,
    },
    /// Run as Unix socket IPC daemon for Python integration
    Daemon {
        /// Nexus server URL
        #[arg(long, env = "NEXUS_URL")]
        url: String,

        /// Nexus API key (DEPRECATED: use --api-key-file instead)
        #[arg(long, env = "NEXUS_API_KEY")]
        api_key: Option<String>,

        /// Path to a file containing the Nexus API key
        #[arg(long)]
        api_key_file: Option<PathBuf>,

        /// Unix socket path (default: /tmp/nexus-fuse-{pid}.sock)
        #[arg(long)]
        socket: Option<PathBuf>,

        /// Agent ID for file attribution
        #[arg(long, env = "NEXUS_AGENT_ID")]
        agent_id: Option<String>,

        /// Foyer DRAM cache size in MiB
        #[arg(long, env = "NEXUS_FUSE_CACHE_MEMORY_MB", default_value_t = 256)]
        cache_memory_mb: usize,

        /// Foyer filesystem cache size in GiB
        #[arg(long, env = "NEXUS_FUSE_CACHE_DISK_GB", default_value_t = 10)]
        cache_disk_gb: usize,

        /// Override cache root directory
        #[arg(long, env = "NEXUS_FUSE_CACHE_DIR")]
        cache_dir: Option<PathBuf>,

        /// Prometheus metrics bind address, for example 127.0.0.1:9464
        #[arg(long, env = "NEXUS_FUSE_METRICS_ADDR")]
        metrics_addr: Option<String>,
    },
    /// Check version
    Version,
}

/// Resolve the API key from --api-key-file or --api-key (Issue 17A).
///
/// Resolution order: --api-key-file > --api-key / NEXUS_API_KEY.
/// Using --api-key prints a deprecation warning to stderr.
fn resolve_api_key(
    api_key: Option<String>,
    api_key_file: Option<PathBuf>,
) -> anyhow::Result<String> {
    if let Some(path) = api_key_file {
        let key = std::fs::read_to_string(&path).map_err(|e| {
            anyhow::anyhow!("Failed to read API key file {}: {}", path.display(), e)
        })?;
        return Ok(key.trim().to_string());
    }

    if let Some(key) = api_key {
        eprintln!(
            "WARNING: --api-key / NEXUS_API_KEY is deprecated and will be removed in a future release. \
             Use --api-key-file instead to avoid leaking secrets via process arguments."
        );
        return Ok(key);
    }

    Err(anyhow::anyhow!(
        "No API key provided. Use --api-key-file <path> to supply a key, \
         or set NEXUS_API_KEY (deprecated)."
    ))
}

fn mib_to_bytes(mib: usize) -> anyhow::Result<usize> {
    mib.checked_mul(1024 * 1024)
        .ok_or_else(|| anyhow::anyhow!("cache memory size overflows usize"))
}

fn gib_to_bytes(gib: usize) -> anyhow::Result<usize> {
    gib.checked_mul(1024 * 1024 * 1024)
        .ok_or_else(|| anyhow::anyhow!("cache disk size overflows usize"))
}

fn build_cache_config(
    cache_memory_mb: usize,
    cache_disk_gb: usize,
    cache_dir: Option<PathBuf>,
) -> anyhow::Result<cache::CacheConfig> {
    let root_dir = cache_dir.unwrap_or_else(|| cache::CacheConfig::default().root_dir);
    cache::CacheConfig::new(
        root_dir,
        mib_to_bytes(cache_memory_mb)?,
        gib_to_bytes(cache_disk_gb)?,
        cache::MAX_FILE_SIZE,
    )
}

fn optional_env_value(name: &str) -> anyhow::Result<Option<String>> {
    match std::env::var(name) {
        Ok(value) => Ok(Some(value)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(std::env::VarError::NotUnicode(_)) => anyhow::bail!("{name} must be valid UTF-8"),
    }
}

fn normalize_pattern_values(values: Vec<String>) -> Vec<String> {
    values
        .into_iter()
        .flat_map(|value| passthrough::parse_pattern_env(&value))
        .collect()
}

fn merge_passthrough_patterns(env_value: Option<&str>, cli_patterns: Vec<String>) -> Vec<String> {
    let mut patterns = env_value
        .map(passthrough::parse_pattern_env)
        .unwrap_or_default();
    patterns.extend(normalize_pattern_values(cli_patterns));
    patterns
}

fn read_merged_passthrough_patterns(
    env_name: &str,
    cli_patterns: Vec<String>,
) -> anyhow::Result<Vec<String>> {
    let env_value = optional_env_value(env_name)?;
    Ok(merge_passthrough_patterns(
        env_value.as_deref(),
        cli_patterns,
    ))
}

fn parse_bool_env_value(env_name: &str, value: &str) -> anyhow::Result<bool> {
    match value.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        _ => anyhow::bail!("{env_name} must be one of 1/0/true/false/yes/no/on/off"),
    }
}

fn bool_flag_with_env(
    cli_enabled: bool,
    env_value: Option<&str>,
    env_name: &str,
) -> anyhow::Result<bool> {
    if cli_enabled {
        return Ok(true);
    }

    env_value
        .map(|value| parse_bool_env_value(env_name, value))
        .unwrap_or(Ok(false))
}

fn read_bool_flag_with_env(cli_enabled: bool, env_name: &str) -> anyhow::Result<bool> {
    let env_value = optional_env_value(env_name)?;
    bool_flag_with_env(cli_enabled, env_value.as_deref(), env_name)
}

fn build_passthrough_config(
    enabled: bool,
    allow_patterns: Vec<String>,
    deny_patterns: Vec<String>,
    threshold_bytes: u64,
    require: bool,
    backing_dir: Option<PathBuf>,
) -> anyhow::Result<passthrough::PassthroughConfig> {
    if threshold_bytes == 0 {
        anyhow::bail!("passthrough threshold must be greater than zero");
    }

    let allow_patterns = normalize_pattern_values(allow_patterns);
    let deny_patterns = normalize_pattern_values(deny_patterns);
    let enabled = if enabled && allow_patterns.is_empty() {
        if require {
            anyhow::bail!("passthrough requires at least one allow pattern");
        }
        warn!("FUSE passthrough disabled: at least one passthrough allow pattern is required");
        false
    } else {
        enabled
    };

    Ok(passthrough::PassthroughConfig {
        enabled,
        allow_patterns,
        deny_patterns,
        threshold_bytes,
        require,
        backing_dir,
    })
}

fn create_passthrough_manager(
    url: &str,
    config: passthrough::PassthroughConfig,
) -> anyhow::Result<Option<Arc<passthrough::PassthroughManager>>> {
    if !config.enabled {
        return Ok(None);
    }

    if !config.require && !passthrough::linux_passthrough_supported() {
        warn!(
            "FUSE passthrough support was not detected before mount; continuing to FUSE negotiation"
        );
    }

    match passthrough::PassthroughManager::new(url.to_string(), config.clone()) {
        Ok(manager) => Ok(Some(Arc::new(manager))),
        Err(err) if config.require => Err(err),
        Err(err) => {
            warn!("FUSE passthrough disabled: {}", err);
            Ok(None)
        }
    }
}

fn open_file_cache(
    url: &str,
    api_key: &str,
    agent_id: Option<&str>,
    config: cache::CacheConfig,
) -> Option<Arc<cache::FileCache>> {
    // Hash both api_key AND agent_id into the foyer directory namespace.
    // Two daemons run by the same API key but impersonating different
    // agents send different X-Agent-ID headers and therefore see
    // different effective ReBAC scopes; they must NOT share a cache
    // (#4055 R8). Format: "<api_key>|agent=<agent_id>" — domain-separated
    // so an api_key that literally contains "|agent=" can't be confused
    // with an agent suffix.
    let principal = match agent_id {
        Some(aid) => format!("{api_key}|agent={aid}"),
        None => api_key.to_string(),
    };
    match cache::FileCache::new_with_config(url, &principal, config) {
        Ok(cache) => {
            let stats = cache.stats();
            info!(
                "Foyer cache ready: {} current-process files ({} MB)",
                stats.file_count,
                stats.total_size / 1024 / 1024
            );
            Some(Arc::new(cache))
        }
        Err(e) => {
            error!(
                "Failed to initialize foyer cache: {} (continuing without cache)",
                e
            );
            None
        }
    }
}

fn main() -> anyhow::Result<()> {
    // Initialize logging
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let cli = Cli::parse();

    match cli.command {
        Commands::Mount {
            mount_point,
            url,
            api_key,
            api_key_file,
            allow_other,
            foreground,
            agent_id,
            cache_memory_mb,
            cache_disk_gb,
            cache_dir,
            passthrough,
            passthrough_patterns,
            passthrough_deny_patterns,
            passthrough_threshold_bytes,
            passthrough_require,
            passthrough_backing_dir,
            metrics_addr,
        } => {
            let api_key = resolve_api_key(api_key, api_key_file)?;
            let _metrics_server = if let Some(addr) = metrics_addr.as_deref() {
                let server = metrics::start_server(addr)?;
                info!("FUSE metrics listening on {}", server.local_addr());
                Some(server)
            } else {
                None
            };

            info!("Nexus FUSE client starting...");
            info!("Server URL: {}", url);
            info!("Mount point: {}", mount_point.display());

            // Create Nexus client. Clone agent_id because open_file_cache
            // also reads it below for the cache namespace (#4055 R9).
            let client = client::NexusClient::new(&url, &api_key, agent_id.clone())?;

            // Verify connection
            info!("Connecting to Nexus server...");
            match client.whoami() {
                Ok(user_info) => {
                    let user = user_info.user_id.as_deref().unwrap_or("admin");
                    let tenant = user_info.tenant_id.as_deref().unwrap_or("default");
                    info!("Authenticated as {} (tenant: {})", user, tenant);
                }
                Err(e) => {
                    error!("Failed to authenticate: {}", e);
                    return Err(e.into());
                }
            }

            let cache_config = build_cache_config(cache_memory_mb, cache_disk_gb, cache_dir)?;
            let file_cache = open_file_cache(&url, &api_key, agent_id.as_deref(), cache_config);

            let passthrough = read_bool_flag_with_env(passthrough, "NEXUS_FUSE_PASSTHROUGH")?;
            let passthrough_require =
                read_bool_flag_with_env(passthrough_require, "NEXUS_FUSE_PASSTHROUGH_REQUIRE")?;
            let passthrough_patterns = read_merged_passthrough_patterns(
                "NEXUS_FUSE_PASSTHROUGH_PATTERNS",
                passthrough_patterns,
            )?;
            let passthrough_deny_patterns = read_merged_passthrough_patterns(
                "NEXUS_FUSE_PASSTHROUGH_DENY_PATTERNS",
                passthrough_deny_patterns,
            )?;
            let passthrough_config = build_passthrough_config(
                passthrough,
                passthrough_patterns,
                passthrough_deny_patterns,
                passthrough_threshold_bytes,
                passthrough_require,
                passthrough_backing_dir,
            )?;
            let passthrough_manager = create_passthrough_manager(&url, passthrough_config)?;

            // Create filesystem
            let filesystem = fs::NexusFs::try_new(client, file_cache, passthrough_manager)?;

            // Build mount options
            let mut options = Config::default();
            options.mount_options = vec![
                MountOption::FSName("nexus".to_string()),
                MountOption::AutoUnmount,
                MountOption::DefaultPermissions,
            ];
            options.acl = if allow_other {
                SessionACL::All
            } else {
                SessionACL::RootAndOwner
            };

            // Mount
            info!("Mounting filesystem...");
            if foreground {
                fuser::mount2(filesystem, &mount_point, &options)?;
            } else {
                // For daemon mode, we'd need to fork - for now just run foreground
                fuser::mount2(filesystem, &mount_point, &options)?;
            }

            info!("Filesystem unmounted");
        }
        Commands::Daemon {
            url,
            api_key,
            api_key_file,
            socket,
            agent_id,
            cache_memory_mb,
            cache_disk_gb,
            cache_dir,
            metrics_addr,
        } => {
            let api_key = resolve_api_key(api_key, api_key_file)?;
            let _metrics_server = if let Some(addr) = metrics_addr.as_deref() {
                let server = metrics::start_server(addr)?;
                info!("FUSE metrics listening on {}", server.local_addr());
                Some(server)
            } else {
                None
            };

            // Determine socket path
            let socket_path = socket.unwrap_or_else(|| {
                let pid = std::process::id();
                PathBuf::from(format!("/tmp/nexus-fuse-{}.sock", pid))
            });

            let cache_config = build_cache_config(cache_memory_mb, cache_disk_gb, cache_dir)?;
            let file_cache = open_file_cache(&url, &api_key, agent_id.as_deref(), cache_config);

            let config = daemon::DaemonConfig {
                socket_path,
                nexus_url: url,
                api_key,
                agent_id,
                file_cache,
            };

            // Create daemon
            let daemon = daemon::Daemon::new(config)?;

            // Run daemon (async)
            tokio::runtime::Runtime::new()?.block_on(daemon.run())?;
        }
        Commands::Version => {
            println!("nexus-fuse {}", env!("CARGO_PKG_VERSION"));
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_passthrough_config_keeps_disabled_default() {
        let config = build_passthrough_config(
            false,
            Vec::new(),
            Vec::new(),
            passthrough::DEFAULT_THRESHOLD_BYTES,
            false,
            None,
        )
        .expect("config");

        assert!(!config.enabled);
        assert_eq!(config.threshold_bytes, passthrough::DEFAULT_THRESHOLD_BYTES);
    }

    #[test]
    fn build_passthrough_config_preserves_patterns_and_require() {
        let config = build_passthrough_config(
            true,
            vec!["/data/**".to_string()],
            vec!["/data/private/**".to_string()],
            256 * 1024,
            true,
            None,
        )
        .expect("config");

        assert!(config.enabled);
        assert_eq!(config.allow_patterns, vec!["/data/**"]);
        assert_eq!(config.deny_patterns, vec!["/data/private/**"]);
        assert_eq!(config.threshold_bytes, 256 * 1024);
        assert!(config.require);
    }

    #[test]
    fn build_passthrough_config_rejects_zero_threshold() {
        let err =
            build_passthrough_config(true, vec!["/data/**".to_string()], vec![], 0, false, None)
                .expect_err("zero threshold should fail");

        assert!(err
            .to_string()
            .contains("passthrough threshold must be greater than zero"));
    }

    #[test]
    fn merge_passthrough_patterns_appends_env_then_cli_and_filters_empty_segments() {
        let patterns = merge_passthrough_patterns(
            Some(" /env/**, ,/env-two/** "),
            vec![
                " /cli/** ".to_string(),
                ",".to_string(),
                "/cli-two/**".to_string(),
            ],
        );

        assert_eq!(
            patterns,
            vec!["/env/**", "/env-two/**", "/cli/**", "/cli-two/**"]
        );
    }

    #[test]
    fn build_passthrough_config_disables_empty_allow_when_not_required() {
        let config = build_passthrough_config(true, vec![], vec![], 128 * 1024, false, None)
            .expect("config");

        assert!(!config.enabled);
    }

    #[test]
    fn build_passthrough_config_rejects_empty_allow_when_required() {
        let err = build_passthrough_config(true, vec![], vec![], 128 * 1024, true, None)
            .expect_err("required passthrough without allow patterns should fail");

        assert!(err
            .to_string()
            .contains("passthrough requires at least one allow pattern"));
    }

    #[test]
    fn create_passthrough_manager_skips_disabled_config() {
        let config = build_passthrough_config(true, vec![], vec![], 128 * 1024, false, None)
            .expect("config");

        assert!(create_passthrough_manager("http://server", config)
            .expect("manager")
            .is_none());
    }

    #[test]
    fn create_passthrough_manager_does_not_pre_gate_on_kernel_version() {
        let backing_dir = tempfile::tempdir().expect("tempdir");
        let config = build_passthrough_config(
            true,
            vec!["/data/**".to_string()],
            vec![],
            128 * 1024,
            false,
            Some(backing_dir.path().to_path_buf()),
        )
        .expect("config");

        assert!(create_passthrough_manager("http://server", config)
            .expect("manager")
            .is_some());
    }

    #[test]
    fn parse_bool_env_accepts_compatible_values() {
        assert!(bool_flag_with_env(false, Some("1"), "FLAG").expect("bool"));
        assert!(bool_flag_with_env(false, Some("yes"), "FLAG").expect("bool"));
        assert!(bool_flag_with_env(false, Some("on"), "FLAG").expect("bool"));
        assert!(!bool_flag_with_env(false, Some("0"), "FLAG").expect("bool"));
        assert!(!bool_flag_with_env(false, Some("no"), "FLAG").expect("bool"));
        assert!(!bool_flag_with_env(false, Some("off"), "FLAG").expect("bool"));
        assert!(bool_flag_with_env(true, Some("0"), "FLAG").expect("bool"));
    }

    #[test]
    fn parse_bool_env_rejects_unknown_values() {
        let err = bool_flag_with_env(false, Some("maybe"), "FLAG").expect_err("invalid bool");

        assert!(err
            .to_string()
            .contains("FLAG must be one of 1/0/true/false/yes/no/on/off"));
    }
}
