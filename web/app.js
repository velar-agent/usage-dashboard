// Dashboard page logic: filters, one report request per change, and rendering every section
// from that single response so all numbers on the page describe the same slice.
// 대시보드 화면 로직입니다. 필터를 다루고, 조건이 바뀔 때마다 리포트를 한 번 요청하며, 그 응답
// 하나로 모든 영역을 그립니다. 그래서 화면의 모든 숫자가 같은 조회 범위를 가리킵니다.

import {
  addDays, compact, daysBetween, duration, full, longDay, percent, shortDateTime, shortDay,
  todayLocal, usd, usdTicks, utcOffsetLabel, weekStart,
} from './format.js';
import { columnChart, el, hideTooltip, segmentedBar } from './charts.js';

/** Select value that means "no filter". / "필터 없음"을 뜻하는 선택 값입니다. */
const ALL = '__all__';
/** Key the server gives the row that folds the long tail of a breakdown. */
/** 서버가 분류별 집계의 긴 꼬리를 합친 행에 붙이는 키입니다. */
const OTHERS_KEY = '(others)';
const PRESETS = ['today', '7d', '30d', '90d', 'all'];
const VISIBLE_ROWS = 10;
const REFRESH_AFTER_MS = 60_000;

// Color means token type everywhere on the page, in this fixed order. A filter never repaints.
// 이 화면에서 색은 언제나 토큰 종류를 뜻하며 순서는 고정입니다. 필터를 바꿔도 색은 바뀌지 않습니다.
const TOKEN_TYPES = [
  { name: '입력', cls: 's1', tokens: (a) => a.input_tokens, cost: (a) => a.cost.input },
  { name: '출력', cls: 's2', tokens: (a) => a.output_tokens, cost: (a) => a.cost.output },
  { name: '캐시 쓰기', cls: 's3', tokens: (a) => a.cache_write_5m_tokens + a.cache_write_1h_tokens, cost: (a) => a.cost.cache_write },
  { name: '캐시 읽기', cls: 's4', tokens: (a) => a.cache_read_tokens, cost: (a) => a.cost.cache_read },
];
const WEB_SEARCH = { name: '웹 검색', cls: 's5', cost: (a) => a.cost.web_search };

const $ = (id) => document.getElementById(id);

const state = { preset: '30d', from: '', to: '', user: null, model: null, project: null };
let requestCounter = 0;
let lastLoadedAt = 0;

// ---------------------------------------------------------------------------------------------
// Aggregates
// 집계 값
// ---------------------------------------------------------------------------------------------

function emptyAgg() {
  return {
    requests: 0, input_tokens: 0, output_tokens: 0, cache_write_5m_tokens: 0, cache_write_1h_tokens: 0,
    cache_read_tokens: 0, thinking_tokens: 0, web_search_requests: 0, cost_usd: 0, unpriced_requests: 0,
    cost: { input: 0, output: 0, cache_write: 0, cache_read: 0, web_search: 0 },
  };
}

function addAgg(target, source) {
  for (const key of Object.keys(target)) {
    if (key === 'cost') {
      for (const part of Object.keys(target.cost)) target.cost[part] += source.cost[part];
    } else {
      target[key] += source[key];
    }
  }
  return target;
}

function totalTokens(a) {
  return a.input_tokens + a.output_tokens + a.cache_write_5m_tokens + a.cache_write_1h_tokens + a.cache_read_tokens;
}

/** Share of prompt tokens that were served from the cache. / 프롬프트 토큰 중 캐시에서 읽은 비율입니다. */
function cacheHitRate(a) {
  const prompt = a.input_tokens + a.cache_write_5m_tokens + a.cache_write_1h_tokens + a.cache_read_tokens;
  return prompt > 0 ? a.cache_read_tokens / prompt : null;
}

function costTypes(agg) {
  return agg.cost.web_search > 0 ? [...TOKEN_TYPES, WEB_SEARCH] : TOKEN_TYPES;
}

