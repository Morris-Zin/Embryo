#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="khitmee-perf"
BACKEND_CONTAINER="khitmee-perf-backend-1"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
RESULT_ROOT="$REPO_ROOT/perf-suite/results/offload"

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/offload/run_summary_benchmark.sh --run-id ID --company NAME --from-date YYYY-MM-DD --to-date YYYY-MM-DD [--maintenance-rows N]

Builds and queries the lightweight MariaDB daily-summary proof-of-concept table
inside the isolated khitmee-perf database, verifies exact parity for the
canonical additive aggregate, measures the line-upsert maintenance primitive,
then rolls the microbenchmark mutation back and drops the table by default.
EOF_USAGE
}

RUN_ID=""
COMPANY=""
FROM_DATE=""
TO_DATE=""
MAINTENANCE_ROWS=1000

while (($#)); do
  case "$1" in
    --run-id) RUN_ID="${2:?missing --run-id value}"; shift 2 ;;
    --run-id=*) RUN_ID="${1#--run-id=}"; shift ;;
    --company) COMPANY="${2:?missing --company value}"; shift 2 ;;
    --company=*) COMPANY="${1#--company=}"; shift ;;
    --from-date) FROM_DATE="${2:?missing --from-date value}"; shift 2 ;;
    --from-date=*) FROM_DATE="${1#--from-date=}"; shift ;;
    --to-date) TO_DATE="${2:?missing --to-date value}"; shift 2 ;;
    --to-date=*) TO_DATE="${1#--to-date=}"; shift ;;
    --maintenance-rows) MAINTENANCE_ROWS="${2:?missing --maintenance-rows value}"; shift 2 ;;
    --maintenance-rows=*) MAINTENANCE_ROWS="${1#--maintenance-rows=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$RUN_ID" || -z "$COMPANY" || -z "$FROM_DATE" || -z "$TO_DATE" ]]; then
  echo "--run-id, --company, --from-date and --to-date are required" >&2
  exit 2
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Invalid --run-id; use letters, numbers, dot, underscore or dash" >&2
  exit 2
fi
if [[ ! "$MAINTENANCE_ROWS" =~ ^[0-9]+$ ]] || (( MAINTENANCE_ROWS < 1 || MAINTENANCE_ROWS > 100000 )); then
  echo "--maintenance-rows must be an integer from 1 to 100000" >&2
  exit 2
fi

project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$BACKEND_CONTAINER" 2>/dev/null || true)"
if [[ "$project" != "$PROJECT_NAME" ]]; then
  echo "Refusing to run: $BACKEND_CONTAINER is not part of $PROJECT_NAME" >&2
  exit 3
fi

RUN_DIR="$RESULT_ROOT/$RUN_ID"
STATS_OUTPUT="$RUN_DIR/container-stats.csv"
STATS_SUMMARY="$RUN_DIR/container-stats-summary.json"
STATS_LOG="$RUN_DIR/container-stats.log"
STATS_PID=""
start_stats_collector() {
  rm -f "$STATS_OUTPUT"
  bash "$REPO_ROOT/perf-suite/scripts/stats-collector.sh" --output "$STATS_OUTPUT" --interval 1 > /dev/null 2> "$STATS_LOG" &
  STATS_PID=$!
}
stop_stats_collector() {
  if [[ -n "$STATS_PID" ]]; then
    kill "$STATS_PID" 2>/dev/null || true
    wait "$STATS_PID" 2>/dev/null || true
    STATS_PID=""
  fi
  if [[ -f "$STATS_OUTPUT" ]]; then
    python3 "$REPO_ROOT/perf-suite/scripts/summarize_container_stats.py" "$STATS_OUTPUT" --json-out "$STATS_SUMMARY" > /dev/null 2>&1 || true
  fi
}
trap stop_stats_collector EXIT
mkdir -p "$RUN_DIR"
start_stats_collector
REMOTE_JSON="/tmp/khitmee-summary-benchmark.json"

docker exec "$BACKEND_CONTAINER" mkdir -p /tmp/khitmee-offload-src
for file in offload_spec.py export_snapshot.py probe_mariadb.py summary_benchmark.py; do
  docker cp "$SCRIPT_DIR/$file" "$BACKEND_CONTAINER:/tmp/khitmee-offload-src/$file" >/dev/null
done

set +e
docker exec -i -e PYTHONDONTWRITEBYTECODE=1 -w /tmp/khitmee-offload-src "$BACKEND_CONTAINER" \
  /home/frappe/frappe-bench/env/bin/python /tmp/khitmee-offload-src/summary_benchmark.py \
  --company "$COMPANY" --from-date "$FROM_DATE" --to-date "$TO_DATE" \
  --maintenance-rows "$MAINTENANCE_ROWS" --json-out "$REMOTE_JSON" | tee "$RUN_DIR/summary-summary.json"
BENCH_STATUS="${PIPESTATUS[0]}"
set -e

docker cp "$BACKEND_CONTAINER:$REMOTE_JSON" "$RUN_DIR/summary-report.json" >/dev/null
docker exec "$BACKEND_CONTAINER" rm -f "$REMOTE_JSON"
stop_stats_collector
echo "Summary benchmark complete: $RUN_DIR/summary-report.json"
exit "$BENCH_STATUS"
