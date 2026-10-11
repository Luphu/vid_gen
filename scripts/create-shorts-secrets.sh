#!/usr/bin/env bash
# Создаёт Secret'ы приложения shorts из локального env-файла (вне Git, каталог secrets/ в .gitignore).
# Значения передаются в kubectl через временные файлы с правами 0600 и не печатаются.
# Повторный запуск идемпотентен (apply). Пароль PostgreSQL и UPLOAD_URI_KEY генерируются один раз:
# если Secret уже есть в кластере, существующие значения сохраняются.
#
#   cp shorts/.env.example secrets/shorts.env   # заполнить
#   scripts/create-shorts-secrets.sh secrets/shorts.env
set -euo pipefail
ENV_FILE=${1:-secrets/shorts.env}
NS=${NS:-shorts}
[[ -f "$ENV_FILE" ]] || { echo "нет файла $ENV_FILE" >&2; exit 1; }

tmp=$(mktemp -d); chmod 700 "$tmp"; trap 'rm -rf "$tmp"' EXIT
# Значение ключа: последняя строка KEY=..., без хвостовых пробелов. Комментарии — только отдельными строками.
get() { grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -e 's/[[:space:]]*$//' || true; }
existing() { kubectl -n "$NS" get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null | base64 -d 2>/dev/null || true; }

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

apply_secret() {  # name key...  (values from ENV_FILE; empty keys are skipped)
  local name=$1; shift
  local f="$tmp/$name.env"; : > "$f"; chmod 600 "$f"
  for k in "$@"; do
    local v; v=$(get "$k")
    [[ -n "$v" ]] && printf '%s=%s\n' "$k" "$v" >> "$f"
  done
  if [[ ! -s "$f" ]]; then echo "skip $name: нет значений" >&2; return; fi
  kubectl -n "$NS" create secret generic "$name" --from-env-file="$f" --dry-run=client -o yaml | kubectl apply -f -
}

# shorts-db: пароль генерируется при первом запуске
PG_PASS=$(existing shorts-db POSTGRES_PASSWORD)
[[ -n "$PG_PASS" ]] || PG_PASS=$(openssl rand -hex 24)
f="$tmp/db.env"; umask 077
printf 'POSTGRES_PASSWORD=%s\nDATABASE_URL=postgresql://shorts:%s@shorts-postgres:5432/shorts\n' "$PG_PASS" "$PG_PASS" > "$f"
kubectl -n "$NS" create secret generic shorts-db --from-env-file="$f" --dry-run=client -o yaml | kubectl apply -f -

# shorts-app: ключ шифрования upload session URI (32 байта)
KEY=$(existing shorts-app UPLOAD_URI_KEY)
[[ -n "$KEY" ]] || KEY=$(openssl rand -base64 32)
printf 'UPLOAD_URI_KEY=%s\n' "$KEY" > "$tmp/app.env"
kubectl -n "$NS" create secret generic shorts-app --from-env-file="$tmp/app.env" --dry-run=client -o yaml | kubectl apply -f -

apply_secret shorts-nvidia NVIDIA_API_KEY NIM_VIDEO_URL NIM_VIDEO_ASYNC_STATUS_URL
# Refresh token можно не копировать руками: YOUTUBE_TOKEN_FILE=secrets/youtube-token.json (из `shorts auth init`)
TOKEN_FILE=$(get YOUTUBE_TOKEN_FILE)
if [[ -n "$TOKEN_FILE" && -z "$(get YOUTUBE_REFRESH_TOKEN)" ]]; then
  cp "$ENV_FILE" "$tmp/merged.env"
  printf 'YOUTUBE_REFRESH_TOKEN=%s\n' "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["refresh_token"])' "$TOKEN_FILE")" >> "$tmp/merged.env"
  ENV_FILE="$tmp/merged.env"
fi
apply_secret shorts-youtube YOUTUBE_API_KEY YOUTUBE_CLIENT_ID YOUTUBE_CLIENT_SECRET YOUTUBE_REFRESH_TOKEN \
  YOUTUBE_CHANNEL_ID PUBLISH_CONSENT_CHANNEL_ID PUBLISH_MADE_FOR_KIDS PUBLISH_PRIVACY PUBLISH_TIME_UTC
apply_secret shorts-s3 S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY

# Pull secret для приватных образов GHCR: GHCR_USER / GHCR_TOKEN (PAT с read:packages)
if [[ -n "${GHCR_TOKEN:-}" ]]; then
  kubectl -n "$NS" create secret docker-registry ghcr-pull --docker-server=ghcr.io \
    --docker-username="${GHCR_USER:-luphu}" --docker-password="$GHCR_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "GHCR_TOKEN не задан — ghcr-pull пропущен" >&2
fi
echo "готово: $(kubectl -n "$NS" get secret -o name | tr '\n' ' ')"
