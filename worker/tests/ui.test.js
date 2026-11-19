import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { execSync } from "node:child_process";

// 어느 cwd에서 돌려도 repo 루트 기준 경로가 맞도록 테스트 파일 기준으로 절대 repo 루트를 쓴다.
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

// 화면(html.js)의 인라인 스크립트를 최소 DOM 스텁으로 평가한다.
// 카드별 시간을 따로 계산하던 cũ 코드를 잡기 위해 순수 함수(chartWindow·buildChartSvg)를 직접 호출한다.

function loadHtml() {
  const src = readFileSync(new URL("../src/html.js", import.meta.url), "utf8");
  const m = src.match(/export default ([\s\S]+);\s*$/);
  assert.ok(m, "html.js export 형식을 찾지 못했어요");
  return JSON.parse(m[1].trim());
}

function extractScripts(html) {
  const out = [];
  for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (/\bsrc\s*=/.test(m[1])) continue;
    out.push(m[2]);
  }
  return out;
}

function makeEl(tag = "div") {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    dataset: {},
    style: { setProperty: () => {}, height: "" },
    classList: { add: () => {}, remove: () => {}, contains: () => false },
    textContent: "",
    innerHTML: "",
    hidden: false,
    value: "",
    checked: false,
    indeterminate: false,
    open: false,
    clientWidth: 320,
    __h: null,
    querySelector: () => makeEl("div"),
    querySelectorAll: () => [],
    addEventListener: () => {},
    setAttribute: () => {},
    getAttribute: () => null,
    hasAttribute: () => false,
    appendChild: () => {},
    insertBefore: () => {},
    remove: () => {},
    replaceWith: () => {},
    contains: () => false,
    focus: () => {},
    select: () => {},
    closest: () => null,
    getBoundingClientRect: () => ({ left: 0, width: 320 }),
    offsetWidth: 190,
    scrollTop: 0,
    scrollLeft: 0,
  };
  if (tag === "template") el.content = { firstElementChild: makeEl("div") };
  return el;
}

function makeSandbox() {
  const html = loadHtml();
  const byId = new Map();
  const __handlers = {};
  const __timers = [];
  const documentStub = {
    hidden: false,
    documentElement: {
      dataset: {},
      setAttribute: () => {},
      removeAttribute: () => {},
      style: { setProperty: () => {} },
    },
    getElementById: (id) => {
      if (!byId.has(id)) byId.set(id, makeEl("div"));
      return byId.get(id);
    },
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: (type, fn) => { (__handlers[type] ||= []).push(fn); },
    createElement: (tag) => makeEl(tag),
    body: makeEl("body"),
  };
  const sandbox = {
    console,
    document: documentStub,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    ResizeObserver: class {
      constructor(cb) {}
      observe() {}
      disconnect() {}
    },
    fetch: () => new Promise(() => {}),
    setTimeout: (cb, ms) => { __timers.push({ cb, ms }); return __timers.length; },
    clearTimeout: () => {},
    setInterval: (cb, ms) => { __timers.push({ cb, ms, interval: true }); return __timers.length; },
    clearInterval: () => {},
    __byId: byId,
    __html: html,
    __handlers,
    __timers,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const code of extractScripts(html)) {
    vm.runInContext(code, sandbox);
  }
  const fn = (name) => vm.runInContext(name, sandbox);
  return { sandbox, html, fn };
}

// 가짜 서비스: 첫 기록 시각과 초기화 시각이 카드마다 다르다 (구 코드는 카드마다 창이 달랐다).
// 12:03:08 로 두면 다음 슬롯(12:05)까지 1:52 남는다.
const NOW = Date.parse("2026-11-19T12:03:08+09:00");
const KST = (ms) => new Date(ms + 9 * 3600e3).toISOString().slice(0, 19) + "+09:00";
function fakeSeries(svc, firstMs, lastMs, resetMs) {
  return {
    key: `${svc}|weekly_used_pct|`,
    label: "주간",
    color: "var(--s1)",
    dash: "",
    rows: [
      { ts: KST(firstMs), value: 10, reset_at: KST(resetMs) },
      { ts: KST(lastMs), value: 30, reset_at: KST(resetMs) },
    ],
  };
}

test("chartWindow는 범위마다 now가 중앙이고 카드마다 동일하다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  assert.equal(typeof chartWindow, "function", "chartWindow가 전역 함수로 있어야 해요");

  // 초기화 시각이 서로 다른 가짜 서비스들.
  const services = [
    fakeSeries("codex", NOW - 3 * 86400e3, NOW - 5 * 60e3, NOW + 1 * 86400e3),
    fakeSeries("claude", NOW - 6 * 86400e3, NOW - 2 * 60e3, NOW + 4 * 86400e3),
    fakeSeries("grok", NOW - 9 * 86400e3, NOW - 1 * 60e3, NOW + 20 * 86400e3),
  ];
  // whole 사용 시 earliest는 보이는 것만이 아니라 전체 중 가장 이른 값이어야 한다.
  const earliest = Math.min(
    ...services.flatMap((s) => s.rows.map((r) => Date.parse(r.ts)))
  );

  for (const range of ["24h", "7d", "all"]) {
    const w = chartWindow(range, NOW, earliest);
    assert.ok(Number.isFinite(w.t0) && Number.isFinite(w.t1), `${range} 창이 유한해야 해요`);
    assert.equal((w.t0 + w.t1) / 2, NOW, `${range}에서 now가 정확히 중앙이어야 해요`);
    assert.ok(w.t0 < NOW && NOW < w.t1, `${range}에서 now가 창 안에 있어야 해요`);
    // 같은 입력으로 카드마다 반복해도 결과가 같다 (구 코드의 카드별 t1 계산과 다름).
    for (const s of services) {
      const again = chartWindow(range, NOW, earliest);
      assert.equal(again.t0, w.t0, `${range} t0가 카드별로 달라지면 안 돼요`);
      assert.equal(again.t1, w.t1, `${range} t1이 카드별로 달라지면 안 돼요`);
      void s;
    }
  }

  const day = 86400e3;
  assert.equal(chartWindow("24h", NOW, earliest).t0, NOW - day);
  assert.equal(chartWindow("24h", NOW, earliest).t1, NOW + day);
  assert.equal(chartWindow("7d", NOW, earliest).t0, NOW - 7 * day);
  assert.equal(chartWindow("7d", NOW, earliest).t1, NOW + 7 * day);
  const all = chartWindow("all", NOW, earliest);
  assert.equal(all.t0, earliest, "전체 창은 가장 이른 기록부터 시작해야 해요");
  assert.equal(all.t1, NOW + (NOW - earliest), "전체 창은 now를 대칭으로 미러야 해요");
});

