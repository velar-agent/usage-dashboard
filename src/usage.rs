//! Turns one reported event into the rows the dashboard stores.
//! 보고된 이벤트 하나를 대시보드가 저장하는 행들로 바꿉니다.
//!
//! A collector sends the Anthropic `usage` object as it received it. The server reads it here so
//! that collectors stay simple and the interpretation can change in one place.
//! 수집 프로세스는 Anthropic `usage` 객체를 받은 그대로 보냅니다. 해석은 서버의 이 파일에서
//! 하므로 수집 프로세스는 단순하게 유지되고, 해석이 바뀌어도 한 곳만 고치면 됩니다.

use crate::pricing::{MOD_BATCH, MOD_FAST, MOD_US_INFERENCE};
use serde::{Deserialize, Deserializer};

pub const MAX_ID_LEN: usize = 200;
pub const MAX_MODEL_LEN: usize = 100;
pub const MAX_LABEL_LEN: usize = 200;
pub const MAX_ITERATIONS: usize = 64;
/// Upper bound for one token counter. Far above any real request and keeps sums inside i64.
/// 토큰 수 하나의 상한입니다. 실제 요청보다 훨씬 크고, 합계가 i64 범위를 넘지 않게 합니다.
pub const MAX_TOKEN_COUNT: u64 = 1_000_000_000_000;

/// One event as sent by a collector.
/// 수집 프로세스가 보내는 이벤트 하나입니다.
#[derive(Debug, Deserialize)]
pub struct IncomingEvent {
    pub id: String,
    pub timestamp: String,
    pub model: String,
    pub usage: Usage,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub project: Option<String>,
    #[serde(default)]
    pub host: Option<String>,
}

/// The Anthropic Messages API usage object. Unknown fields are ignored on purpose.
/// Anthropic Messages API의 usage 객체입니다. 모르는 필드는 일부러 무시합니다.
#[derive(Debug, Default, Deserialize)]
pub struct Usage {
    #[serde(default, deserialize_with = "null_as_zero")]
    pub input_tokens: u64,
    #[serde(default, deserialize_with = "null_as_zero")]
    pub output_tokens: u64,
    #[serde(default, deserialize_with = "null_as_zero")]
    pub cache_creation_input_tokens: u64,
    #[serde(default, deserialize_with = "null_as_zero")]
    pub cache_read_input_tokens: u64,
    #[serde(default)]
    pub cache_creation: Option<CacheCreation>,
    #[serde(default)]
    pub output_tokens_details: Option<OutputTokensDetails>,
    #[serde(default)]
    pub server_tool_use: Option<ServerToolUse>,
    #[serde(default)]
    pub service_tier: Option<String>,
    #[serde(default)]
    pub speed: Option<String>,
    #[serde(default)]
    pub inference_geo: Option<String>,
    #[serde(default)]
    pub iterations: Option<Vec<Iteration>>,
}

#[derive(Debug, Default, Deserialize)]
pub struct CacheCreation {
    #[serde(default, deserialize_with = "null_as_zero")]
    pub ephemeral_5m_input_tokens: u64,
    #[serde(default, deserialize_with = "null_as_zero")]
    pub ephemeral_1h_input_tokens: u64,
}

#[derive(Debug, Default, Deserialize)]
pub struct OutputTokensDetails {
    #[serde(default, deserialize_with = "null_as_zero")]
    pub thinking_tokens: u64,
}

#[derive(Debug, Default, Deserialize)]
pub struct ServerToolUse {
    #[serde(default, deserialize_with = "null_as_zero")]
    pub web_search_requests: u64,
}

