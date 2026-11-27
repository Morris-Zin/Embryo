#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="khitmee-perf"
BACKEND_CONTAINER="khitmee-perf-backend-1"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
RESULT_ROOT="$REPO_ROOT/perf-suite/results/offload"

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/offload/run_benchmark.sh --run-id ID --company NAME --from-date YYYY-MM-DD --to-date YYYY-MM-DD [options]

Runs the isolated MariaDB-vs-DuckDB offload proof-of-concept benchmark against
khitmee-perf. Secrets stay inside the backend container; exported data remains in
the ignored perf-suite/results tree on the benchmark machine.

Options:
  --mode full|delta          Export mode (default full)
  --modified-after TS        Required for --mode delta; exporter lower bound
  --skip-export              Reuse an existing extracted directory
  --skip-mariadb-probe       Reuse existing MariaDB query results
  --duckdb-python PATH       Python with duckdb installed (default python3)
  --threads N                DuckDB threads (default 2)
  --memory-limit LIMIT       DuckDB memory limit (default 512MB)
  -h, --help                 Show this help
EOF_USAGE
}

RUN_ID=""
COMPANY=""
FROM_DATE=""
TO_DATE=""
MODE="full"
MODIFIED_AFTER=""
SKIP_EXPORT=0
SKIP_MARIADB_PROBE=0
DUCKDB_PYTHON="python3"
THREADS=2
MEMORY_LIMIT="512MB"

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
    --mode) MODE="${2:?missing --mode value}"; shift 2 ;;
    --mode=*) MODE="${1#--mode=}"; shift ;;
    --modified-after) MODIFIED_AFTER="${2:?missing --modified-after value}"; shift 2 ;;
    --modified-after=*) MODIFIED_AFTER="${1#--modified-after=}"; shift ;;
    --skip-export) SKIP_EXPORT=1; shift ;;
    --skip-mariadb-probe) SKIP_MARIADB_PROBE=1; shift ;;
    --duckdb-python) DUCKDB_PYTHON="${2:?missing --duckdb-python value}"; shift 2 ;;
    --duckdb-python=*) DUCKDB_PYTHON="${1#--duckdb-python=}"; shift ;;
    --threads) THREADS="${2:?missing --threads value}"; shift 2 ;;
    --threads=*) THREADS="${1#--threads=}"; shift ;;
    --memory-limit) MEMORY_LIMIT="${2:?missing --memory-limit value}"; shift 2 ;;
    --memory-limit=*) MEMORY_LIMIT="${1#--memory-limit=}"; shift ;;
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
case "$MODE" in
  full|delta) ;;
  *) echo "--mode must be full or delta" >&2; exit 2 ;;
esac
if [[ "$MODE" == "delta" && -z "$MODIFIED_AFTER" ]]; then
  echo "--mode delta requires --modified-after" >&2
  exit 2
fi
if [[ ! "$THREADS" =~ ^[0-9]+$ ]] || (( THREADS < 1 || THREADS > 8 )); then
  echo "--threads must be an integer from 1 to 8" >&2
  exit 2
fi

project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$BACKEND_CONTAINER" 2>/dev/null || true)"
if [[ "$project" != "$PROJECT_NAME" ]]; then
  echo "Refusing to run: $BACKEND_CONTAINER is not part of $PROJECT_NAME" >&2
  exit 3
fi

RUN_DIR="$RESULT_ROOT/$RUN_ID"
EXTRACT_DIR="$RUN_DIR/extract"
REMOTE_DIR="/tmp/khitmee-offload-$RUN_ID"
mkdir -p "$RUN_DIR" "$EXTRACT_DIR"

if [[ "$SKIP_EXPORT" == "0" ]]; then
  echo "Exporting consistent MariaDB snapshot in mode $MODE..."
  docker exec "$BACKEND_CONTAINER" rm -rf /tmp/khitmee-offload-src "$REMOTE_DIR"
  docker exec "$BACKEND_CONTAINER" mkdir -p /tmp/khitmee-offload-src
  for file in offload_spec.py export_snapshot.py; do
    docker cp "$SCRIPT_DIR/$file" "$BACKEND_CONTAINER:/tmp/khitmee-offload-src/$file" >/dev/null
  done
  export_args=(
    /tmp/khitmee-offload-src/export_snapshot.py
    --output-dir "$REMOTE_DIR"
    --mode "$MODE"
  )
  if [[ "$MODE" == "delta" ]]; then
    export_args+=(--modified-after "$MODIFIED_AFTER")
  fi
  docker exec -i -e PYTHONDONTWRITEBYTECODE=1 -w /tmp/khitmee-offload-src "$BACKEND_CONTAINER" \
    /home/frappe/frappe-bench/env/bin/python "${export_args[@]}" | tee "$RUN_DIR/export-summary.json"
  rm -rf "$EXTRACT_DIR"
  mkdir -p "$EXTRACT_DIR"
  docker cp "$BACKEND_CONTAINER:$REMOTE_DIR/." "$EXTRACT_DIR/" >/dev/null
  docker exec "$BACKEND_CONTAINER" rm -rf "$REMOTE_DIR"
fi

MARIADB_JSON="$RUN_DIR/mariadb-results.json"
if [[ "$SKIP_MARIADB_PROBE" == "0" ]]; then
  echo "Running canonical queries inside MariaDB..."
  docker exec "$BACKEND_CONTAINER" mkdir -p /tmp/khitmee-offload-src
  for file in offload_spec.py export_snapshot.py probe_mariadb.py; do
    docker cp "$SCRIPT_DIR/$file" "$BACKEND_CONTAINER:/tmp/khitmee-offload-src/$file" >/dev/null
  done
  docker exec -i -e PYTHONDONTWRITEBYTECODE=1 -w /tmp/khitmee-offload-src "$BACKEND_CONTAINER" \
    /home/frappe/frappe-bench/env/bin/python /tmp/khitmee-offload-src/probe_mariadb.py \
    --company "$COMPANY" --from-date "$FROM_DATE" --to-date "$TO_DATE" \
    --json-out /tmp/khitmee-offload-mariadb-results.json | tee "$RUN_DIR/mariadb-summary.json"
  docker cp "$BACKEND_CONTAINER:/tmp/khitmee-offload-mariadb-results.json" "$MARIADB_JSON" >/dev/null
  docker exec "$BACKEND_CONTAINER" rm -f /tmp/khitmee-offload-mariadb-results.json
fi

if [[ ! -f "$EXTRACT_DIR/manifest.json" ]]; then
  echo "Missing extracted manifest at $EXTRACT_DIR/manifest.json" >&2
  exit 4
fi
if [[ ! -f "$MARIADB_JSON" ]]; then
  echo "Missing MariaDB results at $MARIADB_JSON" >&2
  exit 4
fi

echo "Loading extract and running canonical queries in DuckDB..."
"$DUCKDB_PYTHON" "$SCRIPT_DIR/duckdb_benchmark.py" \
  --extract-dir "$EXTRACT_DIR" \
  --database "$RUN_DIR/offload.duckdb" \
  --company "$COMPANY" \
  --from-date "$FROM_DATE" \
  --to-date "$TO_DATE" \
  --mariadb-results "$MARIADB_JSON" \
  --threads "$THREADS" \
  --memory-limit "$MEMORY_LIMIT" \
  --json-out "$RUN_DIR/duckdb-report.json" \
  --markdown-out "$RUN_DIR/offload-report.md" | tee "$RUN_DIR/duckdb-summary.json"

echo "Offload benchmark complete: $RUN_DIR/offload-report.md"
