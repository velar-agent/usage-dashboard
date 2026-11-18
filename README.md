# usage-dashboard

토큰 사용량 대시보드. Cloudflare Worker + D1(무료 플랜)으로 돌아가요.

서비스별 사용량은 5분마다 수집하고, 대시보드는 탭이 보이는 동안 30초마다 새 기록을 확인해요. 마지막 수집 후 10분까지는 실시간, 10분을 넘으면 수집 대기로 표시해요. 15분부터 해당 서비스에 수집 지연을 표시하고, 30분부터 위험으로 표시해요.

- 주소: https://usage-dashboard.velar-agent.workers.dev
- 개발: https://usage-dashboard-dev.velar-agent.workers.dev
- `worker/` Worker 코드, D1 스키마, 배포 스크립트 (자세한 건 `worker/README.md`)
- `web/server.py` 화면 원본 (`worker/scripts/sync-html.py` 로 `worker/src/html.js` 에 반영)
- `design/` 디자인 문서와 화면 시안
- `docs/` 수집 규격(FORMAT), 기록 전송 방법(INGEST), 장애 알림 형식(ALERTS)

키·비밀번호는 Cloudflare 시크릿에만 있고 이 저장소에는 없어요.
