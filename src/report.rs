//! Aggregates stored events into the single response the dashboard page renders.
//! 저장된 이벤트를 집계해 대시보드 화면이 그리는 응답 하나로 만듭니다.
//!
//! Costs are not stored. Each query groups rows by everything that affects the price, and the
//! price table is applied to the grouped token sums, so a changed table needs no data migration.
//! 비용은 저장하지 않습니다. 조회할 때 가격에 영향을 주는 값으로 행을 묶고, 묶인 토큰 합계에
//! 단가표를 적용합니다. 그래서 단가표가 바뀌어도 데이터를 고칠 필요가 없습니다.

use crate::pricing::{CostBreakdown, Pricing, TokenCounts};
use crate::timeutil;
use chrono::{Duration, NaiveDate};
use rusqlite::types::Value;
use rusqlite::{Connection, params, params_from_iter};
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet, HashMap};

pub const MAX_RANGE_DAYS: i64 = 1100;
/// Ranges up to this many days are bucketed by hour instead of by day.
/// 이 날수 이하의 기간은 일 단위 대신 시간 단위로 묶습니다.
const HOURLY_MAX_DAYS: i64 = 2;
const MAX_BREAKDOWN_ROWS: usize = 100;
const MAX_SESSIONS: usize = 20;
const MAX_FILTER_OPTIONS: usize = 500;
/// Key of the row that carries everything beyond MAX_BREAKDOWN_ROWS.
/// MAX_BREAKDOWN_ROWS를 넘는 나머지를 합친 행의 키입니다.
pub const OTHERS_KEY: &str = "(others)";

#[derive(Debug, Clone, Default)]
pub struct ReportParams {
    pub from: Option<NaiveDate>,
    pub to: Option<NaiveDate>,
    pub offset_min: i32,
    pub user: Option<String>,
    pub model: Option<String>,
    pub project: Option<String>,
}

#[derive(Debug)]
pub enum ReportError {
    BadRequest(String),
    Storage(rusqlite::Error),
}

impl From<rusqlite::Error> for ReportError {
    fn from(e: rusqlite::Error) -> Self {
        ReportError::Storage(e)
    }
}

/// Summed usage of a set of rows.
/// 행 집합의 사용량 합계입니다.
#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct Agg {
    pub requests: i64,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_write_5m_tokens: i64,
    pub cache_write_1h_tokens: i64,
    pub cache_read_tokens: i64,
    pub thinking_tokens: i64,
    pub web_search_requests: i64,
    pub cost_usd: f64,
    pub cost: CostBreakdown,
    /// Requests whose model has no price; their cost is missing from cost_usd.
    /// 단가가 없는 모델의 요청 수입니다. 이 요청들의 비용은 cost_usd에 들어 있지 않습니다.
    pub unpriced_requests: i64,
}

impl Agg {
    pub fn total_tokens(&self) -> i64 {
        self.input_tokens
            + self.output_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.cache_read_tokens
    }

    fn add(&mut self, g: &Group) {
        self.requests += g.requests;
        self.input_tokens += g.tokens.input;
        self.output_tokens += g.tokens.output;
        self.cache_write_5m_tokens += g.tokens.cache_write_5m;
        self.cache_write_1h_tokens += g.tokens.cache_write_1h;
        self.cache_read_tokens += g.tokens.cache_read;
        self.thinking_tokens += g.thinking;
        self.web_search_requests += g.tokens.web_search_requests;
        match &g.cost {
            Some(cost) => {
                self.cost.add(cost);
                self.cost_usd += cost.total();
            }
            None => self.unpriced_requests += g.requests,
        }
    }

