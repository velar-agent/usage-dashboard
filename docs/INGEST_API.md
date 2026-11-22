# 수신 API

토큰 수집 프로세스가 사용 기록을 대시보드 서버로 보낼 때 따르는 규칙입니다. 수집 프로세스는 이 저장소에 없으며, 이 문서에 맞춰 별도 저장소에서 만듭니다.

## 한눈에 보기

- 수집 프로세스는 Claude API 응답 하나(모델 호출 한 번)를 이벤트 하나로 보냅니다.
- 이벤트에는 응답의 `usage` 객체를 **받은 그대로** 넣습니다. 해석은 서버가 합니다.
- 대화 내용, 프롬프트, 파일 경로는 보내지 않습니다. 받는 필드도 없습니다.
- 같은 이벤트를 여러 번 보내도 한 번만 저장됩니다. 실패하면 그대로 다시 보내면 됩니다.

## 인증

관리자가 서버에서 사용자를 등록하고 토큰을 발급합니다.

```sh
usage-dashboard --db /path/to/usage.db user add 홍길동 --label 맥북
```

토큰은 `udt_`로 시작하며 발급할 때 한 번만 표시됩니다. 서버에는 해시만 저장되므로 잃어버리면 새로 발급해야 합니다(`token add`). 한 사용자가 컴퓨터마다 다른 토큰을 쓸 수 있고, 토큰 하나를 폐기해도(`token revoke`) 다른 토큰에는 영향이 없습니다.

모든 요청에 다음 헤더를 붙입니다. 토큰은 URL에 넣지 않습니다.

```
Authorization: Bearer udt_...
```

서버는 토큰으로 사용자를 식별합니다. 이벤트 안에 사용자 이름을 적는 필드는 없으므로, 다른 사람 이름으로 보낼 수 없습니다.

토큰이 맞는지는 다음 요청으로 확인할 수 있습니다.

```sh
curl -H "Authorization: Bearer $TOKEN" https://dashboard.example.com/api/v1/me
# {"user":"홍길동"}
```

## POST /api/v1/events

### 요청

```json
{
  "events": [
    {
      "id": "msg_01ExampleAbCdEfGhIjKlMnOp",
      "timestamp": "2026-11-20T01:23:45.678Z",
      "model": "claude-fable-5-2",
      "source": "claude-code",
      "session_id": "11111111-2222-4333-8444-555555555555",
      "project": "my-project",
      "host": "macbook",
      "usage": {
        "input_tokens": 12,
        "output_tokens": 850,
        "cache_creation_input_tokens": 15000,
        "cache_read_input_tokens": 42000,
        "cache_creation": {
          "ephemeral_5m_input_tokens": 0,
          "ephemeral_1h_input_tokens": 15000
        },
        "output_tokens_details": { "thinking_tokens": 300 },
        "server_tool_use": { "web_search_requests": 0 },
        "service_tier": "standard",
        "speed": "standard",
        "inference_geo": "global",
        "iterations": null
      }
    }
  ]
}
```

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `id` | 예 | 이벤트의 고유 id입니다. 사용자별로 유일해야 합니다. Anthropic 메시지 id(`msg_...`)를 그대로 쓰는 것을 권장합니다. 200자 이하. |
| `timestamp` | 예 | 응답을 받은 시각입니다. RFC 3339 형식이며 시간대 표기(`Z` 또는 `+09:00`)가 있어야 합니다. |
| `model` | 예 | 응답의 모델 id입니다. 예: `claude-fable-5-2`. 100자 이하. |
| `usage` | 예 | 응답의 `usage` 객체입니다. 아래 "usage 객체"를 참고하십시오. |
| `source` | 아니요 | 사용량을 만든 프로그램입니다. 예: `claude-code`, `agent-sdk`. 대시보드에서 출처별로 나눠 봅니다. |
| `session_id` | 아니요 | 같은 작업 세션의 이벤트를 묶는 값입니다. 대시보드의 세션 표에 쓰입니다. |
| `project` | 아니요 | 프로젝트 이름입니다. 대시보드에 로그인한 모든 사람이 볼 수 있으므로, 공개하고 싶지 않으면 보내지 않습니다. 전체 경로 대신 폴더 이름만 보내는 것을 권장합니다. |
| `host` | 아니요 | 보낸 컴퓨터를 구분하는 이름입니다. 현재 화면에는 표시되지 않습니다. |

선택 필드는 각각 200자 이하이고 제어 문자를 넣을 수 없습니다. 목록에 없는 필드는 무시합니다.

