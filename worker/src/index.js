// 토큰 사용량 대시보드 Worker — Cloudflare Workers(무료) + D1
// server.py(Python)와 같은 화면·API 모양을 D1에서 돌려줍니다.
import INDEX_HTML from "./html.js";

const COOKIE_NAME = "raw_session";
const SESSION_TTL_S = 12 * 3600;
const MAX_LOGIN_FAILS = 5;
const LOGIN_FAIL_WINDOW_S = 60;
const LOGIN_BODY_MAX = 4096;
const MAX_RAW_FILES = 8;
const MAX_RAW_BYTES = 64 * 1024; // 화면에 보여 주는 최대 크기
const MAX_RAW_UPLOAD_CHARS = 200 * 1024;
const MAX_RECORDS_DEFAULT = 5000;
const MAX_TREND_POINTS = 20000;
const TREND_MINUTES = [0, 30, 60, 180, 360]; // schema.sql의 sample_rank 순서. 0은 원본.
const LATEST_RANK = 7;
const MAX_INGEST_RECORDS = 40; // 무료 플랜: Worker 한 번 호출당 D1 질의 50개 제한을 넘지 않도록
const MAX_INGEST_BODY = 256 * 1024;
const MAX_RECORD_BYTES = 4096;
const RAW_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._\-]{0,200}$/;
const KNOWN = new Set(["ts", "service", "metric", "value", "unit", "reset_at", "note", "source"]);
const enc = new TextEncoder();

// ---------- 공통 ----------
function toKstIso(ms) {
  return new Date(ms + 9 * 3600e3).toISOString().slice(0, 19) + "+09:00";
}
function nowKst() {
  return toKstIso(Date.now());
}
const TS_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$/i;
function parseTs(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  if (!text) return null;
  const m = TS_RE.exec(text);
  if (!m) return null;
  const [, Y, Mo, D, h = "00", mi = "00", s = "00", frac = "", tz = "+09:00"] = m;
  let zone = tz.toUpperCase();
  if (zone !== "Z" && !zone.includes(":")) zone = zone.slice(0, 3) + ":" + zone.slice(3);
  const iso = `${Y}-${Mo}-${D}T${h}:${mi}:${s}${frac ? "." + frac.slice(0, 3).padEnd(3, "0") : ""}${zone}`;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
}

const SECURITY_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
};

function send(status, body, contentType, { cors = true, cookies = [], extra = {} } = {}) {
  const headers = new Headers({ "Content-Type": contentType, "Cache-Control": "no-store", ...SECURITY_HEADERS, ...extra });
  if (cors) headers.set("Access-Control-Allow-Origin", "*");
  for (const c of cookies) headers.append("Set-Cookie", c);
  return new Response(body, { status, headers });
}
function json(status, payload, opts) {
  return send(status, JSON.stringify(payload), "application/json; charset=utf-8", opts);
}

function hex(buf) {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function unhex(str) {
  const out = new Uint8Array(str.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(str.slice(i * 2, i * 2 + 2), 16);
  return out;
}
async function hmac(secret, message) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(message)));
}
function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}
// 길이·내용과 무관하게 같은 시간이 걸리도록 HMAC 값끼리 비교
async function secretEquals(given, expected) {
  if (!expected) return false;
  const [a, b] = await Promise.all([hmac("cmp", given), hmac("cmp", expected)]);
  return timingSafeEqual(a, b);
}

async function readText(request, max) {
  const declared = Number(request.headers.get("Content-Length") || "0");
  if (declared > max) return null;
  const text = await request.text();
  if (enc.encode(text).length > max) return null;
  return text;
}

