//! Users, collector tokens and browser sessions.
//! 사용자, 수집용 토큰, 브라우저 세션을 다룹니다.

use crate::timeutil::now_unix;
use rusqlite::{Connection, OptionalExtension, params};
use sha2::{Digest, Sha256};

pub const TOKEN_PREFIX: &str = "udt_";
pub const SESSION_COOKIE: &str = "ud_session";
pub const SESSION_TTL_SECS: i64 = 30 * 24 * 60 * 60;
pub const MAX_USER_NAME_LEN: usize = 64;

/// The user a request is authenticated as.
/// 요청이 인증된 사용자입니다.
#[derive(Debug, Clone, PartialEq)]
pub struct AuthedUser {
    pub user_id: i64,
    pub user_name: String,
    pub token_id: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct UserInfo {
    pub id: i64,
    pub name: String,
    pub created_at: i64,
    pub active_tokens: i64,
    pub last_event_at: Option<i64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TokenInfo {
    pub id: i64,
    pub user_name: String,
    pub label: String,
    pub hint: String,
    pub created_at: i64,
    pub last_used_at: Option<i64>,
    pub revoked_at: Option<i64>,
}

fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    getrandom::fill(&mut buf).expect("system randomness is required to create tokens");
    hex(&buf)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// SHA-256 as lowercase hex. Tokens are 160 random bits, so a plain fast hash is enough.
/// SHA-256을 소문자 16진수로 돌려줍니다. 토큰은 160비트 난수이므로 빠른 해시 하나로 충분합니다.
pub fn sha256_hex(text: &str) -> String {
    hex(&Sha256::digest(text.as_bytes()))
}

pub fn generate_token() -> String {
    format!("{TOKEN_PREFIX}{}", random_hex(20))
}

/// Trim and check a user name.
/// 사용자 이름을 다듬고 검사합니다.
pub fn clean_user_name(name: &str) -> Result<String, String> {
    let name = name.trim();
    if name.is_empty() {
        return Err("user name must not be empty".into());
    }
    if name.chars().count() > MAX_USER_NAME_LEN {
        return Err(format!(
            "user name is longer than {MAX_USER_NAME_LEN} characters"
        ));
    }
    if name.chars().any(char::is_control) {
        return Err("user name contains control characters".into());
    }
    Ok(name.to_string())
}

pub fn find_user_id(conn: &Connection, name: &str) -> rusqlite::Result<Option<i64>> {
    conn.query_row("SELECT id FROM users WHERE name = ?1", [name], |row| {
        row.get(0)
    })
    .optional()
}

/// Create a user. Returns None when the name is already taken.
/// 사용자를 만듭니다. 이미 있는 이름이면 None을 돌려줍니다.
pub fn create_user(conn: &Connection, name: &str) -> rusqlite::Result<Option<i64>> {
    if find_user_id(conn, name)?.is_some() {
        return Ok(None);
    }
    conn.execute(
        "INSERT INTO users (name, created_at) VALUES (?1, ?2)",
        params![name, now_unix()],
    )?;
    Ok(Some(conn.last_insert_rowid()))
}

/// Create a token for a user and return (token id, token). The token cannot be read back later.
/// 사용자의 토큰을 만들고 (토큰 id, 토큰)을 돌려줍니다. 토큰은 나중에 다시 조회할 수 없습니다.
pub fn create_token(
    conn: &Connection,
    user_id: i64,
    label: &str,
) -> rusqlite::Result<(i64, String)> {
    let token = generate_token();
    let hint = token[token.len() - 4..].to_string();
    conn.execute(
        "INSERT INTO tokens (user_id, label, token_hash, hint, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
        params![user_id, label, sha256_hex(&token), hint, now_unix()],
    )?;
    Ok((conn.last_insert_rowid(), token))
}

/// Revoke a token. Returns false when the id does not exist or is already revoked.
/// 토큰을 폐기합니다. 없는 id이거나 이미 폐기된 토큰이면 false를 돌려줍니다.
pub fn revoke_token(conn: &Connection, token_id: i64) -> rusqlite::Result<bool> {
    let changed = conn.execute(
        "UPDATE tokens SET revoked_at = ?1 WHERE id = ?2 AND revoked_at IS NULL",
        params![now_unix(), token_id],
    )?;
    conn.execute("DELETE FROM sessions WHERE token_id = ?1", [token_id])?;
    Ok(changed > 0)
}

/// Look up the user that owns a live token.
/// 유효한 토큰의 주인인 사용자를 찾습니다.
pub fn lookup_token(conn: &Connection, token: &str) -> rusqlite::Result<Option<AuthedUser>> {
    if !token.starts_with(TOKEN_PREFIX) {
        return Ok(None);
    }
    conn.query_row(
        "SELECT u.id, u.name, t.id FROM tokens t JOIN users u ON u.id = t.user_id
         WHERE t.token_hash = ?1 AND t.revoked_at IS NULL",
        [sha256_hex(token)],
        |row| {
            Ok(AuthedUser {
                user_id: row.get(0)?,
                user_name: row.get(1)?,
                token_id: row.get(2)?,
            })
        },
    )
    .optional()
}

pub fn touch_token(conn: &Connection, token_id: i64) -> rusqlite::Result<()> {
    conn.execute(
        "UPDATE tokens SET last_used_at = ?1 WHERE id = ?2",
        params![now_unix(), token_id],
    )?;
    Ok(())
}

/// Start a browser session for a token and return the session id to put in the cookie.
/// 토큰으로 브라우저 세션을 시작하고, 쿠키에 넣을 세션 id를 돌려줍니다.
pub fn create_session(conn: &Connection, token_id: i64) -> rusqlite::Result<String> {
    let now = now_unix();
    let session_id = random_hex(32);
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?1", [now])?;
    conn.execute(
        "INSERT INTO sessions (id_hash, token_id, created_at, expires_at) VALUES (?1, ?2, ?3, ?4)",
        params![
            sha256_hex(&session_id),
            token_id,
            now,
            now + SESSION_TTL_SECS
        ],
    )?;
    Ok(session_id)
}

pub fn lookup_session(conn: &Connection, session_id: &str) -> rusqlite::Result<Option<AuthedUser>> {
    conn.query_row(
        "SELECT u.id, u.name, t.id FROM sessions s
         JOIN tokens t ON t.id = s.token_id
         JOIN users u ON u.id = t.user_id
         WHERE s.id_hash = ?1 AND s.expires_at > ?2 AND t.revoked_at IS NULL",
        params![sha256_hex(session_id), now_unix()],
        |row| {
            Ok(AuthedUser {
                user_id: row.get(0)?,
                user_name: row.get(1)?,
                token_id: row.get(2)?,
            })
        },
    )
    .optional()
}

pub fn delete_session(conn: &Connection, session_id: &str) -> rusqlite::Result<()> {
    conn.execute(
        "DELETE FROM sessions WHERE id_hash = ?1",
        [sha256_hex(session_id)],
    )?;
    Ok(())
}

pub fn list_users(conn: &Connection) -> rusqlite::Result<Vec<UserInfo>> {
    let mut stmt = conn.prepare(
        "SELECT u.id, u.name, u.created_at,
                (SELECT COUNT(*) FROM tokens t WHERE t.user_id = u.id AND t.revoked_at IS NULL),
                (SELECT MAX(e.ts) FROM events e WHERE e.user_id = u.id)
         FROM users u ORDER BY u.name",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok(UserInfo {
            id: row.get(0)?,
            name: row.get(1)?,
            created_at: row.get(2)?,
            active_tokens: row.get(3)?,
            last_event_at: row.get(4)?,
        })
    })?;
    rows.collect()
}