// ---------------------------------------------------------------------------------------------
// State, URL and loading
// 상태, URL, 불러오기
// ---------------------------------------------------------------------------------------------

function readUrl() {
  const params = new URLSearchParams(location.search);
  const range = params.get('range');
  if (PRESETS.includes(range)) {
    state.preset = range;
  } else if (params.get('from') || params.get('to')) {
    state.preset = 'custom';
    state.from = params.get('from') ?? '';
    state.to = params.get('to') ?? '';
  }
  for (const key of ['user', 'model', 'project']) state[key] = params.has(key) ? params.get(key) : null;
}

function writeUrl() {
  const params = new URLSearchParams();
  if (state.preset === 'custom') {
    if (state.from) params.set('from', state.from);
    if (state.to) params.set('to', state.to);
  } else {
    params.set('range', state.preset);
  }
  for (const key of ['user', 'model', 'project']) if (state[key] !== null) params.set(key, state[key]);
  history.replaceState(null, '', `?${params}`);
}

/** The date range the current preset stands for; a null `from` means everything. */
/** 현재 고른 기간이 가리키는 날짜 범위입니다. `from`이 null이면 전체입니다. */
function currentRange() {
  const today = todayLocal();
  switch (state.preset) {
    case 'today': return { from: today, to: today };
    case '7d': return { from: addDays(today, -6), to: today };
    case '30d': return { from: addDays(today, -29), to: today };
    case '90d': return { from: addDays(today, -89), to: today };
    case 'all': return { from: null, to: today };
    default: return { from: state.from || null, to: state.to || today };
  }
}

function setStatus(message) {
  const node = $('status');
  node.hidden = !message;
  node.replaceChildren();
  if (message) {
    node.append(message, ' ', el('button', { type: 'button', textContent: '다시 시도', onclick: () => load() }));
  }
}

async function load() {
  const ticket = ++requestCounter;
  const content = $('content');
  // The previous render stays in place, dimmed, until the new data arrives.
  // 새 데이터가 올 때까지 이전 화면을 흐리게 한 채로 그대로 둡니다.
  content.classList.add('is-loading');
  writeUrl();
  try {
    const range = currentRange();
    const params = new URLSearchParams();
    if (range.from) params.set('from', range.from);
    if (range.to) params.set('to', range.to);
    // Days are cut in the viewer's own offset.
    // 날짜는 보는 사람의 UTC 오프셋으로 자릅니다.
    params.set('tz', String(-new Date().getTimezoneOffset()));
    for (const key of ['user', 'model', 'project']) if (state[key] !== null) params.set(key, state[key]);
    const response = await fetch(`/api/v1/report?${params}`, { headers: { Accept: 'application/json' } });
    if (response.status === 401) {
      location.assign('/login');
      return;
    }
    const body = await response.json();
    if (!response.ok) throw new Error(body.error ?? `HTTP ${response.status}`);
    if (ticket !== requestCounter) return;
    setStatus('');
    render(body);
    lastLoadedAt = Date.now();
  } catch (error) {
    if (ticket === requestCounter) setStatus(`데이터를 불러오지 못했습니다. (${error.message})`);
  } finally {
    if (ticket === requestCounter) content.classList.remove('is-loading');
  }
}

// ---------------------------------------------------------------------------------------------
// Filters
// 필터
// ---------------------------------------------------------------------------------------------

function projectLabel(project) {
  return project === '' ? '(프로젝트 미지정)' : project;
}

function fillSelect(select, values, current, allLabel, labelOf = (v) => v) {
  const list = [...values];
  // Keep a selected value visible even when the new range has no data for it.
  // 새 기간에 데이터가 없더라도 선택한 값은 목록에 남겨 둡니다.
  if (current !== null && !list.includes(current)) list.unshift(current);
  select.replaceChildren(
    el('option', { value: ALL, textContent: allLabel }),
    ...list.map((value) => el('option', { value, textContent: labelOf(value) })),
  );
  select.value = current === null ? ALL : current;
}