test("buildChartSvg는 지금 표시·미래 음영 없이 now x가 카드마다 동일하다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  assert.equal(typeof buildChartSvg, "function", "buildChartSvg가 있어야 해요");

  const services = [
    fakeSeries("codex", NOW - 3 * 86400e3, NOW - 5 * 60e3, NOW + 1 * 86400e3),
    fakeSeries("claude", NOW - 6 * 86400e3, NOW - 2 * 60e3, NOW + 4 * 86400e3),
    // 창 밖(멀리 있는) 초기화는 그리지 않는다(만기·툴팁 없음).
    fakeSeries("grok", NOW - 2 * 86400e3, NOW - 3 * 60e3, NOW + 30 * 86400e3),
  ];
  const earliest = Math.min(...services.flatMap((s) => s.rows.map((r) => Date.parse(r.ts))));
  const W = 400, H = 200;
  const win = chartWindow("7d", NOW, earliest);
  const nowXs = [];
  for (const ser of services) {
    const out = buildChartSvg({ vis: [ser], now: NOW, t0: win.t0, t1: win.t1, W, H });
    assert.ok(!out.inner.includes("지금"), "SVG에 '지금' 점선이 있으면 안 돼요");
    assert.ok(!out.inner.includes("accent-soft"), "미래 음영 rect가 있으면 안 돼요");
    assert.ok(!out.inner.includes("지금</text>"), "SVG에 '지금' 라벨이 있으면 안 돼요");
    nowXs.push(out.nowX);
  }
  assert.ok(nowXs[0] === nowXs[1] && nowXs[1] === nowXs[2], "now의 x 위치가 카드마다 달라지면 안 돼요");
  // 7일 창 중간의 x: l=36, r=48 이므로 기대값 36 + (W-84)/2.
  assert.equal(nowXs[0], 36 + (W - 36 - 48) / 2, "now x가 창 중앙이어야 해요");

  // 창 안에 있는 초기화는 점선 세로선(라벨·툴팁 없음)으로, 창 밖은 아무것도 그리지 않는다.
  const inside = buildChartSvg({
    vis: [fakeSeries("a", NOW - 7 * 86400e3, NOW - 60e3, NOW + 2 * 86400e3)],
    now: NOW, t0: win.t0, t1: win.t1, W, H,
  });
  assert.equal((inside.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 1, "창 안쪽 초기화 점선 세로선이 하나 있어야 해요");
  assert.ok(!inside.inner.includes("초기화 ·"), "초기화 점선 세로선에 툴팁 텍스트가 있으면 안 돼요");
  assert.ok(!inside.inner.includes("<title>"), "초기화 점선 세로선에 <title>이 있으면 안 돼요");
  assert.ok(!inside.inner.includes("만기"), "차트에 '만기 …' 라벨이 있으면 안 돼요");
  assert.ok(!inside.inner.includes('y1="20" y2="26"'), "짧은 6px 틱 마커가 있으면 안 돼요");
  const outside = buildChartSvg({
    vis: [fakeSeries("b", NOW - 7 * 86400e3, NOW - 60e3, NOW + 30 * 86400e3)],
    now: NOW, t0: win.t0, t1: win.t1, W, H,
  });
  assert.ok(!outside.inner.includes("만기"), "창 밖 초기화는 '만기 …' 라벨이면 안 돼요");
  assert.ok(!outside.inner.includes("초기화 ·"), "창 밖 초기화는 점선 세로선이 아니어야 해요");
  assert.ok(!outside.inner.includes("<title>"), "창 밖 초기화에도 <title>이 있으면 안 돼요");
  assert.equal((outside.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 0, "창 밖 초기화는 점선 세로선이 아니어야 해요");
});

test("fmtDT는 'M월D일(요일) HH:MM' 형식을 쓴다", () => {
  const { fn } = makeSandbox();
  const fmtDT = fn("fmtDT");
  assert.equal(typeof fmtDT, "function", "fmtDT가 전역 함수로 있어야 해요");
  assert.equal(fmtDT(Date.parse("2026-11-26T04:25:00+09:00")), "11월26일(목) 04:25");
  assert.equal(fmtDT(Date.parse("2026-01-05T04:25:00+09:00")), "1월5일(월) 04:25", "월·일 한 자리도 0으로 채우지 않아요");
  // 요일이 없는 x축 틱 라벨(M/D)은 그대로이다.
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  const W = 400, H = 200, earliest = NOW - 7 * 86400e3;
  const win = chartWindow("7d", NOW, earliest);
  const out = buildChartSvg({ vis: [fakeSeries("a", NOW - 7 * 86400e3, NOW - 60e3, NOW + 2 * 86400e3)], now: NOW, t0: win.t0, t1: win.t1, W, H });
  assert.match(out.inner, />\d{1,2}\/\d{1,2}</, "x축 틱 라벨은 M/D 그대로여야 해요");
});

test("buildChartSvg는 시리즈마다 최신 기록의 미래 창 안 초기화를 점선 세로선 하나로 그린다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  const W = 400, H = 200, earliest = NOW - 7 * 86400e3;
  const win = chartWindow("7d", NOW, earliest);
  // 대칭 창은 그대로이다.
  assert.equal((win.t0 + win.t1) / 2, NOW, "대칭 창이 바뀌면 안 돼요");
  assert.equal(win.t1 - NOW, NOW - win.t0);

  // 오래된 기록들의 서로 다른 미래 창 안 초기화는 무시하고, 최신 기록(ts 최대) 하나만 그린다.
  const latestResetMs = NOW + 2 * 86400e3;
  const ser = {
    key: "rst|weekly_used_pct|", label: "주간A", color: "var(--s1)", dash: "",
    rows: [
      { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW - 3 * 86400e3) },   // 과거, 창 안 (무시)
      { ts: KST(NOW - 5 * 86400e3), value: 12, reset_at: KST(NOW + 1 * 86400e3) },   // 오래된 미래 창 안 (무시)
      { ts: KST(NOW - 4 * 86400e3), value: 14, reset_at: KST(NOW + 3 * 86400e3) },   // 오래된 미래 창 안 (무시)
      { ts: KST(NOW - 120e3), value: 16, reset_at: KST(NOW + 30 * 86400e3) },        // 오래된 창 밖 (무시)
      { ts: KST(NOW - 60e3), value: 18, reset_at: KST(latestResetMs) },               // 최신 미래 창 안
    ],
  };
  const out = buildChartSvg({ vis: [ser], now: NOW, t0: win.t0, t1: win.t1, W, H });
  const dotted = out.inner.match(/stroke-dasharray="1\.5 3"/g) || [];
  assert.equal(dotted.length, 1, "오래된 기록의 초기화는 무시하고 최신 기록 하나만 점선 세로선이 있어야 해요");
  assert.ok(out.inner.includes(`<line x1="${out.X(latestResetMs)}" x2="${out.X(latestResetMs)}"`), "점선 세로선이 최신 기록의 초기화 위치여야 해요");
  assert.ok(!out.inner.includes("초기화 ·"), "초기화 점선 세로선에 툴팁 텍스트가 있으면 안 돼요");
  assert.ok(!out.inner.includes("<title>"), "초기화 점선 세로선에 <title>이 있으면 안 돼요");
  assert.ok(!out.inner.includes("만기"), "창 밖 초기화 '만기 …' 라벨이 있으면 안 돼요");
  assert.ok(out.inner.includes('stroke="var(--s1)" stroke-width="1.5"'), "점선 세로선은 시리즈 색이어야 해요");
  assert.ok(out.inner.includes('y1="20" y2="176"'), "점선 세로선은 plot 전체 높이여야 해요");
  assert.ok(out.inner.includes('stroke-linecap="round" opacity=".7"'), "점선 세로선 스타일이 바뀌면 안 돼요");
  assert.ok(!out.inner.includes('y1="20" y2="26"'), "짧은 6px 틱 마커가 있으면 안 돼요");
  assert.ok(out.inner.indexOf('stroke-dasharray="1.5 3"') < out.inner.indexOf("<path"), "점선 세로선은 데이터 경로보다 먼저(=아래에) 그려야 해요");
  assert.ok(!out.inner.includes("지금"), "SVG에 '지금' 표시가 있으면 안 돼요");
  assert.ok(!out.inner.includes("지금</text>"), "SVG에 '지금' 라벨이 있으면 안 돼요");

  // 두 시리즈가 있을 때 각자 최신 기록 기준으로 한 줄씩이다.
  const ser2 = {
    key: "rst2|weekly_used_pct|", label: "주간B", color: "var(--s2)", dash: "",
    rows: [
      { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW + 1 * 86400e3) }, // 오래된 미래 창 안 (무시)
      { ts: KST(NOW - 60e3), value: 20, reset_at: KST(NOW + 4 * 86400e3) },        // 최신 미래 창 안
    ],
  };
  const both = buildChartSvg({ vis: [ser, ser2], now: NOW, t0: win.t0, t1: win.t1, W, H });
  assert.equal((both.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 2, "시리즈마다 최신 기록 초기화 선을 하나씩 그려야 해요");
  assert.ok(both.inner.includes('stroke="var(--s1)" stroke-width="1.5" stroke-dasharray="1.5 3"'), "첫 시리즈 선이 있어야 해요");
  assert.ok(both.inner.includes('stroke="var(--s2)" stroke-width="1.5" stroke-dasharray="1.5 3"'), "둘째 시리즈 선이 있어야 해요");

  // 숨긴(범례 토글) 시리즈의 선은 그리지 않는다.
  const onlyB = buildChartSvg({ vis: [ser2], now: NOW, t0: win.t0, t1: win.t1, W, H });
  assert.equal((onlyB.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 1, "보이는 시리즈 선을 그려야 해요");
  assert.ok(onlyB.inner.includes('stroke="var(--s2)" stroke-width="1.5" stroke-dasharray="1.5 3"'), "보이는 시리즈 색으로 그려야 해요");
  assert.ok(!onlyB.inner.includes('stroke="var(--s1)" stroke-width="1.5" stroke-dasharray="1.5 3"'), "숨긴 시리즈 선을 그리면 안 돼요");
  assert.ok(!onlyB.inner.includes("<title>"), "숨긴 시리즈 검사에서도 <title>이 있으면 안 돼요");

  // 최신 기록의 창 밖 과거 초기화는 선·만기 라벨 모두 아니다.
  const old = {
    key: "old|weekly_used_pct|", label: "주간", color: "var(--s1)", dash: "",
    rows: [
      { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW - 30 * 86400e3) },
      { ts: KST(NOW - 60e3), value: 20, reset_at: KST(NOW - 30 * 86400e3) },
    ],
  };
  const outOld = buildChartSvg({ vis: [old], now: NOW, t0: win.t0, t1: win.t1, W, H });
  assert.equal((outOld.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 0, "창 밖 초기화는 점선 세로선이 아니어야 해요");
  assert.ok(!outOld.inner.includes("만기"), "과거 초기화는 '만기 …' 라벨이 아니어야 해요");
});

test("buildChartSvg는 최신 기록이 창 안 과거이면 오래된 미래가 있어도 그리지 않는다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  const W = 400, H = 200, earliest = NOW - 7 * 86400e3;
  const win = chartWindow("7d", NOW, earliest);
  // 최신 기록이 창 안 과거: 오래된 미래가 있어도 점선 없음.
  const pastLatest = buildChartSvg({
    vis: [{
      key: "past|weekly_used_pct|", label: "주간", color: "var(--s1)", dash: "",
      rows: [
        { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW + 2 * 86400e3) },
        { ts: KST(NOW - 60e3), value: 20, reset_at: KST(NOW - 3 * 86400e3) },
      ],
    }],
    now: NOW, t0: win.t0, t1: win.t1, W, H,
  });
  assert.equal((pastLatest.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 0, "최신 기록의 창 안 과거 초기화는 점선 세로선이 아니어야 해요");
  // 최신 기록이 창 안 미래: 점선 하나.
  const futureLatest = buildChartSvg({
    vis: [{
      key: "future|weekly_used_pct|", label: "주간", color: "var(--s1)", dash: "",
      rows: [
        { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW - 3 * 86400e3) },
        { ts: KST(NOW - 60e3), value: 20, reset_at: KST(NOW + 2 * 86400e3) },
      ],
    }],
    now: NOW, t0: win.t0, t1: win.t1, W, H,
  });
  assert.equal((futureLatest.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 1, "최신 기록의 창 안 미래 초기화 점선 세로선이 하나 있어야 해요");
});

