//! Nexus FUSE Client - High-performance FUSE mount for Nexus filesystem
//!
//! This is a Rust implementation of the Nexus FUSE client, designed for
//! fast startup time (<100ms vs ~10s for Python version).

mod cache;
pub mod client;
pub mod error;
mod fs;

use clap::{Parser, Subcommand};
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

        /// Nexus API key
        #[arg(long, env = "NEXUS_API_KEY")]
        api_key: String,

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
    /// Check version
    Version,
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
            allow_other,
            foreground,
            agent_id,
        } => {
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
        Commands::Version => {
            println!("nexus-fuse {}", env!("CARGO_PKG_VERSION"));
        }
    }

    Ok(())
}
