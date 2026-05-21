//! Friendly wrapper for backend rejection errors. When the upstream
//! API returns 401 or 403 the most likely cause is a stale or missing
//! API key, and the user can fix it without reading the raw HTTP
//! body. The hint names the relevant env var and points at
//! `ensemble models list` so the user can verify the key status
//! without grepping the source.

use reqwest::StatusCode;

/// Provider label used to pick the right env-var name in the hint.
pub enum Provider {
    Anthropic,
    OpenAI,
}

impl Provider {
    fn env_var(&self) -> &'static str {
        match self {
            Provider::Anthropic => "ANTHROPIC_API_KEY",
            Provider::OpenAI => "OPENAI_API_KEY",
        }
    }
}

/// Format a rejection. Adds a sentence pointing at the env-var fix
/// for auth-shaped statuses; passes everything else through.
pub fn format_rejection(provider: Provider, status: StatusCode, body: &str) -> String {
    if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
        let var = provider.env_var();
        return format!(
            "{status}: the upstream rejected the request as unauthorized. \
             Check that {var} is set to a valid key (run `ensemble models list` \
             to see what is currently set). Upstream body: {body}"
        );
    }
    format!("{status}: {body}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unauthorized_anthropic_mentions_the_env_var() {
        let msg = format_rejection(
            Provider::Anthropic,
            StatusCode::UNAUTHORIZED,
            "{\"error\": \"invalid\"}",
        );
        assert!(msg.contains("ANTHROPIC_API_KEY"));
        assert!(msg.contains("ensemble models list"));
    }

    #[test]
    fn forbidden_openai_mentions_the_env_var() {
        let msg = format_rejection(Provider::OpenAI, StatusCode::FORBIDDEN, "no access");
        assert!(msg.contains("OPENAI_API_KEY"));
    }

    #[test]
    fn other_statuses_pass_through_verbatim() {
        let msg = format_rejection(Provider::OpenAI, StatusCode::INTERNAL_SERVER_ERROR, "boom");
        assert!(!msg.contains("ensemble models list"));
        assert!(msg.contains("boom"));
    }
}