/// One inference pass inside a response, e.g. the main message or an advisor consultation.
/// 응답 하나 안에서 일어난 추론 한 번입니다. 본 메시지나 어드바이저 호출이 여기에 해당합니다.
#[derive(Debug, Default, Deserialize)]
pub struct Iteration {
    #[serde(default, rename = "type")]
    pub kind: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default, deserialize_with = "null_as_zero")]
    pub input_tokens: u64,
    #[serde(default, deserialize_with = "null_as_zero")]
    pub output_tokens: u64,
    #[serde(default, deserialize_with = "null_as_zero")]
    pub cache_creation_input_tokens: u64,
    #[serde(default, deserialize_with = "null_as_zero")]
    pub cache_read_input_tokens: u64,
    #[serde(default)]
    pub cache_creation: Option<CacheCreation>,
}

/// One stored row: a single inference pass with its model and token counts.
/// 저장되는 행 하나입니다. 추론 한 번의 모델과 토큰 수를 담습니다.
#[derive(Debug, Clone, PartialEq)]
pub struct UsageRow {
    pub part: i64,
    pub kind: String,
    pub model: String,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_write_5m_tokens: i64,
    pub cache_write_1h_tokens: i64,
    pub cache_read_tokens: i64,
    pub thinking_tokens: i64,
    pub web_search_requests: i64,
    pub mods: u8,
}

fn null_as_zero<'de, D: Deserializer<'de>>(d: D) -> Result<u64, D::Error> {
    Ok(Option::<u64>::deserialize(d)?.unwrap_or(0))
}

