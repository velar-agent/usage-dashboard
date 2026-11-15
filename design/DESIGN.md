# 토큰 사용량 대시보드 — UI 디자인 리뷰 & 개선 명세

작성: 서비스 UI 디자이너 · 대상: `/workspace/token-usage/web/server.py` 의 `INDEX_HTML`
시안: `design/index.html` (샘플 데이터 · 외부 의존성은 Pretendard CDN 폰트뿐 · 차트 라이브러리 없음)
스크린샷: `design/screens/*.png` (재생성: `python design/shoot.py` — playwright + google-chrome 필요)

시안은 **그대로 옮겨 쓸 수 있게** 만들었습니다. `index.html`의 `<style>`(토큰·컴포넌트 CSS 전부)와 `<script>`의 `fmt*`, `statusOf`, `urgency`, `drawChart`, 카드/표 템플릿 함수는 server.py의 `fetch('/api/records')` 결과에 바로 붙일 수 있습니다. (샘플 생성기, `?state=` 미리보기, 상단 "디자인 시안" 띠는 제외하세요.)

---

## 1. 문제점 (코드·스크린샷 근거)

### 1-1. 이미 지적된 것
| # | 문제 | 원인 (server.py) | 해결 |
|---|------|------------------|------|
| P1 | PC에서 차트가 세로로 매우 길게 늘어남 | `.chart-box canvas{height:220px}` + Chart.js `responsive:true, maintainAspectRatio:false`. 부모 `.chart-box`에 **고정 높이가 없어** Chart.js가 캔버스 높이를 부모 높이로 다시 읽고 → 부모가 커지고 → 다시 커지는 되먹임 (before-desktop.png). | 차트 래퍼에 **고정 높이(clamp)**, 또는 시안의 인라인 SVG 사용 (§5-3) |
| P2 | raw 영역이 너무 김 | `renderRaw()`가 최대 8개 파일을 **전부 펼쳐서** 각 `pre` 최대 320px → 약 2,900px. | `<details>` 기본 접힘 + 파일 1개씩(select) + `max-height:320px` (§5-5) |

