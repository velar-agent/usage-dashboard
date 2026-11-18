# token-dashboard (Cloudflare Worker + D1, 무료 플랜)

토큰 사용량 대시보드를 Cloudflare 에서 돌리는 코드예요. 내 컴퓨터(box)가 꺼져 있어도 `https://usage-dashboard.velar-agent.workers.dev` 가 열려요.

| 항목 | 값 |
| --- | --- |
| Worker 이름 | `usage-dashboard` (2026-11-16 usage 에서 이름 변경. 이전: 2026-11-15 token-dashboard → usage) |
| D1 데이터베이스 | `token-usage` (id `3b3117af-7c22-4aaa-85cc-2d8ec448e9b7`) |
| 계정 | `a12d19c670e84a71f5acc26df16e47f7` |
| 주소 | `usage-dashboard.velar-agent.workers.dev` (무료 workers.dev 주소. 2026-11-16 usage.velar-agent → usage-dashboard.velar-agent 로 변경. 이전: 2026-11-15 token-dashboard.jwhan-oe → usage.velar-agent, token.wle.kr 연결은 같은 날 해제) |
| 바인딩 | `DB`(D1), 시크릿 `INGEST_KEY` `SESSION_SECRET` `RAW_SALT` `RAW_HASH` `RAW_ITERATIONS` |

## 파일
- `src/index.js` — Worker 본체 (화면 제공, `/api/records`, `/health`, `/api/raw*`, `/api/ingest`, `/api/raw/upload`)
- `src/html.js` — 화면(HTML/CSS/JS). `web/server.py` 의 `INDEX_HTML` 을 옮긴 것. 화면을 고쳤다면 `python3 scripts/sync-html.py`
- `schema.sql` — D1 테이블 (`records`, `raw_files`, `login_fails`, `revoked_sessions`)
- `wrangler.toml` — wrangler 로 배포할 때 쓰는 설정
- `scripts/make-deploy-code.py` — Cloudflare 연결(MCP `execute`)로 올릴 코드를 만들어요
- `scripts/promote-dev-to-prod.py` — 개발(usage-dashboard-dev)에서 검증한 코드를 운영(usage-dashboard)에 올리기 위한 준비/검증, 그리고 `deploy`(Cloudflare REST API 로 운영 직접 배포, 실패 시 자동 롤백)
- `scripts/nightly-promote.sh` — 매일 00:00(KST) GCE VM 의 시스템 cron 이 돌리는 러너(git pull → precheck → deploy). `scripts/start-nightly-cron.sh`·`cron/crontab` — 예전 box 의 supercronic 스케줄러용(지금은 `FORCE_START=1` 없이는 안 띄움)
- `scripts/import-local-data.py` — `processed/`, `raw/` 파일을 Worker 로 올려요 (중복은 무시)

## 다시 배포하는 방법
### A. wrangler (본인 컴퓨터, Cloudflare 로그인 필요)
```bash
cd worker
npm install wrangler
npx wrangler login
npx wrangler deploy            # D1 id 는 wrangler.toml 에 들어 있어요. 시크릿은 그대로 유지돼요.
```
시크릿을 새로 넣거나 바꾸려면:
```bash
npx wrangler secret put INGEST_KEY       # 로깅 봇이 쓰는 키. 바꾸면 /workspace/token-usage/ingest-key.txt 도 같이 바꿔요
npx wrangler secret put SESSION_SECRET   # 아무 긴 무작위 문자열. 바꾸면 열려 있는 원본 열람 세션이 모두 끊겨요
npx wrangler secret put RAW_SALT         # 원본 비밀번호 PBKDF2 솔트(hex)
npx wrangler secret put RAW_HASH         # PBKDF2-SHA256(hex, 32바이트)
npx wrangler secret put RAW_ITERATIONS   # 10000 (Workers WebCrypto 는 10만 회 초과를 지원하지 않아요)
```
원본 비밀번호 바꾸기: 새 솔트/해시를 만들어 위 세 값을 넣으면 돼요.
```bash
python3 -c "import hashlib,secrets,getpass;s=secrets.token_bytes(16);p=getpass.getpass().encode();print('RAW_SALT',s.hex());print('RAW_HASH',hashlib.pbkdf2_hmac('sha256',p,s,10000).hex())"
```
### B. Cloudflare 연결(MCP) 로 (box 에서)
```bash
python3 worker/scripts/make-deploy-code.py > /tmp/deploy.js   # 코드만 교체, 시크릿 유지(keep_bindings)
# → user-Cloudflare 의 execute 도구에 code="$file:/tmp/deploy.js" 로 실행. 끝나면 /tmp/deploy.js 삭제
```
### C. 대시보드
Workers & Pages → `usage-dashboard` → Edit code 로 `src/index.js` / `src/html.js` 를 직접 붙여 넣어도 돼요.

