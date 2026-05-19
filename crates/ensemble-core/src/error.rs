use thiserror::Error;

#[derive(Debug, Error)]
pub enum CoreError {
    #[error("scheduler exited: {0}")]
    SchedulerExit(String),
    #[error("tick budget exhausted")]
    TickBudgetExhausted,
    #[error("actor not found: {0}")]
    ActorNotFound(String),
    #[error("bus closed")]
    BusClosed,
    #[error("tool error: {0}")]
    Tool(#[from] ToolError),
    #[error("restore error: {0}")]
    Restore(#[from] RestoreError),
    #[error("backend error: {0}")]
    Backend(#[from] crate::backend::BackendError),
    #[error("{0}")]
    Other(String),
}

impl From<serde_json::Error> for CoreError {
    fn from(e: serde_json::Error) -> Self {
        CoreError::Other(format!("json: {e}"))
    }
}

#[derive(Debug, Error)]
pub enum ToolError {
    #[error("unknown tool: {0}")]
    UnknownTool(String),
    #[error("invalid arguments: {0}")]
    InvalidArgs(String),
    #[error("tool execution failed: {0}")]
    Execution(String),
}

#[derive(Debug, Error)]
pub enum RestoreError {
    #[error("snapshot version mismatch")]
    VersionMismatch,
    #[error("snapshot decode error: {0}")]
    Decode(String),
}
