//! Validates a batch of reported events and stores it.
//! 보고된 이벤트 묶음을 검사하고 저장합니다.

use crate::auth::{self, AuthedUser};
use crate::db::Db;
use crate::timeutil;
use crate::usage::{self, IncomingEvent, Labels, UsageRow};
use rusqlite::params;
use serde::{Deserialize, Serialize};

/// Events older than 2020-01-01 or more than two days in the future are treated as clock errors.
/// 2020-01-01보다 이전이거나 이틀 넘게 미래인 이벤트는 시계 오류로 봅니다.
const MIN_TIMESTAMP: i64 = 1_577_836_800;
const MAX_FUTURE_SECS: i64 = 2 * 24 * 60 * 60;

#[derive(Debug, Serialize, PartialEq)]
pub struct IngestSummary {
    pub received: usize,
    pub inserted: usize,
    pub duplicates: usize,
    pub rejected: Vec<Rejected>,
}

/// An event that was not stored, with its position in the request and the reason.
/// 저장하지 않은 이벤트입니다. 요청 안에서의 위치와 이유를 함께 알려 줍니다.
#[derive(Debug, Serialize, PartialEq)]
pub struct Rejected {
    pub index: usize,
    pub id: Option<String>,
    pub error: String,
}

#[derive(Debug, PartialEq)]
pub enum IngestError {
    BadRequest(String),
    Storage(String),
}

#[derive(Deserialize)]
struct Envelope {
    events: Vec<serde_json::Value>,
}

struct Prepared {
    id: String,
    ts: i64,
    labels: Labels,
    rows: Vec<UsageRow>,
}

/// Store a batch for one user. Invalid events are reported back and do not block valid ones, so
/// one bad record cannot stall a collector's queue.
/// 한 사용자의 이벤트 묶음을 저장합니다. 잘못된 이벤트는 이유와 함께 돌려주되 올바른 이벤트의
/// 저장을 막지 않으므로, 잘못된 기록 하나 때문에 수집 프로세스의 대기열이 멈추지 않습니다.
pub fn ingest(
    db: &Db,
    user: &AuthedUser,
    body: &[u8],
    max_events: usize,
) -> Result<IngestSummary, IngestError> {
    let envelope: Envelope = serde_json::from_slice(body).map_err(|e| {
        IngestError::BadRequest(format!(
            "request body must be JSON shaped like {{\"events\": [...]}}: {e}"
        ))
    })?;
    if envelope.events.len() > max_events {
        return Err(IngestError::BadRequest(format!(
            "too many events in one request: {} (limit {max_events})",
            envelope.events.len()
        )));
    }

    let now = timeutil::now_unix();
    let received = envelope.events.len();
    let mut prepared = Vec::with_capacity(received);
    let mut rejected = Vec::new();
    for (index, value) in envelope.events.into_iter().enumerate() {
        let id = value
            .get("id")
            .and_then(|v| v.as_str())
            .map(|s| s.chars().take(usage::MAX_ID_LEN).collect());
        match prepare(value, now) {
            Ok(event) => prepared.push(event),
            Err(error) => rejected.push(Rejected { index, id, error }),
        }
    }

    let (inserted, duplicates) = db
        .write(|conn| {
            let tx = conn.transaction()?;
            let mut inserted = 0;
            let mut duplicates = 0;
            {
                let mut exists = tx.prepare_cached(
                    "SELECT 1 FROM events WHERE user_id = ?1 AND event_id = ?2 LIMIT 1",
                )?;
                let mut stmt = tx.prepare_cached(
                    "INSERT INTO events (
                        user_id, event_id, part, ts, model, kind, source, session_id, project, host,
                        input_tokens, output_tokens, cache_write_5m_tokens, cache_write_1h_tokens,
                        cache_read_tokens, thinking_tokens, web_search_requests, mods, received_at
                     ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19)",
                )?;
                for event in &prepared {
                    // The first copy of an id wins as a whole. Checking the id rather than each
                    // row keeps a resend with a different number of rows from adding to it.
                    // 같은 id는 처음 받은 것이 통째로 유지됩니다. 행 단위가 아니라 id 단위로
                    // 확인하므로, 행 수가 다른 내용으로 다시 보내도 덧붙지 않습니다.
                    if exists.exists(params![user.user_id, event.id])? {
                        duplicates += 1;
                        continue;
                    }
                    for row in &event.rows {
                        stmt.execute(params![
                            user.user_id,
                            event.id,
                            row.part,
                            event.ts,
                            row.model,
                            row.kind,
                            event.labels.source,
                            event.labels.session_id,
                            event.labels.project,
                            event.labels.host,
                            row.input_tokens,
                            row.output_tokens,
                            row.cache_write_5m_tokens,
                            row.cache_write_1h_tokens,
                            row.cache_read_tokens,
                            row.thinking_tokens,
                            row.web_search_requests,
                            row.mods,
                            now,
                        ])?;
                    }
                    inserted += 1;
                }
            }
            auth::touch_token(&tx, user.token_id)?;
            tx.commit()?;
            Ok((inserted, duplicates))
        })
        .map_err(|e| IngestError::Storage(e.to_string()))?;

    Ok(IngestSummary {
        received,
        inserted,
        duplicates,
        rejected,
    })
}

