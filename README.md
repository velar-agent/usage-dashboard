# usage-dashboard

여러 사람의 Claude 토큰 사용량을 한곳에 모아 보는 대시보드 서버입니다. 각자의 컴퓨터에서 도는 토큰 수집 프로세스가 사용 기록을 이 서버로 보내고, 사용자는 브라우저로 접속해 전체 사용량을 봅니다.

- 실행 파일 하나와 SQLite 파일 하나로 동작합니다. 웹 화면은 실행 파일에 들어 있습니다.
- 수집 프로세스는 이 저장소에 없습니다. [docs/INGEST_API.md](docs/INGEST_API.md)의 규칙에 맞춰 별도 저장소에서 만듭니다.
- Claude 계열 모델만 대상으로 합니다. 수신 형식이 Anthropic API의 `usage` 객체를 그대로 받도록 되어 있습니다.

## 화면

로그인하면 모든 사용자의 사용량을 볼 수 있습니다.

- **조회 조건**: 기간(오늘, 7일, 30일, 90일, 전체, 직접 지정), 사용자, 모델, 프로젝트. 화면의 모든 숫자가 같은 조건을 따릅니다.
- **요약**: 총 토큰, 추정 비용, 요청 수, 캐시 적중률, 사용자·세션 수, 그리고 직전 같은 길이 기간 대비 증감.
- **토큰 구성**: 입력, 출력, 캐시 쓰기, 캐시 읽기의 비중을 토큰 수와 추정 비용으로 나란히 보여 줍니다.
- **기간별 추이**: 토큰 종류별 차트 네 개와 추정 비용 차트. 기간 길이에 따라 시간·일·주·월 단위로 묶습니다. 차트마다 표로 바꿔 볼 수 있습니다.
- **분류별 집계**: 사용자별, 모델별, 프로젝트별, 출처별(출처가 둘 이상일 때).
- **세션**: 추정 비용이 큰 세션 20개.

날짜는 보는 사람 브라우저의 시간대로 자릅니다. 다크 모드는 운영체제 설정을 따릅니다.

## 요구 사항

- Rust 1.89 이상(의존성이 요구하는 최소 버전입니다. 빌드와 테스트는 1.100에서 확인했습니다)
- C 컴파일러(`cc`). SQLite를 실행 파일에 함께 컴파일하기 때문에 필요합니다. SQLite를 따로 설치할 필요는 없습니다.

## 빌드와 실행

```sh
cargo build --release

# 사용자를 만들고 토큰을 받습니다. 토큰은 이때 한 번만 표시됩니다.
./target/release/usage-dashboard --db ./usage.db user add 홍길동 --label 맥북

# 서버를 실행합니다. 다른 컴퓨터에서 접속하려면 0.0.0.0으로 엽니다.
./target/release/usage-dashboard --db ./usage.db serve --listen 0.0.0.0:8787
```

브라우저에서 `http://<서버 주소>:8787`로 접속해 받은 토큰으로 로그인합니다. 같은 토큰을 수집 프로세스에도 넣습니다.

수집 프로세스가 아직 없다면 다음처럼 직접 한 건을 보내 동작을 확인할 수 있습니다.

```sh
curl -sS http://localhost:8787/api/v1/events \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"events\": [{\"id\": \"test-1\", \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"model\": \"claude-fable-5-2\",
       \"usage\": {\"input_tokens\": 100, \"output_tokens\": 500, \"cache_read_input_tokens\": 20000}}]}"
```

## 명령

`--db`는 모든 명령에 공통이며 기본값은 현재 폴더의 `usage.db`입니다. 환경 변수 `USAGE_DASHBOARD_DB`로도 지정할 수 있습니다. 서버가 실행 중일 때도 사용자·토큰 명령을 쓸 수 있고, 바로 반영됩니다.

| 명령 | 하는 일 |
| --- | --- |
| `serve` | 서버를 실행합니다. |
| `user add <이름> [--label <메모>]` | 사용자를 만들고 첫 토큰을 출력합니다. |
| `user list` | 사용자 목록, 유효한 토큰 수, 마지막 기록 시각을 보여 줍니다. |
| `token add <사용자> [--label <메모>]` | 기존 사용자에게 토큰을 하나 더 발급합니다(컴퓨터마다 따로 쓸 때). |
| `token list` | 토큰 목록을 보여 줍니다. 토큰 자체는 저장하지 않으므로 끝 네 글자만 보입니다. |
| `token revoke <id>` | 토큰을 폐기합니다. 그 토큰으로 만든 브라우저 로그인도 끊깁니다. |
| `prices` | 내장 단가표를 JSON으로 출력합니다. |

