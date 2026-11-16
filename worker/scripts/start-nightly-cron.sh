#!/usr/bin/env bash
# supercronic 으로 매일 00:00(Asia/Seoul) nightly-promote.sh 를 돌리는 스케줄러를 (없으면) 띄워요. 여러 번 실행해도 안전해요(멱등).
# 상태: --status / 중지: --stop / 다시 시작: --restart
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

alive() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && grep -q supercronic "/proc/$(cat "$PIDFILE")/cmdline" 2>/dev/null; }
stop()  { if alive; then kill "$(cat "$PIDFILE")"; sleep 1; fi; rm -f "$PIDFILE"; }

case "${1:-}" in
  --status) if alive; then echo "running pid=$(cat "$PIDFILE")"; exit 0; else echo "not running"; exit 1; fi ;;
  --stop) stop; echo stopped; exit 0 ;;
  --restart) stop ;;
  "") ;;
  *) echo "usage: $0 [--status|--stop|--restart]" >&2; exit 2 ;;
esac
alive && exit 0
[[ -x "$SC" ]] || { echo "supercronic not found" >&2; exit 1; }

cat > "$CRONTAB" <<EOF
# 매일 00:00 Asia/Seoul: 개발(usage-dashboard-dev) → 운영(usage-dashboard) 코드 배포
CRON_TZ=Asia/Seoul
TZ=Asia/Seoul
0 0 * * * $RUNNER
EOF
"$SC" -test "$CRONTAB" >/dev/null 2>&1 || { echo "crontab 검증 실패" >&2; exit 1; }

nohup setsid "$SC" "$CRONTAB" >>"$LOGFILE" 2>&1 < /dev/null &
echo $! > "$PIDFILE"
sleep 1
alive && echo "started pid=$(cat "$PIDFILE")" || { echo "시작 실패. $LOGFILE 확인" >&2; exit 1; }
