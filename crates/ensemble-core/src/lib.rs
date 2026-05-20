//! Ensemble core: pure simulation primitives.
//!
//! No LLM calls, no HTTP, no Python. All higher layers depend on this crate.

pub mod actor;
pub mod bus;
pub mod error;
pub mod event;
pub mod ids;
pub mod predicate;
pub mod scheduler;
pub mod until;
pub mod world;

pub mod prelude {
    pub use crate::actor::{Actor, ActorHandle, ActorKind};
    pub use crate::bus::{Bus, Envelope, Message, Recipient};
    pub use crate::error::{CoreError, RestoreError, ToolError};
    pub use crate::event::{Event, EventLog, EventPayload, Tick, TraceFile};
    pub use crate::ids::{ActorId, MessageId, RunId};
    pub use crate::predicate::{Predicate, PredicateCtx, PredicateRegistry};
    pub use crate::scheduler::{BudgetCap, Scheduler, StopReason, TickBudget};
    pub use crate::until::{all_of, any_of, turn_count_exceeds, Until};
    pub use crate::world::{World, WorldHandle, WorldState};
}
