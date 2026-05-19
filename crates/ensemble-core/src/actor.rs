use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::{mpsc, Mutex};

use crate::backend::SharedBackend;
use crate::bus::{Bus, Envelope};
use crate::error::CoreError;
use crate::ids::ActorId;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum ActorKind {
    User,
    Agent,
}

/// Trait implemented by anything that wants to react to bus messages.
/// The runtime owns the inbox; the scheduler calls `step` once per
/// arriving envelope.
#[async_trait]
pub trait Actor: Send + Sync + 'static {
    fn id(&self) -> ActorId;
    fn kind(&self) -> ActorKind;
    async fn step(&self, bus: &Bus, envelope: Envelope) -> Result<(), CoreError>;
}

/// A handle to a registered actor: id, kind, and the receiver half of
/// its inbox. The scheduler drains this receiver and dispatches each
/// envelope back to the actor's `step` method.
pub struct ActorHandle {
    pub id: ActorId,
    pub kind: ActorKind,
    pub actor: Arc<dyn Actor>,
    pub inbox: Mutex<Option<mpsc::Receiver<Envelope>>>,
}

impl ActorHandle {
    pub fn new(actor: Arc<dyn Actor>, inbox: mpsc::Receiver<Envelope>) -> Self {
        Self {
            id: actor.id(),
            kind: actor.kind(),
            actor,
            inbox: Mutex::new(Some(inbox)),
        }
    }

    pub async fn take_inbox(&self) -> Option<mpsc::Receiver<Envelope>> {
        self.inbox.lock().await.take()
    }
}

/// Marker struct for user actors. Concrete implementations live in
/// `ensemble-runtime`; this exists so the core can refer to a "user
/// actor" without depending on the runtime crate.
pub struct UserActor {
    pub id: ActorId,
    pub backend: SharedBackend,
}

pub struct AgentActor {
    pub id: ActorId,
    pub backend: SharedBackend,
}
