//! SQLite storage: one writer connection, pooled read-only connections, schema setup.
//! SQLite 저장소입니다. 쓰기 연결 하나와 읽기 전용 연결 풀, 스키마 생성을 맡습니다.

use rusqlite::{Connection, OpenFlags};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};
use std::time::Duration;

const MAX_IDLE_READERS: usize = 8;

/// Schema steps in order; step N brings a database from version N-1 to version N.
/// 순서대로 적용하는 스키마 단계입니다. N번째 단계는 버전 N-1의 데이터베이스를 버전 N으로 올립니다.
const MIGRATIONS: [&str; 2] = [SCHEMA_V1, SCHEMA_V2];
const SCHEMA_VERSION: i64 = MIGRATIONS.len() as i64;

const SCHEMA_V1: &str = "
CREATE TABLE users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL
);

-- Only the SHA-256 hash of a token is stored; the token itself is shown once at creation.
-- 토큰은 SHA-256 해시만 저장합니다. 토큰 자체는 만들 때 한 번만 보여 줍니다.
CREATE TABLE tokens (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    label         TEXT NOT NULL DEFAULT '',
    token_hash    TEXT NOT NULL UNIQUE,
    hint          TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER,
    revoked_at    INTEGER
);

-- Browser login sessions. A session dies with the token it was created from.
-- 브라우저 로그인 세션입니다. 세션은 그것을 만든 토큰이 폐기되면 함께 무효가 됩니다.
CREATE TABLE sessions (
    id_hash     TEXT PRIMARY KEY,
    token_id    INTEGER NOT NULL REFERENCES tokens(id),
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
) WITHOUT ROWID;

-- One row per inference pass. (user_id, event_id, part) makes resending harmless.
-- 추론 한 번이 행 하나입니다. (user_id, event_id, part) 키 덕분에 다시 보내도 중복되지 않습니다.
CREATE TABLE events (
    user_id                INTEGER NOT NULL REFERENCES users(id),
    event_id               TEXT NOT NULL,
    part                   INTEGER NOT NULL,
    ts                     INTEGER NOT NULL,
    model                  TEXT NOT NULL,
    kind                   TEXT NOT NULL,
    source                 TEXT NOT NULL,
    session_id             TEXT NOT NULL,
    project                TEXT NOT NULL,
    host                   TEXT NOT NULL,
    input_tokens           INTEGER NOT NULL,
    output_tokens          INTEGER NOT NULL,
    cache_write_5m_tokens  INTEGER NOT NULL,
    cache_write_1h_tokens  INTEGER NOT NULL,
    cache_read_tokens      INTEGER NOT NULL,
    thinking_tokens        INTEGER NOT NULL,
    web_search_requests    INTEGER NOT NULL,
    mods                   INTEGER NOT NULL,
    received_at            INTEGER NOT NULL,
    PRIMARY KEY (user_id, event_id, part)
) WITHOUT ROWID;

CREATE INDEX events_ts ON events (ts);
";

/// Version 2 replaces the index on `ts` with one that also carries every column the report
/// queries read, so those queries run from the index alone instead of fetching each row from the
/// table. Measured on 2 million rows, a 30-day report scan dropped from about 370 ms to 140 ms.
/// The column list has to keep covering the queries in report.rs; a test there checks the plans.
/// 버전 2는 `ts` 인덱스를 리포트 조회가 읽는 모든 열을 담은 인덱스로 바꿉니다. 그러면 조회가 행마다
/// 테이블을 찾아가지 않고 인덱스만으로 끝납니다. 200만 행에서 측정했을 때 30일 리포트 조회가
/// 약 370ms에서 140ms로 줄었습니다. 열 목록은 report.rs의 조회를 계속 덮어야 하며, 그쪽 테스트가
/// 실행 계획을 확인합니다.
const SCHEMA_V2: &str = "
DROP INDEX events_ts;
CREATE INDEX events_report ON events (
    ts, user_id, model, project, source, mods, session_id,
    input_tokens, output_tokens, cache_write_5m_tokens, cache_write_1h_tokens,
    cache_read_tokens, thinking_tokens, web_search_requests
);
";

pub struct Db {
    path: PathBuf,
    writer: Mutex<Connection>,
    readers: Mutex<Vec<Connection>>,
}

