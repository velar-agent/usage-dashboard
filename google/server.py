#!/usr/bin/env python3
"""usage-dashboard-google-dev — Worker의 화면과 API를 SQLite로 제공해요."""

import gzip
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from records import (
    JS_SPACE, json_bytes, normalize_record, now_kst, number_text, parse_json,
    parse_ts, public_record, sqlite_text, utf16_length,
)
from storage import Database, RECORD_COLUMNS, WORKER_DIR, db_path


COOKIE_NAME = "raw_session"
SESSION_TTL_S = 12 * 3600
MAX_LOGIN_FAILS = 5
LOGIN_FAIL_WINDOW_S = 60
LOGIN_BODY_MAX = 4096
MAX_RAW_FILES = 8
MAX_RAW_BYTES = 64 * 1024
MAX_RAW_UPLOAD_CHARS = 200 * 1024
MAX_RECORDS_DEFAULT = 5000
MAX_INGEST_RECORDS = 40
MAX_INGEST_BODY = 256 * 1024
RAW_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")
SESSION_RE = re.compile(r"([0-9]{1,12})\.([0-9a-f]{32})\.([0-9a-f]{64})")
BEARER_RE = re.compile("Bearer[" + JS_SPACE + "]+([^\r\n\u2028\u2029]+)", re.IGNORECASE | re.ASCII)
INSERT_RECORD = (
    f"INSERT OR IGNORE INTO records ({', '.join(RECORD_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in RECORD_COLUMNS)})"
)
TOO_LARGE = "요청이 너무 커요."
TOO_MANY_LOGINS = "시도가 너무 많아요. 잠시 후 다시 시도해 주세요."
INVALID_PASSWORD = "비밀번호가 올바르지 않아요."
SERVER_ERROR = "서버에서 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def load_html():
    path = WORKER_DIR / "src" / "html.js"
    try:
        source = path.read_text(encoding="utf-8")
        match = re.search(r"^export default (.+);\s*\Z", source, re.MULTILINE | re.DOTALL)
        html = json.loads(match[1]) if match else None
        if not isinstance(html, str) or not html:
            raise ValueError
        return html.encode("utf-8")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"INDEX_HTML을 읽을 수 없어요. Worker 파일을 확인해 주세요 ({path}).") from exc


class Auth:
    def __init__(self, env):
        for key in ("INGEST_KEY", "SESSION_SECRET"):
            if not env.get(key):
                raise ValueError(f"필수 환경 변수 {key}가 비어 있어요.")
        self.ingest_digest = hmac.digest(b"cmp", env["INGEST_KEY"].encode(), "sha256")
        self.session_secret = env["SESSION_SECRET"].encode()
        raw_hash = env.get("RAW_HASH", "")
        self.fingerprint = raw_hash[:16]
        iterations = re.match(r"^[+-]?[0-9]+", env.get("RAW_ITERATIONS", "").lstrip(JS_SPACE))
        try:
            self.iterations = int(iterations[0]) if iterations else 0
            self.salt = bytes.fromhex(env.get("RAW_SALT", ""))
            self.password_hash = bytes.fromhex(raw_hash)
        except ValueError:
            self.iterations = 0

    def accepts_bearer(self, header):
        match = BEARER_RE.fullmatch(header.strip(" \t"))
        if not match:
            return False
        given = match[1].strip(JS_SPACE).encode()
        return hmac.compare_digest(hmac.digest(b"cmp", given, "sha256"), self.ingest_digest)

    def accepts_password(self, password):
        if not password or self.iterations < 1 or not self.salt or not self.password_hash:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", sqlite_text(password).encode(), self.salt, self.iterations, 32)
        return hmac.compare_digest(digest, self.password_hash)

    def signature(self, exp, nonce):
        message = f"{exp}.{nonce}.{self.fingerprint}".encode()
        return hmac.digest(self.session_secret, message, "sha256").hex()

    def new_session(self):
        exp = int(time.time()) + SESSION_TTL_S
        nonce = secrets.token_hex(16)
        return f"{exp}.{nonce}.{self.signature(exp, nonce)}"


def session_cookie(value, age):
    # Worker는 URL이나 전달 헤더와 관계없이 항상 Secure를 붙여요.
    return f"{COOKIE_NAME}={value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={age}"


def session_from(headers):
    for part in ";".join(headers.get_all("Cookie", [])).split(";"):
        key, sep, value = part.partition("=")
        if sep and key.strip(JS_SPACE) == COOKIE_NAME:
            return SESSION_RE.fullmatch(value.strip(JS_SPACE))
    return None


