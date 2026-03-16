//! Nexus FUSE Client - High-performance FUSE mount for Nexus filesystem
//!
//! This is a Rust implementation of the Nexus FUSE client, designed for
//! fast startup time (<100ms vs ~10s for Python version).

use clap::{Parser, Subcommand};
use nexus_fuse::{cache, client, daemon, fs};
use fuser::MountOption;
use log::{error, info};
use std::path::PathBuf;

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
    },
    /// Check version
    Version,
}

/// Resolve the API key from --api-key-file or --api-key (Issue 17A).
///
/// Resolution order: --api-key-file > --api-key / NEXUS_API_KEY.
/// Using --api-key prints a deprecation warning to stderr.
fn resolve_api_key(api_key: Option<String>, api_key_file: Option<PathBuf>) -> anyhow::Result<String> {
    if let Some(path) = api_key_file {
        let key = std::fs::read_to_string(&path)
            .map_err(|e| anyhow::anyhow!("Failed to read API key file {}: {}", path.display(), e))?;
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
        } => {
            let api_key = resolve_api_key(api_key, api_key_file)?;

            info!("Nexus FUSE client starting...");
            info!("Server URL: {}", url);
            info!("Mount point: {}", mount_point.display());

            // Create Nexus client
            let client = client::NexusClient::new(&url, &api_key, agent_id)?;

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

            // Create persistent cache
            let file_cache = match cache::FileCache::new(&url) {
                Ok(cache) => {
                    let stats = cache.stats();
                    info!(
                        "Cache loaded: {} files ({} MB)",
                        stats.file_count,
                        stats.total_size / 1024 / 1024
                    );
                    Some(cache)
                }
                Err(e) => {
                    error!("Failed to initialize cache: {} (continuing without cache)", e);
                    None
                }
            };

            // Create filesystem
            let filesystem = fs::NexusFs::new(client, file_cache);

            // Build mount options
            let mut options = vec![
                MountOption::FSName("nexus".to_string()),
                MountOption::AutoUnmount,
                MountOption::DefaultPermissions,
            ];

            if allow_other {
                options.push(MountOption::AllowOther);
            }

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
        } => {
            let api_key = resolve_api_key(api_key, api_key_file)?;

            // Determine socket path
            let socket_path = socket.unwrap_or_else(|| {
                let pid = std::process::id();
                PathBuf::from(format!("/tmp/nexus-fuse-{}.sock", pid))
            });

            // Create daemon config
            let config = daemon::DaemonConfig {
                socket_path,
                nexus_url: url,
                api_key,
                agent_id,
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
