//! HTTP layer: routes, authentication, the embedded web UI and response headers.
//! HTTP 계층입니다. 경로, 인증, 내장 웹 화면, 응답 헤더를 맡습니다.

use crate::auth::{self, AuthedUser};
use crate::db::Db;
use crate::ingest::{self, IngestError};
use crate::pricing::Pricing;
use crate::report::{self, ReportError, ReportParams};
use crate::timeutil;
use axum::body::Bytes;
use axum::extract::rejection::{BytesRejection, QueryRejection};
use axum::extract::{DefaultBodyLimit, FromRequestParts, Path, Query, Request, State};
use axum::http::request::Parts;
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Redirect, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;
use std::sync::Arc;
use std::time::Instant;

// The web UI is compiled into the binary so a deployment is one file plus its database.
// 웹 화면을 바이너리에 포함하므로 배포물은 실행 파일 하나와 데이터베이스뿐입니다.
const INDEX_HTML: &str = include_str!("../web/index.html");
const LOGIN_HTML: &str = include_str!("../web/login.html");
const APP_CSS: &str = include_str!("../web/app.css");
const APP_JS: &str = include_str!("../web/app.js");
const CHARTS_JS: &str = include_str!("../web/charts.js");
const FORMAT_JS: &str = include_str!("../web/format.js");
const LOGIN_JS: &str = include_str!("../web/login.js");
const FAVICON_SVG: &str = include_str!("../web/favicon.svg");

/// Everything is served from this origin, so scripts and styles from anywhere else are refused.
/// 모든 자원을 이 서버에서 내려 주므로, 다른 곳의 스크립트와 스타일은 거부합니다.
const CONTENT_SECURITY_POLICY: &str = "default-src 'none'; script-src 'self'; style-src 'self'; \
    img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

#[derive(Debug, Clone)]
pub struct Config {
    /// UTC offset used for day boundaries when a request does not name one.
    /// 요청이 시간대를 지정하지 않았을 때 날짜 경계에 쓰는 UTC 오프셋입니다.
    pub default_offset_min: i32,
    /// Mark the session cookie Secure. Turn on when the site is served over HTTPS.
    /// 세션 쿠키에 Secure를 붙입니다. 사이트를 HTTPS로 제공할 때 켭니다.
    pub cookie_secure: bool,
    pub max_events_per_request: usize,
    pub max_body_bytes: usize,
    /// Print one line per request to stdout.
    /// 요청마다 한 줄씩 표준 출력에 기록합니다.
    pub access_log: bool,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            default_offset_min: 9 * 60,
            cookie_secure: false,
            max_events_per_request: 5000,
            max_body_bytes: 16 * 1024 * 1024,
            access_log: false,
        }
    }
}

#[derive(Clone)]
pub struct AppState {
    pub db: Arc<Db>,
    pub pricing: Arc<Pricing>,
    pub config: Arc<Config>,
}

pub fn router(state: AppState) -> Router {
    let max_body = state.config.max_body_bytes;
    Router::new()
        .route("/", get(index_page))
        .route("/login", get(login_page))
        .route("/assets/{name}", get(asset))
        .route("/favicon.svg", get(favicon))
        .route("/healthz", get(healthz))
        .route("/api/login", post(login))
        .route("/api/logout", post(logout))
        .route("/api/v1/me", get(me))
        .route("/api/v1/events", post(post_events))
        .route("/api/v1/report", get(get_report))
        // Unknown paths and wrong methods answer in the same JSON shape as every other error.
        // 없는 경로와 잘못된 메서드도 다른 오류와 같은 JSON 형식으로 응답합니다.
        .fallback(not_found)
        .method_not_allowed_fallback(method_not_allowed)
        .layer(DefaultBodyLimit::max(max_body))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            common_headers,
        ))
        .with_state(state)
}