### 1-2. 추가로 발견한 문제
| # | 문제 | 근거 | 영향 |
|---|------|------|------|
| P3 | **한도가 서로 덮어써짐** | `latestByKey()`의 key = `service::metric`. 실제 `processed/2026-11-15.jsonl`에서 antigravity는 `weekly_used_pct`가 **3줄**(note: Gemini Models / Claude and GPT models / Gemini 4 Argon), `session_used_pct`가 2줄 → 카드·차트 데이터셋이 1개로 합쳐져 일부 한도가 사라져 보임(예: 43%짜리 Claude and GPT). | 핵심 정보 누락 |
| P4 | 정보 우선순위 없음 | 카드가 "최근 기록 순"으로 나열. 값 글자는 항상 검정(막대만 색). 사용률 높은 것·초기화 임박한 것을 찾으려면 전부 읽어야 함. | 한눈에 판단 불가 |
| P5 | 임계값이 애매하고 색에만 의존 | `pctColor` 70/90, 막대 색만 변화, 텍스트/아이콘 없음. 트랙(#e8edf3) 대비 낮음. | 접근성 (WCAG 1.4.1, 1.4.11) |
| P6 | 초기화 정보가 읽기 어려움 | `sub` 한 줄에 `2026-11-17 15:42:43 KST`(초 단위, KST 반복)·기록시각·메모를 12.8px 회색으로 뭉침. "얼마나 남았나"를 계산해야 함. | 가장 궁금한 정보가 약함 |
| P7 | 개발자용 영문 메트릭명 노출 | `weekly_used_pct`, `weekly_scoped_used_pct`; 서비스명은 CSS uppercase. | 한국어 UI와 불일치 |
| P8 | 차트 x축이 시간축이 아님 | `labels=[...new Set(rows.map(fmtShort))]` 범주형 → 간격 불규칙해도 균등, 분 단위 라벨, 날짜 경계 불명. antigravity는 5개 시리즈가 한 차트에 몰림. 5시간형 세션은 톱니라 주간과 섞이면 읽기 어려움. y축은 `beginAtZero`뿐이라 서비스 간 스케일 비교 불가. | 추이 파악 어려움 |
| P9 | 60초마다 DOM 전체 재생성 | `load()`가 `charts[svc].destroy()` + `root.innerHTML=""`, 카드·표·raw 전부 `innerHTML` 재생성 → 차트 깜빡임/툴팁 소실, 표·raw 스크롤·펼침 초기화, select 옵션 재생성. | 사용 중 끊김 |
| P10 | 로딩/오류/오래됨 표시 빈약 | 로딩은 텍스트뿐, 실패 시 영문 오류 노출 가능, 성공하면 배너가 사라지고 "마지막 성공 시각" 없음. **서버는 정상인데 로깅 봇이 멈춘 경우(기록이 낡음)는 감지가 없음.** | 낡은 값을 최신으로 오해 |
| P11 | 빈 화면이 3중으로 반복 | before-empty-mobile.png: 같은 "아직 기록이 없습니다" 큰 카드 3개(~450px) + 필터·빈 표 헤더 노출. | 다음 행동이 안 보임 |
| P12 | 다크모드 없음, 색 하드코딩 | `#f8fafc`(th), `#e8edf3`(트랙), `#1e293b`(raw), `#fef2f2`(에러) 등 토큰 밖. Chart.js 격자색 고정. | 야간 사용 불편 |
| P13 | 표가 모바일에서 가로 스크롤 | 모든 `th,td{white-space:nowrap}`, 7열 → 390px에서 4열만 보임. 시각이 초 포함 전체라 가장 넓은 열. 정렬 불가, 500행 하드컷 안내 없음. 같은 시각 스냅샷이 서비스·지표 수만큼 행으로 반복. | 모바일 가독성 |
| P14 | 헤더가 모바일에서 큼 | sticky 헤더 2~3줄 약 100px. | 화면 낭비 |
| P15 | 접근성 | 필터/검색에 `<label>` 없음(placeholder만), 포커스 스타일 없음, 에러 배너 `role` 없음, 차트 대체 텍스트 없음, 터치 영역 < 44px, 표 영역 키보드 스크롤 불가. | WCAG 2.1 AA |
| P16 | (디자인 외) 공개 노출 | 푸터에 서버 경로 `/workspace/token-usage/processed`, raw 원문(수집 스크립트·엔드포인트명)이 URL만 알면 누구나 열람. `token.wle.kr` 이전 시 접근 제어(Cloudflare Access/Basic Auth 등) 권장. | 보안 |

---

## 2. 정보 우선순위 (위 → 아래)

1. **지금 위험한 한도** — 사용률 높은 순 (헤더 상태칩 + KPI 3개 + 카드 맨 앞)
2. **언제 초기화되나** — "2일 22시간 후" 상대시간을 크게, 절대시각(KST)은 작게
3. **데이터가 신선한가** — 헤더 상태칩(정상/오래됨/실패) + 카드별 "N분 전 기록"
4. **추이** — 서비스별, 기본 7일, 0–100% 고정축
5. **기록 표** — 검색/감사용, 모바일은 카드 리스트
6. **원본 파일** — 디버깅용, 기본 접힘

### 카드 정렬 ("긴급한 순", 기본)
```
tier  : 위험(>85) 3 > 주의(60–85) 2 > 여유(<60) 1 > 값 없음 0
1순위 : tier 내림차순
2순위 : value 내림차순
3순위 : reset_at 오름차순 (없으면 맨 뒤)
```
보조 정렬 토글: `긴급한 순`(기본) / `초기화 임박순` / `서비스별`.

### 한도(시리즈) 식별자 — P3 해결
```js
seriesKey = `${service}|${metric}|${note || ""}`
```
카드, 차트 시리즈, "최신 값" 계산 모두 이 key를 사용.

### 표시 이름 매핑 (모르는 값은 원문 그대로)
```js
SVC_NAME = {grok:"Grok", codex:"Codex", claude:"Claude", gemini:"Gemini", antigravity:"Antigravity", muse:"Muse"}
METRIC_LABEL = {
  weekly_used_pct:"주간", session_used_pct:"세션(5시간)", weekly_scoped_used_pct:"주간·모델별",
  weekly_remaining_pct:"주간 남음", tokens_in:"입력 토큰", tokens_out:"출력 토큰", cost:"비용"
}
```
`note`(요금제/모델명)는 카드 둘째 줄에 `주간 · Claude and GPT models` 형태로 이어 붙임.

---

## 3. 레이아웃 · 브레이크포인트

컨테이너: `max-width:1280px; margin:0 auto; padding-inline:16px (≥640px: 24px)`. 본문 시작은 헤더 아래 24px.

| 구간 | 카드 열 | 차트 열 | 기록 | 헤더 |
|------|---------|---------|------|------|
| ≥1280 | 4 | 2 | 7열 표 | 56px, 전체 상태 문구 |
| 960–1279 | 3 | 2 | 7열 표 | 〃 |
| 720–959 | 2 | 1 | 7열 표 | 〃 |
| 560–719 | 2 | 1 | **카드 리스트** | 〃 |
| <560 | 1 (컴팩트) | 1 | 카드 리스트 | 52px, 상태칩은 점+시각, 버튼 44px |

(640 미만에서 헤더 52px·상태칩 축약, 720 미만에서 표→카드 폴백, 560 미만에서 카드 컴팩트.)

- 섹션 간격 32px, 카드 간격 16px(<560: 12px), 카드 안쪽 16px.
- KPI 3개: ≥640 3열 그리드, <640 가로 스크롤(`scroll-snap`, 카드폭 78%).
- 섹션 순서: 한도 현황(KPI → 카드) → 추이 → 최근 기록 → 원본 파일(접힘) → 푸터.
- 헤더(sticky): 로고·제목·`KST` 뱃지 / 상태칩 / 새로고침 / 테마 버튼. 헤더 아래 2px 진행선이 60초 동안 차올라 다음 자동 갱신을 암시(reduced-motion이면 정지).

---

## 4. 디자인 토큰

### 4-1. 색 (대비는 해당 배경 위 실측)
| 토큰 | 라이트 | 다크 | 용도 |
|------|--------|------|------|
| `--bg` | `#F3F5F9` | `#0A0F1C` | 페이지 배경 |
| `--surface` | `#FFFFFF` | `#121929` | 카드/표 |
| `--surface-2` | `#F7F9FC` | `#182137` | th, 보조면, pre |
| `--line` | `#E1E6EE` | `#263250` | 구분선/카드 테두리 |
| `--line-strong` | `#CBD3E0` | `#33415F` | 입력·칩 테두리 |
| `--track` | `#E3E8F0` | `#232E47` | 진행 막대 트랙 |
| `--ink` | `#121A27` (17.5:1) | `#E9EEF7` (15.1:1) | 본문 |
| `--ink-2` | `#3F4B5E` (8.8:1) | `#C3CDDD` (10.9:1) | 보조 본문 |
| `--muted` | `#5B6779` (5.7:1) | `#97A4BA` (7.0:1) | 캡션·라벨 |
| `--accent` / `--accent-soft` | `#1D4ED8` (6.7:1) / `#E7EEFE` | `#7DA9FF` (7.5:1) / `#172447` | 링크, 선택된 세그먼트, 로고 |
| `--ok` / `--ok-bg` | `#047857` / `#E3F4EC` (4.8:1) | `#34D399` / `#0E2F25` (7.5:1) | 여유 |
| `--warn` / `--warn-bg` | `#A84C08` / `#FDEFDC` (5.0:1) | `#FBBF24` / `#36290B` (8.5:1) | 주의 |
| `--bad` / `--bad-bg` | `#B91C1C` / `#FCE8E8` (5.5:1) | `#F87171` / `#3B1719` (5.7:1) | 위험 |
| `--focus` | `#1D4ED8` | `#7DA9FF` | 포커스 링 |
| `--s1…--s5` (차트 선) | `#2563EB #7C3AED #0E7490 #C026D3 #475569` | `#6EA2FF #A78BFA #22D3EE #E879F9 #94A3B8` | 상태색과 겹치지 않게 청/보라/청록/자홍/회색 |
| `--halo` | `#FFFFFF` | `#121929` | 차트 끝값 글자 외곽선 |

막대·칩의 상태색은 **텍스트용 색을 그대로** 막대 채움에도 사용(트랙 대비 3:1↑). 연한 파스텔 막대 금지.

### 4-2. 상태(임계값) — `statusOf(n)`
| 구간 | 상태 | 칩 | 색 |
|------|------|----|----|
| `n < 60` | ok | ✓ "여유" | `--ok` |
| `60 ≤ n ≤ 85` | warn | ▲ "주의" | `--warn` |
| `n > 85` | bad | ⓘ "위험" | `--bad` |
| 숫자 아님/없음 | na | — | `--muted` |

색만으로 구분하지 않도록 칩(아이콘+글자)이 항상 함께 있음. `warn`/`bad` 카드는 큰 숫자도 상태색, `bad` 카드는 배경 `--bad-bg` 35% 틴트 + 테두리 강조. 임계값은 상수 `THRESH={warn:60,bad:85}` 한 곳에서만 정의.

### 4-3. 타이포 (Pretendard Variable, CDN)
```html
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
```
`font-family:"Pretendard Variable",Pretendard,-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",system-ui,sans-serif`
숫자는 `font-variant-numeric:tabular-nums` (갱신 때 자릿수 흔들림 방지). 현재 쓰는 static CSS보다 variable dynamic-subset이 전송량이 작음.

| 역할 | 크기/행간 | 굵기 | 비고 |
|------|-----------|------|------|
| 큰 수치(카드) | 32/36 (모바일 28/32) | 700 | `letter-spacing:-0.03em`, `%`는 16px/600 |
| H1 | 20/28 | 700 | `-0.02em` |
| H2 | 18/26 | 700 | 섹션 제목 |
| H3 / 카드 서비스명 | 15/22 | 600–700 | |
| 본문 | 14/22 | 400 | 기본 |
| 작은 글씨 | 13/18 | 400–500 | 보조 설명, 표 본문 |
| 캡션 | 12/16 | 400–700 | **최소 12px** |
| 모노(raw) | 12.5/1.6 | 400 | `ui-monospace, SFMono-Regular, Menlo, Consolas` |

### 4-4. 간격·모서리·그림자
- 간격(4px 스케일): `4 · 8 · 12 · 16 · 24 · 32 · 48` → `--sp-1…--sp-7`.
- 모서리: 입력/칩 8px, KPI/작은 카드 12px, 카드/표/차트 16px, 알약 999px.
- 그림자: 라이트 `0 1px 2px rgba(16,24,40,.05), 0 4px 16px rgba(16,24,40,.05)`, 다크는 **없음**(테두리로 구분).
- 터치 영역: 데스크톱 버튼/입력 36–40px, **모바일 44px 이상**.

### 4-5. 다크모드 / 테마 전환
- 기본 `prefers-color-scheme` 자동. 헤더 버튼으로 `자동 → 라이트 → 다크` 순환, `localStorage["tu-theme"]`에 저장, `<html data-theme="light|dark">` (자동이면 속성 제거).
- CSS: `:root`(라이트) + `:root[data-theme="dark"]` + `@media (prefers-color-scheme:dark){ :root:not([data-theme="light"]){…} }` 세 블록에 **같은 토큰 이름**, 값만 교체(시안 상단 그대로 복사). `color-scheme`도 지정해 폼 컨트롤/스크롤바 맞춤.
- 첫 페인트 깜빡임 방지: `<head>`에서 CSS 앞에 `localStorage` 값을 읽어 `data-theme`를 먼저 세팅하는 한 줄 스크립트.
- 차트 색은 모두 `var(--…)` → 테마 전환 시 `renderCharts()`만 다시 호출(Chart.js 유지 시에는 `getComputedStyle`로 색을 읽어 재생성).

---

## 5. 컴포넌트 명세

### 5-1. 헤더 + 상태칩 (60초 갱신 표시)
상태칩 `#status` (`role="status" aria-live="polite"`, 높이 32px, 알약):

| 상태 | 점/칩 색 | 문구 (PC / 모바일) | 조건 |
|------|----------|---------------------|------|
| 정상 | `--ok` | `정상 · 16:07 갱신` / `16:07` | 최근 fetch 성공 & 최신 기록 나이 < 60분 |
| 로딩 | `--muted` 깜빡임 | `불러오는 중…` / `로딩` | 첫 로드만 (이후 자동 갱신은 조용히) |
| 오래됨 | `--warn` (칩 배경 `--warn-bg`) | `기록 오래됨 · 3시간 14분 전 기록` / `오래됨` | 서버 응답은 성공했지만 **가장 최근 `ts`가 60분 이상 전** (로깅 봇 중단 의심) |
| 실패 | `--bad` (칩 배경 `--bad-bg`) | `갱신 실패 · 마지막 성공 16:03` / `실패` | fetch 오류/비-200 |

- **실패 시 이전 데이터를 지우지 말고 유지**하고 상단 배너(`role="alert"`, `--bad-bg`): "서버에 연결하지 못했어요. 16:03 기준 마지막으로 받은 값을 보여 주고 있어요. 60초 뒤 자동으로 다시 시도합니다." + `[지금 다시 시도]`. 영문 오류 문자열은 노출하지 말고 한국어 고정 문구.
- **오래됨** 배너(`role="status"`, `--warn-bg`): "3시간 넘게 새 기록이 없어요. 로깅 봇이 멈췄을 수 있어요. 아래 값은 11/15(일) 16:05 기준이라 실제와 다를 수 있습니다."
- 카드별 기록 나이: `<60분` 기본 / `60분–6시간` `--warn`+⚠ / `≥6시간` `--bad`+⚠. (현재 로깅 주기가 20–40분 간격이라 60분 기준. 바뀌면 상수 `STALE_WARN_MS` 수정.)
- 새로고침 아이콘 버튼(36px, 모바일 44px, `aria-label="지금 새로고침"`)으로 즉시 갱신. 진행선은 갱신 성공마다 CSS 애니메이션 재시작.
- `document.hidden`이면 폴링 중지, 다시 보이면 즉시 1회 갱신.

### 5-2. 한도 카드 (`.card`)
구조(위→아래), 안쪽 16px, 요소 간 8px:
1. **헤더**: 서비스명(15px/700) · 우측 상태 칩(높이 22px, 12px/700).
2. **종류줄**: `주간 · Claude and GPT models` (13px; 지표=`--ink-2`, 메모=`--muted`, 한 줄 말줄임).
3. **값줄**: 좌 `91 %`(32px/700) · 우 **초기화 상대시간**(14px/700 `--ink`) + 아래 `11/18(수) 14:10 초기화`(13px `--ink-2`). 시각 없으면 `—` / "초기화 시각 없음".
4. **진행 막대**: 높이 8px, `--track` 위 상태색 채움, 최소 너비 4px(0%도 보이게), `role="progressbar" aria-valuenow aria-label="Antigravity 주간 Claude and GPT models 사용률 91퍼센트, 위험"`. **페이스 마커**: 2px×14px 세로선 = 한도 기간 중 현재까지 경과한 비율. 채움이 마커보다 앞서면 기간 대비 빨리 쓰는 중. 기간은 `weekly*`=7일, `session*`=5시간으로 가정(§8의 `period_start` 제안 참고).
5. **푸터**(12px `--muted`): 좌 `기간 58% 경과`, 우 `2분 전 기록`(낡으면 상태색+⚠). 모바일(<560)은 기록이 신선하면 푸터 전체 생략.

- 시각 포맷: **`M/D(요일) HH:mm`** (초·연도 생략, "KST"는 헤더 뱃지에 한 번만). 정확한 값은 `title="2026-11-17 15:42 KST"`.
- 상대시간: `≥1일 "N일 M시간 후"` / `≥1시간 "N시간 M분 후"` / `≥1분 "N분 후"` / `<1분 "곧"` / 지남 `"초기화 시각 지남"`. **초기화 시각이 지났는데 기록이 그 이전 것이면** 카드를 `na` 톤(회색)으로 두고 "초기화됨 · 새 값 대기 중"으로 교체(낡은 91%가 계속 위험으로 보이지 않게).
- **카운트다운 갱신**: 서버 갱신(60초)과 별개로 클라이언트에서 30초마다 상대시간 **텍스트만** 재계산(DOM 전체 재생성 금지).
- KPI(카드 위, 높이 약 64px): "가장 높은 사용률 / 가장 가까운 초기화 / 주의 필요 (위험 n · 주의 m)".
- 카드 아래 범례줄(12px): 세로선 의미 + `여유 <60% · 주의 60–85% · 위험 >85%`.

### 5-3. 추이 차트 (고정 비율, 라이브러리 없음)
- 서비스당 카드 1개. 그리드 ≥960 2열, 그 미만 1열. 같은 줄 카드의 헤더/범례/플롯 높이가 맞도록 `grid-row:span 3; grid-template-rows:subgrid`.
- **높이**: `H = clamp(180, round(W × 0.42), 260)` px (W = 플롯 컨테이너 실제 너비). 590px 폭이면 248px, 모바일 358px 폭이면 180px. **부모 높이에 의존하지 않음** → 되먹임 없음. `ResizeObserver`(80ms debounce)로 재그림.
- **구현 선택지**
  1. (권장) 시안의 `drawChart()` 사용: `viewBox="0 0 W H"`를 **실제 px 너비**로 만들어 글자가 축소되지 않는 인라인 SVG. 차트 CDN 제거, 테마 색 변수 직접 사용.
  2. Chart.js 유지 시: `<div class="plot" style="position:relative;height:clamp(180px,42vw,260px)"><canvas></canvas></div>`로 **캔버스를 고정 높이 래퍼 안에**, `maintainAspectRatio:false`, `canvas` CSS 높이 제거, `type:'time'` 축(date adapter 필요) 또는 `{x:ts,y}` 수치 축, `scales.y:{min:0,max:100}`, 갱신은 `chart.data=…; chart.update('none')` (destroy 금지).
- **축**: y = **0–100% 고정**(서비스 간 비교 가능), 눈금 0/25/50/75/100. 60%·85%에 점선(`--warn`/`--bad`, 투명도 .7). x = **실제 시간(KST)**. 기간 세그먼트 `24시간 / 7일(기본) / 전체`. 눈금 간격은 최소 64px(좁으면 1→2→3→7일 간격으로 성기게), 24시간은 3/6/12시간 `HH:00`, 그 외 `M/D`.
- **시리즈**: key=`seriesKey`. 서비스 내 색은 `--s1…--s5` 순서 + **선 모양**(실선, `7 4`, `2 3`, `9 3 2 3`, `1 4`) → 색을 못 봐도 구분. 선 2px, 끝점 원 r=3.5 + 값 라벨(11px/700, 시리즈색, `--halo` 외곽선; 13px 이내로 겹치면 아래로 밀어냄).
- **범례 = 토글 칩**(`button aria-pressed`, 높이 28px/모바일 36px, 선모양 샘플 + "주간 · 모델명"). 꺼진 칩은 흐리게+취소선. **`session_*` 시리즈는 기본 꺼짐**(톱니 모양이라 주간 흐름을 가림). 상태는 `seriesKey`별 메모리 유지(갱신 후에도). 모바일은 가로 스크롤 1줄.
- **툴팁**: 포인터 이동 시 가장 가까운 시각(±3시간)의 값을 세로 가이드선 + 말풍선(시각 + 시리즈별 값). 터치도 `pointermove`로 동일.
- **접근성**: `<svg role="img" aria-label="Codex 사용률 추이. 주간 · ChatGPT prolite 현재 72퍼센트">`; 세부 수치는 아래 기록 표가 텍스트 대안.
- **데이터가 적을 때**(현재 실제 상황: 시리즈당 1건): 모든 시리즈가 1건 이하이면 빈 차트 4개 대신 **안내 카드 1개**(`desktop-sparse-light.png`). 일부 서비스만 부족하면 해당 서비스 자리에 같은 높이의 점선 안내 박스.
- **주기 초기화**: 같은 시리즈에서 값이 감소(예 91→3)하면 선을 이어 그리지 말고 **끊기**(새 `M` 경로). 시안 샘플은 한 주기뿐이라 미구현이나 실데이터에선 필요.
- 카드 헤더 우측 보조값: `최고 91%`.

### 5-4. 기록 표
- 툴바(그리드): `서비스` select 160px · `지표` select 160px · `검색` input(가변) · 건수(`631건`, `aria-live`). **각 컨트롤에 보이는 `<label>`**(12px/600 `--muted`). select 옵션 텍스트는 표시 이름, value는 원문. 필터/검색 변경 시 `shown` 초기화.
- 표 래퍼: `max-height:520px; overflow:auto; tabindex=0; role="region"; aria-label="기록 표 (스크롤 가능)"`, 모서리 16px.
- **sticky 헤더**: `th{position:sticky; top:0; background:var(--surface-2)}`(하드코딩 `#f8fafc` 금지), 아래 테두리 `--line-strong`. `border-collapse:separate; border-spacing:0`(sticky 테두리 깨짐 방지).
- 열: `시각 · 서비스 · 지표 · 사용률 · 초기화 · 메모 · 출처`. 패딩 `10px 12px`, 13px/18px, 행 hover `--surface-2`. 시각 `M/D(요일) HH:mm`(`title`에 전체), 서비스 600, **사용률은 숫자 700 + 44×6px 미니 막대(상태색)**, 메모 `max-width:260px` 줄바꿈 허용, 출처 말줄임. 비숫자 값(토큰/비용)은 미니 막대 없이 `toLocaleString`.
- **페이지네이션**: 처음 30건(모바일 10건), 하단 `[더 보기 (50건)]`로 +50. 서버가 500건에서 자르면 "최근 500건만 표시" 문구 명시.
- 선택 기능: `값이 바뀐 기록만 보기` 체크박스(로깅이 같은 값을 주기적으로 반복 기록해 표 노이즈가 큼). 각 행 `원본` 링크 = `raw_file` 필드 → 원본 `<details>`를 열고 해당 파일 선택.
- **모바일 카드 폴백(<720px)**: CSS만으로 `thead{display:none}`, `tr`을 2열 그리드 카드(`padding:12px 16px; border-radius:12px; margin-bottom:8px; background:--surface`)로.
  - 1행: 좌 서비스명(15px/600) · 우 사용률(22px/700, 2행 걸침)
  - 2행: 지표(`--muted`) / 3행: 시각(12px `--muted`)
  - 4–6행: `초기화 / 메모 / 출처` — `td::before{content:attr(data-label)}`로 라벨 표시. 값이 비면 행 숨김(`td:empty`).
  - 표 내부 스크롤 제거(`max-height:none`), 페이지 스크롤 사용. 각 `td`에 `data-label` 필수.
- 결과 없음: "조건에 맞는 기록이 없어요. [필터 지우기]".

### 5-5. 원본 파일 (`<details class="raw">`, 기본 접힘)
- 접힌 높이 52px(모바일 56px): `▸ 원본 파일` + 우측 `3개 · 펼쳐서 보기`(모바일 숨김). `summary`는 키보드 포커스 링 필수.
- 펼치면 상단 툴바 = **파일 select**(최신순, 최근 8개) · `줄바꿈` 체크박스 · 우측 `2026-11-15 16:05 KST · 372바이트`. 아래 `pre.raw`: `max-height:320px; overflow:auto; tabindex=0; border-radius:12px; background:--surface-2; 12.5px 모노`, 기본 `white-space:pre`(가로 스크롤), `줄바꿈` 체크 시 `pre-wrap; word-break:break-all`.
- 안내(12px): "원본은 수집기가 남긴 그대로라서 시각이 UTC(+00:00 / Z)일 수 있어요. 최근 8개 파일, 파일당 최대 64KB까지 보여 줍니다." (실제 raw의 `period_end`/`reset_at`은 UTC라 KST 표와 달라 보임)
- **성능 제안**: `/api/raw`가 8×64KB를 60초마다 전송. 접힘 상태에서는 목록(이름·크기·mtime)만 받고 열었을 때/파일 선택 시 본문을 `/api/raw?name=…`으로 가져오면 트래픽·렌더 비용 감소. 갱신 때 열림 상태·선택 파일·스크롤 위치 유지(P9).

### 5-6. 빈 화면 / 로딩 / 오류
- **빈 화면**(기록 0건): 한도 현황 자리에 **큰 안내 카드 1개만**(KPI·정렬·`추이`·`최근 기록` 숨김, `원본 파일`은 접힌 채 "없음"). 구성: 72px 아이콘 타일(`--accent-soft`) → "아직 기록이 없어요"(18px/700) → 설명(14px, 폭 46자 이내) → 번호 단계 2개(① 로깅 봇이 `processed/YYYY-MM-DD.jsonl`에 한 줄 추가 + **예시 JSON 한 줄** 코드블록(가로 스크롤, 부모에 `min-width:0` 필수) ② 60초마다 자동 반영) → `[지금 새로고침]` → "기록 형식은 FORMAT.md…". 서버 절대경로는 노출하지 말고 상대경로만.
- **로딩(첫 진입)**: KPI·카드·차트·표 자리에 같은 크기의 **스켈레톤**(`--track → --surface-2` 시머 1.4s, reduced-motion이면 정지). 카드 높이 고정으로 레이아웃 이동 최소화.
- **부분 실패**: `/api/raw`만 실패하면 raw 영역에만 한 줄 오류, 나머지는 정상 표시(현재처럼 조용히 빈 목록 처리하지 말 것).
- `skipped`가 있으면 푸터에 "형식이 잘못된 줄 N개를 건너뛰었어요" 작게 표시.

### 5-7. 모바일 처리 요약
- 헤더 52px 한 줄(로고·제목 / 상태칩(점+시각) / 새로고침 / 테마), `KST` 뱃지는 숨김(표·카드 시각은 모두 KST임을 상태칩 `title`로 안내).
- KPI 가로 스크롤 → 카드 1열 컴팩트(padding 12/16, 숫자 28px, 신선한 기록은 푸터 생략) → 차트 1열(높이 180) + 범례 가로 스크롤 → 필터 2열 + 검색 전폭 → 카드형 기록(10건씩) → 접힌 원본.
- 모든 탭 대상 44px 이상. 세그먼트 버튼 40px+. `viewport` 확대 허용 유지.

### 5-8. 접근성 체크리스트
- 텍스트 대비 ≥ 4.5:1(토큰 표 수치), 그래픽(막대·선) ≥ 3:1.
- 상태는 **색 + 아이콘 + 글자**, 시리즈는 **색 + 선 모양**.
- 포커스: `:focus-visible{outline:2px solid var(--focus); outline-offset:2px}` 전역. `summary`, 스크롤 영역(`tabindex=0`), 세그먼트 버튼 모두 키보드 도달 가능. 스킵 링크 `본문으로 건너뛰기`.
- 라이브 영역: 상태칩 `role=status aria-live=polite`, 오류 배너 `role=alert`, 건수 `aria-live=polite`. **60초 자동 갱신 때 스크린리더가 전체를 다시 읽지 않도록** 실제로 바뀐 노드만 갱신.
- 진행 막대 `role=progressbar` + `aria-valuenow` + 설명형 `aria-label`. 장식 SVG는 `aria-hidden`.
- `prefers-reduced-motion: reduce`에서 진행선·스켈레톤·펄스 애니메이션 제거.
- 표: `<th scope="col">`, 모바일 카드에서도 `data-label`로 의미 유지.

---

## 6. 구현 순서 제안 (빌더용)
1. 토큰 CSS(라이트/다크) + 타이포 + 헤더/상태칩/테마 버튼 교체.
2. `seriesKey` 도입(P3) + 카드 템플릿/정렬/KPI/임계값/상대시간.
3. 차트: `drawChart` 도입(또는 고정 높이 래퍼) + 범례 토글 + 기간 선택. Chart.js `<script>` 제거 가능.
4. 표: 라벨 있는 툴바, sticky 헤더, 더 보기, 모바일 카드 CSS.
5. raw: `<details>` + 파일 select + 지연 로드.
6. 갱신 로직: 부분 갱신(상태 유지), 실패/오래됨/로딩/빈 상태, `document.hidden` 일시정지.
7. 푸터의 서버 경로 제거, 외부 접근 제어(token.wle.kr 이전 시).

## 7. 파일 목록
- `design/index.html` — 상호작용 시안 (샘플 데이터. `?state=ok|sparse|loading|stale|error|empty`, `?theme=light|dark`, `?range=24h|7d|all`, `?raw=open`)
- `design/DESIGN.md` — 이 문서
- `design/shoot.py` — 스크린샷 재생성 스크립트
- `design/screens/` — 렌더 결과

| 파일 | 내용 |
|------|------|
| `desktop-light.png` / `desktop-dark.png` | PC 1440 전체, 정상 |
| `mobile-light.png` / `mobile-dark.png` | 모바일 390 전체 |
| `tablet-light.png` | 820 |
| `desktop-empty-light/dark.png`, `mobile-empty-light/dark.png` | 빈 화면 |
| `desktop-loading-light.png` | 스켈레톤 |
| `desktop-error-light.png`, `mobile-error-light.png` | 갱신 실패 + 이전 값 유지 |
| `desktop-stale-dark.png` | 기록 오래됨 |
| `desktop-sparse-light.png` | 시리즈당 기록 1건(현재 실제 상황) |
| `desktop-raw-open-light.png` | 원본 파일 펼침 |

## 8. 로깅 봇 / FORMAT.md 제안 (선택)
- processed에도 `period_start`(또는 `window:"7d"|"5h"`)를 남기면 페이스 마커가 정확해짐 (raw에는 이미 `period_start`/`period_end` 있음).
- 같은 한도를 가리키는 줄은 `note`가 항상 같아야 시리즈가 끊기지 않음(모델명 표기 고정). 한도 구분용 `scope` 필드(선택)를 추가하면 더 견고.
- raw 시각도 `+09:00`으로 통일하면 raw 보기의 UTC 혼란이 사라짐.