## 개발 환경 (usage-dashboard-dev)
운영과 같은 코드·바인딩 이름·시크릿 이름을 쓰는 복사본이에요. D1은 따로 두고, 운영 Worker(`usage-dashboard`)는 건드리지 않아요.

| 항목 | 값 |
| --- | --- |
| Worker 이름 | `usage-dashboard-dev` (2026-11-16 usage-dev 에서 이름 변경) |
| D1 데이터베이스 | `usage-dev` (id `7d0d0229-8d79-41f3-9a51-64b52b73f440`) |
| 주소 | `https://usage-dashboard-dev.velar-agent.workers.dev` |

배포할 때 운영 기본값(`--name` 기본 `usage-dashboard`)은 그대로 두고, 개발 쪽만 지정해요. `--d1-id`를 생략하면 `wrangler.toml`의 `[env.dev]` database_id 를 씁니다. D1 데이터베이스 이름(`usage-dev`)은 그대로입니다.
```bash
python3 worker/scripts/make-deploy-code.py --name usage-dashboard-dev > /tmp/deploy.js   # 코드만 교체, 시크릿 유지(keep_bindings)
# → user-Cloudflare 의 execute 도구에 code="$file:/tmp/deploy.js" 로 실행. 끝나면 /tmp/deploy.js 삭제
```
처음 만들 때나 시크릿을 다시 넣을 때는 `--secrets FILE`을 붙입니다. wrangler 로는 `npx wrangler deploy --env dev` 입니다.


## 개발에서 운영으로 올리기 (promote)
코드만 올립니다. D1 데이터는 개발에서 운영으로 복사하지 않고, 시크릿·바인딩은 `keep_bindings` 로 그대로 둡니다(이 절은 수동 MCP 경로). box 에는 wrangler 로그인이 없으니, 스크립트는 배포 파일만 만들고 실제 업로드는 Cloudflare 연결(MCP `execute`)로 합니다. `build`/`precheck`/`postcheck`/`diff` 는 운영에 배포하지 않습니다. 직접 배포는 `deploy`(아래 "매일 자정 자동 배포")만 합니다.

`make-deploy-code.py` 의 `--name` 기본값은 `usage-dashboard`(운영)입니다. 개발에 올릴 때만 `--name usage-dashboard-dev` 를 붙이세요. 빠뜨리면 운영에 나갑니다. promote 스크립트의 `build` 는 `--name usage-dashboard` 를 항상 명시하고, 생성된 코드가 운영 Worker·운영 D1 을 가리키는지 확인합니다. `--secrets` 는 쓰지 않습니다.

```bash
# 1) 구문 검사, src/html.js 와 server.py 동기화, 개발(usage-dashboard-dev) 동작 확인
python3 worker/scripts/promote-dev-to-prod.py precheck

# 2) 운영(usage-dashboard)용 배포 코드 생성. 대상이 운영인지 출력과 파일 내용으로 확인하세요. 기본 출력은 /tmp/deploy.js
python3 worker/scripts/promote-dev-to-prod.py build

# 3) user-Cloudflare 의 execute 도구에 code="$file:/tmp/deploy.js" 로 실행
#    (이 단계만 실제로 운영 Worker 코드를 교체합니다)

# 4) 운영(usage-dashboard) 동작 확인 후 /tmp/deploy.js 삭제
python3 worker/scripts/promote-dev-to-prod.py postcheck

# (선택) 개발·운영에 올라가 있는 화면(/)과 /health 를 비교
python3 worker/scripts/promote-dev-to-prod.py diff
```

`precheck` 는 배포 전에 다음을 확인합니다. `server.py` / 생성된 HTML 의 `<script>` 구문(node `new Function`), `src/html.js` 가 `web/server.py` 와 같은지, 개발 `/health` 200, `/` 와 `/api/records` 정상, 인증 없는 `POST /api/ingest` 가 401 인지, 그리고 개발에 올라가 있는 화면이 로컬 코드와 같은지. `postcheck` 는 같은 검사를 운영에 대해 수행한 뒤 배포 파일을 지웁니다.

롤백: Cloudflare 대시보드 또는 API에서 Worker `usage-dashboard` 의 이전 배포(버전)로 되돌리면 됩니다. 소스 쪽은 git 이전 커밋이나 `web/server.py.bak-*` 백업을 씁니다. 시크릿은 배포 때 유지되므로 롤백해도 보통 그대로입니다.