fn prepare(value: serde_json::Value, now: i64) -> Result<Prepared, String> {
    let event: IncomingEvent = serde_json::from_value(value).map_err(|e| e.to_string())?;
    let labels = usage::validate_labels(&event)?;
    let ts = timeutil::parse_rfc3339(&event.timestamp)
        .ok_or_else(|| "'timestamp' must be RFC 3339, e.g. 2026-11-22T06:25:28Z".to_string())?;
    if ts < MIN_TIMESTAMP {
        return Err("'timestamp' is before 2020-01-01".into());
    }
    if ts > now + MAX_FUTURE_SECS {
        return Err("'timestamp' is more than two days in the future".into());
    }
    let rows = usage::normalize(&event.model, &event.usage)?;
    Ok(Prepared {
        id: event.id,
        ts,
        labels,
        rows,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::TempDb;
    use serde_json::json;

    fn setup() -> (TempDb, AuthedUser) {
        let temp = TempDb::new();
        let user = temp
            .db
            .write(|conn| {
                let user_id = auth::create_user(conn, "alice")?.unwrap();
                let (token_id, _) = auth::create_token(conn, user_id, "")?;
                Ok(AuthedUser {
                    user_id,
                    user_name: "alice".into(),
                    token_id,
                })
            })
            .unwrap();
        (temp, user)
    }

    fn event(id: &str) -> serde_json::Value {
        json!({
            "id": id,
            "timestamp": "2026-11-22T06:25:28.553Z",
            "model": "claude-fable-5-2",
            "source": "claude-code",
            "session_id": "s1",
            "project": "demo",
            "usage": {"input_tokens": 2, "output_tokens": 968, "cache_creation_input_tokens": 16870,
                      "cache_read_input_tokens": 26681,
                      "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 16870}}
        })
    }

    fn send(
        temp: &TempDb,
        user: &AuthedUser,
        body: serde_json::Value,
    ) -> Result<IngestSummary, IngestError> {
        ingest(&temp.db, user, body.to_string().as_bytes(), 100)
    }

    fn row_count(temp: &TempDb) -> i64 {
        temp.db
            .read(|c| c.query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0)))
            .unwrap()
    }

    #[test]
    fn stores_events_and_ignores_resends() {
        let (temp, user) = setup();
        let body = json!({"events": [event("msg_1"), event("msg_2")]});
        let first = send(&temp, &user, body.clone()).unwrap();
        assert_eq!(
            first,
            IngestSummary {
                received: 2,
                inserted: 2,
                duplicates: 0,
                rejected: vec![]
            }
        );
        let second = send(&temp, &user, body).unwrap();
        assert_eq!(
            second,
            IngestSummary {
                received: 2,
                inserted: 0,
                duplicates: 2,
                rejected: vec![]
            }
        );
        assert_eq!(row_count(&temp), 2);

        let (ts, project, write_1h): (i64, String, i64) = temp
            .db
            .read(|c| {
                c.query_row(
                    "SELECT ts, project, cache_write_1h_tokens FROM events WHERE event_id = 'msg_1'",
                    [],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
            })
            .unwrap();
        assert_eq!(
            (ts, project.as_str(), write_1h),
            (1_795_328_728, "demo", 16870)
        );
        let used: Option<i64> = temp
            .db
            .read(|c| c.query_row("SELECT last_used_at FROM tokens", [], |r| r.get(0)))
            .unwrap();
        assert!(used.is_some());
    }

    #[test]
    fn the_same_id_from_two_users_is_two_events() {
        let (temp, alice) = setup();
        let bob = temp
            .db
            .write(|conn| {
                let user_id = auth::create_user(conn, "bob")?.unwrap();
                let (token_id, _) = auth::create_token(conn, user_id, "")?;
                Ok(AuthedUser {
                    user_id,
                    user_name: "bob".into(),
                    token_id,
                })
            })
            .unwrap();
        send(&temp, &alice, json!({"events": [event("msg_1")]})).unwrap();
        let summary = send(&temp, &bob, json!({"events": [event("msg_1")]})).unwrap();
        assert_eq!(summary.inserted, 1);
        assert_eq!(row_count(&temp), 2);
    }

    #[test]
    fn rejects_bad_events_but_keeps_good_ones() {
        let (temp, user) = setup();
        let mut no_timestamp = event("msg_bad_1");
        no_timestamp.as_object_mut().unwrap().remove("timestamp");
        let mut bad_time = event("msg_bad_2");
        bad_time["timestamp"] = json!("yesterday");
        let mut negative = event("msg_bad_3");
        negative["usage"]["input_tokens"] = json!(-5);
        let mut future = event("msg_bad_4");
        future["timestamp"] = json!("2999-01-01T00:00:00Z");
        let mut ancient = event("msg_bad_5");
        ancient["timestamp"] = json!("2001-01-01T00:00:00Z");

        let summary = send(
            &temp,
            &user,
            json!({"events": [event("msg_ok"), no_timestamp, bad_time, negative, future, ancient, "not an object"]}),
        )
        .unwrap();
        assert_eq!(
            (summary.received, summary.inserted, summary.duplicates),
            (7, 1, 0)
        );
        let indexes: Vec<usize> = summary.rejected.iter().map(|r| r.index).collect();
        assert_eq!(indexes, vec![1, 2, 3, 4, 5, 6]);
        assert_eq!(summary.rejected[0].id.as_deref(), Some("msg_bad_1"));
        assert!(
            summary.rejected[0].error.contains("timestamp"),
            "{}",
            summary.rejected[0].error
        );
        assert!(summary.rejected[1].error.contains("RFC 3339"));
        assert!(summary.rejected[3].error.contains("future"));
        assert!(summary.rejected[4].error.contains("2020"));
        assert_eq!(summary.rejected[5].id, None);
        assert_eq!(row_count(&temp), 1);
    }

    #[test]
    fn rejects_bad_envelopes() {
        let (temp, user) = setup();
        for body in ["", "[]", "{}", "{\"events\": 3}", "not json"] {
            let result = ingest(&temp.db, &user, body.as_bytes(), 100);
            assert!(matches!(result, Err(IngestError::BadRequest(_))), "{body}");
        }
        let too_many = json!({"events": [event("a"), event("b"), event("c")]});
        let result = ingest(&temp.db, &user, too_many.to_string().as_bytes(), 2);
        assert!(matches!(result, Err(IngestError::BadRequest(m)) if m.contains("too many")));
        let empty = send(&temp, &user, json!({"events": []})).unwrap();
        assert_eq!(
            empty,
            IngestSummary {
                received: 0,
                inserted: 0,
                duplicates: 0,
                rejected: vec![]
            }
        );
    }

    #[test]
    fn stores_one_row_per_iteration() {
        let (temp, user) = setup();
        let mut with_advisor = event("msg_adv");
        with_advisor["usage"]["iterations"] = json!([
            {"type": "message", "input_tokens": 2, "output_tokens": 968},
            {"type": "advisor_message", "model": "claude-opus-5-5", "input_tokens": 68516, "output_tokens": 11111}
        ]);
        let summary = send(&temp, &user, json!({"events": [with_advisor.clone()]})).unwrap();
        assert_eq!(summary.inserted, 1);
        assert_eq!(row_count(&temp), 2);
        let advisor_model: String = temp
            .db
            .read(|c| {
                c.query_row(
                    "SELECT model FROM events WHERE kind = 'advisor_message'",
                    [],
                    |r| r.get(0),
                )
            })
            .unwrap();
        assert_eq!(advisor_model, "claude-opus-5-5");
        let again = send(&temp, &user, json!({"events": [with_advisor]})).unwrap();
        assert_eq!((again.inserted, again.duplicates), (0, 1));
    }

    #[test]
    fn a_resend_with_different_content_changes_nothing() {
        let (temp, user) = setup();
        send(&temp, &user, json!({"events": [event("msg_1")]})).unwrap();

        // Same id, but now with three iterations and other numbers: no row may be added.
        // 같은 id인데 이번에는 추론이 세 개이고 숫자도 다릅니다. 행이 하나도 늘면 안 됩니다.
        let mut changed = event("msg_1");
        changed["usage"]["output_tokens"] = json!(5);
        changed["usage"]["iterations"] = json!([
            {"type": "message", "output_tokens": 1},
            {"type": "advisor_message", "model": "claude-opus-5-5", "output_tokens": 2},
            {"type": "message", "output_tokens": 3}
        ]);
        let summary = send(&temp, &user, json!({"events": [changed]})).unwrap();
        assert_eq!((summary.inserted, summary.duplicates), (0, 1));
        assert_eq!(row_count(&temp), 1);
        let output: i64 = temp
            .db
            .read(|c| c.query_row("SELECT SUM(output_tokens) FROM events", [], |r| r.get(0)))
            .unwrap();
        assert_eq!(output, 968);

        // The same id twice inside one request is also stored once.
        // 한 요청 안에 같은 id가 두 번 있어도 한 번만 저장합니다.
        let twice = send(
            &temp,
            &user,
            json!({"events": [event("msg_2"), event("msg_2")]}),
        )
        .unwrap();
        assert_eq!((twice.inserted, twice.duplicates), (1, 1));
        assert_eq!(row_count(&temp), 2);
    }
}