test("buildChartSvg는 오래된 기록의 서로 다른 초기화를 무시하고 ts 최대 기록만 쓴다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  const W = 400, H = 200, earliest = NOW - 7 * 86400e3;
  const win = chartWindow("7d", NOW, earliest);
  const count = (inner) => (inner.match(/stroke-dasharray="1\.5 3"/g) || []).length;

  // 오래된 기록이 서로 다른 미래 창 안 초기화를 다 가져도 선은 최신 하나만.
  const latestMs = NOW - 60e3, latestResetMs = NOW + 2 * 86400e3;
  const rows = [
    { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW + 1 * 86400e3) },
    { ts: KST(NOW - 5 * 86400e3), value: 12, reset_at: KST(NOW + 3 * 86400e3) },
    { ts: KST(NOW - 4 * 86400e3), value: 14, reset_at: KST(NOW + 4 * 86400e3) },
    { ts: KST(latestMs), value: 20, reset_at: KST(latestResetMs) },
  ];
  const mk = (rs) => ({ key: "ord|weekly_used_pct|", label: "주간", color: "var(--s1)", dash: "", rows: rs });
  const fwd = buildChartSvg({ vis: [mk(rows)], now: NOW, t0: win.t0, t1: win.t1, W, H });
  assert.equal(count(fwd.inner), 1, "오래된 기록의 초기화는 선을 만들지 않아야 해요");
  assert.ok(fwd.inner.includes(`<line x1="${fwd.X(latestResetMs)}" x2="${fwd.X(latestResetMs)}"`), "점선 세로선이 최신 기록의 초기화 위치여야 해요");

  // 행 순서가 바뀌어도 ts 최대 기록이 같은 선을 낸다.
  const rev = buildChartSvg({ vis: [mk([...rows].reverse())], now: NOW, t0: win.t0, t1: win.t1, W, H });
  assert.equal(count(rev.inner), 1, "행 순서가 바뀌어도 점선 세로선은 하나여야 해요");
  assert.ok(rev.inner.includes(`<line x1="${rev.X(latestResetMs)}" x2="${rev.X(latestResetMs)}"`), "순서가 바뀌어도 최신 기록의 초기화 위치가 같아야 해요");

  // 최신 기록이 과거 초기화면 오래된 미래가 있어도 선이 없다.
  const pastLatest = buildChartSvg({
    vis: [mk([
      { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW + 2 * 86400e3) },
      { ts: KST(latestMs), value: 20, reset_at: KST(NOW - 1 * 86400e3) },
    ])],
    now: NOW, t0: win.t0, t1: win.t1, W, H,
  });
  assert.equal(count(pastLatest.inner), 0, "최신 기록의 과거 초기화는 선이 아니어야 해요");

  // 최신 기록의 초기화가 창 밖이면 선이 없다.
  const beyondLatest = buildChartSvg({
    vis: [mk([
      { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW + 2 * 86400e3) },
      { ts: KST(latestMs), value: 20, reset_at: KST(NOW + 30 * 86400e3) },
    ])],
    now: NOW, t0: win.t0, t1: win.t1, W, H,
  });
  assert.equal(count(beyondLatest.inner), 0, "최신 기록의 창 밖 초기화는 선이 아니어야 해요");

  // 최신 기록에 초기화가 없으면(null·무효) 선이 없다.
  for (const bad of [null, "not-a-date"]) {
    const noReset = buildChartSvg({
      vis: [mk([
        { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW + 2 * 86400e3) },
        { ts: KST(latestMs), value: 20, reset_at: bad },
      ])],
      now: NOW, t0: win.t0, t1: win.t1, W, H,
    });
    assert.equal(count(noReset.inner), 0, `최신 기록의 초기화(${bad})가 없으면 선이 아니어야 해요`);
  }
});

test("buildChartSvg는 한 카드에 시리즈마다 최대 하나의 점선 세로선만 그린다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  const W = 400, H = 200, earliest = NOW - 7 * 86400e3;
  const win = chartWindow("7d", NOW, earliest);
  const count = (inner) => (inner.match(/stroke-dasharray="1\.5 3"/g) || []).length;

  // 두 시리즈가 각각 여러 오래된 미래를 가진 채로도 각자 최신 하나씩만 그린다.
  const serA = {
    key: "a|weekly_used_pct|", label: "A", color: "var(--s1)", dash: "",
    rows: [
      { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW + 1 * 86400e3) },
      { ts: KST(NOW - 60e3), value: 20, reset_at: KST(NOW + 2 * 86400e3) },
    ],
  };
  const serB = {
    key: "b|weekly_used_pct|", label: "B", color: "var(--s2)", dash: "",
    rows: [
      { ts: KST(NOW - 6 * 86400e3), value: 12, reset_at: KST(NOW + 2 * 86400e3) },
      { ts: KST(NOW - 60e3), value: 22, reset_at: KST(NOW + 4 * 86400e3) },
    ],
  };
  const both = buildChartSvg({ vis: [serA, serB], now: NOW, t0: win.t0, t1: win.t1, W, H });
  assert.equal(count(both.inner), 2, "두 시리즈는 각자 최대 하나씩, 총 두 줄이어야 해요");
  assert.ok(both.inner.includes('stroke="var(--s1)" stroke-width="1.5" stroke-dasharray="1.5 3"'), "첫 시리즈 선이 있어야 해요");
  assert.ok(both.inner.includes('stroke="var(--s2)" stroke-width="1.5" stroke-dasharray="1.5 3"'), "둘째 시리즈 선이 있어야 해요");
  assert.ok(both.inner.includes(`<line x1="${both.X(NOW + 2 * 86400e3)}" x2="${both.X(NOW + 2 * 86400e3)}"`), "첫 선이 첫 시리즈 최신 위치여야 해요");
  assert.ok(both.inner.includes(`<line x1="${both.X(NOW + 4 * 86400e3)}" x2="${both.X(NOW + 4 * 86400e3)}"`), "둘째 선이 둘째 시리즈 최신 위치여야 해요");

  // 한쪽 최신이 유효하지 않으면 다른 쪽 하나만 그린다.
  const serBStale = {
    key: "b|weekly_used_pct|", label: "B", color: "var(--s2)", dash: "",
    rows: [
      { ts: KST(NOW - 6 * 86400e3), value: 12, reset_at: KST(NOW + 4 * 86400e3) },
      { ts: KST(NOW - 60e3), value: 22, reset_at: KST(NOW - 1 * 86400e3) },
    ],
  };
  const onlyA = buildChartSvg({ vis: [serA, serBStale], now: NOW, t0: win.t0, t1: win.t1, W, H });
  assert.equal(count(onlyA.inner), 1, "최신 기록이 과거인 시리즈는 선이 없어야 해요");
  assert.ok(onlyA.inner.includes('stroke="var(--s1)" stroke-width="1.5" stroke-dasharray="1.5 3"'), "유효한 시리즈 선만 남아야 해요");
  assert.ok(!onlyA.inner.includes('stroke="var(--s2)" stroke-width="1.5" stroke-dasharray="1.5 3"'), "최신 기록이 과거인 시리즈 선은 없어야 해요");
});

