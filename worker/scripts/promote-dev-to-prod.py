#!/usr/bin/env python3
"""usage-dashboard-dev 에서 검증한 로컬 코드를 운영 Worker(usage-dashboard)에 올리기 위한 준비/검증 스크립트.

precheck/build/postcheck/diff 는 운영에 배포하지 않습니다(build 는 MCP execute 용 파일만 만들어요).
`deploy` 는 Cloudflare REST API 로 운영 Worker(usage-dashboard) 코드를 직접 올립니다(밤마다 cron 이 씀).
시크릿·키 파일 내용은 읽거나 출력하거나 복사하거나 덮어쓰지 않습니다. D1 데이터도 옮기지 않습니다.
`build`(MCP execute 용 PUT)는 keep_bindings 로 시크릿을 유지하고, `deploy` 는 Versions API 업로드에서 시크릿을 값 없이 `inherit` 로 이어받습니다.

사용법:
  python3 worker/scripts/promote-dev-to-prod.py precheck
  python3 worker/scripts/promote-dev-to-prod.py build              # /tmp/deploy.js (대상: 운영 usage-dashboard)
  python3 worker/scripts/promote-dev-to-prod.py build -o /tmp/x.js
  # 그다음: user-Cloudflare execute 에 code="$file:/tmp/deploy.js"
  python3 worker/scripts/promote-dev-to-prod.py postcheck
  python3 worker/scripts/promote-dev-to-prod.py diff
  python3 worker/scripts/promote-dev-to-prod.py deploy --dry-run   # REST API 직접 배포. 업로드·롤백만 빼고 전부 수행
  python3 worker/scripts/promote-dev-to-prod.py deploy             # 실제 운영 배포 (API 토큰 필요, 아래 참고)
  python3 worker/scripts/promote-dev-to-prod.py deploy --force     # 변경이 없어도 다시 올림
  python3 worker/scripts/promote-dev-to-prod.py status             # 운영 최근 배포 목록(읽기 전용)

deploy 의 API 토큰: 환경변수 CLOUDFLARE_API_TOKEN, 없으면 --token-file (기본 ~/.config/usage-deploy/cf-token).
토큰 값은 어디에도 출력하지 않습니다.

deploy 종료 코드: 0 성공 또는 변경 없음(건너뜀) / 1 점검·일반 실패 / 2 사용법·토큰 없음 /
  3 개발(live)이 로컬 코드와 달라 운영 반영 거부 / 4 업로드·배포 생성 실패(운영은 그대로) /
  5 배포 후 점검 실패 → 이전 버전으로 롤백했고 롤백 후 점검 통과 / 6 롤백 실패 또는 롤백 후에도 점검 실패(수동 확인 필요) /
  7 업로드 전 안전 점검 거부: 최신 업로드 버전이 활성 배포와 다르거나 필수 바인딩(DB·시크릿 이름)이 없음(수동 확인 필요, 아무것도 올리지 않음)
"""
from __future__ import annotations

import argparse
import hashlib
import email
from http.client import HTTPException
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

SCRIPTS = Path(__file__).resolve().parent
WORKER = SCRIPTS.parent
REPO = WORKER.parent
WEB = REPO / "web"
MAKE_DEPLOY = SCRIPTS / "make-deploy-code.py"
HTML_JS = WORKER / "src" / "html.js"
INDEX_JS = WORKER / "src" / "index.js"
TOML = WORKER / "wrangler.toml"
DEFAULT_OUT = Path("/tmp/deploy.js")

PROD_NAME = "usage-dashboard"
DEV_NAME = "usage-dashboard-dev"
PROD_URL = "https://usage-dashboard.velar-agent.workers.dev"
DEV_URL = "https://usage-dashboard-dev.velar-agent.workers.dev"
HTTP_TIMEOUT = 20
REQUIRED_BINDINGS = ("DB", "INGEST_KEY", "SESSION_SECRET", "RAW_SALT", "RAW_HASH", "RAW_ITERATIONS")
ACCOUNT_ID = "a12d19c670e84a71f5acc26df16e47f7"
CF_API = "https://api.cloudflare.com/client/v4"
DEFAULT_TOKEN_FILE = Path("~/.config/usage-deploy/cf-token")
KEPT_SECRET_NAMES = ("INGEST_KEY", "SESSION_SECRET", "RAW_SALT", "RAW_HASH", "RAW_ITERATIONS")
MODULE_NAMES = ("index.js", "html.js")
RECORDS_KEYS = ("records", "count", "total", "limit", "skipped", "generated_at", "timezone")
USER_AGENT = "token-usage-promote-script"

# wrangler.toml 과 어긋나면 중단하기 위한 기대값
EXPECTED_PROD_D1 = "3b3117af-7c22-4aaa-85cc-2d8ec448e9b7"
EXPECTED_DEV_D1 = "7d0d0229-8d79-41f3-9a51-64b52b73f440"

HTML_JS_HEADER = (
    "// server.py 의 INDEX_HTML 을 그대로 옮긴 파일 (scripts/sync-html.py 로 다시 만들 수 있어요)\n"
    "export default "
)


class Checks:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0

    def ok(self, msg: str) -> None:
        self.passed += 1
        print(f"OK    {msg}")

    def fail(self, msg: str) -> None:
        self.failed += 1
        print(f"FAIL  {msg}", file=sys.stderr)

    def exit(self) -> int:
        print(f"— 통과 {self.passed}개, 실패 {self.failed}개")
        return 1 if self.failed else 0


def d1_id_from_toml(text: str, env: str | None = None) -> str | None:
    if env:
        m = re.search(
            r"\[\[env\.%s\.d1_databases\]\].*?database_id\s*=\s*\"([^\"]+)\"" % re.escape(env),
            text,
            re.S,
        )
        return m.group(1) if m else None
    head = re.split(r"\n\[env\.|\n\[\[env\.", text, maxsplit=1)[0]
    m = re.search(r'database_id\s*=\s*"([^"]+)"', head)
    return m.group(1) if m else None


