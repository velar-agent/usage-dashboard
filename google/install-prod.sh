#!/usr/bin/env bash
# prod 전용 google 인스턴스(포트 8788)를 설치해요. idempotent하게 여러 번 실행해도 안전해요.
# collector 계정에서 실행하세요. 유닛 설치에만 sudo를 써요(NOPASSWD).
set -euo pipefail

if [[ $(id -un) != collector ]]; then
    echo 'collector 계정에서 실행하세요. 유닛 설치에만 sudo를 써요.' >&2
    exit 1
fi

DEV_REPO=/workspace/usage-dashboard
PROD_WORKTREE=/workspace/usage-dashboard-prod
DATA_DIR=/workspace/usage-dashboard-prod-data
PROD_ENV=/home/collector/.config/usage-dashboard-google-prod/env
PROD_DB="$DATA_DIR/usage.sqlite3"
DEV_ENV=/home/collector/.config/usage-dashboard-google-dev/env
KEY_FILE=/workspace/token-usage/google-ingest-key-prod.txt
UNIT_NAME=usage-dashboard-google-prod.service

umask 077

# 데이터 디렉터리를 만들어요 (worktree 밖에 DB를 두면 승격 때 worktree를 옮겨도 DB가 남습니다)
install -d -m 0700 "$DATA_DIR"
install -d -m 0700 "$DATA_DIR/backups"

# prod worktree가 없으면 dev 저장소에서 HEAD를 detached로 만들어요.
# 있으면 건드리지 않아요(자정 승격이 이 worktree를 이동시켜요).
if [[ ! -e "$PROD_WORKTREE/.git" ]]; then
    echo "prod worktree를 만들어요: $PROD_WORKTREE"
    if ! git -C "$DEV_REPO" worktree add --detach "$PROD_WORKTREE" HEAD; then
        echo "worktree가 이미 존재하거나 만들 수 없어요. 현재 상태를 확인하세요." >&2
        if [[ ! -e "$PROD_WORKTREE/.git" ]]; then
            exit 1
        fi
    fi
else
    echo "prod worktree가 이미 있어요: $PROD_WORKTREE"
fi

# prod env를 만들어요. 없으면 gen_env.py로 DB_PATH와 PORT를 함께 생성하고, 있으면 값을 맞춰요.
mkdir -p "$(dirname -- "$PROD_ENV")"
if [[ ! -f "$PROD_ENV" ]]; then
    echo "prod env를 만들어요: $PROD_ENV"
    /usr/bin/python3 "$DEV_REPO/google/gen_env.py" "$PROD_ENV" --db-path "$PROD_DB" --port 8788
fi

# 기존 env에도 DB_PATH와 PORT를 prod 값으로 맞춰요(키·비밀번호는 그대로).
ensure_env_kv() {
  local file="$1" key="$2" value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}
ensure_env_kv "$PROD_ENV" DB_PATH "$PROD_DB"
ensure_env_kv "$PROD_ENV" PORT "8788"
chmod 0600 "$PROD_ENV"
chmod 0600 "$(dirname -- "$PROD_ENV")/raw-password.txt" 2>/dev/null || true

# prod DB가 아직 없으면 dev DB를 backup API로 복사해요. 서비스 시작 전에 끝내요.
if [[ ! -f "$PROD_DB" ]]; then
    DEV_DB_PATH=/workspace/usage-dashboard/google/data/usage.sqlite3
    if [[ -f "$DEV_ENV" ]] && grep -q '^DB_PATH=' "$DEV_ENV"; then
        DEV_DB_PATH=$(grep '^DB_PATH=' "$DEV_ENV" | cut -d= -f2-)
    fi
    if [[ -f "$DEV_DB_PATH" ]]; then
        echo "dev DB를 prod DB로 씨드합니다: $DEV_DB_PATH -> $PROD_DB"
        /usr/bin/python3 - "$DEV_DB_PATH" "$PROD_DB" <<'PY'
import sqlite3, pathlib, sys
src, dst = sys.argv[1], sys.argv[2]
pathlib.Path(dst).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
dst_con = sqlite3.connect(dst, timeout=10)
try:
    with dst_con:
        src_con.backup(dst_con)
finally:
    src_con.close()
    dst_con.close()
PY
        chmod 0600 "$PROD_DB" 2>/dev/null || true
    else
        echo "dev DB가 없어서 씨드하지 않아요 (처음으로 시작하면 새로 만들어요)."
    fi
else
    echo "prod DB가 이미 있어요: $PROD_DB (씨드하지 않아요)"
fi

# 수집기(prod용) 키 파일을 만들어요(없을 때만). 키 값은 로그에 찍지 않아요.
if [[ ! -f "$KEY_FILE" ]]; then
    echo "prod 수집 키 파일을 만들어요: $KEY_FILE"
    install -d -m 0700 "$(dirname -- "$KEY_FILE")"
    PROD_KEY=$(grep '^INGEST_KEY=' "$PROD_ENV" | cut -d= -f2-)
    if [[ -z "$PROD_KEY" ]]; then
        echo "prod env에 INGEST_KEY가 없어요." >&2
        exit 1
    fi
    tmp=$(mktemp "$KEY_FILE.XXXXXX")
    printf '%s\n' "$PROD_KEY" > "$tmp"
    chmod 0600 "$tmp"
    mv "$tmp" "$KEY_FILE"
else
    echo "prod 수집 키 파일이 이미 있어요: $KEY_FILE"
fi

# 유닛은 dev 저장소의 google/에서 설치해요(worktree 복사본이 아니라).
echo "유닛을 설치해요: $DEV_REPO/google/$UNIT_NAME -> /etc/systemd/system/$UNIT_NAME"
sudo install -m 0644 "$DEV_REPO/google/$UNIT_NAME" "/etc/systemd/system/$UNIT_NAME"
sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT_NAME"
echo "prod 서비스(8788)를 시작했어요. 확인: curl -fsS http://127.0.0.1:8788/health"