    fn merge(&mut self, other: &Agg) {
        self.requests += other.requests;
        self.input_tokens += other.input_tokens;
        self.output_tokens += other.output_tokens;
        self.cache_write_5m_tokens += other.cache_write_5m_tokens;
        self.cache_write_1h_tokens += other.cache_write_1h_tokens;
        self.cache_read_tokens += other.cache_read_tokens;
        self.thinking_tokens += other.thinking_tokens;
        self.web_search_requests += other.web_search_requests;
        self.cost_usd += other.cost_usd;
        self.cost.add(&other.cost);
        self.unpriced_requests += other.unpriced_requests;
    }
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Keyed {
    pub key: String,
    /// How many keys were folded into this row; 0 for an ordinary row.
    /// 이 행에 합쳐진 키의 수입니다. 보통 행은 0입니다.
    pub merged: usize,
    #[serde(flatten)]
    pub agg: Agg,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SessionRow {
    pub user: String,
    pub session_id: String,
    pub project: String,
    pub source: String,
    pub models: Vec<String>,
    pub first_ts: i64,
    pub last_ts: i64,
    #[serde(flatten)]
    pub agg: Agg,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct DayRange {
    pub from: String,
    pub to: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RangeInfo {
    pub from: String,
    pub to: String,
    pub utc_offset_minutes: i32,
    /// "hour" or "day": the size of each entry in `buckets`.
    /// "hour" 또는 "day"입니다. `buckets` 항목 하나의 크기입니다.
    pub bucket: &'static str,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct FilterOptions {
    pub users: Vec<String>,
    pub models: Vec<String>,
    pub projects: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Report {
    pub range: RangeInfo,
    pub previous_range: Option<DayRange>,
    pub totals: Agg,
    pub previous_totals: Option<Agg>,
    pub active_users: usize,
    pub active_sessions: usize,
    pub buckets: Vec<Keyed>,
    pub by_user: Vec<Keyed>,
    pub by_model: Vec<Keyed>,
    pub by_project: Vec<Keyed>,
    pub by_source: Vec<Keyed>,
    pub sessions: Vec<SessionRow>,
    pub options: FilterOptions,
    pub unpriced_models: Vec<String>,
    pub prices_as_of: String,
}

/// One GROUP BY result: rows that share a bucket, the dimensions and everything price-relevant.
/// GROUP BY 결과 하나입니다. 구간, 분류 기준, 가격에 영향을 주는 값이 모두 같은 행들의 합계입니다.
struct Group {
    bucket: String,
    user_id: i64,
    model: String,
    project: String,
    source: String,
    requests: i64,
    tokens: TokenCounts,
    thinking: i64,
    cost: Option<CostBreakdown>,
}

struct Filters<'a> {
    user_id: Option<i64>,
    model: Option<&'a str>,
    project: Option<&'a str>,
}

impl Filters<'_> {
    fn matches(&self, g: &Group) -> bool {
        self.user_id.is_none_or(|id| id == g.user_id)
            && self.model.is_none_or(|m| m == g.model)
            && self.project.is_none_or(|p| p == g.project)
    }
}

const TOKEN_SUMS: &str = "COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cache_write_5m_tokens),
    SUM(cache_write_1h_tokens), SUM(cache_read_tokens), SUM(thinking_tokens), SUM(web_search_requests)";
const PROMPT_TOKENS: &str =
    "(input_tokens + cache_write_5m_tokens + cache_write_1h_tokens + cache_read_tokens)";

/// Build the dashboard report for a date range and optional filters.
/// 날짜 범위와 선택한 필터에 대한 대시보드 리포트를 만듭니다.
pub fn build_report(
    conn: &Connection,
    pricing: &Pricing,
    params: &ReportParams,
    now: i64,
) -> Result<Report, ReportError> {
    let offset = params.offset_min;
    let today = timeutil::local_day(now, offset);
    let to = params.to.unwrap_or(today);
    let from = match params.from {
        Some(day) => day,
        // No start date means "everything": begin at the first stored event.
        // 시작일이 없으면 "전체"라는 뜻이므로 저장된 첫 이벤트의 날짜부터 시작합니다.
        None => earliest_day(conn, offset)?.unwrap_or(to).min(to),
    };
    if from > to {
        return Err(ReportError::BadRequest(
            "'from' must not be after 'to'".into(),
        ));
    }
    let days = (to - from).num_days() + 1;
    if days > MAX_RANGE_DAYS {
        return Err(ReportError::BadRequest(format!(
            "date range is longer than {MAX_RANGE_DAYS} days"
        )));
    }

    let users = user_names(conn)?;
    // An unknown user name must match nothing rather than everything.
    // 모르는 사용자 이름은 전체가 아니라 아무것도 맞지 않아야 합니다.
    let user_id = params.user.as_deref().map(|name| {
        users
            .iter()
            .find(|(_, n)| n.as_str() == name)
            .map_or(-1, |(id, _)| *id)
    });
    let filters = Filters {
        user_id,
        model: params.model.as_deref(),
        project: params.project.as_deref(),
    };

    let hourly = days <= HOURLY_MAX_DAYS;
    let from_ts = timeutil::day_start(from, offset);
    let to_ts = timeutil::day_start(to + Duration::days(1), offset);
    let bucket_format = if hourly { "%Y-%m-%dT%H" } else { "%Y-%m-%d" };
    let groups = scan(conn, pricing, Some(bucket_format), from_ts, to_ts, offset)?;

    let mut totals = Agg::default();
    let mut buckets: BTreeMap<String, Agg> = BTreeMap::new();
    let mut by_user: HashMap<i64, Agg> = HashMap::new();
    let mut by_model: HashMap<String, Agg> = HashMap::new();
    let mut by_project: HashMap<String, Agg> = HashMap::new();
    let mut by_source: HashMap<String, Agg> = HashMap::new();
    let mut option_users = BTreeSet::new();
    let mut option_models = BTreeSet::new();
    let mut option_projects = BTreeSet::new();
    let mut unpriced_models = BTreeSet::new();

    for g in &groups {
        // Filter choices list what exists in the date range, whatever is currently selected.
        // 필터 선택지는 지금 무엇을 골랐는지와 관계없이 그 기간에 있는 값을 보여 줍니다.
        if let Some(name) = users.get(&g.user_id) {
            option_users.insert(name.clone());
        }
        option_models.insert(g.model.clone());
        option_projects.insert(g.project.clone());
        if !filters.matches(g) {
            continue;
        }
        if g.cost.is_none() {
            unpriced_models.insert(g.model.clone());
        }
        totals.add(g);
        buckets.entry(g.bucket.clone()).or_default().add(g);
        by_user.entry(g.user_id).or_default().add(g);
        by_model.entry(g.model.clone()).or_default().add(g);
        by_project.entry(g.project.clone()).or_default().add(g);
        by_source.entry(g.source.clone()).or_default().add(g);
    }

    // Compare with the period of the same length right before this one. "Everything" has none.
    // 바로 앞의 같은 길이 기간과 비교합니다. "전체"에는 비교 대상이 없습니다.
    let (previous_range, previous_totals) = if params.from.is_some() {
        let prev_to = from - Duration::days(1);
        let prev_from = from - Duration::days(days);
        let prev_groups = scan(
            conn,
            pricing,
            None,
            timeutil::day_start(prev_from, offset),
            from_ts,
            offset,
        )?;
        let mut agg = Agg::default();
        prev_groups
            .iter()
            .filter(|g| filters.matches(g))
            .for_each(|g| agg.add(g));
        (
            Some(DayRange {
                from: prev_from.to_string(),
                to: prev_to.to_string(),
            }),
            Some(agg),
        )
    } else {
        (None, None)
    };

    let (sessions, active_sessions) =
        top_sessions(conn, pricing, &users, &filters, from_ts, to_ts)?;

    let active_users = by_user.len();
    let by_user = by_user
        .into_iter()
        .map(|(id, agg)| {
            (
                users.get(&id).cloned().unwrap_or_else(|| format!("#{id}")),
                agg,
            )
        })
        .collect();

    Ok(Report {
        range: RangeInfo {
            from: from.to_string(),
            to: to.to_string(),
            utc_offset_minutes: offset,
            bucket: if hourly { "hour" } else { "day" },
        },
        previous_range,
        totals,
        previous_totals,
        active_users,
        active_sessions,
        buckets: buckets
            .into_iter()
            .map(|(key, agg)| Keyed {
                key,
                merged: 0,
                agg,
            })
            .collect(),
        by_user: ranked(by_user),
        by_model: ranked(by_model),
        by_project: ranked(by_project),
        by_source: ranked(by_source),
        sessions,
        options: FilterOptions {
            users: option_users.into_iter().take(MAX_FILTER_OPTIONS).collect(),
            models: option_models.into_iter().take(MAX_FILTER_OPTIONS).collect(),
            projects: option_projects
                .into_iter()
                .take(MAX_FILTER_OPTIONS)
                .collect(),
        },
        unpriced_models: unpriced_models.into_iter().collect(),
        prices_as_of: pricing.table().as_of.clone(),
    })
}

fn earliest_day(conn: &Connection, offset: i32) -> rusqlite::Result<Option<NaiveDate>> {
    let min_ts: Option<i64> = conn.query_row("SELECT MIN(ts) FROM events", [], |row| row.get(0))?;
    Ok(min_ts.map(|ts| timeutil::local_day(ts, offset)))
}

fn user_names(conn: &Connection) -> rusqlite::Result<HashMap<i64, String>> {
    let mut stmt = conn.prepare("SELECT id, name FROM users")?;
    let rows = stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
    rows.collect()
}

/// The report queries name their index. Left to choose, SQLite answers a query filtered by user
/// from the primary key instead, reading that user's whole history to find one date range.
/// Use this source only for queries bounded by a `ts` range: the index is ordered by time, so a
/// query without one (a single session's whole history, say) would read the entire index and
/// needs its own index or query path.
/// 리포트 조회는 쓸 인덱스를 직접 지정합니다. 맡겨 두면 SQLite는 사용자로 거르는 조회에 기본 키를
/// 골라, 날짜 범위 하나를 찾으려고 그 사용자의 전체 기록을 읽습니다. 이 지정은 `ts` 범위 조건이
/// 있는 조회에만 씁니다. 인덱스가 시간순이라서, 범위 조건이 없는 조회(예: 세션 하나의 전체
/// 기록)는 인덱스 전체를 읽게 되므로 별도의 인덱스나 조회 경로가 필요합니다.
const REPORT_SOURCE: &str = "events INDEXED BY events_report";

/// SQL of the main report scan. Every column it reads is in the `events_report` index, so it
/// never touches the table; a test checks that this stays true.
/// 리포트의 주 조회 SQL입니다. 읽는 열이 모두 `events_report` 인덱스에 있어서 테이블을 건드리지
/// 않습니다. 이 성질이 유지되는지는 테스트가 확인합니다.
fn scan_sql() -> String {
    // Days are cut in the viewer's UTC offset, so the same data reads correctly from any zone.
    // 날짜는 보는 사람의 UTC 오프셋으로 자르므로, 같은 데이터를 어느 시간대에서 봐도 맞습니다.
    format!(
        "SELECT strftime(?1, ts + ?2, 'unixepoch') AS bucket, user_id, model, project, source, mods,
                {PROMPT_TOKENS} > ?3 AS long_prompt, {TOKEN_SUMS}
         FROM {REPORT_SOURCE} WHERE ts >= ?4 AND ts < ?5
         GROUP BY 1, 2, 3, 4, 5, 6, 7"
    )
}

/// Read one time range grouped by bucket, dimensions and price-relevant values.
/// 한 기간을 구간, 분류 기준, 가격에 영향을 주는 값으로 묶어서 읽습니다.
fn scan(
    conn: &Connection,
    pricing: &Pricing,
    bucket_format: Option<&str>,
    from_ts: i64,
    to_ts: i64,
    offset_min: i32,
) -> rusqlite::Result<Vec<Group>> {
    let mut stmt = conn.prepare_cached(&scan_sql())?;
    let rows = stmt.query_map(
        params![
            bucket_format.unwrap_or(""),
            i64::from(offset_min) * 60,
            pricing.long_prompt_threshold(),
            from_ts,
            to_ts
        ],
        |row| {
            let model: String = row.get(2)?;
            let mods: u8 = row.get(5)?;
            let long_prompt: bool = row.get(6)?;
            let tokens = TokenCounts {
                input: row.get(8)?,
                output: row.get(9)?,
                cache_write_5m: row.get(10)?,
                cache_write_1h: row.get(11)?,
                cache_read: row.get(12)?,
                web_search_requests: row.get(14)?,
            };
            Ok(Group {
                bucket: row.get::<_, Option<String>>(0)?.unwrap_or_default(),
                user_id: row.get(1)?,
                cost: pricing.cost(&model, mods, long_prompt, &tokens),
                model,
                project: row.get(3)?,
                source: row.get(4)?,
                requests: row.get(7)?,
                tokens,
                thinking: row.get(13)?,
            })
        },
    )?;
    rows.collect()
}

/// Sort a breakdown by total tokens and fold the long tail into one row.
/// 분류별 집계를 토큰 합계 순으로 정렬하고, 긴 꼬리는 행 하나로 합칩니다.
fn ranked(map: HashMap<String, Agg>) -> Vec<Keyed> {
    let mut rows: Vec<Keyed> = map
        .into_iter()
        .map(|(key, agg)| Keyed {
            key,
            merged: 0,
            agg,
        })
        .collect();
    rows.sort_by(|a, b| {
        b.agg
            .total_tokens()
            .cmp(&a.agg.total_tokens())
            .then_with(|| a.key.cmp(&b.key))
    });
    if rows.len() > MAX_BREAKDOWN_ROWS {
        let tail = rows.split_off(MAX_BREAKDOWN_ROWS);
        let mut others = Keyed {
            key: OTHERS_KEY.to_string(),
            merged: tail.len(),
            agg: Agg::default(),
        };
        tail.iter().for_each(|row| others.agg.merge(&row.agg));
        rows.push(others);
    }
    rows
}

/// SQL of the session query for the given filters, with the values to bind in order. Like the
/// main scan it reads only columns of the `events_report` index.
/// 주어진 필터에 대한 세션 조회 SQL과, 순서대로 묶을 값들입니다. 주 조회와 마찬가지로
/// `events_report` 인덱스에 있는 열만 읽습니다.
fn sessions_query(
    long_prompt_threshold: i64,
    filters: &Filters,
    from_ts: i64,
    to_ts: i64,
) -> (String, Vec<Value>) {
    let mut sql = format!(
        "SELECT user_id, session_id, model, mods, {PROMPT_TOKENS} > ?1 AS long_prompt,
                MIN(ts), MAX(ts), MAX(project), MAX(source), {TOKEN_SUMS}
         FROM {REPORT_SOURCE} WHERE ts >= ?2 AND ts < ?3 AND session_id != ''"
    );
    let mut values: Vec<Value> = vec![
        Value::Integer(long_prompt_threshold),
        Value::Integer(from_ts),
        Value::Integer(to_ts),
    ];
    let mut add_filter = |column: &str, value: Value| {
        values.push(value);
        sql.push_str(&format!(" AND {column} = ?{}", values.len()));
    };
    if let Some(id) = filters.user_id {
        add_filter("user_id", Value::Integer(id));
    }
    if let Some(model) = filters.model {
        add_filter("model", Value::Text(model.to_string()));
    }
    if let Some(project) = filters.project {
        add_filter("project", Value::Text(project.to_string()));
    }
    sql.push_str(" GROUP BY user_id, session_id, model, mods, long_prompt");
    (sql, values)
}

/// The most expensive sessions in the slice, and how many sessions the slice has in total.
/// 조회 범위에서 비용이 가장 큰 세션들과, 범위 안의 전체 세션 수를 돌려줍니다.
fn top_sessions(
    conn: &Connection,
    pricing: &Pricing,
    users: &HashMap<i64, String>,
    filters: &Filters,
    from_ts: i64,
    to_ts: i64,
) -> rusqlite::Result<(Vec<SessionRow>, usize)> {
    let (sql, values) = sessions_query(pricing.long_prompt_threshold(), filters, from_ts, to_ts);

    struct Acc {
        row: SessionRow,
        models: BTreeSet<String>,
    }
    let mut sessions: HashMap<(i64, String), Acc> = HashMap::new();
    let mut stmt = conn.prepare_cached(&sql)?;
    let mut rows = stmt.query(params_from_iter(values))?;
    while let Some(row) = rows.next()? {
        let user_id: i64 = row.get(0)?;
        let session_id: String = row.get(1)?;
        let model: String = row.get(2)?;
        let mods: u8 = row.get(3)?;
        let long_prompt: bool = row.get(4)?;
        let first_ts: i64 = row.get(5)?;
        let last_ts: i64 = row.get(6)?;
        let tokens = TokenCounts {
            input: row.get(10)?,
            output: row.get(11)?,
            cache_write_5m: row.get(12)?,
            cache_write_1h: row.get(13)?,
            cache_read: row.get(14)?,
            web_search_requests: row.get(16)?,
        };
        let group = Group {
            bucket: String::new(),
            user_id,
            cost: pricing.cost(&model, mods, long_prompt, &tokens),
            model: model.clone(),
            project: row.get(7)?,
            source: row.get(8)?,
            requests: row.get(9)?,
            tokens,
            thinking: row.get(15)?,
        };
        let acc = sessions
            .entry((user_id, session_id.clone()))
            .or_insert_with(|| Acc {
                row: SessionRow {
                    user: users
                        .get(&user_id)
                        .cloned()
                        .unwrap_or_else(|| format!("#{user_id}")),
                    session_id,
                    project: group.project.clone(),
                    source: group.source.clone(),
                    models: Vec::new(),
                    first_ts,
                    last_ts,
                    agg: Agg::default(),
                },
                models: BTreeSet::new(),
            });
        acc.row.first_ts = acc.row.first_ts.min(first_ts);
        acc.row.last_ts = acc.row.last_ts.max(last_ts);
        acc.row.agg.add(&group);
        acc.models.insert(model);
    }

    let total = sessions.len();
    let mut list: Vec<SessionRow> = sessions
        .into_values()
        .map(|acc| SessionRow {
            models: acc.models.into_iter().collect(),
            ..acc.row
        })
        .collect();
    list.sort_by(|a, b| {
        b.agg
            .cost_usd
            .total_cmp(&a.agg.cost_usd)
            .then_with(|| b.agg.total_tokens().cmp(&a.agg.total_tokens()))
            .then_with(|| a.session_id.cmp(&b.session_id))
    });
    list.truncate(MAX_SESSIONS);
    Ok((list, total))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::{self, AuthedUser};
    use crate::db::TempDb;
    use crate::ingest::ingest;
    use serde_json::json;
    use std::collections::HashSet;

    /// Keys of a breakdown as a set, for comparisons that do not care about order.
    /// 분류별 집계의 키를 집합으로 돌려줍니다. 순서와 무관하게 비교할 때 씁니다.
    fn keys(rows: &[Keyed]) -> HashSet<&str> {
        rows.iter().map(|r| r.key.as_str()).collect()
    }

    fn add_user(temp: &TempDb, name: &str) -> AuthedUser {
        temp.db
            .write(|conn| {
                let user_id = auth::create_user(conn, name)?.unwrap();
                let (token_id, _) = auth::create_token(conn, user_id, "")?;
                Ok(AuthedUser {
                    user_id,
                    user_name: name.into(),
                    token_id,
                })
            })
            .unwrap()
    }

    #[allow(clippy::too_many_arguments)]
    fn event(
        id: &str,
        ts: &str,
        model: &str,
        project: &str,
        session: &str,
        input: u64,
        output: u64,
        read: u64,
    ) -> serde_json::Value {
        json!({"id": id, "timestamp": ts, "model": model, "project": project, "session_id": session,
               "source": "claude-code",
               "usage": {"input_tokens": input, "output_tokens": output, "cache_read_input_tokens": read}})
    }

    fn report(temp: &TempDb, params: &ReportParams) -> Report {
        let pricing = Pricing::embedded();
        // "Now" is fixed at 2026-11-22 12:00 UTC so the tests do not depend on the clock.
        // 테스트가 시계에 의존하지 않도록 "지금"을 2026-11-22 12:00 UTC로 고정합니다.
        let now = timeutil::parse_rfc3339("2026-11-22T12:00:00Z").unwrap();
        temp.db
            .read(|conn| Ok(build_report(conn, &pricing, params, now)))
            .unwrap()
            .unwrap()
    }

    fn day(s: &str) -> Option<NaiveDate> {
        timeutil::parse_day(s)
    }

    fn seed() -> TempDb {
        let temp = TempDb::new();
        let alice = add_user(&temp, "alice");
        let bob = add_user(&temp, "bob");
        let alice_events = json!({"events": [
            event("a1", "2026-11-20T01:00:00Z", "claude-fable-5-2", "web", "s1", 100, 1000, 10_000),
            event("a2", "2026-11-20T02:00:00Z", "claude-fable-5-2", "web", "s1", 100, 1000, 10_000),
            // 16:30 UTC on the 21st is already the 22nd in Korea.
            // 21일 16:30 UTC는 한국에서는 이미 22일입니다.
            event("a3", "2026-11-21T16:30:00Z", "claude-opus-5-5", "api", "s2", 200, 2000, 0),
            event("a4", "2026-11-10T03:00:00Z", "claude-fable-5-2", "web", "s0", 50, 500, 0),
        ]});
        let bob_events = json!({"events": [
            event("b1", "2026-11-20T05:00:00Z", "claude-fable-5-2", "web", "s9", 1000, 5000, 0),
            event("b2", "2026-11-21T05:00:00Z", "mystery-model", "lab", "s9", 7, 7, 7),
        ]});
        ingest(&temp.db, &alice, alice_events.to_string().as_bytes(), 100).unwrap();
        ingest(&temp.db, &bob, bob_events.to_string().as_bytes(), 100).unwrap();
        temp
    }

    #[test]
    fn totals_buckets_and_breakdowns() {
        let temp = seed();
        let r = report(
            &temp,
            &ReportParams {
                from: day("2026-11-16"),
                to: day("2026-11-22"),
                offset_min: 540,
                ..Default::default()
            },
        );

        assert_eq!(
            r.range,
            RangeInfo {
                from: "2026-11-16".into(),
                to: "2026-11-22".into(),
                utc_offset_minutes: 540,
                bucket: "day"
            }
        );
        assert_eq!(r.totals.requests, 5);
        assert_eq!(r.totals.input_tokens, 100 + 100 + 200 + 1000 + 7);
        assert_eq!(r.totals.output_tokens, 1000 + 1000 + 2000 + 5000 + 7);
        assert_eq!(r.totals.cache_read_tokens, 20_007);
        assert_eq!(r.totals.unpriced_requests, 1);
        assert_eq!(r.unpriced_models, vec!["mystery-model"]);
        assert_eq!(r.active_users, 2);
        assert_eq!(r.active_sessions, 3);

        // Fable 5.2: $8 in, $40 out, $0.20 cache read; Opus 5.5: $4 in, $20 out (per million).
        // Fable 5.2는 입력 $8, 출력 $40, 캐시 읽기 $0.20이고 Opus 5.5는 입력 $4, 출력 $20입니다(백만 토큰당).
        let fable = (1200.0 * 8.0 + 7000.0 * 40.0 + 20_000.0 * 0.2) / 1e6;
        let opus = (200.0 * 4.0 + 2000.0 * 20.0) / 1e6;
        assert!(
            (r.totals.cost_usd - (fable + opus)).abs() < 1e-12,
            "{}",
            r.totals.cost_usd
        );
        assert!((r.totals.cost.output - (7000.0 * 40.0 + 2000.0 * 20.0) / 1e6).abs() < 1e-12);

        // Days are local: a3 lands on the 22nd in +09:00.
        // 날짜는 현지 기준입니다. a3는 +09:00에서 22일에 들어갑니다.
        let by_day: Vec<(&str, i64)> = r
            .buckets
            .iter()
            .map(|b| (b.key.as_str(), b.agg.requests))
            .collect();
        assert_eq!(
            by_day,
            vec![("2026-11-20", 3), ("2026-11-21", 1), ("2026-11-22", 1)]
        );

        assert_eq!(
            r.by_user.iter().map(|u| u.key.as_str()).collect::<Vec<_>>(),
            vec!["alice", "bob"]
        );
        assert_eq!(
            keys(&r.by_model),
            HashSet::from(["claude-fable-5-2", "claude-opus-5-5", "mystery-model"])
        );
        assert_eq!(keys(&r.by_project), HashSet::from(["web", "api", "lab"]));
        assert_eq!(r.by_project[0].key, "web");
        assert_eq!(r.options.users, vec!["alice", "bob"]);
        assert_eq!(r.options.projects, vec!["api", "lab", "web"]);

        // The 7 days before the range hold only a4.
        // 조회 기간 직전 7일에는 a4만 있습니다.
        assert_eq!(
            r.previous_range,
            Some(DayRange {
                from: "2026-11-09".into(),
                to: "2026-11-15".into()
            })
        );
        assert_eq!(r.previous_totals.as_ref().unwrap().requests, 1);
        assert_eq!(r.previous_totals.as_ref().unwrap().output_tokens, 500);
    }

    #[test]
    fn utc_offset_moves_day_boundaries() {
        let temp = seed();
        let r = report(
            &temp,
            &ReportParams {
                from: day("2026-11-16"),
                to: day("2026-11-22"),
                offset_min: 0,
                ..Default::default()
            },
        );
        let by_day: Vec<(&str, i64)> = r
            .buckets
            .iter()
            .map(|b| (b.key.as_str(), b.agg.requests))
            .collect();
        assert_eq!(by_day, vec![("2026-11-20", 3), ("2026-11-21", 2)]);
    }

    #[test]
    fn filters_narrow_everything_but_the_choices() {
        let temp = seed();
        let base = ReportParams {
            from: day("2026-11-16"),
            to: day("2026-11-22"),
            offset_min: 540,
            ..Default::default()
        };

        let alice = report(
            &temp,
            &ReportParams {
                user: Some("alice".into()),
                ..base.clone()
            },
        );
        assert_eq!(alice.totals.requests, 3);
        assert_eq!(alice.active_users, 1);
        assert_eq!(alice.active_sessions, 2);
        assert_eq!(alice.options.users, vec!["alice", "bob"]);
        assert!(alice.unpriced_models.is_empty());
        assert_eq!(alice.previous_totals.as_ref().unwrap().requests, 1);

        let web_fable = report(
            &temp,
            &ReportParams {
                model: Some("claude-fable-5-2".into()),
                project: Some("web".into()),
                ..base.clone()
            },
        );
        assert_eq!(web_fable.totals.requests, 3);
        assert_eq!(web_fable.by_user.len(), 2);
        assert!(
            web_fable
                .sessions
                .iter()
                .all(|s| s.models == vec!["claude-fable-5-2"])
        );

        let nobody = report(
            &temp,
            &ReportParams {
                user: Some("carol".into()),
                ..base
            },
        );
        assert_eq!(nobody.totals, Agg::default());
        assert!(nobody.buckets.is_empty());
        assert!(nobody.sessions.is_empty());
    }

    #[test]
    fn sessions_are_ranked_by_cost() {
        let temp = seed();
        let r = report(
            &temp,
            &ReportParams {
                from: day("2026-11-16"),
                to: day("2026-11-22"),
                offset_min: 540,
                ..Default::default()
            },
        );
        let order: Vec<(&str, &str)> = r
            .sessions
            .iter()
            .map(|s| (s.user.as_str(), s.session_id.as_str()))
            .collect();
        assert_eq!(order, vec![("bob", "s9"), ("alice", "s1"), ("alice", "s2")]);
        let s9 = &r.sessions[0];
        assert_eq!(s9.models, vec!["claude-fable-5-2", "mystery-model"]);
        assert_eq!(s9.agg.requests, 2);
        assert_eq!(s9.agg.unpriced_requests, 1);
        assert_eq!(
            s9.first_ts,
            timeutil::parse_rfc3339("2026-11-20T05:00:00Z").unwrap()
        );
        assert_eq!(
            s9.last_ts,
            timeutil::parse_rfc3339("2026-11-21T05:00:00Z").unwrap()
        );
    }

    #[test]
    fn short_ranges_use_hour_buckets() {
        let temp = seed();
        let r = report(
            &temp,
            &ReportParams {
                from: day("2026-11-20"),
                to: day("2026-11-20"),
                offset_min: 540,
                ..Default::default()
            },
        );
        assert_eq!(r.range.bucket, "hour");
        let keys: Vec<&str> = r.buckets.iter().map(|b| b.key.as_str()).collect();
        assert_eq!(
            keys,
            vec!["2026-11-20T10", "2026-11-20T11", "2026-11-20T14"]
        );
    }

    #[test]
    fn open_start_covers_everything_and_has_no_comparison() {
        let temp = seed();
        let r = report(
            &temp,
            &ReportParams {
                offset_min: 540,
                ..Default::default()
            },
        );
        assert_eq!(r.range.from, "2026-11-10");
        assert_eq!(r.range.to, "2026-11-22");
        assert_eq!(r.totals.requests, 6);
        assert_eq!(r.previous_totals, None);

        let empty = TempDb::new();
        let r = report(
            &empty,
            &ReportParams {
                offset_min: 540,
                ..Default::default()
            },
        );
        assert_eq!(r.range.from, "2026-11-22");
        assert_eq!(r.totals, Agg::default());
    }

    #[test]
    fn rejects_bad_ranges() {
        let temp = TempDb::new();
        let pricing = Pricing::embedded();
        let run = |from: &str, to: &str| {
            let params = ReportParams {
                from: day(from),
                to: day(to),
                ..Default::default()
            };
            temp.db
                .read(|conn| Ok(build_report(conn, &pricing, &params, 0)))
                .unwrap()
        };
        assert!(matches!(
            run("2026-11-22", "2026-11-21"),
            Err(ReportError::BadRequest(_))
        ));
        assert!(matches!(
            run("2020-01-01", "2026-11-21"),
            Err(ReportError::BadRequest(_))
        ));
    }

    #[test]
    fn long_breakdowns_fold_into_others() {
        let mut map = HashMap::new();
        for i in 0..(MAX_BREAKDOWN_ROWS + 5) {
            map.insert(
                format!("p{i:03}"),
                Agg {
                    requests: 1,
                    input_tokens: 1000 - i as i64,
                    ..Default::default()
                },
            );
        }
        let rows = ranked(map);
        assert_eq!(rows.len(), MAX_BREAKDOWN_ROWS + 1);
        assert_eq!(rows[0].key, "p000");
        let others = rows.last().unwrap();
        assert_eq!(
            (others.key.as_str(), others.merged, others.agg.requests),
            (OTHERS_KEY, 5, 5)
        );
    }

    #[test]
    fn report_queries_run_from_the_index_alone() {
        // If a query starts reading a column that the index lacks, SQLite goes back to fetching
        // every row from the table and the plan no longer says "COVERING INDEX".
        // 조회가 인덱스에 없는 열을 읽기 시작하면 SQLite는 다시 행마다 테이블을 찾아가고,
        // 실행 계획에서 "COVERING INDEX"가 사라집니다.
        let temp = seed();
        let plan = |sql: &str, values: Vec<Value>| -> String {
            temp.db
                .read(|conn| {
                    let mut stmt = conn.prepare(&format!("EXPLAIN QUERY PLAN {sql}"))?;
                    let rows =
                        stmt.query_map(params_from_iter(values), |r| r.get::<_, String>(3))?;
                    rows.collect::<rusqlite::Result<Vec<String>>>()
                })
                .unwrap()
                .join(" | ")
        };

        let scan_plan = plan(
            &scan_sql(),
            vec![
                Value::Text("%Y-%m-%d".into()),
                Value::Integer(0),
                Value::Integer(100_000),
                Value::Integer(0),
                Value::Integer(10),
            ],
        );
        assert!(
            scan_plan.contains("COVERING INDEX events_report"),
            "{scan_plan}"
        );

        let all_filters = Filters {
            user_id: Some(1),
            model: Some("claude-fable-5-2"),
            project: Some("web"),
        };
        let no_filters = Filters {
            user_id: None,
            model: None,
            project: None,
        };
        for filters in [all_filters, no_filters] {
            let (sql, values) = sessions_query(100_000, &filters, 0, 10);
            let sessions_plan = plan(&sql, values);
            assert!(
                sessions_plan.contains("COVERING INDEX events_report"),
                "{sessions_plan}"
            );
        }
    }
}
