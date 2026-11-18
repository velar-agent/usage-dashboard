#!/usr/bin/env bash
# supercronic 으로 매일 00:00(Asia/Seoul) nightly-promote.sh 를 돌리는 스케줄러를 (없으면) 띄워요. 여러 번, 동시에 실행해도 안전해요(멱등).
# crontab 은 repo 의 worker/cron/crontab 을 그대로 써요(이 스크립트가 만들지 않아요). 토큰은 ~/.config/usage-deploy/cf-token (repo 밖).
# 상태: --status / 중지: --stop / 다시 시작: --restart
# 운영 자정 배포는 GCE VM 의 시스템 cron 으로 옮겼어요. 여기서 띄우려면(시작·재시작) FORCE_START=1 이 필요해요.
# pidfile 이 아니라 "이 crontab 으로 도는 supercronic 프로세스 전부"를 기준으로 판단하고, 시작·중지는 flock 으로 직렬화해요.
set -u
export TZ=Asia/Seoul
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONF=${USAGE_DEPLOY_CONF:-$HOME/.config/usage-deploy}
STATE=${USAGE_DEPLOY_STATE:-$HOME/.local/state/usage-deploy}
CRONTAB=$(cd "$HERE/../cron" && pwd)/crontab   # repo 안의 worker/cron/crontab (git 으로 관리)
LEGACY_CRONTAB="$CONF/crontab"                 # 예전 위치(repo 밖). 이 파일로 도는 옛 supercronic 은 정리 대상
PIDFILE="$STATE/supercronic.pid"; LOGFILE="$STATE/supercronic.log"
SC=$(command -v supercronic || true)
for c in "$HOME/.local/bin/supercronic" /usr/local/bin/supercronic; do [[ -x "${SC:-}" ]] && break; SC=$c; done
mkdir -p "$CONF" "$STATE"; chmod 700 "$CONF" "$STATE" 2>/dev/null

# 예전 위치의 crontab 으로 도는 옛 supercronic PID 들
legacy_procs() {
  local p
  for p in $(pgrep -f -- "supercronic .*$LEGACY_CRONTAB" 2>/dev/null); do
    [[ "$p" == "$$" ]] && continue
    tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "^[^ ]*supercronic .*$LEGACY_CRONTAB" && echo "$p"
  done
}
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
  for p in $(procs) $(legacy_procs); do kill "$p" 2>/dev/null; done
  for _ in 1 2 3 4 5; do [[ -z "$(procs)$(legacy_procs)" ]] && break; sleep 1; done
  for p in $(procs) $(legacy_procs); do kill -9 "$p" 2>/dev/null; done
  rm -f "$PIDFILE"
}

case "${1:-}" in
  --status)
    ps_=$(procs | tr '\n' ' ')
    [[ -n "$(legacy_procs)" ]] && echo "WARN 예전 위치($LEGACY_CRONTAB)로 도는 supercronic 이 있어요: $(legacy_procs | tr '\n' ' '). --restart 로 정리하세요" >&2
    if [[ -n "${ps_// /}" ]]; then
      n=$(wc -w <<<"$ps_"); echo "running pid=${ps_% } (processes: $n)"; [[ "$n" -gt 1 ]] && echo "WARN 여러 개가 떠 있어요. --restart 로 정리하세요" >&2
      exit 0
    fi
    echo "not running"; exit 1 ;;
  --stop|--restart|"") ;;
  *) echo "usage: $0 [--status|--stop|--restart]" >&2; exit 2 ;;
esac

# 운영 자정 배포는 GCE VM 의 시스템 cron(/etc/cron.d/usage-dashboard-nightly)이 해요.
# 두 곳에서 배포하지 않도록, 띄우기(시작·재시작)는 FORCE_START=1 일 때만 해요. --status·--stop 은 그대로 돼요.
if [[ "${1:-}" != --stop && "${FORCE_START:-}" != 1 ]]; then
  echo "start-nightly-cron.sh: 자정 배포는 GCE VM 에서 돌아요(worker/README.md 참고). 정말 여기서 띄우려면 FORCE_START=1 로 실행하세요." >&2
  exit 1
fi

# 시작·중지는 한 번에 하나만
exec 8>"$STATE/start.lock"
flock -w 30 8 || { echo "다른 start/stop 이 오래 진행 중이에요" >&2; exit 1; }

case "${1:-}" in
  --stop) stop_all; echo stopped; exit 0 ;;
  --restart) stop_all ;;
esac

# 옛 위치로 도는 게 남아 있으면(마이그레이션) 겹쳐 돌지 않게 먼저 정리
if [[ -n "$(legacy_procs)" ]]; then
  for p in $(legacy_procs); do kill "$p" 2>/dev/null; done
  for _ in 1 2 3 4 5; do [[ -z "$(legacy_procs)" ]] && break; sleep 1; done
  for p in $(legacy_procs); do kill -9 "$p" 2>/dev/null; done
fi

running=$(procs)
if [[ -n "$running" ]]; then
  # 이미 떠 있음. 둘 이상이면 하나(가장 오래된 pid)만 남기고 정리
  keep=$(sort -n <<<"$running" | head -1)
  for p in $running; do [[ "$p" != "$keep" ]] && kill "$p" 2>/dev/null; done
  echo "$keep" > "$PIDFILE"
  exit 0
fi
[[ -x "$SC" ]] || { echo "supercronic not found" >&2; exit 1; }

[[ -f "$CRONTAB" ]] || { echo "crontab 없음: $CRONTAB" >&2; exit 1; }
"$SC" -test "$CRONTAB" >/dev/null 2>&1 || { echo "crontab 검증 실패" >&2; exit 1; }

# 잠금 fd(8)가 supercronic 에 상속되지 않게 닫고 띄워요
nohup setsid "$SC" "$CRONTAB" >>"$LOGFILE" 2>&1 < /dev/null 8>&- &
echo $! > "$PIDFILE"
sleep 1
if [[ -n "$(procs)" ]]; then echo "started pid=$(cat "$PIDFILE")"; else echo "시작 실패. $LOGFILE 확인" >&2; exit 1; fi