// ---------- 원본 열람 비밀번호 / 세션 ----------
async function verifyPassword(env, password) {
  const iterations = parseInt(env.RAW_ITERATIONS || "0", 10);
  if (!password || !env.RAW_SALT || !env.RAW_HASH || !(iterations >= 1)) return false;
  const key = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: unhex(env.RAW_SALT), iterations }, key, 256);
  return timingSafeEqual(new Uint8Array(bits), unhex(env.RAW_HASH));
}
function fingerprint(env) {
  return (env.RAW_HASH || "").slice(0, 16);
}
async function makeSession(env) {
  const exp = Math.floor(Date.now() / 1000) + SESSION_TTL_S;
  const nonce = hex(crypto.getRandomValues(new Uint8Array(16)));
  const sig = hex(await hmac(env.SESSION_SECRET, `${exp}.${nonce}.${fingerprint(env)}`));
  return { value: `${exp}.${nonce}.${sig}`, nonce, exp };
}
function cookieOf(request) {
  const raw = request.headers.get("Cookie") || "";
  for (const part of raw.split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === COOKIE_NAME) return part.slice(i + 1).trim();
  }
  return "";
}
function parseSession(value) {
  const m = /^(\d{1,12})\.([0-9a-f]{32})\.([0-9a-f]{64})$/.exec(value || "");
  return m ? { exp: Number(m[1]), nonce: m[2], sig: m[3] } : null;
}
async function sessionValid(env, request) {
  const s = parseSession(cookieOf(request));
  if (!s || !env.SESSION_SECRET) return false;
  if (s.exp <= Math.floor(Date.now() / 1000)) return false;
  const expected = await hmac(env.SESSION_SECRET, `${s.exp}.${s.nonce}.${fingerprint(env)}`);
  if (!timingSafeEqual(expected, unhex(s.sig))) return false;
  const revoked = await env.DB.prepare("SELECT 1 FROM revoked_sessions WHERE nonce = ?").bind(s.nonce).first();
  return !revoked;
}
function setCookie(value, maxAge) {
  return `${COOKIE_NAME}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`;
}
async function ipKey(request) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  return hex(await crypto.subtle.digest("SHA-256", enc.encode("ip:" + ip))).slice(0, 24);
}
const deny = () => json(401, { error: "unauthorized" }, { cors: false });

