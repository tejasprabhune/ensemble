use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use parking_lot::Mutex;
use serde::Serialize;
use serde_json::json;

use ensemble_core::error::ToolError;
use ensemble_runtime::{Tool, ToolRegistry};

use crate::state::PlankState;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn arg_str<'a>(
    args: &'a serde_json::Value,
    key: &str,
) -> Result<&'a str, ToolError> {
    args.get(key)
        .and_then(|v| v.as_str())
        .ok_or_else(|| ToolError::InvalidArgs(format!("missing string {key:?}")))
}

fn arg_i64(args: &serde_json::Value, key: &str) -> Result<i64, ToolError> {
    args.get(key)
        .and_then(|v| v.as_i64())
        .ok_or_else(|| ToolError::InvalidArgs(format!("missing integer {key:?}")))
}

#[derive(Serialize)]
struct OkPayload<T: Serialize> {
    ok: bool,
    data: T,
}

fn ok<T: Serialize>(data: T) -> serde_json::Value {
    serde_json::to_value(OkPayload { ok: true, data }).unwrap()
}

pub fn lookup_user(state: &Arc<Mutex<PlankState>>, tools: &mut ToolRegistry) {
    let state = state.clone();
    tools.register(Tool::new(
        "lookup_user",
        "Look up a user by id. Returns the user record or null.",
        json!({
            "type": "object",
            "properties": { "user_id": { "type": "string" } },
            "required": ["user_id"]
        }),
        move |args| {
            let user_id = arg_str(args, "user_id")?;
            let s = state.lock();
            let rec = s.lookup_user(user_id)?;
            s.audit("agent", "lookup_user", &json!({ "user_id": user_id }), now_ms())?;
            Ok(ok(rec))
        },
    ));
}

pub fn lookup_ticket(state: &Arc<Mutex<PlankState>>, tools: &mut ToolRegistry) {
    let state = state.clone();
    tools.register(Tool::new(
        "lookup_ticket",
        "Look up a ticket by id. Returns the ticket record or null.",
        json!({
            "type": "object",
            "properties": { "ticket_id": { "type": "string" } },
            "required": ["ticket_id"]
        }),
        move |args| {
            let ticket_id = arg_str(args, "ticket_id")?;
            let s = state.lock();
            let rec = s.lookup_ticket(ticket_id)?;
            s.audit("agent", "lookup_ticket", &json!({ "ticket_id": ticket_id }), now_ms())?;
            Ok(ok(rec))
        },
    ));
}

pub fn issue_refund(state: &Arc<Mutex<PlankState>>, tools: &mut ToolRegistry) {
    let state = state.clone();
    tools.register(Tool::new(
        "issue_refund",
        "Issue a refund to a user. Amounts are in whole cents.",
        json!({
            "type": "object",
            "properties": {
                "user_id": { "type": "string" },
                "amount_cents": { "type": "integer" },
                "reason": { "type": "string" }
            },
            "required": ["user_id", "amount_cents", "reason"]
        }),
        move |args| {
            let user_id = arg_str(args, "user_id")?;
            let amount = arg_i64(args, "amount_cents")?;
            let reason = arg_str(args, "reason")?;
            let s = state.lock();
            let prior = s.refund_count_for(user_id)?;
            if prior >= 1 {
                return Err(ToolError::Execution(
                    "user already has a refund this run; policy prevents double refunds".into(),
                ));
            }
            let refund_id = format!("r-{}-{}", user_id, now_ms());
            s.record_refund(&refund_id, user_id, amount, reason, now_ms())?;
            s.audit(
                "agent",
                "issue_refund",
                &json!({"user_id": user_id, "amount_cents": amount, "reason": reason}),
                now_ms(),
            )?;
            Ok(ok(json!({
                "refund_id": refund_id,
                "user_id": user_id,
                "amount_cents": amount,
            })))
        },
    ));
}

pub fn escalate(state: &Arc<Mutex<PlankState>>, tools: &mut ToolRegistry) {
    let state = state.clone();
    tools.register(Tool::new(
        "escalate",
        "Escalate a ticket to another team. Sets ticket status to 'escalated'.",
        json!({
            "type": "object",
            "properties": {
                "ticket_id": { "type": "string" },
                "to_team": { "type": "string" }
            },
            "required": ["ticket_id", "to_team"]
        }),
        move |args| {
            let ticket_id = arg_str(args, "ticket_id")?;
            let to_team = arg_str(args, "to_team")?;
            let s = state.lock();
            s.set_ticket_status(ticket_id, "escalated")?;
            s.audit(
                "agent",
                "escalate",
                &json!({"ticket_id": ticket_id, "to_team": to_team}),
                now_ms(),
            )?;
            Ok(ok(json!({
                "ticket_id": ticket_id,
                "to_team": to_team,
                "status": "escalated"
            })))
        },
    ));
}

pub fn search_kb(state: &Arc<Mutex<PlankState>>, tools: &mut ToolRegistry) {
    let state = state.clone();
    tools.register(Tool::new(
        "search_kb",
        "Search the knowledge base for relevant articles.",
        json!({
            "type": "object",
            "properties": { "query": { "type": "string" } },
            "required": ["query"]
        }),
        move |args| {
            let query = arg_str(args, "query")?;
            let s = state.lock();
            let results = s.search_kb(query)?;
            s.audit("agent", "search_kb", &json!({"query": query}), now_ms())?;
            Ok(ok(results))
        },
    ));
}

pub fn update_subscription(state: &Arc<Mutex<PlankState>>, tools: &mut ToolRegistry) {
    let state = state.clone();
    tools.register(Tool::new(
        "update_subscription",
        "Move a user to a different plan.",
        json!({
            "type": "object",
            "properties": {
                "user_id": { "type": "string" },
                "plan": { "type": "string" }
            },
            "required": ["user_id", "plan"]
        }),
        move |args| {
            let user_id = arg_str(args, "user_id")?;
            let plan = arg_str(args, "plan")?;
            let s = state.lock();
            s.set_subscription(user_id, plan)?;
            s.audit(
                "agent",
                "update_subscription",
                &json!({"user_id": user_id, "plan": plan}),
                now_ms(),
            )?;
            Ok(ok(json!({"user_id": user_id, "plan": plan})))
        },
    ));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn six_tools_register() {
        let (_state, tools) = crate::build();
        let names: Vec<String> = tools.schemas().into_iter().map(|s| s.name).collect();
        assert!(names.contains(&"lookup_user".to_string()));
        assert!(names.contains(&"lookup_ticket".to_string()));
        assert!(names.contains(&"issue_refund".to_string()));
        assert!(names.contains(&"escalate".to_string()));
        assert!(names.contains(&"search_kb".to_string()));
        assert!(names.contains(&"update_subscription".to_string()));
        assert_eq!(names.len(), 6);
    }

    #[test]
    fn lookup_user_returns_seeded_user() {
        let (_state, tools) = crate::build();
        let res = tools
            .dispatch("lookup_user", &json!({"user_id": "u-alice"}))
            .unwrap();
        assert_eq!(res["ok"], true);
        assert_eq!(res["data"]["name"], "Alice Chen");
    }

    #[test]
    fn double_refund_is_blocked() {
        let (_state, tools) = crate::build();
        tools
            .dispatch(
                "issue_refund",
                &json!({"user_id": "u-bob", "amount_cents": 999, "reason": "test"}),
            )
            .unwrap();
        let err = tools
            .dispatch(
                "issue_refund",
                &json!({"user_id": "u-bob", "amount_cents": 1, "reason": "again"}),
            )
            .unwrap_err();
        assert!(matches!(err, ToolError::Execution(_)));
    }
}
