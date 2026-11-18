# usage-dashboard-google-dev

Ubuntu 24.04에서 Python 3.12 표준 라이브러리로 실행하는 사용량 대시보드예요. 한 프로세스가 HTTP 요청을 스레드로 처리하고 SQLite(WAL)에 저장해요. 시작할 때 저장소의 `worker/schema.sql`을 적용하고 `worker/src/html.js`에서 화면을 읽어요.

## VM에 설치

`collector` 계정으로 실행하세요. 설치 스크립트가 데이터 디렉터리와 초기 자격 증명을 만들고, sudo로 systemd 유닛을 설치해요.

```bash
cd /workspace/usage-dashboard/google
./install.sh
systemctl status usage-dashboard-google-dev
curl -fsS http://127.0.0.1:8787/health
```

설치 스크립트를 다시 실행해도 기존 키를 유지해요. `/home/collector/.local/bin/cloudflared`가 실행 가능하면 터널도 시작해요. Quick Tunnel의 HTTPS 주소는 터널 로그에 나오고 재시작하면 바뀔 수 있어요.

```bash
sudo systemctl restart usage-dashboard-google-dev
journalctl -u usage-dashboard-google-dev -n 50 --no-pager
journalctl -u cloudflared-usage-dashboard-google-dev -n 50 --no-pager
```

## 환경 변수

기본 설정 파일은 `~/.config/usage-dashboard-google-dev/env`예요. 원본 열람 비밀번호는 같은 디렉터리의 `raw-password.txt`에서 확인해요. 디렉터리 권한은 700이고 두 파일의 권한은 600이에요.

| 변수 | 용도와 기본값 |
| --- | --- |
| `INGEST_KEY` | 수집·원본 업로드의 Bearer 키. 시작에 필요해요. |
| `SESSION_SECRET` | 원본 세션의 HMAC-SHA256 서명 키. 시작에 필요해요. |
| `RAW_SALT`, `RAW_HASH`, `RAW_ITERATIONS` | 원본 열람용 PBKDF2-SHA256 솔트와 해시(hex), 반복 횟수. 생성기는 10,000회를 써요. |
| `DB_PATH` | 기본값은 저장소의 `google/data/usage.sqlite3` 절대 경로예요. |
| `HOST`, `PORT` | 기본값은 `127.0.0.1`, `8787`이에요. |

로그인 시도 제한은 `CF-Connecting-IP`와 `X-Forwarded-For` 헤더를 신뢰하므로 서버를 터널이나 리버스 프록시 뒤에서 `127.0.0.1`에만 바인딩하고, 공개 인터페이스에는 절대 직접 바인딩하지 마세요.

`python3 gen_env.py [전용디렉터리/env]`로 직접 생성할 수도 있어요. `--force`는 수집 키, 세션 키, 원본 비밀번호를 모두 교체해요. 교체한 뒤에는 수집기에 새 키를 설정하고 앱을 재시작하세요. DB를 다른 디렉터리로 옮기면 앱 유닛의 `ReadWritePaths`에 그 경로를 지정하세요. 수신 포트를 바꾸면 터널 유닛의 `--url`에도 반영하세요.

## HTTP 동작

- 수집·업로드 형식은 [기록 전송 안내](../docs/INGEST.md)를 따르고 요청 주소만 이 서버로 바꿔요. 수집은 한 번에 최대 40건, 본문은 256 KiB, 정규화한 기록 하나는 4096바이트예요.
- `/api/records`는 `service`, `metric`, `q`, `limit`에 `since`(ISO 8601를 `ts_kst >=`로 바꿔 걸어요), `latest=1`(시리즈별 최신), `total=0`(총수 생략)도 읽어요. `since`가 ISO 8601이 아니면 400을 돌려줘요. `/api/trends?range=24h|7d|all`(기본 7d)은 버킷 마지막·초기화 양끝·시작/최신만 남겨 최대 2만 건까지 돌려주고 잘못된 `range`는 400으로 돌려줘요. Worker의 20초 엣지 캐시를 프로세스 안에 두므로 같은 URL에는 최대 20초 전 결과가 보일 수 있어요. 브라우저 응답은 `Cache-Control: no-store`예요.
- 원본 세션은 12시간 동안 유효해요. Worker와 같이 HTTP와 HTTPS 모두 쿠키에 `Secure; HttpOnly; SameSite=Strict`를 붙여요. 브라우저에서 원본을 열 때는 터널의 HTTPS 주소를 사용하세요.
- 로그인 제한은 IP당 60초에 비밀번호 실패 5회예요. IP는 `CF-Connecting-IP`, `X-Forwarded-For`의 첫 값, TCP 접속 주소 순서로 골라 해시로 저장해요. 서버가 전달 헤더를 신뢰하므로 외부 요청은 이 헤더를 설정하는 프록시를 통해 받으세요.