### usage 객체

Anthropic Messages API 응답의 `usage`를 수정하지 않고 넣습니다. 서버가 읽는 필드는 다음과 같고, 나머지는 무시합니다. 값이 `null`이거나 없으면 0으로 봅니다.

| 필드 | 용도 |
| --- | --- |
| `input_tokens` | 캐시되지 않은 입력 토큰 |
| `output_tokens` | 출력 토큰(사고 토큰 포함) |
| `cache_creation_input_tokens` | 캐시 쓰기 토큰 합계 |
| `cache_creation.ephemeral_5m_input_tokens`, `cache_creation.ephemeral_1h_input_tokens` | 캐시 쓰기의 보관 시간별 구분. 단가가 다르므로 이 구분을 기준으로 집계합니다. 합계가 구분의 합보다 크면 남는 만큼을 5분 보관으로 계산하고(구분이 아예 없으면 전부 5분 보관), 구분의 합이 합계보다 크면 구분을 그대로 씁니다. |
| `cache_read_input_tokens` | 캐시 읽기 토큰 |
| `output_tokens_details.thinking_tokens` | 출력 중 사고 토큰 |
| `server_tool_use.web_search_requests` | 웹 검색 횟수(건당 과금) |
| `speed` | `"fast"`이면 빠른 모드 단가를 적용합니다. |
| `service_tier` | `"batch"`이면 배치 할인 단가를 적용합니다. |
| `inference_geo` | `"us"`이면 미국 내 추론 할증을 적용합니다. |
| `iterations` | 응답 하나 안에서 일어난 추론 목록. 아래 설명을 참고하십시오. |

토큰 수는 0 이상의 정수여야 합니다. 음수, 소수, 문자열은 그 이벤트를 거부합니다.

#### iterations

`usage.iterations`가 있으면 서버는 그 목록을 추론 단위의 기준으로 삼아 항목마다 행을 하나씩 저장합니다. 최상위 숫자는 `type`이 `"message"`인 항목만 합친 값이라서, 최상위만 읽으면 어드바이저 호출(`"advisor_message"`, 다른 모델이며 따로 과금됨) 같은 항목이 빠지기 때문입니다. 항목에 `model`이 있으면 그 모델로, 없으면 이벤트의 `model`로 집계합니다.

`iterations`가 없거나 `null`이면 최상위 값을 행 하나로 저장합니다. 수집 프로세스는 이 구분을 신경 쓸 필요 없이 `usage`를 그대로 보내면 됩니다.

### 응답

봉투(`{"events": [...]}`)가 올바르면 항상 `200`으로 응답하고, 이벤트별 결과를 알려 줍니다.

```json
{
  "received": 3,
  "inserted": 1,
  "duplicates": 1,
  "rejected": [
    { "index": 2, "id": "msg_bad", "error": "missing field `timestamp`" }
  ]
}
```

| 필드 | 뜻 |
| --- | --- |
| `received` | 요청에 들어 있던 이벤트 수 |
| `inserted` | 새로 저장한 이벤트 수 |
| `duplicates` | 이미 저장되어 있어 건너뛴 이벤트 수 |
| `rejected` | 저장하지 않은 이벤트. `index`는 요청 배열에서의 위치(0부터)입니다. |

잘못된 이벤트가 있어도 나머지는 저장합니다. `rejected`에 든 이벤트는 다시 보내도 같은 이유로 거부되므로 재시도하지 말고, 수집 프로세스의 로그에 남긴 뒤 넘어가십시오.

### 오류

오류 응답의 본문은 `{"error": "설명"}`입니다.

| 상태 | 원인 | 수집 프로세스가 할 일 |
| --- | --- | --- |
| `400` | 본문이 JSON이 아니거나 `events` 배열이 없음, 이벤트가 너무 많음 | 재시도하지 않습니다. 요청을 고칩니다. |
| `401` | 토큰이 없거나 틀렸거나 폐기됨 | 재시도하지 않습니다. 토큰을 확인합니다. |
| `413` | 본문이 너무 큼 | 묶음을 더 작게 나눠 보냅니다. |
| `5xx`, 연결 실패 | 서버 문제 | 같은 묶음을 간격을 두고 다시 보냅니다. |

### 제한

- 요청 하나에 이벤트 5,000개까지(`--max-events`로 변경).
- 요청 본문 16MB까지(`--max-body-mb`로 변경).
- `timestamp`가 2020-01-01보다 이전이거나 서버 시각보다 이틀 넘게 미래이면 거부합니다.
- 응답 하나의 `iterations`는 64개까지.

