# token-dashboard (Cloudflare Worker + D1, 무료 플랜)

토큰 사용량 대시보드를 Cloudflare 에서 돌리는 코드예요. 내 컴퓨터(box)가 꺼져 있어도 `https://usage.velar-agent.workers.dev` 가 열려요.

| 항목 | 값 |
| --- | --- |
| Worker 이름 | `usage` (2026-11-15 token-dashboard 에서 이름 변경) |
| D1 데이터베이스 | `token-usage` (id `3b3117af-7c22-4aaa-85cc-2d8ec448e9b7`) |
| 계정 | `a12d19c670e84a71f5acc26df16e47f7` |
| 주소 | `usage.velar-agent.workers.dev` (무료 workers.dev 주소. 2026-11-15 token-dashboard.jwhan-oe → usage.velar-agent 로 변경, token.wle.kr 연결은 같은 날 해제) |
| 바인딩 | `DB`(D1), 시크릿 `INGEST_KEY` `SESSION_SECRET` `RAW_SALT` `RAW_HASH` `RAW_ITERATIONS` |

## 파일
- `src/index.js` — Worker 본체 (화면 제공, `/api/records`, `/health`, `/api/raw*`, `/api/ingest`, `/api/raw/upload`)
- `src/html.js` — 화면(HTML/CSS/JS). `web/server.py` 의 `INDEX_HTML` 을 옮긴 것. 화면을 고쳤다면 `python3 scripts/sync-html.py`
- `schema.sql` — D1 테이블 (`records`, `raw_files`, `login_fails`, `revoked_sessions`)
- `wrangler.toml` — wrangler 로 배포할 때 쓰는 설정
- `scripts/make-deploy-code.py` — Cloudflare 연결(MCP `execute`)로 올릴 코드를 만들어요
- `scripts/promote-dev-to-prod.py` — 개발(usage-dev)에서 검증한 코드를 운영(usage)에 올리기 위한 준비/검증. 배포 자체는 하지 않아요
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
Workers & Pages → `usage` → Edit code 로 `src/index.js` / `src/html.js` 를 직접 붙여 넣어도 돼요.

## 개발 환경 (usage-dev)
운영과 같은 코드·바인딩 이름·시크릿 이름을 쓰는 복사본이에요. D1은 따로 두고, 운영 Worker(`usage`)는 건드리지 않아요.

| 항목 | 값 |
| --- | --- |
| Worker 이름 | `usage-dev` |
| D1 데이터베이스 | `usage-dev` (id `7d0d0229-8d79-41f3-9a51-64b52b73f440`) |
| 주소 | `https://usage-dev.velar-agent.workers.dev` |

배포할 때 운영 기본값(`--name` 기본 `usage`)은 그대로 두고, 개발 쪽만 지정해요. `--d1-id`를 생략하면 `wrangler.toml`의 `[env.dev]` database_id 를 씁니다.
```bash
python3 worker/scripts/make-deploy-code.py --name usage-dev > /tmp/deploy.js   # 코드만 교체, 시크릿 유지(keep_bindings)
# → user-Cloudflare 의 execute 도구에 code="$file:/tmp/deploy.js" 로 실행. 끝나면 /tmp/deploy.js 삭제
```
처음 만들 때나 시크릿을 다시 넣을 때는 `--secrets FILE`을 붙입니다. wrangler 로는 `npx wrangler deploy --env dev` 입니다.


## 개발에서 운영으로 올리기 (promote)
코드만 올립니다. D1 데이터는 개발에서 운영으로 복사하지 않고, 시크릿·바인딩은 `keep_bindings` 로 그대로 둡니다. box 에는 wrangler 로그인이 없으니, 스크립트는 배포 파일만 만들고 실제 업로드는 Cloudflare 연결(MCP `execute`)로 합니다. `promote-dev-to-prod.py` 는 운영 Worker 에 직접 배포하지 않습니다.

`make-deploy-code.py` 의 `--name` 기본값은 `usage`(운영)입니다. 개발에 올릴 때만 `--name usage-dev` 를 붙이세요. 빠뜨리면 운영에 나갑니다. promote 스크립트의 `build` 는 `--name usage` 를 항상 명시하고, 생성된 코드가 운영 Worker·운영 D1 을 가리키는지 확인합니다. `--secrets` 는 쓰지 않습니다.

```bash
# 1) 구문 검사, src/html.js 와 server.py 동기화, 개발(usage-dev) 동작 확인
python3 worker/scripts/promote-dev-to-prod.py precheck

# 2) 운영(usage)용 배포 코드 생성. 대상이 운영인지 출력과 파일 내용으로 확인하세요. 기본 출력은 /tmp/deploy.js
python3 worker/scripts/promote-dev-to-prod.py build

# 3) user-Cloudflare 의 execute 도구에 code="$file:/tmp/deploy.js" 로 실행
#    (이 단계만 실제로 운영 Worker 코드를 교체합니다)

# 4) 운영(usage) 동작 확인 후 /tmp/deploy.js 삭제
python3 worker/scripts/promote-dev-to-prod.py postcheck

# (선택) 개발·운영에 올라가 있는 화면(/)과 /health 를 비교
python3 worker/scripts/promote-dev-to-prod.py diff
```

`precheck` 는 배포 전에 다음을 확인합니다. `server.py` / 생성된 HTML 의 `<script>` 구문(node `new Function`), `src/html.js` 가 `web/server.py` 와 같은지, 개발 `/health` 200, `/` 와 `/api/records` 정상, 인증 없는 `POST /api/ingest` 가 401 인지, 그리고 개발에 올라가 있는 화면이 로컬 코드와 같은지. `postcheck` 는 같은 검사를 운영에 대해 수행한 뒤 배포 파일을 지웁니다.

롤백: Cloudflare 대시보드 또는 API에서 Worker `usage` 의 이전 배포(버전)로 되돌리면 됩니다. 소스 쪽은 git 이전 커밋이나 `web/server.py.bak-*` 백업을 씁니다. 시크릿은 배포 때 유지되므로 롤백해도 보통 그대로입니다.

## 동작 메모
- `/api/records` 응답은 같은 주소 기준 엣지에 20초 캐시돼요(D1 읽기량 절약). 새 기록은 최대 20초 뒤에 보여요.
- 원본 열람 비밀번호: `RAW_HASH`(PBKDF2-SHA256, 10,000회). 세션은 서명된 HttpOnly·Secure·SameSite=Strict 쿠키 12시간. 로그아웃하면 해당 세션은 D1 `revoked_sessions` 로 무효화돼요. 틀린 비밀번호는 IP당 1분에 5번까지.
- 기록 보내는 법은 `/workspace/token-usage/INGEST.md`.
- 무료 플랜 제약: Worker 호출당 D1 질의 50개 → `/api/ingest` 는 한 번에 40건까지.
- 처음 배포용 시크릿 묶음은 `/workspace/token-usage/.worker-secrets.json`(권한 600)에 있어요. 배포가 끝나면 지워도 돼요. 키는 `ingest-key.txt`(권한 600)에도 있어요.

## GitHub
- 소스 저장소: https://github.com/velar-agent/usage-dashboard (공개). 작업 사본은 `/workspace/usage-dashboard`. 여기서 고친 코드·문서는 그 사본에 복사해 커밋·푸시해요. 키·비밀번호·원본 기록은 올리지 않아요.