새 토큰은 표준 출력에 토큰만 나오고 안내 문구는 표준 오류로 나오므로 `TOKEN=$(usage-dashboard user add 홍길동)`처럼 받아 쓸 수 있습니다.

### serve 옵션

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--listen` | `127.0.0.1:8787` | 수신 주소. 환경 변수 `USAGE_DASHBOARD_LISTEN`. |
| `--prices` | 내장 단가표 | 단가표 JSON 파일. 환경 변수 `USAGE_DASHBOARD_PRICES`. |
| `--utc-offset` | `+09:00` | 요청이 시간대를 보내지 않을 때 날짜 경계에 쓰는 UTC 오프셋. 화면은 항상 브라우저의 시간대를 보냅니다. |
| `--cookie-secure` | 꺼짐 | 세션 쿠키에 `Secure`를 붙입니다. HTTPS로 제공할 때 켭니다. |
| `--max-events` | `5000` | 요청 하나에 받는 이벤트 수 상한. |
| `--max-body-mb` | `16` | 요청 본문 크기 상한(MB). |

## 추정 비용과 단가표

화면의 비용은 토큰 수에 Anthropic API 공시 단가를 곱한 **추정치**입니다. 구독 요금제를 쓰는 경우 실제 청구액과 다릅니다.

- 단가표는 [prices.json](prices.json)이며 빌드할 때 실행 파일에 포함됩니다. 2026-11-22에 [Anthropic 가격 문서](https://platform.claude.com/docs/en/about-claude/pricing)에서 옮겼습니다.
- 비용은 저장하지 않고 조회할 때 계산합니다. 단가표를 바꾸면 과거 기록의 비용도 새 단가로 다시 계산됩니다.
- 단가가 바뀌거나 새 모델이 나오면 `usage-dashboard prices > prices.local.json`으로 꺼내 고친 뒤 `--prices prices.local.json`으로 지정합니다.
- 단가표에 없는 모델은 비슷한 모델의 단가를 빌리지 않습니다. 토큰 수는 집계하되 비용에서는 빼고, 화면에 `*`와 함께 어떤 모델이 빠졌는지 표시합니다. 날짜가 붙은 스냅샷 id(`claude-haiku-4-5-20251001`)만 날짜 없는 id로 찾습니다.
- 캐시 쓰기(5분/1시간), 캐시 읽기, 빠른 모드, 배치, 미국 내 추론, Haiku 5.5의 긴 프롬프트 단가, 웹 검색(건당)을 반영합니다.

단가 계산은 Claude Code가 세션마다 남기는 비용 기록과 대조했습니다. 대조로 확인한 범위는 다음과 같고, 그 밖의 값은 가격 문서에서 옮기기만 한 상태입니다.

- `claude-fable-5`, `claude-fable-5-1`: 입력, 출력, 1시간 캐시 쓰기, 캐시 읽기 단가가 실제 기록과 일치했습니다.
- `claude-haiku-4-5`: 입력과 출력 단가만 일치를 확인했습니다.
- 확인하지 못한 것: 그 밖의 모든 모델(`claude-fable-5-2` 포함), 5분 캐시 쓰기 단가, 빠른 모드·배치·미국 내 추론 배수, Haiku 5.5의 긴 프롬프트 단가, 웹 검색 요금, 어드바이저 호출의 비용. 대조할 기록이 없었습니다.

## 배포

[deploy/usage-dashboard.service](deploy/usage-dashboard.service)는 systemd 유닛 예시입니다. 파일 안의 사용자(`usage-dashboard`)와 경로(`/opt/usage-dashboard`)는 자리표시자이므로 서버의 계정과 폴더로 바꾼 뒤 사용합니다. 빌드하는 서버에는 Rust와 C 컴파일러가 있어야 합니다.

```sh
cargo build --release
sudo cp deploy/usage-dashboard.service /etc/systemd/system/usage-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now usage-dashboard.service
journalctl --follow --unit usage-dashboard.service
```

**HTTPS**: 이 서버는 HTTP만 제공합니다. 토큰이 요청 헤더로 오가므로, 인터넷에 노출할 때는 nginx나 caddy 같은 리버스 프록시로 HTTPS를 붙이고 `--cookie-secure`를 켜십시오. 내부망이나 VPN 안에서만 쓸 때는 HTTP 그대로 쓸 수 있습니다.

**요청 횟수 제한**: 서버에는 요청 횟수 제한이 없습니다. 토큰이 160비트 난수라서 로그인을 반복 시도해도 맞힐 수 없고, 틀린 토큰은 읽기 전용 조회 한 번으로 끝나 기록 저장을 막지 않습니다. 그래도 인터넷에 노출한다면 리버스 프록시에서 `/api/` 경로의 요청 횟수를 제한하는 편이 좋습니다.

**업그레이드**: 새 버전을 실행하면 데이터베이스 구조가 자동으로 올라갑니다(200만 행에서 약 5초). 한번 올라간 데이터베이스는 이전 버전 실행 파일로 열 수 없으므로, 업그레이드 전에 백업해 두십시오.

**저장 용량**: 모델 호출 한 번이 한 행이며, 한 행에 300바이트 안팎을 씁니다(100만 행에 약 300MB로 추정). 그중 절반 가까이는 조회를 빠르게 하는 리포트용 인덱스입니다. 오래된 기록을 자동으로 지우는 기능은 없으므로 파일 크기를 가끔 확인하십시오.

**백업**: 데이터는 `--db`로 지정한 SQLite 파일에 있습니다. 서버가 실행 중일 때는 파일을 그냥 복사하지 말고 다음처럼 백업합니다.

```sh
sqlite3 usage.db ".backup 'usage-backup.db'"
```

## 보안

- 토큰은 서버에 SHA-256 해시로만 저장합니다. 브라우저 쿠키에는 토큰이 아니라 세션 id가 들어가며, 세션은 30일 뒤 만료됩니다.
- 수집 API는 `Authorization` 헤더의 토큰만 받고, 토큰을 URL에 넣는 경로는 없습니다.
- 접근 기록에는 요청 경로와 상태 코드만 남기고 쿼리 문자열과 헤더는 남기지 않습니다.
- 화면에 표시되는 사용자·프로젝트·모델 이름은 모두 글자로만 넣으며, 외부 스크립트와 스타일을 막는 CSP 헤더를 붙입니다.
- 로그인한 사용자는 **모든 사용자의** 사용량과 프로젝트 이름을 볼 수 있습니다. 프로젝트 이름을 공개하고 싶지 않은 사람은 수집 프로세스에서 `project`를 보내지 않으면 됩니다.

## 개발

```sh
cargo test          # 단위 테스트와 HTTP 수준 테스트
cargo clippy --all-targets
```

```
.
├── Cargo.toml
├── prices.json            # 내장 단가표
├── deploy/                # systemd 유닛 예시
├── docs/INGEST_API.md     # 수집 프로세스가 따르는 수신 API 규칙
├── src/
│   ├── main.rs            # 명령줄: serve, user, token, prices
│   ├── server.rs          # 경로, 인증, 응답 헤더, 내장 화면 제공
│   ├── ingest.rs          # 이벤트 묶음 검사와 저장
│   ├── usage.rs           # usage 객체를 저장할 행으로 해석
│   ├── report.rs          # 대시보드용 집계
│   ├── pricing.rs         # 단가표와 비용 추정
│   ├── auth.rs            # 사용자, 토큰, 브라우저 세션
│   ├── db.rs              # SQLite 연결과 스키마
│   └── timeutil.rs        # 시각 해석, UTC 오프셋, 날짜 경계
├── tests/http.rs          # 실제 라우터를 거치는 HTTP 테스트
└── web/                   # 화면(HTML, CSS, JS). 빌드할 때 실행 파일에 포함됩니다.
```

화면 파일을 고친 뒤에는 다시 빌드해야 반영됩니다.

## 알려진 한계

- 사용자 삭제, 기록 삭제, 사용자 이름 변경 명령이 없습니다.
- 날짜 경계는 고정 UTC 오프셋으로 계산합니다. 서머타임이 있는 시간대에서는 전환 전후로 자정 근처 한 시간의 기록이 이웃 날짜로 들어갈 수 있습니다.
- Claude Code가 내부적으로 쓰는 보조 모델 호출은 기록에 남지 않아 수집되지 않습니다([docs/INGEST_API.md](docs/INGEST_API.md) 참고).
- 응답이 거부되어 대체 모델로 다시 처리된 요청(`usage.iterations`의 `fallback_message`)은 시도마다 행을 저장합니다. 과금되지 않은 시도가 섞여 있으면 비용이 실제보다 높게 추정될 수 있습니다.
