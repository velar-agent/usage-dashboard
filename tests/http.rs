//! HTTP-level tests: every request goes through the real router, auth and headers.
//! HTTP 수준 테스트입니다. 모든 요청이 실제 라우터, 인증, 헤더 처리를 거칩니다.

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use std::sync::Arc;
use tower::ServiceExt;
use usage_dashboard::auth;
use usage_dashboard::db::TempDb;
use usage_dashboard::pricing::Pricing;
use usage_dashboard::server::{AppState, Config, router};

struct TestApp {
    temp: TempDb,
    app: Router,
}

struct Reply {
    status: StatusCode,
    headers: axum::http::HeaderMap,
    body: Vec<u8>,
}

impl Reply {
    fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap_or_else(|e| {
            panic!(
                "body is not JSON ({e}): {}",
                String::from_utf8_lossy(&self.body)
            )
        })
    }

    fn header(&self, name: header::HeaderName) -> &str {
        self.headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
    }
}

impl TestApp {
    fn new() -> TestApp {
        TestApp::with_config(Config::default())
    }

    fn with_config(config: Config) -> TestApp {
        let temp = TempDb::new();
        let state = AppState {
            db: temp.db.clone(),
            pricing: Arc::new(Pricing::embedded()),
            config: Arc::new(config),
        };
        TestApp {
            app: router(state),
            temp,
        }
    }

    /// Create a user and return their token.
    /// 사용자를 만들고 그 토큰을 돌려줍니다.
    fn add_user(&self, name: &str) -> String {
        self.temp
            .db
            .write(|conn| {
                let user_id = auth::create_user(conn, name)?.expect("new user");
                Ok(auth::create_token(conn, user_id, "test")?.1)
            })
            .unwrap()
    }

    async fn send(&self, request: Request<Body>) -> Reply {
        let response = self
            .app
            .clone()
            .oneshot(request)
            .await
            .expect("router never fails");
        let status = response.status();
        let headers = response.headers().clone();
        let body = response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes()
            .to_vec();
        Reply {
            status,
            headers,
            body,
        }
    }

    async fn get(&self, path: &str, auth: Auth<'_>) -> Reply {
        self.send(auth.apply(Request::get(path)).body(Body::empty()).unwrap())
            .await
    }

    async fn post_json(&self, path: &str, auth: Auth<'_>, body: Value) -> Reply {
        let request = auth
            .apply(Request::post(path))
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(body.to_string()))
            .unwrap();
        self.send(request).await
    }
}

#[derive(Clone, Copy)]
enum Auth<'a> {
    None,
    Bearer(&'a str),
    Cookie(&'a str),
}

impl Auth<'_> {
    fn apply(self, builder: axum::http::request::Builder) -> axum::http::request::Builder {
        match self {
            Auth::None => builder,
            Auth::Bearer(token) => builder.header(header::AUTHORIZATION, format!("Bearer {token}")),
            Auth::Cookie(cookie) => builder.header(header::COOKIE, cookie),
        }
    }
}