pub fn list_tokens(conn: &Connection) -> rusqlite::Result<Vec<TokenInfo>> {
    let mut stmt = conn.prepare(
        "SELECT t.id, u.name, t.label, t.hint, t.created_at, t.last_used_at, t.revoked_at
         FROM tokens t JOIN users u ON u.id = t.user_id ORDER BY u.name, t.id",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok(TokenInfo {
            id: row.get(0)?,
            user_name: row.get(1)?,
            label: row.get(2)?,
            hint: row.get(3)?,
            created_at: row.get(4)?,
            last_used_at: row.get(5)?,
            revoked_at: row.get(6)?,
        })
    })?;
    rows.collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::TempDb;

    #[test]
    fn tokens_are_random_and_prefixed() {
        let a = generate_token();
        let b = generate_token();
        assert_ne!(a, b);
        assert!(a.starts_with(TOKEN_PREFIX));
        assert_eq!(a.len(), TOKEN_PREFIX.len() + 40);
    }

    #[test]
    fn sha256_matches_a_known_vector() {
        assert_eq!(
            sha256_hex("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn user_names_are_cleaned() {
        assert_eq!(clean_user_name("  홍길동 ").unwrap(), "홍길동");
        assert!(clean_user_name("   ").is_err());
        assert!(clean_user_name(&"a".repeat(65)).is_err());
        assert!(clean_user_name("a\tb").is_err());
    }

    #[test]
    fn token_lifecycle() {
        let temp = TempDb::new();
        temp.db
            .write(|conn| {
                let user_id = create_user(conn, "alice")?.expect("new user");
                assert_eq!(create_user(conn, "alice")?, None);
                let (token_id, token) = create_token(conn, user_id, "laptop")?;

                // The stored hash is not the token.
                // 저장된 해시는 토큰과 다릅니다.
                let stored: String =
                    conn.query_row("SELECT token_hash FROM tokens", [], |r| r.get(0))?;
                assert_ne!(stored, token);
                assert_eq!(stored, sha256_hex(&token));

                let authed = lookup_token(conn, &token)?.expect("token should resolve");
                assert_eq!(
                    authed,
                    AuthedUser {
                        user_id,
                        user_name: "alice".into(),
                        token_id
                    }
                );
                assert_eq!(lookup_token(conn, "udt_wrong")?, None);
                assert_eq!(lookup_token(conn, "")?, None);

                let session = create_session(conn, token_id)?;
                assert_eq!(lookup_session(conn, &session)?, Some(authed.clone()));
                assert_eq!(lookup_session(conn, "nope")?, None);

                // Revoking the token also ends its sessions.
                // 토큰을 폐기하면 그 토큰의 세션도 끝납니다.
                assert!(revoke_token(conn, token_id)?);
                assert!(!revoke_token(conn, token_id)?);
                assert_eq!(lookup_token(conn, &token)?, None);
                assert_eq!(lookup_session(conn, &session)?, None);

                let users = list_users(conn)?;
                assert_eq!(users.len(), 1);
                assert_eq!(users[0].active_tokens, 0);
                let tokens = list_tokens(conn)?;
                assert_eq!(tokens.len(), 1);
                assert!(tokens[0].revoked_at.is_some());
                assert_eq!(tokens[0].hint, token[token.len() - 4..]);
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn expired_sessions_do_not_authenticate() {
        let temp = TempDb::new();
        temp.db
            .write(|conn| {
                let user_id = create_user(conn, "bob")?.unwrap();
                let (token_id, _) = create_token(conn, user_id, "")?;
                let session = create_session(conn, token_id)?;
                conn.execute("UPDATE sessions SET expires_at = ?1", [now_unix() - 1])?;
                assert_eq!(lookup_session(conn, &session)?, None);
                delete_session(conn, &session)?;
                Ok(())
            })
            .unwrap();
    }
}