impl Db {
    /// Open the database file, creating it and its schema when missing.
    /// 데이터베이스 파일을 엽니다. 없으면 파일과 스키마를 만듭니다.
    pub fn open(path: &Path) -> Result<Db, OpenError> {
        let mut conn = Connection::open(path)?;
        configure(&conn)?;
        // WAL lets dashboard reads proceed while a collector batch is being written.
        // WAL 모드에서는 수집 데이터가 기록되는 동안에도 대시보드 조회가 막히지 않습니다.
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        migrate(&mut conn)?;
        Ok(Db {
            path: path.to_path_buf(),
            writer: Mutex::new(conn),
            readers: Mutex::new(Vec::new()),
        })
    }

    /// Run a closure with the single writer connection.
    /// 하나뿐인 쓰기 연결로 클로저를 실행합니다.
    pub fn write<T>(
        &self,
        f: impl FnOnce(&mut Connection) -> rusqlite::Result<T>,
    ) -> rusqlite::Result<T> {
        let mut conn = lock(&self.writer);
        f(&mut conn)
    }

    /// Run a closure with a read-only connection taken from the pool.
    /// 풀에서 꺼낸 읽기 전용 연결로 클로저를 실행합니다.
    pub fn read<T>(
        &self,
        f: impl FnOnce(&Connection) -> rusqlite::Result<T>,
    ) -> rusqlite::Result<T> {
        let pooled = lock(&self.readers).pop();
        let conn = match pooled {
            Some(conn) => conn,
            None => self.open_reader()?,
        };
        let result = f(&conn);
        let mut pool = lock(&self.readers);
        if pool.len() < MAX_IDLE_READERS {
            pool.push(conn);
        }
        result
    }

    fn open_reader(&self) -> rusqlite::Result<Connection> {
        let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let conn = Connection::open_with_flags(&self.path, flags)?;
        configure(&conn)?;
        Ok(conn)
    }
}

/// A poisoned lock only means another thread panicked mid-call; the connection is still usable.
/// 잠금이 오염됐다는 것은 다른 스레드가 호출 도중 패닉했다는 뜻일 뿐이고, 연결은 계속 쓸 수 있습니다.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn configure(conn: &Connection) -> rusqlite::Result<()> {
    conn.busy_timeout(Duration::from_secs(5))?;
    conn.pragma_update(None, "foreign_keys", "ON")?;
    Ok(())
}

/// Why a database could not be opened.
/// 데이터베이스를 열지 못한 이유입니다.
#[derive(Debug)]
pub enum OpenError {
    Sqlite(rusqlite::Error),
    /// The file was written by a newer version of this program.
    /// 이 프로그램의 더 새 버전이 만든 파일입니다.
    NewerSchema {
        found: i64,
        supported: i64,
    },
}

impl std::fmt::Display for OpenError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OpenError::Sqlite(e) => write!(f, "{e}"),
            OpenError::NewerSchema { found, supported } => write!(
                f,
                "schema version {found} is newer than this binary supports ({supported}); upgrade usage-dashboard"
            ),
        }
    }
}

impl std::error::Error for OpenError {}

impl From<rusqlite::Error> for OpenError {
    fn from(e: rusqlite::Error) -> Self {
        OpenError::Sqlite(e)
    }
}

fn migrate(conn: &mut Connection) -> Result<(), OpenError> {
    let version: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    if version > SCHEMA_VERSION {
        return Err(OpenError::NewerSchema {
            found: version,
            supported: SCHEMA_VERSION,
        });
    }
    for (index, step) in MIGRATIONS.iter().enumerate() {
        let target = index as i64 + 1;
        if version < target {
            // Each step commits together with its version number, so an interrupted upgrade
            // resumes at the step that did not finish.
            // 각 단계는 버전 번호와 함께 커밋되므로, 업그레이드가 중간에 끊겨도 끝나지 않은
            // 단계부터 다시 시작합니다.
            let tx = conn.transaction()?;
            tx.execute_batch(step)?;
            tx.pragma_update(None, "user_version", target)?;
            tx.commit()?;
        }
    }
    Ok(())
}

/// Test helper: a database in a unique temporary file that is removed on drop.
/// 테스트 도우미입니다. 고유한 임시 파일에 만든 데이터베이스이며 drop될 때 지워집니다.
pub struct TempDb {
    pub db: std::sync::Arc<Db>,
    path: PathBuf,
}

impl TempDb {
    pub fn new() -> TempDb {
        let path = unique_temp_path();
        let db = std::sync::Arc::new(Db::open(&path).expect("open temp db"));
        TempDb { db, path }
    }
}

