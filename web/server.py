#!/usr/bin/env python3
"""토큰 사용량 대시보드 — Python 표준 라이브러리만 사용합니다."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from raw_auth import fingerprint, load_auth, verify_password

KST = timezone(timedelta(hours=9))
DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
MAX_RAW_FILES = 8
MAX_RAW_BYTES = 64 * 1024
MAX_RECORDS_DEFAULT = 5000
MAX_TREND_POINTS = 20000
TREND_MINUTES = (0, 30, 60, 180, 360)

COOKIE_NAME = "raw_session"
SESSION_TTL = timedelta(hours=12)
MAX_LOGIN_FAILS = 5
LOGIN_FAIL_WINDOW = 60.0
LOGIN_BODY_MAX = 4096

_SESSION_LOCK = threading.Lock()
_SESSIONS: dict[str, dict] = {}  # token -> {exp: datetime, fp: str}
_FAIL_LOCK = threading.Lock()
_FAILS: dict[str, list[float]] = {}


def _prune_sessions(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    expired = [tok for tok, rec in _SESSIONS.items() if rec["exp"] <= now]
    for tok in expired:
        _SESSIONS.pop(tok, None)


def create_session(fp: str) -> str:
    token = secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc) + SESSION_TTL
    with _SESSION_LOCK:
        _prune_sessions()
        _SESSIONS[token] = {"exp": exp, "fp": fp}
    return token


def validate_session(token: str) -> bool:
    if not token:
        return False
    record = load_auth()
    fp = fingerprint(record)
    if not fp:
        return False
    now = datetime.now(timezone.utc)
    with _SESSION_LOCK:
        _prune_sessions(now)
        rec = _SESSIONS.get(token)
        if rec is None or rec["exp"] <= now:
            return False
        if rec["fp"] != fp:
            _SESSIONS.pop(token, None)
            return False
        return True


def drop_session(token: str) -> None:
    if not token:
        return
    with _SESSION_LOCK:
        _SESSIONS.pop(token, None)


def peer_ip(handler: BaseHTTPRequestHandler) -> str:
    cf = (handler.headers.get("CF-Connecting-IP") or "").strip()
    if cf:
        return cf
    xff = (handler.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        return xff.split(",")[0].strip() or handler.client_address[0]
    return handler.client_address[0]


def login_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _FAIL_LOCK:
        times = [t for t in _FAILS.get(ip, []) if now - t < LOGIN_FAIL_WINDOW]
        _FAILS[ip] = times
        return len(times) >= MAX_LOGIN_FAILS


def record_login_fail(ip: str) -> None:
    now = time.monotonic()
    with _FAIL_LOCK:
        times = [t for t in _FAILS.get(ip, []) if now - t < LOGIN_FAIL_WINDOW]
        times.append(now)
        _FAILS[ip] = times


def env_root() -> Path:
    return Path(os.environ.get("TOKEN_USAGE_ROOT", "/workspace/token-usage")).resolve()


def env_port() -> int:
    return int(os.environ.get("PORT", "8787"))


def now_kst() -> datetime:
    return datetime.now(KST)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="seconds")


def parse_ts(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except ValueError:
        return None


def load_records(processed_dir: Path) -> tuple[list[dict], int]:
    records: list[dict] = []
    skipped = 0
    if not processed_dir.is_dir():
        return records, skipped
    files = sorted(
        p for p in processed_dir.iterdir() if p.is_file() and DATE_FILE_RE.match(p.name)
    )
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            rec = dict(obj)
            rec["_file"] = path.name
            rec["_line"] = line_no
            rec["_ts"] = to_iso(parse_ts(rec.get("ts")) or datetime.fromtimestamp(path.stat().st_mtime, KST))
            records.append(rec)
    records.sort(key=lambda r: r.get("_ts") or "", reverse=True)
    return records, skipped


def list_raw_files(raw_dir: Path) -> list[dict]:
    """최근 파일 목록(이름·크기·수정시각)만 돌려준다. 본문은 /api/raw/file 로 따로 받는다."""
    if not raw_dir.is_dir():
        return []
    files = [p for p in raw_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for path in files[:MAX_RAW_FILES]:
        try:
            st = path.stat()
        except OSError:
            continue
        out.append({"name": path.name, "size": st.st_size, "mtime": to_iso(datetime.fromtimestamp(st.st_mtime, KST))})
    return out


RAW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,200}$")


def read_raw_file(raw_dir: Path, name: str) -> dict | None:
    """raw/ 안의 파일 하나를 읽는다. 경로 조작(../, 하위 경로)은 거부한다."""
    if not RAW_NAME_RE.match(name or ""):
        return None
    path = (raw_dir / name)
    try:
        if path.resolve().parent != raw_dir.resolve() or not path.is_file():
            return None
        st = path.stat()
        data = path.read_bytes()[: MAX_RAW_BYTES + 1]
    except OSError:
        return None
    return {
        "name": name,
        "size": st.st_size,
        "mtime": to_iso(datetime.fromtimestamp(st.st_mtime, KST)),
        "truncated": len(data) > MAX_RAW_BYTES,
        "text": data[:MAX_RAW_BYTES].decode("utf-8", errors="replace"),
    }


def public_record(rec: dict) -> dict:
    item = {k: v for k, v in rec.items() if not k.startswith("_")}
    item["ts"] = rec.get("ts", rec.get("_ts"))
    return item


def series_key(rec: dict) -> tuple:
    return tuple(str(rec.get(k) or "") for k in ("service", "metric", "note"))


def trend_payload(records: list[dict], period: str, now: datetime) -> dict:
    """로컬 JSONL 서버도 Worker와 같은 버킷의 마지막 기록과 초기화 양끝을 돌려준다."""
    end = to_iso(now)
    history = [r for r in records if r["_ts"] <= end]
    since = None
    if period == "all":
        span = parse_ts(history[0]["_ts"]) - parse_ts(history[-1]["_ts"]) if history else timedelta()
        minutes = 60 if span <= timedelta(days=14) else 180 if span <= timedelta(days=60) else 360
    else:
        since = to_iso(now - timedelta(days=1 if period == "24h" else 7))
        minutes = 0 if period == "24h" else 30

    groups = {}
    for i, rec in enumerate(history):
        indices = groups.setdefault(series_key(rec), [])
        if not indices or history[indices[-1]]["_ts"] != rec["_ts"]:
            indices.append(i)

    def value(rec):
        try:
            return float(rec.get("value"))
        except (ValueError, TypeError, OverflowError):
            return float("nan")

    picks = []
    min_rank = TREND_MINUTES.index(minutes)
    for indices in groups.values():
        for j, i in enumerate(indices):
            rec = history[i]
            if since and rec["_ts"] < since:
                break
            if j == 0:
                rank = 7  # schema.sql과 같은 등급
            elif j == len(indices) - 1:
                rank = 6
            else:
                newer, older = history[indices[j - 1]], history[indices[j + 1]]
                if value(newer) < value(rec) or value(rec) < value(older):
                    rank = 5
                else:
                    t = int(parse_ts(rec["_ts"]).timestamp())
                    n = int(parse_ts(newer["_ts"]).timestamp())
                    rank = 0
                    for level in range(len(TREND_MINUTES) - 1, 0, -1):
                        seconds = TREND_MINUTES[level] * 60
                        if t // seconds != n // seconds:
                            rank = level
                            break
            if rank >= min_rank:
                picks.append((-rank, i))
    truncated = len(picks) > MAX_TREND_POINTS
    if truncated:
        picks.sort()
        picks = picks[:MAX_TREND_POINTS]
    rows = [public_record(history[i]) for i in sorted(i for _, i in picks)]
    return {"records": rows, "range": period, "since": since, "bucket_minutes": minutes,
            "count": len(rows), "limit": MAX_TREND_POINTS, "truncated": truncated,
            "generated_at": end, "timezone": "Asia/Seoul"}


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>토큰 사용량</title>
<link rel="icon" href="data:,">
<script>try{var t=localStorage.getItem("tu-theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}catch(e){}</script>
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>
/* =========================================================
   1. 디자인 토큰 — 라이트(기본) / 다크(prefers-color-scheme + data-theme)
   ========================================================= */
:root {
  --bg:#F3F5F9; --surface:#FFFFFF; --surface-2:#F7F9FC;
  --line:#E1E6EE; --line-strong:#CBD3E0; --track:#E3E8F0;
  --ink:#121A27; --ink-2:#3F4B5E; --muted:#5B6779;
  --accent:#1D4ED8; --accent-soft:#E7EEFE; --on-accent:#FFFFFF;
  --ok:#047857;   --ok-bg:#E3F4EC;
  --warn:#A84C08; --warn-bg:#FDEFDC;
  --bad:#B91C1C;  --bad-bg:#FCE8E8;
  --focus:#1D4ED8;
  --s1:#2563EB; --s2:#7C3AED; --s3:#0E7490; --s4:#C026D3; --s5:#475569;
  --halo:#FFFFFF;
  --shadow:0 1px 2px rgba(16,24,40,.05), 0 4px 16px rgba(16,24,40,.05);

  /* 간격 4px 스케일 */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px; --sp-6:32px; --sp-7:48px;
  --r-sm:8px; --r-md:12px; --r-lg:16px; --r-pill:999px;

  --font:"Pretendard Variable",Pretendard,-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",system-ui,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Noto Sans Mono CJK KR",monospace;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg:#0A0F1C; --surface:#121929; --surface-2:#182137;
  --line:#263250; --line-strong:#33415F; --track:#232E47;
  --ink:#E9EEF7; --ink-2:#C3CDDD; --muted:#97A4BA;
  --accent:#7DA9FF; --accent-soft:#172447; --on-accent:#0A0F1C;
  --ok:#34D399;   --ok-bg:#0E2F25;
  --warn:#FBBF24; --warn-bg:#36290B;
  --bad:#F87171;  --bad-bg:#3B1719;
  --focus:#7DA9FF;
  --s1:#6EA2FF; --s2:#A78BFA; --s3:#22D3EE; --s4:#E879F9; --s5:#94A3B8;
  --halo:#121929;
  --shadow:none;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg:#0A0F1C; --surface:#121929; --surface-2:#182137;
    --line:#263250; --line-strong:#33415F; --track:#232E47;
    --ink:#E9EEF7; --ink-2:#C3CDDD; --muted:#97A4BA;
    --accent:#7DA9FF; --accent-soft:#172447; --on-accent:#0A0F1C;
    --ok:#34D399;   --ok-bg:#0E2F25;
    --warn:#FBBF24; --warn-bg:#36290B;
    --bad:#F87171;  --bad-bg:#3B1719;
    --focus:#7DA9FF;
    --s1:#6EA2FF; --s2:#A78BFA; --s3:#22D3EE; --s4:#E879F9; --s5:#94A3B8;
    --halo:#121929;
    --shadow:none;
  }
}

/* =========================================================
   2. 기본
   ========================================================= */
*,*::before,*::after { box-sizing:border-box; }
[hidden] { display:none !important; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--bg); color:var(--ink); font:400 14px/22px var(--font);
  font-feature-settings:"tnum" 1; letter-spacing:-0.005em; -webkit-font-smoothing:antialiased; }
h1,h2,h3,p { margin:0; }
button,select,input { font:inherit; color:inherit; }
:focus-visible { outline:2px solid var(--focus); outline-offset:2px; border-radius:4px; }
.sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }
.skip { position:absolute; left:8px; top:-48px; background:var(--accent); color:var(--on-accent); padding:8px 12px; border-radius:var(--r-sm); z-index:100; }
.skip:focus { top:8px; }
.num { font-variant-numeric:tabular-nums; }

/* 타입 스케일 */
.t-h1 { font-size:20px; line-height:28px; font-weight:700; letter-spacing:-0.02em; }
.t-h2 { font-size:18px; line-height:26px; font-weight:700; letter-spacing:-0.015em; }
.t-h3 { font-size:15px; line-height:22px; font-weight:600; }
.t-sm { font-size:13px; line-height:18px; }
.t-cap { font-size:12px; line-height:16px; }
.muted { color:var(--muted); }

/* =========================================================
   3. 레이아웃
   ========================================================= */
.wrap { max-width:1280px; margin:0 auto; padding-inline:var(--sp-4); }
main { padding-top:var(--sp-5); padding-bottom:var(--sp-7); }
section { margin-top:var(--sp-6); }
section:first-of-type { margin-top:0; }
.sec-h { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:var(--sp-2) var(--sp-4); margin-bottom:var(--sp-3); }
.sec-h .sub { color:var(--muted); font-size:13px; }
@media (min-width:640px){ .wrap { padding-inline:var(--sp-5); } }

/* 헤더 */
.topbar { position:sticky; top:0; z-index:20; background:color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px); border-bottom:1px solid var(--line); }
.topbar .wrap { height:56px; display:flex; align-items:center; gap:var(--sp-3); }
.brand { display:flex; align-items:center; gap:var(--sp-2); margin-right:auto; min-width:0; }
.brand-mark { width:28px; height:28px; border-radius:8px; background:var(--accent); display:grid; place-items:center; flex:none; }
.brand-mark svg { width:16px; height:16px; stroke:var(--on-accent); }
.brand h1 { white-space:nowrap; }
.tz { color:var(--muted); font-size:12px; line-height:16px; border:1px solid var(--line-strong); padding:1px 6px; border-radius:var(--r-pill); white-space:nowrap; }
.refresh-bar { height:2px; background:transparent; position:relative; overflow:hidden; }
.refresh-bar i { position:absolute; inset:0 auto 0 0; width:100%; background:var(--accent); opacity:.55; transform-origin:left; transform:scaleX(0); }
@keyframes tick { from { transform:scaleX(0); } to { transform:scaleX(1); } }

.status { display:inline-flex; align-items:center; gap:var(--sp-2); height:32px; padding:0 var(--sp-3); border-radius:var(--r-pill);
  background:var(--surface); border:1px solid var(--line); font-size:13px; white-space:nowrap; }
.status .dot { width:8px; height:8px; border-radius:50%; background:var(--muted); flex:none; }
.status[data-s="ok"] .dot { background:var(--ok); animation:pulse 2s ease-in-out infinite; }
.status[data-s="stale"] { background:var(--warn-bg); border-color:transparent; color:var(--warn); }
.status[data-s="error"], .status[data-s="stale"][data-late="2"] { background:var(--bad-bg); border-color:transparent; color:var(--bad); }
.status:is([data-s="stale"], [data-s="error"]) .dot { background:currentColor; }
@keyframes pulse { 50% { opacity:.45; } }
@keyframes valueFlash { 0% { background:var(--accent-soft); } 100% { background:transparent; } }
.flash { animation:valueFlash 1.2s ease-out; border-radius:6px; }
.icon-btn { width:36px; height:36px; display:grid; place-items:center; border:1px solid var(--line); background:var(--surface); border-radius:var(--r-sm); cursor:pointer; color:var(--ink-2); }
.icon-btn:hover { border-color:var(--line-strong); color:var(--ink); }
.icon-btn svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
@media (max-width:639px){
  .topbar .wrap { height:auto; display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:2px var(--sp-2); padding-block:var(--sp-1) var(--sp-2); }
  .status { grid-column:1 / -1; grid-row:2; justify-self:start; min-height:26px; height:auto; max-width:100%; white-space:normal; }
  .icon-btn { width:44px; height:44px; }
  .tz { display:none; }
}

/* 배너 */
.banner { display:flex; gap:var(--sp-3); align-items:flex-start; padding:var(--sp-3) var(--sp-4); border-radius:var(--r-md); margin-bottom:var(--sp-4); font-size:14px; }
.banner[data-kind="stale"] { background:var(--warn-bg); color:var(--warn); }
.banner[data-kind="error"], .banner[data-late="2"] { background:var(--bad-bg); color:var(--bad); }
.banner b { font-weight:700; }
.banner .msg { flex:1; min-width:0; word-break:keep-all; overflow-wrap:anywhere; }
.banner .msg span { display:block; color:var(--ink-2); font-size:13px; }
.btn { min-height:36px; padding:0 var(--sp-4); border-radius:var(--r-sm); border:1px solid var(--line-strong); background:var(--surface); cursor:pointer; font-weight:600; font-size:13px; }
.btn:hover { border-color:var(--ink-2); }
.btn-primary { background:var(--accent); color:var(--on-accent); border-color:transparent; }
@media (max-width:639px){
  .btn { min-height:44px; }
  .banner { display:grid; grid-template-columns:auto minmax(0,1fr); gap:var(--sp-2); }
  .banner .btn { grid-column:2; justify-self:start; }
}

/* =========================================================
   4. 요약(KPI) + 한도 카드
   ========================================================= */
.kpis { display:grid; grid-template-columns:repeat(2,1fr); gap:var(--sp-3); margin-bottom:var(--sp-4); }
.kpi { background:var(--surface); border:1px solid var(--line); border-radius:var(--r-md); padding:var(--sp-3) var(--sp-4); box-shadow:var(--shadow); min-width:0; }
.kpi .k { color:var(--muted); font-size:12px; line-height:16px; }
.kpi .v { margin-top:2px; font-size:16px; line-height:24px; font-weight:700; letter-spacing:-0.01em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.kpi .v small { font-size:13px; font-weight:500; color:var(--ink-2); margin-left:var(--sp-1); }
.kpi[data-s="bad"] .v { color:var(--bad); } .kpi[data-s="warn"] .v { color:var(--warn); }
@media (max-width:639px){
  .kpis { grid-template-columns:none; grid-auto-flow:column; grid-auto-columns:78%; overflow-x:auto; scroll-snap-type:x mandatory; margin-inline:calc(var(--sp-4) * -1); padding-inline:var(--sp-4); scrollbar-width:none; }
  .kpi { scroll-snap-align:start; }
}

.seg { display:inline-flex; background:var(--surface); border:1px solid var(--line); border-radius:var(--r-sm); padding:2px; gap:2px; }
.seg button { border:0; background:transparent; height:30px; padding:0 var(--sp-3); border-radius:6px; font-size:13px; font-weight:600; color:var(--muted); cursor:pointer; }
.seg button[aria-pressed="true"] { background:var(--accent-soft); color:var(--accent); }
@media (max-width:639px){ .seg button { height:40px; } }
@media (max-width:419px){
  #sortSeg { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); width:100%; }
  #sortSeg button { white-space:nowrap; }
}

.cards { display:grid; grid-template-columns:1fr; gap:var(--sp-3); }
@media (min-width:560px){ .cards { grid-template-columns:repeat(2,1fr); gap:var(--sp-4); } }
@media (min-width:960px){ .cards { grid-template-columns:repeat(3,1fr); } }
@media (min-width:1280px){ .cards { grid-template-columns:repeat(4,1fr); } }

.card { background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); box-shadow:var(--shadow);
  padding:var(--sp-4); display:flex; flex-direction:column; gap:var(--sp-2); min-width:0; }
.card[data-s="warn"] { border-color:color-mix(in srgb, var(--warn) 45%, var(--line)); }
.card[data-s="bad"]  { border-color:color-mix(in srgb, var(--bad) 55%, var(--line)); background:color-mix(in srgb, var(--bad-bg) 35%, var(--surface)); }
.card-h { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:var(--sp-2); }
.card .svc { font-size:15px; line-height:22px; font-weight:700; letter-spacing:-0.01em; overflow-wrap:anywhere; }
.chip { display:inline-flex; align-items:center; gap:4px; height:22px; padding:0 8px; border-radius:var(--r-pill); font-size:12px; font-weight:700; line-height:16px; white-space:nowrap; }
.chip svg { width:12px; height:12px; fill:none; stroke:currentColor; stroke-width:2.4; stroke-linecap:round; stroke-linejoin:round; }
.chip[data-s="ok"]   { background:var(--ok-bg);   color:var(--ok); }
.chip[data-s="warn"] { background:var(--warn-bg); color:var(--warn); }
.chip[data-s="bad"]  { background:var(--bad-bg);  color:var(--bad); }
.chip[data-s="na"]   { background:var(--surface-2); color:var(--muted); border:1px solid var(--line); }
.kind { color:var(--ink-2); font-size:13px; line-height:18px; min-height:18px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.kind .note { color:var(--muted); }
.kind .note::before { content:"·"; margin:0 6px; }
.val-row { display:flex; align-items:baseline; justify-content:space-between; gap:var(--sp-3); margin-top:var(--sp-1); }
.big { font-size:32px; line-height:36px; font-weight:700; letter-spacing:-0.03em; font-variant-numeric:tabular-nums; }
.big small { font-size:16px; font-weight:600; margin-left:2px; color:var(--ink-2); letter-spacing:0; }
.card[data-s="warn"] .big { color:var(--warn); } .card[data-s="bad"] .big { color:var(--bad); }
.reset { text-align:right; font-size:13px; line-height:18px; color:var(--ink-2); }
.reset b { display:block; font-size:14px; line-height:20px; color:var(--ink); }

.meter { position:relative; height:8px; background:var(--track); border-radius:var(--r-pill); }
.meter .fill { position:absolute; inset:0 auto 0 0; border-radius:var(--r-pill); background:var(--ok); min-width:4px; }
.card[data-s="warn"] .fill, .lim-list td[data-s="warn"] .fill { background:var(--warn); }
.card[data-s="bad"] .fill, .lim-list td[data-s="bad"] .fill { background:var(--bad); }
.card[data-s="na"] .fill, .lim-list td[data-s="na"] .fill { background:var(--muted); }
.card-f { display:flex; justify-content:space-between; gap:var(--sp-2); color:var(--muted); font-size:12px; line-height:16px; }
.age[data-late="1"] { color:var(--warn); font-weight:600; }
.age[data-late="2"] { color:var(--bad); font-weight:600; }
@media (max-width:559px){
  .card { padding:var(--sp-3) var(--sp-4); gap:6px; }
  .big { font-size:28px; line-height:32px; }
  .card-f .pacetxt { display:none; }                       /* 모바일: 진행률 문구 생략 */
  .card-f { justify-content:flex-end; }
}

/* 스켈레톤 */
.skel { background:linear-gradient(90deg,var(--track),var(--surface-2),var(--track)); background-size:200% 100%; animation:shimmer 1.4s linear infinite; border-radius:6px; }
@keyframes shimmer { to { background-position:-200% 0; } }

/* =========================================================
   5. 추이 차트 (고정 비율)
   ========================================================= */
.charts { display:grid; grid-template-columns:1fr; column-gap:var(--sp-4); row-gap:0; }
@media (min-width:960px){ .charts { grid-template-columns:repeat(2,1fr); } }
.chart-card { row-gap:0; display:grid; grid-row:span 3; grid-template-rows:subgrid; align-content:start; margin-bottom:var(--sp-4); background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); box-shadow:var(--shadow); padding:var(--sp-4); min-width:0; }
.chart-h { display:flex; align-items:baseline; justify-content:space-between; gap:var(--sp-3); }
.chart-h .cur { font-size:13px; color:var(--muted); }
.legend { display:flex; flex-wrap:wrap; align-content:flex-start; gap:var(--sp-2); padding:var(--sp-2) 0 var(--sp-3); }
.lg { display:inline-flex; align-items:center; gap:6px; height:28px; padding:0 10px; border-radius:var(--r-pill); border:1px solid var(--line-strong); background:var(--surface); font-size:12px; cursor:pointer; color:var(--ink-2); max-width:100%; }
.lg svg { width:20px; height:8px; flex:none; }
.lg[aria-pressed="false"] { opacity:.55; text-decoration:line-through; background:var(--surface-2); }
@media (max-width:639px){
  .legend { flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none; margin-inline:calc(var(--sp-4) * -1); padding-inline:var(--sp-4); }
  .lg { height:36px; flex:none; white-space:nowrap; }
}
.plot { position:relative; width:100%; /* 높이는 JS가 clamp(180, w*0.42, 260)으로 지정 */ }
.plot svg { display:block; width:100%; height:100%; overflow:visible; }
.plot text { font-family:var(--font); font-size:11px; fill:var(--muted); }
.plot .endlab { font-size:11px; font-weight:700; paint-order:stroke; stroke:var(--halo); stroke-width:3px; stroke-linejoin:round; }
.tip { position:absolute; pointer-events:none; background:var(--surface); border:1px solid var(--line-strong); border-radius:var(--r-sm); padding:8px 10px; font-size:12px; line-height:16px; box-shadow:0 6px 20px rgba(0,0,0,.18); z-index:5; min-width:140px; }
.tip b { display:block; margin-bottom:4px; font-weight:700; }
.tip div { display:flex; justify-content:space-between; gap:12px; }
.tip i { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
.chart-empty { display:grid; place-items:center; text-align:center; color:var(--muted); background:var(--surface-2); border:1px dashed var(--line-strong); border-radius:var(--r-md); padding:var(--sp-4); }

/* =========================================================
   5.5 리셋 일정 달력
   ========================================================= */
.cal-wrap { background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); box-shadow:var(--shadow); padding:var(--sp-4); min-width:0; }
.cal-title { font-size:15px; line-height:22px; font-weight:700; margin-bottom:var(--sp-3); color:var(--ink); }
.cal-grid { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:1px; background:var(--line); border:1px solid var(--line); border-radius:var(--r-md); overflow:hidden; }
.cal-dow { background:var(--surface-2); color:var(--muted); font-size:12px; line-height:16px; font-weight:700; text-align:center; padding:8px 4px; }
.cal-day { background:var(--surface); min-height:76px; padding:6px 6px 8px 8px; display:flex; flex-direction:column; gap:2px; min-width:0; }
.cal-day[data-out="1"] { background:var(--surface-2); opacity:.55; }
.cal-day[data-today="1"] { background:var(--accent-soft); box-shadow:inset 0 0 0 1px var(--accent); }
.cal-num { font-size:12px; line-height:16px; font-weight:700; color:var(--ink-2); }
.cal-day[data-today="1"] .cal-num { color:var(--accent); }
.cal-entry { font-size:13px; line-height:18px; color:var(--ink-2); border-left:3px solid var(--accent); padding-left:5px; overflow-wrap:anywhere; word-break:keep-all; }
.cal-pct { white-space:nowrap; font-weight:400; font-variant-numeric:tabular-nums }
.cal-pct[data-s="ok"]{color:var(--ok)} .cal-pct[data-s="warn"]{color:var(--warn)} .cal-pct[data-s="bad"]{color:var(--bad)} .cal-pct[data-s="na"]{color:var(--muted)}
@media (max-width:719px){
  .cal-wrap { padding:var(--sp-3); }
  .cal-day { min-height:56px; padding:4px 4px 6px 4px; }
  .cal-dow { padding:6px 2px; font-size:11px; }
  .cal-entry { font-size:11px; line-height:14px; }
  .cal-num { font-size:11px; }
}

/* =========================================================
   6. 기록 표
   ========================================================= */
.lim-tb[hidden] { display:none; }
.toolbar.lim-tb { grid-template-columns:300px auto; justify-content:end; }
.msel { position:relative; }
.msel > summary { list-style:none; height:40px; border:1px solid var(--line-strong); background:var(--surface); border-radius:var(--r-sm); padding:0 30px 0 var(--sp-3); display:flex; align-items:center; cursor:pointer; font-size:14px; position:relative; }
.msel > summary::-webkit-details-marker { display:none; }
.msel > summary::after { content:""; position:absolute; right:12px; top:50%; width:7px; height:7px; border-right:2px solid var(--muted); border-bottom:2px solid var(--muted); transform:translateY(-70%) rotate(45deg); }
.msel[open] > summary::after { transform:translateY(-20%) rotate(225deg); }
.msel-t { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.msel-p { position:absolute; z-index:20; right:0; top:calc(100% + 4px); min-width:100%; width:max-content; max-width:380px; max-height:420px; overflow:auto; background:var(--surface); border:1px solid var(--line-strong); border-radius:var(--r-sm); box-shadow:var(--shadow); padding:4px; }
.msel-p label { display:flex; align-items:center; gap:8px; padding:8px 10px; border-radius:6px; font-size:14px; cursor:pointer; white-space:nowrap; }
.msel-p label:hover { background:var(--surface-2); }
.msel-p label.all { border-bottom:1px solid var(--line); border-radius:6px 6px 0 0; margin-bottom:2px; font-weight:600; }
.field .msel-p input { width:16px; height:16px; min-height:0; padding:0; border:0; flex:none; accent-color:var(--accent); margin:0; }
.field .msel-p label { font-size:14px; line-height:20px; color:var(--ink); font-weight:400; justify-content:flex-start; }
.field .msel-p label.all { font-weight:600; }
.field .msel-p label.grp { font-weight:700; margin-top:4px; }
.field .msel-p label.sub { padding-left:30px; }
.msel-p .m2 { color:var(--muted); }
@media (max-width:719px){ .msel > summary { height:44px; } .msel-p { left:0; right:auto; max-width:calc(100vw - 32px); } .msel-p label { padding:11px 10px; } }
.flabel { font-size:12px; line-height:16px; color:var(--muted); font-weight:600; }
.lim-tb .seg button { height:34px; }
.lim-list { max-height:none; margin-bottom:var(--sp-2); }
.lim-list[hidden], #cards[hidden] { display:none; }
.lim-list td.c-reset b { font-weight:400; }
.lim-list td.c-reset { white-space:nowrap; font-variant-numeric:tabular-nums; }
.lim-list .c-svc .chip { display:flex; width:max-content; margin-top:2px; }
.lim-list td { white-space:nowrap; }
.lim-list th:nth-child(1), .lim-list td.c-svc, .lim-list th:nth-child(2), .lim-list td:nth-child(2) { width:1%; white-space:nowrap; }
.lim-list th:nth-child(3), .lim-list td.c-note { width:1%; min-width:140px; max-width:200px; white-space:normal; overflow-wrap:break-word; word-break:keep-all; }
.lim-list th:nth-child(4), .lim-list td.c-val { width:auto; }
.lim-list .usage { display:flex; flex-wrap:wrap; align-items:center; gap:var(--sp-1) var(--sp-2); }
.lim-list .usage-value { min-width:5ch; }
.lim-list .meter { flex:1; min-width:120px; }
.lim-list .fill { min-width:0; }
@media (min-width:720px) and (max-width:959px){ .lim-list th, .lim-list td { padding-inline:6px; } }
@media (max-width:719px){ .toolbar.lim-tb { grid-template-columns:1fr; } .lim-tb .seg button { height:40px; flex:1; } .lim-tb .seg { display:flex; } }
.toolbar { display:grid; grid-template-columns:160px 160px minmax(200px,1fr) auto; gap:var(--sp-2); align-items:end; margin-bottom:var(--sp-3); }
.field { display:flex; flex-direction:column; gap:4px; min-width:0; }
.field label { font-size:12px; line-height:16px; color:var(--muted); font-weight:600; }
.field select,.field input { height:40px; border:1px solid var(--line-strong); background:var(--surface); border-radius:var(--r-sm); padding:0 var(--sp-3); width:100%; }
.field input::placeholder { color:var(--muted); }
.count { color:var(--muted); font-size:13px; white-space:nowrap; align-self:center; padding-bottom:0; }
@media (max-width:719px){
  .toolbar { grid-template-columns:1fr 1fr; }
  .toolbar .field.q { grid-column:1 / -1; order:3; }
  .toolbar .count { grid-column:1 / -1; order:4; }
  .field select,.field input { height:44px; }
}
.table-wrap { background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); box-shadow:var(--shadow); overflow:auto; max-height:520px; }
table { width:100%; border-collapse:separate; border-spacing:0; font-size:13px; line-height:18px; }
th,td { padding:10px var(--sp-3); text-align:left; border-bottom:1px solid var(--line); vertical-align:middle; }
th { position:sticky; top:0; z-index:2; background:var(--surface-2); color:var(--muted); font-size:12px; font-weight:700; border-bottom:1px solid var(--line-strong); white-space:nowrap; }
tbody tr:hover td { background:var(--surface-2); }
tbody tr:last-child td { border-bottom:0; }
td.c-time { white-space:nowrap; color:var(--ink-2); }
td.c-svc { font-weight:600; white-space:nowrap; }
td.c-val { white-space:nowrap; font-weight:700; }
td.c-val .mini { display:inline-block; width:44px; height:6px; border-radius:99px; background:var(--track); vertical-align:middle; margin-left:8px; position:relative; overflow:hidden; }
td.c-val .mini i { position:absolute; inset:0 auto 0 0; background:var(--ok); }
td.c-val[data-s="warn"] .mini i { background:var(--warn); } td.c-val[data-s="bad"] .mini i { background:var(--bad); }
td.c-note { max-width:260px; color:var(--ink-2); }
td.c-src { color:var(--muted); max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
a.raw-link { color:var(--accent); font-size:12px; margin-left:6px; text-decoration:underline; text-underline-offset:2px; }
.more { display:flex; justify-content:center; padding:var(--sp-3); border-top:1px solid var(--line); }
@media (max-width:719px){
  .table-wrap { max-height:none; overflow:visible; background:transparent; border:0; box-shadow:none; }
  thead { display:none; }
  table,tbody { display:block; }
  tbody tr { display:grid; grid-template-columns:minmax(0,1fr) auto; column-gap:var(--sp-3); row-gap:2px; padding:var(--sp-3) var(--sp-4);
    background:var(--surface); border:1px solid var(--line); border-radius:var(--r-md); margin-bottom:var(--sp-2); }
  td { display:block; padding:0; border:0; }
  tbody tr:hover td { background:transparent; }
  td.c-svc { grid-column:1; grid-row:1; font-size:15px; white-space:normal; overflow-wrap:anywhere; }
  td.c-kind { grid-column:1; grid-row:2; color:var(--muted); overflow-wrap:anywhere; }
  td.c-val { grid-column:2; grid-row:1 / span 2; align-self:center; font-size:22px; line-height:28px; text-align:right; }
  td.c-val .mini { display:none; }
  td.c-time { grid-column:1 / -1; grid-row:3; font-size:12px; color:var(--muted); margin-top:4px; }
  td.c-reset,td.c-note,td.c-src { grid-column:1 / -1; font-size:12px; line-height:16px; max-width:none; white-space:normal; }
  td.c-reset::before,td.c-note::before,td.c-src::before { content:attr(data-label) "  "; color:var(--muted); display:inline-block; min-width:44px; }
  td.c-note:empty,td.c-src:empty { display:none; }
  .more { border:0; }
  .lim-list td { white-space:normal; overflow-wrap:anywhere; }
  .lim-list th:nth-child(1), .lim-list th:nth-child(2), .lim-list td.c-svc, .lim-list td:nth-child(2) { width:auto; white-space:normal; }
  .lim-list th:nth-child(3), .lim-list td.c-note { width:auto; min-width:0; max-width:none; }
  .lim-list td.c-val { grid-column:1 / -1; grid-row:2; width:auto; text-align:left; }
  .lim-list .usage { display:grid; gap:var(--sp-1); }
  .lim-list .meter { min-width:0; }
}

/* =========================================================
   7. 원본 파일 (기본 접힘)
   ========================================================= */
details.raw { background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); box-shadow:var(--shadow); }
details.raw > summary { list-style:none; cursor:pointer; display:flex; align-items:center; gap:var(--sp-3); padding:var(--sp-3) var(--sp-4); min-height:52px; border-radius:var(--r-lg); }
details.raw > summary::-webkit-details-marker { display:none; }
details.raw > summary .chev { width:18px; height:18px; stroke:var(--muted); fill:none; stroke-width:2; transition:transform .15s; flex:none; }
details.raw[open] > summary .chev { transform:rotate(90deg); }
details.raw > summary .t { font-weight:700; font-size:16px; }
details.raw > summary .hint { margin-left:auto; color:var(--muted); font-size:13px; }
.raw-body { padding:0 var(--sp-4) var(--sp-4); }
.raw-tools { display:flex; flex-wrap:wrap; gap:var(--sp-2) var(--sp-3); align-items:center; margin-bottom:var(--sp-2); }
.raw-tools select { height:36px; border:1px solid var(--line-strong); background:var(--surface); border-radius:var(--r-sm); padding:0 var(--sp-3); max-width:100%; }
.raw-tools .meta { color:var(--muted); font-size:12px; margin-left:auto; }
.raw-tools label.chk { display:inline-flex; gap:6px; align-items:center; font-size:13px; color:var(--ink-2); }
pre.raw { margin:0; background:var(--surface-2); border:1px solid var(--line); border-radius:var(--r-md); padding:var(--sp-3) var(--sp-4);
  max-height:320px; overflow:auto; font:12.5px/1.6 var(--mono); color:var(--ink-2); white-space:pre; tab-size:2; }
pre.raw.wrapped { white-space:pre-wrap; word-break:break-all; }
.raw-note { margin-top:var(--sp-2); color:var(--muted); font-size:12px; line-height:16px; }
@media (max-width:639px){ details.raw > summary { min-height:56px; } details.raw > summary .hint { display:none; } .raw-tools .meta { margin-left:0; width:100%; } }

/* =========================================================
   8. 빈 화면
   ========================================================= */
.empty { grid-template-columns:minmax(0,1fr); background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); box-shadow:var(--shadow);
  padding:var(--sp-7) var(--sp-5); display:grid; justify-items:center; text-align:center; gap:var(--sp-3); }
.empty .ill { width:72px; height:72px; border-radius:20px; background:var(--accent-soft); display:grid; place-items:center; }
.empty .ill svg { width:36px; height:36px; stroke:var(--accent); fill:none; stroke-width:1.6; stroke-linecap:round; stroke-linejoin:round; }
.empty > * { min-width:0; max-width:100%; }
.empty p { color:var(--ink-2); max-width:46ch; }
.empty li > div { min-width:0; flex:1; }
.empty ol { margin:var(--sp-2) 0 0; padding:0; list-style:none; display:grid; grid-template-columns:minmax(0,1fr); gap:var(--sp-2); text-align:left; max-width:560px; width:100%; counter-reset:s; }
.empty li { counter-increment:s; display:flex; gap:var(--sp-3); align-items:flex-start; color:var(--ink-2); font-size:13px; }
.empty li::before { content:counter(s); flex:none; width:22px; height:22px; border-radius:50%; background:var(--surface-2); border:1px solid var(--line-strong); display:grid; place-items:center; font-size:12px; font-weight:700; color:var(--muted); }
.empty code { display:block; background:var(--surface-2); border:1px solid var(--line); border-radius:var(--r-sm); padding:8px 10px; margin-top:6px; font:12px/1.5 var(--mono); overflow-x:auto; white-space:pre; color:var(--ink-2); }
.empty .next { color:var(--muted); font-size:13px; }
@media (max-width:639px){ .empty { padding:var(--sp-6) var(--sp-4); } }

footer.foot { margin-top:var(--sp-6); color:var(--muted); font-size:12px; line-height:16px; text-align:center; }
@media (prefers-reduced-motion: reduce){ *,*::before,*::after { animation:none !important; transition:none !important; } }

/* ---- 서버 연동 보강 ---- */
.refresh-bar i.run { animation:tick var(--refresh-period) linear 1 forwards; }
.refresh-bar { display:none; }   /* 상단 진행 표시줄 움직임 끔 */
html[data-hidden="1"] .refresh-bar i { animation-play-state:paused; }
.err-box { background:var(--surface); border:1px dashed var(--line-strong); border-radius:var(--r-lg); padding:var(--sp-6) var(--sp-4); text-align:center; color:var(--ink-2); display:grid; gap:var(--sp-3); justify-items:center; }
.banner svg { flex:none; margin-top:1px; }
.card[data-s="na"] { background:var(--surface-2); }
.card[data-s="na"] .big { color:var(--muted); }
.card .note-line { color:var(--muted); font-size:12px; line-height:16px; }
.raw-err { color:var(--bad); font-size:13px; padding:var(--sp-2) 0; }
.raw-lock { display:grid; gap:var(--sp-2); max-width:420px; padding:var(--sp-2) 0 var(--sp-3); }
.raw-lock-lead { color:var(--ink-2); font-size:14px; line-height:22px; }
.raw-lock label { font-size:12px; line-height:16px; color:var(--muted); font-weight:600; }
.raw-lock-row { display:flex; flex-wrap:wrap; gap:var(--sp-2); align-items:center; }
.raw-lock input[type=password] { height:40px; border:1px solid var(--line-strong); background:var(--surface); border-radius:var(--r-sm); padding:0 var(--sp-3); flex:1; min-width:160px; }
.raw-unlock-bar { display:flex; justify-content:flex-end; margin-bottom:var(--sp-2); }
@media (max-width:639px){
  .raw-lock input[type=password], .raw-lock .btn, .raw-unlock-bar .btn { min-height:44px; height:44px; }
}
.foot a { color:inherit; text-decoration:underline; text-underline-offset:2px; }
.foot span + span::before { content:" · "; }
.plot .hit { touch-action:pan-y; }
.chart-card .plot { min-height:180px; }
td.c-time[data-dup] { color:var(--muted); }
</style>
</head>
<body>
<a class="skip" href="#main">본문으로 건너뛰기</a>

<header class="topbar">
  <div class="wrap">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke-width="2.4" stroke-linecap="round"><path d="M4 20V10M12 20V4M20 20v-7"/></svg></span>
      <h1 class="t-h1">토큰 사용량</h1>
      <span class="tz" title="모든 시각은 한국 표준시(UTC+9)">KST</span>
    </div>
    <div class="status" id="status" data-s="loading" role="status" aria-live="polite" aria-atomic="true" title="모든 시각은 한국 표준시(KST, UTC+9)">
      <span class="dot" aria-hidden="true"></span><span id="statusLabel">불러오는 중…</span>
    </div>
    <button class="icon-btn" id="btnRefresh" type="button" aria-label="지금 새로고침" title="지금 새로고침">
      <svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.5-5.8"/><path d="M20 4v5h-5"/></svg></button>
    <button class="icon-btn" id="btnTheme" type="button" aria-label="테마: 자동" title="테마: 자동 (눌러서 변경)"></button>
  </div>
  <div class="refresh-bar" aria-hidden="true"><i id="rbar"></i></div>
</header>

<main id="main" class="wrap" tabindex="-1">
  <div id="banner"></div>

  <section id="s-limits" aria-labelledby="h-limits">
    <div class="sec-h">
      <div><h2 class="t-h2" id="h-limits">한도 현황</h2><p class="sub" id="limitsSub"></p></div>
      <div class="seg" role="group" aria-label="정렬" id="sortSeg" hidden>
        <button type="button" data-sort="urgent" aria-pressed="true">사용률 높은 순</button>
        <button type="button" data-sort="reset" aria-pressed="false">초기화 임박순</button>
        <button type="button" data-sort="resetLate" aria-pressed="false">초기화 늦은 순</button>
        <button type="button" data-sort="service" aria-pressed="false">서비스별</button>
      </div>
    </div>
    <div class="toolbar lim-tb" id="limToolbar" hidden>
      <div class="field"><span class="flabel" id="lSeriesL">서비스 · 지표 · 메모</span><details class="msel" id="lSeries" data-k="k" data-all="모든 한도"><summary aria-labelledby="lSeriesL"><span class="msel-t">모든 한도</span></summary><div class="msel-p" role="group" aria-labelledby="lSeriesL"></div></details></div>
      <div class="field"><span class="flabel" id="lViewL">보기</span><div class="seg" role="group" aria-labelledby="lViewL">
        <button type="button" data-view="card" aria-pressed="true">카드</button>
        <button type="button" data-view="list" aria-pressed="false">목록</button>
      </div></div>
    </div>
    <div id="kpis" class="kpis"></div>
    <div id="cards" class="cards"></div>
    <div class="table-wrap lim-list" id="limList" hidden role="region" aria-label="한도 목록" tabindex="0">
      <table>
        <thead><tr><th scope="col">서비스</th><th scope="col">지표</th><th scope="col">메모</th><th scope="col">사용률</th><th scope="col">초기화</th></tr></thead>
        <tbody id="limBody"></tbody>
      </table>
    </div>
  </section>

  <section id="s-calendar" aria-labelledby="h-calendar">
    <div class="sec-h">
      <div><h2 class="t-h2" id="h-calendar">리셋 일정</h2><p class="sub" id="calSub">미래 초기화만 KST 달력으로 보여 줘요</p></div>
      <div class="seg" role="group" aria-label="월 이동">
        <button type="button" id="calPrev">이전 달</button>
        <button type="button" id="calNext">다음 달</button>
        <button type="button" id="calToday">오늘</button>
      </div>
    </div>
    <div class="toolbar lim-tb" id="calToolbar" hidden>
      <div class="field"><span class="flabel" id="calSeriesL">서비스 · 지표 · 메모</span><details class="msel" id="calSeries" data-k="k" data-st="cal" data-all="모든 한도"><summary aria-labelledby="calSeriesL"><span class="msel-t">모든 한도</span></summary><div class="msel-p" role="group" aria-labelledby="calSeriesL"></div></details></div>
    </div>
    <div id="calBody"></div>
  </section>

  <section id="s-trends" aria-labelledby="h-trends">
    <div class="sec-h">
      <div><h2 class="t-h2" id="h-trends">추이</h2><p class="sub">세로축 0–100% 고정 · 가로 점선은 60% / 85% 기준</p><p class="sub" id="trendStatus" role="status"></p></div>
      <div class="seg" role="group" aria-label="기간">
        <button type="button" data-range="24h" aria-pressed="false">24시간</button>
        <button type="button" data-range="7d" aria-pressed="false">7일</button>
        <button type="button" data-range="all" aria-pressed="false">전체</button>
      </div>
    </div>
    <div id="charts" class="charts"></div>
  </section>

  <section id="s-records" aria-labelledby="h-records">
    <div class="sec-h"><h2 class="t-h2" id="h-records">최근 기록</h2></div>
    <div class="toolbar" id="toolbar">
      <div class="field"><label for="fService">서비스</label><select id="fService"><option value="">모든 서비스</option></select></div>
      <div class="field"><label for="fMetric">지표</label><select id="fMetric"><option value="">모든 지표</option></select></div>
      <div class="field q"><label for="fQ">검색</label><input id="fQ" type="search" placeholder="서비스, 지표, 메모, 출처" autocomplete="off"></div>
      <span class="count" id="count" aria-live="polite"></span>
    </div>
    <div class="table-wrap" id="tableWrap" tabindex="0" role="region" aria-label="기록 표 (스크롤 가능)">
      <table>
        <thead><tr><th scope="col">시각</th><th scope="col">서비스</th><th scope="col">지표</th><th scope="col">사용률</th><th scope="col">초기화</th><th scope="col">메모</th><th scope="col">출처</th></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
      <div class="more" id="more" hidden><button class="btn" id="btnMore" type="button">더 보기</button></div>
    </div>
  </section>

  <section id="s-raw" aria-labelledby="h-raw">
    <details class="raw" id="rawDetails">
      <summary><svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg><span class="t" id="h-raw">원본 파일</span><span class="hint" id="rawHint">확인 중…</span></summary>
      <div class="raw-body" id="rawBody">
        <form class="raw-lock" id="rawLockForm" hidden>
          <p class="raw-lock-lead" id="rawLockLead">원본 파일을 보려면 비밀번호를 입력하세요.</p>
          <label for="rawPw">비밀번호</label>
          <div class="raw-lock-row">
            <input type="password" id="rawPw" name="password" autocomplete="current-password" required aria-describedby="rawLockLead rawLockErr">
            <button class="btn btn-primary" type="submit">열기</button>
          </div>
          <div class="raw-err" id="rawLockErr" role="alert" hidden></div>
        </form>
        <div id="rawUnlocked" hidden>
          <div class="raw-unlock-bar"><button class="btn" type="button" id="rawLogout">잠금</button></div>
          <p class="muted t-sm" id="rawEmpty" style="padding:8px 0" hidden>원본 파일이 아직 없어요. 로깅 봇이 raw/ 에 파일을 두면 여기에 나타납니다.</p>
          <div id="rawMain">
            <div class="raw-tools"><label class="sr-only" for="rawSel">파일 선택</label><select id="rawSel"></select>
              <label class="chk"><input type="checkbox" id="rawWrap"> 줄바꿈</label><span class="meta" id="rawMeta"></span></div>
            <div class="raw-err" id="rawErr" role="alert" hidden></div>
            <pre class="raw" id="rawPre" tabindex="0" aria-label="원본 파일 내용"></pre>
            <p class="raw-note">원본은 수집기가 남긴 그대로라서 시각이 UTC(+00:00 / Z)일 수 있어요. 최근 8개 파일, 파일당 최대 64KB까지 보여 줍니다.</p>
          </div>
        </div>
      </div>
    </details>
  </section>

  <footer class="foot" id="foot"><span><a href="/api/records">API</a></span></footer>
</main>
<script>
const KST = "Asia/Seoul";
const THRESH = { warn: 60, bad: 85 };
const COLLECT_MS = 5 * 60e3;       // 서비스별 수집 주기 (KST */5 슬롯)
const AGE_OK = 2 * COLLECT_MS;     // 10분까지 정상, 그보다 오래면 수집 지연 경고
const AGE_WARN = 3 * COLLECT_MS;   // 3회 연속 누락(15분)부터 주의 칩
const AGE_BAD = 6 * COLLECT_MS;    // 6회 연속 누락(30분)부터 위험 칩
const REFRESH_MS = 30e3;           // 구형 폴링 상수 (진행 표시줄 길이 기준 유지)
const TICK_MS = 30e3;              // 지연 재평가 간격
const COLLECT_HINT = `${COLLECT_MS / 60e3}분마다 수집`;
const REFRESH_INTERVAL = `${REFRESH_MS / 1000}초`;
const SLOT_LOAD_DELAY = 45e3;      // 슬롯 뒤 이만큼 지나서 새 기록을 받는다
const SLOT_RETRY_MS = 15e3;        // 슬롯 뒤 새 기록이 없으면 이 간격으로 재시도
const SLOT_RETRY_CAP = 2 * 60e3;   // 재시도 상한 (slot+45s 시도 뒤 2분, 그 뒤는 다음 슬롯까지 대기)
const SVC_NAME = { grok:"Grok", codex:"Codex", claude:"Claude", gemini:"Gemini", antigravity:"Antigravity", muse:"Muse" };
const METRIC_LABEL = { weekly_used_pct:"주간", session_used_pct:"세션(5시간)", weekly_scoped_used_pct:"주간·모델별", weekly_remaining_pct:"주간 남음", tokens_in:"입력 토큰", tokens_out:"출력 토큰", cost:"비용" };
const WINDOW_MS = m => /session/.test(m || "") ? 5 * 3600e3 : /weekly/.test(m || "") ? 7 * 86400e3 : null;
const $ = id => document.getElementById(id);

/* ---------- 포맷 (모두 KST) ---------- */
const WD = ["일","월","화","수","목","금","토"];
const KFMT = new Intl.DateTimeFormat("en-US", { timeZone:KST, hourCycle:"h23", year:"numeric", month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit", weekday:"short" });
function kp(t) {
  const p = {}; for (const x of KFMT.formatToParts(new Date(t))) p[x.type] = x.value;
  return { y:+p.year, mo:+p.month, d:+p.day, h:+p.hour % 24, mi:+p.minute, wd:["Sun","Mon","Tue","Wed","Thu","Fri","Sat"].indexOf(p.weekday) };
}
const p2 = n => String(n).padStart(2, "0");
const tms = v => { const t = Date.parse(v); return Number.isFinite(t) ? t : null; };
const fmtDT = t => { const k = kp(t); return `${k.mo}월${k.d}일(${WD[k.wd]}) ${p2(k.h)}:${p2(k.mi)}`; };
const fmtHM = t => { const k = kp(t); return `${p2(k.h)}:${p2(k.mi)}`; };
const fmtFull = t => { const k = kp(t); return `${k.y}-${p2(k.mo)}-${p2(k.d)} ${p2(k.h)}:${p2(k.mi)} KST`; };
function fmtIn(ms) {
  if (ms <= -60e3) return "초기화 시각 지남";
  const m = Math.max(0, Math.round(ms / 60e3)), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mi = m % 60;
  if (d >= 1) return h ? `${d}일 ${h}시간 후` : `${d}일 후`;
  if (h >= 1) return mi ? `${h}시간 ${mi}분 후` : `${h}시간 후`;
  return m >= 1 ? `${m}분 후` : "곧";
}
function fmtAgo(ms) {
  const m = Math.floor(ms / 60e3);
  if (m < 1) return "방금"; if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60); return h < 24 ? `${h}시간 ${m % 60 ? (m % 60) + "분 " : ""}전` : `${Math.floor(h / 24)}일 전`;
}
const fmtPct = n => Number.isInteger(n) ? String(n) : (Math.round(n * 10) / 10).toFixed(1).replace(/\.0$/, "");
const num = v => (v === null || v === undefined || v === "") ? NaN : Number(v);
const statusOf = n => !Number.isFinite(n) ? "na" : "ok";   // 사용률에 따른 위험·주의 구분은 2026-11-15 제거
const STATUS_TXT = { ok:"여유", warn:"주의", bad:"위험", na:"—" };
const ICON = {
  ok:  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  warn:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.01"/></svg>',
  bad: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.01"/></svg>',
  na:  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 12h12"/></svg>'
};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const svcName = s => SVC_NAME[s] || s || "알 수 없음";
const metricLabel = m => METRIC_LABEL[m] || m || "지표 없음";
const isPctRec = r => r.unit === "%" || /pct|percent/i.test(r.metric || "");
const serKey = r => `${r.service || ""}|${r.metric || ""}|${r.note || ""}`;
const fmtVal = r => { const n = num(r.value); if (!Number.isFinite(n)) return r.value === null || r.value === undefined || r.value === "" ? "—" : String(r.value);
  return isPctRec(r) ? fmtPct(n) + "%" : n.toLocaleString("ko-KR") + (r.unit ? " " + r.unit : ""); };

/* ---------- DOM 부분 갱신 (스크롤·포커스·펼침 상태 유지) ---------- */
function morph(a, b) {
  if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName) { a.replaceWith(b); return; }
  if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
  for (const at of [...a.attributes]) if (!b.hasAttribute(at.name)) a.removeAttribute(at.name);
  for (const at of [...b.attributes]) if (a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value);
  const ac = [...a.childNodes], bc = [...b.childNodes];
  for (let i = 0; i < bc.length; i++) { if (i < ac.length) morph(ac[i], bc[i]); else a.appendChild(bc[i]); }
  for (let i = ac.length - 1; i >= bc.length; i--) ac[i].remove();
}
function htmlToEl(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }
function morphList(el, items) {
  const old = new Map([...el.children].map(c => [c.dataset.key, c]));
  let prev = null;
  for (const it of items) {
    const nu = htmlToEl(it.html); nu.dataset.key = it.key;
    let cur = old.get(it.key);
    if (cur) { morph(cur, nu); old.delete(it.key); } else cur = nu;
    const want = prev ? prev.nextElementSibling : el.firstElementChild;
    if (cur !== want) el.insertBefore(cur, want);
    prev = cur;
  }
  old.forEach(c => c.remove());
}
const modes = {};   // 컨테이너별 현재 모드(skeleton | empty | data …) — 바뀔 때만 통째로 교체
function setMode(id, mode) { if (modes[id] !== mode) { modes[id] = mode; $(id).innerHTML = ""; $(id).__h = null; return true; } return false; }
function setHTML(el, html) { if (el.__h !== html) { el.__h = html; el.innerHTML = html; } }
const prevVals = new Map();   // 시리즈별 지난 표시값 (바뀐 숫자만 잠깐 강조)
function flashNumber(el) {
  if (!el || el.classList.contains("flash")) return;
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1200);
}

/* ---------- 상태 ---------- */
let RECORDS = [];          // 최신순, 중복 제거
let latestByService = new Map();   // 한도 필터와 무관한 서비스별 마지막 기록 시각
let RAW_LIST = [], rawListOk = true, rawLoaded = false, rawAuthed = false, rawLocked = false, pendingRaw = "";
let loaded = false, fetchFailed = false, lastOkAt = null, skipped = 0, hasMoreRecords = false, serverLimit = 0, extraLatest = 0;
const LF = (() => { try { return JSON.parse(localStorage.getItem("limFilter") || "{}") || {}; } catch (e) { return {}; } })();
LF.k = Array.isArray(LF.k) ? LF.k : []; delete LF.s; delete LF.m;
const saveLF = () => { try { localStorage.setItem("limFilter", JSON.stringify(LF)); } catch (e) {} };
const CF = (() => { try { return JSON.parse(localStorage.getItem("calFilter") || "{}") || {}; } catch (e) { return {}; } })();
CF.k = Array.isArray(CF.k) ? CF.k : [];
const saveCF = () => { try { localStorage.setItem("calFilter", JSON.stringify(CF)); } catch (e) {} };
// 서비스 · 지표 · 메모 체크박스 드롭다운: 선택 없음 = 전체. 서비스 줄을 누르면 그 서비스 전체 선택/해제
const serLabel = r => `${svcName(r.service)} · ${metricLabel(r.metric)}${r.note ? " · " + r.note : ""}`;
function fillSeries(el, rows, st = LF) {
  const panel = el.querySelector(".msel-p");
  const items = rows.map(r => ({ k: serKey(r), r })).sort((a, b) => svcName(a.r.service).localeCompare(svcName(b.r.service)) || metricLabel(a.r.metric).localeCompare(metricLabel(b.r.metric)) || (a.r.note || "").localeCompare(b.r.note || ""));
  const keys = items.map(x => x.k), sig = keys.join("\u0001");
  if (el.__sig !== sig) {
    el.__sig = sig;
    let html = `<label class="all"><input type="checkbox" data-all="1">${esc(el.dataset.all)}</label>`, last = null;
    for (const { k, r } of items) {
      if (r.service !== last) { last = r.service; html += `<label class="grp"><input type="checkbox" data-svc="${esc(r.service)}">${esc(svcName(r.service))}</label>`; }
      html += `<label class="sub"><input type="checkbox" value="${esc(k)}" data-s="${esc(r.service)}">${esc(metricLabel(r.metric))}${r.note ? `<span class="m2">· ${esc(r.note)}</span>` : ""}</label>`;
    }
    panel.innerHTML = html;
  }
  const sel = (Array.isArray(st && st.k) ? st.k : []).filter(k => keys.includes(k));
  panel.querySelectorAll("input[value]").forEach(i => { i.checked = sel.includes(i.value); });
  panel.querySelectorAll("input[data-svc]").forEach(i => { const sub = [...panel.querySelectorAll("input[value]")].filter(x => x.dataset.s === i.dataset.svc); const n = sub.filter(x => x.checked).length; i.checked = n > 0 && n === sub.length; i.indeterminate = n > 0 && n < sub.length; });
  panel.querySelector("input[data-all]").checked = !sel.length;
  const byK = new Map(items.map(x => [x.k, x.r]));
  el.querySelector(".msel-t").textContent = !sel.length ? el.dataset.all : sel.length === 1 ? serLabel(byK.get(sel[0])) : `${sel.length}개 한도 선택`;
  return sel;
}
const SORTS = ["urgent", "reset", "resetLate", "service"];
let sortMode = SORTS.includes(LF.o) ? LF.o : "urgent", userRange = ["24h", "7d", "all"].includes(LF.r) ? LF.r : null, shown = 0;   // 추이 기간: 고른 값은 새로고침해도 유지
const seriesVisible = {};
const trendCache = new Map();
const rangeLabel = { "24h":"24시간", "7d":"7일", all:"전체" };
let displayedTrend = null;
const pageSize = () => matchMedia("(max-width:719px)").matches ? 10 : 30;
shown = pageSize();

/* ---------- 파생 ---------- */
function latestSeries() {
  const m = new Map();
  for (const r of RECORDS) { const k = serKey(r); if (!m.has(k)) m.set(k, r); }
  return [...m.values()];
}
function filterSeriesByKeys(rows, keys) {
  const list = Array.isArray(rows) ? rows : [];
  if (!Array.isArray(keys) || keys.length === 0) return list.slice();
  const wanted = new Set(keys);
  return list.filter(row => {
    if (!row) return false;
    const k = typeof row.key === "string" ? row.key : serKey(row.r || row);
    return wanted.has(k);
  });
}
function viewOf(r, now) {
  const n = num(r.value), pct = isPctRec(r), ts = tms(r.ts), rs = tms(r.reset_at);
  const expired = false;  // 2026-11-16 사용자 요청: 초기화 시각이 지나도 '초기화됨' 판단 없이 마지막 값을 그대로 보여준다
  const st = expired || !pct ? "na" : statusOf(n);
  return { r, n, pct, ts, rs, expired, st, age: ts === null ? null : Math.max(0, now - ts), key: serKey(r) };
}
const RANK = { bad:3, warn:2, ok:1, na:0 };
function urgency(a, b) {
  if (RANK[a.st] !== RANK[b.st]) return RANK[b.st] - RANK[a.st];
  const av = Number.isFinite(a.n) ? a.n : -1, bv = Number.isFinite(b.n) ? b.n : -1;
  if (bv !== av) return bv - av;
  return (a.rs ?? 9e15) - (b.rs ?? 9e15);
}
const ageLevel = ms => ms >= AGE_BAD ? 2 : ms >= AGE_WARN ? 1 : 0;

/* ---------- KST 5분 슬롯 ---------- */
function nextSlotMs(now) {
  // KST는 UTC+9 라 고정 오프셋으로 벽시계를 얻는다 (서머타임 없음).
  const SLOT = COLLECT_MS, OFF = 9 * 3600e3;
  const kst = now + OFF;
  return Math.ceil(kst / SLOT) * SLOT - OFF;
}

/* ---------- 헤더 상태 / 배너 ---------- */
function renderCollection(now) {
  const services = Array.from(latestByService, ([service, ts]) => {
    const age = Math.max(0, now - ts);
    return { service, age, level:ageLevel(age), delayed: age > AGE_OK };
  }).sort((a, b) => b.age - a.age);
  const oldest = services[0], latest = services.at(-1);
  let state, label;
  if (fetchFailed) { state = "error"; label = "갱신 실패"; }
  else if (!loaded) { state = "loading"; label = "불러오는 중…"; }
  else if (services.some(s => s.delayed)) {
    state = "stale";
    const delayedList = services.filter(s => s.delayed);
    if (delayedList.length > 2) {
      label = `${delayedList.length}개 서비스 수집 지연 · 가장 오래 ${svcName(oldest.service)} ${Math.floor(oldest.age / 60e3)}분째`;
    } else {
      label = delayedList
        .map(s => `${svcName(s.service)} ${Math.floor(s.age / 60e3)}분째 수집 없음`).join(" · ");
      if (oldest.level === 2) label += " · 위험";
    }
  } else if (skipped) { state = "error"; label = "기록 형식 오류"; }
  else if (!latest) { state = "waiting"; label = RECORDS.length ? "수집 시각 없음" : "첫 수집 대기"; }
  else { state = "ok"; label = "실시간 수집 중"; }

  const el = $("status"), statusLabel = $("statusLabel");
  el.dataset.s = state;
  el.dataset.late = oldest?.level ?? 0;
  if (statusLabel.textContent !== label) statusLabel.textContent = label;
  renderBanner(services.filter(s => s.delayed), latest);
}
const BANNER_ICON = { error:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.01"/></svg>',
  stale:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.01"/></svg>' };
function renderBanner(delayed, latest) {
  const banners = [];
  if (fetchFailed) {
    const cached = lastOkAt ? `${fmtHM(lastOkAt)} 기준 마지막으로 받은 값을 보여 주고 있어요.` : "아직 받아 온 값이 없어요.";
    banners.push({ key:"fetch", html:`<div class="banner" data-kind="error" role="alert">
      ${BANNER_ICON.error}<div class="msg"><b>서버에 연결하지 못했어요.</b>
        <span>${cached} 수집 슬롯 기준으로 자동으로 다시 시도합니다.</span></div>
      <button class="btn" type="button" data-act="retry">지금 다시 시도</button></div>` });
  } else if (delayed.length) {
    const level = Math.max(...delayed.map(s => s.level), 1);
    const head = latest?.delayed ? `수집이 ${Math.floor(latest.age / 60e3)}분째 없어요 · 수집기가 멈췄을 수 있어요` : "일부 서비스 수집 지연";
    const details = delayed.map(s => `${svcName(s.service)} ${Math.floor(s.age / 60e3)}분째`).join(" · ");
    const cadence = `${COLLECT_MS / 60e3}분 주기, ${AGE_WARN / COLLECT_MS}회 이상 누락`;
    banners.push({ key:"stale", html:`<div class="banner" data-kind="stale" data-late="${level}" role="status">
      ${level === 2 ? BANNER_ICON.error : BANNER_ICON.stale}
      <div class="msg"><b>${head}</b><span>${esc(details)} (${cadence})</span></div></div>` });
  }
  // skipped는 파일을 읽을 때 건너뛴 줄 수로, 서비스나 발생 시각 정보가 없다.
  if (skipped) {
    banners.push({ key:"format", html:`<div class="banner" data-kind="error" role="alert">
      ${BANNER_ICON.error}<div class="msg"><b>기록 형식 오류</b>
        <span>전체 기록에서 형식이 잘못된 줄 ${esc(skipped)}개를 건너뛰었어요.</span></div></div>` });
  }
  morphList($("banner"), banners);
}

/* ---------- 요약 + 카드 ---------- */
function recordAge(v) {
  if (v.ts === null) return "";
  if (v.age <= AGE_OK) return "";   // 정상 상태는 나이 문구 없이 실시간으로 보여준다
  const level = ageLevel(v.age);
  const warning = level === 2 ? ' <span aria-hidden="true">⚠</span> 위험' : ' <span aria-hidden="true">⚠</span> 주의';
  return `<span class="age" data-late="${level || 1}" title="${fmtFull(v.ts)}">기록 ${fmtAgo(v.age)}${warning}</span>`;
}
function collectionBadge(service, now) {
  const ts = latestByService.get(service);
  if (ts === undefined) return "";
  const age = now - ts;
  if (age <= AGE_OK) return "";
  const severity = age >= AGE_BAD ? "bad" : "warn";
  return `<span class="chip" data-s="${severity}" title="서비스 마지막 수집 ${fmtFull(ts)}">${ICON[severity]}수집 지연</span>`;
}
const SKEL_KPI = Array.from({ length:3 }, () => `<div class="kpi"><div class="skel" style="height:12px;width:40%"></div><div class="skel" style="height:20px;width:70%;margin-top:8px"></div></div>`).join("");
const SKEL_CARD = Array.from({ length:8 }, () => `<article class="card" aria-hidden="true"><div class="skel" style="height:18px;width:45%"></div><div class="skel" style="height:14px;width:60%"></div><div class="skel" style="height:36px;width:35%;margin-top:6px"></div><div class="skel" style="height:8px"></div><div class="skel" style="height:12px;width:55%"></div></article>`).join("");
const EMPTY_JSON = '{"ts":"2026-11-15T15:20:00+09:00","service":"grok","metric":"weekly_used_pct","value":7,"unit":"%","reset_at":"2026-11-17T15:42:00+09:00","note":"X Premium+"}';
function renderLimits(now = Date.now()) {
  const cardsEl = $("cards"), kpis = $("kpis"), sub = $("limitsSub"), seg = $("sortSeg");
  if (!loaded || !RECORDS.length) {
    seg.hidden = true; sub.textContent = COLLECT_HINT;
    $("limToolbar").hidden = true; $("limList").hidden = true; cardsEl.hidden = false;
  }
  if (!loaded) {
    if (fetchFailed) {
      kpis.innerHTML = ""; modes.kpis = "none";
      if (setMode("cards", "failed")) cardsEl.className = "";
      setHTML(cardsEl, `<div class="err-box"><p>아직 값을 받아 오지 못했어요.</p><button class="btn btn-primary" type="button" data-act="retry">다시 시도</button></div>`);
    } else {
      if (setMode("kpis", "skel")) kpis.innerHTML = SKEL_KPI;
      if (setMode("cards", "skel")) { cardsEl.className = "cards"; cardsEl.innerHTML = SKEL_CARD; }
    }
    return;
  }
  if (!RECORDS.length) {
    kpis.innerHTML = ""; modes.kpis = "none";
    if (setMode("cards", "empty")) cardsEl.className = "";
    setHTML(cardsEl, `<div class="empty"><div class="ill"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M12 20V4M20 20v-7"/><path d="M3 20h18"/></svg></div>
      <h3 class="t-h2">아직 기록이 없어요</h3>
      <p>토큰 사용 로깅 봇이 기록을 남기면 이 화면에 서비스별 사용률이 자동으로 나타나요.</p>
      <ol><li><div>로깅 봇이 <b>processed/YYYY-MM-DD.jsonl</b>에 한 줄씩 추가합니다 (날짜는 KST 기준).<code>${esc(EMPTY_JSON)}</code></div></li>
      <li><div>수집 슬롯 기준으로 자동으로 새로고침되어 카드·추이·표가 채워집니다.</div></li></ol>
      <button class="btn btn-primary" type="button" data-act="retry">지금 새로고침</button><p class="next">기록 형식은 FORMAT.md 에서 볼 수 있어요.</p></div>`);
    return;
  }
  seg.hidden = false;
  if (setMode("cards", "data")) cardsEl.className = "cards";
  if (modes.kpis !== "data") { modes.kpis = "data"; kpis.innerHTML = ""; }
  const all = latestSeries();
  $("limToolbar").hidden = false;
  const sel = fillSeries($("lSeries"), all);
  const filtered = sel.length > 0;
  const list = all.filter(r => !filtered || sel.includes(serKey(r))).map(r => viewOf(r, now));
  if (!list.length) {
    kpis.innerHTML = ""; modes.kpis = "none";
    sub.textContent = `${COLLECT_HINT} · 조건에 맞는 한도가 없어요`;
    cardsEl.hidden = false; $("limList").hidden = true;
    if (setMode("cards", "nofilter")) cardsEl.className = "";
    setHTML(cardsEl, `<div class="empty"><p>조건에 맞는 한도가 없어요. 필터에서 "모든 한도"를 선택해 보세요.</p></div>`);
    return;
  }
  if (sortMode === "urgent") list.sort(urgency);
  else if (sortMode === "reset") list.sort((a, b) => (a.rs ?? 9e15) - (b.rs ?? 9e15) || urgency(a, b));
  else if (sortMode === "resetLate") list.sort((a, b) => (b.rs ?? -1) - (a.rs ?? -1) || urgency(a, b));
  else list.sort((a, b) => svcName(a.r.service).localeCompare(svcName(b.r.service)) || urgency(a, b));

  const byU = [...list].sort(urgency)[0];
  const withReset = list.filter(v => v.rs !== null && v.rs > now).sort((a, b) => a.rs - b.rs)[0];
  const kp1 = Number.isFinite(byU.n) && byU.pct
    ? `<div class="kpi"><div class="k">가장 높은 사용률</div><div class="v num">${fmtPct(byU.n)}%<small>${esc(svcName(byU.r.service))} · ${esc(metricLabel(byU.r.metric))}${byU.r.note ? " · " + esc(byU.r.note) : ""}</small></div></div>`
    : `<div class="kpi"><div class="k">가장 높은 사용률</div><div class="v">—</div></div>`;
  morphList(kpis, [
    { key:"max", html:kp1 },
    { key:"reset", html:`<div class="kpi"><div class="k">가장 가까운 초기화</div><div class="v">${withReset ? esc(fmtIn(withReset.rs - now)) : "—"}<small>${withReset ? esc(svcName(withReset.r.service)) + " · " + esc(metricLabel(withReset.r.metric)) : ""}</small></div></div>` },
  ]);
  sub.textContent = `${COLLECT_HINT} · 서비스 ${new Set(list.map(v => v.r.service)).size}개 · 한도 ${list.length}개${filtered ? ` (전체 ${all.length}개 중)` : ""} · ${sortMode === "urgent" ? "사용률이 높은 순" : sortMode === "reset" ? "초기화가 가까운 순" : sortMode === "resetLate" ? "초기화가 늦은 순" : "서비스 이름 순"}`;

  const listMode = LF.v === "list";
  syncView();
  cardsEl.hidden = listMode; $("limList").hidden = !listMode;
  if (listMode) {
    setHTML($("limBody"), list.map(v => {
      const r = v.r, n = v.n, st = v.st;
      const reset = v.rs ? `${esc(fmtDT(v.rs))}` : `—`;
      const txt = v.pct && Number.isFinite(n) ? fmtPct(n) + "%" : fmtVal(r);
      const changed = prevVals.has(v.key) && prevVals.get(v.key) !== txt;
      prevVals.set(v.key, txt);
      const cls = changed ? "usage-value flash" : "usage-value";
      const val = v.pct && Number.isFinite(n)
        ? `<span class="usage"><span class="${cls}">${fmtPct(n)}%</span><span class="meter" aria-hidden="true"><span class="fill" style="width:${Math.max(0, Math.min(100, n))}%"></span></span></span>`
        : v.pct ? "—" : esc(fmtVal(r));
      return `<tr><td class="c-svc">${esc(svcName(r.service))}${collectionBadge(r.service, now)}</td><td>${esc(metricLabel(r.metric))}</td><td class="c-note">${esc(r.note || "")}</td><td class="c-val num" data-s="${st}">${val}</td><td class="c-reset" title="${v.rs ? esc(fmtFull(v.rs)) : ""}">${reset}</td></tr>`;
    }).join(""));
    setTimeout(() => $("limBody").querySelectorAll(".flash").forEach(el => el.classList.remove("flash")), 1200);
  } else {
    morphList(cardsEl, list.map(v => {
      const r = v.r, n = v.n, st = v.st, win = WINDOW_MS(r.metric);
      let pace = null; if (v.rs && win && v.rs > now && !v.expired) pace = Math.max(0, Math.min(100, (1 - (v.rs - now) / win) * 100));
      let resetHtml;
      if (v.expired) resetHtml = `<b>초기화됨</b>새 값 대기 중`;
      else if (v.rs && v.rs > now) resetHtml = `<b>${esc(fmtIn(v.rs - now))}</b>${esc(fmtDT(v.rs))} 초기화`;
      else if (v.rs) resetHtml = `<b>${esc(fmtDT(v.rs))}</b>초기화`;
      else resetHtml = `<b>—</b>초기화 시각 없음`;
      const aria = v.pct ? `${svcName(r.service)} ${metricLabel(r.metric)}${r.note ? " " + r.note : ""} 사용률 ${Number.isFinite(n) ? fmtPct(n) : "알 수 없음"}퍼센트${v.expired ? ", 초기화됨, 새 값 대기 중" : ""}`
                         : `${svcName(r.service)} ${metricLabel(r.metric)}${r.note ? " " + r.note : ""} ${fmtVal(r)}`;
      const big = v.pct ? `${Number.isFinite(n) ? fmtPct(n) : "—"}<small>%</small>` : esc(fmtVal(r));
      const chip = v.expired ? `<span class="chip" data-s="na">${ICON.na}초기화됨</span>` : "";
      const meter = v.pct ? `<div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Number.isFinite(n) ? n : 0}" aria-label="${esc(aria)}"><span class="fill" style="width:${Math.max(0, Math.min(100, Number.isFinite(n) ? n : 0))}%"></span></div>` : "";
      return { key:v.key, html:`<article class="card" data-s="${st}" aria-label="${esc(aria)}">
        <div class="card-h"><span class="svc">${esc(svcName(r.service))}</span>${collectionBadge(r.service, now)}${chip}</div>
        <p class="kind">${esc(metricLabel(r.metric))}${r.note ? `<span class="note">${esc(r.note)}</span>` : ""}</p>
        <div class="val-row"><span class="big num">${big}</span><span class="reset">${resetHtml}</span></div>
        ${meter}
        <div class="card-f"><span class="pacetxt">${pace !== null ? "기간 " + Math.round(pace) + "% 경과" : "&nbsp;"}</span>${recordAge(v)}</div>
      </article>` };
    }));
    // 바뀐 숫자만 잠깐 강조한다 (제자리 갱신 유지).
    for (const card of cardsEl.children) {
      const big = card.querySelector(".big");
      if (!big) continue;
      const key = card.dataset.key, txt = big.textContent;
      if (prevVals.has(key) && prevVals.get(key) !== txt) flashNumber(big);
      prevVals.set(key, txt);
    }
  }
}

/* ---------- 차트 (인라인 SVG, 고정 높이 비율) ---------- */
const DASH = ["", "7 4", "2 3", "9 3 2 3", "1 4"];
function buildSeries(records) {
  const by = new Map();   // service -> Map(key -> rows)
  for (let i = records.length - 1; i >= 0; i--) {
    const r = records[i]; if (!isPctRec(r) || !Number.isFinite(num(r.value)) || tms(r.ts) === null) continue;
    const s = r.service || "기타"; if (!by.has(s)) by.set(s, new Map());
    const m = by.get(s), k = serKey(r); if (!m.has(k)) m.set(k, []); m.get(k).push(r);
  }
  const series = new Map();
  for (const [svc, m] of by) {
    const out = []; let i = 0;
    for (const [k, rows] of m) {
      const f = rows[0]; if (!(k in seriesVisible)) seriesVisible[k] = !/session/.test(f.metric || "");
      out.push({ key:k, label:`${metricLabel(f.metric)}${f.note ? " · " + f.note : ""}`, color:`var(--s${(i % 5) + 1})`, dash:DASH[i % 5], rows });
      i++;
    }
    series.set(svc, out);
  }
  return series;
}
function effRange() {
  if (userRange) return userRange;
  const oldest = tms(RECORDS.at(-1)?.ts);
  return oldest !== null && Date.now() - oldest < 24 * 3600e3 ? "all" : "7d";
}
function chartWindow(range, now, earliestTs, farthestResetTs) {
  // 모든 카드가 공유하는 시간 창. now가 정확히 중앙에 오도록 만든다 (순수 함수).
  const DAY = 86400e3;
  if (range === "24h") return { t0: now - DAY, t1: now + DAY };
  if (range === "7d") return { t0: now - 7 * DAY, t1: now + 7 * DAY };
  let t0 = Number.isFinite(earliestTs) ? earliestTs : now - DAY;
  if (t0 >= now) t0 = now - 30 * 60e3;
  let span = now - t0;
  if (Number.isFinite(farthestResetTs) && farthestResetTs > now) {
    const resetSpan = farthestResetTs - now;
    if (resetSpan > span) span = resetSpan;
  }
  return { t0: now - span, t1: now + span };
}
function renderTrends() {
  const root = $("charts"), rng = effRange();
  const entry = trendCache.get(rng);
  if (entry?.series) displayedTrend = entry;
  document.querySelectorAll("[data-range]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.range === rng)));
  const status = [];
  if (entry?.error) status.push(`${rangeLabel[rng]} 추이 갱신 실패`);
  else if (!entry || entry.loading) status.push(`${rangeLabel[rng]} 추이 불러오는 중…`);
  if (displayedTrend) {
    if (displayedTrend.range !== rng || entry?.error) status.push(`이전에 받은 ${rangeLabel[displayedTrend.range]} 추이 표시 중`);
    if (displayedTrend.minutes) status.push(`${displayedTrend.minutes}분 간격으로 요약`);
    if (displayedTrend.truncated) status.push(`${displayedTrend.count.toLocaleString("ko-KR")}건 상한으로 일부 지점 생략`);
  }
  const message = status.join(" · ");
  if ($("trendStatus").textContent !== message) $("trendStatus").textContent = message;
  $("trendStatus").hidden = !message;
  if (!displayedTrend) {
    if (entry?.error) { setMode("charts", "error"); return; }
    if (setMode("charts", "skel")) root.innerHTML = Array.from({ length:4 }, () => `<div class="chart-card" aria-hidden="true"><div class="skel" style="height:18px;width:30%"></div><div class="skel" style="height:200px;margin-top:16px;border-radius:12px"></div></div>`).join("");
    return;
  }
  const services = [...displayedTrend.series.keys()];
  const sparse = ![...displayedTrend.series.values()].some(arr => arr.some(s => s.rows.length >= 2));
  if (!services.length || sparse) {
    if (setMode("charts", "sparse")) root.innerHTML = "";
    setHTML(root, `<div class="chart-empty" style="grid-column:1/-1;min-height:140px"><div><b style="color:var(--ink-2)">이 기간에 추이를 그릴 사용률 기록이 부족해요</b><br>같은 시리즈에 기록이 2건 이상 있으면 그래프가 나타납니다.</div></div>`);
    return;
  }
  // 모든 카드가 같은 now·창을 공유한다 (카드별 따로 계산하지 않음).
  const now = Date.now(), rngShown = displayedTrend.range;
  let earliest = Infinity;
  let farthestReset = null;
  for (const arr of displayedTrend.series.values()) for (const s of arr) {
    for (const r of s.rows) { const t = tms(r.ts); if (t !== null && t < earliest) earliest = t; }
    const fr = latestFutureReset(s, now);
    if (fr !== null && (farthestReset === null || fr > farthestReset)) farthestReset = fr;
  }
  const win = chartWindow(rngShown, now, earliest === Infinity ? null : earliest, farthestReset);
  setMode("charts", "data");
  const have = new Map([...root.children].map(c => [c.dataset.svc, c]));
  let prev = null;
  for (const svc of services) {
    let card = have.get(svc);
    if (!card) {
      card = htmlToEl(`<div class="chart-card" data-svc="${esc(svc)}"><div class="chart-h"><h3 class="t-h3">${esc(svcName(svc))}</h3><span class="cur" data-cur></span></div><div class="legend" data-legend></div><div class="plot" data-plot></div></div>`);
    } else have.delete(svc);
    const want = prev ? prev.nextElementSibling : root.firstElementChild;
    if (card !== want) root.insertBefore(card, want);
    prev = card;
    const ser = displayedTrend.series.get(svc);
    morphList(card.querySelector("[data-legend]"), ser.map(s => ({ key:s.key,
      html:`<button class="lg" type="button" aria-pressed="${seriesVisible[s.key]}"><svg viewBox="0 0 20 8" aria-hidden="true"><line x1="1" y1="4" x2="19" y2="4" stroke="${s.color}" stroke-width="2.5" stroke-linecap="round" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""}/></svg>${esc(s.label)}</button>` })));
    drawChart(card, now, win);
  }
  have.forEach(c => c.remove());
}
// Antigravity는 미사용 0% 한도에 대해 조회 시점 기준 now+5h / now+7d 롤링 리셋을 반환해 조회할 때마다 밀리므로 그 경우만 숨긴다. 다른 서비스는 고정 리셋이라 0%도 그대로 보인다.
const hidesResetAtZero = r => {
  if (!r) return false;
  const svc = r.service || (r.key ? String(r.key).split("|")[0] : "") || (Array.isArray(r.rows) && r.rows.length ? r.rows[0].service : "");
  return String(svc || "").toLowerCase() === "antigravity";
};
function latestFutureReset(s, now) {
  // 시리즈의 최신 기록(ts 최대)의 초기화 시각이 미래이면 그 값, 아니면 null.
  let latest = null, latestTs = null;
  for (const r of s.rows) {
    const t = tms(r.ts);
    if (t === null) continue;
    if (latestTs === null || t > latestTs) { latestTs = t; latest = r; }
  }
  if (!latest) return null;
  if (num(latest.value) === 0 && (hidesResetAtZero(latest) || hidesResetAtZero(s))) return null;
  const cand = tms(latest.reset_at);
  return cand !== null && cand > now ? cand : null;
}
function latestResetInWin(s, now, t1) {
  // 시리즈의 최신 기록(ts 최대)의 초기화 시각이 미래이고 창 안이면 그 값, 아니면 null (점선 세로선 한 줄).
  const cand = latestFutureReset(s, now);
  return cand !== null && cand <= t1 ? cand : null;
}

/* ---------- 리셋 일정 달력 (순수 함수 + 렌더) ---------- */
let calCursor = null;   // 표시 중인 월 (미persist) null=현재 KST 월
function monthShift(year, month, delta) {
  const total = year * 12 + (month - 1) + delta;
  const y = Math.floor(total / 12);
  const m = total - y * 12 + 1;
  return { y, m, year: y, month: m };
}
function calendarEntries(seriesList, now) {
  const out = [];
  for (const item of seriesList || []) {
    if (!item) continue;
    const r = item.r || item;
    if (!r) continue;
    const rs = item.rs !== undefined && item.rs !== null ? item.rs : tms(r.reset_at);
    const n = item.n !== undefined && item.n !== null ? item.n : num(r.value);
    if (rs === null || rs === undefined || !Number.isFinite(rs) || rs <= now) continue;
    if (n === 0 && hidesResetAtZero(r)) continue;
    const nr = num(r.value);
    const usage = isPctRec(r) && !Number.isFinite(nr) ? "—" : fmtVal(r);
    const st = item.st !== undefined && item.st !== null ? item.st : (isPctRec(r) ? statusOf(num(r.value)) : "na");
    out.push({ t: rs, service: r.service, metric: r.metric, note: r.note || "", usage, st });
  }
  out.sort((a, b) => a.t - b.t || String(a.service).localeCompare(String(b.service)) || String(a.metric).localeCompare(String(b.metric)));
  return out;
}
function calendarMonth(year, month, entries, now) {
  const title = `${year}년 ${month}월`;
  const todayK = kp(now);
  const kstMidnight = (y, m, d) => Date.UTC(y, m - 1, d, 0, 0) - 9 * 3600e3;
  const firstMs = kstMidnight(year, month, 1);
  const firstWd = kp(firstMs).wd;
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const numWeeks = Math.ceil((firstWd + daysInMonth) / 7);
  const startMs = firstMs - firstWd * 86400e3;
  const byDay = new Map();
  for (const e of entries || []) {
    const k = kp(e.t);
    const key = `${k.y}-${k.mo}-${k.d}`;
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(e);
  }
  for (const arr of byDay.values()) arr.sort((a, b) => a.t - b.t || String(a.service).localeCompare(String(b.service)) || String(a.metric).localeCompare(String(b.metric)));
  const weeks = [];
  for (let w = 0; w < numWeeks; w++) {
    const week = [];
    for (let d = 0; d < 7; d++) {
      const cellMs = startMs + (w * 7 + d) * 86400e3;
      const k = kp(cellMs);
      const key = `${k.y}-${k.mo}-${k.d}`;
      const inMonth = k.y === year && k.mo === month;
      const today = k.y === todayK.y && k.mo === todayK.mo && k.d === todayK.d;
      week.push({ y: k.y, m: k.mo, d: k.d, inMonth, today, entries: byDay.get(key) || [] });
    }
    weeks.push(week);
  }
  return { title, weeks };
}
function renderCalendar(now = Date.now()) {
  const root = $("calBody");
  if (!root) return;
  const calSub = $("calSub"), calToolbar = $("calToolbar"), calSeries = $("calSeries");
  const calSubText = filtered => { if (calSub) calSub.textContent = filtered ? "미래 초기화만 KST 달력으로 보여 줘요 · 필터 적용 중" : "미래 초기화만 KST 달력으로 보여 줘요"; };
  if (!loaded) {
    if (calToolbar) calToolbar.hidden = true;
    calSubText(false);
    root.innerHTML = `<div class="chart-empty" style="min-height:140px"><div>리셋 일정을 불러오는 중이에요</div></div>`;
    return;
  }
  if (!RECORDS.length) {
    if (calToolbar) calToolbar.hidden = true;
    calSubText(false);
    root.innerHTML = `<div class="chart-empty" style="min-height:140px"><div>표시할 리셋 일정이 없어요</div></div>`;
    return;
  }
  if (calToolbar) calToolbar.hidden = false;
  const all = latestSeries();
  const sel = calSeries ? fillSeries(calSeries, all, CF) : CF.k.filter(k => all.some(r => serKey(r) === k));
  const filtered = sel.length > 0;
  const seriesList = filterSeriesByKeys(all.map(r => viewOf(r, now)), sel);
  const entries = calendarEntries(seriesList, now);
  calSubText(filtered);
  if (!entries.length) {
    root.innerHTML = filtered
      ? `<div class="chart-empty" style="min-height:140px"><div>조건에 맞는 리셋 일정이 없어요</div></div>`
      : `<div class="chart-empty" style="min-height:140px"><div>표시할 리셋 일정이 없어요</div></div>`;
    return;
  }
  if (!calCursor) {
    const k = kp(now);
    calCursor = { y: k.y, m: k.mo };
  }
  const { title, weeks } = calendarMonth(calCursor.y, calCursor.m, entries, now);
  const dowHtml = WD.map(c => `<div class="cal-dow">${c}</div>`).join("");
  let cells = "";
  for (const week of weeks) {
    for (const day of week) {
      const todayAttr = day.today ? ` data-today="1" aria-current="date"` : "";
      const outAttr = day.inMonth ? "" : ` data-out="1"`;
      const entryHtml = day.entries.map(e => {
        const label = `${esc(svcName(e.service))} ${esc(metricLabel(e.metric))} ${fmtHM(e.t)}`;
        const usage = e.usage !== undefined && e.usage !== null ? e.usage : "—";
        const st = e.st !== undefined && e.st !== null ? e.st : "na";
        const titleAttr = ` title="${label} - ${esc(usage)}${e.note ? " · " + esc(e.note) : ""}"`;
        return `<div class="cal-entry"${titleAttr}>${label} - <span class="cal-pct" data-s="${esc(st)}">${esc(usage)}</span></div>`;
      }).join("");
      cells += `<div class="cal-day"${outAttr}${todayAttr}><div class="cal-num">${day.d}</div>${entryHtml}</div>`;
    }
  }
  root.innerHTML = `<div class="cal-wrap"><div class="cal-title">${esc(title)}</div><div class="cal-grid" role="grid" aria-label="${esc(title)} 리셋 일정">${dowHtml}${cells}</div></div>`;
}
try {
  const calPrev = $("calPrev"), calNext = $("calNext"), calTodayBtn = $("calToday");
  if (calPrev) calPrev.onclick = () => {
    const cur = calCursor || (() => { const k = kp(Date.now()); return calCursor = { y: k.y, m: k.mo }; })();
    const nxt = monthShift(cur.y, cur.m, -1);
    calCursor = { y: nxt.y, m: nxt.m };
    renderCalendar(Date.now());
  };
  if (calNext) calNext.onclick = () => {
    const cur = calCursor || (() => { const k = kp(Date.now()); return calCursor = { y: k.y, m: k.mo }; })();
    const nxt = monthShift(cur.y, cur.m, 1);
    calCursor = { y: nxt.y, m: nxt.m };
    renderCalendar(Date.now());
  };
  if (calTodayBtn) calTodayBtn.onclick = () => {
    const k = kp(Date.now());
    calCursor = { y: k.y, m: k.mo };
    renderCalendar(Date.now());
  };
} catch (e) {}

function buildChartSvg(opts) {
  // 카드 SVG 내부 문자열을 만드는 순수 빌더 (테스트에서 직접 호출할 수 있다).
  const { vis, now, t0, t1, W, H } = opts;
  const range = opts.range;
  const weekStart = range !== undefined ? (range === "7d" || range === "all") : !!opts.weekStart;
  const isWeeklySeries = s => {
    const keyMetric = (s.key || "").split("|")[1] || "";
    if (/weekly/.test(keyMetric)) return true;
    if (/weekly/.test(s.metric || "")) return true;
    if ((s.rows || []).some(r => /weekly/.test(r.metric || ""))) return true;
    if (!(s.key || "").includes("|") && /weekly/.test(s.key || "")) return true;
    return false;
  };
  const MIN = 60e3, HR = 3600e3, DAY = 86400e3;
  // 시리즈마다 최신 기록(ts 최대)의 미래·창 안 초기화 시각 하나만 (점선 세로선). 오래된 기록은 무시한다.
  const resetsInWin = vis.map(s => ({ s, reset: latestResetInWin(s, now, t1) }));
  const inHistory = r => {
    const t = tms(r.ts);
    return t >= t0 && t <= now;
  };
  const inWin = vis.map(s => ({ s, rows:s.rows.filter(inHistory) })).filter(o => o.rows.length);
  const m = { l:36, r:48, t:20, b:24 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const span = t1 - t0;
  const X = t => m.l + (t - t0) / span * iw, Y = v => m.t + (1 - Math.max(0, Math.min(100, v)) / 100) * ih;
  const nowX = X(now), right = W - m.r;
  let g = "";   // 미래 구간은 빈 여백으로 두어 음영·현재선 없이 둔다
  [0, 25, 50, 75, 100].forEach(v => { g += `<line x1="${m.l}" x2="${m.l + iw}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)" stroke-width="1"/><text x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${v}%</text>`; });
  g += `<line x1="${m.l}" x2="${m.l + iw}" y1="${Y(THRESH.warn)}" y2="${Y(THRESH.warn)}" stroke="var(--warn)" stroke-width="1" stroke-dasharray="3 4" opacity=".7"/>`;
  g += `<line x1="${m.l}" x2="${m.l + iw}" y1="${Y(THRESH.bad)}" y2="${Y(THRESH.bad)}" stroke="var(--bad)" stroke-width="1" stroke-dasharray="3 4" opacity=".7"/>`;
  // 초기화 점선 세로선: 데이터 경로보다 먼저 그려(=아래에) 둔다.
  for (const { s, reset } of resetsInWin) {
    if (reset === null) continue;
    const x = X(reset);
    g += `<line x1="${x}" x2="${x}" y1="${m.t}" y2="${m.t + ih}" stroke="${s.color}" stroke-width="1.5" stroke-dasharray="1.5 3" stroke-linecap="round" opacity=".7"/>`;
    if (weekStart && isWeeklySeries(s)) {
      const weekMs = reset - 7 * DAY;
      if (weekMs >= t0 && weekMs <= t1) {
        const wx = X(weekMs);
        g += `<line x1="${wx}" x2="${wx}" y1="${m.t}" y2="${m.t + ih}" stroke="${s.color}" stroke-width="1.5" stroke-dasharray="1.5 3" stroke-linecap="round" opacity=".4"/>`;
      }
    }
  }
  const maxTicks = Math.max(3, Math.floor(iw / 64));
  const cand = [10 * MIN, 30 * MIN, HR, 3 * HR, 6 * HR, 12 * HR, DAY, 2 * DAY, 3 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];
  const step = cand.find(c => span / c <= maxTicks) || cand[cand.length - 1];
  const off = 9 * HR; let tk = Math.ceil((t0 + off) / step) * step - off;
  for (; tk <= t1; tk += step) { const k = kp(tk), x = X(tk);
    const lab = step >= DAY || (span > DAY && k.h === 0 && k.mi === 0) ? `${k.mo}/${k.d}` : `${p2(k.h)}:${p2(k.mi)}`;
    g += `<line x1="${x}" x2="${x}" y1="${m.t + ih}" y2="${m.t + ih + 4}" stroke="var(--line-strong)"/><text x="${x}" y="${H - 6}" text-anchor="middle">${lab}</text>`; }
  const ends = [];
  inWin.forEach(({ s, rows }) => {
    let d = "", prev = null;
    rows.forEach(r => { const v = num(r.value), x = X(tms(r.ts)).toFixed(1), y = Y(v).toFixed(1);
      d += `${prev === null || v < prev - 1e-9 ? "M" : "L"}${x},${y}`; prev = v; });   // 값이 줄면(주기 초기화) 선을 끊음
    if (rows.length === 1) g += `<circle cx="${X(tms(rows[0].ts))}" cy="${Y(num(rows[0].value))}" r="2.5" fill="${s.color}"/>`;
    else g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""}/>`;
    const last = rows[rows.length - 1]; ends.push({ s, x:X(tms(last.ts)), y:Y(num(last.value)), v:num(last.value) });
  });
  ends.sort((a, b) => a.y - b.y);
  const gap = Math.min(13, ih / Math.max(1, ends.length - 1));
  let py = m.t - gap;
  ends.forEach((e, i) => {
    py = Math.min(Math.max(e.y, py + gap), m.t + ih - (ends.length - 1 - i) * gap);
    g += `<circle cx="${e.x}" cy="${e.y}" r="3.5" fill="${e.s.color}" stroke="var(--surface)" stroke-width="1.5"/><text class="endlab" x="${right + 8}" y="${py + 4}" style="fill:${e.s.color}">${fmtPct(e.v)}%</text>`;
  });
  return { inner: g, nowX, X, Y, m, iw, ih, span, inWin, ends };
}
function drawChart(card, now, win) {
  // now·창은 renderTrends가 한 번 계산해서 넘긴다 (여기서 현재시각을 새로 구하지 않음).
  const plot = card.querySelector("[data-plot]"), ser = displayedTrend.series.get(card.dataset.svc) || [];
  const W = Math.max(240, Math.round(plot.clientWidth || card.clientWidth - 34 || 320));
  const H = Math.max(180, Math.min(260, Math.round(W * 0.42)));
  const HR = 3600e3;
  const rng = displayedTrend.range;
  const { t0, t1 } = win;
  const vis = ser.filter(s => seriesVisible[s.key]);
  const sig = [W, H, rng, displayedTrend.version, t0, t1,
    ...vis.map(s => `${s.key}:${latestResetInWin(s, now, t1) ?? ""}`)].join("|");
  if (plot.dataset.sig === sig) return;
  plot.dataset.sig = sig; plot.style.height = H + "px";
  const cur = card.querySelector("[data-cur]");
  const emptyBox = msg => { plot.innerHTML = `<div class="chart-empty" style="height:${H}px"><div>${msg}</div></div>`; cur.textContent = ""; };
  if (!vis.length) return emptyBox("표시할 항목을 위 범례에서 선택하세요.");
  const built = buildChartSvg({ vis, now, t0, t1, W, H, range: rng });
  if (!built.inWin.length) return emptyBox("이 기간에는 기록이 없어요.<br><span class='t-cap'>기간을 ‘전체’로 바꿔 보세요.</span>");
  const { inner: g, nowX, X, m, iw, ih, span, inWin, ends } = built;
  const label = `${svcName(card.dataset.svc)} 사용률 추이. ` + ends.map(e => `${e.s.label} 현재 ${fmtPct(e.v)}퍼센트`).join(", ");
  plot.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">${g}<line class="xh" y1="${m.t}" y2="${m.t + ih}" stroke="var(--ink-2)" stroke-width="1" opacity="0"/><rect class="hit" x="${m.l}" y="${m.t}" width="${nowX - m.l}" height="${ih}" fill="transparent"/></svg><div class="tip" hidden></div>`;
  cur.textContent = "최고 " + fmtPct(Math.max(...ends.map(e => e.v))) + "%";
  const svg = plot.querySelector("svg"), tip = plot.querySelector(".tip"), xh = svg.querySelector(".xh");
  const hit = svg.querySelector(".hit");
  const hideTip = () => { tip.hidden = true; xh.setAttribute("opacity", 0); };
  const move = ev => {
    const bb = svg.getBoundingClientRect(), px = (ev.clientX - bb.left) * (W / bb.width);
    if (px < m.l || px > nowX) return hideTip();
    const tt = t0 + (px - m.l) / iw * span;
    const rowsAt = inWin.map(({ s, rows }) => { let best = null, bd = 1e15; rows.forEach(r => { const dd = Math.abs(tms(r.ts) - tt); if (dd < bd) { bd = dd; best = r; } }); return bd < 3 * HR ? { s, r:best } : null; }).filter(Boolean);
    if (!rowsAt.length) return hideTip();
    const tx = X(tms(rowsAt[0].r.ts)); xh.setAttribute("x1", tx); xh.setAttribute("x2", tx); xh.setAttribute("opacity", .5);
    tip.hidden = false; tip.innerHTML = `<b>${esc(fmtDT(tms(rowsAt[0].r.ts)))}</b>` + rowsAt.map(o => `<div><span><i style="background:${o.s.color}"></i>${esc(o.s.label)}</span><span class="num">${fmtPct(num(o.r.value))}%</span></div>`).join("");
    const tw = tip.offsetWidth || 190; tip.style.left = Math.max(0, Math.min(W - tw, tx + 12)) + "px"; tip.style.top = "8px";
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", hideTip);
}

/* ---------- 표 ---------- */
function filteredRows() {
  const s = $("fService").value, m = $("fMetric").value, t = $("fQ").value.trim().toLowerCase();
  return RECORDS.filter(r => (!s || r.service === s) && (!m || r.metric === m) && (!t || [r.service, svcName(r.service), r.metric, metricLabel(r.metric), r.note, r.source, r.value].join(" ").toLowerCase().includes(t)));
}
function fillSelect(el, vals, label, nameFn) {
  const sig = vals.join("\u0001"); if (el.__sig === sig) return; el.__sig = sig;
  const cur = el.value;
  el.innerHTML = `<option value="">${label}</option>` + vals.map(v => `<option value="${esc(v)}">${esc(nameFn(v))}</option>`).join("");
  el.value = vals.includes(cur) ? cur : "";
}
function rowKey(r) { return [r.ts, r.service, r.metric, r.note, r.value, r.reset_at].join("|"); }
function renderTable() {
  const tb = $("tbody"), more = $("more"), count = $("count");
  if (!loaded) {
    count.textContent = "";
    if (setMode("tbody", "skel")) tb.innerHTML = Array.from({ length:6 }, () => `<tr><td colspan="7"><div class="skel" style="height:16px"></div></td></tr>`).join("");
    more.hidden = true; return;
  }
  fillSelect($("fService"), [...new Set(RECORDS.map(r => r.service).filter(Boolean))].sort(), "모든 서비스", svcName);
  fillSelect($("fMetric"), [...new Set(RECORDS.map(r => r.metric).filter(Boolean))].sort(), "모든 지표", metricLabel);
  const rows = filteredRows();
  const scope = hasMoreRecords ? `최근 ${serverLimit.toLocaleString("ko-KR")}건${extraLatest ? ` + 시리즈별 최신 ${extraLatest}건` : ""}` : "";
  count.textContent = `${rows.length.toLocaleString("ko-KR")}건${scope ? ` (${scope})` : ""}`;
  if (!rows.length) {
    setMode("tbody", "none"); more.hidden = true;
    setHTML(tb, `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:32px">조건에 맞는 기록이 없어요. <button class="btn" type="button" data-act="clear" style="margin-left:8px">필터 지우기</button></td></tr>`);
    return;
  }
  if (setMode("tbody", "data")) tb.__h = null;
  morphList(tb, rows.slice(0, shown).map(r => { const n = num(r.value), pct = isPctRec(r), st = pct ? statusOf(n) : "na", ts = tms(r.ts), rs = tms(r.reset_at);
    return { key:rowKey(r), html:`<tr>
      <td class="c-time" data-label="시각" title="${ts !== null ? esc(fmtFull(ts)) : ""}">${ts !== null ? esc(fmtDT(ts)) : "—"}</td>
      <td class="c-svc" data-label="서비스">${esc(svcName(r.service))}</td>
      <td class="c-kind" data-label="지표">${esc(metricLabel(r.metric))}</td>
      <td class="c-val num" data-label="사용률" data-s="${st}">${esc(fmtVal(r))}${pct && Number.isFinite(n) ? `<span class="mini" aria-hidden="true"><i style="width:${Math.max(0, Math.min(100, n))}%"></i></span>` : ""}</td>
      <td class="c-reset" data-label="초기화" title="${rs !== null ? esc(fmtFull(rs)) : ""}">${rs !== null ? esc(fmtDT(rs)) : "—"}</td>
      <td class="c-note" data-label="메모">${esc(r.note || "")}</td>
      <td class="c-src" data-label="출처">${esc(r.source || "")}${r.raw_file ? `<a class="raw-link" href="#s-raw" data-raw="${esc(r.raw_file)}">원본</a>` : ""}</td>
    </tr>` }; }));
  more.hidden = rows.length <= shown;
  $("btnMore").textContent = `더 보기 (${Math.min(50, rows.length - shown)}건)`;
}

/* ---------- 원본 (접힘 · 잠금 · 파일 1개씩 · 열 때만 본문 요청) ---------- */
let rawSeq = 0;
function showRawLocked() {
  rawAuthed = false; rawLocked = true; RAW_LIST = []; rawListOk = true;
  const pre = $("rawPre"); if (pre) { pre.textContent = ""; pre.dataset.ver = ""; }
}
function renderRawList() {
  if (!rawLoaded) return;
  const det = $("rawDetails"), hint = $("rawHint"), sel = $("rawSel");
  const form = $("rawLockForm"), unlocked = $("rawUnlocked");
  if (rawLocked && !rawAuthed) {
    hint.textContent = "잠김";
    form.hidden = false; unlocked.hidden = true;
    return;
  }
  form.hidden = true; unlocked.hidden = false;
  if (!rawListOk && !RAW_LIST.length) { hint.textContent = "목록을 불러오지 못했어요"; $("rawEmpty").hidden = false; $("rawEmpty").textContent = "원본 파일 목록을 불러오지 못했어요. 잠시 뒤 자동으로 다시 시도합니다."; $("rawMain").hidden = true; return; }
  $("rawEmpty").textContent = "원본 파일이 아직 없어요. 로깅 봇이 raw/ 에 파일을 두면 여기에 나타납니다.";
  hint.textContent = RAW_LIST.length ? `${RAW_LIST.length}개 · ${det.open ? "보는 중" : "펼쳐서 보기"}` : "없음";
  $("rawEmpty").hidden = RAW_LIST.length > 0; $("rawMain").hidden = !RAW_LIST.length;
  if (!RAW_LIST.length) return;
  const sig = RAW_LIST.map(f => f.name).join("\u0001");
  if (sel.__sig !== sig) { const cur = sel.value || pendingRaw; sel.__sig = sig;
    sel.innerHTML = RAW_LIST.map(f => `<option value="${esc(f.name)}">${esc(f.name)}</option>`).join("");
    sel.value = RAW_LIST.some(f => f.name === cur) ? cur : RAW_LIST[0].name; }
  if (det.open) loadRaw(false);
}
async function fetchRawList() {
  const res = await fetch("/api/raw", { cache:"no-store", credentials:"same-origin" });
  if (res.status === 401) { showRawLocked(); return "locked"; }
  if (!res.ok) throw new Error(String(res.status));
  const d = await res.json();
  rawAuthed = true; rawLocked = false; RAW_LIST = d.files || []; rawListOk = true;
  return "ok";
}
async function loadRaw(force) {
  if (!rawAuthed) return;
  const sel = $("rawSel"), pre = $("rawPre"), meta = $("rawMeta"), err = $("rawErr");
  const f = RAW_LIST.find(x => x.name === sel.value); if (!f) return;
  const ver = `${f.name}|${f.mtime}|${f.size}`;
  if (!force && pre.dataset.ver === ver) return;
  const my = ++rawSeq;
  try {
    const res = await fetch("/api/raw/file?name=" + encodeURIComponent(f.name), { cache:"no-store", credentials:"same-origin" });
    if (res.status === 401) { showRawLocked(); renderRawList(); return; }
    if (!res.ok) throw new Error(res.status);
    const d = await res.json(); if (my !== rawSeq) return;
    const top = pre.scrollTop, left = pre.scrollLeft;
    if (pre.textContent !== d.text) pre.textContent = d.text;
    pre.scrollTop = top; pre.scrollLeft = left;
    pre.dataset.ver = ver; err.hidden = true;
    meta.textContent = `${fmtFull(Date.parse(d.mtime))} · ${Number(d.size).toLocaleString("ko-KR")}바이트${d.truncated ? " · 앞부분 64KB만 표시" : ""}`;
  } catch (e) { if (my !== rawSeq) return; err.hidden = false; err.textContent = `원본 파일을 불러오지 못했어요. 수집 슬롯 기준으로 다시 확인합니다.`; }
}

/* ---------- 불러오기 ---------- */
async function getJSON(url) { const res = await fetch(url, { cache:"no-store", credentials:"same-origin" }); if (!res.ok) throw new Error(String(res.status)); return res.json(); }
async function loadTrends() {
  const range = effRange();
  let entry = trendCache.get(range);
  if (!entry) { entry = { range, version:0, fetchedAt:0 }; trendCache.set(range, entry); }
  if (entry.loading || Date.now() - entry.fetchedAt < 20e3) { renderTrends(); return; }
  entry.loading = true; entry.error = false;
  renderTrends();
  try {
    const data = await getJSON("/api/trends?range=" + range);
    if (data.range !== range || !Array.isArray(data.records)) throw new Error("추이 응답 형식");
    entry.series = buildSeries(data.records);
    entry.minutes = data.bucket_minutes; entry.truncated = data.truncated; entry.count = data.count;
    entry.fetchedAt = Date.now(); entry.version++;
  } catch (e) { entry.error = true; }
  finally {
    entry.loading = false;
    if (range === effRange()) renderTrends();
  }
}
let loading = false;
async function load() {
  const rangeAtStart = loaded ? (RECORDS.length ? effRange() : null) : userRange;
  if (rangeAtStart) loadTrends();
  if (loading) return; loading = true;
  const wantRaw = rawAuthed || !rawLoaded;
  const tasks = [getJSON("/api/records?latest=1&total=0")];
  if (wantRaw) tasks.push(fetchRawList().catch(() => "error"));
  const results = await Promise.allSettled(tasks);
  loading = false;
  const rec = results[0];
  if (rec.status === "fulfilled") {
    const seen = new Set(), nextRecords = [], latest = new Map();
    const recent = rec.value.records || [];
    // latest도 최신순이다. 최근 페이지와 겹치는 행을 빼면 뒤에는 더 오래된 행만 남는다.
    extraLatest = 0;
    for (const [i, r] of recent.concat(rec.value.latest || []).entries()) {
      const k = rowKey(r);
      if (seen.has(k)) continue;
      if (i >= recent.length) extraLatest++;
      seen.add(k);
      nextRecords.push(r);
      const ts = tms(r.ts);
      if (ts !== null && ts > (latest.get(r.service) ?? -Infinity)) latest.set(r.service, ts);
    }
    RECORDS = nextRecords;
    latestByService = latest;
    skipped = rec.value.skipped || 0; hasMoreRecords = rec.value.has_more; serverLimit = rec.value.limit || 0;
    loaded = true; fetchFailed = false; lastOkAt = Date.now();
    const bar = $("rbar"); bar.classList.remove("run"); void bar.offsetWidth; bar.classList.add("run");
  } else fetchFailed = true;
  if (wantRaw) {
    rawLoaded = true;
    const raw = results[1];
    if (raw.status === "fulfilled" && raw.value === "error") rawListOk = false;
    else if (raw.status === "rejected" && !rawLocked) rawListOk = false;
  }
  renderAll();
  if ((!loaded || RECORDS.length) && effRange() !== rangeAtStart) loadTrends();
}
function renderAll() {
  const now = Date.now();
  renderCollection(now); renderLimits(now); renderCalendar(now);
  const empty = loaded && !RECORDS.length;
  $("s-trends").hidden = empty; $("s-records").hidden = empty; $("s-calendar").hidden = empty;
  if (!empty) { renderTrends(); renderTable(); }
  renderRawList();
  const foot = [`5분 수집 슬롯 기준 자동 새로고침`];
  if (loaded) foot.unshift(`기록 ${RECORDS.length.toLocaleString("ko-KR")}건`);
  if (skipped) foot.push(`형식이 잘못된 줄 ${skipped}개를 건너뛰었어요`);
  const html = foot.map(t => `<span>${esc(t)}</span>`).join("") + `<span><a href="/api/records">API</a></span>`;
  setHTML($("foot"), html);
}
function tick() {   // 30초마다 지연 여부만 다시 평가한다 (서버 요청 없음)
  if (document.hidden) return;
  const now = Date.now();
  renderCollection(now);
  if (loaded && RECORDS.length) renderLimits(now);
  renderCalendar(now);
}

/* ---------- 테마 ---------- */
const THEMES = ["auto", "light", "dark"], T_LABEL = { auto:"자동", light:"라이트", dark:"다크" };
const T_ICON = {
  auto:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 4v16"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none"/></svg>',
  light:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4L7 17M17 7l1.4-1.4"/></svg>',
  dark:'<svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>' };
function applyTheme(t, persist) {
  const r = document.documentElement; if (t === "auto") r.removeAttribute("data-theme"); else r.setAttribute("data-theme", t);
  const b = $("btnTheme"); b.innerHTML = T_ICON[t]; b.setAttribute("aria-label", `테마: ${T_LABEL[t]}`); b.title = `테마: ${T_LABEL[t]} (눌러서 변경)`; b.dataset.t = t;
  if (persist) try { if (t === "auto") localStorage.removeItem("tu-theme"); else localStorage.setItem("tu-theme", t); } catch (e) {}
}
$("btnTheme").onclick = e => applyTheme(THEMES[(THEMES.indexOf(e.currentTarget.dataset.t) + 1) % 3], true);
applyTheme((() => { try { const t = localStorage.getItem("tu-theme"); return t === "light" || t === "dark" ? t : "auto"; } catch (e) { return "auto"; } })());

/* ---------- 이벤트 ---------- */
function syncView() { document.querySelectorAll("[data-view]").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.view === (LF.v === "list" ? "list" : "card")))); }
document.querySelectorAll("[data-view]").forEach(b => b.onclick = () => { LF.v = b.dataset.view; saveLF(); renderLimits(); });
document.querySelectorAll(".msel").forEach(el => el.addEventListener("change", ev => {
  const i = ev.target, k = el.dataset.k || "k", isCal = el.dataset.st === "cal", st = isCal ? CF : LF;
  if (i.dataset.all) st[k] = [];
  else {
    if (i.dataset.svc) el.querySelectorAll("input[value]").forEach(x => { if (x.dataset.s === i.dataset.svc) x.checked = i.checked; });
    st[k] = [...el.querySelectorAll("input[value]:checked")].map(x => x.value);
  }
  if (isCal) { saveCF(); renderCalendar(); } else { saveLF(); renderLimits(); }
}));
document.addEventListener("click", ev => document.querySelectorAll(".msel[open]").forEach(el => { if (!el.contains(ev.target)) el.open = false; }));
document.addEventListener("keydown", ev => { if (ev.key === "Escape") document.querySelectorAll(".msel[open]").forEach(el => { el.open = false; el.querySelector("summary").focus(); }); });
function syncSort() { document.querySelectorAll("[data-sort]").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.sort === sortMode))); }
syncSort(); syncView();
document.querySelectorAll("[data-sort]").forEach(b => b.onclick = () => { sortMode = b.dataset.sort; LF.o = sortMode; saveLF(); syncSort(); renderLimits(); });
document.querySelectorAll("[data-range]").forEach(b => b.onclick = () => { userRange = LF.r = b.dataset.range; saveLF(); loadTrends(); });
["fService", "fMetric", "fQ"].forEach(id => $(id).addEventListener("input", () => { shown = pageSize(); renderTable(); }));
$("btnMore").onclick = () => { shown += 50; renderTable(); };
$("btnRefresh").onclick = () => load();
$("charts").addEventListener("click", ev => {
  const b = ev.target.closest(".lg"); if (!b) return;
  const key = b.dataset.key;
  seriesVisible[key] = !seriesVisible[key]; b.setAttribute("aria-pressed", String(seriesVisible[key]));
  renderTrends();
});
document.addEventListener("click", ev => {
  const a = ev.target.closest("[data-act]");
  if (a) { if (a.dataset.act === "retry") load(); if (a.dataset.act === "clear") { $("fService").value = $("fMetric").value = $("fQ").value = ""; shown = pageSize(); renderTable(); } return; }
  const l = ev.target.closest("[data-raw]");
  if (l) {
    ev.preventDefault();
    pendingRaw = l.dataset.raw || "";
    const det = $("rawDetails"); det.open = true;
    if (rawAuthed) {
      const sel = $("rawSel");
      if (sel && RAW_LIST.some(f => f.name === pendingRaw)) sel.value = pendingRaw;
      loadRaw(true);
    } else {
      renderRawList();
      const pw = $("rawPw"); if (pw) pw.focus();
    }
  }
});
$("rawDetails").addEventListener("toggle", () => {
  renderRawList();
  if ($("rawDetails").open && rawLocked && !rawAuthed) { const pw = $("rawPw"); if (pw) pw.focus(); }
});
$("rawSel").addEventListener("change", () => { $("rawPre").dataset.ver = ""; loadRaw(true); });
$("rawWrap").addEventListener("change", e => $("rawPre").classList.toggle("wrapped", e.target.checked));
$("rawLockForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  const err = $("rawLockErr"), pw = $("rawPw");
  err.hidden = true;
  try {
    const res = await fetch("/api/raw/login", {
      method:"POST", credentials:"same-origin",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ password: pw.value })
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) {
      err.hidden = false;
      err.textContent = d.error || (res.status === 429 ? "시도가 너무 많아요. 잠시 후 다시 시도해 주세요." : "비밀번호가 올바르지 않아요.");
      pw.focus(); pw.select();
      return;
    }
    pw.value = "";
    rawAuthed = true; rawLocked = false;
    const st = await fetchRawList().catch(() => "error");
    if (st === "error") rawListOk = false;
    if (pendingRaw && $("rawSel") && RAW_LIST.some(f => f.name === pendingRaw)) $("rawSel").value = pendingRaw;
    renderAll();
    if ($("rawDetails").open) loadRaw(true);
  } catch (e) {
    err.hidden = false; err.textContent = "로그인에 실패했어요. 잠시 후 다시 시도해 주세요.";
  }
});
$("rawLogout").addEventListener("click", async ev => {
  ev.preventDefault(); ev.stopPropagation();
  try { await fetch("/api/raw/logout", { method:"POST", credentials:"same-origin" }); } catch (e) {}
  showRawLocked(); pendingRaw = "";
  $("rawPw").value = ""; $("rawLockErr").hidden = true;
  renderAll();
});
let rt, lastW = 0;
new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => { const w = $("main").clientWidth; if (w === lastW) return; lastW = w;
  if (displayedTrend) renderTrends(); }, 80); }).observe($("main"));
document.addEventListener("visibilitychange", () => {
  document.documentElement.dataset.hidden = document.hidden ? "1" : "0";
  if (!document.hidden) { load(); scheduleSlotLoad(); }
});
$("rbar").style.setProperty("--refresh-period", `${REFRESH_MS}ms`);
/* ---------- 슬롯 기반 자동 새로고침 (고정 폴링 없음) ---------- */
let slotTimer = null, retryTimer = null, slotBaseline = null, slotRetrySince = 0;
function latestTsNow() {
  let m = -Infinity;
  for (const ts of latestByService.values()) if (ts > m) m = ts;
  return m;
}
function nextLoadMs(now) {
  // 가장 최근 슬롯+45초가 아직 미래면 그때, 아니면 다음 슬롯+45초에 받는다.
  const SLOT = COLLECT_MS, OFF = 9 * 3600e3;
  const kst = now + OFF;
  let at = Math.floor(kst / SLOT) * SLOT + SLOT_LOAD_DELAY - OFF;
  if (at <= now) at += SLOT;
  return at;
}
function scheduleSlotLoad() {
  clearTimeout(slotTimer);
  const now = Date.now();
  slotTimer = setTimeout(onSlot, Math.max(0, nextLoadMs(now) - now));
}
async function onSlot() {
  if (document.hidden) return;
  clearTimeout(retryTimer);
  slotBaseline = latestTsNow();
  slotRetrySince = Date.now();
  await load();
  await slotMaybeRetry();
  scheduleSlotLoad();
}
async function slotMaybeRetry() {
  // 슬롯 뒤 새 기록이 없으면 15초 간격으로 재시도 (2분 상한).
  if (document.hidden) return;
  if (slotBaseline === null) return;
  if (latestTsNow() > slotBaseline) { slotBaseline = null; return; }
  if (Date.now() - slotRetrySince >= SLOT_RETRY_CAP) { slotBaseline = null; return; }
  clearTimeout(retryTimer);
  retryTimer = setTimeout(async () => {
    if (document.hidden) return;
    await load();
    await slotMaybeRetry();
  }, SLOT_RETRY_MS);
}
setInterval(tick, TICK_MS);
renderAll();
load();
scheduleSlotLoad();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "TokenUsageDashboard/2.0"

    @property
    def root(self) -> Path:
        return self.server.root  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        stamp = now_kst().strftime("%Y-%m-%d %H:%M:%S %z")
        sys.stderr.write(f"{stamp} {self.address_string()} {fmt % args}\n")

    def _send(
        self,
        code: int,
        body: bytes,
        content_type: str,
        cache: str = "no-store",
        cors: bool = True,
        cookies: list[str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        if cookies:
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict, cors: bool = True, cookies: list[str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", cors=cors, cookies=cookies)

    def _is_https(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if proto == "https":
            return True
        visitor = (self.headers.get("CF-Visitor") or "").lower()
        if "https" in visitor:
            return True
        return (self.headers.get("X-Forwarded-Ssl") or "").lower() == "on"

    def _set_cookie(self, value: str, max_age: int) -> str:
        parts = [
            f"{COOKIE_NAME}={value}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={max_age}",
        ]
        if self._is_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def _raw_authorized(self) -> bool:
        return validate_session(self._session_token())

    def _deny_raw(self) -> None:
        self._json(401, {"error": "unauthorized"}, cors=False)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/records":
            self._api_records(parse_qs(parsed.query, keep_blank_values=True))
            return
        if path == "/api/trends":
            period = parse_qs(parsed.query, keep_blank_values=True).get("range", ["7d"])[0]
            if period not in ("24h", "7d", "all"):
                self._json(400, {"error": "`range`는 24h, 7d, all 중 하나여야 해요."})
                return
            records, _ = load_records(self.root / "processed")
            self._json(200, trend_payload(records, period, now_kst()))
            return
        if path == "/api/raw":
            if not self._raw_authorized():
                self._deny_raw()
                return
            self._json(
                200,
                {"files": list_raw_files(self.root / "raw"), "generated_at": to_iso(now_kst())},
                cors=False,
            )
            return
        if path == "/api/raw/file":
            if not self._raw_authorized():
                self._deny_raw()
                return
            name = (parse_qs(parsed.query).get("name") or [""])[0]
            item = read_raw_file(self.root / "raw", name)
            if item is None:
                self._json(404, {"error": "not found"}, cors=False)
            else:
                self._json(200, item, cors=False)
            return
        if path in ("/health", "/api/health"):
            self._json(200, {"ok": True, "time": to_iso(now_kst())})
            return
        self._send(404, b"Not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/raw/login":
            self._api_raw_login()
            return
        if path == "/api/raw/logout":
            self._api_raw_logout()
            return
        self._send(404, b"Not found\n", "text/plain; charset=utf-8")

    def _read_body(self, max_len: int = LOGIN_BODY_MAX) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length < 0:
            return b""
        if length > max_len:
            return None
        return self.rfile.read(length) if length else b""

    def _parse_login_password(self, body: bytes) -> str:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/x-www-form-urlencoded":
            return (parse_qs(body.decode("utf-8", errors="replace")).get("password") or [""])[0]
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return ""
        if isinstance(payload, dict):
            return str(payload.get("password") or "")
        return ""

    def _api_raw_login(self) -> None:
        ip = peer_ip(self)
        if login_rate_limited(ip):
            self._json(
                429,
                {"error": "시도가 너무 많아요. 잠시 후 다시 시도해 주세요."},
                cors=False,
            )
            return
        body = self._read_body()
        if body is None:
            self._json(413, {"error": "요청이 너무 커요."}, cors=False)
            return
        password = self._parse_login_password(body)
        record = load_auth()
        if not password or not verify_password(password, record):
            record_login_fail(ip)
            self._json(401, {"error": "비밀번호가 올바르지 않아요."}, cors=False)
            return
        fp = fingerprint(record)
        token = create_session(fp)
        self._json(200, {"ok": True}, cors=False, cookies=[self._set_cookie(token, int(SESSION_TTL.total_seconds()))])

    def _api_raw_logout(self) -> None:
        drop_session(self._session_token())
        self._json(200, {"ok": True}, cors=False, cookies=[self._set_cookie("", 0)])

    def _api_records(self, query: dict) -> None:
        service = (query.get("service") or [""])[0].strip()
        metric = (query.get("metric") or [""])[0].strip()
        q = (query.get("q") or [""])[0].strip().lower()
        include_latest = query.get("latest", [""])[0] == "1"
        include_total = query.get("total", [""])[0] != "0"
        since = parse_ts(query["since"][0]) if "since" in query else None
        if "since" in query and since is None:
            self._json(400, {"error": "`since`는 ISO 8601 시각이어야 해요."})
            return
        since_kst = to_iso(since) if since else ""
        records, skipped = load_records(self.root / "processed")
        try:
            limit = int((query.get("limit") or [str(MAX_RECORDS_DEFAULT)])[0])
        except ValueError:
            limit = MAX_RECORDS_DEFAULT
        limit = max(1, min(limit, 20000))
        filtered, latest, seen = [], [], set()
        total = 0
        for rec in records:
            if rec["_ts"] < since_kst:
                break
            if include_latest:
                key = series_key(rec)
                is_latest = key not in seen
                seen.add(key)
            pub = public_record(rec)
            if service and str(pub.get("service") or "") != service:
                continue
            if metric and str(pub.get("metric") or "") != metric:
                continue
            if q:
                blob = " ".join(str(pub.get(k) or "") for k in ("service", "metric", "note", "source", "unit", "value")).lower()
                if q not in blob:
                    continue
            total += 1
            if len(filtered) < limit:
                filtered.append(pub)
            if include_latest and is_latest:
                latest.append(pub)
            if not include_total and not include_latest and total > limit:
                break
        self._json(
            200,
            {
                "records": filtered,
                "count": len(filtered),
                "total": total if include_total else None,
                "has_more": total > limit,
                **({"latest": latest} if include_latest else {}),
                "limit": limit,
                "skipped": skipped,
                "generated_at": to_iso(now_kst()),
                "timezone": "Asia/Seoul",
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Token usage dashboard web server")
    parser.add_argument("--root", default=str(env_root()), help="token-usage root directory")
    parser.add_argument("--port", type=int, default=env_port(), help="listen port (default 8787)")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "processed").mkdir(parents=True, exist_ok=True)
    (root / "raw").mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.root = root  # type: ignore[attr-defined]
    print(
        f"token-usage dashboard listening on http://{args.host}:{args.port}  root={root}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
