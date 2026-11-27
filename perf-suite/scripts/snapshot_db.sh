#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/scripts/snapshot_db.sh /path/to/year-db.tar.gz

Creates a stopped-database tar.gz snapshot of the isolated khitmee-perf MariaDB
volume so identical benchmark datasets can be restored before candidate runs.
It never targets the live khitmee-erpnext project.
EOF_USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
ARCHIVE="${1:?snapshot archive path is required}"
CONTAINER="khitmee-perf-db-1"
PROJECT="khitmee-perf"
VOLUME="${DB_VOLUME:-perf-db-data}"
IMAGE="${DB_IMAGE:-mariadb:11.8}"

project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$project" != "$PROJECT" ]]; then
  echo "Refusing snapshot: $CONTAINER is not part of $PROJECT" >&2
  exit 3
fi
mkdir -p "$(dirname "$ARCHIVE")"
ARCHIVE="$(cd -- "$(dirname -- "$ARCHIVE")" && pwd)/$(basename -- "$ARCHIVE")"
TMP_ARCHIVE="$ARCHIVE.tmp"
rm -f "$TMP_ARCHIVE"

DB_STOPPED=0
restart_db() {
  if [[ "$DB_STOPPED" == "1" ]]; then
    docker start "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap restart_db EXIT

echo "Stopping $CONTAINER and snapshotting volume $VOLUME to $ARCHIVE"
docker stop "$CONTAINER" >/dev/null
DB_STOPPED=1
docker run --rm \
  -e SNAPSHOT_NAME="$(basename "$TMP_ARCHIVE")" \
  -v "$VOLUME:/from:ro" \
  -v "$(dirname "$ARCHIVE"):/snapshot" \
  "$IMAGE" \
  sh -c 'cd /from && tar czf "/snapshot/$SNAPSHOT_NAME" .'
mv "$TMP_ARCHIVE" "$ARCHIVE"
docker start "$CONTAINER" >/dev/null
DB_STOPPED=0
echo "Snapshot complete: $ARCHIVE"