def password_string(value):
    # JSON 로그인은 Worker의 String(p.password || "") 변환을 사용해요.
    if isinstance(value, list):
        return ",".join("" if x is None else password_string(x) for x in value)
    if isinstance(value, dict):
        if "toString" in value:
            raise ValueError
        return "[object Object]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return number_text(value)
    return str(value)


def login_password(text, content_type):
    if content_type.split(";")[0].strip().lower() == "application/x-www-form-urlencoded":
        return parse_qs(text.removeprefix("?"), keep_blank_values=True).get("password", [""])[0]
    try:
        data = parse_json(text or "{}")
        value = data.get("password") if isinstance(data, dict) else None
        if value is None or value is False or value == 0 or value == "":
            return ""
        return password_string(value)
    except (ValueError, RecursionError):
        return ""


def request_url(target):
    # URL.pathname은 빈 경로 조각을 보존하고 점 경로(%2e 포함)를 정리해요.
    url = urlsplit("http://localhost" + target if target.startswith("/") else target)
    path = (url.path or "/").replace("\\", "/")
    parts = path.split("/")
    normalized = []
    for index, part in enumerate(parts):
        dots = part.lower().replace("%2e", ".")
        if dots in (".", ".."):
            if dots == ".." and len(normalized) > 1:
                normalized.pop()
            if index == len(parts) - 1:
                normalized.append("")
        else:
            normalized.append(part)
    return "/".join(normalized), url.query


