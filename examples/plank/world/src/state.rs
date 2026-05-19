use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use ensemble_core::error::ToolError;

/// In-memory SQLite database holding all of Plank's state. Each world
/// instance owns its own connection. Snapshot/restore is backed by
/// `serialize`/`deserialize` if needed; for the MVP we just dump rows
/// to JSON, which is enough for the trace viewer.
pub struct PlankState {
    pub conn: Connection,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct UserRecord {
    pub id: String,
    pub name: String,
    pub email: String,
    pub plan: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TicketRecord {
    pub id: String,
    pub user_id: String,
    pub subject: String,
    pub status: String,
    pub opened_at: i64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct KBArticle {
    pub id: String,
    pub title: String,
    pub body: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Subscription {
    pub user_id: String,
    pub plan: String,
}

impl PlankState {
    pub fn new() -> Self {
        let conn = Connection::open_in_memory().expect("sqlite open in memory");
        Self::install_schema(&conn);
        Self { conn }
    }

    fn install_schema(conn: &Connection) {
        conn.execute_batch(
            "
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                plan TEXT NOT NULL
            );
            CREATE TABLE tickets (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_at INTEGER NOT NULL
            );
            CREATE TABLE subscriptions (
                user_id TEXT PRIMARY KEY,
                plan TEXT NOT NULL
            );
            CREATE TABLE kb (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                who TEXT NOT NULL,
                action TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE refunds (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                reason TEXT NOT NULL,
                ts INTEGER NOT NULL
            );
            ",
        )
        .expect("install schema");
    }

    /// Seed a small set of demo users, tickets, subscriptions, and
    /// knowledge-base articles. Deterministic across runs.
    pub fn seed_default() -> Self {
        let s = Self::new();
        let users = [
            ("u-alice", "Alice Chen", "alice@example.com", "team"),
            ("u-bob", "Bob Diaz", "bob@example.com", "free"),
            ("u-carol", "Carol Ng", "carol@example.com", "enterprise"),
        ];
        for (id, name, email, plan) in users {
            s.conn
                .execute(
                    "INSERT INTO users (id, name, email, plan) VALUES (?, ?, ?, ?)",
                    params![id, name, email, plan],
                )
                .unwrap();
            s.conn
                .execute(
                    "INSERT INTO subscriptions (user_id, plan) VALUES (?, ?)",
                    params![id, plan],
                )
                .unwrap();
        }
        let articles = [
            ("kb-1", "How refunds work", "Refunds are processed within 5 business days."),
            ("kb-2", "Plan changes", "You can change plans at any time from settings."),
            ("kb-3", "Escalation policy", "Tier-2 handles billing disputes over $200."),
        ];
        for (id, title, body) in articles {
            s.conn
                .execute(
                    "INSERT INTO kb (id, title, body) VALUES (?, ?, ?)",
                    params![id, title, body],
                )
                .unwrap();
        }
        s
    }

    pub fn lookup_user(&self, user_id: &str) -> Result<Option<UserRecord>, ToolError> {
        self.conn
            .query_row(
                "SELECT id, name, email, plan FROM users WHERE id = ?",
                params![user_id],
                |row| {
                    Ok(UserRecord {
                        id: row.get(0)?,
                        name: row.get(1)?,
                        email: row.get(2)?,
                        plan: row.get(3)?,
                    })
                },
            )
            .optional()
            .map_err(|e| ToolError::Execution(e.to_string()))
    }

    pub fn lookup_ticket(&self, ticket_id: &str) -> Result<Option<TicketRecord>, ToolError> {
        self.conn
            .query_row(
                "SELECT id, user_id, subject, status, opened_at FROM tickets WHERE id = ?",
                params![ticket_id],
                |row| {
                    Ok(TicketRecord {
                        id: row.get(0)?,
                        user_id: row.get(1)?,
                        subject: row.get(2)?,
                        status: row.get(3)?,
                        opened_at: row.get(4)?,
                    })
                },
            )
            .optional()
            .map_err(|e| ToolError::Execution(e.to_string()))
    }

    pub fn open_ticket(
        &self,
        ticket_id: &str,
        user_id: &str,
        subject: &str,
        now_ms: i64,
    ) -> Result<TicketRecord, ToolError> {
        self.conn
            .execute(
                "INSERT INTO tickets (id, user_id, subject, status, opened_at) VALUES (?, ?, ?, 'open', ?)",
                params![ticket_id, user_id, subject, now_ms],
            )
            .map_err(|e| ToolError::Execution(e.to_string()))?;
        Ok(TicketRecord {
            id: ticket_id.into(),
            user_id: user_id.into(),
            subject: subject.into(),
            status: "open".into(),
            opened_at: now_ms,
        })
    }

    pub fn set_ticket_status(&self, ticket_id: &str, status: &str) -> Result<(), ToolError> {
        self.conn
            .execute(
                "UPDATE tickets SET status = ? WHERE id = ?",
                params![status, ticket_id],
            )
            .map_err(|e| ToolError::Execution(e.to_string()))?;
        Ok(())
    }

    pub fn record_refund(
        &self,
        refund_id: &str,
        user_id: &str,
        amount_cents: i64,
        reason: &str,
        now_ms: i64,
    ) -> Result<(), ToolError> {
        self.conn
            .execute(
                "INSERT INTO refunds (id, user_id, amount_cents, reason, ts) VALUES (?, ?, ?, ?, ?)",
                params![refund_id, user_id, amount_cents, reason, now_ms],
            )
            .map_err(|e| ToolError::Execution(e.to_string()))?;
        Ok(())
    }

    pub fn search_kb(&self, query: &str) -> Result<Vec<KBArticle>, ToolError> {
        let needle = format!("%{}%", query.to_lowercase());
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, title, body FROM kb \
                 WHERE LOWER(title) LIKE ?1 OR LOWER(body) LIKE ?1 \
                 ORDER BY id",
            )
            .map_err(|e| ToolError::Execution(e.to_string()))?;
        let rows = stmt
            .query_map(params![needle], |row| {
                Ok(KBArticle {
                    id: row.get(0)?,
                    title: row.get(1)?,
                    body: row.get(2)?,
                })
            })
            .map_err(|e| ToolError::Execution(e.to_string()))?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r.map_err(|e| ToolError::Execution(e.to_string()))?);
        }
        Ok(out)
    }

    pub fn set_subscription(&self, user_id: &str, plan: &str) -> Result<(), ToolError> {
        self.conn
            .execute(
                "UPDATE subscriptions SET plan = ? WHERE user_id = ?",
                params![plan, user_id],
            )
            .map_err(|e| ToolError::Execution(e.to_string()))?;
        Ok(())
    }

    pub fn audit(
        &self,
        who: &str,
        action: &str,
        payload: &serde_json::Value,
        now_ms: i64,
    ) -> Result<(), ToolError> {
        self.conn
            .execute(
                "INSERT INTO audit_log (ts, who, action, payload) VALUES (?, ?, ?, ?)",
                params![now_ms, who, action, payload.to_string()],
            )
            .map_err(|e| ToolError::Execution(e.to_string()))?;
        Ok(())
    }

    pub fn refund_count_for(&self, user_id: &str) -> Result<i64, ToolError> {
        self.conn
            .query_row(
                "SELECT COUNT(*) FROM refunds WHERE user_id = ?",
                params![user_id],
                |row| row.get(0),
            )
            .map_err(|e| ToolError::Execution(e.to_string()))
    }
}

impl Default for PlankState {
    fn default() -> Self {
        Self::new()
    }
}
