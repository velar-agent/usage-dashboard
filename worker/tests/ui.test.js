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
// 12:03:08 로 두면 다음 슬롯(12:05)까지 '다음 수집 1:52' 가 된다.
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
    // 창 밖(멀리 있는) 초기화는 우측 '만기 …' 라벨이 되어야 한다.
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

  // 창 안에 있는 초기화는 틱으로, 창 밖은 '만기 …' 라벨로 보인다.
  const inside = buildChartSvg({
    vis: [fakeSeries("a", NOW - 7 * 86400e3, NOW - 60e3, NOW + 2 * 86400e3)],
    now: NOW, t0: win.t0, t1: win.t1, W, H,
  });
  assert.ok(inside.inner.includes("초기화"), "창 안쪽 초기화 틱이 있어야 해요");
  const outside = buildChartSvg({
    vis: [fakeSeries("b", NOW - 7 * 86400e3, NOW - 60e3, NOW + 30 * 86400e3)],
    now: NOW, t0: win.t0, t1: win.t1, W, H,
  });
  assert.ok(outside.inner.includes("만기"), "창 밖 초기화는 '만기 …' 라벨이어야 해요");
  assert.ok(!outside.inner.includes("초기화 ·"), "창 밖 초기화는 틱이 아니어야 해요");
});

test("정상 헤더는 실시간 수집 중과 카운트다운을 보이고 분 전이 없다", () => {
  const { sandbox, fn } = makeSandbox();
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
  const nextCollect = sandbox.__byId.get("nextCollect")?.textContent ?? "";
  const headerText = `${statusLabel} ${nextCollect}`;
  assert.ok(!headerText.includes("분 전"), `정상 헤더에 '분 전'이 있으면 안 돼요: ${headerText}`);
  assert.ok(statusLabel.includes("실시간 수집 중"), `정상 헤더는 '실시간 수집 중'이어야 해요: ${statusLabel}`);
  assert.match(nextCollect, /^다음 수집 \d+:\d{2}$/, `카운트다운 형식 오류: ${nextCollect}`);
  // 슬롯 뒤 30초는 '수집 중…' 로 보이고, 그 뒤부터 다음 슬롯까지 카운트다운한다.
  const countdownText = fn("countdownText");
  const slot = Date.parse("2026-11-19T10:05:00+09:00");
  assert.equal(countdownText(slot), "수집 중…", "슬롯 경계에서는 수집 중이어야 해요");
  assert.equal(countdownText(slot + 1000), "수집 중…", "슬롯 직후 30초는 수집 중이어야 해요");
  assert.equal(countdownText(slot + 10_000), "수집 중…", "슬롯 뒤 10초도 수집 중이어야 해요");
  assert.equal(countdownText(slot + 29_000), "수집 중…", "슬롯 뒤 29초도 수집 중이어야 해요");
  assert.equal(countdownText(slot + 29_999), "수집 중…", "슬롯 뒤 30초 직전까지 수집 중이어야 해요");
  assert.equal(countdownText(slot + 30_000), "다음 수집 4:30", "30초가 지나면 카운트다운이어야 해요");
  assert.equal(countdownText(slot + 31_000), "다음 수집 4:29", "30초 뒤 카운트다운이 이어져야 해요");
  assert.equal(countdownText(slot + 45_000), "다음 수집 4:15", "슬롯 뒤 45초 카운트다운이 이어져야 해요");
  assert.equal(countdownText(slot + 299_000), "다음 수집 0:01", "다음 슬롯 직전 카운트다운이어야 해요");
  assert.equal(countdownText(slot + 300_000), "수집 중…", "다음 슬롯 경계에서는 다시 수집 중이어야 해요");
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

test("갱신 실패면 다음 수집 카운트다운을 숨기고 tick도 다시 채우지 않는다", () => {
  const { sandbox, fn } = makeSandbox();
  vm.runInContext(
    `loaded = true; fetchFailed = true; skipped = 0;
     RECORDS = [
       { ts: ${JSON.stringify(KST(NOW - 2 * 60e3))}, service: "codex", metric: "weekly_used_pct", value: 20, note: "", unit: "%" },
     ];
     latestByService = new Map([["codex", ${NOW - 2 * 60e3}]]);`,
    sandbox
  );
  vm.runInContext(`renderCollection(${NOW});`, sandbox);
  const nextCollect = sandbox.__byId.get("nextCollect");
  assert.equal(nextCollect.textContent, "", `fetchFailed면 카운트다운이 비어야 해요: ${nextCollect.textContent}`);
  assert.equal(nextCollect.hidden, true, "fetchFailed면 카운트다운이 hidden이어야 해요");
  // 1초 tick도 숨김을 유지해야 한다 (다시 채우면 1초 뒤에 다시 보인다).
  nextCollect.textContent = "SENTINEL";
  nextCollect.hidden = false;
  fn("tickCountdown")();
  assert.equal(nextCollect.textContent, "", "tick도 fetchFailed면 카운트다운을 채우지 않아야 해요");
  assert.equal(nextCollect.hidden, true, "tick 후에도 hidden이어야 해요");
  // 복구되면 다시 보인다 (NOW=12:03:08 이면 다음 슬롯까지 1:52).
  vm.runInContext(`fetchFailed = false;`, sandbox);
  vm.runInContext(`renderCollection(${NOW});`, sandbox);
  assert.equal(nextCollect.textContent, "다음 수집 1:52", `복구 후 카운트다운: ${nextCollect.textContent}`);
  assert.equal(nextCollect.hidden, false, "복구 후에는 hidden이 아니어야 해요");
});

test("숨긴 탭에서는 슬롯·재시도·카운트다운이 네트워크를 내지 않는다", async () => {
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
  // 카운트다운: 숨김이면 텍스트를 바꾸지 않는다.
  const nextCollect = sandbox.__byId.get("nextCollect");
  nextCollect.textContent = "SENTINEL";
  fn("tickCountdown")();
  assert.equal(nextCollect.textContent, "SENTINEL", "숨김에서는 카운트다운 텍스트를 바꾸지 않아야 해요");
  // 보이면 다시 카운트다운한다.
  sandbox.document.hidden = false;
  fn("tickCountdown")();
  assert.match(nextCollect.textContent, /^(수집 중…|다음 수집 \d+:\d{2})$/, `보이면 카운트다운: ${nextCollect.textContent}`);
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
  assert.equal(vm.runInContext("COUNTDOWN_COLLECTING_MS", sandbox), 30_000, "수집 중 구간 상수");
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
    assert.match(cell, /<b>\d{1,2}\/\d{1,2}\([일월화수목금토]\)\s\d{2}:\d{2}<\/b>/, `초기화 셀은 절대시각이어야 해요: ${cell}`);
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
