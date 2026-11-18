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
CREATE INDEX IF NOT EXISTS idx_records_series_ts ON records (service, metric, COALESCE(note, ''), ts_kst);

-- 한 행이 대표할 수 있는 가장 긴 버킷. 바로 다음 기록이 다른 버킷에 있으면 이 행이 버킷의 마지막이다.
-- 0 원본, 1 30분, 2 1시간, 3 3시간, 4 6시간, 5 감소 양끝, 6 시리즈 시작, 7 시리즈 최신.
CREATE VIEW IF NOT EXISTS trend_neighbors AS
SELECT r.id, r.ts_kst, p.id AS prev_id, n.id AS next_id,
  CASE
    WHEN n.id IS NULL THEN 7
    WHEN p.id IS NULL THEN 6
    WHEN r.value < p.value OR n.value < r.value THEN 5
    WHEN floor(unixepoch(r.ts_kst) / 21600.0) != floor(unixepoch(n.ts_kst) / 21600.0) THEN 4
    WHEN floor(unixepoch(r.ts_kst) / 10800.0) != floor(unixepoch(n.ts_kst) / 10800.0) THEN 3
    WHEN floor(unixepoch(r.ts_kst) / 3600.0) != floor(unixepoch(n.ts_kst) / 3600.0) THEN 2
    WHEN floor(unixepoch(r.ts_kst) / 1800.0) != floor(unixepoch(n.ts_kst) / 1800.0) THEN 1
    ELSE 0
  END AS sample_rank
FROM records r
LEFT JOIN records p ON p.id = (
  SELECT id FROM records WHERE service = r.service AND metric = r.metric
    AND COALESCE(note, '') = COALESCE(r.note, '') AND ts_kst < r.ts_kst
  ORDER BY ts_kst DESC LIMIT 1)
LEFT JOIN records n ON n.id = (
  SELECT id FROM records WHERE service = r.service AND metric = r.metric
    AND COALESCE(note, '') = COALESCE(r.note, '') AND ts_kst > r.ts_kst
  ORDER BY ts_kst LIMIT 1);

CREATE TABLE IF NOT EXISTS trend_points (
  id INTEGER PRIMARY KEY,
  ts_kst TEXT NOT NULL,
  sample_rank INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trend_rank_ts ON trend_points (sample_rank DESC, ts_kst DESC, id);

-- 늦게 들어온 기록도 같은 시리즈의 양옆만 바꾼다. 중복 INSERT OR IGNORE는 트리거를 실행하지 않는다.
CREATE TRIGGER IF NOT EXISTS records_index_trend AFTER INSERT ON records BEGIN
  INSERT INTO trend_points (id, ts_kst, sample_rank)
  SELECT id, ts_kst, sample_rank FROM trend_neighbors WHERE id IN (
    NEW.id, (SELECT prev_id FROM trend_neighbors WHERE id = NEW.id),
    (SELECT next_id FROM trend_neighbors WHERE id = NEW.id)
  )
  ON CONFLICT(id) DO UPDATE SET sample_rank = excluded.sample_rank;
END;
-- 기존 DB에 schema.sql을 처음 적용할 때만 과거 기록의 등급을 계산한다.
INSERT INTO trend_points (id, ts_kst, sample_rank)
SELECT id, ts_kst, sample_rank FROM trend_neighbors n
WHERE NOT EXISTS (SELECT 1 FROM trend_points WHERE id = n.id);

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
