#!/usr/bin/env bash
# 매일 00:00(KST) cron 이 실행하는 자정 승격 러너: dev(main) 을 점검하고 prod worktree(8788)로 올려요.
# Cloudflare 로직은 없어요. prod는 worktree를 pinned 커밋으로 옮기고 systemd만 재시작해요.
# DB는 worktree 밖에 두며( /workspace/usage-dashboard-prod-data/usage.sqlite3 ), 서버가 시작할 때
# worker/schema.sql 을 additive 로 적용하므로 롤백해도 DB를 되돌리지 않아요. 백업은 수동 복구용이에요.
# 비밀 값은 없어요. 유닛 파일은 건드리지 않아요(설치는 install-prod.sh가 dev 저장소에서 해요).
#
# 사용법: promote-prod.sh [--dry-run] [--force]
#   --dry-run  prod worktree/재시작/백업을 빼고 점검·변경 보고만 해요(git pull 대신 git fetch 만)
#   --force    prod HEAD가 dev HEAD와 같아도 다시 올리고 재시작해요
# 종료 코드: 0 성공/변경 없음(건너뜀·dry-run 보고 포함), 1 점검·git 실패(브랜치·추적 트리·pull·HEAD),
#            2 사전 검사(precheck) 실패, 5 배포 후 점검 실패 → 이전 커밋으로 롤백했고 점검 통과,
#            6 롤백 실패 또는 롤백 후에도 점검 실패(수동 확인 필요), 9 이미 실행 중, 10 사용법
set -u -o pipefail
export TZ=Asia/Seoul
export LC_ALL=C.UTF-8 PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEV_REPO=${USAGE_REPO:-$(cd "$HERE/.." && pwd)}
PROD_WORKTREE=${PROD_WORKTREE:-/workspace/usage-dashboard-prod}
DATA_DIR=${PROD_DATA_DIR:-/workspace/usage-dashboard-prod-data}
PROD_DB="${PROD_DB:-$DATA_DIR/usage.sqlite3}"
BACKUP_DIR="${PROD_BACKUP_DIR:-$DATA_DIR/backups}"
PROD_PORT=8788
PROD_UNIT=usage-dashboard-google-prod.service
STATE=${USAGE_DEPLOY_STATE:-$HOME/.local/state/usage-deploy}

DRY_RUN=false
FORCE=false
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=true ;;
    --force) FORCE=true ;;
    *) echo "unknown option: $a" >&2; exit 10 ;;
  esac
done

umask 077
mkdir -p "$STATE"; chmod 700 "$STATE"
LOG="$STATE/$(date +%F).log"
log() { printf '%s %s\n' "$(date '+%F %T %Z')" "$*" >> "$LOG"; }

exec 9>"$STATE/lock"
if ! flock -n 9; then log "SKIP 이미 다른 실행이 진행 중이에요"; exit 9; fi

find "$STATE" -maxdepth 1 -name '20??-??-??.log' -mtime +60 -delete 2>/dev/null || true

exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T %Z') promote-prod start dry_run=$DRY_RUN force=$FORCE ==="

write_status() {
  printf '{"time":"%s","exit":%s,"dry_run":%s,"force":%s,"log":"%s"}\n' \
    "$(date '+%FT%T%z')" "$1" "$DRY_RUN" "$FORCE" "$LOG" > "$STATE/last-run.json" 2>/dev/null || true
}
finish() { echo "=== $(date '+%F %T %Z') done exit=$1 ==="; write_status "$1"; exit "$1"; }