def load_d1_ids() -> tuple[str, str]:
    text = TOML.read_text(encoding="utf-8")
    prod = d1_id_from_toml(text)
    dev = d1_id_from_toml(text, "dev")
    if not prod or not dev:
        raise SystemExit("FAIL  wrangler.toml 에서 운영/개발 D1 database_id 를 읽지 못했어요.")
    if prod == dev:
        raise SystemExit("FAIL  운영 D1 id 와 개발 D1 id 가 같습니다. 중단합니다.")
    if prod != EXPECTED_PROD_D1 or dev != EXPECTED_DEV_D1:
        raise SystemExit(
            "FAIL  wrangler.toml 의 D1 id 가 알려진 운영/개발 값과 다릅니다. "
            "시크릿·데이터를 건드리지 않고 중단합니다."
        )
    return prod, dev


def load_index_html() -> str:
    sys.path.insert(0, str(WEB))
    if not (WEB / "raw_auth.py").exists():
        # 공개 저장소에는 raw_auth.py 가 없어요(비밀번호 파일과 묶여 있음).
        # INDEX_HTML 만 읽으면 되므로, 없을 때만 빈 모듈로 대신합니다.
        import types

        stub = types.ModuleType("raw_auth")
        stub.fingerprint = stub.load_auth = stub.verify_password = lambda *a, **k: None
        sys.modules.setdefault("raw_auth", stub)
    import server  # noqa: E402

    return server.INDEX_HTML


def expected_html_js(index_html: str) -> str:
    return HTML_JS_HEADER + json.dumps(index_html, ensure_ascii=False) + ";\n"


def extract_inline_scripts(html: str) -> list[str]:
    out: list[str] = []
    for attrs, body in re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.I | re.S):
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        out.append(body)
    return out


def node_new_function(code: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(code)
        path = fh.name
    try:
        r = subprocess.run(
            ["node", "-e", "new Function(require('fs').readFileSync(process.argv[1], 'utf8'))", path],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "unknown error").strip().splitlines()
            raise RuntimeError(err[-1] if err else f"exit {r.returncode}")
    finally:
        Path(path).unlink(missing_ok=True)


def node_check_file(path: Path) -> None:
    r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "unknown error").strip().splitlines()
        raise RuntimeError(err[-1] if err else f"exit {r.returncode}")


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authorization 헤더가 다른 호스트로 넘어가지 않게 리다이렉트는 따라가지 않고, 3xx 를 그대로 돌려줘요.
        # (http() 가 상태 코드를 반환하고 cf_request 가 3xx 를 ApiError 로 바꿔요. newurl 은 메시지에 쓰지 않아요)
        raise HTTPError(req.full_url, code, msg, headers, fp)


_NO_REDIRECT_OPENER = build_opener(_NoRedirect)