## D1 기록 가져오기

D1 읽기 권한이 있는 컴퓨터의 비공개 디렉터리에서 두 표의 모든 열을 내보내세요. 아래 예시는 개발 DB `usage-dev`를 읽어요.

```bash
umask 077
wrangler d1 execute usage-dev --remote --json --command 'SELECT * FROM records ORDER BY id' > records.json
wrangler d1 execute usage-dev --remote --json --command 'SELECT * FROM raw_files ORDER BY name' > raw_files.json
python3 - <<'PY'
import json
from pathlib import Path

tables = {}
for name in ("records", "raw_files"):
    result = json.loads(Path(name + ".json").read_text(encoding="utf-8"))
    tables[name] = result[0]["results"]
with Path("export.json").open("w", encoding="utf-8") as out:
    json.dump(tables, out, ensure_ascii=False)
PY
```

완성한 `{"records":[...],"raw_files":[...]}` 파일을 VM의 비공개 경로에 놓고 `collector` 계정으로 실행하세요.

```bash
cd /workspace/usage-dashboard/google
python3 import_d1_json.py ~/export.json
```

경로를 바꾼 DB는 `--db /경로/usage.sqlite3`로 지정해요. 가져오기는 파일을 한 행씩 읽어 원래 id와 모든 열을 보존하고 표별 추가·중복 건수만 출력해요. 같은 `dedupe_key`나 파일 이름은 건너뛰어요. 다른 기록이 같은 id를 쓰면 전체 가져오기를 롤백해요.

로컬 검사는 `google/`에서 `python3 -m unittest -v`로 실행해요. 임시 DB와 임의의 루프백 포트를 사용해요.

## 운영(prod) 인스턴스와 자정 승격

prod는 dev와 같은 코드·스키마를 쓰지만 worktree와 DB·포트를 분리해 자정에 승격해요. 시간대는 `Asia/Seoul`이에요.

| 항목 | dev | prod |
| --- | --- | --- |
| 포트 | `127.0.0.1:8787` | `127.0.0.1:8788` |
| 작업 디렉터리 | `/workspace/usage-dashboard/google` | `/workspace/usage-dashboard-prod/google` (dev 저장소에서 `git worktree add --detach`로 분리, 마지막 승격 커밋 고정) |
| 데이터 디렉터리(DB) | `/workspace/usage-dashboard/google/data` | `/workspace/usage-dashboard-prod-data` (DB는 `/workspace/usage-dashboard-prod-data/usage.sqlite3`, `DB_PATH`로 지정해 worktree 밖) |
| env | `~/.config/usage-dashboard-google-dev/env` | `~/.config/usage-dashboard-google-prod/env` (`DB_PATH`, `PORT=8788` 고정) |
| systemd 유닛 | `usage-dashboard-google-dev.service` | `usage-dashboard-google-prod.service` (`WorkingDirectory=/workspace/usage-dashboard-prod/google`, `ReadWritePaths=/workspace/usage-dashboard-prod-data`) |
| 수집 키 파일 | `/workspace/token-usage/google-ingest-key.txt` | `/workspace/token-usage/google-ingest-key-prod.txt` (0600) |

`ProtectSystem=strict`라도 `ReadWritePaths=/workspace/usage-dashboard-prod-data` 안에 WAL(`-wal`, `-shm`)이 생겨요. DB 파일과 WAL은 같은 디렉터리 안에서만 움직이므로 이중 쓰기 문제가 없습니다.

설치(`collector`에서 한 번):

```bash
cd /workspace/usage-dashboard/google
./install-prod.sh
systemctl status usage-dashboard-google-prod
curl -fsS http://127.0.0.1:8788/health
```