function syncFilters(report) {
  for (const button of document.querySelectorAll('[data-preset]')) {
    button.setAttribute('aria-pressed', String(button.dataset.preset === state.preset));
  }
  $('from').value = report.range.from;
  $('to').value = report.range.to;
  $('to').max = todayLocal();
  fillSelect($('f-user'), report.options.users, state.user, '모든 사용자');
  fillSelect($('f-model'), report.options.models, state.model, '모든 모델');
  fillSelect($('f-project'), report.options.projects, state.project, '모든 프로젝트', projectLabel);
}

function bindControls() {
  for (const button of document.querySelectorAll('[data-preset]')) {
    button.addEventListener('click', () => {
      state.preset = button.dataset.preset;
      load();
    });
  }
  for (const id of ['from', 'to']) {
    $(id).addEventListener('change', () => {
      const from = $('from').value;
      const to = $('to').value;
      if (!from || !to) return;
      state.preset = 'custom';
      // Picking an end before the start swaps them instead of failing.
      // 끝 날짜를 시작보다 앞으로 고르면 오류 대신 둘을 맞바꿉니다.
      [state.from, state.to] = from <= to ? [from, to] : [to, from];
      load();
    });
  }
  for (const [id, key] of [['f-user', 'user'], ['f-model', 'model'], ['f-project', 'project']]) {
    $(id).addEventListener('change', (event) => {
      state[key] = event.target.value === ALL ? null : event.target.value;
      load();
    });
  }
  $('refresh').addEventListener('click', () => load());
  $('logout').addEventListener('click', async () => {
    await fetch('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    location.assign('/login');
  });
  for (const button of document.querySelectorAll('[data-toggle]')) {
    button.addEventListener('click', () => {
      const name = button.dataset.toggle;
      const showTable = button.getAttribute('aria-pressed') !== 'true';
      button.setAttribute('aria-pressed', String(showTable));
      button.textContent = showTable ? '차트로 보기' : '표로 보기';
      $(`${name}-chart`).hidden = showTable;
      $(`${name}-table`).hidden = !showTable;
      hideTooltip();
    });
  }
  // Coming back to the tab after a while shows fresh numbers without a manual reload.
  // 한동안 다른 탭에 있다가 돌아오면 직접 새로고침하지 않아도 최신 숫자를 보여 줍니다.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && Date.now() - lastLoadedAt > REFRESH_AFTER_MS) load();
  });
  window.addEventListener('scroll', hideTooltip, { passive: true });
}

// ---------------------------------------------------------------------------------------------
// Tables
// 표
// ---------------------------------------------------------------------------------------------

/**
 * Build a table. columns: [{ title, num, className, cell(row) -> string | Node }].
 * 표를 만듭니다. columns는 [{ title, num, className, cell(row) -> 문자열 | 노드 }]입니다.
 */
function buildTable(columns, rows, { caption, foot } = {}) {
  const classOf = (column) => [column.num ? 'num' : '', column.className ?? ''].join(' ').trim() || undefined;
  const line = (row, tag) => el('tr', {}, columns.map((column) => el(tag, { className: classOf(column) }, [column.cell(row)])));
  const table = el('table', {}, [
    caption ? el('caption', { className: 'visually-hidden', textContent: caption }) : null,
    el('thead', {}, [el('tr', {}, columns.map((column) => el('th', { scope: 'col', className: classOf(column), textContent: column.title })))]),
    el('tbody', {}, rows.map((row) => line(row, 'td'))),
  ]);
  if (foot) table.append(el('tfoot', {}, [line(foot, 'td')]));
  return table;
}

function costCell(agg) {
  if (agg.unpriced_requests === 0) return usd(agg.cost_usd);
  // Nothing in this row could be priced, so a dollar figure would read as "free".
  // 이 줄에는 단가를 매길 수 있는 요청이 하나도 없습니다. 금액을 적으면 "무료"로 읽힙니다.
  if (agg.unpriced_requests >= agg.requests) return '단가 없음';
  // A marked cost is a lower bound: some requests have no price.
  // 표시가 붙은 비용은 하한입니다. 일부 요청에 단가가 없습니다.
  return el('span', { title: `단가가 없는 요청 ${full(agg.unpriced_requests)}건은 빠져 있습니다.`, textContent: `${usd(agg.cost_usd)} *` });
}

