#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: bash perf-suite/desktop/run_experiment.sh [options]

Runs reproducible isolated-browser benchmarks for the optimized web counter and
optional local-catalog modes while sampling khitmee-perf server memory/CPU.

Options:
  --modes <list>       off,browser,rust (default: off,browser,rust)
  --scenarios <list>   benchmark scenarios (default: search,scan,repeat-scan,keyboard,lines)
  --profiles <list>    normal,throttled (default: normal,throttled)
  --repeat <n>         repetitions for interactive scenarios (default: 1)
  --output <dir>       artifact directory (default: perf-suite/results/desktop-experiment/<timestamp>)
  --help               show this help

Prerequisites: khitmee-perf is up and seeded; perf-suite/browser dependencies are
installed. Rust mode requires desktop-client/target/release/khitmee-catalog-service.
EOF
}

MODES="off,browser,rust"
SCENARIOS="search,scan,repeat-scan,keyboard,lines"
PROFILES="normal,throttled"
REPEAT="1"
OUTPUT=""
while (($#)); do
  case "$1" in
    --modes) MODES="$2"; shift 2 ;;
    --modes=*) MODES="${1#--modes=}"; shift ;;
    --scenarios) SCENARIOS="$2"; shift 2 ;;
    --scenarios=*) SCENARIOS="${1#--scenarios=}"; shift ;;
    --profiles) PROFILES="$2"; shift 2 ;;
    --profiles=*) PROFILES="${1#--profiles=}"; shift ;;
    --repeat) REPEAT="$2"; shift 2 ;;
    --repeat=*) REPEAT="${1#--repeat=}"; shift ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --output=*) OUTPUT="${1#--output=}"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -z "$OUTPUT" ]]; then
  OUTPUT="$ROOT/perf-suite/results/desktop-experiment/$(date -u +%Y%m%dT%H%M%SZ)"
fi
mkdir -p "$OUTPUT"
SERVICE_PID=""
COLLECTOR_PID=""
cleanup() {
  if [[ -n "$COLLECTOR_PID" ]]; then kill "$COLLECTOR_PID" 2>/dev/null || true; fi
  if [[ -n "$SERVICE_PID" ]]; then kill "$SERVICE_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

start_catalog_service() {
  if curl -fsS --max-time 1 http://127.0.0.1:18422/health >/dev/null 2>&1; then
    return 0
  fi
  local binary="$ROOT/desktop-client/target/release/khitmee-catalog-service"
  if [[ ! -x "$binary" ]]; then
    echo "Rust catalog service binary missing; run: cd desktop-client && cargo build --release" >&2
    return 1
  fi
  "$binary" serve \
    --db "$OUTPUT/catalog.sqlcipher.db" \
    --key "$OUTPUT/catalog.key" \
    --port 18422 \
    --allowed-origin http://127.0.0.1:18086 \
    >"$OUTPUT/catalog-service.log" 2>&1 &
  SERVICE_PID="$!"
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 1 http://127.0.0.1:18422/health >/dev/null 2>&1; then return 0; fi
    sleep 0.2
  done
  echo "Catalog service did not become healthy" >&2
  return 1
}

run_mode() {
  local mode="$1"
  local csv="$OUTPUT/$mode-server.csv"
  local memory_json="$OUTPUT/$mode-server.json"
  local bench_json=""
  if [[ "$mode" == "rust" ]]; then start_catalog_service; fi
  bash "$ROOT/perf-suite/scripts/stats-collector.sh" --output "$csv" --interval 1 >"$OUTPUT/$mode-collector.log" 2>&1 &
  COLLECTOR_PID=$!
  sleep 1
  (
    cd "$ROOT/perf-suite/browser"
    node run_benchmarks.mjs \
      --label "desktop-$mode" \
      --catalog-mode "$mode" \
      --profiles "$PROFILES" \
      --scenarios "$SCENARIOS" \
      --line-counts 10,50 \
      --repeat "$REPEAT"
  ) >"$OUTPUT/$mode-browser.log" 2>&1
  kill "$COLLECTOR_PID" 2>/dev/null || true
  wait "$COLLECTOR_PID" 2>/dev/null || true
  COLLECTOR_PID=""
  python3 "$ROOT/tools/memory_summary.py" "$csv" --json-out "$memory_json"
  bench_json="$(find "$ROOT/perf-suite/results/browser" -path "*desktop-$mode/benchmark.json" -type f -print0 2>/dev/null | sort -z | tail -z -n 1 | tr -d '\0')"
  if [[ -z "$bench_json" ]]; then
    echo "Benchmark JSON missing for $mode" >&2
    return 1
  fi
  cp "$bench_json" "$OUTPUT/$mode-benchmark.json"
  echo "PASS $mode"
}

IFS=',' read -r -a mode_list <<< "$MODES"
for mode in "${mode_list[@]}"; do
  case "$mode" in off|browser|rust) ;; *) echo "Unsupported mode $mode" >&2; exit 2 ;; esac
  run_mode "$mode"
done
python3 "$ROOT/perf-suite/desktop/summarize_experiment.py" "$OUTPUT"
echo "Artifacts: $OUTPUT"
