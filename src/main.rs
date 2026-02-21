//! OpenEnv to MicroVM converter CLI

mod convert;
mod error;
mod pool;
mod runtime;

use clap::{Parser, Subcommand};
use std::path::PathBuf;
use tracing_subscriber::EnvFilter;

#[derive(Parser)]
#[command(name = "openenv-microvm")]
#[command(about = "Convert OpenEnv environments to Firecracker microVMs")]
#[command(version)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Convert an OpenEnv environment to a microVM package
    Convert {
        /// Path to OpenEnv environment or hf:org/repo for HuggingFace
        env_path: String,

        /// Output path for the .microvm package
        #[arg(short, long)]
        output: PathBuf,

        /// Memory allocation in MB
        #[arg(long, default_value = "256")]
        memory: u32,

        /// Number of vCPUs
        #[arg(long, default_value = "1")]
        vcpus: u32,

        /// Path to custom vmlinux kernel
        #[arg(long)]
        kernel: Option<PathBuf>,
    },

    /// Run a microVM package
    Run {
        /// Path to the .microvm package
        package_path: PathBuf,

        /// Port to expose
        #[arg(short, long, default_value = "8000")]
        port: u16,

        /// IP address for the VM
        #[arg(long, default_value = "172.16.0.2")]
        ip: String,
    },

    /// Run a pool of microVMs
    Pool {
        /// Path to the .microvm package
        package_path: PathBuf,

        /// Pool size
        #[arg(short = 'n', long, default_value = "8")]
        size: usize,

        /// Base port
        #[arg(short, long, default_value = "8000")]
        port: u16,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Initialize logging
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .init();

    let cli = Cli::parse();

    match cli.command {
        Commands::Convert {
            env_path,
            output,
            memory,
            vcpus,
            kernel,
        } => {
            println!("Converting {} to microVM...", env_path);
            let result = convert::convert_env_to_microvm(
                &env_path,
                &output,
                kernel.as_deref(),
                memory,
                vcpus,
            )?;
            println!("Created microVM package at {}", result.display());
        }

        Commands::Run {
            package_path,
            port,
            ip,
        } => {
            println!("Starting microVM from {}...", package_path.display());
            let vm = runtime::start_microvm(&package_path, None, port, &ip, "172.16.0.1").await?;
            println!("MicroVM running at {}", vm.url());
            println!("Press Ctrl+C to stop");

            // Wait for Ctrl+C
            tokio::signal::ctrl_c().await?;
            println!("\nStopping VM...");
            vm.stop().await?;
        }

        Commands::Pool {
            package_path,
            size,
            port,
        } => {
            println!("Starting pool of {} microVMs...", size);
            let pool = pool::MicroVMPool::new(&package_path, size, 2, "172.16", port).await?;

            println!("Pool ready. Stats: {:?}", pool.stats().await);
            println!("Press Ctrl+C to stop");

            // Print stats periodically
            let pool_clone = pool.clone();
            let stats_task = tokio::spawn(async move {
                loop {
                    tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
                    println!("Stats: {:?}", pool_clone.stats().await);
                }
            });

            // Wait for Ctrl+C
            tokio::signal::ctrl_c().await?;
            println!("\nShutting down pool...");

            stats_task.abort();
            pool.shutdown().await?;
        }
    }

    Ok(())
}
