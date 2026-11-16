#!/usr/bin/env bash
# 매일 00:00(KST) cron 이 실행하는 러너: 최신 main 을 받고, 점검하고, 개발 코드를 운영(usage-dashboard)에 올려요.
# 비밀 값은 없어요. API 토큰은 파일(기본 ~/.config/usage-deploy/cf-token)에서 promote 스크립트가 직접 읽어요.
#
# 사용법: nightly-promote.sh [--dry-run] [--force]
#   --dry-run  업로드·롤백만 빼고 전부 수행(git pull, precheck, API 읽기, 비교)
# 종료 코드: 0 성공/변경 없음, 1 점검·git 실패, 2 토큰 없음, 3 개발≠main, 4 버전 업로드·배포 생성 실패,
#            5 배포 후 점검 실패→롤백됨, 6 롤백 실패(수동 확인), 7 업로드 전 안전 점검 거부(수동 확인),
#            9 이미 실행 중, 10 사용법
set -u -o pipefail
export TZ=Asia/Seoul
export LC_ALL=C.UTF-8 PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=${USAGE_REPO:-$(cd "$HERE/../.." && pwd)}
PROMOTE="$HERE/promote-dev-to-prod.py"
STATE=${USAGE_DEPLOY_STATE:-$HOME/.local/state/usage-deploy}
TOKEN_FILE=${USAGE_DEPLOY_TOKEN_FILE:-$HOME/.config/usage-deploy/cf-token}

extra=()
for a in "$@"; do
  case "$a" in
    --dry-run|--force) extra+=("$a") ;;
    *) echo "unknown option: $a" >&2; exit 10 ;;
  esac
done

umask 077
mkdir -p "$STATE"; chmod 700 "$STATE"
LOG="$STATE/$(date +%F).log"
log() { printf '%s %s\n' "$(date '+%F %T %Z')" "$*" >> "$LOG"; }

# 겹쳐 실행 방지
exec 9>"$STATE/lock"
if ! flock -n 9; then log "SKIP 이미 다른 실행이 진행 중이에요"; exit 9; fi

# 60일 지난 로그 정리
find "$STATE" -maxdepth 1 -name '20??-??-??.log' -mtime +60 -delete 2>/dev/null

# 모든 출력은 로그로. (토큰은 어디에도 출력하지 않아요)
exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T %Z') nightly-promote start ${extra[*]:-} ==="

write_status() {  # $1=exit code
  printf '{"time":"%s","exit":%s,"args":"%s","log":"%s"}\n' \
    "$(date '+%FT%T%z')" "$1" "${extra[*]:-}" "$LOG" > "$STATE/last-run.json"
}
finish() { echo "=== $(date '+%F %T %Z') done exit=$1 ==="; write_status "$1"; exit "$1"; }

cd "$REPO" || finish 1
# 1) 최신 main (fast-forward 만). main 브랜치·깨끗한 작업 트리·origin/main 과 동일해야만 진행해요
#    (커밋 안 된 수정이나 다른 브랜치의 코드가 운영에 나가지 않게 하기 위함)
branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$branch" != "main" ]]; then echo "FAIL 현재 브랜치가 main 이 아니에요 ($branch). 배포하지 않아요."; finish 1; fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "FAIL 커밋되지 않은 변경이 있어요. 배포하지 않아요:"; git status --short --untracked-files=no; finish 1
fi
if ! git pull --ff-only; then echo "FAIL git pull --ff-only 실패. 배포하지 않아요."; finish 1; fi
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
  echo "FAIL 로컬 HEAD 가 origin/main 과 달라요(푸시 안 된 커밋?). 배포하지 않아요."; finish 1
fi
echo "HEAD $(git rev-parse --short HEAD) (= origin/main)"

# 2) precheck (전부 통과해야 함)
if ! python3 "$PROMOTE" precheck; then echo "FAIL precheck 실패. 배포하지 않아요."; finish 1; fi

# 3) 비교 → 배포 → 배포 후 점검 → 필요하면 롤백 (모두 promote deploy 안에서)
python3 "$PROMOTE" deploy --token-file "$TOKEN_FILE" "${extra[@]}"
rc=$?
finish "$rc"
