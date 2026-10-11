#!/bin/sh
# Обратный SSH-туннель до сервера: порт TUNNEL_REMOTE на 127.0.0.1 сервера -> TUNNEL_TARGET в кластере.
# Повторяет поведение systemd-юнита srvmon-tunnel: autossh после 3 запусков ssh завершается,
# цикл снимает «зависшую» сессию на сервере (держит порт после обрыва связи) и запускает заново.
set -u
: "${SERVER:?}" "${TUNNEL_REMOTE:?}" "${TUNNEL_TARGET:?}"
KEY=/tmp/server_key
install -m 600 /secrets/server_key "$KEY"   # ssh отвергает ключ из Secret с правами, не равными 0600
SSH_OPTS="-i $KEY -o IdentitiesOnly=yes -o BatchMode=yes \
  -o UserKnownHostsFile=/secrets/known_hosts -o StrictHostKeyChecking=yes -o ConnectTimeout=15"
PORT=${TUNNEL_REMOTE##*:}
export AUTOSSH_GATETIME=0 AUTOSSH_MAXSTART=3

while true; do
  ssh $SSH_OPTS "$SERVER" \
    "pid=\$(ss -Htlnp 'sport = :$PORT' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2);
     [ -n \"\$pid\" ] && kill \"\$pid\" && echo \"killed stale tunnel pid \$pid (port $PORT)\"; true" \
    || echo "cleanup failed (rc=$?)"
  echo "$(date -Iseconds) starting tunnel -R $TUNNEL_REMOTE:$TUNNEL_TARGET -> $SERVER"
  autossh -M 0 -N $SSH_OPTS \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
    -R "$TUNNEL_REMOTE:$TUNNEL_TARGET" "$SERVER"
  echo "$(date -Iseconds) autossh exited (rc=$?), restart in 10s"
  sleep 10
done
