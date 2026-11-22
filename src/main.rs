//! Command line entry point: run the server, manage users and tokens.
//! 명령줄 진입점입니다. 서버를 실행하고 사용자와 토큰을 관리합니다.

use clap::{Args, Parser, Subcommand};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use usage_dashboard::auth;
use usage_dashboard::db::Db;
use usage_dashboard::pricing::{EMBEDDED_PRICES, Pricing};
use usage_dashboard::server::{self, AppState, Config};
use usage_dashboard::timeutil;

#[derive(Parser)]
#[command(
    name = "usage-dashboard",
    version,
    about = "Shared dashboard server for Claude token usage"
)]
struct Cli {
    /// SQLite database file (created when missing)
    #[arg(
        long,
        global = true,
        env = "USAGE_DASHBOARD_DB",
        default_value = "usage.db"
    )]
    db: PathBuf,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run the dashboard server
    Serve(ServeArgs),
    /// Manage the people who report and view usage
    #[command(subcommand)]
    User(UserCommand),
    /// Manage the tokens collectors and browsers sign in with
    #[command(subcommand)]
    Token(TokenCommand),
    /// Print the built-in price table as JSON (a starting point for --prices)
    Prices,
}

#[derive(Args)]
struct ServeArgs {
    /// Address to listen on; use 0.0.0.0:8787 to accept other machines
    #[arg(long, env = "USAGE_DASHBOARD_LISTEN", default_value = "127.0.0.1:8787")]
    listen: SocketAddr,

    /// Price table JSON that replaces the built-in one
    #[arg(long, env = "USAGE_DASHBOARD_PRICES")]
    prices: Option<PathBuf>,

    /// UTC offset for day boundaries when a request does not send one
    #[arg(long, default_value = "+09:00", allow_hyphen_values = true)]
    utc_offset: String,

    /// Mark the session cookie Secure (use when served over HTTPS)
    #[arg(long)]
    cookie_secure: bool,

    /// Most events accepted in one request
    #[arg(long, default_value_t = 5000)]
    max_events: usize,

    /// Largest accepted request body, in megabytes
    #[arg(long, default_value_t = 16)]
    max_body_mb: usize,
}

#[derive(Subcommand)]
enum UserCommand {
    /// Create a user and print their first token
    Add {
        name: String,
        /// Note for the first token, e.g. the machine it is for
        #[arg(long, default_value = "")]
        label: String,
    },
    /// List users
    List,
}