## 매일 자정 자동 배포 (GCE VM cron)
매일 00:00(Asia/Seoul)에 GCE VM(`token-usage-collector`, box 에서 `ssh gce-collector`)의 시스템 cron 이 `worker/scripts/nightly-promote.sh` 를 실행해 개발(`usage-dashboard-dev`)에서 검증된 코드를 운영(`usage-dashboard`)에 올립니다. MCP·wrangler 로그인 없이 Cloudflare REST API 를 직접 씁니다. 코드만 올리고 D1 데이터는 복사하지 않으며, 시크릿은 값 없이 `inherit`(Versions API)로 이어받습니다. Versions API 메타에는 허용된 필드만 보내므로 `observability`·logpush 같은 스크립트 설정은 건드리지 않고 기존 설정이 그대로 유지됩니다(수동 MCP 배포 `build` 경로만 `keep_bindings` 를 씁니다). `deploy` 는 `usage-dashboard` 이외의 Worker 는 거부합니다.

순서(러너): ① 락(`flock`, 겹침 방지) ② main 브랜치·깨끗한 작업 트리 확인 후 `git pull --ff-only`(HEAD 가 origin/main 과 같아야 함) ③ `precheck` 11개 전부 통과 ④ `deploy`:
API 로 현재 운영 배포 id 기록 → 개발·운영의 실제 올라가 있는 코드(index.js/html.js)를 읽어 비교 →
개발(live) ≠ 로컬 main 이면 **거부(종료 3)** → **업로드 전 점검**: 가장 최근에 올린 버전이 지금 트래픽을 받는 버전과 같고 필수 바인딩(DB=운영 D1, 시크릿 5개 이름)을 가졌는지, 아니면 **거부(종료 7)** → 개발 = 운영이면 **건너뜀(종료 0)** → 아니면 ① Versions API 로 **배포 없이** 새 버전 업로드(시크릿은 값 없이 `inherit` + `bindings_inherit=strict`) ② 새 버전의 바인딩 확인 ③ 새 버전을 100% 로 배포 ④ 바인딩·`postcheck` → 실패하면 기록해 둔 이전 배포로 롤백하고 다시 점검. 응답을 잃은 요청은 버전/배포 목록을 다시 조회해(재시도) 실제로 반영됐는지 확인하고, 배포됐으면 점검·롤백을 합니다. 롤백 요청의 응답을 잃은 경우도 활성 배포를 다시 조회하고(한 번 재시도) 실제로 되돌아갔으면 종료 5 로 처리합니다.

새 버전은 "가장 최근에 올린 버전"에서 시크릿을 이어받기 때문에, 롤백 뒤에는 실패한 버전이 최근 업로드로 남아 다음 실행이 종료 7 로 멈춰요(의도된 안전장치). 풀려면 대시보드(Worker → Deployments)에서 정상 버전을 100% 로 배포해 "최근 업로드 = 활성" 상태로 만드세요. 이 안전장치가 걸린 날은 `last-run.json` 의 `exit` 가 7 입니다.
배포 코드는 메모리에서만 만들고 임시 파일은 만들지 않습니다(수동 MCP 배포용 `/tmp/deploy.js` 도 건드리지 않아요).

| 종료 코드 | 뜻 |
|---|---|
| 0 | 성공 또는 변경 없음(건너뜀) |
| 1 | git pull / precheck / API 읽기 실패 (배포 안 함) |
| 2 | API 토큰 파일 없음·비어 있음·권한 과다(스코프 검사는 안 함) |
| 3 | 개발(live)이 로컬 main 과 달라 운영 반영 거부 |
| 4 | 버전 업로드·배포 생성 실패(운영은 그대로) |
| 5 | 배포 후 점검 실패 → 이전 버전으로 롤백, 롤백 후 점검 통과 |
| 7 | 업로드 전 안전 점검 거부(최근 업로드 ≠ 활성 버전, 필수 바인딩 없음 등) → **수동 확인 필요**, 아무것도 올리지 않음 |
| 6 | 롤백 실패 또는 롤백 후에도 점검 실패 → **수동 확인 필요** |
| 9 | 이미 다른 실행 중 |

