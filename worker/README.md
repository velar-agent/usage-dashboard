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

## 동작 메모
- `/api/records` 응답은 같은 주소 기준 엣지에 20초 캐시돼요(D1 읽기량 절약). 새 기록은 최대 20초 뒤에 보여요.
- 원본 열람 비밀번호: `RAW_HASH`(PBKDF2-SHA256, 10,000회). 세션은 서명된 HttpOnly·Secure·SameSite=Strict 쿠키 12시간. 로그아웃하면 해당 세션은 D1 `revoked_sessions` 로 무효화돼요. 틀린 비밀번호는 IP당 1분에 5번까지.
- 기록 보내는 법은 `/workspace/token-usage/INGEST.md`.
- 무료 플랜 제약: Worker 호출당 D1 질의 50개 → `/api/ingest` 는 한 번에 40건까지.
- 처음 배포용 시크릿 묶음은 `/workspace/token-usage/.worker-secrets.json`(권한 600)에 있어요. 배포가 끝나면 지워도 돼요. 키는 `ingest-key.txt`(권한 600)에도 있어요.
