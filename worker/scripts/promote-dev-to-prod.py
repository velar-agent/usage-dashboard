#!/usr/bin/env python3
"""usage-dev 에서 검증한 로컬 코드를 운영 Worker(usage)에 올리기 위한 준비/검증 스크립트.

이 스크립트는 운영에 배포하지 않습니다. Cloudflare MCP execute 는 호출하지 않습니다.
시크릿·키 파일 내용은 읽거나 출력하거나 복사하거나 덮어쓰지 않습니다. D1 데이터도 옮기지 않습니다.

사용법:
  python3 worker/scripts/promote-dev-to-prod.py precheck
  python3 worker/scripts/promote-dev-to-prod.py build              # /tmp/deploy.js (대상: 운영 usage)
  python3 worker/scripts/promote-dev-to-prod.py build -o /tmp/x.js
  # 그다음: user-Cloudflare execute 에 code="$file:/tmp/deploy.js"
  python3 worker/scripts/promote-dev-to-prod.py postcheck
  python3 worker/scripts/promote-dev-to-prod.py diff
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPTS = Path(__file__).resolve().parent
WORKER = SCRIPTS.parent
REPO = WORKER.parent
WEB = REPO / "web"
MAKE_DEPLOY = SCRIPTS / "make-deploy-code.py"
HTML_JS = WORKER / "src" / "html.js"
INDEX_JS = WORKER / "src" / "index.js"
TOML = WORKER / "wrangler.toml"
DEFAULT_OUT = Path("/tmp/deploy.js")

PROD_NAME = "usage"
DEV_NAME = "usage-dev"
PROD_URL = "https://usage.velar-agent.workers.dev"
DEV_URL = "https://usage-dev.velar-agent.workers.dev"
HTTP_TIMEOUT = 20
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


def http(method: str, url: str, data: bytes | None = None, headers: dict[str, str] | None = None,
         timeout: int = HTTP_TIMEOUT) -> tuple[int, dict[str, str], bytes]:
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except HTTPError as e:
        body = e.read() if e.fp else b""
        return e.code, {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}, body
    except URLError as e:
        raise RuntimeError(f"{url} 요청 실패: {e.reason}") from e


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
        status, _, body = http("GET", base + "/api/records?limit=1")
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
    """생성된 배포 코드가 운영(usage)만 가리키고, 시크릿을 포함하지 않는지 확인해요. 시크릿 값은 출력하지 않습니다."""
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
    print("  개발(usage-dev)이 아닙니다.")
    print(f"  URL : {PROD_URL}")
    print(f"  D1  : token-usage ({prod_d1})")
    print("  시크릿·기존 바인딩은 keep_bindings 로 유지됩니다.")
    print("  이 단계는 파일만 만듭니다. MCP execute 는 실행하지 않습니다.")
    print("*" * 64)
    print()

    if not MAKE_DEPLOY.is_file():
        print(f"FAIL  {MAKE_DEPLOY} 가 없어요", file=sys.stderr)
        return 1

    # --name usage 를 명시합니다. 기본값이 usage 여도 실수로 빠져 운영에 나가는 일을
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
    out = Path(args.output).expanduser()
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

    if out.is_file():
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


def main() -> int:
    p = argparse.ArgumentParser(
        description="usage-dev 에서 검증한 로컬 코드를 운영(usage)에 올리기 위한 준비/검증. 이 스크립트는 배포하지 않습니다.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("precheck", help="구문 검사, html 동기화, 개발 환경 동작 확인")

    b = sub.add_parser("build", help="운영(usage)용 /tmp/deploy.js 생성. 배포는 하지 않음")
    b.add_argument("-o", "--output", default=str(DEFAULT_OUT), help=f"출력 경로 (기본 {DEFAULT_OUT})")

    po = sub.add_parser("postcheck", help="운영 동작 확인 후 배포 파일 삭제")
    po.add_argument("-o", "--output", default=str(DEFAULT_OUT), help=f"삭제할 배포 파일 (기본 {DEFAULT_OUT})")

    sub.add_parser("diff", help="개발·운영 /health 와 / 화면 비교 (읽기 전용)")

    args = p.parse_args()
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