/** A cell whose text may be cut off by its column; the full text stays available on hover. */
/** 열 너비 때문에 글자가 잘릴 수 있는 칸입니다. 마우스를 올리면 전체 글자를 볼 수 있습니다. */
function nameCell(text) {
  return el('span', { title: text, textContent: text });
}

// ---------------------------------------------------------------------------------------------
// Summary tiles
// 요약 타일
// ---------------------------------------------------------------------------------------------

function changeText(current, previous) {
  if (previous === null || previous === undefined) return null;
  if (previous === 0) return current === 0 ? '변동 없음' : '직전 기간 기록 없음';
  const change = (current - previous) / previous;
  if (Math.abs(change) < 0.0005) return '변동 없음';
  return `${change > 0 ? '▲' : '▼'} ${percent(Math.abs(change))}`;
}

function pointChangeText(current, previous) {
  if (current === null || previous === null || previous === undefined) return null;
  const points = (current - previous) * 100;
  if (Math.abs(points) < 0.05) return '변동 없음';
  return `${points > 0 ? '▲' : '▼'} ${Math.abs(points).toFixed(1)}%p`;
}

function statTile(label, value, sub, delta) {
  return el('article', { className: 'stat' }, [
    el('h3', { className: 'stat-label', textContent: label }),
    el('p', { className: 'stat-value', textContent: value }),
    el('p', { className: 'stat-sub', textContent: sub }),
    delta ? el('p', { className: 'stat-delta', textContent: delta }) : null,
  ]);
}

function renderSummary(report) {
  const now = report.totals;
  const before = report.previous_totals;
  const tokens = totalTokens(now);
  const hitRate = cacheHitRate(now);
  // When some models have no price the figure is a lower bound, and the tile says so.
  // 단가가 없는 모델이 있으면 금액은 하한이며, 타일에 그 사실을 적습니다.
  const nothingPriced = now.requests > 0 && now.unpriced_requests >= now.requests;
  const costNote = now.unpriced_requests > 0 ? `단가 없는 요청 ${full(now.unpriced_requests)}건 제외` : 'API 공시가로 환산';
  $('kpis').replaceChildren(
    statTile('총 토큰', compact(tokens), `${full(tokens)}개`, before && changeText(tokens, totalTokens(before))),
    statTile('추정 비용', nothingPriced ? '단가 없음' : usd(now.cost_usd), costNote, before && !nothingPriced && changeText(now.cost_usd, before.cost_usd)),
    statTile('요청 수', full(now.requests), now.requests ? `요청당 평균 ${compact(tokens / now.requests)} 토큰` : '요청 없음', before && changeText(now.requests, before.requests)),
    statTile('캐시 적중률', hitRate === null ? '-' : percent(hitRate), '프롬프트 토큰 중 캐시에서 읽은 비율', before && pointChangeText(hitRate, cacheHitRate(before))),
    statTile('사용자 · 세션', `${full(report.active_users)}명 · ${full(report.active_sessions)}개`, '기록이 있는 사용자와 세션', null),
  );
  const note = $('compare-note');
  note.hidden = !report.previous_range;
  if (report.previous_range) {
    const days = daysBetween(report.range.from, report.range.to);
    const { from, to } = report.previous_range;
    const span = from === to ? shortDay(from) : `${shortDay(from)} ~ ${shortDay(to)}`;
    note.textContent = `▲▼는 직전 ${days}일(${span})과 비교한 증감입니다.`;
  }
}

// ---------------------------------------------------------------------------------------------
// Token mix
// 토큰 구성
// ---------------------------------------------------------------------------------------------

function legendItems(types) {
  return types.map((type) => el('li', {}, [el('span', { className: `swatch bg-${type.cls}` }), type.name]));
}

