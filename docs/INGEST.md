# 토큰 사용 기록 보내기 / Posting token usage records

대시보드는 Google VM에서 돌아가고, 데이터 전송은 **https 전용**이에요. 개발(dev)은 `https://usage-dashboard-dev.velar-agent.duckdns.org`, 운영(prod)은 `https://usage-dashboard.velar-agent.duckdns.org`로 보내세요. plain http(`http://`)는 수집기가 거부하고, bare IP(`:80`)로 보낸 `/api/` 요청은 nginx가 403으로 거부해요.
The dashboard runs on a Google VM and data transfer is **https only**. Send to dev `https://usage-dashboard-dev.velar-agent.duckdns.org` or prod `https://usage-dashboard.velar-agent.duckdns.org`. Plain http is refused by the collector, and `/api/` requests over the bare IP (:80) are answered 403 by nginx.

- 주소 / URLs: dev `https://usage-dashboard-dev.velar-agent.duckdns.org/api/ingest` · prod `https://usage-dashboard.velar-agent.duckdns.org/api/ingest`
- 인증 / Auth: 헤더 `Authorization: Bearer <INGEST_KEY>`
- 키 파일 / Key files (box): dev `/workspace/token-usage/google-ingest-key.txt`, prod `/workspace/token-usage/google-ingest-key-prod.txt` (권한 600, 채팅·로그에 찍지 마세요 / never print or log it)
- 본문 / Body: `Content-Type: application/json` — 기록 객체 하나, 객체 배열, 또는 `{"records":[...]}`. **한 번에 최대 40건 / max 40 records per request**, 본문 256KB 이하.

## 필드 / Fields (FORMAT.md 와 같아요 / same as FORMAT.md)

| 필드 | 필수 | 규칙 / Rule |
| --- | --- | --- |
| `service` | 예/yes | 문자열 1~64자 / string, 1–64 chars |
| `metric` | 예/yes | 문자열 1~64자 (`weekly_used_pct`, `session_used_pct`, …) |
| `value` | 예/yes | **숫자** / number (문자열 `"14"` 은 거부 / strings rejected) |
| `ts` | 아니오/no | ISO 8601. `+09:00` 권장. 없으면 서버 현재 시각(KST). 시간대가 없으면 KST 로 봐요 |
| `unit` | 아니오 | 문자열 ≤20자 |
| `reset_at` | 아니오 | ISO 8601 |
| `note` | 아니오 | 문자열 ≤200자 |
| `source` | 아니오 | 문자열 ≤100자 |
| `reason` | 아니오 | 문자열 ≤100자. 사용량 없음 사유(예: `server_sent_no_usage`) |
| `hint` | 아니오 | 문자열 ≤200자. 사용량 없음에 대한 부가 설명 |
| 그 밖의 필드 | 아니오 | 그대로 보존돼요 (예: `raw_file`, `raw_source`). 밑줄(`_`)로 시작하는 이름은 안 돼요. 기록 하나 4KB 이하 |

`metric`이 `usage_unavailable`이면 `value`는 숫자 0이지만 사용률 0%가 아니에요. 서버가 사용량 스냅샷을 보내지 않았다는 관측입니다. SQLite `value` 칼럼이 NOT NULL이라 숫자는 필요해요.

중복 / Dedupe: `ts` + `service` + `metric` + `note` 가 같은 기록은 한 번만 저장돼요 (두 번째는 `duplicates` 로 세어지고 무시돼요). 같은 값을 다시 보내도 안전해요.
Records with the same `ts`+`service`+`metric`+`note` are stored once; re-sending is safe.

## 예 1: 한 건 / one record

```bash
curl -sS -X POST https://usage-dashboard-dev.velar-agent.duckdns.org/api/ingest \
  -H "Authorization: Bearer $(cat /workspace/token-usage/google-ingest-key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"ts":"2026-11-15T15:20:00+09:00","service":"grok","metric":"weekly_used_pct","value":7,"unit":"%","reset_at":"2026-11-17T15:42:00+09:00","note":"X Premium+","source":"token-usage-logging-bot"}'
```

## 예 2: 여러 건 / several records

```bash
curl -sS -X POST https://usage-dashboard-dev.velar-agent.duckdns.org/api/ingest \
  -H "Authorization: Bearer $(cat /workspace/token-usage/google-ingest-key.txt)" \
  -H "Content-Type: application/json" \
  -d '[{"service":"claude","metric":"session_used_pct","value":0,"unit":"%","note":"all models"},
       {"service":"claude","metric":"weekly_used_pct","value":14,"unit":"%","note":"all models","reset_at":"2026-11-17T19:59:59+09:00"}]'
```

## 예 3: Python

```python
import json, urllib.request
from datetime import datetime, timedelta, timezone
KST = timezone(timedelta(hours=9))
key = open("/workspace/token-usage/google-ingest-key.txt").read().strip()
recs = [{"ts": datetime.now(KST).isoformat(timespec="seconds"), "service": "codex",
         "metric": "weekly_used_pct", "value": 36, "unit": "%", "note": "ChatGPT prolite",
         "source": "token-usage-logging-bot"}]
req = urllib.request.Request("https://usage-dashboard-dev.velar-agent.duckdns.org/api/ingest", data=json.dumps(recs).encode(),
      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "User-Agent": "token-usage-logging-bot/1.0"}, method="POST")
# User-Agent 헤더를 꼭 지정하세요.
print(urllib.request.urlopen(req, timeout=30).read().decode())
```

## 응답 / Responses

- `200` `{"ok":true,"received":N,"inserted":X,"duplicates":Y}`
- `400` 잘못된 JSON 또는 필드 — 한 건이라도 잘못되면 **아무것도 저장하지 않고** `{"error":"…","details":[{"index":0,"error":"…"}]}` 로 이유를 알려 줘요 / nothing is stored if any record is invalid
- `401` 키가 없거나 틀림 / missing or wrong key
- `405` POST 가 아님 · `413` 너무 큼 / too large

## 원본(raw) 파일 올리기 (선택) / Uploading a raw dump (optional)

```bash
python3 - <<'PY' | curl -sS -X POST https://usage-dashboard-dev.velar-agent.duckdns.org/api/raw/upload \
  -H "Authorization: Bearer $(cat /workspace/token-usage/google-ingest-key.txt)" -H "Content-Type: application/json" --data-binary @-
import json; print(json.dumps({"name":"2026-11-15_1800_grok.jsonl","content":open("/workspace/token-usage/raw/2026-11-15_1800_grok.jsonl").read()}))
PY
```

`name`: 영문·숫자·`.`·`_`·`-` 200자 이하. `content`: 200,000자 이하. 같은 이름이면 덮어써요(`replaced:true`). 선택 `created_at`(ISO 8601). 대시보드에는 최신 8개만 보이고, 열람에는 비밀번호가 필요해요.
