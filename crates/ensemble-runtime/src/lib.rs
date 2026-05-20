//! Ensemble runtime: LLM clients, prompted personas, and the tool
//! runtime that bridges model output into world state changes.

pub mod actors;
pub mod backend;
pub mod backends;
pub mod persona;
pub mod pricing;
pub mod resources;
pub mod tools;

pub use backend::{
    BackendError, ChatMessage, CompletionRequest, CompletionResponse, LLMBackend, ProposedToolCall,
    SharedBackend, ToolSchema, Usage,
};
pub use backends::anthropic::AnthropicBackend;
#[cfg(feature = "mock")]
pub use backends::mock::{MockBackend, MockScript, MockTurn};
pub use backends::openai::OpenAIBackend;
pub use backends::vllm::LocalAdapterBackend;
pub use actors::{AgentActor, UserActor};
pub use persona::{HiddenState, PromptedPersona};
pub use resources::{ResourceKind, ResourceManager};
pub use tools::{
    DispatchResult, ProgressEmitter, ProgressEntry, Tool, ToolOutcome, ToolRegistry,
};
