#!/usr/bin/env bash
# Создаёт Secret'ы srvmon из файлов прежнего развёртывания на rpi1 (/etc/srvmon).
# Значения передаются ssh -> kubectl через stdin и не сохраняются на локальном диске.
# Повторный запуск идемпотентен (apply). Секреты в Git не попадают.
set -euo pipefail
RPI=${RPI:-luphu@192.168.100.71}
NS=${NS:-srvmon}

remote() { ssh "$RPI" "sudo sh -c '$1'"; }
apply_secret() {  # name, затем пары key=команда
  local name=$1; shift
  local args=()
  local tmp; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
  for kv in "$@"; do
    remote "${kv#*=}" > "$tmp/${kv%%=*}"
    args+=(--from-file="${kv%%=*}=$tmp/${kv%%=*}")
  done
  kubectl -n "$NS" create secret generic "$name" "${args[@]}" --dry-run=client -o yaml | kubectl apply -f -
}

apply_secret srvmon-vm-auth \
  'username=. /etc/srvmon/report.env; printf %s "$VM_AUTH_USER"' \
  'password=. /etc/srvmon/report.env; printf %s "$VM_AUTH_PASSWORD"'
apply_secret srvmon-telegram \
  'bot_token=tr -d "\n" < /etc/srvmon/tg_token' \
  'chat_id=. /etc/srvmon/report.env; printf %s "$TG_CHAT_ID"'
apply_secret srvmon-ssh \
  'server_key=cat /etc/srvmon/server_key' \
  'known_hosts=cat /etc/srvmon/known_hosts'
apply_secret srvmon-report \
  'HC_DAILY_REPORT_URL=. /etc/srvmon/report.env; printf %s "$HC_DAILY_REPORT_URL"'

# Pull secret для приватных образов GHCR: GHCR_USER / GHCR_TOKEN (PAT с read:packages)
if [[ -n "${GHCR_TOKEN:-}" ]]; then
  kubectl -n "$NS" create secret docker-registry ghcr-pull --docker-server=ghcr.io \
    --docker-username="${GHCR_USER:-luphu}" --docker-password="$GHCR_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "GHCR_TOKEN не задан — ghcr-pull пропущен" >&2
fi
