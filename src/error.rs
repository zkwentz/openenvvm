//! Error types for openenv-microvm

use thiserror::Error;

#[derive(Error, Debug)]
pub enum MicroVMError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("HTTP error: {0}")]
    Http(#[from] reqwest::Error),

    #[error("Command failed: {command} - {message}")]
    CommandFailed { command: String, message: String },

    #[error("Timeout: {0}")]
    Timeout(String),

    #[error("VM not healthy after {0} seconds")]
    HealthCheckFailed(f64),

    #[error("Pool exhausted: no VMs available")]
    PoolExhausted,

    #[error("Invalid environment path: {0}")]
    InvalidEnvPath(String),

    #[error("Socket not available: {0}")]
    SocketNotAvailable(String),

    #[error("API error: {status} - {message}")]
    ApiError { status: u16, message: String },
}

pub type Result<T> = std::result::Result<T, MicroVMError>;
