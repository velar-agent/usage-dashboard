-- D1 'token-usage' 스키마
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_kst TEXT NOT NULL,          -- 정규화한 기록 시각(KST, 초 단위) — 정렬용
  service TEXT NOT NULL,
  metric TEXT NOT NULL,
  value REAL NOT NULL,
  unit TEXT,
  reset_at TEXT,
  note TEXT,
  source TEXT,
  data TEXT NOT NULL,            -- 받은 기록 전체(JSON, 추가 필드 포함)
  search TEXT NOT NULL,          -- 검색용 소문자 문자열
  dedupe_key TEXT NOT NULL UNIQUE, -- ts_kst|service|metric|note
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_ts ON records (ts_kst DESC, id);
CREATE INDEX IF NOT EXISTS idx_records_sm ON records (service, metric);
CREATE TABLE IF NOT EXISTS raw_files (
  name TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  size INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_created ON raw_files (created_at DESC);
CREATE TABLE IF NOT EXISTS login_fails (ip TEXT NOT NULL, t INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_login_fails ON login_fails (ip, t);
CREATE TABLE IF NOT EXISTS revoked_sessions (nonce TEXT PRIMARY KEY, exp INTEGER NOT NULL);
