#!/usr/bin/env bash
# supercronic 으로 매일 00:00(Asia/Seoul) nightly-promote.sh 를 돌리는 스케줄러를 (없으면) 띄워요. 여러 번, 동시에 실행해도 안전해요(멱등).
# 상태: --status / 중지: --stop / 다시 시작: --restart
# pidfile 이 아니라 "이 crontab 으로 도는 supercronic 프로세스 전부"를 기준으로 판단하고, 시작·중지는 flock 으로 직렬화해요.
set -u
export TZ=Asia/Seoul
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RUNNER="$HERE/nightly-promote.sh"
CONF=${USAGE_DEPLOY_CONF:-$HOME/.config/usage-deploy}
STATE=${USAGE_DEPLOY_STATE:-$HOME/.local/state/usage-deploy}
CRONTAB="$CONF/crontab"; PIDFILE="$STATE/supercronic.pid"; LOGFILE="$STATE/supercronic.log"
SC=$(command -v supercronic || true)
for c in "$HOME/.local/bin/supercronic" /usr/local/bin/supercronic; do [[ -x "${SC:-}" ]] && break; SC=$c; done
mkdir -p "$CONF" "$STATE"; chmod 700 "$CONF" "$STATE" 2>/dev/null

# 이 crontab 을 쓰는 supercronic PID 들 (한 줄에 하나)
procs() {
  local p
  for p in $(pgrep -f -- "supercronic .*$CRONTAB" 2>/dev/null); do
    [[ "$p" == "$$" ]] && continue
    tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "^[^ ]*supercronic .*$CRONTAB" && echo "$p"
  done
}
stop_all() {
  local p
  for p in $(procs); do kill "$p" 2>/dev/null; done
  for _ in 1 2 3 4 5; do [[ -z "$(procs)" ]] && break; sleep 1; done
  for p in $(procs); do kill -9 "$p" 2>/dev/null; done
  rm -f "$PIDFILE"
}

case "${1:-}" in
  --status)
    ps_=$(procs | tr '\n' ' ')
    if [[ -n "${ps_// /}" ]]; then
      n=$(wc -w <<<"$ps_"); echo "running pid=${ps_% } (processes: $n)"; [[ "$n" -gt 1 ]] && echo "WARN 여러 개가 떠 있어요. --restart 로 정리하세요" >&2
      exit 0
    fi
    echo "not running"; exit 1 ;;
  --stop|--restart|"") ;;
  *) echo "usage: $0 [--status|--stop|--restart]" >&2; exit 2 ;;
esac

# 시작·중지는 한 번에 하나만
exec 8>"$STATE/start.lock"
flock -w 30 8 || { echo "다른 start/stop 이 오래 진행 중이에요" >&2; exit 1; }

case "${1:-}" in
  --stop) stop_all; echo stopped; exit 0 ;;
  --restart) stop_all ;;
esac

running=$(procs)
if [[ -n "$running" ]]; then
  # 이미 떠 있음. 둘 이상이면 하나(가장 오래된 pid)만 남기고 정리
  keep=$(sort -n <<<"$running" | head -1)
  for p in $running; do [[ "$p" != "$keep" ]] && kill "$p" 2>/dev/null; done
  echo "$keep" > "$PIDFILE"
  exit 0
fi
[[ -x "$SC" ]] || { echo "supercronic not found" >&2; exit 1; }

cat > "$CRONTAB" <<EOF
# 매일 00:00 Asia/Seoul: 개발(usage-dashboard-dev) → 운영(usage-dashboard) 코드 배포
CRON_TZ=Asia/Seoul
TZ=Asia/Seoul
0 0 * * * $RUNNER
EOF
"$SC" -test "$CRONTAB" >/dev/null 2>&1 || { echo "crontab 검증 실패" >&2; exit 1; }

# 잠금 fd(8)가 supercronic 에 상속되지 않게 닫고 띄워요
nohup setsid "$SC" "$CRONTAB" >>"$LOGFILE" 2>&1 < /dev/null 8>&- &
echo $! > "$PIDFILE"
sleep 1
if [[ -n "$(procs)" ]]; then echo "started pid=$(cat "$PIDFILE")"; else echo "시작 실패. $LOGFILE 확인" >&2; exit 1; fi