function renderMix(report) {
  const totals = report.totals;
  const tokenTotal = totalTokens(totals);
  const types = costTypes(totals);
  const share = (ratio) => percent(ratio, ratio < 0.0995 ? 1 : 0);

  const bars = [
    ['토큰 수', TOKEN_TYPES.map((type) => ({ name: type.name, cls: type.cls, value: type.tokens(totals), display: `${full(type.tokens(totals))}개` }))],
    ['추정 비용', types.map((type) => ({ name: type.name, cls: type.cls, value: type.cost(totals), display: usd(type.cost(totals)) }))],
  ];
  const holder = $('mix-bars');
  holder.replaceChildren();
  for (const [label, parts] of bars) {
    const slot = el('div', { className: 'mix-bar' });
    holder.append(el('div', { className: 'mix-row' }, [el('div', { className: 'mix-label', textContent: label }), slot]));
    segmentedBar(slot, { label, parts, formatShare: share });
  }
  $('mix-legend').replaceChildren(...legendItems(types));

  // This table is also the bars' table view: every number a bar cannot print is here.
  // 이 표는 위 막대의 표 보기이기도 합니다. 막대에 적지 못한 숫자가 모두 여기에 있습니다.
  const ratio = (part, whole) => (whole > 0 ? percent(part / whole) : '-');
  const columns = [
    {
      title: '종류',
      // The total row has no swatch.
      // 합계 줄에는 색 견본이 없습니다.
      cell: (type) => (type.cls ? el('span', { className: 'name-cell' }, [el('span', { className: `swatch bg-${type.cls}` }), type.name]) : type.name),
    },
    { title: '토큰 수', num: true, cell: (type) => (type.tokens ? full(type.tokens(totals)) : '-') },
    { title: '토큰 비중', num: true, cell: (type) => (type.tokens ? ratio(type.tokens(totals), tokenTotal) : '-') },
    // With no priced request at all, a column of $0.00 would read as "free"; show a dash.
    // 단가를 매길 수 있는 요청이 하나도 없으면 $0.00이 "무료"로 읽히므로 줄표를 적습니다.
    { title: '추정 비용', num: true, cell: (type) => (totals.cost_usd > 0 ? usd(type.cost(totals)) : '-') },
    { title: '비용 비중', num: true, cell: (type) => ratio(type.cost(totals), totals.cost_usd) },
  ];
  const foot = { name: '합계', cls: null, tokens: () => tokenTotal, cost: () => totals.cost_usd };
  $('mix-table').replaceChildren(buildTable(columns, types, { caption: '토큰 종류별 토큰 수와 추정 비용', foot }));

  const notes = [];
  if (totals.output_tokens > 0) {
    notes.push(`출력 토큰 중 사고(thinking) 토큰은 ${full(totals.thinking_tokens)}개(${percent(totals.thinking_tokens / totals.output_tokens)})입니다.`);
  }
  const writes = totals.cache_write_5m_tokens + totals.cache_write_1h_tokens;
  if (writes > 0) {
    notes.push(`캐시 쓰기는 5분 보관 ${full(totals.cache_write_5m_tokens)}개, 1시간 보관 ${full(totals.cache_write_1h_tokens)}개입니다.`);
  }
  if (totals.web_search_requests > 0) notes.push(`웹 검색은 ${full(totals.web_search_requests)}회입니다.`);
  $('mix-notes').textContent = notes.join(' ');
}

// ---------------------------------------------------------------------------------------------
// Trends over time
// 기간별 추이
// ---------------------------------------------------------------------------------------------

/**
 * Turn the report's sparse buckets into a gapless series, coarsening long ranges to weeks or
 * months so the columns stay readable.
 * 리포트의 띄엄띄엄한 구간을 빈틈없는 계열로 바꿉니다. 기간이 길면 주나 월 단위로 묶어 막대를
 * 읽을 수 있는 굵기로 유지합니다.
 */
