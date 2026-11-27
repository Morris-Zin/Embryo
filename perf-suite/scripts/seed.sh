#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/scripts/seed.sh [--scale smoke|month|year]

Seeds the isolated khitmee-perf backend container with the benchmark dataset.
The script never targets the live khitmee-erpnext project.
EOF_USAGE
}

SCALE="smoke"
while (($#)); do
  case "$1" in
    --scale)
      [[ $# -ge 2 ]] || { echo "Missing value for --scale" >&2; exit 2; }
      SCALE="$2"
      shift 2
      ;;
    --scale=*)
      SCALE="${1#--scale=}"
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

case "$SCALE" in
  smoke|month|year) ;;
  *)
    echo "Invalid --scale '$SCALE'; expected smoke, month, or year" >&2
    exit 2
    ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
SEEDER_DIR="$ROOT/perf-suite/seeder"
CONTAINER="khitmee-perf-backend-1"

if [[ ! -f "$SEEDER_DIR/seed_dataset.py" ]]; then
  echo "Missing seeder: $SEEDER_DIR/seed_dataset.py" >&2
  exit 1
fi
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Benchmark backend container $CONTAINER is not running" >&2
  exit 1
fi
if docker inspect --format '{{.Config.Labels.com.docker.compose.project}}' "$CONTAINER" | grep -qx 'khitmee-erpnext'; then
  echo "Refusing to seed the live khitmee-erpnext project" >&2
  exit 1
fi

ORIGINAL_BACKEND_MEMORY=""
ORIGINAL_BACKEND_SWAP=""
RESTORE_BACKEND_MEMORY=0
if [[ "$SCALE" == "year" ]]; then
  ORIGINAL_BACKEND_MEMORY="$(docker inspect --format '{{.HostConfig.Memory}}' "$CONTAINER")"
  ORIGINAL_BACKEND_SWAP="$(docker inspect --format '{{.HostConfig.MemorySwap}}' "$CONTAINER")"
  if [[ "$ORIGINAL_BACKEND_MEMORY" =~ ^[0-9]+$ ]] && (( ORIGINAL_BACKEND_MEMORY > 0 && ORIGINAL_BACKEND_MEMORY < 2147483648 )); then
    echo "Temporarily raising the isolated backend memory cap for one-time year seed generation"
    docker update --memory 2147483648 --memory-swap 2147483648 "$CONTAINER" >/dev/null
    RESTORE_BACKEND_MEMORY=1
  fi
fi
restore_backend_memory() {
  if [[ "$RESTORE_BACKEND_MEMORY" == "1" ]]; then
    docker update --memory "$ORIGINAL_BACKEND_MEMORY" --memory-swap "$ORIGINAL_BACKEND_SWAP" "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap restore_backend_memory EXIT

docker exec "$CONTAINER" rm -rf /tmp/khitmee-seeder /tmp/khitmee-seed-manifest.json
docker cp "$SEEDER_DIR" "$CONTAINER:/tmp/khitmee-seeder"
docker exec -i -e PYTHONDONTWRITEBYTECODE=1 \
  -w /home/frappe/frappe-bench/sites \
  "$CONTAINER" \
  /home/frappe/frappe-bench/env/bin/python /tmp/khitmee-seeder/seed_dataset.py \
  --scale "$SCALE" \
  --manifest-output /tmp/khitmee-seed-manifest.json

mkdir -p "$ROOT/perf-suite/results"
docker cp "$CONTAINER:/tmp/khitmee-seed-manifest.json" "$ROOT/perf-suite/results/seed-manifest-$SCALE.json" >/dev/null
echo "Seed manifest copied to perf-suite/results/seed-manifest-$SCALE.json"