test("drawChart는 최신 기록 초기화가 바뀌면 다시 그린다 (버전은 그대로)", () => {
  const { sandbox, fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const drawChart = fn("drawChart");
  const W = 400, earliest = NOW - 7 * 86400e3;
  const win = chartWindow("7d", NOW, earliest);
  const rows = [
    { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(NOW + 2 * 86400e3) },
    { ts: KST(NOW - 60e3), value: 20, reset_at: KST(NOW + 2 * 86400e3) },
  ];
  vm.runInContext(
    `displayedTrend = { range: "7d", version: 1, series: new Map([["codex", [{ key: "rst|weekly_used_pct|", label: "주간", color: "var(--s1)", dash: "", rows: ${JSON.stringify(rows)} }]]]) };
     seriesVisible["rst|weekly_used_pct|"] = true;`,
    sandbox
  );
  const plot = makeEl("div"); plot.clientWidth = W;
  const cur = makeEl("span");
  const card = { dataset: { svc: "codex" }, clientWidth: W,
    querySelector: (sel) => (sel === "[data-plot]" ? plot : sel === "[data-cur]" ? cur : makeEl("div")) };

  drawChart(card, NOW, win);
  const sig1 = plot.dataset.sig, inner1 = plot.innerHTML;
  assert.ok(sig1, "처음 drawChart는 sig를 남겨야 해요");
  assert.equal((inner1.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.7"/g) || []).length, 1, "창 안 초기화 점선 세로선이 하나여야 해요");
  assert.equal((inner1.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.4"/g) || []).length, 1, "주간 -7일 선이 하나여야 해요");

  // 같은 데이터면 sig가 같아 다시 그리지 않는다.
  drawChart(card, NOW, win);
  assert.equal(plot.dataset.sig, sig1, "같은 데이터에서는 sig가 같아야 해요");
  assert.equal(plot.innerHTML, inner1, "같은 데이터에서는 다시 그리지 않아야 해요");

  // 오래된 기록을 추가해도 최신 초기화는 그대로라 sig·선은 변하지 않는다.
  vm.runInContext(
    `displayedTrend.series.get("codex")[0].rows.push({ ts: ${JSON.stringify(KST(NOW - 5 * 86400e3))}, value: 12, reset_at: ${JSON.stringify(KST(NOW + 5 * 86400e3))} });`,
    sandbox
  );
  drawChart(card, NOW, win);
  assert.equal(plot.dataset.sig, sig1, "오래된 기록 추가는 sig를 바꾸면 안 돼요");
  assert.equal(plot.innerHTML, inner1, "오래된 기록 추가는 다시 그리지 않아야 해요");

  // 버전을 그대로 두고 최신 기록의 초기화만 바꿔도 다시 그려야 한다.
  const innerBeforeLatestPush = plot.innerHTML;
  vm.runInContext(
    `displayedTrend.series.get("codex")[0].rows.push({ ts: ${JSON.stringify(KST(NOW - 10e3))}, value: 22, reset_at: ${JSON.stringify(KST(NOW + 4 * 86400e3))} });`,
    sandbox
  );
  drawChart(card, NOW, win);
  assert.notEqual(plot.dataset.sig, sig1, "최신 기록 초기화가 바뀌면 sig가 바뀌어야 해요");
  assert.equal((plot.innerHTML.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.7"/g) || []).length, 1, "초기화 선은 최신 기록의 하나만 남아야 해요");
  assert.equal((plot.innerHTML.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.4"/g) || []).length, 1, "주간 -7일 선은 최신 기록의 하나만 남아야 해요");
  assert.notEqual(plot.innerHTML, innerBeforeLatestPush, "최신 기록 초기화가 바뀌면 선 위치가 바뀌어야 해요");

  // 범례 토글로 시리즈를 숨기면 선이 사라진다.
  vm.runInContext(`seriesVisible["rst|weekly_used_pct|"] = false;`, sandbox);
  drawChart(card, NOW, win);
  assert.ok(plot.innerHTML.includes("범례에서 선택"), `숨긴 시리즈는 빈 안내만 남아야 해요: ${plot.innerHTML.slice(0, 80)}`);
});

test("drawChart sig는 now가 미래 초기화를 지나면 바뀐다", () => {
  const { sandbox, fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const drawChart = fn("drawChart");
  const W = 400, earliest = NOW - 7 * 86400e3;
  const win = chartWindow("7d", NOW, earliest);
  const resetMs = NOW + 2 * 86400e3;
  const rows = [
    { ts: KST(NOW - 6 * 86400e3), value: 10, reset_at: KST(resetMs) },
    { ts: KST(NOW - 60e3), value: 20, reset_at: KST(resetMs) },
  ];
  vm.runInContext(
    `displayedTrend = { range: "7d", version: 1, series: new Map([["codex", [{ key: "rst|weekly_used_pct|", label: "주간", color: "var(--s1)", dash: "", rows: ${JSON.stringify(rows)} }]]]) };
     seriesVisible["rst|weekly_used_pct|"] = true;`,
    sandbox
  );
  const plot = makeEl("div"); plot.clientWidth = W;
  const cur = makeEl("span");
  const card = { dataset: { svc: "codex" }, clientWidth: W,
    querySelector: (sel) => (sel === "[data-plot]" ? plot : sel === "[data-cur]" ? cur : makeEl("div")) };
  drawChart(card, NOW, win);
  const sigBefore = plot.dataset.sig;
  assert.equal((plot.innerHTML.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.7"/g) || []).length, 1, "미래 초기화 선이 하나여야 해요");
  assert.equal((plot.innerHTML.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.4"/g) || []).length, 1, "주간 -7일 선이 하나여야 해요");
  // now가 초기화를 지난 뒤에는 최신 기록의 미래 초기화가 비어 sig가 바뀌고 선이 사라진다 (같은 창이어도).
  drawChart(card, resetMs + 1000, win);
  assert.notEqual(plot.dataset.sig, sigBefore, "now가 초기화를 지나면 sig가 바뀌어야 해요");
  assert.equal((plot.innerHTML.match(/stroke-dasharray="1\.5 3"/g) || []).length, 0, "지난 초기화 선은 사라져야 해요");
});

test("정상 헤더는 실시간 수집 중만 보이고 분 전이 없다", () => {
  const { sandbox, html } = makeSandbox();
  vm.runInContext(
    `loaded = true; fetchFailed = false; skipped = 0;
     RECORDS = [
       { ts: ${JSON.stringify(KST(NOW - 2 * 60e3))}, service: "codex", metric: "weekly_used_pct", value: 20, note: "", unit: "%" },
       { ts: ${JSON.stringify(KST(NOW - 2 * 60e3))}, service: "claude", metric: "weekly_used_pct", value: 30, note: "", unit: "%" },
     ];
     latestByService = new Map([["codex", ${NOW - 2 * 60e3}], ["claude", ${NOW - 2 * 60e3}]]);`,
    sandbox
  );
  vm.runInContext(`renderCollection(${NOW});`, sandbox);
  const statusLabel = sandbox.__byId.get("statusLabel")?.textContent ?? "";
  assert.ok(!statusLabel.includes("분 전"), `정상 헤더에 '분 전'이 있으면 안 돼요: ${statusLabel}`);
  assert.ok(statusLabel.includes("실시간 수집 중"), `정상 헤더는 '실시간 수집 중'이어야 해요: ${statusLabel}`);
  // 슬롯 타이머는 표시 없이 동작한다: 서버가 내리는 껍질에 표시용 span이 없다.
  assert.ok(html.includes('id="statusLabel"'), "헤더 라벨 마크업이 있어야 해요");
  assert.ok(!html.includes('class="cnt"'), "표시용 span이 있으면 안 돼요");
});

test("지연 헤더는 N분째를 보인다", () => {
  const { sandbox } = makeSandbox();
  vm.runInContext(
    `loaded = true; fetchFailed = false; skipped = 0;
     RECORDS = [
       { ts: ${JSON.stringify(KST(NOW - 17 * 60e3))}, service: "codex", metric: "weekly_used_pct", value: 20, note: "", unit: "%" },
       { ts: ${JSON.stringify(KST(NOW - 2 * 60e3))}, service: "claude", metric: "weekly_used_pct", value: 30, note: "", unit: "%" },
     ];
     latestByService = new Map([["codex", ${NOW - 17 * 60e3}], ["claude", ${NOW - 2 * 60e3}]]);`,
    sandbox
  );
  vm.runInContext(`renderCollection(${NOW});`, sandbox);
  const statusLabel = sandbox.__byId.get("statusLabel")?.textContent ?? "";
  assert.ok(/Codex\s+17분째/.test(statusLabel), `지연 헤더는 'Codex 17분째'를 보여야 해요: ${statusLabel}`);
  // 카드(또는 칩) 쪽에서도 지연 항목만 나이 문구를 받는다.
  const recordAge = vm.runInContext("recordAge", sandbox);
  assert.ok(recordAge({ ts: NOW - 17 * 60e3, age: 17 * 60e3 }).includes("분 전"), "지연 항목은 '기록 …분 전'을 유지해야 해요");
  assert.equal(recordAge({ ts: NOW - 2 * 60e3, age: 2 * 60e3 }), "", "정상 항목은 나이 문구가 없어야 해요");
});

test("3개 이상 지연이면 헤더는 짧아지고 2개까지는 긴 형식을 유지한다", () => {
  const { sandbox } = makeSandbox();
  // 2개 지연: 긴 형식
  vm.runInContext(
    `loaded = true; fetchFailed = false; skipped = 0;
     RECORDS = [
       { ts: ${JSON.stringify(KST(NOW - 17 * 60e3))}, service: "codex", metric: "weekly_used_pct", value: 20, note: "", unit: "%" },
       { ts: ${JSON.stringify(KST(NOW - 14 * 60e3))}, service: "claude", metric: "weekly_used_pct", value: 30, note: "", unit: "%" },
     ];
     latestByService = new Map([["codex", ${NOW - 17 * 60e3}], ["claude", ${NOW - 14 * 60e3}]]);`,
    sandbox
  );
  vm.runInContext(`renderCollection(${NOW});`, sandbox);
  let statusLabel = sandbox.__byId.get("statusLabel")?.textContent ?? "";
  assert.equal(statusLabel, "Codex 17분째 수집 없음 · Claude 14분째 수집 없음", `2개 지연 헤더: ${statusLabel}`);
  // 3개 지연: 짧은 형식 (가장 오래 = 가장 나이 많은 Codex 17분)
  vm.runInContext(
    `RECORDS = [
       { ts: ${JSON.stringify(KST(NOW - 17 * 60e3))}, service: "codex", metric: "weekly_used_pct", value: 20, note: "", unit: "%" },
       { ts: ${JSON.stringify(KST(NOW - 14 * 60e3))}, service: "claude", metric: "weekly_used_pct", value: 30, note: "", unit: "%" },
       { ts: ${JSON.stringify(KST(NOW - 12 * 60e3))}, service: "grok", metric: "weekly_used_pct", value: 10, note: "", unit: "%" },
     ];
     latestByService = new Map([["codex", ${NOW - 17 * 60e3}], ["claude", ${NOW - 14 * 60e3}], ["grok", ${NOW - 12 * 60e3}]]);`,
    sandbox
  );
  vm.runInContext(`renderCollection(${NOW});`, sandbox);
  statusLabel = sandbox.__byId.get("statusLabel")?.textContent ?? "";
  assert.equal(statusLabel, "3개 서비스 수집 지연 · 가장 오래 Codex 17분째", `3개 지연 짧은 헤더: ${statusLabel}`);
  // 배너는 전체 상세를 유지한다 (DOM 스텁은 HTML 파싱이 안 되므로 소스로 확인한다).
  const renderBanner = vm.runInContext("renderBanner", sandbox);
  assert.ok(String(renderBanner).includes("delayed.map"), "배너는 전체 지연 목록을 유지해야 해요");
  assert.ok(String(renderBanner).includes("분째"), "배너 상세에 분째가 있어야 해요");
});

test("갱신 실패 헤더는 에러 라벨만 보인다", () => {
  const { sandbox, html } = makeSandbox();
  vm.runInContext(
    `loaded = true; fetchFailed = true; skipped = 0;
     RECORDS = [
       { ts: ${JSON.stringify(KST(NOW - 2 * 60e3))}, service: "codex", metric: "weekly_used_pct", value: 20, note: "", unit: "%" },
     ];
     latestByService = new Map([["codex", ${NOW - 2 * 60e3}]]);`,
    sandbox
  );
  vm.runInContext(`renderCollection(${NOW});`, sandbox);
  const statusLabel = sandbox.__byId.get("statusLabel")?.textContent ?? "";
  assert.equal(statusLabel, "갱신 실패", `갱신 실패 헤더 라벨: ${statusLabel}`);
  assert.ok(!html.includes('class="cnt"'), "실패 헤더에도 표시용 span이 있으면 안 돼요");
});

test("숨긴 탭에서는 슬롯·재시도가 네트워크를 내지 않는다", async () => {
  const { sandbox, fn } = makeSandbox();
  let fetchCalls = 0;
  sandbox.fetch = () => { fetchCalls++; return new Promise(() => {}); };
  // 초기 load는 pending fetch 때문에 loading이 true로 남는다. 가드를 검증하려면 풀어준다.
  vm.runInContext(`loading = false; fetchFailed = false;`, sandbox);
  const withTimeout = (p) => Promise.race([
    p,
    new Promise((_, rej) => setTimeout(() => rej(new Error("타임아웃: 숨김 가드가 없어 load가 대기 중이에요")), 2000)),
  ]);
  // onSlot: 숨김이면 fetch 없이 바로 끝난다.
  sandbox.document.hidden = true;
  await withTimeout(fn("onSlot")());
  assert.equal(fetchCalls, 0, "숨김 onSlot은 fetch를 내지 않아야 해요");
  // slot retry: 숨김이면 타이머를 잡지 않는다.
  let captured = null;
  sandbox.setTimeout = (cb, ms) => { captured = { cb, ms }; return 999; };
  vm.runInContext(
    `latestByService = new Map([["codex", ${NOW - 17 * 60e3}]]); slotBaseline = ${NOW - 17 * 60e3}; slotRetrySince = Date.now();`,
    sandbox
  );
  captured = null;
  await withTimeout(fn("slotMaybeRetry")());
  assert.equal(captured, null, "숨김에서는 재시도 타이머를 잡지 않아야 해요");
  // 보이면 15초 간격으로 잡힌다 (재시도 부하 감소).
  sandbox.document.hidden = false;
  captured = null;
  await withTimeout(fn("slotMaybeRetry")());
  assert.ok(captured, "보이면 재시도 타이머가 잡혀야 해요");
  assert.equal(captured.ms, 15_000, "재시도 간격은 15초여야 해요");
  // 재시도 콜백: 숨김이면 실제 load를 내지 않는다.
  const retryCb = captured.cb;
  sandbox.document.hidden = true;
  vm.runInContext(`loading = false;`, sandbox);
  fetchCalls = 0;
  await withTimeout(retryCb());
  assert.equal(fetchCalls, 0, "숨김 재시도 콜백은 fetch를 내지 않아야 해요");
  sandbox.document.hidden = false;
});

test("재시도 상한은 slot+45s 시도 뒤 2분이다", async () => {
  const { sandbox, fn } = makeSandbox();
  sandbox.document.hidden = false;
  let captured = null;
  sandbox.setTimeout = (cb, ms) => { captured = { cb, ms }; return 999; };
  // 2분 지난 기준이면 더 잡지 않는다.
  vm.runInContext(
    `latestByService = new Map([["codex", ${NOW - 17 * 60e3}]]); slotBaseline = ${NOW - 17 * 60e3}; slotRetrySince = Date.now() - 121000;`,
    sandbox
  );
  captured = null;
  await fn("slotMaybeRetry")();
  assert.equal(captured, null, "2분이 지나면 재시도를 잡지 않아야 해요");
  // 2분 직전이면 잡는다.
  vm.runInContext(`slotBaseline = ${NOW - 17 * 60e3}; slotRetrySince = Date.now() - 119000;`, sandbox);
  captured = null;
  await fn("slotMaybeRetry")();
  assert.ok(captured, "2분 직전이면 재시도를 잡아야 해요");
  assert.equal(vm.runInContext("SLOT_RETRY_MS", sandbox), 15_000, "SLOT_RETRY_MS 상수");
  assert.equal(vm.runInContext("SLOT_RETRY_CAP", sandbox), 120_000, "SLOT_RETRY_CAP 상수");
});

test("탭이 다시 보이면 슬롯 타이머를 다시 잡는다", () => {
  const { sandbox } = makeSandbox();
  const handlers = (sandbox.__handlers.visibilitychange || []);
  assert.ok(handlers.length >= 1, "visibilitychange 핸들러가 있어야 해요");
  vm.runInContext(`loading = false;`, sandbox);
  let fetchCalls = 0;
  sandbox.fetch = () => { fetchCalls++; return new Promise(() => {}); };
  let captured = null;
  sandbox.setTimeout = (cb, ms) => { captured = { cb, ms }; return 123; };
  sandbox.document.hidden = false;
  for (const h of handlers) h();
  assert.ok(fetchCalls >= 1, "보이면 다시 불러와야 해요");
  assert.ok(captured, "보이면 슬롯 타이머를 다시 잡아야 해요");
  assert.ok(captured.ms > 0 && captured.ms <= 300_000, `슬롯 타이머 지연 범위: ${captured.ms}`);
  assert.equal(typeof captured.cb, "function", "슬롯 타이머 콜백이 있어야 해요");
});

test("목록 표는 기간 경과·기록 열 없이 초기화 절대시각만 보인다", () => {
  const { sandbox, html } = makeSandbox();
  // 정적 헤더 검사 (서버가 내리는 실제 thead).
  const thead = html.match(/<div class="table-wrap lim-list"[^>]*>[\s\S]*?<thead>([\s\S]*?)<\/thead>/i);
  assert.ok(thead, "한도 목록 thead를 찾지 못했어요");
  assert.ok(!thead[1].includes("기간 경과"), "목록 헤더에 '기간 경과'가 있으면 안 돼요");
  assert.ok(!thead[1].includes(">기록<"), "목록 헤더에 '기록' 열이 있으면 안 돼요");
  assert.ok(thead[1].includes(">초기화<"), "목록 헤더에 '초기화'가 있어야 해요");
  assert.ok((thead[1].match(/<th\b/g) || []).length === 5, "목록 열은 5개(서비스·지표·메모·사용률·초기화)여야 해요");

  // 표 렌더링 검사.
  vm.runInContext(
    `loaded = true; fetchFailed = false; skipped = 0;
     LF.v = "list";
     RECORDS = [
       { ts: ${JSON.stringify(KST(NOW - 2 * 60e3))}, service: "codex", metric: "weekly_used_pct", value: 42, note: "Pro", unit: "%", reset_at: ${JSON.stringify(KST(NOW + 3 * 86400e3))} },
       { ts: ${JSON.stringify(KST(NOW - 2 * 60e3))}, service: "claude", metric: "weekly_used_pct", value: 10, note: "", unit: "%", reset_at: ${JSON.stringify(KST(NOW + 1 * 86400e3))} },
     ];
     latestByService = new Map([["codex", ${NOW - 2 * 60e3}], ["claude", ${NOW - 2 * 60e3}]]);`,
    sandbox
  );
  vm.runInContext(`renderLimits(${NOW});`, sandbox);
  const body = sandbox.__byId.get("limBody")?.innerHTML ?? "";
  assert.ok(body.includes("Codex") && body.includes("42"), "표에 샘플 행(Codex 42%)이 있어야 해요");
  assert.ok(!body.includes("기간"), "표 셀에 '기간 경과' 문구가 있으면 안 돼요");
  assert.ok(!body.includes("기록"), "표 셀에 '기록 …분 전' 문구가 있으면 안 돼요");
  // 초기화 셀은 fmtDT 절대시각만: 상대 문구(…후)가 들어가면 안 된다.
  const resetCells = [...body.matchAll(/<td class="c-reset"[^>]*>([\s\S]*?)<\/td>/g)].map((m) => m[1]);
  assert.equal(resetCells.length, 2, "초기화 셀이 행마다 하나씩 있어야 해요");
  for (const cell of resetCells) {
    assert.ok(!cell.includes("후"), `초기화 셀에 상대 문구가 있으면 안 돼요: ${cell}`);
    assert.match(cell, /\d{1,2}월\d{1,2}일\([일월화수목금토]\) \d{2}:\d{2}/, `초기화 셀은 절대시각이어야 해요: ${cell}`);
    assert.ok(!cell.includes("<b>"), `초기화 셀은 <b>로 감싸면 안 돼요: ${cell}`);
  }
  // 지연 칩은 이상 항목에만 붙는다.
  vm.runInContext(
    `latestByService = new Map([["codex", ${NOW - 17 * 60e3}], ["claude", ${NOW - 2 * 60e3}]]);
     RECORDS[0] = { ...RECORDS[0], ts: ${JSON.stringify(KST(NOW - 17 * 60e3))} };`,
    sandbox
  );
  vm.runInContext(`renderLimits(${NOW});`, sandbox);
  const delayedBody = sandbox.__byId.get("limBody")?.innerHTML ?? "";
  assert.ok(delayedBody.includes("수집 지연"), "지연 항목 행에 지연 칩이 있어야 해요");
});

test("한도 목록 초기화 셀은 볼드이면 안 된다", () => {
  const { sandbox, html } = makeSandbox();
  // CSS: td.c-reset b 에 font-weight:700 이 있으면 안 된다.
  const cResetRule = html.match(/\.lim-list td\.c-reset b\s*\{([^}]*)\}/);
  assert.ok(cResetRule, ".lim-list td.c-reset b 규칙을 찾지 못했어요");
  assert.ok(!/font-weight\s*:\s*700/.test(cResetRule[1]), "한도 목록 초기화 셀은 font-weight:700 이면 안 돼요");
  assert.ok(/font-weight\s*:\s*400/.test(cResetRule[1]), "한도 목록 초기화 셀은 font-weight:400 이어야 해요");
  // 카드 쪽 .reset b 는 그대로 두어도 되고, 표 마크업은 <b>로 감싸면 안 된다.
  vm.runInContext(
    `loaded = true; fetchFailed = false; skipped = 0;
     LF.v = "list";
     RECORDS = [
       { ts: ${JSON.stringify(KST(NOW - 2 * 60e3))}, service: "codex", metric: "weekly_used_pct", value: 42, note: "Pro", unit: "%", reset_at: ${JSON.stringify(KST(NOW + 3 * 86400e3))} },
     ];
     latestByService = new Map([["codex", ${NOW - 2 * 60e3}]]);`,
    sandbox
  );
  vm.runInContext(`renderLimits(${NOW});`, sandbox);
  const body = sandbox.__byId.get("limBody")?.innerHTML ?? "";
  const resetCells = [...body.matchAll(/<td class="c-reset"[^>]*>([\s\S]*?)<\/td>/g)].map((m) => m[1]);
  assert.equal(resetCells.length, 1, "초기화 셀이 하나 있어야 해요");
  assert.ok(!resetCells[0].includes("<b>"), `초기화 셀은 <b>로 감싸면 안 돼요: ${resetCells[0]}`);
  assert.match(resetCells[0], /\d{1,2}월\d{1,2}일\([일월화수목금토]\) \d{2}:\d{2}/, `초기화 셀은 절대시각이어야 해요: ${resetCells[0]}`);
});

test("한도 목록 메모 열은 내용 너비로 제한된다", () => {
  const { html } = makeSandbox();
  // 한도 목록(.lim-list)의 메모 열은 내용에 맞게 좁게, 다른 열이 나머지를 받는다.
  const memoRule = html.match(/\.lim-list td\.c-note[^\{]*\{([^}]*)\}/);
  assert.ok(memoRule, ".lim-list td.c-note 규칙을 찾지 못했어요");
  assert.ok(/width\s*:\s*1%/.test(memoRule[1]), `메모 열은 width:1% 로 내용 너비여야 해요: ${memoRule[1]}`);
  assert.ok(/max-width\s*:\s*200px/.test(memoRule[1]), `메모 열은 max-width:200px 로 제한돼야 해요: ${memoRule[1]}`);
  assert.ok(/min-width\s*:\s*140px/.test(memoRule[1]), `메모 열은 글자 단위로 쪼개지지 않게 min-width:140px 이 필요해요: ${memoRule[1]}`);
  assert.ok(/white-space\s*:\s*normal/.test(memoRule[1]), "메모 열은 줄바꿈 가능해야 해요");
  assert.ok(/overflow-wrap\s*:\s*break-word/.test(memoRule[1]) && /word-break\s*:\s*keep-all/.test(memoRule[1]), "메모 열은 단어 단위로 줄바꿈해야 해요");
  assert.ok(!/overflow-wrap\s*:\s*anywhere/.test(memoRule[1]), "overflow-wrap:anywhere 는 width:1% 와 만나 열을 글자 단위로 무너뜨려요");
  // 헤더도 같은 너비 규칙을 받아야 실제 표에서 좁아진다.
  assert.ok(/\.lim-list th:nth-child\(3\)/.test(html), "메모 헤더(.lim-list th:nth-child(3))에 너비 규칙이 있어야 해요");
  // 기록 표의 메모 열은 건드리지 않는다(260px 유지).
  assert.ok(/td\.c-note\s*\{[^}]*max-width\s*:\s*260px/.test(html), "기록 표 메모 열(260px)은 그대로여야 해요");
  // 모바일에서는 카드 레이아웃으로 풀린다.
  assert.ok(/@media\s*\(max-width:719px\)[\s\S]*\.lim-list td\.c-note[^\{]*\{[^}]*max-width\s*:\s*none/.test(html), "모바일에서는 메모 열 상한이 없어야 해요");
});

test("차트는 창 밖 초기화 만기 라벨·툴팁을 보이지 않고 창 안 점선만 남긴다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  const W = 400, H = 200;
  // 모든 범위에서 창 밖 초기화가 만기·title 없이 그려지지 않는다.
  for (const range of ["24h", "7d", "all"]) {
    const earliest = NOW - 7 * 86400e3;
    const win = chartWindow(range, NOW, earliest);
    const beyond = buildChartSvg({
      vis: [fakeSeries("b", NOW - 7 * 86400e3, NOW - 60e3, NOW + 30 * 86400e3)],
      now: NOW, t0: win.t0, t1: win.t1, W, H,
    });
    assert.ok(!beyond.inner.includes("만기"), `${range}: 창 밖 초기화는 '만기 …' 라벨이면 안 돼요`);
    assert.ok(!beyond.inner.includes("<title>"), `${range}: 초기화 점선 세로선에 <title>이 있으면 안 돼요`);
    assert.ok(!beyond.inner.includes("초기화 ·"), `${range}: 초기화 점선 세로선에 툴팁 텍스트가 있으면 안 돼요`);
    // 끝값 % 라벨과 축 틱 라벨은 그대로 남는다.
    assert.ok(beyond.inner.includes("endlab"), `${range}: 끝값 % 라벨(endlab)이 있어야 해요`);
    assert.ok(beyond.inner.includes("0%"), `${range}: % 축 틱 라벨이 있어야 해요`);
  }
  // 창 안 초기화는 점선 세로선이 그대로 있다.
  const earliest = NOW - 7 * 86400e3;
  const win = chartWindow("7d", NOW, earliest);
  const inside = buildChartSvg({
    vis: [fakeSeries("a", NOW - 7 * 86400e3, NOW - 60e3, NOW + 2 * 86400e3)],
    now: NOW, t0: win.t0, t1: win.t1, W, H,
  });
  assert.equal((inside.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 1, "창 안 초기화 점선 세로선이 하나 있어야 해요");
  assert.ok(!inside.inner.includes("<title>"), "창 안 초기화 점선 세로선에 <title>이 있으면 안 돼요");
  assert.ok(!inside.inner.includes("만기"), "창 안 차트에 '만기 …' 라벨이 있으면 안 돼요");
});

test("chartWindow(all)은 가장 먼 미래 초기화를 대칭으로 포함한다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  assert.equal(typeof chartWindow, "function", "chartWindow가 전역 함수로 있어야 해요");
  const DAY = 86400e3;
  const earliest = NOW - 4 * DAY;
  const reset = NOW + 6 * DAY;
  // (a) 가장 먼 초기화(6일)가 대칭 span(4일)을 넘으면 span=6일로 대칭 확장한다.
  const w = chartWindow("all", NOW, earliest, reset);
  const span = reset - NOW;
  assert.equal(w.t0, NOW - span, "t0는 now - span 이어야 해요");
  assert.equal(w.t1, NOW + span, "t1은 now + span 이어야 해요");
  assert.equal((w.t0 + w.t1) / 2, NOW, "now가 정확히 중앙이어야 해요");
  // 왼쪽은 가장 이른 기록보다 앞에 빈 공간이 생길 수 있다 (허용).
  assert.ok(w.t0 < earliest, "왼쪽 빈 공간이 생겨도 된다");
  // 범위 상한 경계에 정확히 닿아도 된다 (이 조건에서는 초과하지 않음).
  const edge = chartWindow("all", NOW, earliest, NOW + 4 * DAY);
  assert.equal(edge.t0, earliest, "초기화가 정확히 span과 같으면 창은 그대로이다");
  assert.equal(edge.t1, NOW + 4 * DAY, "초기화가 정확히 span과 같으면 창은 그대로이다");
});

test("chartWindow(all)은 범위 안 초기화를 무시하고 가장자리 가드를 유지한다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const DAY = 86400e3;
  // (b) 범위 안(2일) 초기화는 창을 바꾸지 않는다.
  const earliest = NOW - 7 * DAY;
  const inside = chartWindow("all", NOW, earliest, NOW + 2 * DAY);
  assert.equal(inside.t0, earliest, "범위 안 초기화는 t0를 바꾸면 안 돼요");
  assert.equal(inside.t1, NOW + 7 * DAY, "범위 안 초기화는 t1을 바꾸면 안 돼요");
  assert.equal((inside.t0 + inside.t1) / 2, NOW, "대칭 중앙이 유지되어야 해요");
  // (b) farthest가 없거나 null/미정의이면 기존 earliest 대칭 그대로이다.
  const noFar = chartWindow("all", NOW, earliest);
  assert.equal(noFar.t0, earliest, "farthest가 없으면 t0는 earliest이다");
  assert.equal(noFar.t1, NOW + (NOW - earliest), "farthest가 없으면 t1은 대칭이다");
  const nullFar = chartWindow("all", NOW, earliest, null);
  assert.equal(nullFar.t0, earliest, "null farthest면 t0는 earliest이다");
  assert.equal(nullFar.t1, NOW + (NOW - earliest), "null farthest면 t1은 대칭이다");
  // earliest >= now 가드: farthest가 없으면 30분 가드 그대로이다.
  const guarded = chartWindow("all", NOW, NOW + 3600e3);
  assert.equal(guarded.t0, NOW - 30 * 60e3, "미래 earliest는 30분 가드이다");
  assert.equal(guarded.t1, NOW + 30 * 60e3, "미래 earliest 대칭 가드이다");
  // 가드 상황에서도 farthest가 더 멀면 대칭 확장한다.
  const guardedFar = chartWindow("all", NOW, NOW + 3600e3, NOW + 2 * DAY);
  assert.equal(guardedFar.t0, NOW - 2 * DAY, "가드보다 먼 farthest는 대칭 확장한다");
  assert.equal(guardedFar.t1, NOW + 2 * DAY, "가드보다 먼 farthest는 대칭 확장한다");
});

test("chartWindow 24h/7d는 farthestReset 인자를 무시한다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const DAY = 86400e3;
  // (c) 24h/7d는 4번째 인자가 아무리 커도 무시한다.
  const earliest = NOW - 4 * DAY;
  const far = NOW + 30 * DAY;
  const w24 = chartWindow("24h", NOW, earliest, far);
  assert.equal(w24.t0, NOW - DAY, "24h t0는 고정이다");
  assert.equal(w24.t1, NOW + DAY, "24h t1은 고정이다");
  const w7 = chartWindow("7d", NOW, earliest, far);
  assert.equal(w7.t0, NOW - 7 * DAY, "7d t0는 고정이다");
  assert.equal(w7.t1, NOW + 7 * DAY, "7d t1은 고정이다");
  // 4번째 인자가 없어도 동일하다 (기존 호출 호환).
  assert.equal(chartWindow("24h", NOW, earliest).t0, w24.t0, "24h 무인자와 유인자는 같아야 해요");
  assert.equal(chartWindow("7d", NOW, earliest).t1, w7.t1, "7d 무인자와 유인자는 같아야 해요");
});

test("all 창은 6일 뒤 초기화를 그려내고 두 카드가 같은 창을 공유한다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  const latestFutureReset = fn("latestFutureReset");
  assert.equal(typeof latestFutureReset, "function", "latestFutureReset이 있어야 해요");
  const DAY = 86400e3;
  const W = 400, H = 200;
  // earliest 4일 Ago, reset 6일 Ahead 케이스 (d).
  const earliest = NOW - 4 * DAY;
  const resetFar = NOW + 6 * DAY;
  const serFar = fakeSeries("far", NOW - 4 * DAY, NOW - 60e3, resetFar);
  const win = chartWindow("all", NOW, earliest, resetFar);
  const outFar = buildChartSvg({ vis: [serFar], now: NOW, t0: win.t0, t1: win.t1, W, H });
  assert.equal((outFar.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 1, "6일 뒤 초기화가 창 안에 있으면 점선이 하나여야 해요");
  assert.equal((win.t0 + win.t1) / 2, NOW, "now가 정확히 중앙이어야 해요");
  assert.ok(!outFar.inner.includes("지금"), "지금 표시가 있으면 안 돼요");
  assert.ok(!outFar.inner.includes("accent-soft"), "미래 음영 rect가 있으면 안 돼요");
  assert.ok(!outFar.inner.includes("<title>"), "점선에 <title>이 있으면 안 돼요");
  assert.ok(!outFar.inner.includes("만기"), "만기 라벨이 있으면 안 돼요");
  // 좁은 창(대칭 4일)으로는 그려지지 않는다는 대조군.
  const narrow = chartWindow("all", NOW, earliest);
  const outNarrow = buildChartSvg({ vis: [serFar], now: NOW, t0: narrow.t0, t1: narrow.t1, W, H });
  assert.equal((outNarrow.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 0, "좁은 창에서는 6일 뒤 초기화가 그려지지 않아야 해요");

  // 두 카드가 서로 다른 farthest를 가져도 창은 한 번에 계산해 동일하다.
  const serA = fakeSeries("a", NOW - 4 * DAY, NOW - 60e3, NOW + 2 * DAY);
  const serB = fakeSeries("b", NOW - 4 * DAY, NOW - 60e3, NOW + 6 * DAY);
  // renderTrends와 같은 규칙으로 계산: 전체 시리즈의 latestFutureReset 최대값.
  const fars = [serA, serB].map((s) => latestFutureReset(s, NOW));
  const farthest = Math.max(...fars);
  assert.equal(farthest, NOW + 6 * DAY, "farthest는 전체 최대값이어야 해요");
  // earliest도 전체 최소값.
  const earliestAll = Math.min(NOW - 4 * DAY, NOW - 4 * DAY);
  const sharedWin = chartWindow("all", NOW, earliestAll, farthest);
  assert.equal(sharedWin.t0, NOW - 6 * DAY, "공유 창 t0는 farthest 기준이어야 해요");
  assert.equal(sharedWin.t1, NOW + 6 * DAY, "공유 창 t1는 farthest 기준이어야 해요");
  // 카드별로 따로 계산하면 serA alone은 좁은 4일 창이 된다 — 공유 창과 달라야 한다.
  const perCardA = chartWindow("all", NOW, earliestAll, latestFutureReset(serA, NOW));
  assert.notEqual(perCardA.t1, sharedWin.t1, "카드별 계산은 공유 창과 달라야 해요 (이것이 공유 창이 필요한 이유)");
  // 공유 창으로 두 카드 모두 그려보면 nowX가 동일하고 점선이 각각 하나이다.
  const outA = buildChartSvg({ vis: [serA], now: NOW, t0: sharedWin.t0, t1: sharedWin.t1, W, H });
  const outB = buildChartSvg({ vis: [serB], now: NOW, t0: sharedWin.t0, t1: sharedWin.t1, W, H });
  assert.equal(outA.nowX, outB.nowX, "두 카드의 now x는 동일해야 해요");
  assert.equal(outA.nowX, 36 + (W - 36 - 48) / 2, "now x가 창 중앙이어야 해요");
  assert.equal((outA.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 1, "A 카드도 점선이 하나여야 해요");
  assert.equal((outB.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 1, "B 카드도 점선이 하나여야 해요");
  // 범례에서 숨겨도 창은 움직이지 않는다: farthest 계산이 숨긴 시리즈까지 포함해야 한다.
  // 숨긴 serB가 있더라도 매핑은 그대로 최대값이다.
  seriesVisibleToggleCheck(serA, serB, NOW, latestFutureReset);
});

function seriesVisibleToggleCheck(serA, serB, NOW, latestFutureReset) {
  // 숨긴(seriesVisible=false) 시리즈가 있어도 farthest는 그 시리즈를 포함한다.
  // renderTrends는 vis 필터링 전에 전체 series.values()로 farthest를 계산한다.
  const DAY = 86400e3;
  void DAY;
  // latestFutureReset은 visibility와 무관하게 최신 기록만으로 결정된다.
  assert.equal(latestFutureReset(serA, NOW), NOW + 2 * DAY, "숨김과 무관하게 최신 미래 초기화");
  assert.equal(latestFutureReset(serB, NOW), NOW + 6 * DAY, "숨김과 무관하게 최신 미래 초기화");
}

test("drawChart는 창 확장에 따라 다시 그려진다", () => {
  const { sandbox, fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const drawChart = fn("drawChart");
  const DAY = 86400e3;
  const W = 400;
  const earliest = NOW - 4 * DAY;
  const resetFar = NOW + 6 * DAY;
  const rows = [
    { ts: KST(NOW - 4 * DAY), value: 10, reset_at: KST(resetFar) },
    { ts: KST(NOW - 60e3), value: 30, reset_at: KST(resetFar) },
  ];
  vm.runInContext(
    `displayedTrend = { range: "all", version: 1, series: new Map([["codex", [{ key: "rst|weekly_used_pct|", label: "주간", color: "var(--s1)", dash: "", rows: ${JSON.stringify(rows)} }]]]) };
     seriesVisible["rst|weekly_used_pct|"] = true;`,
    sandbox
  );
  const plot = makeEl("div"); plot.clientWidth = W;
  const cur = makeEl("span");
  const card = { dataset: { svc: "codex" }, clientWidth: W,
    querySelector: (sel) => (sel === "[data-plot]" ? plot : sel === "[data-cur]" ? cur : makeEl("div")) };
  const narrowWin = chartWindow("all", NOW, earliest);
  const broadWin = chartWindow("all", NOW, earliest, resetFar);
  assert.equal((narrowWin.t0 + narrowWin.t1) / 2, NOW, "좁은 창도 대칭이다");
  assert.equal((broadWin.t0 + broadWin.t1) / 2, NOW, "넓은 창도 대칭이다");
  assert.ok(broadWin.t1 > narrowWin.t1, "넓은 창이 더 넓어야 해요");
  drawChart(card, NOW, narrowWin);
  const sigNarrow = plot.dataset.sig;
  const innerNarrow = plot.innerHTML;
  assert.ok(sigNarrow, "처음 drawChart는 sig를 남겨야 해요");
  assert.equal((innerNarrow.match(/stroke-dasharray="1\.5 3"/g) || []).length, 0, "좁은 창에서는 6일 뒤 초기화가 그려지지 않아야 해요");
  drawChart(card, NOW, broadWin);
  const sigBroad = plot.dataset.sig;
  assert.notEqual(sigBroad, sigNarrow, "창이 바뀌면 sig가 바뀌어야 해요");
  assert.equal((plot.innerHTML.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.7"/g) || []).length, 1, "넓은 창에서는 6일 뒤 초기화가 그려져야 해요");
  assert.equal((plot.innerHTML.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.4"/g) || []).length, 1, "넓은 창에서는 -7일 선이 그려져야 해요");
});

test("한도 목록 1·2열은 내용 너비로 좁히고 사용률 열이 남는다", () => {
  const { html } = makeSandbox();
  assert.ok(/\.lim-list th:nth-child\(1\)[^\{]*\{[^}]*width\s*:\s*1%/.test(html), "1열 헤더는 width:1% 로 내용 너비여야 해요");
  assert.ok(/\.lim-list td\.c-svc[^\{]*\{[^}]*width\s*:\s*1%/.test(html), "1열 셀(td.c-svc)은 width:1% 여야 해요");
  assert.ok(/\.lim-list th:nth-child\(2\)[^\{]*\{[^}]*width\s*:\s*1%/.test(html), "2열 헤더는 width:1% 여야 해요");
  assert.ok(/\.lim-list td:nth-child\(2\)[^\{]*\{[^}]*width\s*:\s*1%/.test(html), "2열 셀(td:nth-child(2))은 width:1% 여야 해요");
  assert.ok(/\.lim-list th:nth-child\(1\)[^\{]*\{[^}]*white-space\s*:\s*nowrap/.test(html), "1열은 nowrap 으로 내용 너비여야 해요");
  assert.ok(/\.lim-list td\.c-svc[^\{]*\{[^}]*white-space\s*:\s*nowrap/.test(html), "1열 셀은 nowrap 이어야 해요");
  assert.ok(/\.lim-list td:nth-child\(2\)[^\{]*\{[^}]*white-space\s*:\s*nowrap/.test(html), "2열 셀은 nowrap 이어야 해요");
  assert.ok(!/\.lim-list td\.c-val\s*\{[^}]*width\s*:\s*25%/.test(html), "사용률 열은 width:25% 면 안 돼요");
  assert.ok(/\.lim-list td\.c-val\s*\{[^}]*width\s*:\s*auto/.test(html), "사용률 열은 width:auto 로 유연 열이어야 해요");
  assert.ok(/\.lim-list th:nth-child\(4\)[^\{]*\{[^}]*width\s*:\s*auto/.test(html), "사용률 헤더도 width:auto 여야 해요");
  assert.ok(/@media\s*\(max-width:719px\)[\s\S]*\.lim-list th:nth-child\(1\)[^\{]*\{[^}]*width\s*:\s*auto/.test(html), "모바일에서는 1열 헤더 width:auto 여야 해요");
  assert.ok(/@media\s*\(max-width:719px\)[\s\S]*\.lim-list td\.c-svc[^\{]*\{[^}]*width\s*:\s*auto/.test(html), "모바일에서는 1열 셀 width:auto 여야 해요");
  assert.ok(/@media\s*\(max-width:719px\)[\s\S]*\.lim-list th:nth-child\(2\)[^\{]*\{[^}]*width\s*:\s*auto/.test(html), "모바일에서는 2열 헤더 width:auto 여야 해요");
  assert.ok(/@media\s*\(max-width:719px\)[\s\S]*\.lim-list td:nth-child\(2\)[^\{]*\{[^}]*width\s*:\s*auto/.test(html), "모바일에서는 2열 셀 width:auto 여야 해요");
});

test("최신 기록값 0이면 미래 초기화로 창을 넓히지 않는다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  const latestFutureReset = fn("latestFutureReset");
  const DAY = 86400e3, W = 400, H = 200;
  const mk = (svc, latestValue, resetMs, firstAgo = 6 * DAY) => ({
    key: `${svc}|weekly_used_pct|`,
    label: "주간",
    color: "var(--s1)",
    dash: "",
    rows: [
      { ts: KST(NOW - firstAgo), value: 10, reset_at: KST(resetMs) },
      { ts: KST(NOW - 60e3), value: latestValue, reset_at: KST(resetMs) },
    ],
  });
  const resetCount = (inner) => (inner.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.7"/g) || []).length;
  const weekCount = (inner) => (inner.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.4"/g) || []).length;
  const win7 = chartWindow("7d", NOW, NOW - 7 * DAY);
  // 0인 시리즈(muse): 창 안 미래 초기화라도 선이 없다.
  const zero = mk("muse", 0, NOW + 2 * DAY);
  assert.equal(latestFutureReset(zero, NOW), null, "최신 0이면 latestFutureReset은 null 이야");
  const outZero = buildChartSvg({ vis: [zero], now: NOW, t0: win7.t0, t1: win7.t1, W, H, range: "7d" });
  assert.equal(resetCount(outZero.inner), 0, "최신 0이면 초기화 선이 없어야 해요");
  assert.equal(weekCount(outZero.inner), 0, "최신 0이면 -7일 선도 없어야 해요");
  assert.equal((outZero.inner.match(/stroke-dasharray="1\.5 3"/g) || []).length, 0, "최신 0이면 점선이 전혀 없어야 해요");
  // 7인 시리즈(codex): 같은 초기화에서 선이 있다.
  const seven = mk("codex", 7, NOW + 2 * DAY);
  assert.equal(latestFutureReset(seven, NOW), NOW + 2 * DAY, "최신 7이면 미래 초기화를 돌려줘야 해요");
  const outSeven = buildChartSvg({ vis: [seven], now: NOW, t0: win7.t0, t1: win7.t1, W, H, range: "7d" });
  assert.equal(resetCount(outSeven.inner), 1, "최신 7이면 초기화 선이 하나 있어야 해요");
  assert.equal(weekCount(outSeven.inner), 1, "최신 7이면 -7일 선이 하나 있어야 해요");
  // all 창 farthestReset: 0은 창을 넓히지 않고 7은 넓힌다.
  const earliest = NOW - 4 * DAY, resetFar = NOW + 6 * DAY;
  const zeroFar = mk("muse", 0, resetFar, 4 * DAY);
  const sevenFar = mk("codex", 7, resetFar, 4 * DAY);
  const farZero = latestFutureReset(zeroFar, NOW);
  const farSeven = latestFutureReset(sevenFar, NOW);
  assert.equal(farZero, null, "0인 farthest는 null 이야");
  assert.equal(farSeven, resetFar, "7인 farthest는 그 값이야");
  const winZero = chartWindow("all", NOW, earliest, farZero);
  assert.equal(winZero.t0, earliest, "0이면 창 t0는 earliest 그대로");
  assert.equal(winZero.t1, NOW + 4 * DAY, "0이면 창 t1은 대칭 4일 그대로");
  const winSeven = chartWindow("all", NOW, earliest, farSeven);
  assert.equal(winSeven.t0, NOW - 6 * DAY, "7이면 창 t0는 farthest 기준");
  assert.equal(winSeven.t1, NOW + 6 * DAY, "7이면 창 t1은 farthest 기준");
  // num() 경계: null·빈 문자열은 0이 아니다 (Number(null)===0 과 다르다).
  const nullLatest = mk("codex", null, NOW + 2 * DAY);
  assert.equal(latestFutureReset(nullLatest, NOW), NOW + 2 * DAY, "null은 0이 아니어서 선이 있어야 해요");
});

test("주간 시리즈는 7d·all에서 -7일 선을 함께 그린다", () => {
  const { fn } = makeSandbox();
  const chartWindow = fn("chartWindow");
  const buildChartSvg = fn("buildChartSvg");
  const DAY = 86400e3, W = 400, H = 200;
  const resetCount = (inner) => (inner.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.7"/g) || []).length;
  const weekCount = (inner) => (inner.match(/stroke-dasharray="1\.5 3"[^>]*opacity="\.4"/g) || []).length;
  const mkWeekly = (resetMs, firstAgo = 6 * DAY, latest = 20) => ({
    key: "codex|weekly_used_pct|",
    label: "주간",
    color: "var(--s1)",
    dash: "",
    rows: [
      { ts: KST(NOW - firstAgo), value: 10, metric: "weekly_used_pct", reset_at: KST(resetMs) },
      { ts: KST(NOW - 60e3), value: latest, metric: "weekly_used_pct", reset_at: KST(resetMs) },
    ],
  });
  const mkSession = (resetMs) => ({
    key: "codex|session_used_pct|",
    label: "세션",
    color: "var(--s1)",
    dash: "",
    rows: [
      { ts: KST(NOW - 6 * DAY), value: 10, metric: "session_used_pct", reset_at: KST(resetMs) },
      { ts: KST(NOW - 60e3), value: 20, metric: "session_used_pct", reset_at: KST(resetMs) },
    ],
  });
  // 7d: 창 안 미래 주간 초기화는 초기화+(-7일) 두 줄.
  const reset7 = NOW + 2 * DAY, week7 = reset7 - 7 * DAY;
  const win7 = chartWindow("7d", NOW, NOW - 7 * DAY);
  const out7 = buildChartSvg({ vis: [mkWeekly(reset7)], now: NOW, t0: win7.t0, t1: win7.t1, W, H, range: "7d" });
  assert.equal(resetCount(out7.inner), 1, "7d 주간은 초기화 선 하나");
  assert.equal(weekCount(out7.inner), 1, "7d 주간은 -7일 선 하나");
  assert.ok(out7.inner.includes(`<line x1="${out7.X(reset7)}"`), "초기화 선 위치");
  assert.ok(out7.inner.includes(`<line x1="${out7.X(week7)}"`), "-7일 선 위치");
  assert.ok(out7.inner.includes('stroke="var(--s1)" stroke-width="1.5" stroke-dasharray="1.5 3"'), "-7일 선은 같은 색·같은 점선");
  assert.ok(!out7.inner.includes("<title>"), "-7일 선에 title이 있으면 안 돼요");
  assert.ok(out7.inner.indexOf('opacity=".4"') < out7.inner.indexOf("<path"), "-7일 선은 데이터 경로 아래에 그려야 해요");
  assert.ok(out7.inner.indexOf('opacity=".7"') < out7.inner.indexOf("<path"), "초기화 선은 데이터 경로 아래에 그려야 해요");
  assert.ok(!out7.inner.includes("지금"), "지금 표시가 있으면 안 돼요");
  // all: 넓은 창에서는 두 줄.
  const resetAll = NOW + 2 * DAY, weekAll = resetAll - 7 * DAY;
  const winAll = chartWindow("all", NOW, NOW - 6 * DAY, resetAll);
  assert.equal(winAll.t0, NOW - 6 * DAY, "all 넓은 창 t0");
  const outAll = buildChartSvg({ vis: [mkWeekly(resetAll, 5 * DAY)], now: NOW, t0: winAll.t0, t1: winAll.t1, W, H, range: "all" });
  assert.equal(resetCount(outAll.inner), 1, "all 주간은 초기화 선 하나");
  assert.equal(weekCount(outAll.inner), 1, "all 주간은 -7일 선 하나");
  assert.ok(outAll.inner.includes(`<line x1="${outAll.X(weekAll)}"`), "all -7일 선 위치");
  // 24h: 창 안 미래라도 -7일 선은 절대 그리지 않는다.
  const mk24 = (resetMs) => ({
    key: "codex|weekly_used_pct|",
    label: "주간",
    color: "var(--s1)",
    dash: "",
    rows: [
      { ts: KST(NOW - 12 * 3600e3), value: 10, metric: "weekly_used_pct", reset_at: KST(resetMs) },
      { ts: KST(NOW - 60e3), value: 20, metric: "weekly_used_pct", reset_at: KST(resetMs) },
    ],
  });
  const win24 = chartWindow("24h", NOW, NOW - DAY);
  const out24 = buildChartSvg({ vis: [mk24(NOW + 12 * 3600e3)], now: NOW, t0: win24.t0, t1: win24.t1, W, H, range: "24h" });
  assert.equal(resetCount(out24.inner), 1, "24h 창 안은 초기화 선 하나");
  assert.equal(weekCount(out24.inner), 0, "24h는 -7일 선이 없어야 해요");
  const out24Out = buildChartSvg({ vis: [mk24(NOW + 2 * DAY)], now: NOW, t0: win24.t0, t1: win24.t1, W, H, range: "24h" });
  assert.equal(resetCount(out24Out.inner), 0, "24h 창 밖은 초기화 선이 없어야 해요");
  assert.equal(weekCount(out24Out.inner), 0, "24h 창 밖은 -7일 선이 없어야 해요");
  // session: 7d라도 -7일 선이 없다.
  const outSess = buildChartSvg({ vis: [mkSession(NOW + 2 * DAY)], now: NOW, t0: win7.t0, t1: win7.t1, W, H, range: "7d" });
  assert.equal(resetCount(outSess.inner), 1, "session도 초기화 선은 있다");
  assert.equal(weekCount(outSess.inner), 0, "session은 -7일 선이 없어야 해요");
  // -7일이 창 밖이면 그리지 않는다 (all 좁은 창: reset 안, -7일 밖).
  const narrowAll = chartWindow("all", NOW, NOW - 4 * DAY);
  const outNarrow = buildChartSvg({ vis: [mkWeekly(NOW + 2 * DAY, 3 * DAY)], now: NOW, t0: narrowAll.t0, t1: narrowAll.t1, W, H, range: "all" });
  assert.equal(resetCount(outNarrow.inner), 1, "좁은 창 안 reset은 있다");
  assert.equal(weekCount(outNarrow.inner), 0, "-7일이 창 밖이면 그리지 않아야 해요");
  // 0 사용 주간은 7d에서도 둘 다 없다.
  const outZero = buildChartSvg({ vis: [mkWeekly(NOW + 2 * DAY, 6 * DAY, 0)], now: NOW, t0: win7.t0, t1: win7.t1, W, H, range: "7d" });
  assert.equal(resetCount(outZero.inner), 0, "0이면 초기화 선이 없어야 해요");
  assert.equal(weekCount(outZero.inner), 0, "0이면 -7일 선이 없어야 해요");
  // weekStart 불리언 그리기 형식도 같은 동작이다.
  const outBool = buildChartSvg({ vis: [mkWeekly(reset7)], now: NOW, t0: win7.t0, t1: win7.t1, W, H, weekStart: true });
  assert.equal(weekCount(outBool.inner), 1, "weekStart true면 -7일 선이 있어야 해요");
  const outBoolOff = buildChartSvg({ vis: [mkWeekly(reset7)], now: NOW, t0: win7.t0, t1: win7.t1, W, H, weekStart: false });
  assert.equal(weekCount(outBoolOff.inner), 0, "weekStart false면 -7일 선이 없어야 해요");
});

test("html 동기화와 스크립트 구문이 유지된다", () => {
  const html = loadHtml();
  const scripts = extractScripts(html);
  assert.ok(scripts.length >= 2, "인라인 스크립트를 찾지 못했어요");
  for (let i = 0; i < scripts.length; i++) {
    assert.doesNotThrow(() => new Function(scripts[i]), `스크립트 #${i + 1} 구문 오류`);
  }
  // server.py INDEX_HTML 과 html.js 동기화 (네트워크 없이 로컬만).
  // cwd에 따라 repo 상대 경로가 달라지므로 절대 repo 루트를 cwd로 준다.
  const out = execSync(
    "python3 -c \"import importlib.util,sys; from pathlib import Path; spec=importlib.util.spec_from_file_location('p',Path('worker/scripts/promote-dev-to-prod.py')); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); h=m.load_index_html(); exp=m.expected_html_js(h); act=Path('worker/src/html.js').read_text(encoding='utf-8'); sys.exit(0 if act==exp else 1)\"",
    { encoding: "utf8", cwd: REPO_ROOT }
  );
  assert.equal(out.status ?? 0, 0, "html.js가 server.py와 다릅니다");
});