function buildBuckets(report) {
  const { from, to, bucket } = report.range;
  const byKey = new Map(report.buckets.map((b) => [b.key, b]));
  const days = [];
  for (let day = from; day <= to; day = addDays(day, 1)) days.push(day);

  if (bucket === 'hour') {
    const buckets = [];
    for (const day of days) {
      for (let hour = 0; hour < 24; hour += 1) {
        const key = `${day}T${String(hour).padStart(2, '0')}`;
        buckets.push({
          label: days.length > 1 && hour === 0 ? shortDay(day) : `${hour}시`,
          title: `${longDay(day)} ${hour}시`,
          agg: byKey.get(key) ?? emptyAgg(),
        });
      }
    }
    return { unit: '시간별', tickSteps: [1, 2, 3, 6, 12, 24], buckets };
  }

  if (days.length <= 92) {
    return {
      unit: '일별',
      tickSteps: [1, 2, 7, 14, 28],
      buckets: days.map((day) => ({ label: shortDay(day), title: longDay(day), agg: byKey.get(day) ?? emptyAgg() })),
    };
  }

  const weekly = days.length <= 730;
  const groups = new Map();
  for (const day of days) {
    const key = weekly ? weekStart(day) : day.slice(0, 7);
    if (!groups.has(key)) groups.set(key, { first: day, last: day, agg: emptyAgg() });
    const group = groups.get(key);
    group.last = day;
    const found = byKey.get(day);
    if (found) addAgg(group.agg, found);
  }
  const buckets = [...groups.entries()].map(([key, group]) => {
    if (weekly) {
      return { label: shortDay(group.first), title: `${longDay(group.first)} ~ ${shortDay(group.last)}`, agg: group.agg };
    }
    const [year, month] = key.split('-').map(Number);
    return { label: `${String(year).slice(2)}.${month}`, title: `${year}년 ${month}월`, agg: group.agg };
  });
  return weekly
    ? { unit: '주별', tickSteps: [1, 2, 4, 8, 13, 26], buckets }
    : { unit: '월별', tickSteps: [1, 2, 3, 6, 12], buckets };
}

function renderTrends(report) {
  const series = buildBuckets(report);
  const categories = series.buckets.map((b) => ({ label: b.label, title: b.title }));
  const types = costTypes(report.totals);

  // Token counts: one small chart per type, each on its own axis. Cache reads are tens of times
  // larger than everything else, so a shared axis would flatten the other three.
  // 토큰 수는 종류마다 작은 차트를 따로 두고 축도 따로 씁니다. 캐시 읽기가 나머지보다 수십 배
  // 크기 때문에 축을 함께 쓰면 나머지 셋이 납작해집니다.
  $('tokens-title').textContent = `${series.unit} 토큰`;
  const panels = $('tokens-chart');
  if (panels.childElementCount === 0) {
    for (const type of TOKEN_TYPES) {
      panels.append(
        el('figure', { className: 'multiple' }, [
          el('figcaption', {}, [
            el('span', { className: `swatch bg-${type.cls}` }),
            el('span', { className: 'multiple-name', textContent: type.name }),
            el('span', { className: 'multiple-total' }),
          ]),
          el('div', { className: 'chart-slot' }),
        ]),
      );
    }
  }
  TOKEN_TYPES.forEach((type, index) => {
    const figure = panels.children[index];
    figure.querySelector('.multiple-total').textContent = `${compact(type.tokens(report.totals))} 토큰`;
    columnChart(figure.querySelector('.chart-slot'), {
      label: `${series.unit} ${type.name} 토큰. 방향키로 구간을 옮겨 값을 읽을 수 있습니다.`,
      categories,
      series: [{ name: type.name, cls: type.cls, values: series.buckets.map((b) => type.tokens(b.agg)) }],
      height: 170,
      integer: true,
      formatTicks: (ticks) => ticks.map(compact),
      formatValue: (value) => `${full(value)}개`,
      tickSteps: series.tickSteps,
    });
  });
  $('tokens-table').replaceChildren(
    buildTable(
      [
        { title: '구간', cell: (b) => b.title },
        ...TOKEN_TYPES.map((type) => ({ title: type.name, num: true, cell: (b) => full(type.tokens(b.agg)) })),
        { title: '합계', num: true, cell: (b) => full(totalTokens(b.agg)) },
      ],
      series.buckets,
      { caption: `${series.unit} 토큰 수` },
    ),
  );

  // With nothing priced there is no cost to chart; an empty plot would only suggest "free".
  // 단가를 매긴 요청이 없으면 그릴 비용이 없습니다. 빈 차트는 "무료"라는 오해만 줍니다.
  $('card-cost').hidden = !(report.totals.cost_usd > 0);
  if ($('card-cost').hidden) return;

  // Cost puts every token type on one comparable axis, so here the types stack.
  // 비용은 모든 토큰 종류를 하나의 축에서 비교할 수 있게 해 주므로 여기서는 종류를 쌓습니다.
  $('cost-title').textContent = `${series.unit} 추정 비용`;
  $('cost-legend').replaceChildren(...legendItems(types));
  columnChart($('cost-plot'), {
    label: `${series.unit} 추정 비용, 토큰 종류별로 쌓음. 방향키로 구간을 옮겨 값을 읽을 수 있습니다.`,
    categories,
    series: types.map((type) => ({ name: type.name, cls: type.cls, values: series.buckets.map((b) => type.cost(b.agg)) })),
    height: 250,
    integer: false,
    formatTicks: usdTicks,
    formatValue: usd,
    tickSteps: series.tickSteps,
    totalName: '합계',
  });
  $('cost-table').replaceChildren(
    buildTable(
      [
        { title: '구간', cell: (b) => b.title },
        ...types.map((type) => ({ title: type.name, num: true, cell: (b) => usd(type.cost(b.agg)) })),
        { title: '합계', num: true, cell: (b) => costCell(b.agg) },
      ],
      series.buckets,
      { caption: `${series.unit} 추정 비용` },
    ),
  );
}

