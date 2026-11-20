# 토큰 사용 기록 형식 / Token usage log contract

토큰 사용 로깅 봇이 남긴 기록을 웹 대시보드가 읽어 보여 주기 위한 공통 형식입니다.
이 파일을 로깅 봇과 수집 봇이 함께 따르면 됩니다.

This is the shared contract so the logging bot can write records the dashboard can display.
The viewer is tolerant: extra fields are kept, and missing fields are shown as empty rather than rejected.

## 위치 / Location

- 처리된 기록 (대시보드가 읽는 곳 / what the dashboard reads):
  `/workspace/token-usage/processed/YYYY-MM-DD.jsonl`
  - 날짜는 **KST (UTC+9)** 기준입니다. / Dates are in **KST (UTC+9)**.
  - 하루당 파일 하나, JSON Lines (한 줄에 기록 하나). / One file per day, JSON Lines (one JSON object per line).
- 원본 덤프 (선택, 있는 그대로 표시) / Optional raw dumps (shown as plain text):
  `/workspace/token-usage/raw/`
  - 파일 이름·내용은 자유입니다. 대시보드는 최근 파일의 본문을 그대로 보여 줍니다.
  - Filenames and contents are free-form. The dashboard shows the latest files as plain text.

빈 줄과 `#`으로 시작하는 줄은 무시합니다. 잘못된 JSON 줄은 건너뜁니다.
Blank lines and lines starting with `#` are ignored. Invalid JSON lines are skipped.

## 필드 / Fields

권장 필드입니다. 모두 선택이며, 없는 필드는 비워 두고 추가 필드는 그대로 보존합니다.
Recommended fields. All are optional; missing fields are tolerated and unknown fields are preserved.

| 필드 / Field | 형 / Type | 설명 / Description |
| --- | --- | --- |
| `ts` | string (ISO 8601) | 기록 시각. 가능하면 `+09:00` 오프셋을 붙입니다. / Record timestamp. Prefer an offset of `+09:00`. 예: `2026-11-15T15:20:00+09:00` |
| `service` | string | 서비스 이름. 예: `codex`, `grok`, `claude`, `gemini`, `muse` |
| `metric` | string | 지표 이름. 예: `weekly_used_pct`, `session_used_pct`, `weekly_remaining_pct`, `tokens_in`, `tokens_out`, `cost` |
| `value` | number | 측정값 / Numeric value |
| `unit` | string | 단위. 예: `%`, `tokens`, `USD` |
| `reset_at` | string (ISO 8601, 선택) | 한도 초기화 시각. `+09:00` 권장. / Optional limit reset time. Prefer `+09:00`. |
| `note` | string (선택) | 요금제명, 모델명 등 짧은 메모 / Optional short note (plan name, model, …) |
| `source` | string | 기록을 남긴 봇 이름 / Name of the bot that wrote the record. 예: `token-usage-logging-bot` |

참고 / Notes:

- 사용률은 **사용한 비율**(`weekly_used_pct`)로 통일하는 것을 권장합니다. Codex·Gemini 원본이 “남은 비율”이어도 로깅 시 `100 - 남은%`로 바꿔 적습니다.
  Prefer **used** percentages (`weekly_used_pct`). If a source reports remaining percent (Codex, Gemini), convert with `100 - remaining` before writing.
- Claude처럼 세션 한도와 주간 한도가 함께 있으면 각각 다른 `metric`으로 한 줄씩 적습니다 (`session_used_pct`, `weekly_used_pct`). 모델별 한도는 `note`에 모델명을 넣거나 `metric`을 구분해 적습니다.
  If a service has both session and weekly limits (e.g. Claude, Muse), write one line per metric.
- 같은 시각에 여러 지표를 남길 때는 줄을 여러 개 추가하면 됩니다. / Write one line per metric even if they share the same `ts`.

## 예제 한 줄 / Example line

```json
{"ts":"2026-11-15T15:20:00+09:00","service":"grok","metric":"weekly_used_pct","value":7,"unit":"%","reset_at":"2026-11-17T15:42:00+09:00","note":"X Premium+","source":"token-usage-logging-bot"}
```

## 기록 추가 한 줄 명령 / Example one-liner to append a record

Python을 쓰면 시각과 JSON 이스케이프를 안전하게 처리할 수 있습니다.
A Python one-liner is the safest way to format the timestamp and escape JSON:

```bash
python3 -c '
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
KST = timezone(timedelta(hours=9))
now = datetime.now(KST)
rec = {
    "ts": now.isoformat(timespec="seconds"),
    "service": "grok",
    "metric": "weekly_used_pct",
    "value": 7,
    "unit": "%",
    "reset_at": "2026-11-17T15:42:00+09:00",
    "note": "X Premium+",
    "source": "token-usage-logging-bot",
}
path = Path("/workspace/token-usage/processed") / f"{now:%Y-%m-%d}.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
'
```

같은 형식으로 `codex` / `claude` / `gemini` / `muse` 줄도 `processed/YYYY-MM-DD.jsonl`에 이어 쓰면 됩니다. 수집 주기는 5분이며, 대시보드 탭을 보고 있는 동안 30초마다 새 기록을 확인합니다.
Append records for other services in the same format. Collection runs every 5 minutes. The dashboard checks for new records every 30 seconds while the tab is visible.
