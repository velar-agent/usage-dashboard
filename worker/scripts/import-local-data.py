#!/usr/bin/env python3
"""processed/*.jsonl 과 raw/ 파일을 배포된 Worker 로 올려요 (여러 번 실행해도 중복은 무시돼요).

  python3 import-local-data.py --url https://usage-dashboard.velar-agent.workers.dev [--ip 104.21.x.x] [--key-file /workspace/token-usage/ingest-key.txt]
"""
import argparse, json, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
BASE = Path("/workspace/token-usage")
ap = argparse.ArgumentParser()
ap.add_argument("--url", default="https://usage-dashboard.velar-agent.workers.dev")
ap.add_argument("--ip", help="DNS 를 건너뛰고 이 엣지 IP 로 접속 (curl --resolve)")
ap.add_argument("--key-file", default=str(BASE / "ingest-key.txt"))
ap.add_argument("--chunk", type=int, default=25)
a = ap.parse_args()
key = Path(a.key_file).read_text().strip()
host = a.url.split("//", 1)[1].split("/", 1)[0]


def post(path, payload):
    cmd = ["curl", "-sS", "-m", "60", "-X", "POST", a.url + path, "-H", f"Authorization: Bearer {key}",
           "-H", "Content-Type: application/json", "--data-binary", "@-", "-w", "\n%{http_code}"]
    if a.ip:
        cmd += ["--resolve", f"{host}:443:{a.ip}"]
    out = subprocess.run(cmd, input=json.dumps(payload, ensure_ascii=False).encode(), capture_output=True)
    if out.returncode: print("curl error:", out.stderr.decode()[:300], file=sys.stderr)
    out = out.stdout.decode()
    body, _, code = out.rpartition("\n")
    return int(code or 0), body


recs, skipped = [], 0
for f in sorted((BASE / "processed").glob("*.jsonl")):
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(o, dict):
            recs.append(o)
        else:
            skipped += 1
ins = dup = 0
for i in range(0, len(recs), a.chunk):
    code, body = post("/api/ingest", recs[i : i + a.chunk])
    if code != 200:
        sys.exit(f"ingest 실패 HTTP {code}: {body[:300]}")
    r = json.loads(body)
    ins += r["inserted"]; dup += r["duplicates"]
print(f"records: read={len(recs)} inserted={ins} duplicates={dup} skipped_lines={skipped}")

raw_ok = 0
for p in sorted((BASE / "raw").iterdir()):
    if not p.is_file() or p.name.startswith("."):
        continue
    mt = datetime.fromtimestamp(p.stat().st_mtime, KST).isoformat(timespec="seconds")
    code, body = post("/api/raw/upload", {"name": p.name, "content": p.read_text(encoding="utf-8", errors="replace"), "created_at": mt})
    if code != 200:
        sys.exit(f"raw 업로드 실패 {p.name} HTTP {code}: {body[:300]}")
    raw_ok += 1
print(f"raw files uploaded: {raw_ok}")
