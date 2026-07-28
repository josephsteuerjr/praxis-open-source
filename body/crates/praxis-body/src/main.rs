mod artifact;
mod compose;
mod config;
mod desktop;
mod fsops;
mod identity;
mod interactive_host;
mod interactive_router;
mod journal;
mod local_router;
mod process;
mod runtime;
mod system_router;
mod transport;
mod uia;

use std::io::Read;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::Result;
use clap::{Parser, Subcommand, ValueEnum};
use praxis_body_protocol::{Envelope, ExecutionKind, Frame};
use serde_json::Value;
use uuid::Uuid;

use config::BodyConfig;
use runtime::Runtime;

#[derive(Debug, Parser)]
#[command(
    name = "praxis-body",
    version,
    about = "Brainless Windows execution body for Praxis"
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Debug, Subcommand)]
enum Commands {
    Identity,
    Capabilities {
        #[arg(long, default_value = "windows-pc")]
        device_id: String,
    },
    Connect {
        #[arg(long)]
        config: PathBuf,
    },
    #[command(hide = true)]
    ServeInteractive {
        #[arg(long)]
        config: PathBuf,
    },
    InvokeLocal {
        #[arg(long)]
        config: PathBuf,
        #[arg(long)]
        capability: String,
        #[arg(long, default_value = "{}")]
        args: String,
        #[arg(long, value_enum, default_value_t = ExecutionArg::Interactive)]
        execution: ExecutionArg,
        #[arg(long)]
        operation_id: Option<String>,
    },
    #[command(hide = true)]
    InvokeEnvelope {
        #[arg(long)]
        config: PathBuf,
    },
    #[command(hide = true)]
    Supervise {
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long)]
        operation_id: String,
    },
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum ExecutionArg {
    Interactive,
    System,
}

impl From<ExecutionArg> for ExecutionKind {
    fn from(value: ExecutionArg) -> Self {
        match value {
            ExecutionArg::Interactive => Self::Interactive,
            ExecutionArg::System => Self::System,
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "praxis_body=info".into()),
        )
        .with_ansi(false)
        .with_writer(std::io::stderr)
        .init();
    let cli = Cli::parse();
    match cli.command {
        Commands::Identity => {
            println!("{}", serde_json::to_string_pretty(&identity::current())?);
        }
        Commands::Capabilities { device_id } => {
            println!(
                "{}",
                serde_json::to_string_pretty(&identity::manifest(&device_id))?
            );
        }
        Commands::Connect { config } => {
            let runtime = Arc::new(Runtime::open(BodyConfig::load(&config)?)?);
            transport::connect_forever(runtime).await?;
        }
        Commands::ServeInteractive { config } => {
            let runtime = Arc::new(Runtime::open(BodyConfig::load(&config)?)?);
            interactive_host::serve(runtime).await?;
        }
        Commands::InvokeLocal {
            config,
            capability,
            args,
            execution,
            operation_id,
        } => {
            let runtime = Runtime::open(BodyConfig::load(&config)?)?;
            let request_id = Uuid::new_v4().to_string();
            let operation_id = operation_id.unwrap_or_else(|| format!("op-{}", Uuid::new_v4()));
            let envelope = Envelope::new(
                runtime.config.device_id.clone(),
                1,
                Frame::Invoke {
                    request_id,
                    operation_id,
                    execution: execution.into(),
                    capability,
                    args: serde_json::from_str::<Value>(&args)?,
                    deadline: None,
                },
            );
            println!(
                "{}",
                serde_json::to_string_pretty(&runtime.handle(envelope).await)?
            );
        }
        Commands::Supervise {
            state_dir,
            operation_id,
        } => process::supervise(&state_dir, &operation_id)?,
        Commands::InvokeEnvelope { config } => {
            let runtime = Runtime::open(BodyConfig::load(&config)?)?;
            let mut raw = Vec::new();
            std::io::stdin().read_to_end(&mut raw)?;
            let envelope: Envelope = serde_json::from_slice(&raw)?;
            println!(
                "{}",
                serde_json::to_string(&runtime.handle(envelope).await)?
            );
        }
    }
    Ok(())
}
