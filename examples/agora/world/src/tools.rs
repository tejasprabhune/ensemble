use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use parking_lot::Mutex;
use serde::Serialize;
use serde_json::json;

use ensemble_core::error::ToolError;
use ensemble_runtime::{Tool, ToolRegistry};

use crate::state::AgoraState;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn arg_str<'a>(args: &'a serde_json::Value, key: &str) -> Result<&'a str, ToolError> {
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

/// A deliberately slow tool that emits progress while it works.
/// Useful for exercising the progress-event + timeout machinery on
/// realistic workloads. Sleeps 100ms x `steps` (default 5) emitting
/// fractional progress after each chunk.
pub fn slow_billing_check(state: &Arc<Mutex<AgoraState>>, tools: &ToolRegistry) {
    let _ = state;
    tools.register(Tool::new_with_progress(
        "slow_billing_check",
        "Run a slow billing reconciliation. Emits progress fractions \
         while it works. Used to demonstrate progress events and \
         tool timeouts.",
        json!({
            "type": "object",
            "properties": {
                "user_id": { "type": "string" },
                "steps": { "type": "integer" }
            },
            "required": ["user_id"]
        }),
        move |args, emitter| {
            let user_id = arg_str(args, "user_id")?;
            let steps = args
                .get("steps")
                .and_then(|v| v.as_u64())
                .unwrap_or(5)
                .max(1);
            for i in 1..=steps {
                std::thread::sleep(std::time::Duration::from_millis(100));
                let fraction = i as f32 / steps as f32;
                emitter.emit(
                    fraction,
                    format!("scanned {i}/{steps} months for {user_id}"),
                );
            }
            Ok(ok(json!({
                "user_id": user_id,
                "reconciled_months": steps,
            })))
        },
    ));
}

pub fn open_ticket(state: &Arc<Mutex<AgoraState>>, tools: &ToolRegistry) {
    let state = state.clone();
    tools.register(Tool::new_with_diff(
        "open_ticket",
        "Open a support ticket on behalf of a user. Required for any \
         follow-up tool that takes a ticket_id.",
        json!({
            "type": "object",
            "properties": {
                "ticket_id": { "type": "string" },
                "user_id": { "type": "string" },
                "subject": { "type": "string" }
            },
            "required": ["ticket_id", "user_id", "subject"]
        }),
        move |args| {
            let ticket_id = arg_str(args, "ticket_id")?;
            let user_id = arg_str(args, "user_id")?;
            let subject = arg_str(args, "subject")?;
            let s = state.lock();
            let rec = s.open_ticket(ticket_id, user_id, subject, now_ms())?;
            s.audit(
                "user",
                "open_ticket",
                &json!({"ticket_id": ticket_id, "user_id": user_id, "subject": subject}),
                now_ms(),
            )?;
            let diff = json!([{
                "table": "tickets",
                "row_id": ticket_id,
                "field": "row",
                "old": null,
                "new": {"user_id": user_id, "subject": subject, "status": "open"}
            }]);
            Ok((ok(rec), diff))
        },
    ));
}

pub fn lookup_user(state: &Arc<Mutex<AgoraState>>, tools: &ToolRegistry) {
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
            s.audit(
                "agent",
                "lookup_user",
                &json!({ "user_id": user_id }),
                now_ms(),
            )?;
            Ok(ok(rec))
        },
    ));
}

pub fn lookup_ticket(state: &Arc<Mutex<AgoraState>>, tools: &ToolRegistry) {
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
            s.audit(
                "agent",
                "lookup_ticket",
                &json!({ "ticket_id": ticket_id }),
                now_ms(),
            )?;
            Ok(ok(rec))
        },
    ));
}