health_check() {
  local attempts=20 delay=1 i
  for ((i=1; i<=attempts; i++)); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${PROD_PORT}/health" >/dev/null 2>&1; then
      if [[ "$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:${PROD_PORT}/" 2>/dev/null || echo "000")" == "200" ]]; then
        return 0
      fi
    fi
    [[ $i -lt $attempts ]] && sleep "$delay"
  done
  return 1
}

cd "$DEV_REPO" || finish 1
branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$branch" != "main" ]]; then echo "FAIL 현재 브랜치가 main 이 아니에요 ($branch). prod로 올리지 않아요."; finish 1; fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "FAIL 커밋되지 않은 변경이 있어요. prod로 올리지 않아요:"; git status --short --untracked-files=no; finish 1
fi
if [[ "$DRY_RUN" == "true" ]]; then
  echo "dry-run: git fetch 만 해요 (pull은 하지 않아요)"
  if ! git fetch --quiet origin; then echo "FAIL git fetch 실패. 배포하지 않아요."; finish 1; fi
  if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
    echo "HEAD $(git rev-parse --short HEAD) != origin/main $(git rev-parse --short origin/main) (dry-run 보고만)"
  else
    echo "HEAD $(git rev-parse --short HEAD) (= origin/main)"
  fi
else
  if ! git pull --ff-only; then echo "FAIL git pull --ff-only 실패. 배포하지 않아요."; finish 1; fi
  if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
    echo "FAIL 로컬 HEAD 가 origin/main 과 달라요(푸시 안 된 커밋?). 배포하지 않아요."; finish 1
  fi
  echo "HEAD $(git rev-parse --short HEAD) (= origin/main)"
fi

echo "=== 사전 검사(precheck) ==="
if ! (cd "$DEV_REPO/google" && python3 -m unittest 2>&1); then
  echo "FAIL precheck(unittest) 실패. prod로 올리지 않아요."; finish 2
fi
if ! (cd "$DEV_REPO/google" && python3 -m py_compile *.py 2>&1); then
  echo "FAIL precheck(py_compile) 실패. prod로 올리지 않아요."; finish 2
fi
echo "precheck 통과"

if [[ ! -d "$PROD_WORKTREE/.git" ]] && [[ ! -e "$PROD_WORKTREE/.git" ]]; then
  echo "FAIL prod worktree가 없어요: $PROD_WORKTREE (install-prod.sh를 먼저 실행하세요)"; finish 1
fi
if [[ "$DRY_RUN" == "true" ]]; then
  TARGET=$(git rev-parse origin/main)
else
  TARGET=$(git rev-parse HEAD)
fi
DEV_HEAD="$TARGET"
PROD_HEAD=$(git -C "$PROD_WORKTREE" rev-parse HEAD 2>/dev/null || echo "unknown")
echo "dev HEAD $DEV_HEAD"
echo "prod HEAD $PROD_HEAD"

if [[ "$TARGET" == "$PROD_HEAD" && "$FORCE" == "false" ]]; then
  echo "no change: prod가 이미 dev HEAD와 같아요. 건너뛰어요."
  finish 0
fi

if [[ "$DRY_RUN" == "true" ]]; then
  echo "dry-run: prod ${PROD_HEAD:0:7} -> ${TARGET:0:7} 로 올리는 변경이 있어요"
  echo "dry-run: 백업 예정 $BACKUP_DIR/usage-<timestamp>.sqlite3 (최근 7개 유지)"
  echo "dry-run: prod worktree 이동(git checkout --detach $TARGET)하고 $PROD_UNIT 재시작할 예정이에요"
  echo "dry-run: html.js 등 추적 파일은 자동 갱신돼요 (worker/src/html.js)"
  echo "dry-run: 유닛 파일은 건드리지 않아요"
  finish 0
fi

PREV="$PROD_HEAD"
echo "previous prod commit $PREV"

install -d -m 0700 "$BACKUP_DIR" 2>/dev/null || true
if [[ -f "$PROD_DB" ]]; then
  TIMESTAMP=$(date +%Y%m%d-%H%M%S)
  BACKUP="$BACKUP_DIR/usage-${TIMESTAMP}.sqlite3"
  echo "prod DB를 백업해요: $BACKUP"
  if ! /usr/bin/python3 - "$PROD_DB" "$BACKUP" <<'PY'
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
  then
    echo "FAIL DB 백업 실패. 배포하지 않아요."; finish 1
  fi
  chmod 0600 "$BACKUP" 2>/dev/null || true
  count=$(find "$BACKUP_DIR" -maxdepth 1 -name 'usage-*.sqlite3' -print | wc -l)
  if [[ "$count" -gt 7 ]]; then
    ls -1t "$BACKUP_DIR"/usage-*.sqlite3 | tail -n +8 | xargs -r rm -f
    echo "백업 정리: 최근 7개 유지"
  fi
else
  echo "prod DB가 없어서 백업하지 않아요 (첫 배포)"
fi

# DB 백업은 수동 복구용이에요. 서버가 시작할 때 schema.sql을 additive로 적용하므로
# 롤백해도 DB를 되돌리지 않아요. 스키마 추가는 이미 적용된 상태로 남습니다.
echo "prod worktree를 이동해요: git checkout --detach $TARGET"
if ! git -C "$PROD_WORKTREE" checkout --detach "$TARGET"; then
  echo "FAIL prod worktree 이동 실패. 배포하지 않아요."; finish 1
fi
echo "prod 서비스를 재시작해요: $PROD_UNIT"
if ! sudo systemctl restart "$PROD_UNIT"; then
  echo "FAIL 재시작 실패. 롤백을 시도해요."
  if ! git -C "$PROD_WORKTREE" checkout --detach "$PREV"; then
    echo "FAIL 롤백 checkout 실패(수동 확인 필요)"; finish 6
  fi
  sudo systemctl restart "$PROD_UNIT" 2>/dev/null || true
  if health_check; then
    echo "롤백 완료 및 점검 통과"
    finish 5
  else
    echo "롤백 후에도 점검 실패 (수동 확인 필요)"; finish 6
  fi
fi

echo "prod 점검 중 (http://127.0.0.1:${PROD_PORT}/health 및 /)..."
if health_check; then
  echo "점검 통과"
  finish 0
else
  echo "FAIL 배포 후 점검 실패. 이전 커밋으로 롤백해요 ($PREV)"
  if ! git -C "$PROD_WORKTREE" checkout --detach "$PREV"; then
    echo "FAIL 롤백 checkout 실패(수동 확인 필요)"; finish 6
  fi
  if ! sudo systemctl restart "$PROD_UNIT"; then
    echo "FAIL 롤백 재시작 실패(수동 확인 필요)"; finish 6
  fi
  if health_check; then
    echo "롤백 완료 및 점검 통과"
    finish 5
  else
    echo "FAIL 롤백 후에도 점검 실패 (수동 확인 필요)"; finish 6
  fi
fi