파일 위치(토큰·로그는 repo 밖이고 토큰은 절대 커밋하지 않음)
- API 토큰: `~/.config/usage-deploy/cf-token` (디렉터리 700, 파일 600). 권한: Workers Scripts Edit + Account Settings Read, 계정 `a12d19c6…`. 환경변수 `CLOUDFLARE_API_TOKEN` 이 있으면 그걸 먼저 씁니다. 값은 로그·출력에 나오지 않습니다.
- 스케줄(운영): VM 의 `/etc/cron.d/usage-dashboard-nightly` — `0 0 * * * collector /workspace/usage-dashboard/worker/scripts/nightly-promote.sh` (`CRON_TZ`=Asia/Seoul, VM 시간대도 Asia/Seoul). VM 의 `/workspace/usage-dashboard` 는 GitHub 에서 받은 git 체크아웃이고, 러너가 매번 `git pull --ff-only` 로 최신 main 을 받아요. VM 에는 precheck 용 `node`(v24.22.1, `~/.local/node`, `~/.local/bin/node`)를 설치했어요.
- `worker/cron/crontab` 과 `start-nightly-cron.sh` 는 예전 box 의 `supercronic` 용이에요. 두 곳에서 배포하지 않도록 `start-nightly-cron.sh` 는 `FORCE_START=1` 없이는 띄우지 않아요(`--status`·`--stop` 은 됨).
- 러너는 커밋·푸시 전의 변경(스테이징 포함)이 있으면 배포하지 않아요(종료 1).
- 로그(VM): `~/.local/state/usage-deploy/YYYY-MM-DD.log`(60일 보관), 마지막 결과 `last-run.json`

VM 에서 확인·수동 실행 (box 에서):
```bash
ssh gce-collector
S=/workspace/usage-dashboard/worker/scripts
$S/nightly-promote.sh --dry-run         # 업로드·롤백만 빼고 전부 수행 (--force: 같아도 계획까지 출력)
tail -n 40 ~/.local/state/usage-deploy/$(date +%F).log; cat ~/.local/state/usage-deploy/last-run.json
python3 $S/promote-dev-to-prod.py status    # 운영 최근 배포 목록 (읽기 전용)
cat /etc/cron.d/usage-dashboard-nightly     # 스케줄
sudo mv /etc/cron.d/usage-dashboard-nightly /etc/cron.d/usage-dashboard-nightly.disabled   # 일시 중지 (cron 은 이름에 . 이 든 파일을 읽지 않아요. 되돌리려면 다시 mv)
```

VM 의 시스템 cron 은 재부팅 뒤에도 그대로 돌아요. cron 은 채팅으로 알림을 보내지 못하므로 결과는 VM 의 로그·`last-run.json` 으로 확인합니다. API 토큰은 box 의 `~/.config/usage-deploy/cf-token` 을 VM 같은 경로에 복사해 두었어요(권한 600). 토큰을 바꾸면 VM 쪽도 바꿔야 해요.

수동 롤백(API): `promote-dev-to-prod.py status` 로 이전 배포의 version id 를 확인한 뒤, 대시보드의 Worker → Deployments 에서 되돌리거나 API `POST /accounts/<acct>/workers/scripts/usage-dashboard/deployments` 에 `{"strategy":"percentage","versions":[{"version_id":"<id>","percentage":100}]}` 를 보냅니다. 자정 배포가 반복해서 같은 코드를 올리지 않게 하려면 먼저 VM 의 자정 cron 을 위처럼 일시 중지하거나 개발에 되돌릴 코드를 올려 두세요.

## 동작 메모
- `/api/records` 응답은 같은 주소 기준 엣지에 20초 캐시돼요(D1 읽기량 절약). 새 기록은 최대 20초 뒤에 보여요.
- 원본 열람 비밀번호: `RAW_HASH`(PBKDF2-SHA256, 10,000회). 세션은 서명된 HttpOnly·Secure·SameSite=Strict 쿠키 12시간. 로그아웃하면 해당 세션은 D1 `revoked_sessions` 로 무효화돼요. 틀린 비밀번호는 IP당 1분에 5번까지.
- 기록 보내는 법은 `/workspace/token-usage/INGEST.md`.
- 무료 플랜 제약: Worker 호출당 D1 질의 50개 → `/api/ingest` 는 한 번에 40건까지.
- 처음 배포용 시크릿 묶음은 `/workspace/token-usage/.worker-secrets.json`(권한 600)에 있어요. 배포가 끝나면 지워도 돼요. 키는 `ingest-key.txt`(권한 600)에도 있어요.

## GitHub
- 소스 저장소: https://github.com/velar-agent/usage-dashboard (공개). 작업 사본은 `/workspace/usage-dashboard`. 여기서 고친 코드·문서는 그 사본에 복사해 커밋·푸시해요. 키·비밀번호·원본 기록은 올리지 않아요.