def http(method: str, url: str, data: bytes | None = None, headers: dict[str, str] | None = None,
         timeout: int = HTTP_TIMEOUT) -> tuple[int, dict[str, str], bytes]:
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        opener = _NO_REDIRECT_OPENER.open if "Authorization" in hdrs else urlopen
        with opener(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except HTTPError as e:
        body = e.read() if e.fp else b""
        return e.code, {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}, body
    except URLError as e:
        raise RuntimeError(f"{url} 요청 실패: {e.reason}") from e
    except (OSError, HTTPException) as e:  # 타임아웃, 연결 끊김, 본문 읽기 중단 등
        raise RuntimeError(f"{url} 요청 실패: {type(e).__name__}: {e}") from e


def look_json(body: bytes) -> object | None:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def smoke(checks: Checks, base: str, label: str, expect_html: str | None = None) -> None:
    # /health
    try:
        status, _, body = http("GET", base + "/health")
        payload = look_json(body)
        if status == 200 and isinstance(payload, dict) and payload.get("ok") is True and payload.get("time"):
            checks.ok(f"{label} GET /health → 200 ok=true time={payload['time']}")
        else:
            checks.fail(f"{label} GET /health → {status} (200 과 ok=true 가 필요해요)")
    except RuntimeError as e:
        checks.fail(f"{label} GET /health → {e}")

    # /
    try:
        status, headers, body = http("GET", base + "/")
        ctype = headers.get("content-type", "")
        if status != 200 or "text/html" not in ctype:
            checks.fail(f"{label} GET / → {status} content-type={ctype or '?'} (200 text/html 이 필요해요)")
        elif expect_html is not None and body.decode("utf-8") != expect_html:
            checks.fail(
                f"{label} GET / 화면이 로컬 src/html.js(server.py) 와 다릅니다 "
                f"(live {len(body)} bytes sha256={sha16(body)}). "
                f"{'개발에 현재 로컬 코드를 먼저 배포하세요.' if label == 'dev' else '운영 배포가 반영됐는지 확인하세요.'}"
            )
        else:
            extra = f" 로컬과 동일 sha256={sha16(body)}" if expect_html is not None else f" {len(body)} bytes"
            checks.ok(f"{label} GET / → 200 text/html{extra}")
    except RuntimeError as e:
        checks.fail(f"{label} GET / → {e}")

    # /api/records (데이터 내용은 출력하지 않음)
    try:
        # 운영은 엣지 캐시(20초)를 피하려고 매번 다른 쿼리를 붙여 실제 D1 읽기를 확인해요
        bust = f"&_={int(time.time() * 1000)}" if label == "prod" else ""
        status, _, body = http("GET", base + "/api/records?limit=1" + bust)
        payload = look_json(body)
        if status != 200 or not isinstance(payload, dict):
            checks.fail(f"{label} GET /api/records → {status} (200 JSON 이 필요해요)")
        else:
            missing = [k for k in RECORDS_KEYS if k not in payload]
            tz = payload.get("timezone")
            recs = payload.get("records")
            if missing:
                checks.fail(f"{label} GET /api/records JSON 키 부족: {', '.join(missing)}")
            elif tz != "Asia/Seoul":
                checks.fail(f"{label} GET /api/records timezone={tz!r} (Asia/Seoul 이어야 해요)")
            elif not isinstance(recs, list):
                checks.fail(f"{label} GET /api/records.records 가 배열이 아닙니다")
            else:
                checks.ok(
                    f"{label} GET /api/records → 200 count={payload.get('count')} "
                    f"total={payload.get('total')} (본문은 생략)"
                )
    except RuntimeError as e:
        checks.fail(f"{label} GET /api/records → {e}")

    # 인증 없는 POST /api/ingest 는 401
    try:
        status, _, body = http(
            "POST",
            base + "/api/ingest",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        payload = look_json(body)
        if status == 401 and isinstance(payload, dict) and payload.get("error"):
            checks.ok(f"{label} POST /api/ingest (인증 없음) → 401")
        else:
            checks.fail(f"{label} POST /api/ingest (인증 없음) → {status} (401 이 필요해요)")
    except RuntimeError as e:
        checks.fail(f"{label} POST /api/ingest → {e}")


def cmd_precheck(args: argparse.Namespace) -> int:
    checks = Checks()
    print("== precheck (배포하지 않음, 시크릿 미사용) ==")
    try:
        prod_d1, dev_d1 = load_d1_ids()
        checks.ok(f"wrangler.toml D1 id 확인 (운영 {prod_d1}, 개발 {dev_d1})")
    except SystemExit as e:
        checks.fail(str(e).removeprefix("FAIL  "))
        return checks.exit()

    try:
        index_html = load_index_html()
        checks.ok("web/server.py 에서 INDEX_HTML 을 읽었어요")
    except Exception as e:
        checks.fail(f"web/server.py 를 불러오지 못했어요: {type(e).__name__}: {e}")
        return checks.exit()

    if not HTML_JS.is_file():
        checks.fail(f"{HTML_JS} 가 없어요. python3 worker/scripts/sync-html.py 를 실행하세요.")
    else:
        expected = expected_html_js(index_html)
        actual = HTML_JS.read_text(encoding="utf-8")
        if actual == expected:
            checks.ok("src/html.js 가 server.py INDEX_HTML 과 같습니다")
        else:
            checks.fail("src/html.js 가 server.py 와 다릅니다. python3 worker/scripts/sync-html.py 를 실행하세요.")

    scripts = extract_inline_scripts(index_html)
    if not scripts:
        checks.fail("INDEX_HTML 에서 <script> 를 찾지 못했어요")
    else:
        for i, body in enumerate(scripts, 1):
            try:
                node_new_function(body)
                checks.ok(f"server.py <script> #{i} 구문 검사 통과 ({len(body)} bytes, node new Function)")
            except RuntimeError as e:
                checks.fail(f"server.py <script> #{i} 구문 오류: {e}")

    if INDEX_JS.is_file():
        try:
            node_check_file(INDEX_JS)
            checks.ok("src/index.js 구문 검사 통과 (node --check)")
        except RuntimeError as e:
            checks.fail(f"src/index.js 구문 오류: {e}")
    else:
        checks.fail(f"{INDEX_JS} 가 없어요")

    if HTML_JS.is_file():
        try:
            node_check_file(HTML_JS)
            checks.ok("src/html.js 구문 검사 통과 (node --check)")
        except RuntimeError as e:
            checks.fail(f"src/html.js 구문 오류: {e}")

    smoke(checks, DEV_URL, "dev", expect_html=index_html)
    return checks.exit()


def extract_js_json(src: str, prefix: str) -> object:
    i = src.index(prefix) + len(prefix)
    obj, _ = json.JSONDecoder().raw_decode(src[i:])
    return obj


def verify_deploy_js(text: str, prod_d1: str) -> list[str]:
    """생성된 배포 코드가 운영(usage-dashboard)만 가리키고, 시크릿을 포함하지 않는지 확인해요. 시크릿 값은 출력하지 않습니다."""
    errors: list[str] = []
    m = re.search(
        r"cloudflare\.request\(\{method:'PUT', path:`/accounts/\$\{accountId\}/workers/scripts/([^`]+)`",
        text,
    )
    if not m:
        errors.append("cloudflare.request PUT 경로를 찾지 못했어요")
        return errors
    name = m.group(1)
    if name != PROD_NAME:
        errors.append(f"대상 Worker 가 {name!r} 입니다. 운영({PROD_NAME})이어야 해요. 파일을 쓰지 않았거나 삭제합니다.")
        return errors
    if f"/workers/scripts/{DEV_NAME}" in text:
        errors.append(f"생성된 코드에 개발 Worker 경로({DEV_NAME})가 들어 있어요")

    try:
        meta = extract_js_json(text, "const meta = ")
    except (ValueError, json.JSONDecodeError) as e:
        errors.append(f"meta JSON 을 읽지 못했어요: {e}")
        return errors
    if not isinstance(meta, dict):
        errors.append("meta 가 객체가 아닙니다")
        return errors

    keep = meta.get("keep_bindings")
    if keep != ["secret_text"]:
        errors.append("keep_bindings 가 ['secret_text'] 가 아닙니다. 시크릿을 덮어쓸 수 있어 중단합니다.")

    bindings = meta.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        errors.append("bindings 가 비어 있어요")
        return errors

    d1 = [b for b in bindings if isinstance(b, dict) and b.get("type") == "d1"]
    if len(d1) != 1 or d1[0].get("name") != "DB" or d1[0].get("id") != prod_d1:
        errors.append("D1 바인딩이 운영 DB(token-usage)가 아닙니다. 개발 D1 을 쓰지 않도록 중단합니다.")

    secret_bindings = [b for b in bindings if isinstance(b, dict) and b.get("type") == "secret_text"]
    if secret_bindings:
        names = [str(b.get("name") or "?") for b in secret_bindings]
        errors.append(
            "생성된 코드에 secret_text 바인딩이 들어 있어요 "
            f"({', '.join(names)}). 값은 출력하지 않습니다. --secrets 없이 다시 만드세요."
        )
    return errors


def cmd_build(args: argparse.Namespace) -> int:
    out = Path(args.output).expanduser()
    prod_d1, _dev_d1 = load_d1_ids()
    print("== build (배포하지 않음, 시크릿 미사용) ==")
    print()
    print("*" * 64)
    print("  대상은 운영(PRODUCTION) Worker 입니다:  " + PROD_NAME)
    print("  개발(usage-dashboard-dev)이 아닙니다.")
    print(f"  URL : {PROD_URL}")
    print(f"  D1  : token-usage ({prod_d1})")
    print("  시크릿·기존 바인딩은 keep_bindings 로 유지됩니다.")
    print("  이 단계는 파일만 만듭니다. MCP execute 는 실행하지 않습니다.")
    print("*" * 64)
    print()

    if not MAKE_DEPLOY.is_file():
        print(f"FAIL  {MAKE_DEPLOY} 가 없어요", file=sys.stderr)
        return 1

    # --name usage-dashboard 를 명시합니다. 기본값이 usage-dashboard 여도 실수로 빠져 운영에 나가는 일을
    # 이 스크립트에서만이라도 분명히 하기 위함입니다. --secrets 는 절대 넘기지 않습니다.
    r = subprocess.run(
        [sys.executable, str(MAKE_DEPLOY), "--name", PROD_NAME],
        capture_output=True,
        text=True,
        cwd=str(WORKER),
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        # 시크릿이 있을 수 있는 생성기 출력을 그대로 덤프하지 않습니다.
        print(f"FAIL  make-deploy-code.py 가 실패했어요 (exit {r.returncode})", file=sys.stderr)
        for line in err.splitlines()[:8]:
            if re.search(r"secret|INGEST_KEY|SESSION_SECRET|RAW_SALT|RAW_HASH|password", line, re.I):
                print("      (시크릿으로 보일 수 있는 줄은 생략)", file=sys.stderr)
            else:
                print(f"      {line}", file=sys.stderr)
        return 1

    text = r.stdout
    errors = verify_deploy_js(text, prod_d1)
    if errors:
        for e in errors:
            print(f"FAIL  {e}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        print(f"주의  {out} 가 이미 있어 덮어씁니다.")
    out.write_text(text, encoding="utf-8")
    out.chmod(0o600)
    print(f"OK    생성: {out} ({out.stat().st_size} bytes, 권한 600)")
    print(f"OK    대상 Worker 경로: /workers/scripts/{PROD_NAME}")
    print(f"OK    D1 id = {prod_d1}, keep_bindings = secret_text (시크릿 값 없음)")
    print()
    print("다음 단계 (이 스크립트는 실행하지 않습니다):")
    print(f'  1) user-Cloudflare execute 에 code="$file:{out}" 로 실행')
    print("  2) 끝나면:")
    print(f"       python3 {Path(__file__).name} postcheck" + (f" -o {out}" if out != DEFAULT_OUT else ""))
    print("     가 운영 동작을 확인하고 배포 파일을 지웁니다.")
    return 0


def cmd_postcheck(args: argparse.Namespace) -> int:
    checks = Checks()
    out = Path(args.output).expanduser() if args.output is not None else None
    print("== postcheck (운영 읽기 전용 확인, 시크릿 미사용) ==")
    try:
        index_html = load_index_html()
        checks.ok("로컬 INDEX_HTML 을 읽었어요")
    except Exception as e:
        index_html = None
        checks.fail(f"web/server.py 를 불러오지 못했어요: {type(e).__name__}: {e}")

    expected = None
    if index_html is not None and HTML_JS.is_file() and HTML_JS.read_text(encoding="utf-8") == expected_html_js(index_html):
        expected = index_html
        checks.ok("src/html.js 가 server.py 와 같습니다")
    elif index_html is not None:
        checks.fail("src/html.js 가 server.py 와 다릅니다. 운영 화면 비교를 건너뜁니다.")

    smoke(checks, PROD_URL, "prod", expect_html=expected)

    if args.output is None:
        pass
    elif out.is_file():
        out.unlink()
        checks.ok(f"배포 파일을 삭제했어요: {out}")
    else:
        print(f"주의  삭제할 배포 파일이 없어요: {out}")

    return checks.exit()


def cmd_diff(args: argparse.Namespace) -> int:
    checks = Checks()
    print("== diff (읽기 전용, 데이터·시크릿은 출력하지 않음) ==")
    try:
        index_html = load_index_html()
        local_bytes = index_html.encode("utf-8")
        checks.ok(f"로컬 INDEX_HTML {len(local_bytes)} bytes sha256={sha16(local_bytes)}")
    except Exception as e:
        index_html = None
        local_bytes = None
        checks.fail(f"web/server.py 를 불러오지 못했어요: {type(e).__name__}: {e}")

    pages: dict[str, bytes | None] = {}
    for label, url in (("dev", DEV_URL), ("prod", PROD_URL)):
        try:
            status, _, body = http("GET", url + "/health")
            payload = look_json(body)
            if status == 200 and isinstance(payload, dict) and payload.get("ok") is True:
                checks.ok(f"{label} /health 200 ok=true time={payload.get('time')}")
            else:
                checks.fail(f"{label} /health → {status}")
        except RuntimeError as e:
            checks.fail(f"{label} /health → {e}")
        try:
            status, _, body = http("GET", url + "/")
            if status == 200:
                pages[label] = body
                checks.ok(f"{label} GET / {len(body)} bytes sha256={sha16(body)}")
            else:
                pages[label] = None
                checks.fail(f"{label} GET / → {status}")
        except RuntimeError as e:
            pages[label] = None
            checks.fail(f"{label} GET / → {e}")

    dev_html, prod_html = pages.get("dev"), pages.get("prod")
    if dev_html is not None and prod_html is not None:
        if dev_html == prod_html:
            checks.ok("개발과 운영의 / 화면이 같습니다")
        else:
            # 차이는 정보가 목적이라 실패로 치지 않습니다.
            print(
                f"INFO  개발과 운영의 / 화면이 다릅니다 "
                f"(dev sha256={sha16(dev_html)}, prod sha256={sha16(prod_html)}). "
                "코드 프로모션이 필요할 수 있어요."
            )
    if local_bytes is not None:
        for label, body in (("dev", dev_html), ("prod", prod_html)):
            if body is None:
                continue
            if body.decode("utf-8") == index_html:
                checks.ok(f"{label} / 화면이 로컬과 같습니다")
            else:
                print(f"INFO  {label} / 화면이 로컬과 다릅니다 (live sha256={sha16(body)})")

    return checks.exit()


# ---------------------------------------------------------------------------
# REST API 직접 배포 (deploy / status)
# ---------------------------------------------------------------------------
class ApiError(RuntimeError):
    pass


def token_fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def read_token(token_file: str | None) -> str:
    tok = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not tok:
        path = Path(token_file or DEFAULT_TOKEN_FILE).expanduser()
        if not path.is_file():
            token_fail(f"FAIL  Cloudflare API 토큰이 없어요. CLOUDFLARE_API_TOKEN 또는 {path} (권한 600) 가 필요해요.")
        if path.stat().st_mode & 0o077:
            token_fail(f"FAIL  {path} 권한이 너무 열려 있어요. chmod 600 하세요.")
        tok = path.read_text().strip()
    if not tok:
        token_fail("FAIL  Cloudflare API 토큰이 비어 있어요.")
    return tok


def cf_request(token: str, method: str, path: str, data: bytes | None = None,
               content_type: str | None = None, timeout: int = 60) -> tuple[dict[str, str], bytes]:
    """Cloudflare REST 호출. 실패 시 토큰이 들어가지 않은 메시지로 ApiError."""
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    last: Exception | None = None
    for attempt in range(3 if method == "GET" else 1):
        try:
            status, hdrs, body = http(method, CF_API + path, data=data, headers=headers, timeout=timeout)
        except RuntimeError as e:  # 네트워크 오류
            last = e
            time.sleep(3 * (attempt + 1))
            continue
        if status >= 500 and method == "GET":
            last = ApiError(f"{method} {path} → HTTP {status}")
            time.sleep(3 * (attempt + 1))
            continue
        if 300 <= status < 400:  # 리다이렉트는 따라가지 않아요(토큰 보호). Location 은 출력하지 않아요
            raise ApiError(f"{method} {path} → HTTP {status} 리다이렉트는 따르지 않아요")
        if status >= 400:
            msg = ""
            j = look_json(body)
            if isinstance(j, dict):
                msg = "; ".join(str(e.get("code", "")) + " " + str(e.get("message", "")) for e in (j.get("errors") or []) if isinstance(e, dict))
            raise ApiError(f"{method} {path} → HTTP {status} {msg}".strip())
        return hdrs, body
    raise ApiError(f"{method} {path} 실패: {last}")


def cf_json(token: str, method: str, path: str, payload: object | None = None) -> object:
    data = json.dumps(payload).encode() if payload is not None else None
    _, body = cf_request(token, method, path, data=data, content_type="application/json" if data else None)
    j = look_json(body)
    if not isinstance(j, dict) or not j.get("success"):
        raise ApiError(f"{method} {path} 응답이 success 가 아니에요")
    return j.get("result")


def script_path(name: str) -> str:
    if name != PROD_NAME and name != DEV_NAME:
        raise SystemExit(f"FAIL  알 수 없는 Worker 이름 {name!r}")
    return f"/accounts/{ACCOUNT_ID}/workers/scripts/{name}"


def latest_deployment(token: str, name: str) -> dict:
    res = cf_json(token, "GET", script_path(name) + "/deployments")
    deps = res.get("deployments") if isinstance(res, dict) else None
    if not deps:
        raise ApiError(f"{name} 배포 기록이 없어요")
    return deps[0]  # 최신이 먼저


def fmt_dep(d: dict) -> str:
    vs = ",".join(f"{v.get('version_id')}@{v.get('percentage')}%" for v in d.get("versions", []))
    return f"deployment {d.get('id')} ({d.get('created_on')}) versions[{vs}]"


def live_modules(token: str, name: str) -> dict[str, bytes]:
    hdrs, body = cf_request(token, "GET", script_path(name) + "/content/v2")
    ct = hdrs.get("content-type", "")
    msg = email.message_from_bytes(b"Content-Type: " + ct.encode() + b"\r\n\r\n" + body)
    out: dict[str, bytes] = {}
    for part in msg.get_payload():  # type: ignore[union-attr]
        n = part.get_param("name", "", "content-disposition")
        if n:
            out[n] = part.get_payload(decode=True)
    return out


def local_deploy_payload() -> tuple[dict, dict[str, str]]:
    """make-deploy-code.py 로 만든 코드를 검증한 뒤 meta/modules 를 꺼냅니다(로직을 복제하지 않고 재사용).
    파일로 쓰지 않으므로 지울 임시 파일이 없습니다."""
    prod_d1, _ = load_d1_ids()
    r = subprocess.run([sys.executable, str(MAKE_DEPLOY), "--name", PROD_NAME],
                       capture_output=True, text=True, cwd=str(WORKER))
    if r.returncode != 0:
        raise RuntimeError(f"make-deploy-code.py 실패 (exit {r.returncode})")
    errors = verify_deploy_js(r.stdout, prod_d1)
    if errors:
        raise RuntimeError("; ".join(errors))
    meta = extract_js_json(r.stdout, "const meta = ")
    modules = extract_js_json(r.stdout, "const modules = ")
    if not isinstance(meta, dict) or not isinstance(modules, dict) or set(modules) != set(MODULE_NAMES):
        raise RuntimeError("생성된 코드에서 meta/modules 를 읽지 못했어요")
    if meta.get("keep_bindings") != ["secret_text"] or meta.get("main_module") != "index.js":
        raise RuntimeError("meta 가 예상과 달라요(keep_bindings/main_module)")
    return meta, modules


def build_multipart(meta: dict, modules: dict[str, str]) -> tuple[bytes, str]:
    b = "----usage-deploy-" + uuid.uuid4().hex
    parts: list[bytes] = []

    def add(name: str, ctype: str, data: bytes, filename: str | None = None) -> None:
        fn = f'; filename="{filename}"' if filename else ""
        parts.append(
            (f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"{fn}\r\nContent-Type: {ctype}\r\n\r\n").encode()
            + data + b"\r\n"
        )

    add("metadata", "application/json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    for n in MODULE_NAMES:
        add(n, "application/javascript+module", modules[n].encode("utf-8"), n)
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def sha_mods(mods: dict[str, bytes]) -> dict[str, str]:
    return {n: sha16(mods[n]) for n in sorted(mods)}


def rollback(token: str, prev: dict) -> None:
    payload = {
        "strategy": "percentage",
        "versions": [{"version_id": v["version_id"], "percentage": v["percentage"]} for v in prev["versions"]],
        "annotations": {"workers/message": "nightly promote auto-rollback"},
    }
    path = script_path(PROD_NAME) + "/deployments"
    try:
        cf_json(token, "POST", path, payload)
    except ApiError as e:
        # 시크릿이 바뀐 상태에서 옛 버전으로 되돌리는 것은 기본적으로 막힐 수 있어요. 긴급 롤백이라 force 로 한 번 더 시도합니다.
        print(f"WARN  롤백이 막혔어요({e}). force=true 로 다시 시도합니다.", file=sys.stderr)
        cf_json(token, "POST", path + "?force=true", payload)


def rollback_with_recovery(token: str, prev: dict) -> bool:
    """롤백 요청. 응답을 못 받는 모호한 실패면 활성 배포를 다시 조회하고(재시도 포함), 서버가 실제로 되돌렸으면 성공으로 봐요."""
    def restored() -> bool | None:
        return same_versions(latest_deployment(token, PROD_NAME).get("versions", []), prev.get("versions", [])) or None

    try:
        rollback(token, prev)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"WARN  롤백 요청 오류: {type(e).__name__}: {e}. 활성 배포를 다시 확인합니다.", file=sys.stderr)
    if poll(restored, tries=3, wait=10):
        print("INFO  서버에서 이미 이전 버전으로 되돌려져 있어요.", file=sys.stderr)
        return True
    try:
        rollback(token, prev)  # 한 번 더(같은 버전으로의 롤백은 무해)
    except Exception as e:  # noqa: BLE001
        print(f"WARN  롤백 재시도도 오류: {type(e).__name__}: {e}", file=sys.stderr)
        return bool(poll(restored, tries=3, wait=10))
    return True


def post_deploy_check() -> bool:
    """배포 직후 운영 점검. 전파 지연을 고려해 한 번 더 시도해요."""
    ns = argparse.Namespace(output=None)
    for i, wait in enumerate((5, 20)):
        time.sleep(wait)
        print(f"-- postcheck 시도 {i + 1}")
        if cmd_postcheck(ns) == 0:
            return True
    return False


def same_versions(a: list, b: list) -> bool:
    f = lambda vs: sorted((v.get("version_id"), v.get("percentage")) for v in vs)  # noqa: E731
    return f(a) == f(b)


def rollback_check(token: str, prev: dict) -> bool:
    """롤백 뒤 점검: 활성 배포의 버전이 기록해 둔 이전 배포와 같고, 화면(/) 내용 비교 없이 /health·/·/api/records·ingest 401 이 정상인지."""
    for i, wait in enumerate((5, 20)):  # 전파 지연을 고려해 최대 두 번
        time.sleep(wait)
        print(f"-- 롤백 후 점검 시도 {i + 1}")
        checks = Checks()
        try:
            cur = latest_deployment(token, PROD_NAME)
            if same_versions(cur.get("versions", []), prev.get("versions", [])):
                checks.ok(f"활성 배포가 이전 버전으로 돌아왔어요: {fmt_dep(cur)}")
            else:
                checks.fail(f"활성 배포가 이전 버전과 달라요: {fmt_dep(cur)}")
            smoke(checks, PROD_URL, "prod", expect_html=None)
        except Exception as e:  # noqa: BLE001
            checks.fail(f"롤백 점검 중 오류: {type(e).__name__}: {e}")
        if checks.exit() == 0:
            return True
    return False


def list_versions(token: str) -> list[dict]:
    res = cf_json(token, "GET", script_path(PROD_NAME) + "/versions")
    items = res.get("items") if isinstance(res, dict) else None
    if not items:
        raise ApiError("운영 Worker 의 버전 목록이 비어 있어요")
    return items  # 최신 업로드가 먼저


def version_binding_problems(token: str, version_id: str, prod_d1: str) -> list[str]:
    """버전의 바인딩 이름·타입만 읽어 필수 항목(DB=운영 D1, 시크릿 5개)을 확인해요. 값은 API 가 주지 않아요."""
    res = cf_json(token, "GET", script_path(PROD_NAME) + f"/versions/{version_id}")
    blist = ((res or {}).get("resources") or {}).get("bindings") if isinstance(res, dict) else None  # type: ignore[union-attr]
    if not isinstance(blist, list):
        return ["버전의 바인딩 목록을 읽지 못했어요"]
    by = {b.get("name"): b for b in blist if isinstance(b, dict)}
    problems: list[str] = []
    db = by.get("DB")
    if not db or db.get("type") != "d1" or (db.get("database_id") or db.get("id")) != prod_d1:
        problems.append("DB(d1, 운영 token-usage)")
    for n in KEPT_SECRET_NAMES:
        if (by.get(n) or {}).get("type") != "secret_text":
            problems.append(n)
    return problems


def preupload_gate(token: str, prev: dict, prod_d1: str) -> str | None:
    """업로드 전 안전 점검. 문제가 있으면 사유 문자열(→ 종료 7), 없으면 None.
    새 버전은 '가장 최근에 올린 버전'에서 시크릿을 물려받으므로, 그 버전이 지금 트래픽을 받는 버전이고
    필수 바인딩을 모두 갖고 있어야만 올립니다."""
    vs = prev.get("versions", [])
    if len(vs) != 1 or vs[0].get("percentage") != 100:
        return f"활성 배포가 단일 버전 100% 가 아니에요: {fmt_dep(prev)}"
    items = list_versions(token)
    latest = items[0].get("id")
    active = vs[0].get("version_id")
    if latest != active:
        return (f"최근에 올린 버전({latest})이 지금 트래픽을 받는 버전({active})과 달라요 "
                "(이전 롤백·중단된 배포·수동 업로드 흔적). 시크릿을 잘못 물려받을 수 있어 올리지 않습니다")
    problems = version_binding_problems(token, active, prod_d1)
    if problems:
        return f"활성 버전({active})에 필수 바인딩이 없거나 달라요: {', '.join(problems)}"
    return None


# POST /workers/scripts/{name}/versions 가 받는 메타 필드(Cloudflare API 문서 기준). 이 밖의 키는 보내지 않아요.
# 예: observability·logpush 는 스크립트 설정(script-settings)에 있고 버전 업로드로 바꾸지 않습니다.
VERSION_META_ALLOWED = ("main_module", "annotations", "bindings", "cache_options", "compatibility_date",
                        "compatibility_flags", "exports", "package_dependencies", "usage_model")


def version_upload_meta(meta: dict) -> dict:
    """Versions API(배포 없이 업로드)용 메타. 허용 목록(VERSION_META_ALLOWED)의 필드만 담아요(observability 등은 제외).
    시크릿은 값을 쓰지 않고 'inherit'(최신 업로드 버전에서 이어받기)로만 지정해요. keep_bindings 는 쓰지 않아요.
    bindings_inherit=strict 쿼리와 함께 써서, 이어받지 못하면 조용히 빠지지 않고 업로드가 실패하게 합니다."""
    d1 = [b for b in meta["bindings"] if b.get("type") == "d1"][0]
    out = {k: v for k, v in meta.items() if k in VERSION_META_ALLOWED and k != "bindings"}
    out["bindings"] = [{"type": "d1", "name": d1["name"], "database_id": d1["id"]}] + [
        {"type": "inherit", "name": n} for n in KEPT_SECRET_NAMES
    ]
    return out


def poll(fn, tries: int = 4, wait: float = 15.0):
    """fn() 이 참 값을 줄 때까지 기다려요(응답을 잃은 요청이 서버에 반영됐는지 볼 때 씀). 끝내 없으면 None."""
    for i in range(tries):
        if i:
            time.sleep(wait)
        try:
            v = fn()
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
    return None


def cmd_status(args: argparse.Namespace) -> int:
    token = read_token(args.token_file)
    res = cf_json(token, "GET", script_path(PROD_NAME) + "/deployments")
    for d in (res.get("deployments") or [])[:5]:  # type: ignore[union-attr]
        print(fmt_dep(d))
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    if args.name != PROD_NAME:
        print(f"FAIL  deploy 는 {PROD_NAME} 만 대상으로 해요 (요청: {args.name!r})", file=sys.stderr)
        return 2
    token = read_token(args.token_file)
    mode = "DRY-RUN (업로드·배포·롤백 안 함)" if args.dry_run else "실제 배포"
    print(f"== deploy → {PROD_NAME} [{mode}] ==")

    try:
        prod_d1, _ = load_d1_ids()
        meta, modules = local_deploy_payload()
        print("OK    배포 코드 생성·검증 (대상 usage-dashboard, 운영 D1, 시크릿 값 없음)")
        local = {n: modules[n].encode("utf-8") for n in MODULE_NAMES}
        print(f"INFO  로컬 코드 {sha_mods(local)}")
        prev = latest_deployment(token, PROD_NAME)
        print(f"OK    API 인증 확인. 현재 운영: {fmt_dep(prev)}")
        prod_live = live_modules(token, PROD_NAME)
        dev_live = live_modules(token, DEV_NAME)
        gate = preupload_gate(token, prev, prod_d1)
    except (ApiError, RuntimeError) as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 1
    print(f"INFO  운영(live) {sha_mods(prod_live)}")
    print(f"INFO  개발(live) {sha_mods(dev_live)}")

    if any(dev_live.get(n) != local[n] for n in MODULE_NAMES):
        print("FAIL  개발(live) 코드가 로컬(origin/main 과 같은 체크아웃) 코드와 달라요. 개발에서 검증되지 않은 코드는 운영에 올리지 않습니다.", file=sys.stderr)
        return 3
    print("OK    개발(live) = 로컬 코드")

    if gate:
        print(f"REFUSE  {gate}. 수동 확인이 필요해요(대시보드에서 정상 버전을 100% 로 배포하면 풀려요).", file=sys.stderr)
        return 7
    print("OK    업로드 전 점검: 최근 업로드 버전 = 활성 버전이고 필수 바인딩(DB, 시크릿 5개 이름)이 있어요")

    if all(prod_live.get(n) == dev_live.get(n) for n in MODULE_NAMES) and not args.force:
        print("SKIP  개발과 운영 코드가 이미 같고 운영이 최신 버전이에요. 배포하지 않습니다.")
        return 0
    if args.dry_run:
        print(f"DRY   예정: ① POST {script_path(PROD_NAME)}/versions?bindings_inherit=strict (배포 없이 업로드) "
              f"② 새 버전 바인딩 확인 ③ POST .../deployments 100% ④ 점검 → 실패 시 {fmt_dep(prev)} 로 롤백. 실제 호출은 하지 않았어요.")
        return 0

    # ① 새 버전 업로드 (배포하지 않음 → 응답을 잃어도 트래픽에는 영향이 없어요)
    latest_before = list_versions(token)[0].get("id")
    body, ctype = build_multipart(version_upload_meta(meta), modules)
    new_id = None
    try:
        _, resp = cf_request(token, "POST", script_path(PROD_NAME) + "/versions?bindings_inherit=strict",
                             data=body, content_type=ctype, timeout=120)
        jj = look_json(resp)
        if not isinstance(jj, dict) or not jj.get("success"):
            raise ApiError("버전 업로드 응답이 success 가 아니에요")
        new_id = (jj.get("result") or {}).get("id")
        if not new_id:
            raise ApiError("버전 업로드 응답에 버전 id 가 없어요")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  버전 업로드 오류: {type(e).__name__}: {e}", file=sys.stderr)
        def _new_version_id() -> str | None:
            cur_id = list_versions(token)[0].get("id")  # 한 번만 조회해서 비교와 보고에 같은 값을 써요
            return cur_id if cur_id != latest_before else None

        seen = poll(_new_version_id)
        if seen:
            print(f"      응답은 못 받았지만 새 버전 {seen} 이 올라가 있어요. 배포하지 않았으니 트래픽은 이전 버전 그대로예요. "
                  "다음 실행은 수동 확인 전까지 거부(종료 7)됩니다.", file=sys.stderr)
        else:
            print("      새 버전이 보이지 않아요. 운영은 그대로입니다.", file=sys.stderr)
        return 4
    print(f"OK    새 버전 업로드(배포 전): {new_id}")

    # ② 배포 전에 새 버전의 바인딩(DB·시크릿 이름)을 확인
    try:
        problems = version_binding_problems(token, new_id, prod_d1)
    except (ApiError, RuntimeError) as e:
        problems = [f"확인 실패: {e}"]
    if problems:
        print(f"FAIL  새 버전({new_id})에 필수 바인딩이 없어요: {', '.join(problems)}. 배포하지 않았어요(운영은 그대로, 다음 실행은 종료 7).", file=sys.stderr)
        return 4
    print("OK    새 버전 바인딩 확인: DB(운영 D1) + 시크릿 5개 이름")

    # ③ 100% 배포
    payload = {"strategy": "percentage", "versions": [{"version_id": new_id, "percentage": 100}],
               "annotations": {"workers/message": "nightly promote dev -> prod"}}
    try:
        cf_json(token, "POST", script_path(PROD_NAME) + "/deployments", payload)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  배포 생성 오류: {type(e).__name__}: {e}", file=sys.stderr)
        # 응답을 잃었어도 서버에서 배포됐을 수 있어요. 새 배포가 보이면 점검·롤백 경로로 갑니다.
        dep = poll(lambda: (latest_deployment(token, PROD_NAME).get("id") != prev.get("id")) or None)
        if not dep:
            print("      새 배포가 보이지 않아요. 운영은 이전 버전 그대로입니다.", file=sys.stderr)
            return 4
        print("      새 배포가 확인됐어요. 점검을 진행합니다.", file=sys.stderr)

    # ④ 배포 후 확인. 어떤 오류든 롤백으로 이어져요.
    healthy = False
    try:
        cur = latest_deployment(token, PROD_NAME)
        if not same_versions(cur.get("versions", []), [{"version_id": new_id, "percentage": 100}]):
            print(f"FAIL  활성 배포가 새 버전이 아니에요: {fmt_dep(cur)}", file=sys.stderr)
        else:
            blist = cf_json(token, "GET", script_path(PROD_NAME) + "/bindings")  # 이름·타입만, 값은 오지 않아요
            names = {b.get("name") for b in (blist or []) if isinstance(b, dict)}  # type: ignore[union-attr]
            print(f"OK    배포됨 {fmt_dep(cur)} bindings={sorted(n for n in names if n)}")
            try:  # 로그·관측 설정은 스크립트 설정이라 버전 업로드로 바뀌지 않아야 해요(읽기 전용 확인, 실패해도 무시)
                st = cf_json(token, "GET", script_path(PROD_NAME) + "/script-settings")
                obs = (st or {}).get("observability") or {}  # type: ignore[union-attr]
                print(f"INFO  observability.enabled={obs.get('enabled')} (스크립트 설정, 업로드로 바꾸지 않음)")
            except Exception:  # noqa: BLE001
                pass
            now = {}
            for _ in range(3):
                now = live_modules(token, PROD_NAME)
                if all(now.get(n) == local[n] for n in MODULE_NAMES):
                    break
                time.sleep(3)
            if not all(now.get(n) == local[n] for n in MODULE_NAMES):
                print(f"FAIL  운영 코드가 올리려던 코드와 달라요 {sha_mods(now)}", file=sys.stderr)
            elif not set(REQUIRED_BINDINGS) <= names:
                print("FAIL  배포 후 바인딩(DB/시크릿 이름)이 빠졌어요.", file=sys.stderr)
            else:
                healthy = post_deploy_check()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  배포 후 확인 중 오류: {type(e).__name__}: {e}", file=sys.stderr)
    if healthy:
        print("OK    배포 성공")
        return 0

    print(f"ROLLBACK  이전 배포로 되돌립니다: {fmt_dep(prev)}", file=sys.stderr)
    if not rollback_with_recovery(token, prev):
        print("CRITICAL  롤백 요청이 실패했고 활성 배포도 이전 버전이 아니에요. 수동 확인이 필요해요!", file=sys.stderr)
        return 6
    if rollback_check(token, prev):
        print("OK    롤백 후 운영 점검 통과(새 버전이 최신 업로드로 남아 다음 실행은 수동 확인 전까지 거부돼요)", file=sys.stderr)
        return 5
    print("CRITICAL  롤백했지만 운영 점검이 통과하지 않아요. 수동 확인이 필요해요!", file=sys.stderr)
    return 6


def main() -> int:
    p = argparse.ArgumentParser(
        description="usage-dashboard-dev 에서 검증한 로컬 코드를 운영(usage-dashboard)에 올리기 위한 준비/검증, 그리고 deploy(REST API 직접 배포).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("precheck", help="구문 검사, html 동기화, 개발 환경 동작 확인")

    b = sub.add_parser("build", help="운영(usage-dashboard)용 /tmp/deploy.js 생성. 배포는 하지 않음")
    b.add_argument("-o", "--output", default=str(DEFAULT_OUT), help=f"출력 경로 (기본 {DEFAULT_OUT})")

    po = sub.add_parser("postcheck", help="운영 동작 확인 후 배포 파일 삭제")
    po.add_argument("-o", "--output", default=str(DEFAULT_OUT), help=f"삭제할 배포 파일 (기본 {DEFAULT_OUT})")

    sub.add_parser("diff", help="개발·운영 /health 와 / 화면 비교 (읽기 전용)")

    d = sub.add_parser("deploy", help="REST API 로 운영(usage-dashboard) 직접 배포 (실패 시 자동 롤백)")
    d.add_argument("--dry-run", action="store_true", help="업로드·롤백만 빼고 전부 수행")
    d.add_argument("--force", action="store_true", help="개발·운영 코드가 같아도 다시 올림")
    d.add_argument("--name", default=PROD_NAME, help=f"{PROD_NAME} 외에는 거부")
    d.add_argument("--token-file", default=None, help=f"토큰 파일 (기본 {DEFAULT_TOKEN_FILE}, CLOUDFLARE_API_TOKEN 이 우선)")
    st = sub.add_parser("status", help="운영 최근 배포 목록(읽기 전용, API 토큰 필요)")
    st.add_argument("--token-file", default=None)

    args = p.parse_args()
    if args.cmd == "deploy":
        return cmd_deploy(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "precheck":
        return cmd_precheck(args)
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "postcheck":
        return cmd_postcheck(args)
    if args.cmd == "diff":
        return cmd_diff(args)
    p.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