#[derive(Subcommand)]
enum TokenCommand {
    /// Create another token for an existing user
    Add {
        user: String,
        /// Note for this token, e.g. the machine it is for
        #[arg(long, default_value = "")]
        label: String,
    },
    /// List tokens (only the last four characters are known)
    List,
    /// Revoke a token by id; its browser sessions end too
    Revoke { id: i64 },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run(cli: Cli) -> Result<(), String> {
    match cli.command {
        Command::Prices => {
            print!("{EMBEDDED_PRICES}");
            Ok(())
        }
        Command::Serve(args) => serve(&cli.db, args),
        Command::User(command) => user_command(&open_db_for_admin(&cli.db)?, command),
        Command::Token(command) => token_command(&open_db_for_admin(&cli.db)?, command),
    }
}

fn open_db(path: &Path) -> Result<Db, String> {
    Db::open(path).map_err(|e| format!("cannot open database '{}': {e}", path.display()))
}

/// Open the database for a user or token command and say which file it is. Opening creates a
/// missing file, so without this line a mistyped --db would quietly manage an empty database
/// instead of the one the server uses.
/// 사용자·토큰 명령을 위해 데이터베이스를 열고 어떤 파일인지 알려 줍니다. 파일이 없으면 새로
/// 만들기 때문에, 이 안내가 없으면 --db를 잘못 적었을 때 서버가 쓰는 데이터베이스가 아니라
/// 빈 데이터베이스를 조용히 다루게 됩니다.
fn open_db_for_admin(path: &Path) -> Result<Db, String> {
    let existed = path.exists();
    let db = open_db(path)?;
    eprintln!(
        "database: {}{}",
        path.display(),
        if existed { "" } else { " (newly created)" }
    );
    Ok(db)
}

fn storage(e: rusqlite::Error) -> String {
    format!("database error: {e}")
}

fn serve(db_path: &Path, args: ServeArgs) -> Result<(), String> {
    let pricing = match &args.prices {
        Some(path) => {
            let text = std::fs::read_to_string(path)
                .map_err(|e| format!("cannot read price table '{}': {e}", path.display()))?;
            Pricing::from_json(&text)?
        }
        None => Pricing::embedded(),
    };
    let config = Config {
        default_offset_min: timeutil::parse_utc_offset(&args.utc_offset)?,
        cookie_secure: args.cookie_secure,
        max_events_per_request: args.max_events,
        max_body_bytes: args.max_body_mb.saturating_mul(1024 * 1024),
        access_log: true,
    };
    let db = open_db(db_path)?;
    let users = db.read(auth::list_users).map_err(storage)?;

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .map_err(|e| format!("cannot start async runtime: {e}"))?;
    runtime.block_on(async move {
        let listener = tokio::net::TcpListener::bind(args.listen)
            .await
            .map_err(|e| format!("cannot listen on {}: {e}", args.listen))?;
        let address = listener.local_addr().map_err(|e| e.to_string())?;
        println!(
            "usage-dashboard {} listening on http://{address}",
            env!("CARGO_PKG_VERSION")
        );
        println!("database: {}", db_path.display());
        println!(
            "prices as of {} ({} model ids), default UTC offset {}",
            pricing.table().as_of,
            pricing
                .table()
                .models
                .iter()
                .map(|m| m.models.len())
                .sum::<usize>(),
            timeutil::format_utc_offset(config.default_offset_min)
        );
        if users.is_empty() {
            println!("no users yet: create one with `usage-dashboard user add <name>`");
        }
        let state = AppState {
            db: Arc::new(db),
            pricing: Arc::new(pricing),
            config: Arc::new(config),
        };
        server::serve(listener, state)
            .await
            .map_err(|e| format!("server stopped: {e}"))
    })
}

/// Print a new token. The token goes to stdout alone so scripts can capture it; the explanation
/// goes to stderr.
/// 새 토큰을 출력합니다. 스크립트가 받아 쓸 수 있도록 토큰만 표준 출력으로 보내고, 설명은
/// 표준 오류로 보냅니다.
fn print_new_token(user: &str, token_id: i64, token: &str) {
    eprintln!("Token #{token_id} for '{user}'. It is shown only once; store it now:");
    println!("{token}");
}

fn user_command(db: &Db, command: UserCommand) -> Result<(), String> {
    match command {
        UserCommand::Add { name, label } => {
            let name = auth::clean_user_name(&name)?;
            let created = db
                .write(|conn| {
                    let tx = conn.transaction()?;
                    let Some(user_id) = auth::create_user(&tx, &name)? else {
                        return Ok(None);
                    };
                    let token = auth::create_token(&tx, user_id, label.trim())?;
                    tx.commit()?;
                    Ok(Some(token))
                })
                .map_err(storage)?;
            match created {
                Some((token_id, token)) => {
                    eprintln!("Created user '{name}'.");
                    print_new_token(&name, token_id, &token);
                    Ok(())
                }
                None => Err(format!(
                    "user '{name}' already exists (use `token add {name}` for another token)"
                )),
            }
        }
        UserCommand::List => {
            let users = db.read(auth::list_users).map_err(storage)?;
            if users.is_empty() {
                eprintln!("No users. Create one with `usage-dashboard user add <name>`.");
                return Ok(());
            }
            let mut rows = vec![vec![
                "NAME".to_string(),
                "ACTIVE TOKENS".to_string(),
                "LAST EVENT (UTC)".to_string(),
                "CREATED (UTC)".to_string(),
            ]];
            for user in users {
                rows.push(vec![
                    user.name,
                    user.active_tokens.to_string(),
                    format_time(user.last_event_at),
                    format_time(Some(user.created_at)),
                ]);
            }
            print_table(&rows);
            Ok(())
        }
    }
}

fn token_command(db: &Db, command: TokenCommand) -> Result<(), String> {
    match command {
        TokenCommand::Add { user, label } => {
            let name = user.trim().to_string();
            let created = db
                .write(|conn| match auth::find_user_id(conn, &name)? {
                    Some(user_id) => Ok(Some(auth::create_token(conn, user_id, label.trim())?)),
                    None => Ok(None),
                })
                .map_err(storage)?;
            match created {
                Some((token_id, token)) => {
                    print_new_token(&name, token_id, &token);
                    Ok(())
                }
                None => Err(format!(
                    "no user named '{name}' (see `usage-dashboard user list`)"
                )),
            }
        }
        TokenCommand::List => {
            let tokens = db.read(auth::list_tokens).map_err(storage)?;
            if tokens.is_empty() {
                eprintln!("No tokens.");
                return Ok(());
            }
            let mut rows = vec![vec![
                "ID".to_string(),
                "USER".to_string(),
                "LABEL".to_string(),
                "ENDS WITH".to_string(),
                "STATUS".to_string(),
                "LAST USED (UTC)".to_string(),
                "CREATED (UTC)".to_string(),
            ]];
            for token in tokens {
                rows.push(vec![
                    token.id.to_string(),
                    token.user_name,
                    token.label,
                    token.hint,
                    if token.revoked_at.is_some() {
                        "revoked".into()
                    } else {
                        "active".into()
                    },
                    format_time(token.last_used_at),
                    format_time(Some(token.created_at)),
                ]);
            }
            print_table(&rows);
            Ok(())
        }
        TokenCommand::Revoke { id } => {
            if db
                .write(|conn| auth::revoke_token(conn, id))
                .map_err(storage)?
            {
                eprintln!("Revoked token #{id}.");
                Ok(())
            } else {
                Err(format!(
                    "no active token with id {id} (see `usage-dashboard token list`)"
                ))
            }
        }
    }
}

fn format_time(unix: Option<i64>) -> String {
    match unix {
        Some(ts) => timeutil::format_rfc3339_utc(ts),
        None => "-".to_string(),
    }
}

/// Terminal columns a string occupies. Hangul, CJK and other full-width characters take two
/// columns, so counting characters would misalign rows that hold Korean names.
/// 문자열이 터미널에서 차지하는 칸 수입니다. 한글, 한자 등 전각 문자는 두 칸을 차지하므로
/// 글자 수로 세면 한글 이름이 있는 줄의 정렬이 어긋납니다.
fn display_width(text: &str) -> usize {
    text.chars()
        .map(|c| match u32::from(c) {
            0x1100..=0x115F
            | 0x2E80..=0xA4CF
            | 0xAC00..=0xD7A3
            | 0xF900..=0xFAFF
            | 0xFE30..=0xFE4F
            | 0xFF00..=0xFF60
            | 0xFFE0..=0xFFE6
            | 0x1F300..=0x1FAFF
            | 0x20000..=0x3FFFD => 2,
            _ => 1,
        })
        .sum()
}

/// Print rows as left-aligned columns.
/// 행들을 왼쪽 정렬 열로 출력합니다.
fn print_table(rows: &[Vec<String>]) {
    let columns = rows.iter().map(Vec::len).max().unwrap_or(0);
    let widths: Vec<usize> = (0..columns)
        .map(|i| {
            rows.iter()
                .filter_map(|r| r.get(i))
                .map(|c| display_width(c))
                .max()
                .unwrap_or(0)
        })
        .collect();
    for row in rows {
        let line: Vec<String> = row
            .iter()
            .enumerate()
            .map(|(i, cell)| format!("{cell}{}", " ".repeat(widths[i] - display_width(cell))))
            .collect();
        println!("{}", line.join("  ").trim_end());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_width_characters_take_two_columns() {
        assert_eq!(display_width("alice"), 5);
        assert_eq!(display_width("홍길동"), 6);
        assert_eq!(display_width("맥북 pro"), 8);
        assert_eq!(display_width(""), 0);
    }
}