// ---------------------------------------------------------------------------------------------
// Breakdowns and sessions
// 분류별 집계와 세션
// ---------------------------------------------------------------------------------------------

function rankingName(row, labelOf) {
  if (row.merged > 0 && row.key === OTHERS_KEY) return `기타 ${full(row.merged)}개`;
  return labelOf(row.key);
}

/**
 * Ranked rows with a bar for total tokens. The bar is neutral gray because color on this page
 * is reserved for token types.
 * 토큰 합계 막대가 붙은 순위 표입니다. 이 화면에서 색은 토큰 종류에만 쓰므로 막대는 무채색입니다.
 */
function renderRanking(containerId, rows, nameTitle, labelOf = (key) => key || '(미지정)') {
  const container = $(containerId);
  const largest = Math.max(0, ...rows.map((row) => totalTokens(row)));
  // The bar and its exact value share one cell, so the bar never stands without a number.
  // 막대와 정확한 값을 한 칸에 함께 두어, 막대가 숫자 없이 놓이는 일이 없게 합니다.
  const barWithValue = (row) => {
    const bar = el('div', { className: 'rank-bar' });
    bar.style.width = `${largest > 0 ? (totalTokens(row) / largest) * 100 : 0}%`;
    return el('div', { className: 'bar-value' }, [
      el('div', { className: 'rank-track' }, [bar]),
      el('span', { textContent: full(totalTokens(row)) }),
    ]);
  };
  const columns = [
    { title: nameTitle, className: 'name', cell: (row) => nameCell(rankingName(row, labelOf)) },
    { title: '토큰 합계', className: 'barcell', cell: barWithValue },
    ...TOKEN_TYPES.map((type) => ({ title: type.name, num: true, cell: (row) => full(type.tokens(row)) })),
    { title: '요청', num: true, cell: (row) => full(row.requests) },
    { title: '추정 비용', num: true, cell: costCell },
  ];
  const draw = (expanded) => {
    const shown = expanded ? rows : rows.slice(0, VISIBLE_ROWS);
    const nodes = [buildTable(columns, shown, { caption: `${nameTitle}별 토큰 사용량` })];
    if (rows.length > VISIBLE_ROWS) {
      nodes.push(
        el('button', {
          type: 'button',
          className: 'more',
          textContent: expanded ? '상위 10개만 보기' : `나머지 ${full(rows.length - VISIBLE_ROWS)}개 더 보기`,
          onclick: () => draw(!expanded),
        }),
      );
    }
    container.replaceChildren(...nodes);
  };
  draw(false);
}