/// Serve until SIGINT or SIGTERM arrives.
/// SIGINT나 SIGTERM을 받을 때까지 서비스합니다.
pub async fn serve(listener: tokio::net::TcpListener, state: AppState) -> std::io::Result<()> {
    axum::serve(listener, router(state))
        .with_graceful_shutdown(shutdown_signal())
        .await
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut signal) => {
                signal.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

// ---------------------------------------------------------------------------------------------
// Errors
// 오류
// ---------------------------------------------------------------------------------------------

/// An error answered as JSON: {"error": "..."}.
/// JSON {"error": "..."}으로 응답하는 오류입니다.
#[derive(Debug)]
pub struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    fn new(status: StatusCode, message: impl Into<String>) -> ApiError {
        ApiError {
            status,
            message: message.into(),
        }
    }

    fn bad_request(message: impl Into<String>) -> ApiError {
        ApiError::new(StatusCode::BAD_REQUEST, message)
    }

    fn unauthorized(message: impl Into<String>) -> ApiError {
        ApiError::new(StatusCode::UNAUTHORIZED, message)
    }

    /// Log the cause on the server and tell the client nothing about it.
    /// 원인은 서버에만 기록하고 클라이언트에는 알리지 않습니다.
    fn internal(cause: impl std::fmt::Display) -> ApiError {
        eprintln!("internal error: {cause}");
        ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "internal error")
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({ "error": self.message }))).into_response()
    }
}

/// Run blocking SQLite work off the async worker threads.
/// 블로킹 SQLite 작업을 비동기 작업 스레드 밖에서 실행합니다.
async fn blocking<T, F>(work: F) -> Result<T, ApiError>
where
    F: FnOnce() -> rusqlite::Result<T> + Send + 'static,
    T: Send + 'static,
{
    match tokio::task::spawn_blocking(work).await {
        Ok(Ok(value)) => Ok(value),
        Ok(Err(e)) => Err(ApiError::internal(e)),
        Err(e) => Err(ApiError::internal(e)),
    }
}

// ---------------------------------------------------------------------------------------------
// Authentication
// 인증
// ---------------------------------------------------------------------------------------------

/// A collector, authenticated only by a bearer token.
/// 수집 프로세스입니다. Bearer 토큰으로만 인증합니다.
pub struct Collector(pub AuthedUser);

/// A dashboard reader, authenticated by the session cookie or a bearer token.
/// 대시보드를 읽는 쪽입니다. 세션 쿠키나 Bearer 토큰으로 인증합니다.
pub struct Viewer(pub AuthedUser);

fn bearer_token(headers: &HeaderMap) -> Option<String> {
    let value = headers.get(header::AUTHORIZATION)?.to_str().ok()?;
    let (scheme, token) = value.split_once(' ')?;
    let token = token.trim();
    (scheme.eq_ignore_ascii_case("bearer") && !token.is_empty()).then(|| token.to_string())
}

fn session_cookie(headers: &HeaderMap) -> Option<String> {
    for value in headers.get_all(header::COOKIE) {
        let Ok(text) = value.to_str() else { continue };
        for pair in text.split(';') {
            if let Some((name, value)) = pair.trim().split_once('=')
                && name == auth::SESSION_COOKIE
                && !value.is_empty()
            {
                return Some(value.to_string());
            }
        }
    }
    None
}

async fn user_from_bearer(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<Option<AuthedUser>, ApiError> {
    let Some(token) = bearer_token(headers) else {
        return Ok(None);
    };
    let db = state.db.clone();
    blocking(move || db.read(|conn| auth::lookup_token(conn, &token))).await
}

async fn user_from_session(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<Option<AuthedUser>, ApiError> {
    let Some(session_id) = session_cookie(headers) else {
        return Ok(None);
    };
    let db = state.db.clone();
    blocking(move || db.read(|conn| auth::lookup_session(conn, &session_id))).await
}

impl FromRequestParts<AppState> for Collector {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, ApiError> {
        match user_from_bearer(state, &parts.headers).await? {
            Some(user) => Ok(Collector(user)),
            None => Err(ApiError::unauthorized(
                "missing or invalid token; send 'Authorization: Bearer <token>'",
            )),
        }
    }
}

impl FromRequestParts<AppState> for Viewer {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, ApiError> {
        if let Some(user) = user_from_bearer(state, &parts.headers).await? {
            return Ok(Viewer(user));
        }
        match user_from_session(state, &parts.headers).await? {
            Some(user) => Ok(Viewer(user)),
            None => Err(ApiError::unauthorized("not signed in")),
        }
    }
}

/// Cross-site forms cannot send this content type without a CORS preflight, which is never
/// granted here, so requiring it keeps other sites from signing a browser in or out.
/// 다른 사이트의 폼은 CORS 사전 요청 없이는 이 콘텐츠 형식을 보낼 수 없고, 이 서버는 사전 요청을
/// 허용하지 않습니다. 그래서 이 형식을 요구하면 다른 사이트가 브라우저를 로그인/로그아웃시킬 수 없습니다.
fn require_json(headers: &HeaderMap) -> Result<(), ApiError> {
    let is_json = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| {
            v.trim()
                .to_ascii_lowercase()
                .starts_with("application/json")
        });
    if is_json {
        Ok(())
    } else {
        Err(ApiError::new(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "Content-Type must be application/json",
        ))
    }
}

fn cookie_header(value: &str, max_age: i64, secure: bool) -> HeaderValue {
    let secure = if secure { "; Secure" } else { "" };
    let text = format!(
        "{}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}",
        auth::SESSION_COOKIE
    );
    HeaderValue::from_str(&text).expect("session cookie is ASCII")
}

// ---------------------------------------------------------------------------------------------
// Handlers
// 핸들러
// ---------------------------------------------------------------------------------------------

async fn healthz() -> &'static str {
    "ok\n"
}

