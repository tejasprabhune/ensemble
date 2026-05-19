use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

use crate::event::Event;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Scores(pub BTreeMap<String, f32>);

impl Scores {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, key: impl Into<String>, value: f32) {
        self.0.insert(key.into(), value);
    }

    pub fn merge(&mut self, other: Scores) {
        self.0.extend(other.0);
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RunResult {
    pub scenario: String,
    pub trace: Vec<Event>,
    pub scores: Scores,
}

/// Owner-side handle for a scenario. The actual driver lives in the
/// runtime crate and Python bindings; this type is here so the
/// language-independent vocabulary stays in core.
pub struct Scenario {
    pub name: String,
}

impl Scenario {
    pub fn new(name: impl Into<String>) -> Self {
        Self { name: name.into() }
    }
}