async function apiRawLogin(request, env, ctx) {
  const ip = await ipKey(request);
  const since = Math.floor(Date.now() / 1000) - LOGIN_FAIL_WINDOW_S;
  const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM login_fails WHERE ip = ? AND t > ?").bind(ip, since).first();
  if ((row?.n ?? 0) >= MAX_LOGIN_FAILS) {
    return json(429, { error: "시도가 너무 많아요. 잠시 후 다시 시도해 주세요." }, { cors: false });
  }
  const body = await readText(request, LOGIN_BODY_MAX);
  if (body === null) return json(413, { error: "요청이 너무 커요." }, { cors: false });
  const ctype = (request.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
  let password = "";
  if (ctype === "application/x-www-form-urlencoded") {
    password = new URLSearchParams(body).get("password") || "";
  } else {
    try {
      const p = JSON.parse(body || "{}");
      if (p && typeof p === "object" && !Array.isArray(p)) password = String(p.password || "");
    } catch (_) {
      password = "";
    }
  }
  if (!password || !(await verifyPassword(env, password))) {
    ctx.waitUntil(
      env.DB.batch([
        env.DB.prepare("INSERT INTO login_fails (ip, t) VALUES (?, ?)").bind(ip, Math.floor(Date.now() / 1000)),
        env.DB.prepare("DELETE FROM login_fails WHERE t < ?").bind(since - 3600),
      ]).catch(() => {}),
    );
    return json(401, { error: "비밀번호가 올바르지 않아요." }, { cors: false });
  }
  const s = await makeSession(env);
  ctx.waitUntil(
    env.DB.prepare("DELETE FROM revoked_sessions WHERE exp < ?").bind(Math.floor(Date.now() / 1000)).run().catch(() => {}),
  );
  return json(200, { ok: true }, { cors: false, cookies: [setCookie(s.value, SESSION_TTL_S)] });
}
async function apiRawLogout(request, env) {
  const s = parseSession(cookieOf(request));
  if (s) {
    await env.DB.prepare("INSERT OR IGNORE INTO revoked_sessions (nonce, exp) VALUES (?, ?)").bind(s.nonce, s.exp).run();
  }
  return json(200, { ok: true }, { cors: false, cookies: [setCookie("", 0)] });
}

// ---------- 기록 읽기 ----------
function publicRecord(row) {
  let rec;
  try {
    rec = JSON.parse(row.data);
  } catch (_) {
    rec = {};
  }
  const item = {};
  for (const [k, v] of Object.entries(rec)) if (!k.startsWith("_")) item[k] = v;
  item.ts = rec.ts ?? row.ts_kst;
  return item;
}

async function cachedRead(request, ctx, read) {
  const cache = caches.default;
  const cacheKey = new Request(request.url, { method: "GET" }); // 필터·기간을 포함한 전체 URL
  const hit = await cache.match(cacheKey);
  if (hit) {
    const r = new Response(hit.body, hit);
    r.headers.set("Cache-Control", "no-store");
    return r;
  }
  const res = await read();
  if (res.ok) {
    const toCache = new Response(res.clone().body, res);
    toCache.headers.set("Cache-Control", "public, max-age=20");
    ctx.waitUntil(cache.put(cacheKey, toCache));
  }
  return res;
}

async function apiRecords(url, env) {
  const service = (url.searchParams.get("service") || "").trim();
  const metric = (url.searchParams.get("metric") || "").trim();
  const q = (url.searchParams.get("q") || "").trim().toLowerCase();
  const includeLatest = url.searchParams.get("latest") === "1";
  const includeTotal = url.searchParams.get("total") !== "0";
  const limRaw = (url.searchParams.get("limit") ?? String(MAX_RECORDS_DEFAULT)).trim();
  let limit = /^[+-]?\d+$/.test(limRaw) ? parseInt(limRaw, 10) : MAX_RECORDS_DEFAULT;
  limit = Math.max(1, Math.min(limit, 20000));
  const where = [];
  const args = [];
  if (service) { where.push("r.service = ?"); args.push(service); }
  if (metric) { where.push("r.metric = ?"); args.push(metric); }
  if (q) { where.push("instr(r.search, ?) > 0"); args.push(q); }
  if (url.searchParams.has("since")) {
    const since = parseTs(url.searchParams.get("since"));
    if (since === null) return json(400, { error: "`since`는 ISO 8601 시각이어야 해요." });
    where.push("r.ts_kst >= ?"); args.push(toKstIso(since));
  }
  const w = where.length ? " WHERE " + where.join(" AND ") : "";
  const queries = [
    env.DB.prepare(`SELECT data, ts_kst FROM records r${w} ORDER BY ts_kst DESC, id LIMIT ?`).bind(...args, limit + (includeTotal ? 0 : 1)),
  ];
  if (includeTotal) queries.push(env.DB.prepare(`SELECT COUNT(*) AS n FROM records r${w}`).bind(...args));
  if (includeLatest) queries.push(env.DB.prepare(`
    SELECT r.data, r.ts_kst FROM trend_points p JOIN records r ON r.id = p.id AND p.sample_rank = ?
    ${w} ORDER BY p.ts_kst DESC, p.id`).bind(LATEST_RANK, ...args));
  const results = await env.DB.batch(queries);
  const rows = results[0].results;
  const total = includeTotal ? results[1].results[0].n : null;
  const records = rows.slice(0, limit).map(publicRecord);
  return json(200, {
    records,
    count: records.length,
    total,
    has_more: (total ?? rows.length) > limit,
    ...(includeLatest ? { latest: results.at(-1).results.map(publicRecord) } : {}),
    limit,
    skipped: 0,
    generated_at: nowKst(),
    timezone: "Asia/Seoul",
  });
}

async function apiTrends(url, env) {
  const range = url.searchParams.get("range") ?? "7d";
  if (!["24h", "7d", "all"].includes(range)) return json(400, { error: "`range`는 24h, 7d, all 중 하나여야 해요." });
  const now = Date.now(), end = toKstIso(now), day = 86400e3;
  const since = range === "all" ? null : toKstIso(now - (range === "24h" ? 1 : 7) * day);
  let minutes = range === "24h" ? 0 : 30;
  if (range === "all") {
    const span = await env.DB.prepare(`
      SELECT (SELECT ts_kst FROM records WHERE ts_kst <= ?1 ORDER BY ts_kst LIMIT 1) AS oldest,
             (SELECT ts_kst FROM records WHERE ts_kst <= ?1 ORDER BY ts_kst DESC LIMIT 1) AS newest`).bind(end).first();
    const days = span.oldest ? (Date.parse(span.newest) - Date.parse(span.oldest)) / day : 0;
    minutes = days <= 14 ? 60 : days <= 60 ? 180 : 360;
  }
  // 등급별로 시간 인덱스를 탐색한다. 상한에서는 최신, 시작, 초기화 양끝, 큰 버킷 순으로 보존한다.
  const minRank = TREND_MINUTES.indexOf(minutes);
  const ranks = Array.from({ length: LATEST_RANK - minRank + 1 }, (_, i) => minRank + i);
  const { results } = await env.DB.prepare(`
    WITH as_of(id) AS MATERIALIZED (
      -- 미래 시각의 기록이 있는 시리즈도 조회 종료 시각까지의 마지막 값은 포함한다.
      SELECT (SELECT id FROM records WHERE service = f.service AND metric = f.metric
        AND COALESCE(note, '') = COALESCE(f.note, '') AND ts_kst BETWEEN ?1 AND ?2
        ORDER BY ts_kst DESC LIMIT 1)
      FROM trend_points t JOIN records f ON f.id = t.id WHERE t.sample_rank = ?3 AND t.ts_kst > ?2
    ), picks AS (
      SELECT id, ts_kst, sample_rank FROM trend_points
      WHERE sample_rank IN (${ranks.map(() => "?").join(",")}) AND ts_kst BETWEEN ?1 AND ?2
        AND id NOT IN (SELECT id FROM as_of WHERE id IS NOT NULL)
      UNION ALL
      SELECT r.id, r.ts_kst, ?3 AS sample_rank FROM as_of a JOIN records r ON r.id = a.id
      ORDER BY sample_rank DESC, ts_kst DESC, id LIMIT ?
    )
    SELECT r.id, r.ts_kst, r.data FROM picks p JOIN records r ON r.id = p.id
    ORDER BY p.sample_rank DESC, p.ts_kst DESC, p.id
  `).bind(since ?? "", end, LATEST_RANK, ...ranks, MAX_TREND_POINTS + 1).all();
  const truncated = results.length > MAX_TREND_POINTS;
  if (truncated) results.pop();
  results.sort((a, b) => b.ts_kst.localeCompare(a.ts_kst) || a.id - b.id);
  return json(200, {
    records: results.map(publicRecord), range, since, bucket_minutes: minutes,
    count: results.length, limit: MAX_TREND_POINTS, truncated, generated_at: end, timezone: "Asia/Seoul",
  });
}

// ---------- 원본 파일 ----------
async function apiRawList(env) {
  const { results } = await env.DB.prepare(
    "SELECT name, size, created_at FROM raw_files ORDER BY created_at DESC, name DESC LIMIT ?",
  ).bind(MAX_RAW_FILES).all();
  return json(200, { files: results.map((r) => ({ name: r.name, size: r.size, mtime: r.created_at })), generated_at: nowKst() }, { cors: false });
}
async function apiRawFile(request, env) {
  const name = new URL(request.url).searchParams.get("name") || "";
  if (!RAW_NAME_RE.test(name)) return json(404, { error: "not found" }, { cors: false });
  const row = await env.DB.prepare("SELECT name, content, size, created_at FROM raw_files WHERE name = ?").bind(name).first();
  if (!row) return json(404, { error: "not found" }, { cors: false });
  const bytes = enc.encode(row.content);
  return json(200, {
    name: row.name,
    size: row.size,
    mtime: row.created_at,
    truncated: bytes.length > MAX_RAW_BYTES,
    text: new TextDecoder().decode(bytes.slice(0, MAX_RAW_BYTES)),
  }, { cors: false });
}

// ---------- 수집(ingest) ----------
async function ingestAuthorized(request, env) {
  const h = request.headers.get("Authorization") || "";
  const m = /^Bearer\s+(.+)$/i.exec(h);
  return m ? secretEquals(m[1].trim(), env.INGEST_KEY) : false;
}
const unauthorizedIngest = () =>
  json(401, { error: "인증이 필요해요. Authorization: Bearer <INGEST_KEY> 헤더를 보내 주세요." }, { cors: false, extra: { "WWW-Authenticate": 'Bearer realm="token-usage"' } });

function strField(obj, key, { required = false, max }) {
  const v = obj[key];
  if (v === undefined || v === null) return required ? [null, `\`${key}\` 값이 필요해요.`] : [null, null];
  if (typeof v !== "string") return [null, `\`${key}\`는 문자열이어야 해요.`];
  const t = v.trim();
  if (!t) return required ? [null, `\`${key}\` 값이 비어 있어요.`] : [null, null];
  if (t.length > max) return [null, `\`${key}\`는 ${max}자 이하여야 해요.`];
  return [t, null];
}

function normalizeRecord(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { error: "기록은 JSON 객체여야 해요." };
  for (const k of Object.keys(raw)) if (k.startsWith("_")) return { error: `\`${k}\`처럼 밑줄(_)로 시작하는 필드는 쓸 수 없어요.` };
  let err;
  let service, metric, unit, note, source;
  [service, err] = strField(raw, "service", { required: true, max: 64 }); if (err) return { error: err };
  [metric, err] = strField(raw, "metric", { required: true, max: 64 }); if (err) return { error: err };
  [unit, err] = strField(raw, "unit", { max: 20 }); if (err) return { error: err };
  [note, err] = strField(raw, "note", { max: 200 }); if (err) return { error: err };
  [source, err] = strField(raw, "source", { max: 100 }); if (err) return { error: err };
  if (typeof raw.value !== "number" || !Number.isFinite(raw.value)) {
    return { error: "`value`는 숫자여야 해요. (예: 14 또는 7.5)" };
  }
  let ts = null;
  let tsMs;
  if (raw.ts === undefined || raw.ts === null || raw.ts === "") {
    tsMs = Date.now();
    ts = toKstIso(tsMs);
  } else {
    if (typeof raw.ts !== "string") return { error: "`ts`는 ISO 8601 문자열이어야 해요. (예: 2026-11-15T15:20:00+09:00)" };
    tsMs = parseTs(raw.ts);
    if (tsMs === null) return { error: "`ts` 형식이 올바르지 않아요. ISO 8601로 적어 주세요. (예: 2026-11-15T15:20:00+09:00)" };
    ts = raw.ts.trim();
  }
  let resetAt = null;
  if (raw.reset_at !== undefined && raw.reset_at !== null && raw.reset_at !== "") {
    if (typeof raw.reset_at !== "string" || parseTs(raw.reset_at) === null) {
      return { error: "`reset_at` 형식이 올바르지 않아요. ISO 8601로 적어 주세요. (예: 2026-11-17T15:42:00+09:00)" };
    }
    resetAt = raw.reset_at.trim();
  }
  const data = { ts, service, metric, value: raw.value };
  if (unit !== null) data.unit = unit;
  if (resetAt !== null) data.reset_at = resetAt;
  if (note !== null) data.note = note;
  if (source !== null) data.source = source;
  for (const [k, v] of Object.entries(raw)) if (!KNOWN.has(k)) data[k] = v; // 알 수 없는 필드는 그대로 보존
  const dataText = JSON.stringify(data);
  if (enc.encode(dataText).length > MAX_RECORD_BYTES) return { error: `기록 하나는 ${MAX_RECORD_BYTES}바이트 이하여야 해요.` };
  const tsKst = toKstIso(tsMs);
  return {
    row: {
      tsKst, service, metric, value: raw.value, unit, resetAt, note, source, dataText,
      search: [service, metric, note, source, unit, raw.value].map((x) => String(x ?? "")).join(" ").toLowerCase(),
      dedupe: [tsKst, service, metric, note ?? ""].join("|"),
    },
  };
}

async function apiIngest(request, env) {
  if (!(await ingestAuthorized(request, env))) return unauthorizedIngest();
  const text = await readText(request, MAX_INGEST_BODY);
  if (text === null) return json(413, { error: "요청이 너무 커요. (최대 256KB)" }, { cors: false });
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (_) {
    return json(400, { error: "JSON 형식이 올바르지 않아요. 기록 객체 하나 또는 객체 배열을 보내 주세요." }, { cors: false });
  }
  if (payload && !Array.isArray(payload) && typeof payload === "object" && Array.isArray(payload.records)) payload = payload.records;
  const list = Array.isArray(payload) ? payload : [payload];
  if (list.length === 0) return json(400, { error: "보낼 기록이 없어요." }, { cors: false });
  if (list.length > MAX_INGEST_RECORDS) {
    return json(400, { error: `한 번에 최대 ${MAX_INGEST_RECORDS}건까지 보낼 수 있어요. 나눠서 보내 주세요.` }, { cors: false });
  }
  const rows = [];
  const problems = [];
  list.forEach((item, i) => {
    const r = normalizeRecord(item);
    if (r.error) problems.push({ index: i, error: r.error });
    else rows.push(r.row);
  });
  if (problems.length) {
    return json(400, { error: `잘못된 기록이 ${problems.length}건 있어서 아무것도 저장하지 않았어요.`, details: problems.slice(0, 20) }, { cors: false });
  }
  const at = nowKst();
  const stmt = env.DB.prepare(
    "INSERT OR IGNORE INTO records (ts_kst, service, metric, value, unit, reset_at, note, source, data, search, dedupe_key, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
  );
  const results = await env.DB.batch(
    rows.map((r) => stmt.bind(r.tsKst, r.service, r.metric, r.value, r.unit, r.resetAt, r.note, r.source, r.dataText, r.search, r.dedupe, at)),
  );
  const inserted = results.reduce((n, r) => n + r.results.length, 0);
  return json(200, { ok: true, received: rows.length, inserted, duplicates: rows.length - inserted }, { cors: false });
}

async function apiRawUpload(request, env) {
  if (!(await ingestAuthorized(request, env))) return unauthorizedIngest();
  const text = await readText(request, MAX_RAW_UPLOAD_CHARS * 3 + 4096);
  if (text === null) return json(413, { error: "요청이 너무 커요." }, { cors: false });
  let p;
  try {
    p = JSON.parse(text);
  } catch (_) {
    return json(400, { error: 'JSON 형식이 올바르지 않아요. {"name": "...", "content": "..."} 로 보내 주세요.' }, { cors: false });
  }
  if (!p || typeof p !== "object" || Array.isArray(p)) return json(400, { error: "요청 본문은 JSON 객체여야 해요." }, { cors: false });
  if (typeof p.name !== "string" || !RAW_NAME_RE.test(p.name)) {
    return json(400, { error: "`name`은 영문·숫자·점·밑줄·하이픈으로만 이루어진 200자 이하 파일 이름이어야 해요." }, { cors: false });
  }
  if (typeof p.content !== "string") return json(400, { error: "`content`는 문자열이어야 해요." }, { cors: false });
  if (p.content.length > MAX_RAW_UPLOAD_CHARS) return json(400, { error: `\`content\`는 ${MAX_RAW_UPLOAD_CHARS}자 이하여야 해요.` }, { cors: false });
  let created = nowKst();
  if (p.created_at !== undefined && p.created_at !== null) {
    const ms = typeof p.created_at === "string" ? parseTs(p.created_at) : null;
    if (ms === null) return json(400, { error: "`created_at` 형식이 올바르지 않아요. ISO 8601로 적어 주세요." }, { cors: false });
    created = toKstIso(ms);
  }
  const size = enc.encode(p.content).length;
  const prev = await env.DB.prepare("SELECT 1 FROM raw_files WHERE name = ?").bind(p.name).first();
  await env.DB.prepare(
    "INSERT INTO raw_files (name, content, size, created_at) VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET content = excluded.content, size = excluded.size, created_at = excluded.created_at",
  ).bind(p.name, p.content, size, created).run();
  return json(200, { ok: true, name: p.name, size, replaced: !!prev }, { cors: false });
}

// ---------- 라우터 ----------
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method === "HEAD" ? "GET" : request.method;
    try {
      if (method === "GET") {
        if (path === "/" || path === "/index.html") return send(200, INDEX_HTML, "text/html; charset=utf-8");
        if (path === "/api/records") return await cachedRead(request, ctx, () => apiRecords(url, env));
        if (path === "/api/trends") return await cachedRead(request, ctx, () => apiTrends(url, env));
        if (path === "/api/raw") return (await sessionValid(env, request)) ? await apiRawList(env) : deny();
        if (path === "/api/raw/file") return (await sessionValid(env, request)) ? await apiRawFile(request, env) : deny();
        if (path === "/health" || path === "/api/health") return json(200, { ok: true, time: nowKst() });
      } else if (method === "POST") {
        if (path === "/api/raw/login") return await apiRawLogin(request, env, ctx);
        if (path === "/api/raw/logout") return await apiRawLogout(request, env);
        if (path === "/api/ingest") return await apiIngest(request, env);
        if (path === "/api/raw/upload") return await apiRawUpload(request, env);
      }
      if (path === "/api/ingest" || path === "/api/raw/upload") {
        return json(405, { error: "POST로 보내 주세요." }, { cors: false, extra: { Allow: "POST" } });
      }
      return send(404, "Not found\n", "text/plain; charset=utf-8");
    } catch (e) {
      console.log(JSON.stringify({ level: "error", path, message: String(e && e.message || e) }));
      return json(500, { error: "서버에서 문제가 생겼어요. 잠시 후 다시 시도해 주세요." }, { cors: false });
    }
  },
};