async fn not_found() -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, "not found")
}

async fn method_not_allowed() -> ApiError {
    ApiError::new(StatusCode::METHOD_NOT_ALLOWED, "method not allowed")
}

/// Unwrap a request body, turning a refused one (too large, broken upload) into a JSON error.
/// 요청 본문을 꺼냅니다. 거부된 본문(너무 큼, 전송 중 끊김)은 JSON 오류로 바꿉니다.
fn request_body(body: Result<Bytes, BytesRejection>) -> Result<Bytes, ApiError> {
    body.map_err(|rejection| {
        let status = rejection.status();
        let message = if status == StatusCode::PAYLOAD_TOO_LARGE {
            "request body is too large".to_string()
        } else {
            rejection.body_text()
        };
        ApiError::new(status, message)
    })
}

fn html(body: &'static str) -> Response {
    ([(header::CONTENT_TYPE, "text/html; charset=utf-8")], body).into_response()
}

async fn index_page(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    match user_from_session(&state, &headers).await? {
        Some(_) => Ok(html(INDEX_HTML)),
        None => Ok(Redirect::to("/login").into_response()),
    }
}

async fn login_page() -> Response {
    html(LOGIN_HTML)
}

async fn asset(Path(name): Path<String>) -> Response {
    let (body, content_type) = match name.as_str() {
        "app.css" => (APP_CSS, "text/css; charset=utf-8"),
        "app.js" => (APP_JS, "text/javascript; charset=utf-8"),
        "charts.js" => (CHARTS_JS, "text/javascript; charset=utf-8"),
        "format.js" => (FORMAT_JS, "text/javascript; charset=utf-8"),
        "login.js" => (LOGIN_JS, "text/javascript; charset=utf-8"),
        _ => return ApiError::new(StatusCode::NOT_FOUND, "not found").into_response(),
    };
    ([(header::CONTENT_TYPE, content_type)], body).into_response()
}

async fn favicon() -> Response {
    ([(header::CONTENT_TYPE, "image/svg+xml")], FAVICON_SVG).into_response()
}

#[derive(Deserialize)]
struct LoginBody {
    token: String,
}

