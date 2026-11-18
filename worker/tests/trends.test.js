import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import worker from "../src/index.js";

const schema = readFileSync(new URL("../schema.sql", import.meta.url), "utf8");
const now = Date.parse("2026-11-18T12:25:05+09:00");
const tick = 300000;
const kst = ms => new Date(ms + 9 * 3600e3).toISOString().slice(0, 19) + "+09:00";

// 실제 Worker 라우터와 SQL을 메모리 SQLite에서 실행한다. 캐시는 URL별 읽기 횟수도 검증한다.
function dashboard(t) {
  const db = new DatabaseSync(":memory:");
  const cache = new Map(), queries = [];
  t.mock.method(Date, "now", () => now);
  globalThis.caches = { default: {
    match: key => cache.get(key.url)?.clone(),
    put: (key, response) => cache.set(key.url, response),
  } };
  t.after(() => { db.close(); delete globalThis.caches; });
  const env = { INGEST_KEY: "fixture", DB: {
    prepare(sql) {
      queries.push(sql);
      const statement = db.prepare(sql);
      return { bind: (...args) => ({
        all: () => ({ results: statement.all(...args) }),
        first: () => statement.get(...args),
      }) };
    },
    batch: statements => statements.map(s => s.all()),
  } };
  const request = (path, options) => worker.fetch(new Request("https://test.local" + path, options), env, { waitUntil() {} });
  async function get(path, options) {
    const response = await request(path, options);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("cache-control"), "no-store");
    return response.json();
  }
  return { db, get, request, queries };
}

function writer(db) {
  const stmt = db.prepare(`INSERT INTO records (ts_kst, service, metric, note, value, data, search, dedupe_key, ingested_at)
    VALUES (?, ?, 'weekly_used_pct', ?, ?, ?, ?, ?, ?)`);
  return (ms, series, value, seq) => {
    const ts = kst(ms), service = "svc", note = "plan-" + series;
    const data = JSON.stringify({ ts, service, metric: "weekly_used_pct", note, value, seq, _private: true });
    stmt.run(ts, service, note, value, data, note, [ts, service, "weekly_used_pct", note].join("|"), ts);
  };
}

test("기존 7일치 20,160건에서 버킷 마지막·감소 양끝·양 끝 시각을 복원한다", async t => {
  const { db, get, queries } = dashboard(t);
  db.exec(schema.slice(0, schema.indexOf("CREATE INDEX"))); // 추이 색인이 없는 기존 DB
  const insert = writer(db), slots = 7 * 24 * 12, start = now - 5000 - (slots - 1) * tick;
  const expected = new Set(), lastInBucket = new Map();
  db.exec("BEGIN");
  for (let s = 0; s < 10; s++) {
    for (let i = 0; i < slots; i++) {
      const ts = start + i * tick, value = (i + s) % 83, seq = s * slots + i;
      insert(ts, s, value, seq);
      lastInBucket.set(s + "/" + Math.floor(ts / 1800000), seq);
      if (i === 0 || i === slots - 1) expected.add(seq);
      if (i > 0 && value === 0) { expected.add(seq - 1); expected.add(seq); }
    }
  }
  db.exec("COMMIT");
  db.exec(schema);
  db.exec(schema); // 재적용
  for (const seq of lastInBucket.values()) expected.add(seq);
  const week = await get("/api/trends?range=7d");
  assert.equal(week.bucket_minutes, 30);
  assert.equal(week.truncated, false);
  assert.equal(week.count, expected.size);
  assert.ok(week.count < 4000);
  assert.deepEqual(new Set(week.records.map(r => r.seq)), expected);
  assert.ok(week.records.every((r, i, rows) => !("_private" in r) && (!i || rows[i - 1].ts >= r.ts)));
  const reads = queries.length;
  assert.equal((await get("/api/trends?range=7d")).count, week.count);
  assert.equal(queries.length, reads);
  assert.equal((await get("/api/trends?range=24h")).count, 2880);
  assert.equal((await get("/api/trends?range=all")).bucket_minutes, 60);
  assert.ok(queries.every(q => !/COUNT\s*\(/i.test(q)));
});

test("늦은 수집·중복 재전송·미래 시각과 최근 페이지 밖의 시리즈", async t => {
  const { db, get, request, queries } = dashboard(t);
  db.exec(schema);
  const record = (offset, value, note = "active") => ({
    ts: kst(now + offset), service: "svc", metric: "weekly_used_pct", value, note,
  });
  const points = [record(-1000, 82), record(-4000, 80), record(-3000, 2), record(-2000, 45),
    record(60000, 95), record(-70 * 86400e3, 7, "paused")];
  const post = () => get("/api/ingest", { method: "POST", headers: { Authorization: "Bearer fixture" }, body: JSON.stringify(points) });
  assert.equal((await post()).inserted, points.length);
  assert.equal((await post()).duplicates, points.length);
  const all = await get("/api/trends?range=all");
  assert.equal(all.bucket_minutes, 360);
  assert.deepEqual(all.records.map(r => r.value), [82, 2, 80, 7]);
  const recent = await get("/api/records?latest=1&total=0&limit=1");
  assert.deepEqual([recent.total, recent.count, recent.has_more], [null, 1, true]);
  assert.deepEqual(recent.latest.map(r => r.note), ["active", "paused"]);
  assert.ok(queries.every(q => !/COUNT\s*\(/i.test(q)));
  const legacy = await get("/api/records?limit=1");
  assert.equal(legacy.total, 6);
  assert.equal(legacy.latest, undefined);
  const filtered = await get("/api/records?latest=1&since=" + encodeURIComponent(kst(now - 3000)));
  assert.equal(filtered.count, 4);
  assert.deepEqual(filtered.latest.map(r => r.note), ["active"]);
  for (const path of ["/api/trends?range=", "/api/trends?range=oops", "/api/records?since=?"]) {
    const res = await request(path);
    assert.equal(res.status, 400);
    assert.equal(res.headers.get("access-control-allow-origin"), "*");
    assert.equal(typeof (await res.json()).error, "string");
  }
});

test("상한에서 오래된 시리즈 최신을 보존하고 실제 초과 여부를 구분한다", async t => {
  const { db, get } = dashboard(t);
  db.exec(schema);
  const insert = writer(db);
  db.exec("BEGIN");
  insert(now - 86400e3, 1, 9, -1);
  for (let i = 1; i < 20000; i++) insert(now - i * 1000, 0, i % 2, i);
  db.exec("COMMIT");
  assert.equal((await get("/api/trends?range=24h")).truncated, false);
  insert(now, 0, 0, 0);
  const page = await get("/api/trends?range=24h&v=2");
  assert.deepEqual([page.count, page.limit, page.truncated], [20000, 20000, true]);
  assert.equal(page.records[0].seq, 0);
  assert.equal(page.records.at(-1).seq, -1);
});