pub fn issue_refund(state: &Arc<Mutex<AgoraState>>, tools: &ToolRegistry) {
    let state = state.clone();
    tools.register(
        Tool::new_with_diff(
            "issue_refund",
            "Issue a refund to a user. Amounts are in whole cents. Acquires \
         the `billing_db` resource so concurrent refund attempts \
         serialize through the runtime.",
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
                let effect = ok(json!({
                    "refund_id": refund_id,
                    "user_id": user_id,
                    "amount_cents": amount,
                }));
                let diff = json!([{
                    "table": "refunds",
                    "row_id": refund_id,
                    "field": "row",
                    "old": null,
                    "new": {"user_id": user_id, "amount_cents": amount, "reason": reason}
                }]);
                Ok((effect, diff))
            },
        )
        .with_resources(vec!["billing_db".to_string()]),
    );
}

pub fn escalate(state: &Arc<Mutex<AgoraState>>, tools: &ToolRegistry) {
    let state = state.clone();
    tools.register(Tool::new_with_diff(
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
            let prior_status = s
                .lookup_ticket(ticket_id)?
                .map(|t| t.status)
                .unwrap_or_else(|| "missing".into());
            s.set_ticket_status(ticket_id, "escalated")?;
            s.audit(
                "agent",
                "escalate",
                &json!({"ticket_id": ticket_id, "to_team": to_team}),
                now_ms(),
            )?;
            let effect = ok(json!({
                "ticket_id": ticket_id,
                "to_team": to_team,
                "status": "escalated"
            }));
            let diff = json!([{
                "table": "tickets",
                "row_id": ticket_id,
                "field": "status",
                "old": prior_status,
                "new": "escalated"
            }]);
            Ok((effect, diff))
        },
    ));
}

pub fn search_kb(state: &Arc<Mutex<AgoraState>>, tools: &ToolRegistry) {
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

pub fn update_subscription(state: &Arc<Mutex<AgoraState>>, tools: &ToolRegistry) {
    let state = state.clone();
    tools.register(Tool::new_with_diff(
        "update_subscription",
        "Move a user to a different plan. Upserts: if the user has no \
         subscription row yet, the call inserts one.",
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
            let prior = s.set_subscription(user_id, plan)?;
            s.audit(
                "agent",
                "update_subscription",
                &json!({"user_id": user_id, "plan": plan}),
                now_ms(),
            )?;
            let effect = ok(json!({"user_id": user_id, "plan": plan}));
            let diff = json!([{
                "table": "subscriptions",
                "row_id": user_id,
                "field": "plan",
                "old": prior,
                "new": plan,
            }]);
            Ok((effect, diff))
        },
    ));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn all_tools_register() {
        let (_state, tools, _preds) = crate::build();
        let names: Vec<String> = tools.schemas().into_iter().map(|s| s.name).collect();
        for expected in [
            "open_ticket",
            "lookup_user",
            "lookup_ticket",
            "issue_refund",
            "escalate",
            "search_kb",
            "update_subscription",
            "slow_billing_check",
        ] {
            assert!(
                names.contains(&expected.to_string()),
                "missing tool {expected}"
            );
        }
        assert_eq!(names.len(), 8);
    }

    #[test]
    fn lookup_user_returns_seeded_user() {
        let (_state, tools, _preds) = crate::build();
        let res = tools
            .dispatch("lookup_user", &json!({"user_id": "u-alice"}))
            .unwrap();
        assert_eq!(res.effect["ok"], true);
        assert_eq!(res.effect["data"]["name"], "Alice Chen");
    }

    #[test]
    fn double_refund_is_blocked() {
        let (_state, tools, _preds) = crate::build();
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

    #[test]
    fn open_ticket_persists_to_state() {
        let (state, tools, _preds) = crate::build();
        tools
            .dispatch(
                "open_ticket",
                &json!({"ticket_id": "t-x1", "user_id": "u-alice", "subject": "hi"}),
            )
            .unwrap();
        let rec = state.lock().lookup_ticket("t-x1").unwrap();
        assert!(rec.is_some());
    }
}