async fn login(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<Response, ApiError> {
    require_json(&headers)?;
    let body = request_body(body)?;
    let body: LoginBody = serde_json::from_slice(&body)
        .map_err(|_| ApiError::bad_request("body must be JSON like {\"token\": \"...\"}"))?;
    let token = body.token.trim().to_string();
    let db = state.db.clone();
    // A wrong token is answered from a read-only connection. Only a valid one takes the single
    // writer connection, so a flood of bad sign-in attempts cannot hold up collectors that are
    // writing.
    // 틀린 토큰은 읽기 전용 연결로 응답합니다. 올바른 토큰만 하나뿐인 쓰기 연결을 쓰므로, 잘못된
    // 로그인 시도가 몰려도 기록을 쓰고 있는 수집 프로세스를 막지 못합니다.
    let signed_in = blocking(move || {
        let Some(user) = db.read(|conn| auth::lookup_token(conn, &token))? else {
            return Ok(None);
        };
        let session_id = db.write(|conn| auth::create_session(conn, user.token_id))?;
        Ok(Some((user, session_id)))
    })
    .await?;
    match signed_in {
        Some((user, session_id)) => {
            let cookie = cookie_header(
                &session_id,
                auth::SESSION_TTL_SECS,
                state.config.cookie_secure,
            );
            Ok((
                [(header::SET_COOKIE, cookie)],
                Json(json!({ "user": user.user_name })),
            )
                .into_response())
        }
        None => Err(ApiError::unauthorized("invalid token")),
    }
}

async fn logout(State(state): State<AppState>, headers: HeaderMap) -> Result<Response, ApiError> {
    require_json(&headers)?;
    if let Some(session_id) = session_cookie(&headers) {
        let db = state.db.clone();
        // As with sign-in, only a real session reaches the writer connection.
        // 로그인과 마찬가지로, 실제로 있는 세션만 쓰기 연결까지 갑니다.
        blocking(move || {
            if db
                .read(|conn| auth::lookup_session(conn, &session_id))?
                .is_some()
            {
                db.write(|conn| auth::delete_session(conn, &session_id))?;
            }
            Ok(())
        })
        .await?;
    }
    let cookie = cookie_header("", 0, state.config.cookie_secure);
    Ok(([(header::SET_COOKIE, cookie)], Json(json!({ "ok": true }))).into_response())
}

async fn me(Viewer(user): Viewer) -> Json<serde_json::Value> {
    Json(json!({ "user": user.user_name }))
}

async fn post_events(
    State(state): State<AppState>,
    Collector(user): Collector,
    body: Result<Bytes, BytesRejection>,
) -> Result<Response, ApiError> {
    let body = request_body(body)?;
    let db = state.db.clone();
    let max_events = state.config.max_events_per_request;
    let outcome =
        tokio::task::spawn_blocking(move || ingest::ingest(&db, &user, &body, max_events))
            .await
            .map_err(ApiError::internal)?;
    match outcome {
        Ok(summary) => Ok(Json(summary).into_response()),
        Err(IngestError::BadRequest(message)) => Err(ApiError::bad_request(message)),
        Err(IngestError::Storage(cause)) => Err(ApiError::internal(cause)),
    }
}

#[derive(Debug, Deserialize)]
struct ReportQuery {
    from: Option<String>,
    to: Option<String>,
    tz: Option<String>,
    user: Option<String>,
    model: Option<String>,
    project: Option<String>,
}

async fn get_report(
    State(state): State<AppState>,
    Viewer(_viewer): Viewer,
    query: Result<Query<ReportQuery>, QueryRejection>,
) -> Result<Response, ApiError> {
    let Query(query) = query.map_err(|e| ApiError::bad_request(e.body_text()))?;
    let parse_day = |name: &str, value: &Option<String>| match value.as_deref() {
        None | Some("") => Ok(None),
        Some(text) => timeutil::parse_day(text).map(Some).ok_or_else(|| {
            ApiError::bad_request(format!("'{name}' must be a date like 2026-11-22"))
        }),
    };
    let offset_min = match query.tz.as_deref() {
        None | Some("") => state.config.default_offset_min,
        Some(text) => timeutil::parse_utc_offset(text).map_err(ApiError::bad_request)?,
    };
    let params = ReportParams {
        from: parse_day("from", &query.from)?,
        to: parse_day("to", &query.to)?,
        offset_min,
        user: query.user,
        model: query.model,
        project: query.project,
    };

    let db = state.db.clone();
    let pricing = state.pricing.clone();
    let outcome = blocking(move || {
        db.read(|conn| {
            Ok(report::build_report(
                conn,
                &pricing,
                &params,
                timeutil::now_unix(),
            ))
        })
    })
    .await?;
    match outcome {
        Ok(report) => Ok(Json(report).into_response()),
        Err(ReportError::BadRequest(message)) => Err(ApiError::bad_request(message)),
        Err(ReportError::Storage(cause)) => Err(ApiError::internal(cause)),
    }
}

// ---------------------------------------------------------------------------------------------
// Response headers and the access log
// 응답 헤더와 접근 기록
// ---------------------------------------------------------------------------------------------

async fn common_headers(State(state): State<AppState>, request: Request, next: Next) -> Response {
    let method = request.method().clone();
    // Only the path is logged; query strings and headers never are.
    // 경로만 기록합니다. 쿼리 문자열과 헤더는 기록하지 않습니다.
    let path = request.uri().path().to_string();
    let started = Instant::now();

    let mut response = next.run(request).await;

    let headers = response.headers_mut();
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(CONTENT_SECURITY_POLICY),
    );
    // Usage data and pages must not linger in shared caches or browser history.
    // 사용량 데이터와 화면이 공유 캐시나 브라우저 기록에 남지 않게 합니다.
    headers
        .entry(header::CACHE_CONTROL)
        .or_insert(HeaderValue::from_static("no-store"));

    if state.config.access_log && path != "/healthz" {
        println!(
            "{} {method} {path} {} {}ms",
            timeutil::format_rfc3339_utc(timeutil::now_unix()),
            response.status().as_u16(),
            started.elapsed().as_millis()
        );
    }
    response
}
