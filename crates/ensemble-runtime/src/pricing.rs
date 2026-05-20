//! Token pricing lookup for the shipped backends.
//!
//! The table at `crates/ensemble-runtime/pricing.toml` is parsed once
//! at process start. Models in the table get a USD cost computed from
//! their token counts; models not in the table return `None`, so
//! callers record token totals only rather than fabricating a USD
//! number from a missing or stale entry.

use once_cell::sync::Lazy;
use serde::Deserialize;
use std::collections::HashMap;

#[derive(Debug, Deserialize, Clone, Copy)]
pub(crate) struct Pricing {
    pub input_per_million: f64,
    pub output_per_million: f64,
}

#[derive(Debug, Deserialize, Default)]
struct PricingTable {
    #[serde(default)]
    anthropic: HashMap<String, Pricing>,
    #[serde(default)]
    openai: HashMap<String, Pricing>,
}

static TABLE: Lazy<PricingTable> = Lazy::new(|| {
    let raw = include_str!("../pricing.toml");
    toml::from_str(raw).unwrap_or_default()
});

/// The provider whose price list to look up. vLLM endpoints serve
/// arbitrary adapters, so they reuse the OpenAI table when a request
/// happens to name an OpenAI model.
#[derive(Clone, Copy, Debug)]
pub enum Provider {
    Anthropic,
    OpenAI,
}

/// USD cost for a completion at the requested token counts, or
/// `None` when the model is not in the pricing table.
pub fn usd_for(
    provider: Provider,
    model: &str,
    input_tokens: u64,
    output_tokens: u64,
) -> Option<f64> {
    let map = match provider {
        Provider::Anthropic => &TABLE.anthropic,
        Provider::OpenAI => &TABLE.openai,
    };
    let entry = map.get(model)?;
    let usd = (input_tokens as f64) * entry.input_per_million / 1_000_000.0
        + (output_tokens as f64) * entry.output_per_million / 1_000_000.0;
    Some(usd)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_anthropic_model_resolves() {
        let usd = usd_for(Provider::Anthropic, "claude-sonnet-4-5", 1_000_000, 500_000);
        assert!(usd.is_some());
        let v = usd.unwrap();
        assert!((v - (3.0 + 0.5 * 15.0)).abs() < 1e-9, "got {v}");
    }

    #[test]
    fn unknown_model_returns_none() {
        assert!(usd_for(Provider::Anthropic, "claude-unmapped-99", 100, 100).is_none());
        assert!(usd_for(Provider::OpenAI, "ghost-model", 100, 100).is_none());
    }

    #[test]
    fn zero_tokens_yields_zero_usd_for_known_model() {
        let usd = usd_for(Provider::OpenAI, "gpt-5", 0, 0);
        assert_eq!(usd, Some(0.0));
    }
}
