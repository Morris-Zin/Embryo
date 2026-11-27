#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="khitmee-perf"
LIVE_PROJECT_NAME="khitmee-erpnext"
PURGE=0

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/scripts/down.sh [--purge]

Stops the disposable khitmee-perf benchmark stack.
The script never targets the live khitmee-erpnext project.

Options:
  --purge    Also delete only the three perf volumes: perf-db-data, perf-sites, perf-redis-queue-data
  -h, --help Show this help
EOF_USAGE
}

while (($#)); do
  case "$1" in
    --purge)
      PURGE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$PROJECT_NAME" == "$LIVE_PROJECT_NAME" ]]; then
  echo "Refusing to target the live $LIVE_PROJECT_NAME project" >&2
  exit 2
fi

if [[ -n "${COMPOSE_PROJECT_NAME:-}" && "$COMPOSE_PROJECT_NAME" != "$PROJECT_NAME" ]]; then
  echo "Refusing to stop: COMPOSE_PROJECT_NAME is '$COMPOSE_PROJECT_NAME', expected '$PROJECT_NAME'" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PERF_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DOCKER_DIR="$PERF_DIR/docker"

# Both profile files are included so every possible perf service is present in
# the Compose model. This command never starts containers; down-only dummy
# values avoid reading the secret-bearing local .env file.
compose() {
  DB_PASSWORD=down-unused \
  SITE_NAME=bench-loadtest.localhost \
    docker compose \
      --project-name "$PROJECT_NAME" \
      --project-directory "$DOCKER_DIR" \
      --env-file /dev/null \
      -f "$DOCKER_DIR/compose.base.yaml" \
      -f "$DOCKER_DIR/profile-2gb.yaml" \
      -f "$DOCKER_DIR/profile-4gb.yaml" \
      "$@"
}

snapshot_live_project() {
  docker ps -a \
    --filter "label=com.docker.compose.project=$LIVE_PROJECT_NAME" \
    --format '{{.ID}} {{.State}}' | LC_ALL=C sort
}

live_before="$(snapshot_live_project)"

echo "Stopping $PROJECT_NAME..."
compose down --remove-orphans

if [[ "$PURGE" == "1" ]]; then
  echo "Purging only the explicit perf volumes..."
  for volume in perf-db-data perf-sites perf-redis-queue-data; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      volume_project="$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$volume" 2>/dev/null || printf '')"
      if [[ "$volume_project" != "$PROJECT_NAME" ]]; then
        echo "Refusing to remove volume $volume: com.docker.compose.project label is '${volume_project:-<unset>}', expected '$PROJECT_NAME'" >&2
        exit 1
      fi
      docker volume rm "$volume" >/dev/null
      echo "Removed volume $volume"
    fi
  done
fi

live_after="$(snapshot_live_project)"
if [[ "$live_before" != "$live_after" ]]; then
  echo "ERROR: $LIVE_PROJECT_NAME container state changed while stopping $PROJECT_NAME" >&2
  exit 1
fi

echo "$PROJECT_NAME is stopped. Verified $LIVE_PROJECT_NAME containers were untouched."