function renderSessions(report) {
  const card = $('card-sessions');
  card.hidden = report.sessions.length === 0;
  if (card.hidden) return;
  $('sessions-sub').textContent = report.active_sessions > report.sessions.length
    ? `전체 ${full(report.active_sessions)}개 중 추정 비용이 큰 ${full(report.sessions.length)}개입니다.`
    : `조회 범위의 세션 ${full(report.sessions.length)}개입니다.`;
  const columns = [
    { title: '사용자', className: 'name', cell: (s) => nameCell(s.user) },
    { title: '프로젝트', className: 'name', cell: (s) => nameCell(projectLabel(s.project)) },
    { title: '모델', cell: (s) => s.models.join(', ') },
    { title: '시작', num: true, cell: (s) => shortDateTime(s.first_ts) },
    { title: '길이', num: true, cell: (s) => duration(s.last_ts - s.first_ts) },
    { title: '요청', num: true, cell: (s) => full(s.requests) },
    { title: '토큰 합계', num: true, cell: (s) => full(totalTokens(s)) },
    { title: '추정 비용', num: true, cell: costCell },
  ];
  $('sessions-table').replaceChildren(buildTable(columns, report.sessions, { caption: '추정 비용이 큰 세션' }));
}

function renderFootnote(report) {
  const lines = [
    `추정 비용은 Anthropic API 공시 단가(${report.prices_as_of} 기준)로 환산한 값입니다. 구독 요금제를 쓰는 경우 실제 청구액과 다릅니다.`,
    `날짜와 시각은 이 브라우저의 시간대(${utcOffsetLabel(report.range.utc_offset_minutes)}) 기준입니다.`,
  ];
  if (report.unpriced_models.length > 0) {
    lines.push(`* 단가표에 없는 모델(${report.unpriced_models.join(', ')})의 요청 ${full(report.totals.unpriced_requests)}건은 비용에서 빠져 있습니다. 토큰 수에는 포함됩니다.`);
  }
  $('footnote').replaceChildren(...lines.map((text) => el('p', { textContent: text })));
}

// ---------------------------------------------------------------------------------------------
// Page
// 화면
// ---------------------------------------------------------------------------------------------

function render(report) {
  hideTooltip();
  syncFilters(report);
  renderSummary(report);
  const hasData = report.totals.requests > 0;
  $('empty').hidden = hasData;
  // Charts measure their containers, so the section must be visible before they draw.
  // 차트는 컨테이너 크기를 재므로, 그리기 전에 영역이 먼저 보여야 합니다.
  $('details').hidden = !hasData;
  if (hasData) {
    renderMix(report);
    renderTrends(report);
    renderRanking('users-table', report.by_user, '사용자');
    renderRanking('models-table', report.by_model, '모델');
    renderRanking('projects-table', report.by_project, '프로젝트', projectLabel);
    // A source breakdown only says something when more than one program reports.
    // 출처별 집계는 둘 이상의 프로그램이 보고할 때만 의미가 있습니다.
    $('card-sources').hidden = report.by_source.length < 2;
    if (!$('card-sources').hidden) renderRanking('sources-table', report.by_source, '출처');
    renderSessions(report);
  }
  renderFootnote(report);
}

async function showViewer() {
  try {
    const response = await fetch('/api/v1/me', { headers: { Accept: 'application/json' } });
    if (response.ok) $('viewer').textContent = `${(await response.json()).user} 님`;
  } catch {
    // The name is decoration; the report request reports real failures.
    // 이름은 부가 정보입니다. 실제 오류는 리포트 요청 쪽에서 알립니다.
  }
}

readUrl();
bindControls();
showViewer();
load();
