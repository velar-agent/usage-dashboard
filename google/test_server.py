"""로컬 HTTP 서버와 임시 SQLite 파일로 Worker 계약을 검사해요."""

import concurrent.futures
import gzip
import hashlib
import hmac
import http.client
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import server
from storage import Database, GOOGLE_DIR, WORKER_DIR


PASSWORD = "테스트 password&+"
SALT = bytes(range(16))
RAW_HASH = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), SALT, 10000).hex()
ENV = dict(INGEST_KEY="test-ingest", SESSION_SECRET="test-session", RAW_SALT=SALT.hex(),
           RAW_HASH=RAW_HASH, RAW_ITERATIONS="10000", HOST="127.0.0.1", PORT="0")
BEARER = {"Authorization": "Bearer " + ENV["INGEST_KEY"]}
RECORD = dict(service="grok", metric="weekly_used_pct", value=14, ts="2026-11-15T06:20:00.789Z")


def cli(script, *args, env=None):
    return subprocess.run([sys.executable, GOOGLE_DIR / script, *args], env=env,
                          capture_output=True, text=True, timeout=10)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.dict(os.environ, {**ENV, "DB_PATH": str(Path(temp.name) / "db.sqlite3")}, clear=True):
            self.app = server.make_server()
        thread = threading.Thread(target=self.app.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.app.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.app.shutdown)

    def request(self, method, path, body=None, headers=None, *, chunked=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=5)
        try:
            conn.request(method, path, body, headers or {}, encode_chunked=chunked)
            response = conn.getresponse()
            data = response.read()
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            return response.status, response.headers, data
        finally:
            conn.close()

    def api(self, method, path, status, *, body=None, headers=None, chunked=False):
        actual, result_headers, data = self.request(method, path, body, headers, chunked=chunked)
        self.assertEqual(actual, status)
        self.assertEqual(result_headers["Content-Type"], "application/json; charset=utf-8")
        if path.startswith(("/api/ingest", "/api/raw")):
            self.assertNotIn("Access-Control-Allow-Origin", result_headers)
        return json.loads(data), result_headers

    def post(self, path, payload, status=200, headers=None):
        return self.api("POST", path, status, body=json.dumps(payload, ensure_ascii=False).encode(),
                        headers={"Content-Type": "application/json", **(headers or {})})

    def ingest(self, payload, status=200):
        return self.post("/api/ingest", payload, status, BEARER)[0]

    def login(self, **headers):
        data, response = self.post("/api/raw/login", {"password": PASSWORD}, headers=headers)
        self.assertEqual(data, {"ok": True})
        return response["Set-Cookie"].split(";", 1)[0]

    def assert_kst(self, text):
        self.assertRegex(text, r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+09:00$")
        self.assertEqual(datetime.fromisoformat(text).utcoffset(), timedelta(hours=9))

    def test_health_and_exact_worker_html(self):
        for path in ("/health", "/api/health", "/api/%2e%2e/health"):
            data, headers = self.api("GET", path, 200)
            self.assertEqual(set(data), {"ok", "time"})
            self.assertIs(data["ok"], True)
            self.assert_kst(data["time"])
            self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        source = (WORKER_DIR / "src/html.js").read_text(encoding="utf-8")
        expected = json.loads(source.split("export default ", 1)[1].strip().removesuffix(";")).encode()
        for path in ("/", "/index.html"):
            status, headers, html = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
            self.assertEqual(html, expected)

    def test_head_options_and_unsupported_methods(self):
        status, headers, data = self.request("HEAD", "/")
        self.assertEqual((status, data), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(self.app.html))
        for method, path in (("GET", "/missing"), ("POST", "/api/records"), ("OPTIONS", "/health"),
                             ("OPTIONS", "/api/raw/login"), ("PUT", "/api/raw"), ("CUSTOM", "/"), ("GET", "//health")):
            with self.subTest(method=method, path=path):
                status, headers, data = self.request(method, path)
                self.assertEqual((status, data), (404, b"Not found\n"))
                self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        for path in ("/api/ingest", "/api/raw/upload"):
            for method in ("GET", "OPTIONS", "PATCH"):
                data, headers = self.api(method, path, 405)
                self.assertEqual((headers["Allow"], data), ("POST", {"error": "POST로 보내 주세요."}))
            status, headers, data = self.request("HEAD", path)
            self.assertEqual((status, headers["Allow"], data), (405, "POST", b""))

    def test_gzip_large_small_and_opt_out(self):
        for path in ("/", "/health"):
            status, headers, data = self.request("GET", path, headers={"Accept-Encoding": "br, GZip;q=0.7"})
            self.assertEqual((status, headers["Content-Encoding"], headers["Vary"]), (200, "gzip", "Accept-Encoding"))
            self.assertEqual(int(headers["Content-Length"]), len(data))
            plain = gzip.decompress(data)
            if path == "/":
                self.assertEqual(plain, self.app.html)
            else:
                self.assertTrue(json.loads(plain)["ok"])
        self.assertNotIn("Content-Encoding", self.request("GET", "/", headers={"Accept-Encoding": "gzip;q=0"})[1])

    def test_ingest_auth_empty_records_cache_and_dedupe(self):
        empty, headers = self.api("GET", "/api/records", 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assert_kst(empty.pop("generated_at"))
        self.assertEqual(empty, dict(records=[], count=0, total=0, limit=5000, skipped=0, timezone="Asia/Seoul"))
        for auth in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic test-ingest"}):
            data, headers = self.post("/api/ingest", RECORD, 401, auth)
            self.assertEqual(headers["WWW-Authenticate"], 'Bearer realm="token-usage"')
            self.assertEqual(data, {"error": "인증이 필요해요. Authorization: Bearer <INGEST_KEY> 헤더를 보내 주세요."})
        accepted = self.post("/api/ingest", RECORD, headers={"Authorization": "bEaReR  test-ingest"})[0]
        self.assertEqual(accepted, dict(ok=True, received=1, inserted=1, duplicates=0))
        repeat = {**RECORD, "value": 99, "ts": "2026-11-15 15:20:00"}
        self.assertEqual(self.ingest({"records": [repeat, RECORD]}), dict(ok=True, received=2, inserted=0, duplicates=2))
        self.assertEqual(self.api("GET", "/api/records?limit=5000", 200)[0]["records"], [RECORD])
        self.assertEqual(self.api("GET", "/api/records", 200)[0]["count"], 0)
        with patch("server.time.monotonic", return_value=time.monotonic() + 21):
            self.assertEqual(self.api("GET", "/api/records", 200)[0]["records"], [RECORD])

    def test_records_storage_query_params_and_order(self):
        a = {**RECORD, "service": "  GroK ", "unit": " % ", "note": " 한도 ", "source": " Bot ",
             "reset_at": " 2026-11-17 ", "value": 0.000001, "raw_file": "a", "extra": [True, 12]}
        b = {**RECORD, "service": "GroK", "note": "B"}
        c = {**RECORD, "ts": "2026-11-16 24:00", "metric": "quota"}
        self.assertEqual(self.ingest([a, b, c])["inserted"], 3)
        expected = {**a, "service": "GroK", "unit": "%", "note": "한도", "source": "Bot", "reset_at": "2026-11-17"}
        records = self.api("GET", "/api/records?since=2099-01-01", 200)[0]["records"]
        self.assertEqual(records, [c, expected, b])
        with self.app.db.transaction() as db:
            row = db.execute("SELECT * FROM records ORDER BY id LIMIT 1").fetchone()
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertGreater(db.execute("PRAGMA busy_timeout").fetchone()[0], 0)
            self.assertEqual(row["search"], "grok weekly_used_pct 한도 bot % 0.000001")
            self.assertEqual(row["dedupe_key"], "2026-11-15T15:20:00+09:00|GroK|weekly_used_pct|한도")
            self.assertEqual(json.loads(row["data"]), expected)
            self.assertIn('"value":0.000001', row["data"])
            self.assert_kst(row["ingested_at"])
        for query, total in (("service=GroK&metric=weekly_used_pct", 2), ("service=GroK&q=BOT", 1),
                             ("service=&service=grok", 3), ("q=0.000001", 1), ("service=GROK", 0),
                             (urlencode({"q": "' OR 1=1 --"}), 0)):
            self.assertEqual(self.api("GET", "/api/records?" + query, 200)[0]["total"], total)
        for raw, limit in (("-2", 1), ("0", 1), ("%2B02", 2), ("1.1", 5000), ("1e1", 5000),
                           ("", 5000), ("9999999999999999999999999999999", 20000)):
            data = self.api("GET", "/api/records?limit=" + raw, 200)[0]
            self.assertEqual((data["count"], data["total"], data["limit"]), (min(limit, 3), 3, limit))

    def test_ingest_validation_is_atomic_and_details_capped(self):
        bad = self.api("POST", "/api/ingest", 400, body=b"{", headers=BEARER)[0]
        self.assertEqual(bad, {"error": "JSON 형식이 올바르지 않아요. 기록 객체 하나 또는 객체 배열을 보내 주세요."})
        self.assertEqual(self.ingest([], 400), {"error": "보낼 기록이 없어요."})
        self.assertEqual(self.ingest([RECORD] * 41, 400), {"error": "한 번에 최대 40건까지 보낼 수 있어요. 나눠서 보내 주세요."})
        invalid = [None, {"_bad": 0}, {"service": 1}, {"service": " "}, {**RECORD, "value": True},
                   {**RECORD, "ts": False}, {**RECORD, "ts": "2026-13-01"}, {**RECORD, "reset_at": "bad"},
                   {**RECORD, "metric": "😀" * 33}, {**RECORD, "extra": "가" * 1400}]
        errors = self.ingest([RECORD] + invalid * 3, 400)
        self.assertEqual(errors["error"], "잘못된 기록이 30건 있어서 아무것도 저장하지 않았어요.")
        self.assertEqual([x["index"] for x in errors["details"]], list(range(1, 21)))
        self.assertEqual(errors["details"][4]["error"], "`value`는 숫자여야 해요. (예: 14 또는 7.5)")
        self.assertEqual(errors["details"][8]["error"], "`metric`는 64자 이하여야 해요.")
        self.assertEqual(errors["details"][9]["error"], "기록 하나는 4096바이트 이하여야 해요.")
        self.assertEqual(self.api("GET", "/api/records", 200)[0]["count"], 0)
        accepted = self.ingest([RECORD] * 40)
        self.assertEqual((accepted["inserted"], accepted["duplicates"]), (1, 39))

    def test_ingest_body_limits_and_chunked_transport(self):
        large = "요청이 너무 커요. (최대 256KB)"
        data, _ = self.api("POST", "/api/ingest", 413, headers={**BEARER, "Content-Length": str(256 * 1024 + 1)})
        self.assertEqual(data, {"error": large})
        data, _ = self.api("POST", "/api/ingest", 413, body=[b"x" * (256 * 1024 + 4)], headers=BEARER, chunked=True)
        self.assertEqual(data, {"error": large})
        data, _ = self.api("POST", "/api/ingest", 413, body=b"\xff" * 90000, headers=BEARER)
        self.assertEqual(data, {"error": large})
        data, _ = self.api("POST", "/api/ingest", 200, body=[b"\xef\xbb\xbf", json.dumps(RECORD).encode()], headers=BEARER, chunked=True)
        self.assertEqual(data["inserted"], 1)

    def test_deep_extra_field_within_record_limit(self):
        nested = b"[" * 1750 + b'{"n":[0,true]}' + b"]" * 1750
        record = b'{"ts":"2026-11-15","service":"s","metric":"m","value":1,"extra":' + nested + b"}"
        self.api("POST", "/api/ingest", 200, body=record, headers=BEARER)
        status, _, body = self.request("GET", "/api/records")
        self.assertEqual(status, 200)
        self.assertIn(record, body)

    def test_internal_error_body_and_log_redaction(self):
        logs = StringIO()
        with redirect_stderr(logs), patch.object(server.Handler, "query_records", side_effect=RuntimeError("test-private")):
            data, headers = self.api("GET", "/api/records?secret=test-private", 500)
        self.assertEqual(data, {"error": "서버에서 문제가 생겼어요. 잠시 후 다시 시도해 주세요."})
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("test-private", logs.getvalue())

    def test_normalized_time_and_record_byte_boundary(self):
        cases = (("2024-02-31", "2024-03-02T00:00:00+09:00"),
                 ("2026-11-16t24:00:00.0009z", "2026-11-17T09:00:00+09:00"),
                 ("2026-11-15 15:20-0430", "2026-11-16T04:50:00+09:00"))
        for value, expected in cases:
            self.ingest({**RECORD, "ts": value})
            with self.app.db.transaction() as db:
                self.assertEqual(db.execute("SELECT ts_kst FROM records ORDER BY id DESC LIMIT 1").fetchone()[0], expected)
        self.ingest({"service": "clock", "metric": "m", "value": 0})
        self.assert_kst(self.api("GET", "/api/records?service=clock", 200)[0]["records"][0]["ts"])
        rec = {**RECORD, "extra": ""}
        size = len(json.dumps(rec, separators=(",", ":")).encode())
        rec["extra"] = "a" * (4096 - size)
        self.assertEqual(self.ingest(rec)["inserted"], 1)
        self.assertEqual(self.ingest({**rec, "extra": rec["extra"] + "a"}, 400)["details"][0]["error"],
                         "기록 하나는 4096바이트 이하여야 해요.")

    def test_session_cookie_signature_expiry_logout_and_gates(self):
        paths = ("/api/raw", "/api/raw/file?name=bad%2Fname")
        for path in paths:
            self.assertEqual(self.api("GET", path, 401, headers=BEARER)[0], {"error": "unauthorized"})
        self.assertEqual(self.post("/api/raw/login", {"password": "wrong"}, 401)[0], {"error": "비밀번호가 올바르지 않아요."})
        form = urlencode({"password": PASSWORD}).encode()
        start = int(time.time())
        _, headers = self.api("POST", "/api/raw/login", 200, body=form, headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        cookie, flags = headers["Set-Cookie"].split(";", 1)
        self.assertEqual(flags, " Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=43200")
        self.assertRegex(cookie, r"^raw_session=[0-9]+\.[0-9a-f]{32}\.[0-9a-f]{64}$")
        exp, nonce, sig = cookie.split("=")[1].split(".")
        self.assertTrue(start + 43200 <= int(exp) <= int(time.time()) + 43200)
        def signature(expiry, fingerprint=RAW_HASH[:16]):
            return hmac.digest(ENV["SESSION_SECRET"].encode(), f"{expiry}.{nonce}.{fingerprint}".encode(), "sha256").hex()
        self.assertTrue(hmac.compare_digest(sig, signature(exp)))
        self.assertEqual(self.api("GET", paths[0], 200, headers={"Cookie": "other=1; " + cookie})[0]["files"], [])
        self.assertEqual(self.api("GET", paths[1], 404, headers={"Cookie": cookie})[0], {"error": "not found"})
        expired = int(time.time()) - 1
        for bad in (f"raw_session={expired}.{nonce}.{signature(expired)}", f"raw_session={exp}.{nonce}.{signature(exp, 'old')}"):
            self.api("GET", "/api/raw", 401, headers={"Cookie": bad})
        other = self.login(**{"X-Forwarded-Proto": "https"})
        _, headers = self.api("POST", "/api/raw/logout", 200, headers={"Cookie": cookie})
        self.assertEqual(headers["Set-Cookie"], "raw_session=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0")
        for path in paths:
            self.api("GET", path, 401, headers={"Cookie": cookie})
        self.api("GET", "/api/raw", 200, headers={"Cookie": other})
        with self.app.db.transaction() as db:
            self.assertEqual(db.execute("SELECT exp FROM revoked_sessions WHERE nonce = ?", (nonce,)).fetchone()[0], int(exp))

    def test_upload_list_preview_and_replacement(self):
        cookie = {"Cookie": self.login()}
        upload = "/api/raw/upload"
        item = {"name": "test.jsonl", "content": "a" * 65535 + "한", "created_at": "2026-11-15T12:00Z"}
        for auth in ({}, cookie, {"Authorization": "Bearer wrong"}):
            self.post(upload, item, 401, auth)
        self.assertEqual(self.post(upload, item, 200, BEARER)[0], dict(ok=True, name="test.jsonl", size=65538, replaced=False))
        mtime = "2026-11-15T21:00:00+09:00"
        preview = self.api("GET", "/api/raw/file?name=test.jsonl", 200, headers=cookie)[0]
        self.assertEqual(preview, dict(name="test.jsonl", size=65538, mtime=mtime, truncated=True, text="a" * 65535 + "�"))
        self.assertTrue(self.post(upload, {**item, "content": "\ufeff끝"}, 200, BEARER)[0]["replaced"])
        self.assertEqual(self.api("GET", "/api/raw/file?name=test.jsonl", 200, headers=cookie)[0]["text"], "끝")
        for i in range(9):
            self.post(upload, {**item, "name": f"z{i}", "content": ""}, 200, BEARER)
        result = self.api("GET", "/api/raw", 200, headers=cookie)[0]
        self.assert_kst(result["generated_at"])
        self.assertEqual(result["files"], [dict(name=f"z{i}", size=0, mtime=mtime) for i in range(8, 0, -1)])
        self.api("GET", "/api/raw/file?name=z0", 200, headers=cookie)
        self.assertEqual(self.api("GET", "/api/raw/file?name=absent", 404, headers=cookie)[0], {"error": "not found"})

    def test_upload_validation_and_size_limits(self):
        upload = "/api/raw/upload"

        def post(item, status):
            return self.post(upload, item, status, BEARER)[0]
        self.assertEqual(self.api("POST", upload, 400, body=b"}", headers=BEARER)[0],
                         {"error": 'JSON 형식이 올바르지 않아요. {"name": "...", "content": "..."} 로 보내 주세요.'})
        for item in ([], {"name": "../a"}, {"name": "a" * 202}, {"name": "a", "content": 0},
                     {"name": "a", "content": "", "created_at": ""}):
            self.assertEqual(set(post(item, 400)), {"error"})
        item = {"name": "a" * 201, "content": "😀" * 102400}
        self.assertEqual(post(item, 200)["size"], 409600)
        self.assertEqual(post({**item, "content": item["content"] + "a"}, 400), {"error": "`content`는 204800자 이하여야 해요."})
        self.assertEqual(self.api("POST", upload, 413, headers={**BEARER, "Content-Length": str(204800 * 3 + 4097)})[0],
                         {"error": "요청이 너무 커요."})

    def test_login_throttle_ip_order_window_and_cleanup(self):
        path = "/api/raw/login"
        ip = {"CF-Connecting-IP": "192.0.2.5", "X-Forwarded-For": "192.0.2.1, 127.0.0.1"}
        self.api("POST", path, 413, body=b"x" * 4097, headers=ip)
        for _ in range(5):
            self.api("POST", path, 401, body=b"{", headers=ip)
        denied, _ = self.api("POST", path, 429, body=b"x" * 4097, headers=ip)
        self.assertEqual(denied, {"error": "시도가 너무 많아요. 잠시 후 다시 시도해 주세요."})
        self.post(path, {"password": PASSWORD}, 429, {"X-Forwarded-For": "192.0.2.5, 192.0.2.1"})
        self.login(**{"X-Forwarded-For": ip["X-Forwarded-For"]})
        self.login()
        with self.app.db.transaction(write=True) as db:
            key = hashlib.sha256(b"ip:192.0.2.5").hexdigest()[:24]
            self.assertEqual(db.execute("SELECT COUNT(*) FROM login_fails WHERE ip = ?", (key,)).fetchone()[0], 5)
            old = int(time.time()) - 4000
            db.execute("UPDATE login_fails SET t = ?", (int(time.time()) - 60,))
            db.execute("INSERT INTO login_fails VALUES (?,?)", ("old", old))
            db.execute("INSERT INTO revoked_sessions VALUES (?,?)", ("old", old))
        self.login(**ip)
        self.post(path, {"password": "wrong"}, 401)
        with self.app.db.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM revoked_sessions WHERE nonce='old'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM login_fails WHERE ip='old'").fetchone())

    def test_concurrent_duplicates_and_failed_logins(self):
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            inserts = list(pool.map(lambda _: self.ingest(RECORD)["inserted"], range(6)))
            failures = list(pool.map(lambda _: self.request("POST", "/api/raw/login", b"{}")[0], range(6)))
        self.assertEqual(sum(inserts), 1)
        self.assertCountEqual(failures, [401] * 5 + [429])


class ToolTests(unittest.TestCase):
    def test_startup_requires_both_secrets_and_html(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "absent.sqlite3"
            for missing in ("INGEST_KEY", "SESSION_SECRET"):
                env = {**os.environ, **ENV, "DB_PATH": str(path), missing: ""}
                result = cli("server.py", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(missing, result.stderr)
                self.assertFalse(path.exists())
            with patch.object(server, "WORKER_DIR", Path(temp)):
                with self.assertRaisesRegex(RuntimeError, "INDEX_HTML"):
                    server.load_html()

    def test_import_preserves_all_columns_and_skips_only_matching_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            dump, dbfile = Path(temp) / "export.json", Path(temp) / "db.sqlite3"
            row = dict(id=71, ts_kst="2026-11-15T15:20:00+09:00", service="legacy", metric="quota", value=3.5,
                       unit=None, reset_at=None, note="", source=None, data='{"_line":7,"value":3.5}',
                       search="legacy quota    3.5", dedupe_key="old|key", ingested_at="2026-11-16T17:00:00+09:00")
            content = '내용 "\\{}[]\n' * 14000
            raw = dict(name="saved", content=content, size=len(content.encode()), created_at=row["ingested_at"])
            def write(rows, files):
                dump.write_text(json.dumps(dict(raw_files=files, records=rows)), encoding="utf-8")
            write([row], [raw])
            for n in (1, 0):
                result = cli("import_d1_json.py", dump, "--db", dbfile)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [f"{t} inserted={n} ignored={1-n}" for t in ("records", "raw_files")])
            write([{**row, "id": 72}], [{**raw, "size": 900}])
            self.assertEqual(cli("import_d1_json.py", dump, "--db", dbfile).returncode, 0)
            write([{**row, "id": 72, "dedupe_key": "new"}, {**row, "dedupe_key": "id-collision"}],
                  [{**raw, "name": "rolled-back"}])
            self.assertNotEqual(cli("import_d1_json.py", dump, "--db", dbfile).returncode, 0)
            with Database(dbfile).transaction() as db:
                self.assertEqual([dict(r) for r in db.execute("SELECT * FROM records")], [row])
                self.assertEqual([dict(r) for r in db.execute("SELECT * FROM raw_files")], [raw])
                self.assertEqual(server.public_record(row), {"value": 3.5, "ts": row["ts_kst"]})

    def test_generate_permissions_refusal_rotation_and_no_secrets_in_output(self):
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / "private" / "env"
            password = env.with_name("raw-password.txt")
            def read_env():
                return dict(line.split("=", 1) for line in env.read_text().splitlines())
            first = cli("gen_env.py", env)
            self.assertEqual(first.returncode, 0, first.stderr)
            values = read_env()
            original = password.read_text().strip()
            self.assertEqual(stat.S_IMODE(env.parent.stat().st_mode), 0o700)
            for path in (env, password):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual((values["HOST"], values["PORT"], values["RAW_ITERATIONS"]), ("127.0.0.1", "8787", "10000"))
            self.assertTrue(server.Auth(values).accepts_password(original))
            refused = cli("gen_env.py", env)
            self.assertNotEqual(refused.returncode, 0)
            self.assertTrue(values == read_env() and password.read_text().strip() == original)
            forced = cli("gen_env.py", "--force", env)
            self.assertEqual(forced.returncode, 0, forced.stderr)
            new = read_env()
            fresh = password.read_text().strip()
            self.assertTrue(server.Auth(new).accepts_password(fresh))
            self.assertFalse(server.Auth(new).accepts_password(original))
            for key in ("INGEST_KEY", "SESSION_SECRET", "RAW_SALT", "RAW_HASH"):
                self.assertFalse(values[key] == new[key])
            logs = "".join(run.stdout + run.stderr for run in (first, refused, forced))
            for settings, secret in ((values, original), (new, fresh)):
                self.assertFalse(any(settings[k] in logs for k in ("INGEST_KEY", "SESSION_SECRET", "RAW_SALT", "RAW_HASH")))
                self.assertFalse(secret in logs or secret in env.read_text())
            env.unlink()
            self.assertNotEqual(cli("gen_env.py", env).returncode, 0)
            self.assertFalse(env.exists())


if __name__ == "__main__":
    unittest.main()