## 중복 처리

저장 키는 (사용자, `id`)입니다. 같은 `id`가 다시 오면 **처음 받은 값이 유지**되고 나중 값은 버립니다. 그러므로

- 응답이 끝나 사용량이 확정된 뒤에 보냅니다. 스트리밍 도중의 중간 값은 보내지 않습니다.
- 전송 성공 여부가 불확실하면(타임아웃 등) 같은 묶음을 그대로 다시 보냅니다. 중복은 서버가 걸러 냅니다.
- 어디까지 보냈는지 기록을 잃었다면 처음부터 전부 다시 보내도 결과가 같습니다.

## Claude Code 기록에서 이벤트 만들기

Claude Code는 `~/.claude/projects/<프로젝트>/<세션 id>.jsonl`에 한 줄에 JSON 하나씩 기록합니다. 사용량은 `type`이 `"assistant"`인 줄에 있습니다.

| 이벤트 필드 | 기록의 값 |
| --- | --- |
| `id` | `message.id` |
| `timestamp` | `timestamp` |
| `model` | `message.model` |
| `usage` | `message.usage` (그대로) |
| `session_id` | `sessionId` |
| `project` | `cwd`의 마지막 폴더 이름 |
| `source` | `"claude-code"` |

주의할 점입니다.

- `message.model`이 `"<synthetic>"`인 줄은 실제 API 호출이 아니므로 건너뜁니다.
- 응답 하나가 내용 블록마다 한 줄씩 기록되어 같은 `message.id`가 여러 줄에 나옵니다. 확인한 기록에서는 `usage`가 줄마다 같았으므로 전부 보내도 되지만(서버가 중복을 거름), 수집 프로세스에서 한 번만 보내면 전송량이 줄어듭니다.
- Claude Code가 내부적으로 쓰는 보조 모델(Haiku) 호출은 이 기록에 줄로 남지 않아 수집되지 않습니다. 그래서 대시보드의 비용은 Claude Code가 세션마다 계산하는 총비용보다 조금 적습니다. 확인한 세션 28개에서 그 차이는 대부분 0.1센트였고 가장 큰 경우가 5센트(세션 비용의 1% 미만)였습니다.

이 매핑은 이 저장소를 만들 때 실제 기록 760여 건(2026년 8월~11월)을 보내 검증했습니다. 토큰 합계는 기록과 일치했고, 비용 기록이 남아 있는 세션 32개에서는 본 모델의 추정 비용이 Claude Code가 기록한 비용과 일치했습니다.

## 예시

```sh
curl -sS https://dashboard.example.com/api/v1/events \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "events": [{
      "id": "msg_01ExampleAbCdEfGhIjKlMnOp",
      "timestamp": "2026-11-20T01:23:45.678Z",
      "model": "claude-fable-5-2",
      "source": "claude-code",
      "session_id": "11111111-2222-4333-8444-555555555555",
      "project": "my-project",
      "usage": {"input_tokens": 12, "output_tokens": 850,
                "cache_creation_input_tokens": 15000, "cache_read_input_tokens": 42000,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 15000}}
    }]
  }'
# {"received":1,"inserted":1,"duplicates":0,"rejected":[]}
```

## 조회 API

대시보드 화면이 쓰는 API이며, 스크립트에서도 같은 토큰으로 부를 수 있습니다.

```
GET /api/v1/report?from=2026-11-01&to=2026-11-22&tz=540&user=홍길동&model=claude-fable-5-2&project=my-project
```

| 파라미터 | 설명 |
| --- | --- |
| `from`, `to` | 조회 기간(양 끝 포함, `YYYY-MM-DD`). `from`을 빼면 첫 기록부터, `to`를 빼면 오늘까지입니다. 최대 1,100일. |
| `tz` | 날짜를 자를 UTC 오프셋입니다. 분 단위 숫자(`540`)나 `+09:00` 형식. 빼면 서버의 `--utc-offset` 값을 씁니다. |
| `user`, `model`, `project` | 값이 정확히 일치하는 기록만 집계합니다. 파라미터를 빼면 전체입니다. `project=`(빈 값)는 프로젝트를 보내지 않은 기록만 고릅니다. |

기간이 2일 이하이면 시간 단위, 그보다 길면 일 단위로 묶은 `buckets`를 돌려줍니다. 응답에는 합계(`totals`), 직전 같은 길이 기간의 합계(`previous_totals`), 사용자·모델·프로젝트·출처별 집계, 비용이 큰 세션 20개가 들어 있습니다.
