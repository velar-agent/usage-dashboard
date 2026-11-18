#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -un) != collector ]]; then
    echo 'collector 계정에서 실행하세요. 유닛 설치에만 sudo를 써요.' >&2
    exit 1
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077
install -d -m 0700 data
env_file=/home/collector/.config/usage-dashboard-google-dev/env
if [[ ! -f "$env_file" ]]; then
    /usr/bin/python3 gen_env.py "$env_file"
fi

app=usage-dashboard-google-dev.service
tunnel=cloudflared-usage-dashboard-google-dev.service
sudo install -m 0644 -t /etc/systemd/system "$app" "$tunnel"
sudo systemctl daemon-reload
sudo systemctl enable --now "$app"
if [[ -x /home/collector/.local/bin/cloudflared ]]; then
    sudo systemctl enable --now "$tunnel"
elif systemctl is-enabled --quiet "$tunnel"; then
    sudo systemctl disable --now "$tunnel"
fi
