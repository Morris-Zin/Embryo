#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="khitmee-perf"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PERF_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DOCKER_DIR="$PERF_DIR/docker"
ENV_FILE="$DOCKER_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

SITE_NAME="${SITE_NAME:-bench-loadtest.localhost}"
PERF_HTTP_PORT="${PERF_HTTP_PORT:-18086}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is missing in $ENV_FILE}"

base_url="http://127.0.0.1:${PERF_HTTP_PORT}"
cookie_jar="$(mktemp)"
search_response="$(mktemp)"
cleanup() {
  rm -f -- "$cookie_jar" "$search_response"
}
trap cleanup EXIT
chmod 600 "$cookie_jar" "$search_response" 2>/dev/null || true

general_status="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' \
  -H "Host: $SITE_NAME" \
  "$base_url/api/method/ping")"
printf 'general_route=/api/method/ping status=%s\n' "$general_status"

login_status="$(printf '%s' "$ADMIN_PASSWORD" | curl -sS --max-time 10 -X POST \
  -o /dev/null -w '%{http_code}' \
  -H "Host: $SITE_NAME" \
  -c "$cookie_jar" \
  --data-urlencode "usr=Administrator" \
  --data-urlencode "pwd@-" \
  "$base_url/api/method/login")"
printf 'login_route=/api/method/login status=%s\n' "$login_status"

search_url="$base_url/api/method/khitmee_ops.api.search.search_items?q=KM-00001&warehouse=Shop%201%20-%20KMD&price_list=Standard%20Selling&limit=5"
cashier_status="$(curl -sS --max-time 15 -o "$search_response" -w '%{http_code}' \
  -H "Host: $SITE_NAME" \
  -b "$cookie_jar" \
  "$search_url")"
printf 'cashier_route=/api/method/khitmee_ops.api.search.search_items status=%s\n' "$cashier_status"

frontend_id="$(docker ps \
  --filter "label=com.docker.compose.project=$PROJECT_NAME" \
  --filter "label=com.docker.compose.service=frontend" \
  --format '{{.ID}}' | head -n 1)"
if [[ -z "$frontend_id" ]]; then
  echo "No running frontend container for $PROJECT_NAME" >&2
  exit 1
fi

general_line="$(docker logs "$frontend_id" 2>&1 | awk '/\/api\/method\/ping/ { line=$0 } END { print line }')"
cashier_line="$(docker logs "$frontend_id" 2>&1 | awk '/search_items/ { line=$0 } END { print line }')"

printf 'general_nginx=%s\n' "$general_line"
printf 'cashier_nginx=%s\n' "$cashier_line"

if [[ "$general_line" != *"upstream_name=general"* ]]; then
  echo "General route did not reach the general pool according to nginx" >&2
  exit 1
fi
if [[ "$cashier_line" != *"upstream_name=cashier"* ]]; then
  echo "Cashier route did not reach the cashier pool according to nginx" >&2
  exit 1
fi

echo "PASS: nginx routed one general request and one cashier request to the intended pools"