fn unique_temp_path() -> PathBuf {
    let mut random = [0u8; 8];
    getrandom::fill(&mut random).expect("system randomness");
    let suffix: String = random.iter().map(|b| format!("{b:02x}")).collect();
    std::env::temp_dir().join(format!("usage-dashboard-test-{suffix}.db"))
}

impl Default for TempDb {
    fn default() -> Self {
        TempDb::new()
    }
}

impl Drop for TempDb {
    fn drop(&mut self) {
        for extension in ["db", "db-wal", "db-shm"] {
            let _ = std::fs::remove_file(self.path.with_extension(extension));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn creates_schema_and_reopens() {
        let temp = TempDb::new();
        let tables: Vec<String> = temp
            .db
            .read(|conn| {
                let mut stmt = conn
                    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")?;
                let rows = stmt.query_map([], |row| row.get(0))?;
                rows.collect()
            })
            .unwrap();
        assert_eq!(tables, ["events", "sessions", "tokens", "users"]);

        // Opening the same file again must not try to create the schema twice.
        // 같은 파일을 다시 열 때 스키마를 두 번 만들려고 해서는 안 됩니다.
        let again = Db::open(&temp.path).unwrap();
        let version: i64 = again
            .read(|c| c.query_row("PRAGMA user_version", [], |r| r.get(0)))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
    }

    #[test]
    fn readers_see_committed_writes() {
        let temp = TempDb::new();
        temp.db
            .write(|conn| conn.execute("INSERT INTO users (name, created_at) VALUES ('a', 1)", []))
            .unwrap();
        let count: i64 = temp
            .db
            .read(|c| c.query_row("SELECT COUNT(*) FROM users", [], |r| r.get(0)))
            .unwrap();
        assert_eq!(count, 1);
        // Read-only connections must refuse writes.
        // 읽기 전용 연결은 쓰기를 거부해야 합니다.
        assert!(
            temp.db
                .read(|c| c.execute("DELETE FROM users", []))
                .is_err()
        );
    }

    fn index_names(db: &Db) -> Vec<String> {
        db.read(|conn| {
            let mut stmt = conn.prepare(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'events'
                 AND name NOT LIKE 'sqlite_%' ORDER BY name",
            )?;
            let rows = stmt.query_map([], |row| row.get(0))?;
            rows.collect()
        })
        .unwrap()
    }

    #[test]
    fn a_new_database_has_the_report_index() {
        let temp = TempDb::new();
        assert_eq!(index_names(&temp.db), ["events_report"]);
    }

    #[test]
    fn upgrades_a_version_1_database_in_place() {
        // Build a file exactly as version 1 of this program left it, with one stored event.
        // 이 프로그램의 버전 1이 남긴 그대로의 파일을 만들고 이벤트 하나를 넣어 둡니다.
        let path = unique_temp_path();
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(SCHEMA_V1).unwrap();
            conn.pragma_update(None, "user_version", 1).unwrap();
            conn.execute_batch(
                "INSERT INTO users (id, name, created_at) VALUES (1, 'alice', 1);
                 INSERT INTO events VALUES (1, 'msg_1', 0, 1795328728, 'claude-fable-5-2', 'message',
                     'claude-code', 's1', 'demo', '', 2, 968, 0, 16870, 26681, 267, 0, 0, 1795328730);",
            )
            .unwrap();
        }
        let temp = TempDb {
            db: std::sync::Arc::new(Db::open(&path).unwrap()),
            path,
        };

        let version: i64 = temp
            .db
            .read(|c| c.query_row("PRAGMA user_version", [], |r| r.get(0)))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        assert_eq!(index_names(&temp.db), ["events_report"]);
        let output: i64 = temp
            .db
            .read(|c| c.query_row("SELECT output_tokens FROM events", [], |r| r.get(0)))
            .unwrap();
        assert_eq!(output, 968);

        // Opening it again changes nothing.
        // 다시 열어도 바뀌는 것이 없습니다.
        drop(Db::open(&temp.path).unwrap());
        assert_eq!(index_names(&temp.db), ["events_report"]);
    }

    #[test]
    fn refuses_a_database_from_a_newer_version() {
        let temp = TempDb::new();
        temp.db
            .write(|conn| conn.pragma_update(None, "user_version", 99))
            .unwrap();
        match Db::open(&temp.path) {
            Err(OpenError::NewerSchema {
                found: 99,
                supported: SCHEMA_VERSION,
            }) => {}
            Err(other) => panic!("unexpected error: {other}"),
            Ok(_) => panic!("a newer schema must not open"),
        }
    }
}