class RecordsCache:
    """URL별 20초 캐시. 작은 VM에 맞춰 오래된 응답부터 비워요."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.size = 0
        self.max_bytes = 8 * 1024 * 1024

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry:
                if time.monotonic() < entry[0]:
                    return entry[1]
                self.size -= len(self.entries.pop(key)[1])
            return None

    def put(self, key, body):
        with self.lock:
            if key in self.entries:
                self.size -= len(self.entries.pop(key)[1])
            # 큰 단일 응답도 20초 동안 보존하되, 다른 응답은 먼저 비워요.
            while self.entries and (self.size + len(body) > self.max_bytes or len(self.entries) >= 128):
                _, (_, old) = self.entries.popitem(last=False)
                self.size -= len(old)
            self.entries[key] = (time.monotonic() + 20, body)
            self.size += len(body)


class APIError(Exception):
    def __init__(self, status, message, *, headers=None, details=None):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message}
        if details is not None:
            self.payload["details"] = details
        self.headers = headers


def accepts_gzip(header):
    for coding in header.lower().split(","):
        name, *params = coding.strip().split(";")
        if name.strip() == "gzip":
            quality = 1.0
            for param in params:
                key, _, value = param.strip().partition("=")
                if key.strip() == "q":
                    try:
                        quality = float(value)
                    except ValueError:
                        quality = 0
            if 0 < quality <= 1:
                return True
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, fmt, *args):
        # 경로·쿼리·헤더·요청 본문에 들어온 비밀값을 로그에 남기지 않아요.
        pass

    def send_body(self, status, body, content_type, *, cors=False, headers=None):
        self.close_connection = True
        compress = accepts_gzip(",".join(self.headers.get_all("Accept-Encoding", [])))
        if compress:
            body = gzip.compress(body, compresslevel=4, mtime=0)
        self.send_response_only(status)
        self.send_header("Date", self.date_time_string())
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Connection", "close")
        if compress:
            self.send_header("Content-Encoding", "gzip")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, payload, **options):
        self.send_body(status, json_bytes(payload), "application/json; charset=utf-8", **options)

    def chunked_body(self, limit):
        data = bytearray()
        while True:
            line = self.rfile.readline(8192)
            size = line.split(b";", 1)[0].strip()
            if not line.endswith(b"\r\n") or not re.fullmatch(b"[0-9a-fA-F]+", size):
                raise APIError(400, "잘못된 요청 본문이에요.")
            length = int(size, 16)
            if length == 0:
                # 응답 후 연결을 닫으므로 트레일러는 읽을 필요가 없어요.
                break
            if len(data) + length > limit:
                return None
            chunk = self.rfile.read(length)
            if len(chunk) != length or self.rfile.read(2) != b"\r\n":
                raise APIError(400, "잘못된 요청 본문이에요.")
            data.extend(chunk)
        return data

    def read_text(self, limit, too_large=TOO_LARGE):
        transfer = self.headers.get("Transfer-Encoding", "").strip().lower()
        lengths = self.headers.get_all("Content-Length", [])
        if transfer:
            if transfer != "chunked" or lengths:
                raise APIError(400, "잘못된 요청 본문이에요.")
            # request.text()는 맨 앞의 UTF-8 BOM(3바이트)을 제거해요.
            data = self.chunked_body(limit + 3)
        else:
            size = lengths[0].strip() if lengths else "0"
            if len(lengths) > 1 or not re.fullmatch(r"[0-9]+", size):
                raise APIError(400, "잘못된 요청 본문이에요.")
            if float(size) > limit:
                raise APIError(413, too_large)
            length = int(size.lstrip("0") or "0")
            data = self.rfile.read(length)
            if len(data) != length:
                raise APIError(400, "잘못된 요청 본문이에요.")
        if data is None:
            raise APIError(413, too_large)
        text = data.decode("utf-8-sig", "replace")
        if len(text.encode("utf-8")) > limit:
            raise APIError(413, too_large)
        return text

    def require_bearer(self):
        header = ", ".join(self.headers.get_all("Authorization", []))
        if not self.server.auth.accepts_bearer(header):
            raise APIError(
                401, "인증이 필요해요. Authorization: Bearer <INGEST_KEY> 헤더를 보내 주세요.",
                headers={"WWW-Authenticate": 'Bearer realm="token-usage"'},
            )

    def require_session(self):
        session = session_from(self.headers)
        if session:
            exp, nonce, sig = int(session[1]), session[2], session[3]
            if exp > int(time.time()) and hmac.compare_digest(self.server.auth.signature(exp, nonce), sig):
                with self.server.db.transaction() as db:
                    if not db.execute("SELECT 1 FROM revoked_sessions WHERE nonce = ?", (nonce,)).fetchone():
                        return
        raise APIError(401, "unauthorized")

    def login(self):
        ip = (self.headers.get("CF-Connecting-IP", "").strip()
              or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or self.client_address[0])
        ip_key = hashlib.sha256(("ip:" + ip).encode()).hexdigest()[:24]

        def check_limit(db, since):
            count = db.execute(
                "SELECT COUNT(*) FROM login_fails WHERE ip = ? AND t > ?", (ip_key, since)
            ).fetchone()[0]
            if count >= MAX_LOGIN_FAILS:
                raise APIError(429, TOO_MANY_LOGINS)

        with self.server.db.transaction() as db:
            check_limit(db, int(time.time()) - LOGIN_FAIL_WINDOW_S)
        text = self.read_text(LOGIN_BODY_MAX)
        password = login_password(text, self.headers.get("Content-Type", ""))
        valid = self.server.auth.accepts_password(password)
        # 본문을 기다리는 동안 다른 요청도 처리하고, 쓰기 직전에 횟수를 다시 확인해요.
        with self.server.db.transaction(write=True) as db:
            now = int(time.time())
            since = now - LOGIN_FAIL_WINDOW_S
            check_limit(db, since)
            if valid:
                db.execute("DELETE FROM revoked_sessions WHERE exp < ?", (now,))
            else:
                db.execute("INSERT INTO login_fails (ip, t) VALUES (?, ?)", (ip_key, now))
                db.execute("DELETE FROM login_fails WHERE t < ?", (since - 3600,))
        if not valid:
            raise APIError(401, INVALID_PASSWORD)
        cookie = session_cookie(self.server.auth.new_session(), SESSION_TTL_S)
        self.send_json(200, {"ok": True}, headers={"Set-Cookie": cookie})

    def logout(self):
        session = session_from(self.headers)
        if session:
            with self.server.db.transaction(write=True) as db:
                db.execute(
                    "INSERT OR IGNORE INTO revoked_sessions (nonce, exp) VALUES (?, ?)",
                    (session[2], int(session[1])),
                )
        self.send_json(200, {"ok": True}, headers={"Set-Cookie": session_cookie("", 0)})

    def records(self, query):
        scheme = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip().lower()
        key = (scheme, self.headers.get("Host", "").lower(), query, "?" in self.path)
        body = self.server.cache.get(key)
        if body is None:
            body = self.query_records(parse_qs(query, keep_blank_values=True))
            self.server.cache.put(key, body)
        self.send_body(200, body, "application/json; charset=utf-8", cors=True)

    def query_records(self, query):
        service = query.get("service", [""])[0].strip(JS_SPACE)
        metric = query.get("metric", [""])[0].strip(JS_SPACE)
        q = query.get("q", [""])[0].strip(JS_SPACE).lower()
        raw_limit = query.get("limit", [""])[0].strip(JS_SPACE)
        limit = MAX_RECORDS_DEFAULT
        if re.fullmatch(r"[+-]?[0-9]+", raw_limit):
            limit = int(max(1, min(float(raw_limit), 20000)))
        conditions, args = [], []
        for condition, value in (("service = ?", service), ("metric = ?", metric), ("instr(search, ?) > 0", q)):
            if value:
                conditions.append(condition)
                args.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        # 최대 2만 건을 읽어도 파싱한 객체는 한 건씩만 메모리에 둬요.
        body = io.BytesIO()
        body.write(b'{"records":[')
        count = 0
        with self.server.db.transaction() as db:
            rows = db.execute(
                "SELECT data, ts_kst FROM records" + where + " ORDER BY ts_kst DESC, id ASC LIMIT ?",
                (*args, limit),
            )
            for row in rows:
                if count:
                    body.write(b",")
                body.write(json_bytes(public_record(row)))
                count += 1
            total = db.execute("SELECT COUNT(*) FROM records" + where, args).fetchone()[0]
        body.write(b"],")
        meta = dict(count=count, total=total, limit=limit, skipped=0, generated_at=now_kst(), timezone="Asia/Seoul")
        body.write(json_bytes(meta)[1:])
        return body.getvalue()

    def ingest(self):
        self.require_bearer()
        text = self.read_text(MAX_INGEST_BODY, "요청이 너무 커요. (최대 256KB)")
        try:
            payload = parse_json(text)
        except (ValueError, RecursionError):
            raise APIError(400, "JSON 형식이 올바르지 않아요. 기록 객체 하나 또는 객체 배열을 보내 주세요.")
        if isinstance(payload, dict) and isinstance(payload.get("records"), list):
            payload = payload["records"]
        items = payload if isinstance(payload, list) else [payload]
        if not items:
            raise APIError(400, "보낼 기록이 없어요.")
        if len(items) > MAX_INGEST_RECORDS:
            raise APIError(400, f"한 번에 최대 {MAX_INGEST_RECORDS}건까지 보낼 수 있어요. 나눠서 보내 주세요.")
        rows, problems = [], []
        for index, item in enumerate(items):
            try:
                rows.append(normalize_record(item))
            except ValueError as exc:
                problems.append({"index": index, "error": str(exc)})
        if problems:
            raise APIError(400, f"잘못된 기록이 {len(problems)}건 있어서 아무것도 저장하지 않았어요.", details=problems[:20])
        at = now_kst()
        with self.server.db.transaction(write=True) as db:
            inserted = db.executemany(INSERT_RECORD, (row + (at,) for row in rows)).rowcount
        self.send_json(200, dict(ok=True, received=len(rows), inserted=inserted, duplicates=len(rows) - inserted))

    def raw_list(self):
        self.require_session()
        with self.server.db.transaction() as db:
            files = [dict(row) for row in db.execute(
                "SELECT name, size, created_at AS mtime FROM raw_files ORDER BY created_at DESC, name DESC LIMIT ?",
                (MAX_RAW_FILES,),
            )]
        self.send_json(200, dict(files=files, generated_at=now_kst()))

    def raw_file(self, query):
        self.require_session()
        name = query.get("name", [""])[0]
        if not RAW_NAME_RE.fullmatch(name):
            raise APIError(404, "not found")
        with self.server.db.transaction() as db:
            row = db.execute("SELECT content, size, created_at FROM raw_files WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise APIError(404, "not found")
        data = row["content"].encode()
        self.send_json(200, dict(
            name=name, size=row["size"], mtime=row["created_at"],
            truncated=len(data) > MAX_RAW_BYTES, text=data[:MAX_RAW_BYTES].decode("utf-8-sig", "replace"),
        ))

    def upload(self):
        self.require_bearer()
        text = self.read_text(MAX_RAW_UPLOAD_CHARS * 3 + 4096)
        try:
            payload = parse_json(text)
        except (ValueError, RecursionError):
            raise APIError(400, 'JSON 형식이 올바르지 않아요. {"name": "...", "content": "..."} 로 보내 주세요.')
        if not isinstance(payload, dict):
            raise APIError(400, "요청 본문은 JSON 객체여야 해요.")
        name, content = payload.get("name"), payload.get("content")
        if not isinstance(name, str) or not RAW_NAME_RE.fullmatch(name):
            raise APIError(400, "`name`은 영문·숫자·점·밑줄·하이픈으로만 이루어진 200자 이하 파일 이름이어야 해요.")
        if not isinstance(content, str):
            raise APIError(400, "`content`는 문자열이어야 해요.")
        if utf16_length(content) > MAX_RAW_UPLOAD_CHARS:
            raise APIError(400, f"`content`는 {MAX_RAW_UPLOAD_CHARS}자 이하여야 해요.")
        created = now_kst() if payload.get("created_at") is None else parse_ts(payload["created_at"])
        if created is None:
            raise APIError(400, "`created_at` 형식이 올바르지 않아요. ISO 8601로 적어 주세요.")
        content = sqlite_text(content)
        size = len(content.encode())
        with self.server.db.transaction(write=True) as db:
            prev = db.execute("SELECT 1 FROM raw_files WHERE name = ?", (name,)).fetchone()
            db.execute(
                "INSERT INTO raw_files (name, content, size, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET content=excluded.content, size=excluded.size, "
                "created_at=excluded.created_at",
                (name, content, size, created),
            )
        self.send_json(200, dict(ok=True, name=name, size=size, replaced=prev is not None))

    def route(self):
        # http.server가 축약하기 전의 //도 Worker와 같은 경로로 해석해요.
        path, query = request_url(self.requestline.split()[1])
        if self.command in ("GET", "HEAD"):
            if path in ("/", "/index.html"):
                return self.send_body(200, self.server.html, "text/html; charset=utf-8", cors=True)
            if path in ("/health", "/api/health"):
                return self.send_json(200, dict(ok=True, time=now_kst()), cors=True)
            if path == "/api/records":
                return self.records(query)
            if path == "/api/raw":
                return self.raw_list()
            if path == "/api/raw/file":
                return self.raw_file(parse_qs(query, keep_blank_values=True))
        elif self.command == "POST":
            if path == "/api/raw/login":
                return self.login()
            if path == "/api/raw/logout":
                return self.logout()
            if path == "/api/ingest":
                return self.ingest()
            if path == "/api/raw/upload":
                return self.upload()
        if path in ("/api/ingest", "/api/raw/upload"):
            raise APIError(405, "POST로 보내 주세요.", headers={"Allow": "POST"})
        self.send_body(404, b"Not found\n", "text/plain; charset=utf-8", cors=True)

    def dispatch(self):
        try:
            self.route()
        except APIError as exc:
            self.send_json(exc.status, exc.payload, headers=exc.headers)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            print(f"요청 처리 실패 ({type(exc).__name__})", file=sys.stderr, flush=True)
            self.send_json(500, {"error": SERVER_ERROR})

    def __getattr__(self, name):
        # http.server의 501 대신 OPTIONS 등 모든 메서드를 Worker 라우터로 보내요.
        if name.startswith("do_"):
            return self.dispatch
        raise AttributeError(name)


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, address, db, auth, html):
        self.db = db
        self.auth = auth
        self.html = html
        self.cache = RecordsCache()
        self.next_cleanup = 0
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        # 클라이언트가 응답 도중 연결을 닫아도 요청 내용을 출력하지 않아요.
        error = sys.exc_info()[1]
        if not isinstance(error, (ConnectionError, TimeoutError)):
            print(f"HTTP 연결 처리 실패 ({type(error).__name__})", file=sys.stderr, flush=True)

    def service_actions(self):
        if time.monotonic() >= self.next_cleanup:
            self.next_cleanup = time.monotonic() + 3600
            now = int(time.time())
            try:
                with self.db.transaction(write=True) as db:
                    db.execute("DELETE FROM login_fails WHERE t < ?", (now - LOGIN_FAIL_WINDOW_S - 3600,))
                    db.execute("DELETE FROM revoked_sessions WHERE exp < ?", (now,))
            except sqlite3.Error as exc:
                print(f"만료 항목 정리 실패 ({type(exc).__name__})", file=sys.stderr, flush=True)


def make_server():
    auth = Auth(os.environ)
    html = load_html()
    try:
        port = int(os.environ.get("PORT", "8787"))
        if not 0 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("PORT는 0~65535 정수여야 해요.") from None
    return DashboardServer((os.environ.get("HOST", "127.0.0.1"), port), Database(db_path()), auth, html)


def main():
    try:
        with make_server() as server:
            print(f"usage-dashboard-google-dev 시작 (port {server.server_port})", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        sys.exit(f"usage-dashboard-google-dev 시작 실패 — {exc}")


if __name__ == "__main__":
    main()
