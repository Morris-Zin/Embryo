#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/scripts/restore_db_snapshot.sh /path/to/year-db.tar.gz

Restores the isolated khitmee-perf MariaDB volume from a stopped-database tar.gz
snapshot. This is only for immutable benchmark resets; it never targets the live
khitmee-erpnext project.
EOF_USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
ARCHIVE="${1:?snapshot archive path is required}"
if [[ ! -f "$ARCHIVE" ]]; then
  echo "Snapshot archive does not exist: $ARCHIVE" >&2
  exit 2
fi
ARCHIVE="$(cd -- "$(dirname -- "$ARCHIVE")" && pwd)/$(basename -- "$ARCHIVE")"
CONTAINER="khitmee-perf-db-1"
PROJECT="khitmee-perf"
VOLUME="${DB_VOLUME:-perf-db-data}"
IMAGE="${DB_IMAGE:-${MARIADB_IMAGE:-mariadb:11.8}}"

project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$project" != "$PROJECT" ]]; then
  echo "Refusing restore: $CONTAINER is not part of $PROJECT" >&2
  exit 3
fi

DB_STOPPED=0
restart_db() {
  if [[ "$DB_STOPPED" == "1" ]]; then
    docker start "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap restart_db EXIT

echo "Stopping $CONTAINER and restoring $(basename "$ARCHIVE") into volume $VOLUME"
docker stop "$CONTAINER" >/dev/null
DB_STOPPED=1
docker run --rm \
  -e SNAPSHOT_NAME="$(basename "$ARCHIVE")" \
  -v "$VOLUME:/from" \
  -v "$(dirname "$ARCHIVE"):/snapshot:ro" \
  "$IMAGE" \
  sh -c '
    set -e
    trap "rm -rf /from/.restore-new" EXIT
    rm -rf /from/.restore-new /from/.restore-old
    mkdir /from/.restore-new
    tar xzf "/snapshot/$SNAPSHOT_NAME" -C /from/.restore-new
    mkdir /from/.restore-old
    for path in /from/* /from/.[!.]* /from/..?*; do
      [ -e "$path" ] || continue
      case "$path" in /from/.restore-new|/from/.restore-old) continue ;; esac
      mv "$path" /from/.restore-old/
    done
    for path in /from/.restore-new/* /from/.restore-new/.[!.]* /from/.restore-new/..?*; do
      [ -e "$path" ] || continue
      mv "$path" /from/
    done
    rmdir /from/.restore-new
    trap - EXIT
    rm -rf /from/.restore-old
  '
docker start "$CONTAINER" >/dev/null
DB_STOPPED=0

for _ in $(seq 1 120); do
  status="$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || true)"
  if [[ "$status" == "healthy" ]]; then
    echo "Restored $VOLUME; $CONTAINER is healthy"
    exit 0
  fi
  sleep 2
done
echo "Restore finished but $CONTAINER did not become healthy in time" >&2
exit 4
