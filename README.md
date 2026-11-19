# usage-dashboard

서비스별 토큰 사용량 대시보드예요. GCE VM에서 Python과 SQLite(`google/`)로 돌아가요. Cloudflare는 쓰지 않아요.

서비스별 사용량은 5분마다 수집하고, 대시보드는 탭이 보이는 동안 30초마다 새 기록을 확인해요. 마지막 수집 후 10분까지는 실시간, 10분을 넘으면 수집 대기로 표시해요. 15분부터 해당 서비스에 수집 지연을 표시하고, 30분부터 위험으로 표시해요.

## 주소와 실행 구성

| | 운영(prod) | 개발(dev) |
| --- | --- | --- |
| 주소 | https://usage-dashboard.velar-agent.duckdns.org | https://usage-dashboard-dev.velar-agent.duckdns.org |
| 내부 포트 | `127.0.0.1:8788` | `127.0.0.1:8787` |
| systemd 유닛 | `usage-dashboard-google-prod` | `usage-dashboard-google-dev` |

- nginx가 호스트 이름으로 prod와 dev에 나눠 프록시해요. 인증서는 Let's Encrypt이고 certbot 타이머가 자동 갱신해요.
- http는 https로 301 리다이렉트해요. 도메인 없이 IP로 들어오면 `/api/`는 403이고, 그 외 경로는 dev 주소로 301이에요.
- 수집기(`token-usage-collector`)는 prod와 dev 두 곳에 https로만 보내요. http 전송은 받지 않아요.
- 매일 00:00(KST)에 cron(`/etc/cron.d/usage-dashboard-nightly`)이 `google/promote-prod.sh`를 돌려 dev의 `main`을 prod로 올려요. 점검에 실패하면 이전 커밋으로 되돌려요. 배포와 설치 절차는 [google/README.md](google/README.md)를 보세요.

## 화면 기능

- **리셋 일정**: 초기화 시각을 KST 월 달력으로 보여 줘요. 앞으로 올 초기화만 나오고, 항목마다 "서비스 지표 HH:MM 사용률"을 적어요. 한도 현황과 따로 "서비스 · 지표 · 메모" 필터를 쓰고, 선택은 브라우저에 저장돼요.
- **한도 현황**: 서비스·지표별 최신 사용률을 카드나 목록으로 보여 줘요. "서비스 · 지표 · 메모" 필터와 정렬(사용률 높은 순, 초기화 임박순 등)이 있어요.
- **추이**: 24시간, 7일, 기간 전체 그래프예요. 앞으로 올 초기화 시점에 세로 점선을 긋고, 7일·전체 그래프에서는 주간 한도의 7일 전 위치에도 연한 점선을 그어요.
- 안티그래비티 사용률이 0%인 한도만 리셋 점선과 리셋 일정에서 숨겨요. 아직 안 쓴 한도의 리셋 시각이 조회할 때마다 now+5h/now+7d로 밀리기 때문이며, 다른 서비스는 0%여도 그대로 보여줘요.
- 시각은 모두 KST이고 "11월26일(목) 04:25" 형식으로 보여 줘요.

## 저장소 구성

- `web/server.py` 화면 원본(`INDEX_HTML`). 고친 뒤 `python3 worker/scripts/sync-html.py`로 `worker/src/html.js`에 반영해야 해요. 서버는 `worker/src/html.js`에서 화면을 읽어요.
- `google/` 실행 중인 서버(Python 표준 라이브러리 + SQLite), 설치·승격 스크립트, nginx 설정, 테스트.
- `worker/schema.sql`, `worker/src/html.js`, `worker/scripts/sync-html.py`는 지금도 `google/` 서버가 써요. `worker/`의 나머지(Cloudflare Worker 코드, wrangler 설정, 배포 스크립트)는 예전 Cloudflare용이라 배포하지 않아요.
- `design/` 디자인 문서와 화면 시안
- `docs/` 수집 규격(FORMAT), 기록 전송 방법(INGEST), 장애 알림 형식(ALERTS)

테스트는 `cd worker && node --test`(화면)와 `cd google && python3 -m unittest`(서버)예요.

키·비밀번호는 VM의 설정 파일에만 있고 이 저장소에는 없어요.