/// Check the text fields of an event and return the trimmed optional labels.
/// 이벤트의 문자열 필드를 검사하고, 다듬은 선택 필드들을 돌려줍니다.
pub fn validate_labels(ev: &IncomingEvent) -> Result<Labels, String> {
    check_text("id", &ev.id, MAX_ID_LEN, true)?;
    check_text("model", &ev.model, MAX_MODEL_LEN, true)?;
    Ok(Labels {
        source: optional_label("source", &ev.source)?,
        session_id: optional_label("session_id", &ev.session_id)?,
        project: optional_label("project", &ev.project)?,
        host: optional_label("host", &ev.host)?,
    })
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Labels {
    pub source: String,
    pub session_id: String,
    pub project: String,
    pub host: String,
}

fn optional_label(name: &str, value: &Option<String>) -> Result<String, String> {
    match value {
        None => Ok(String::new()),
        Some(v) => {
            let trimmed = v.trim();
            check_text(name, trimmed, MAX_LABEL_LEN, false)?;
            Ok(trimmed.to_string())
        }
    }
}

fn check_text(name: &str, value: &str, max_len: usize, required: bool) -> Result<(), String> {
    if required && value.trim().is_empty() {
        return Err(format!("'{name}' must not be empty"));
    }
    if value.chars().count() > max_len {
        return Err(format!("'{name}' is longer than {max_len} characters"));
    }
    if value.chars().any(char::is_control) {
        return Err(format!("'{name}' contains control characters"));
    }
    Ok(())
}

/// Split an event's usage into rows.
///
/// `usage.iterations`, when present, is the per-pass source of truth: the top-level counters only
/// cover the passes of type "message", so an advisor pass (a different model, billed separately)
/// would be lost by reading the top level alone. Without `iterations` the top level is one row.
///
/// 이벤트의 usage를 행들로 나눕니다.
///
/// `usage.iterations`가 있으면 추론 단위의 기준 자료로 씁니다. 최상위 숫자는 type이 "message"인
/// 추론만 합친 값이라서, 최상위만 읽으면 어드바이저 추론(다른 모델이며 따로 과금됨)이 빠집니다.
/// `iterations`가 없으면 최상위 값을 행 하나로 저장합니다.
pub fn normalize(model: &str, usage: &Usage) -> Result<Vec<UsageRow>, String> {
    let mut mods = 0u8;
    if usage.speed.as_deref() == Some("fast") {
        mods |= MOD_FAST;
    }
    if usage.service_tier.as_deref() == Some("batch") {
        mods |= MOD_BATCH;
    }
    if usage.inference_geo.as_deref() == Some("us") {
        mods |= MOD_US_INFERENCE;
    }

    let thinking = count(
        "output_tokens_details.thinking_tokens",
        usage
            .output_tokens_details
            .as_ref()
            .map_or(0, |d| d.thinking_tokens),
    )?;
    let web_search = count(
        "server_tool_use.web_search_requests",
        usage
            .server_tool_use
            .as_ref()
            .map_or(0, |s| s.web_search_requests),
    )?;

    let iterations = usage.iterations.as_deref().unwrap_or(&[]);
    if iterations.len() > MAX_ITERATIONS {
        return Err(format!(
            "'usage.iterations' has more than {MAX_ITERATIONS} entries"
        ));
    }

    let mut rows = Vec::with_capacity(iterations.len().max(1));
    if iterations.is_empty() {
        rows.push(make_row(
            0,
            "message",
            model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_input_tokens,
            usage.cache_read_input_tokens,
            usage.cache_creation.as_ref(),
            mods,
        )?);
    } else {
        for (i, it) in iterations.iter().enumerate() {
            let kind = it.kind.as_deref().unwrap_or("message");
            check_text("usage.iterations[].type", kind, 40, true)?;
            let it_model = it.model.as_deref().unwrap_or(model);
            check_text("usage.iterations[].model", it_model, MAX_MODEL_LEN, true)?;
            rows.push(make_row(
                i as i64,
                kind,
                it_model,
                it.input_tokens,
                it.output_tokens,
                it.cache_creation_input_tokens,
                it.cache_read_input_tokens,
                it.cache_creation.as_ref(),
                mods,
            )?);
        }
    }

    // Thinking tokens and web searches are only reported for the whole response. Attach them to
    // the main message pass so that they are counted once.
    // 사고 토큰과 웹 검색 횟수는 응답 전체에 대해서만 보고됩니다. 한 번만 세도록 본 메시지
    // 추론에 붙입니다.
    let main = rows
        .iter()
        .position(|r| r.kind == "message" && r.model == model)
        .unwrap_or(0);
    rows[main].thinking_tokens = thinking;
    rows[main].web_search_requests = web_search;
    Ok(rows)
}

#[allow(clippy::too_many_arguments)]
fn make_row(
    part: i64,
    kind: &str,
    model: &str,
    input: u64,
    output: u64,
    cache_write_total: u64,
    cache_read: u64,
    cache_creation: Option<&CacheCreation>,
    mods: u8,
) -> Result<UsageRow, String> {
    let total = count("cache_creation_input_tokens", cache_write_total)?;
    let mut write_5m = count(
        "cache_creation.ephemeral_5m_input_tokens",
        cache_creation.map_or(0, |c| c.ephemeral_5m_input_tokens),
    )?;
    let write_1h = count(
        "cache_creation.ephemeral_1h_input_tokens",
        cache_creation.map_or(0, |c| c.ephemeral_1h_input_tokens),
    )?;
    // The per-TTL breakdown is what gets priced, so it is the source of truth. The total only
    // fills in what the breakdown leaves out: cache writes without a TTL count as 5-minute
    // writes, the API default. A breakdown that adds up to more than the total is kept as it
    // is; cutting it down to the total would zero the cache writes of a sender that reports the
    // breakdown alone.
    // 단가는 보관 시간별 구분에 매겨지므로 그 구분을 기준으로 삼습니다. 합계는 구분에 빠진 부분만
    // 채웁니다. 보관 시간이 없는 캐시 쓰기는 API 기본값인 5분 쓰기로 셉니다. 구분의 합이 합계보다
    // 크면 구분을 그대로 둡니다. 합계에 맞춰 깎으면, 구분만 보내는 쪽의 캐시 쓰기가 0이 됩니다.
    if total > write_5m + write_1h {
        write_5m = total - write_1h;
    }
    Ok(UsageRow {
        part,
        kind: kind.to_string(),
        model: model.to_string(),
        input_tokens: count("input_tokens", input)?,
        output_tokens: count("output_tokens", output)?,
        cache_write_5m_tokens: write_5m,
        cache_write_1h_tokens: write_1h,
        cache_read_tokens: count("cache_read_input_tokens", cache_read)?,
        thinking_tokens: 0,
        web_search_requests: 0,
        mods,
    })
}

fn count(name: &str, value: u64) -> Result<i64, String> {
    if value > MAX_TOKEN_COUNT {
        return Err(format!("'{name}' is too large"));
    }
    Ok(value as i64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn usage(value: serde_json::Value) -> Usage {
        serde_json::from_value(value).expect("usage should deserialize")
    }

    #[test]
    fn reads_a_plain_usage_object() {
        let u = usage(json!({
            "input_tokens": 2,
            "output_tokens": 968,
            "cache_creation_input_tokens": 16870,
            "cache_read_input_tokens": 26681,
            "output_tokens_details": {"thinking_tokens": 267},
            "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 0},
            "service_tier": "standard",
            "cache_creation": {"ephemeral_1h_input_tokens": 16870, "ephemeral_5m_input_tokens": 0},
            "inference_geo": "global",
            "iterations": null,
            "speed": "standard",
            "fallback_credit": null
        }));
        let rows = normalize("claude-fable-5-2", &u).unwrap();
        assert_eq!(
            rows,
            vec![UsageRow {
                part: 0,
                kind: "message".into(),
                model: "claude-fable-5-2".into(),
                input_tokens: 2,
                output_tokens: 968,
                cache_write_5m_tokens: 0,
                cache_write_1h_tokens: 16870,
                cache_read_tokens: 26681,
                thinking_tokens: 267,
                web_search_requests: 1,
                mods: 0,
            }]
        );
    }

    #[test]
    fn splits_iterations_and_keeps_the_advisor_model() {
        // Shape taken from a Claude Code transcript: the top level equals the sum of the two
        // "message" passes and leaves the advisor pass out.
        // Claude Code 기록에서 가져온 모양입니다. 최상위 값은 "message" 추론 둘의 합이고
        // 어드바이저 추론은 들어 있지 않습니다.
        let u = usage(json!({
            "input_tokens": 5,
            "output_tokens": 300,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 50000,
            "output_tokens_details": {"thinking_tokens": 120},
            "iterations": [
                {"type": "message", "input_tokens": 2, "output_tokens": 100, "cache_read_input_tokens": 20000,
                 "cache_creation_input_tokens": 400, "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 400}},
                {"type": "advisor_message", "model": "claude-opus-5-5", "input_tokens": 68516, "output_tokens": 11111,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}},
                {"type": "message", "input_tokens": 3, "output_tokens": 200, "cache_read_input_tokens": 30000,
                 "cache_creation_input_tokens": 600, "cache_creation": {"ephemeral_5m_input_tokens": 600, "ephemeral_1h_input_tokens": 0}}
            ]
        }));
        let rows = normalize("claude-fable-5-2", &u).unwrap();
        assert_eq!(rows.len(), 3);
        assert_eq!(
            rows.iter().map(|r| r.part).collect::<Vec<_>>(),
            vec![0, 1, 2]
        );
        assert_eq!(rows[1].kind, "advisor_message");
        assert_eq!(rows[1].model, "claude-opus-5-5");
        assert_eq!(rows[1].input_tokens, 68516);
        assert_eq!(rows[0].model, "claude-fable-5-2");
        assert_eq!(rows[0].cache_write_1h_tokens, 400);
        assert_eq!(rows[2].cache_write_5m_tokens, 600);
        // Counted once, on the first main pass.
        // 첫 번째 본 메시지 추론에 한 번만 셉니다.
        assert_eq!(
            rows.iter().map(|r| r.thinking_tokens).collect::<Vec<_>>(),
            vec![120, 0, 0]
        );
        let message_output: i64 = rows
            .iter()
            .filter(|r| r.kind == "message")
            .map(|r| r.output_tokens)
            .sum();
        assert_eq!(message_output, 300);
    }

    #[test]
    fn cache_writes_without_a_breakdown_count_as_5m() {
        let u = usage(
            json!({"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 500}),
        );
        let rows = normalize("m", &u).unwrap();
        assert_eq!(
            (rows[0].cache_write_5m_tokens, rows[0].cache_write_1h_tokens),
            (500, 0)
        );

        let partial = usage(
            json!({"cache_creation_input_tokens": 500, "cache_creation": {"ephemeral_1h_input_tokens": 200}}),
        );
        let rows = normalize("m", &partial).unwrap();
        assert_eq!(
            (rows[0].cache_write_5m_tokens, rows[0].cache_write_1h_tokens),
            (300, 200)
        );
    }

    #[test]
    fn the_cache_write_breakdown_wins_over_a_smaller_total() {
        // Only the breakdown, no total: the writes must not be lost.
        // 합계 없이 구분만 온 경우입니다. 캐시 쓰기가 사라지면 안 됩니다.
        let breakdown_only = usage(json!({"cache_creation": {
            "ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 400
        }}));
        let rows = normalize("m", &breakdown_only).unwrap();
        assert_eq!(
            (rows[0].cache_write_5m_tokens, rows[0].cache_write_1h_tokens),
            (100, 400)
        );

        // A total that contradicts the breakdown does not shrink it.
        // 구분과 맞지 않는 합계가 와도 구분을 줄이지 않습니다.
        let contradictory = usage(
            json!({"cache_creation_input_tokens": 50, "cache_creation": {
                "ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 400
            }}),
        );
        let rows = normalize("m", &contradictory).unwrap();
        assert_eq!(
            (rows[0].cache_write_5m_tokens, rows[0].cache_write_1h_tokens),
            (100, 400)
        );
    }

    #[test]
    fn reads_pricing_modifiers() {
        let u = usage(json!({"speed": "fast", "service_tier": "batch", "inference_geo": "us"}));
        let rows = normalize("m", &u).unwrap();
        assert_eq!(rows[0].mods, MOD_FAST | MOD_BATCH | MOD_US_INFERENCE);
        let plain =
            usage(json!({"speed": null, "service_tier": "priority", "inference_geo": "global"}));
        assert_eq!(normalize("m", &plain).unwrap()[0].mods, 0);
    }

    #[test]
    fn rejects_bad_numbers() {
        assert!(serde_json::from_value::<Usage>(json!({"input_tokens": -1})).is_err());
        assert!(serde_json::from_value::<Usage>(json!({"input_tokens": 1.5})).is_err());
        assert!(serde_json::from_value::<Usage>(json!({"input_tokens": "12"})).is_err());
        let huge = usage(json!({"output_tokens": 2_000_000_000_000u64}));
        assert!(normalize("m", &huge).unwrap_err().contains("too large"));
    }

    #[test]
    fn validates_labels() {
        let ev = |id: &str, project: Option<&str>| IncomingEvent {
            id: id.to_string(),
            timestamp: String::new(),
            model: "m".into(),
            usage: Usage::default(),
            source: Some("  claude-code ".into()),
            session_id: None,
            project: project.map(str::to_string),
            host: None,
        };
        let labels = validate_labels(&ev("msg_1", Some("프로젝트"))).unwrap();
        assert_eq!(labels.source, "claude-code");
        assert_eq!(labels.project, "프로젝트");
        assert_eq!(labels.session_id, "");
        assert!(validate_labels(&ev("", None)).is_err());
        assert!(validate_labels(&ev(&"x".repeat(201), None)).is_err());
        assert!(validate_labels(&ev("msg_1", Some("a\nb"))).is_err());
        assert!(validate_labels(&ev("msg_1", Some(&"가".repeat(201)))).is_err());
    }
}