fn event(id: &str, timestamp: &str, output: u64) -> Value {
    json!({
        "id": id,
        "timestamp": timestamp,
        "model": "claude-fable-5-2",
        "source": "claude-code",
        "session_id": "session-1",
        "project": "demo",
        "usage": {"input_tokens": 10, "output_tokens": output, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 100,
                  "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 100}}
    })
}

/// Sign in and return the "name=value" cookie pair to send back.
/// 로그인하고, 다시 보낼 "이름=값" 쿠키 쌍을 돌려줍니다.
async fn sign_in(app: &TestApp, token: &str) -> String {
    let reply = app
        .post_json("/api/login", Auth::None, json!({"token": token}))
        .await;
    assert_eq!(
        reply.status,
        StatusCode::OK,
        "{}",
        String::from_utf8_lossy(&reply.body)
    );
    let set_cookie = reply.header(header::SET_COOKIE).to_string();
    set_cookie.split(';').next().unwrap().to_string()
}

#[tokio::test]
async fn health_check_needs_no_auth() {
    let app = TestApp::new();
    let reply = app.get("/healthz", Auth::None).await;
    assert_eq!(reply.status, StatusCode::OK);
    assert_eq!(reply.body, b"ok\n");
}

#[tokio::test]
async fn every_response_carries_security_headers() {
    let app = TestApp::new();
    for path in ["/login", "/api/v1/report", "/assets/app.js", "/nope"] {
        let reply = app.get(path, Auth::None).await;
        assert!(
            reply
                .header(header::CONTENT_SECURITY_POLICY)
                .contains("default-src 'none'"),
            "{path}"
        );
        assert_eq!(
            reply.header(header::X_CONTENT_TYPE_OPTIONS),
            "nosniff",
            "{path}"
        );
        assert_eq!(reply.header(header::X_FRAME_OPTIONS), "DENY", "{path}");
        assert_eq!(reply.header(header::CACHE_CONTROL), "no-store", "{path}");
    }
}

#[tokio::test]
async fn pages_and_assets() {
    let app = TestApp::new();
    let token = app.add_user("alice");

    // The dashboard page sends visitors without a session to the login page.
    // 세션이 없는 방문자는 대시보드 화면에서 로그인 화면으로 보냅니다.
    let reply = app.get("/", Auth::None).await;
    assert_eq!(reply.status, StatusCode::SEE_OTHER);
    assert_eq!(reply.header(header::LOCATION), "/login");

    let login = app.get("/login", Auth::None).await;
    assert_eq!(login.status, StatusCode::OK);
    assert!(login.header(header::CONTENT_TYPE).starts_with("text/html"));

    let cookie = sign_in(&app, &token).await;
    let page = app.get("/", Auth::Cookie(&cookie)).await;
    assert_eq!(page.status, StatusCode::OK);
    assert!(page.header(header::CONTENT_TYPE).starts_with("text/html"));

    for (path, content_type) in [
        ("/assets/app.js", "text/javascript"),
        ("/assets/charts.js", "text/javascript"),
        ("/assets/format.js", "text/javascript"),
        ("/assets/login.js", "text/javascript"),
        ("/assets/app.css", "text/css"),
        ("/favicon.svg", "image/svg+xml"),
    ] {
        let reply = app.get(path, Auth::None).await;
        assert_eq!(reply.status, StatusCode::OK, "{path}");
        assert!(
            reply.header(header::CONTENT_TYPE).starts_with(content_type),
            "{path}"
        );
    }
    let missing = app.get("/assets/secret.txt", Auth::None).await;
    assert_eq!(missing.status, StatusCode::NOT_FOUND);
    assert_eq!(missing.json(), json!({"error": "not found"}));
}

#[tokio::test]
async fn sign_in_and_out() {
    let app = TestApp::new();
    let token = app.add_user("alice");

    let wrong = app
        .post_json("/api/login", Auth::None, json!({"token": "udt_wrong"}))
        .await;
    assert_eq!(wrong.status, StatusCode::UNAUTHORIZED);
    assert_eq!(wrong.json(), json!({"error": "invalid token"}));
    assert!(wrong.headers.get(header::SET_COOKIE).is_none());

    let malformed = app
        .post_json("/api/login", Auth::None, json!({"nope": 1}))
        .await;
    assert_eq!(malformed.status, StatusCode::BAD_REQUEST);

    // A cross-site form can only send form or plain-text bodies; those are refused.
    // 다른 사이트의 폼은 폼 형식이나 일반 텍스트 본문만 보낼 수 있고, 그런 요청은 거부합니다.
    let form = Request::post("/api/login")
        .header(header::CONTENT_TYPE, "text/plain")
        .body(Body::from(json!({"token": token}).to_string()))
        .unwrap();
    assert_eq!(
        app.send(form).await.status,
        StatusCode::UNSUPPORTED_MEDIA_TYPE
    );

    let reply = app
        .post_json(
            "/api/login",
            Auth::None,
            json!({"token": format!("  {token}\n")}),
        )
        .await;
    assert_eq!(reply.status, StatusCode::OK);
    assert_eq!(reply.json(), json!({"user": "alice"}));
    let set_cookie = reply.header(header::SET_COOKIE);
    assert!(set_cookie.starts_with("ud_session="));
    assert!(
        set_cookie.contains("HttpOnly")
            && set_cookie.contains("SameSite=Lax")
            && set_cookie.contains("Path=/")
    );
    assert!(!set_cookie.contains("Secure"));
    // The cookie holds a session id, never the token.
    // 쿠키에는 세션 id가 들어가며 토큰은 들어가지 않습니다.
    assert!(!set_cookie.contains(&token));
    let cookie = set_cookie.split(';').next().unwrap().to_string();

    let me = app.get("/api/v1/me", Auth::Cookie(&cookie)).await;
    assert_eq!(me.json(), json!({"user": "alice"}));
    let with_other_cookies = format!("theme=dark; {cookie}; x=1");
    assert_eq!(
        app.get("/api/v1/me", Auth::Cookie(&with_other_cookies))
            .await
            .status,
        StatusCode::OK
    );

    let out = app
        .post_json("/api/logout", Auth::Cookie(&cookie), json!({}))
        .await;
    assert_eq!(out.status, StatusCode::OK);
    assert!(out.header(header::SET_COOKIE).contains("Max-Age=0"));
    assert_eq!(
        app.get("/api/v1/me", Auth::Cookie(&cookie)).await.status,
        StatusCode::UNAUTHORIZED
    );
}

#[tokio::test]
async fn secure_cookie_when_configured() {
    let app = TestApp::with_config(Config {
        cookie_secure: true,
        ..Config::default()
    });
    let token = app.add_user("alice");
    let reply = app
        .post_json("/api/login", Auth::None, json!({"token": token}))
        .await;
    assert!(reply.header(header::SET_COOKIE).contains("; Secure"));
}

#[tokio::test]
async fn events_need_a_live_bearer_token() {
    let app = TestApp::new();
    let token = app.add_user("alice");
    let body = json!({"events": [event("msg_1", "2026-11-22T06:25:28Z", 500)]});

    let anonymous = app
        .post_json("/api/v1/events", Auth::None, body.clone())
        .await;
    assert_eq!(anonymous.status, StatusCode::UNAUTHORIZED);
    assert!(
        anonymous.json()["error"]
            .as_str()
            .unwrap()
            .contains("Bearer")
    );
    assert_eq!(
        app.post_json("/api/v1/events", Auth::Bearer("udt_nope"), body.clone())
            .await
            .status,
        StatusCode::UNAUTHORIZED
    );

    // A browser session is not a collector credential.
    // 브라우저 세션은 수집용 자격 증명이 아닙니다.
    let cookie = sign_in(&app, &token).await;
    assert_eq!(
        app.post_json("/api/v1/events", Auth::Cookie(&cookie), body.clone())
            .await
            .status,
        StatusCode::UNAUTHORIZED
    );

    let stored = app
        .post_json("/api/v1/events", Auth::Bearer(&token), body.clone())
        .await;
    assert_eq!(stored.status, StatusCode::OK);
    assert_eq!(
        stored.json(),
        json!({"received": 1, "inserted": 1, "duplicates": 0, "rejected": []})
    );
    let again = app
        .post_json("/api/v1/events", Auth::Bearer(&token), body.clone())
        .await;
    assert_eq!(
        again.json(),
        json!({"received": 1, "inserted": 0, "duplicates": 1, "rejected": []})
    );

    // Revoking the token stops both the collector and the browser session made from it.
    // 토큰을 폐기하면 수집 프로세스와, 그 토큰으로 만든 브라우저 세션이 모두 막힙니다.
    app.temp
        .db
        .write(|conn| auth::revoke_token(conn, 1))
        .unwrap();
    assert_eq!(
        app.post_json("/api/v1/events", Auth::Bearer(&token), body)
            .await
            .status,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        app.get("/api/v1/me", Auth::Cookie(&cookie)).await.status,
        StatusCode::UNAUTHORIZED
    );
}

#[tokio::test]
async fn events_report_what_was_rejected() {
    let app = TestApp::with_config(Config {
        max_events_per_request: 3,
        ..Config::default()
    });
    let token = app.add_user("alice");

    let mixed = json!({"events": [
        event("msg_ok", "2026-11-22T06:25:28Z", 500),
        {"id": "msg_bad", "model": "claude-fable-5-2", "usage": {}},
    ]});
    let reply = app
        .post_json("/api/v1/events", Auth::Bearer(&token), mixed)
        .await;
    assert_eq!(reply.status, StatusCode::OK);
    let summary = reply.json();
    assert_eq!(
        (summary["received"].as_u64(), summary["inserted"].as_u64()),
        (Some(2), Some(1))
    );
    assert_eq!(summary["rejected"][0]["index"], 1);
    assert_eq!(summary["rejected"][0]["id"], "msg_bad");
    assert!(
        summary["rejected"][0]["error"]
            .as_str()
            .unwrap()
            .contains("timestamp")
    );

    let not_json = Request::post("/api/v1/events")
        .header(header::AUTHORIZATION, format!("Bearer {token}"))
        .body(Body::from("hello"))
        .unwrap();
    let reply = app.send(not_json).await;
    assert_eq!(reply.status, StatusCode::BAD_REQUEST);
    assert!(reply.json()["error"].as_str().unwrap().contains("events"));

    let too_many: Vec<Value> = (0..4)
        .map(|i| event(&format!("m{i}"), "2026-11-22T06:25:28Z", 1))
        .collect();
    let reply = app
        .post_json(
            "/api/v1/events",
            Auth::Bearer(&token),
            json!({"events": too_many}),
        )
        .await;
    assert_eq!(reply.status, StatusCode::BAD_REQUEST);
    assert!(reply.json()["error"].as_str().unwrap().contains("too many"));
}

#[tokio::test]
async fn oversized_bodies_are_refused() {
    let app = TestApp::with_config(Config {
        max_body_bytes: 2048,
        ..Config::default()
    });
    let token = app.add_user("alice");
    let padding = "x".repeat(4096);
    let body = json!({"events": [], "padding": padding});
    let reply = app
        .post_json("/api/v1/events", Auth::Bearer(&token), body)
        .await;
    assert_eq!(reply.status, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(reply.json(), json!({"error": "request body is too large"}));
}

#[tokio::test]
async fn every_error_is_json() {
    let app = TestApp::new();
    let token = app.add_user("alice");

    let unknown = app.get("/api/v2/everything", Auth::Bearer(&token)).await;
    assert_eq!(unknown.status, StatusCode::NOT_FOUND);
    assert_eq!(unknown.json(), json!({"error": "not found"}));

    // The right path with the wrong method.
    // 경로는 맞지만 메서드가 틀린 경우입니다.
    let wrong_method = app.get("/api/v1/events", Auth::Bearer(&token)).await;
    assert_eq!(wrong_method.status, StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(wrong_method.json(), json!({"error": "method not allowed"}));
    let wrong_method = app
        .post_json("/api/v1/report", Auth::Bearer(&token), json!({}))
        .await;
    assert_eq!(wrong_method.status, StatusCode::METHOD_NOT_ALLOWED);
    assert!(wrong_method.json()["error"].is_string());
}

#[tokio::test]
async fn report_reflects_what_collectors_sent() {
    let app = TestApp::new();
    let alice = app.add_user("alice");
    let bob = app.add_user("bob");
    let alice_events = json!({"events": [
        event("a1", "2026-11-20T01:00:00Z", 1000),
        event("a2", "2026-11-21T01:00:00Z", 2000),
    ]});
    let bob_events = json!({"events": [event("b1", "2026-11-21T02:00:00Z", 4000)]});
    app.post_json("/api/v1/events", Auth::Bearer(&alice), alice_events)
        .await;
    app.post_json("/api/v1/events", Auth::Bearer(&bob), bob_events)
        .await;

    assert_eq!(
        app.get("/api/v1/report", Auth::None).await.status,
        StatusCode::UNAUTHORIZED
    );

    // Everyone signed in sees everyone's usage; a collector token works for scripts too.
    // 로그인한 사람은 모두의 사용량을 봅니다. 스크립트에서는 수집용 토큰으로도 조회할 수 있습니다.
    let cookie = sign_in(&app, &bob).await;
    let path = "/api/v1/report?from=2026-11-16&to=2026-11-22&tz=540";
    let by_cookie = app.get(path, Auth::Cookie(&cookie)).await;
    assert_eq!(by_cookie.status, StatusCode::OK);
    let report = by_cookie.json();
    assert_eq!(report, app.get(path, Auth::Bearer(&alice)).await.json());

    assert_eq!(
        report["range"],
        json!({"from": "2026-11-16", "to": "2026-11-22", "utc_offset_minutes": 540, "bucket": "day"})
    );
    assert_eq!(report["totals"]["requests"], 3);
    assert_eq!(report["totals"]["output_tokens"], 7000);
    assert_eq!(report["totals"]["cache_write_1h_tokens"], 300);
    // 30 input at $8, 7000 output at $40, 300 one-hour cache writes at $16, 3000 cache reads at $0.20.
    // 입력 30개 $8, 출력 7000개 $40, 1시간 캐시 쓰기 300개 $16, 캐시 읽기 3000개 $0.20입니다.
    let expected = (30.0 * 8.0 + 7000.0 * 40.0 + 300.0 * 16.0 + 3000.0 * 0.2) / 1e6;
    assert!((report["totals"]["cost_usd"].as_f64().unwrap() - expected).abs() < 1e-12);
    assert_eq!(report["active_users"], 2);
    // Breakdowns rank by total tokens (alice 5,220, bob 5,110); sessions rank by cost, and bob's
    // single request has the most output tokens.
    // 분류별 집계는 토큰 합계 순이고(alice 5,220, bob 5,110), 세션은 비용 순입니다. bob의 요청
    // 하나가 출력 토큰이 가장 많습니다.
    assert_eq!(report["by_user"][0]["key"], "alice");
    assert_eq!(report["by_user"][1]["key"], "bob");
    assert_eq!(report["buckets"].as_array().unwrap().len(), 2);
    assert_eq!(report["options"]["users"], json!(["alice", "bob"]));
    assert_eq!(report["sessions"][0]["user"], "bob");
    assert_eq!(report["prices_as_of"], Pricing::embedded().table().as_of);

    let only_alice = app
        .get(&format!("{path}&user=alice"), Auth::Cookie(&cookie))
        .await
        .json();
    assert_eq!(only_alice["totals"]["requests"], 2);
    assert_eq!(only_alice["options"]["users"], json!(["alice", "bob"]));

    for bad in [
        "?from=yesterday",
        "?from=2026-11-22&to=2026-11-01",
        "?tz=mars",
        "?from=2020-01-01&to=2026-11-22",
    ] {
        let reply = app
            .get(&format!("/api/v1/report{bad}"), Auth::Cookie(&cookie))
            .await;
        assert_eq!(reply.status, StatusCode::BAD_REQUEST, "{bad}");
        assert!(reply.json()["error"].is_string(), "{bad}");
    }
}
