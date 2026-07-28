use std::path::Path;
use std::sync::Mutex;

use anyhow::{Context, Result};
use chrono::Utc;
use praxis_body_protocol::OperationStatus;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::Value;
use sha2::{Digest, Sha256};

#[derive(Debug, Clone)]
pub struct RequestRecord {
    pub args_digest: String,
    pub operation_id: String,
    pub capability: String,
    pub status: OperationStatus,
    pub result: Option<Value>,
}

pub enum Admission {
    New,
    Reused(RequestRecord),
    Conflict,
}

pub struct Journal {
    connection: Mutex<Connection>,
}

impl Journal {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("create body state {}", parent.display()))?;
        }
        let connection = Connection::open(path)
            .with_context(|| format!("open body journal {}", path.display()))?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA busy_timeout=5000;
             PRAGMA synchronous=FULL;
             CREATE TABLE IF NOT EXISTS requests (
                 request_id TEXT PRIMARY KEY,
                 args_digest TEXT NOT NULL,
                 operation_id TEXT NOT NULL,
                 capability TEXT NOT NULL,
                 status_json TEXT NOT NULL,
                 result_json TEXT,
                 created_at TEXT NOT NULL,
                 updated_at TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS counters (
                 name TEXT PRIMARY KEY,
                 value INTEGER NOT NULL
             );",
        )?;
        Ok(Self {
            connection: Mutex::new(connection),
        })
    }

    pub fn digest(capability: &str, args: &Value) -> Result<String> {
        let canonical = serde_json::to_vec(&(capability, args))?;
        Ok(hex::encode(Sha256::digest(canonical)))
    }

    pub fn admit(
        &self,
        request_id: &str,
        args_digest: &str,
        operation_id: &str,
        capability: &str,
    ) -> Result<Admission> {
        let mut conn = self.connection.lock().expect("journal mutex poisoned");
        let transaction = conn.transaction()?;
        let existing = read_record(&transaction, request_id)?;
        let admission = match existing {
            Some(record) if record.args_digest == args_digest => Admission::Reused(record),
            Some(_) => Admission::Conflict,
            None => {
                let now = Utc::now().to_rfc3339();
                transaction.execute(
                    "INSERT INTO requests
                     (request_id, args_digest, operation_id, capability, status_json, created_at, updated_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?6)",
                    params![
                        request_id,
                        args_digest,
                        operation_id,
                        capability,
                        serde_json::to_string(&OperationStatus::Admitted)?,
                        now,
                    ],
                )?;
                Admission::New
            }
        };
        transaction.commit()?;
        Ok(admission)
    }

    pub fn lookup(&self, request_id: &str) -> Result<Option<RequestRecord>> {
        let connection = self.connection.lock().expect("journal mutex poisoned");
        read_record(&connection, request_id)
    }

    pub fn finish(&self, request_id: &str, status: OperationStatus, result: &Value) -> Result<()> {
        let conn = self.connection.lock().expect("journal mutex poisoned");
        conn.execute(
            "UPDATE requests SET status_json=?2, result_json=?3, updated_at=?4 WHERE request_id=?1",
            params![
                request_id,
                serde_json::to_string(&status)?,
                serde_json::to_string(result)?,
                Utc::now().to_rfc3339(),
            ],
        )?;
        Ok(())
    }

    pub fn set_status(&self, request_id: &str, status: OperationStatus) -> Result<()> {
        let conn = self.connection.lock().expect("journal mutex poisoned");
        conn.execute(
            "UPDATE requests SET status_json=?2, updated_at=?3 WHERE request_id=?1",
            params![
                request_id,
                serde_json::to_string(&status)?,
                Utc::now().to_rfc3339(),
            ],
        )?;
        Ok(())
    }

    pub fn next_seq(&self, name: &str) -> Result<u64> {
        let mut conn = self.connection.lock().expect("journal mutex poisoned");
        let transaction = conn.transaction()?;
        transaction.execute(
            "INSERT INTO counters(name, value) VALUES (?1, 0)
             ON CONFLICT(name) DO NOTHING",
            params![name],
        )?;
        transaction.execute(
            "UPDATE counters SET value=value+1 WHERE name=?1",
            params![name],
        )?;
        let value: i64 = transaction.query_row(
            "SELECT value FROM counters WHERE name=?1",
            params![name],
            |row| row.get(0),
        )?;
        transaction.commit()?;
        Ok(value as u64)
    }
}

fn read_record(conn: &Connection, request_id: &str) -> Result<Option<RequestRecord>> {
    conn.query_row(
        "SELECT request_id, args_digest, operation_id, capability, status_json, result_json
         FROM requests WHERE request_id=?1",
        params![request_id],
        |row| {
            let status_raw: String = row.get(4)?;
            let result_raw: Option<String> = row.get(5)?;
            Ok((
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                status_raw,
                result_raw,
            ))
        },
    )
    .optional()?
    .map(
        |(args_digest, operation_id, capability, status_raw, result_raw)| {
            Ok(RequestRecord {
                args_digest,
                operation_id,
                capability,
                status: serde_json::from_str(&status_raw)?,
                result: result_raw
                    .map(|raw| serde_json::from_str(&raw))
                    .transpose()?,
            })
        },
    )
    .transpose()
}

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;

    #[test]
    fn same_request_is_reused_and_changed_args_conflict() {
        let path = std::env::temp_dir().join(format!("praxis-body-{}.db", Uuid::new_v4()));
        let journal = Journal::open(&path).unwrap();
        assert!(matches!(
            journal.admit("r", "a", "op", "fs.write").unwrap(),
            Admission::New
        ));
        assert!(matches!(
            journal.admit("r", "a", "op", "fs.write").unwrap(),
            Admission::Reused(_)
        ));
        assert!(matches!(
            journal.admit("r", "b", "op", "fs.write").unwrap(),
            Admission::Conflict
        ));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn sequence_survives_calls() {
        let path = std::env::temp_dir().join(format!("praxis-body-seq-{}.db", Uuid::new_v4()));
        let journal = Journal::open(&path).unwrap();
        assert_eq!(journal.next_seq("out").unwrap(), 1);
        assert_eq!(journal.next_seq("out").unwrap(), 2);
        let _ = std::fs::remove_file(path);
    }
}
