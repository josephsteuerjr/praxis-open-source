use std::path::Path;
use std::sync::Mutex;

use anyhow::{Context, Result};
use chrono::Utc;
use praxis_body_protocol::Envelope;
use rusqlite::{Connection, params};

pub const TO_DEVICE: &str = "to_device";
pub const TO_CONTROLLER: &str = "to_controller";
const MAX_PENDING_PAGE: usize = 256;

pub struct Spool {
    connection: Mutex<Connection>,
}

impl Spool {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("create bridge state {}", parent.display()))?;
        }
        let mut connection = Connection::open(path)
            .with_context(|| format!("open bridge spool {}", path.display()))?;
        connection.execute_batch(
            "PRAGMA journal_mode=DELETE;
             PRAGMA synchronous=FULL;
             CREATE TABLE IF NOT EXISTS frames (
                 message_id TEXT PRIMARY KEY,
                 device_id TEXT NOT NULL,
                 direction TEXT NOT NULL,
                 seq INTEGER NOT NULL,
                 payload TEXT NOT NULL,
                 acknowledged INTEGER NOT NULL DEFAULT 0,
                 created_at TEXT NOT NULL
             );
             CREATE INDEX IF NOT EXISTS frames_pending
                 ON frames(device_id, direction, acknowledged, seq);
             CREATE TABLE IF NOT EXISTS counters (
                 device_id TEXT NOT NULL,
                 direction TEXT NOT NULL,
                 value INTEGER NOT NULL,
                 PRIMARY KEY(device_id, direction)
             );
             CREATE TABLE IF NOT EXISTS responses (
                 device_id TEXT NOT NULL,
                 request_id TEXT NOT NULL,
                 operation_id TEXT,
                 frame_type TEXT NOT NULL,
                 terminal INTEGER NOT NULL,
                 payload TEXT NOT NULL,
                 updated_at TEXT NOT NULL,
                 PRIMARY KEY(device_id, request_id)
             );",
        )?;
        let response_pk = {
            let mut statement = connection.prepare("PRAGMA table_info(responses)")?;
            statement
                .query_map([], |row| {
                    Ok((row.get::<_, String>(1)?, row.get::<_, i64>(5)?))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?
                .into_iter()
                .filter(|(_, position)| *position > 0)
                .collect::<Vec<_>>()
        };
        if response_pk != vec![("device_id".into(), 1), ("request_id".into(), 2)] {
            let transaction = connection.transaction()?;
            transaction.execute_batch(
                "CREATE TABLE responses_v2 (
                     device_id TEXT NOT NULL,
                     request_id TEXT NOT NULL,
                     operation_id TEXT,
                     frame_type TEXT NOT NULL,
                     terminal INTEGER NOT NULL,
                     payload TEXT NOT NULL,
                     updated_at TEXT NOT NULL,
                     PRIMARY KEY(device_id, request_id)
                 );
                 INSERT OR REPLACE INTO responses_v2
                     (device_id, request_id, operation_id, frame_type, terminal, payload, updated_at)
                     SELECT device_id, request_id, operation_id, frame_type, terminal, payload, updated_at
                     FROM responses;
                 DROP TABLE responses;
                 ALTER TABLE responses_v2 RENAME TO responses;",
            )?;
            transaction.commit()?;
        }
        // Rows acknowledged by a peer are no longer replay evidence. Older versions retained
        // them forever; remove that legacy tail on open and bound genuinely offline replay.
        connection.execute(
            "DELETE FROM frames
             WHERE acknowledged=1 OR datetime(created_at) < datetime('now', '-30 days')",
            [],
        )?;
        connection.execute(
            "DELETE FROM responses
             WHERE (terminal=1 AND datetime(updated_at) < datetime('now', '-30 days'))
                OR (terminal=0 AND datetime(updated_at) < datetime('now', '-7 days'))",
            [],
        )?;
        normalize_pending_sequences(&mut connection)?;
        Ok(Self {
            connection: Mutex::new(connection),
        })
    }

    pub fn store(&self, direction: &str, envelope: &Envelope, payload: &str) -> Result<()> {
        let conn = self.connection.lock().expect("spool mutex poisoned");
        conn.execute(
            "INSERT INTO frames
             (message_id, device_id, direction, seq, payload, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(message_id) DO NOTHING",
            params![
                envelope.message_id.to_string(),
                envelope.device_id,
                direction,
                envelope.seq as i64,
                payload,
                Utc::now().to_rfc3339(),
            ],
        )?;
        Ok(())
    }

    pub fn acknowledge(&self, device_id: &str, direction: &str, through_seq: u64) -> Result<usize> {
        let conn = self.connection.lock().expect("spool mutex poisoned");
        let changed = conn.execute(
            "DELETE FROM frames
             WHERE device_id=?1 AND direction=?2 AND seq<=?3",
            params![device_id, direction, through_seq as i64],
        )?;
        Ok(changed)
    }

    pub fn pending_page(
        &self,
        device_id: &str,
        direction: &str,
        after_seq: u64,
        limit: usize,
    ) -> Result<Vec<(u64, String)>> {
        let conn = self.connection.lock().expect("spool mutex poisoned");
        let mut statement = conn.prepare(
            "SELECT seq, payload FROM frames
             WHERE device_id=?1 AND direction=?2 AND acknowledged=0 AND seq>?3
             ORDER BY seq ASC
             LIMIT ?4",
        )?;
        let rows = statement
            .query_map(
                params![
                    device_id,
                    direction,
                    i64::try_from(after_seq).context("bridge pending cursor overflow")?,
                    i64::try_from(limit.clamp(1, MAX_PENDING_PAGE))?
                ],
                |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows.into_iter()
            .map(|(seq, payload)| {
                Ok((
                    u64::try_from(seq).context("negative bridge pending sequence")?,
                    payload,
                ))
            })
            .collect()
    }

    pub fn next_seq(&self, device_id: &str, direction: &str) -> Result<u64> {
        let mut conn = self.connection.lock().expect("spool mutex poisoned");
        let transaction = conn.transaction()?;
        transaction.execute(
            "INSERT INTO counters(device_id, direction, value) VALUES (?1, ?2, 0)
             ON CONFLICT(device_id, direction) DO NOTHING",
            params![device_id, direction],
        )?;
        transaction.execute(
            "UPDATE counters SET value=value+1 WHERE device_id=?1 AND direction=?2",
            params![device_id, direction],
        )?;
        let value: i64 = transaction.query_row(
            "SELECT value FROM counters WHERE device_id=?1 AND direction=?2",
            params![device_id, direction],
            |row| row.get(0),
        )?;
        transaction.commit()?;
        Ok(value as u64)
    }

    pub fn record_response(
        &self,
        request_id: &str,
        device_id: &str,
        operation_id: Option<&str>,
        frame_type: &str,
        terminal: bool,
        payload: &str,
    ) -> Result<()> {
        let conn = self.connection.lock().expect("spool mutex poisoned");
        conn.execute(
            "INSERT INTO responses
             (device_id, request_id, operation_id, frame_type, terminal, payload, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
             ON CONFLICT(device_id, request_id) DO UPDATE SET
                 operation_id=excluded.operation_id,
                 frame_type=excluded.frame_type,
                 terminal=excluded.terminal,
                 payload=excluded.payload,
                 updated_at=excluded.updated_at
             WHERE (responses.operation_id IS NULL
                    OR responses.operation_id=excluded.operation_id)
               AND (responses.terminal=0
                    OR (responses.frame_type='error'
                        AND excluded.frame_type IN ('result', 'cancelled')
                        AND excluded.terminal=1))",
            params![
                device_id,
                request_id,
                operation_id,
                frame_type,
                terminal as i32,
                payload,
                Utc::now().to_rfc3339(),
            ],
        )?;
        conn.execute(
            "DELETE FROM responses
             WHERE (terminal=1 AND datetime(updated_at) < datetime('now', '-30 days'))
                OR (terminal=0 AND datetime(updated_at) < datetime('now', '-7 days'))",
            [],
        )?;
        Ok(())
    }

    pub fn response(&self, request_id: &str, device_id: &str) -> Result<Option<String>> {
        use rusqlite::OptionalExtension;
        let conn = self.connection.lock().expect("spool mutex poisoned");
        Ok(conn
            .query_row(
                "SELECT payload FROM responses WHERE request_id=?1 AND device_id=?2",
                params![request_id, device_id],
                |row| row.get(0),
            )
            .optional()?)
    }

    #[cfg(test)]
    pub fn pending(&self, device_id: &str, direction: &str) -> Result<Vec<String>> {
        let mut rows = Vec::new();
        let mut after_seq = 0;
        loop {
            let page = self.pending_page(device_id, direction, after_seq, MAX_PENDING_PAGE)?;
            if page.is_empty() {
                return Ok(rows);
            }
            for (seq, payload) in page {
                after_seq = seq;
                rows.push(payload);
            }
        }
    }

    #[cfg(test)]
    pub fn pending_count(&self, device_id: &str, direction: &str) -> usize {
        self.pending(device_id, direction).unwrap().len()
    }
}

fn normalize_pending_sequences(connection: &mut Connection) -> Result<()> {
    // Older bridge builds trusted producer-owned sequences, including duplicates, and did not
    // advance a bridge counter for TO_CONTROLLER. Rewrite the complete pending prefix and its
    // JSON payload in one transaction before any peer can connect. New canonical frames then
    // sort strictly after every replayed row, which makes cumulative acknowledgements sound.
    let transaction = connection.transaction()?;
    transaction.execute("DROP INDEX IF EXISTS frames_sequence_unique", [])?;
    let rows = {
        let mut statement = transaction.prepare(
            "SELECT rowid, device_id, direction, payload
             FROM frames
             ORDER BY device_id, direction, rowid",
        )?;
        statement
            .query_map([], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?
    };
    let mut group: Option<(String, String)> = None;
    let mut sequence = 0u64;
    for (rowid, device_id, direction, payload) in rows {
        let key = (device_id.clone(), direction.clone());
        if group.as_ref() != Some(&key) {
            group = Some(key);
            sequence = 1;
        } else {
            sequence = sequence
                .checked_add(1)
                .context("bridge pending sequence overflow")?;
        }
        let mut envelope: Envelope = serde_json::from_str(&payload)
            .with_context(|| format!("decode pending bridge frame row {rowid}"))?;
        if envelope.device_id != device_id {
            anyhow::bail!(
                "pending bridge frame row {rowid} belongs to {}, payload names {}",
                device_id,
                envelope.device_id
            );
        }
        envelope.seq = sequence;
        let payload = serde_json::to_string(&envelope)?;
        transaction.execute(
            "UPDATE frames SET seq=?2, payload=?3 WHERE rowid=?1",
            params![rowid, sequence as i64, payload],
        )?;
    }
    transaction.execute(
        "INSERT INTO counters(device_id, direction, value)
         SELECT device_id, direction, MAX(seq)
         FROM frames
         GROUP BY device_id, direction
         ON CONFLICT(device_id, direction) DO UPDATE SET
             value=MAX(counters.value, excluded.value)",
        [],
    )?;
    transaction.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS frames_sequence_unique
         ON frames(device_id, direction, seq)",
        [],
    )?;
    transaction.commit()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Duration, Utc};
    use praxis_body_protocol::Frame;
    use uuid::Uuid;

    #[test]
    fn ack_is_cumulative_per_device_and_direction() {
        let path = std::env::temp_dir().join(format!("praxis-spool-{}.db", Uuid::new_v4()));
        let spool = Spool::open(&path).unwrap();
        for seq in 1..=3 {
            let envelope = Envelope::new(
                "pc",
                seq,
                Frame::Heartbeat {
                    at: Utc::now(),
                    active_operations: None,
                },
            );
            spool
                .store(
                    TO_DEVICE,
                    &envelope,
                    &serde_json::to_string(&envelope).unwrap(),
                )
                .unwrap();
        }
        assert_eq!(spool.pending_count("pc", TO_DEVICE), 3);
        spool.acknowledge("pc", TO_DEVICE, 2).unwrap();
        assert_eq!(spool.pending_count("pc", TO_DEVICE), 1);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn pending_pages_are_bounded_and_keyset_ordered() {
        let path = std::env::temp_dir().join(format!("praxis-spool-{}.db", Uuid::new_v4()));
        let spool = Spool::open(&path).unwrap();
        for seq in 1..=300 {
            let envelope = Envelope::new(
                "pc",
                seq,
                Frame::Heartbeat {
                    at: Utc::now(),
                    active_operations: None,
                },
            );
            spool
                .store(
                    TO_DEVICE,
                    &envelope,
                    &serde_json::to_string(&envelope).unwrap(),
                )
                .unwrap();
        }

        let first = spool.pending_page("pc", TO_DEVICE, 0, usize::MAX).unwrap();
        assert_eq!(first.len(), MAX_PENDING_PAGE);
        assert_eq!(first.first().map(|row| row.0), Some(1));
        assert_eq!(first.last().map(|row| row.0), Some(256));

        let second = spool
            .pending_page("pc", TO_DEVICE, 256, usize::MAX)
            .unwrap();
        assert_eq!(second.len(), 44);
        assert_eq!(second.first().map(|row| row.0), Some(257));
        assert_eq!(second.last().map(|row| row.0), Some(300));
        assert!(
            spool
                .pending_page("pc", TO_DEVICE, 300, 1)
                .unwrap()
                .is_empty()
        );

        drop(spool);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn responses_are_isolated_by_device() {
        let path = std::env::temp_dir().join(format!("praxis-spool-{}.db", Uuid::new_v4()));
        let spool = Spool::open(&path).unwrap();
        spool
            .record_response("same", "pc-a", Some("op-a"), "result", true, "a")
            .unwrap();
        spool
            .record_response("same", "pc-b", Some("op-b"), "result", true, "b")
            .unwrap();
        assert_eq!(
            spool.response("same", "pc-a").unwrap().as_deref(),
            Some("a")
        );
        assert_eq!(
            spool.response("same", "pc-b").unwrap().as_deref(),
            Some("b")
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn terminal_response_obeys_operation_and_outcome_priority() {
        let path = std::env::temp_dir().join(format!("praxis-spool-{}.db", Uuid::new_v4()));
        let spool = Spool::open(&path).unwrap();
        spool
            .record_response("request", "pc", Some("op"), "accepted", false, "accepted")
            .unwrap();
        spool
            .record_response("request", "pc", Some("other"), "result", true, "wrong")
            .unwrap();
        spool
            .record_response("request", "pc", None, "error", true, "uncorrelated")
            .unwrap();
        assert_eq!(
            spool.response("request", "pc").unwrap().as_deref(),
            Some("accepted")
        );
        spool
            .record_response("request", "pc", Some("op"), "result", true, "result")
            .unwrap();
        spool
            .record_response("request", "pc", Some("op"), "progress", false, "stale")
            .unwrap();
        assert_eq!(
            spool.response("request", "pc").unwrap().as_deref(),
            Some("result")
        );
        spool
            .record_response("recovery", "pc", Some("op"), "error", true, "temporary")
            .unwrap();
        spool
            .record_response("recovery", "pc", Some("op"), "progress", false, "stale")
            .unwrap();
        assert_eq!(
            spool.response("recovery", "pc").unwrap().as_deref(),
            Some("temporary")
        );
        spool
            .record_response("recovery", "pc", Some("other"), "result", true, "wrong")
            .unwrap();
        assert_eq!(
            spool.response("recovery", "pc").unwrap().as_deref(),
            Some("temporary")
        );
        spool
            .record_response("recovery", "pc", Some("op"), "result", true, "recovered")
            .unwrap();
        spool
            .record_response("recovery", "pc", Some("op"), "error", true, "late-error")
            .unwrap();
        assert_eq!(
            spool.response("recovery", "pc").unwrap().as_deref(),
            Some("recovered")
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn startup_resequences_duplicate_legacy_rows_and_enforces_unique_sequences() {
        let path = std::env::temp_dir().join(format!("praxis-spool-{}.db", Uuid::new_v4()));
        let connection = Connection::open(&path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE frames (
                     message_id TEXT PRIMARY KEY,
                     device_id TEXT NOT NULL,
                     direction TEXT NOT NULL,
                     seq INTEGER NOT NULL,
                     payload TEXT NOT NULL,
                     acknowledged INTEGER NOT NULL DEFAULT 0,
                     created_at TEXT NOT NULL
                 );",
            )
            .unwrap();
        let mut insertion_order = Vec::new();
        for index in 0..2 {
            let legacy = Envelope::new(
                "pc",
                47,
                Frame::Heartbeat {
                    at: Utc::now(),
                    active_operations: None,
                },
            );
            connection
                .execute(
                    "INSERT INTO frames(message_id, device_id, direction, seq, payload, created_at)
                     VALUES (?1, 'pc', ?2, 47, ?3, ?4)",
                    params![
                        legacy.message_id.to_string(),
                        TO_CONTROLLER,
                        serde_json::to_string(&legacy).unwrap(),
                        (Utc::now() + Duration::seconds(index)).to_rfc3339(),
                    ],
                )
                .unwrap();
            insertion_order.push(legacy.message_id);
        }
        drop(connection);

        let reopened = Spool::open(&path).unwrap();
        let pending = reopened.pending("pc", TO_CONTROLLER).unwrap();
        let sequences = pending
            .iter()
            .map(|raw| serde_json::from_str::<Envelope>(raw).unwrap().seq)
            .collect::<Vec<_>>();
        assert_eq!(sequences, vec![1, 2]);
        assert_eq!(reopened.next_seq("pc", TO_CONTROLLER).unwrap(), 3);
        let conflict = Envelope::new(
            "pc",
            2,
            Frame::Heartbeat {
                at: Utc::now(),
                active_operations: None,
            },
        );
        assert!(
            reopened
                .store(
                    TO_CONTROLLER,
                    &conflict,
                    &serde_json::to_string(&conflict).unwrap(),
                )
                .is_err()
        );
        let rollback_clock = Envelope::new(
            "pc",
            3,
            Frame::Heartbeat {
                at: Utc::now(),
                active_operations: None,
            },
        );
        reopened
            .store(
                TO_CONTROLLER,
                &rollback_clock,
                &serde_json::to_string(&rollback_clock).unwrap(),
            )
            .unwrap();
        insertion_order.push(rollback_clock.message_id);
        drop(reopened);

        let connection = Connection::open(&path).unwrap();
        connection
            .execute(
                "UPDATE frames SET created_at=?2 WHERE message_id=?1",
                params![
                    rollback_clock.message_id.to_string(),
                    (Utc::now() - Duration::hours(1)).to_rfc3339()
                ],
            )
            .unwrap();
        drop(connection);
        let reopened = Spool::open(&path).unwrap();
        let replay = reopened
            .pending("pc", TO_CONTROLLER)
            .unwrap()
            .iter()
            .map(|raw| serde_json::from_str::<Envelope>(raw).unwrap())
            .collect::<Vec<_>>();
        let sequences = replay
            .iter()
            .map(|envelope| envelope.seq)
            .collect::<Vec<_>>();
        assert_eq!(sequences, vec![1, 2, 3]);
        assert_eq!(
            replay
                .iter()
                .map(|envelope| envelope.message_id)
                .collect::<Vec<_>>(),
            insertion_order
        );
        assert_eq!(reopened.next_seq("pc", TO_CONTROLLER).unwrap(), 4);
        drop(reopened);
        let _ = std::fs::remove_file(path);
    }
}
