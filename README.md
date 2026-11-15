# usage-dashboard

토큰 사용량 대시보드. Cloudflare Worker + D1(무료 플랜)으로 돌아가요.

- 주소: https://usage.velar-agent.workers.dev
- `worker/` Worker 코드, D1 스키마, 배포 스크립트 (자세한 건 `worker/README.md`)
- `web/server.py` 화면 원본 (`worker/scripts/sync-html.py` 로 `worker/src/html.js` 에 반영)
- `design/` 디자인 문서와 화면 시안
- `docs/` 수집 규격(FORMAT), 기록 전송 방법(INGEST), 장애 알림 형식(ALERTS)

키·비밀번호는 Cloudflare 시크릿에만 있고 이 저장소에는 없어요.
