#!/usr/bin/env python3
"""Cloudflare 연결(MCP execute) 또는 API로 Worker를 올릴 때 쓰는 코드(JS)를 만들어요.

사용법:
  python3 make-deploy-code.py > /tmp/deploy.js            # 운영(usage) 코드만 다시 올림 (시크릿은 그대로 유지)
  python3 make-deploy-code.py --name usage-dev > /tmp/deploy.js  # 개발 환경. D1은 wrangler.toml [env.dev]
  python3 make-deploy-code.py --name usage-dev --d1-id UUID > /tmp/deploy.js
  python3 make-deploy-code.py --secrets FILE > /tmp/deploy.js   # 시크릿까지 새로 설정 (처음 배포/교체)
FILE 은 {"INGEST_KEY":..,"SESSION_SECRET":..,"RAW_SALT":..,"RAW_HASH":..,"RAW_ITERATIONS":..} JSON.
출력에는 시크릿이 들어갈 수 있으니 화면에 찍거나 공유하지 말고 쓰고 나면 지우세요.
--name 이 usage 가 아닌데 --d1-id 도 없고 wrangler.toml 의 해당 env 에도 database_id 가 없으면, 운영 D1을 쓰지 않고 중단합니다.
"""
import argparse, json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser()
p.add_argument("--secrets")
p.add_argument("--d1-id", default=None)
p.add_argument("--name", default="usage")
a = p.parse_args()

def d1_id_from_toml(text, env=None):
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

d1_id = a.d1_id
if not d1_id:
    toml = (ROOT / "wrangler.toml").read_text()
    if a.name == "usage":
        d1_id = d1_id_from_toml(toml)
    else:
        env = "dev" if a.name.endswith("-dev") else a.name
        d1_id = d1_id_from_toml(toml, env)
        if not d1_id:
            raise SystemExit(
                f"--d1-id 가 없고 wrangler.toml 의 env.{env} 에도 database_id 가 없어요. "
                "운영 D1을 쓰지 않도록 중단합니다."
            )

bindings = [{"type": "d1", "name": "DB", "id": d1_id}]
meta = {
    "main_module": "index.js",
    "compatibility_date": "2026-11-01",
    "observability": {"enabled": True},
}
if a.secrets:
    sec = json.loads(Path(a.secrets).read_text())
    for k in ("INGEST_KEY", "SESSION_SECRET", "RAW_SALT", "RAW_HASH", "RAW_ITERATIONS"):
        bindings.append({"type": "secret_text", "name": k, "text": str(sec[k])})
else:
    meta["keep_bindings"] = ["secret_text"]
meta["bindings"] = bindings

modules = {n: (ROOT / "src" / n).read_text(encoding="utf-8") for n in ("index.js", "html.js")}
print(
    """async () => {
  const meta = %s;
  const modules = %s;
  const form = new FormData();
  form.append('metadata', new Blob([JSON.stringify(meta)], {type:'application/json'}));
  for (const [n, src] of Object.entries(modules)) form.append(n, new Blob([src], {type:'application/javascript+module'}), n);
  const r = await cloudflare.request({method:'PUT', path:`/accounts/${accountId}/workers/scripts/%s`, body: form});
  return {success: r.success, id: r.result && r.result.id, modified_on: r.result && r.result.modified_on, bindings: (r.result && r.result.bindings || []).map(b=>b.name+':'+b.type)};
}"""
    % (json.dumps(meta, ensure_ascii=False), json.dumps(modules, ensure_ascii=False), a.name)
)