`install-prod.sh`는 idempotent해요. worktree가 없으면 `git -C /workspace/usage-dashboard worktree add --detach /workspace/usage-dashboard-prod HEAD`로 만들고, 데이터 디렉터리(0700)를 `install -d -m 0700`으로 만들어요. prod env는 `gen_env.py --db-path /workspace/usage-dashboard-prod-data/usage.sqlite3 --port 8788`로 없으면 생성하고, 있으면 `DB_PATH`·`PORT`만 맞춰요(키·비밀번호는 유지). prod DB가 아직 없으면 dev DB를 `sqlite3` backup API로 씨드해요(서비스 시작 전). 유닛은 prod worktree 복사본이 아니라 dev 저장소 `google/usage-dashboard-google-prod.service`에서 설치해 `daemon-reload`·`enable --now` 해요. prod `INGEST_KEY`를 `/workspace/token-usage/google-ingest-key-prod.txt`(0600)로 복사해요.

자정 승격:

```bash
# cron: /etc/cron.d/usage-dashboard-nightly (CRON_TZ=Asia/Seoul)
0 0 * * * collector /workspace/usage-dashboard/google/promote-prod.sh
google/promote-prod.sh [--dry-run] [--force]
```

`promote-prod.sh`는 옛 `worker/scripts/nightly-promote.sh`와 비슷한 동작을 해요(locks `flock`, 로그 `~/.local/state/usage-deploy/YYYY-MM-DD.log`, `last-run.json`, 60일 로그 정리, 종료 코드). Cloudflare 로직은 없어요.

- dev 저장소 `/workspace/usage-dashboard`에서 브랜치 `main`, 추적 트리 깨끗함, `git pull --ff-only`(dry-run이면 `git fetch`만), `HEAD == origin/main`을 확인해요.
- precheck: `google/`에서 `python3 -m unittest`와 `python3 -m py_compile *.py`를 통과해야 올려요.
- prod HEAD가 dev HEAD와 같으면(`--force`가 아니면) `no change`로 0으로 끝나고 건너뛰어요.
- 다르면 이전 prod 커밋을 기록하고, prod DB를 `sqlite3` backup API로 `/workspace/usage-dashboard-prod-data/backups/usage-<timestamp>.sqlite3`에 백업해요(최근 7개 유지). prod worktree에서 `git checkout --detach <dev HEAD>`로 이동해요(`worker/src/html.js` 같은 추적 파일은 자동 갱신). 유닛 파일은 승격에서 건드리지 않아요. `sudo systemctl restart usage-dashboard-google-prod`로 재시작하고 `http://127.0.0.1:8788/health`(최대 ~20초 재시도)와 `GET / 200`을 확인해요. 실패하면 이전 커밋으로 `checkout`·재시작·재점검하고 `5`(롤백됨) 또는 `6`(롤백 실패, 수동 확인)으로 끝나고 로그에 남겨요. 서버가 시작할 때 `schema.sql`을 additive로 적용하므로 롤백해도 DB는 그대로 둬요(백업은 수동 복구용).
- `--dry-run`은 prod worktree/재시작/백업을 건너뛰고 `git fetch`+precheck+변경 보고만 해요.

nginx:

```bash
sudo install -m 644 google/nginx-usage-dashboard.conf /etc/nginx/sites-available/usage-dashboard
sudo ln -sf /etc/nginx/sites-available/usage-dashboard /etc/nginx/sites-enabled/usage-dashboard
sudo nginx -t && sudo systemctl reload nginx
```

`google/nginx-usage-dashboard.conf`는 같은 nginx에 다른 서비스가 추가될 수 있어 usage-dashboard 서버만 담아요.

```nginx
# nginx-usage-dashboard.conf
# usage-dashboard.velar-agent.duckdns.org -> 8788(prod), usage-dashboard-dev... -> 8787(dev), default_server(bare IP) -> 8787(dev)
```

프록시 헤더(`Host`, `X-Forwarded-For`, `X-Forwarded-Proto http`)는 기존 설정과 같아요.

수집기(`token-usage-collector`)는 두 곳에 모두 보내요. `bin/log_raw.py`가 `INGEST_URL`(dev, `http://127.0.0.1:8787/api/ingest`, 키 `/workspace/token-usage/google-ingest-key.txt`)과 `INGEST_PROD_URL`(prod, `http://127.0.0.1:8788/api/ingest`, 키 `/workspace/token-usage/google-ingest-key-prod.txt`)에 각 배치(최대 40건)를 독립적으로 POST해요. 하나라도 실패하면 로그에 키를 `[redacted]`로 찍고 다른 쪽도 시도한 뒤 에러를 올려요 — 파일은 `.logged`에 표시되지 않아 다음 회차에 다시 시도돼요. 서버는 `INSERT OR IGNORE`(`dedupe_key`)로 중복 전송을 무시하므로 안전해요. `*.workers.dev` 호스트 거부는 양쪽 모두 적용해요.
