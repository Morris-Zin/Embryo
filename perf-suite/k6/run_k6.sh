#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PERF_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RESULT_DIR="$PERF_ROOT/results"
mkdir -p "$RESULT_DIR"

SCENARIO="${1:-${SCENARIO:-june_sustained}}"
if [[ ! "$SCENARIO" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "Invalid scenario name '$SCENARIO'. Use a file name without path separators." >&2
  exit 2
fi
SCENARIO_FILE="$SCRIPT_DIR/scenarios/$SCENARIO.js"
if [[ ! -f "$SCENARIO_FILE" ]]; then
  echo "Scenario not found: $SCENARIO_FILE" >&2
  exit 2
fi

TARGET_URL="${TARGET_URL:-http://127.0.0.1:18086}"
if [[ ! "$TARGET_URL" =~ ^https?://([^/:?#]+)(:([0-9]+))? ]]; then
  echo "TARGET_URL must be an http(s) URL, got '$TARGET_URL'" >&2
  exit 2
fi
TARGET_HOST="${BASH_REMATCH[1],,}"
TARGET_PORT="${BASH_REMATCH[3]:-}"
if [[ -z "$TARGET_PORT" ]]; then
  if [[ "${TARGET_URL,,}" == https://* ]]; then
    TARGET_PORT="443"
  else
    TARGET_PORT="80"
  fi
fi

truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ "$TARGET_PORT" == "18085" || "$TARGET_HOST" == "khitmee.localhost" || "$TARGET_HOST" == *.khitmee.localhost ]]; then
  echo "Refusing to run against the live khitmee-erpnext development stack at $TARGET_URL." >&2
  echo "Use the isolated khitmee-perf stack, normally http://127.0.0.1:18086. There is no override." >&2
  exit 3
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=python3
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN=python
  else
    echo "python3 or python is required for evaluate_run.py" >&2
    exit 2
  fi
fi

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$SCENARIO}"
RUN_ID="${RUN_ID//[^A-Za-z0-9_.-]/-}"
export RUN_ID
K6_SUMMARY="$RESULT_DIR/${RUN_ID}-k6-summary.json"
K6_LOG="$RESULT_DIR/${RUN_ID}-k6.log"
STATS_CSV="$RESULT_DIR/${RUN_ID}-container-stats.csv"
STATS_LOG="$RESULT_DIR/${RUN_ID}-stats-collector.log"
SCORECARD="$RESULT_DIR/${RUN_ID}-scorecard.md"
SEASONAL_VERIFICATION_JSON="$RESULT_DIR/${RUN_ID}-seasonal-verification.json"
ATTRIBUTION_DIR="$RESULT_DIR/${RUN_ID}-attribution"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-khitmee-perf}"
MEMORY_BUDGET_GB="${MEMORY_BUDGET_GB:-}"
STATS_COLLECTOR="${STATS_COLLECTOR:-$PERF_ROOT/scripts/stats-collector.sh}"
STATS_STARTED=0
COLLECTOR_START_PID=""

start_stats_collector() {
  if [[ ! -x "$STATS_COLLECTOR" ]]; then
    if truthy "${ALLOW_MISSING_STATS_COLLECTOR:-0}"; then
      echo "WARNING: stats collector is missing or not executable at $STATS_COLLECTOR; memory evidence will be incomplete." | tee "$STATS_LOG"
      return 0
    fi
    echo "Missing executable stats collector: $STATS_COLLECTOR" >&2
    exit 4
  fi

  echo "Starting container stats collector"
  if "$STATS_COLLECTOR" --help 2>&1 | grep -q -- '--output'; then
    COLLECTOR_MODE=foreground
    env       RUN_ID="$RUN_ID"       STATS_OUTPUT="$STATS_CSV"       COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME"       TARGET_URL="$TARGET_URL"       MEMORY_BUDGET_GB="$MEMORY_BUDGET_GB"       "$STATS_COLLECTOR" --output "$STATS_CSV" --interval "${STATS_INTERVAL_SECONDS:-2}" >"$STATS_LOG" 2>&1 &
  else
    COLLECTOR_MODE=subcommand
    env       RUN_ID="$RUN_ID"       STATS_OUTPUT="$STATS_CSV"       COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME"       TARGET_URL="$TARGET_URL"       MEMORY_BUDGET_GB="$MEMORY_BUDGET_GB"       "$STATS_COLLECTOR" start "$STATS_CSV" >"$STATS_LOG" 2>&1 &
  fi
  COLLECTOR_START_PID=$!
  sleep "${STATS_STARTUP_SECONDS:-1}"
  if kill -0 "$COLLECTOR_START_PID" 2>/dev/null; then
    STATS_STARTED=1
    return 0
  fi
  start_status=0
  wait "$COLLECTOR_START_PID" || start_status=$?
  if [[ "$start_status" != "0" ]]; then
    echo "Stats collector failed to start; see $STATS_LOG" >&2
    sed -n '1,120p' "$STATS_LOG" >&2 || true
    exit 4
  fi
  STATS_STARTED=1
}

stop_stats_collector() {
  if [[ "$STATS_STARTED" != "1" ]]; then
    return 0
  fi
  echo "Stopping container stats collector"
  if [[ "${COLLECTOR_MODE:-}" == "foreground" ]]; then
    if [[ -n "$COLLECTOR_START_PID" ]] && kill -0 "$COLLECTOR_START_PID" 2>/dev/null; then
      kill -INT "$COLLECTOR_START_PID" 2>/dev/null || kill "$COLLECTOR_START_PID" 2>/dev/null || true
      for _ in 1 2 3 4 5; do
        kill -0 "$COLLECTOR_START_PID" 2>/dev/null || break
        sleep 1
      done
      kill -TERM "$COLLECTOR_START_PID" 2>/dev/null || true
    fi
  elif [[ -x "$STATS_COLLECTOR" ]]; then
    if ! env       RUN_ID="$RUN_ID"       STATS_OUTPUT="$STATS_CSV"       COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME"       TARGET_URL="$TARGET_URL"       MEMORY_BUDGET_GB="$MEMORY_BUDGET_GB"       "$STATS_COLLECTOR" stop "$STATS_CSV" >>"$STATS_LOG" 2>&1; then
      echo "WARNING: stats collector did not stop cleanly; see $STATS_LOG" >&2
    fi
    if [[ -n "$COLLECTOR_START_PID" ]] && kill -0 "$COLLECTOR_START_PID" 2>/dev/null; then
      kill "$COLLECTOR_START_PID" 2>/dev/null || true
    fi
  fi
  STATS_STARTED=0
}

cleanup() {
  trap - EXIT
  stop_stats_collector || true
}
trap cleanup EXIT

docker_available() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

K6_DOCKER_HOST="${K6_DOCKER_HOST:-}"
k6_docker() {
  if [[ -n "$K6_DOCKER_HOST" ]]; then
    DOCKER_HOST="$K6_DOCKER_HOST" docker "$@"
  else
    docker "$@"
  fi
}

host_k6_available() {
  command -v k6 >/dev/null 2>&1
}

select_network() {
  if [[ -n "${K6_NETWORK:-}" ]]; then
    printf '%s\n' "$K6_NETWORK"
    return 0
  fi
  local candidate
  for candidate in khitmee-perf khitmee-perf_default; do
    if k6_docker network inspect "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

K6_RUNTIME="${K6_RUNTIME:-auto}"
USE_DOCKER=0
if [[ "$K6_RUNTIME" == "docker" ]]; then
  if ! docker_available; then
    echo "K6_RUNTIME=docker but Docker is unavailable or the daemon is not running." >&2
    exit 2
  fi
  USE_DOCKER=1
elif [[ "$K6_RUNTIME" == "host" ]]; then
  if ! host_k6_available; then
    echo "K6_RUNTIME=host but no k6 binary is on PATH." >&2
    exit 2
  fi
elif docker_available; then
  USE_DOCKER=1
elif ! host_k6_available; then
  echo "Neither Docker nor a host k6 binary is available." >&2
  exit 2
fi

MANIFEST_STAGING="$RESULT_DIR/${RUN_ID}-seed-manifest.json"
CONTAINER_MANIFEST_PATH="/perf-suite/results/$(basename "$MANIFEST_STAGING")"
SEED_MANIFEST_EFFECTIVE=""
rm -f "$MANIFEST_STAGING"

refresh_seed_manifest() {
  if [[ -n "${SEED_MANIFEST_PATH:-}" ]]; then
    if [[ -f "$SEED_MANIFEST_PATH" && -s "$SEED_MANIFEST_PATH" ]]; then
      cp "$SEED_MANIFEST_PATH" "$MANIFEST_STAGING"
      echo "Using caller-provided seed manifest: $SEED_MANIFEST_PATH"
      return 0
    fi
    echo "WARNING: SEED_MANIFEST_PATH is set but not a readable non-empty file ($SEED_MANIFEST_PATH); refreshing from the backend container instead." >&2
  fi
  if docker_available; then
    local container="${SEED_MANIFEST_CONTAINER:-${COMPOSE_PROJECT_NAME}-backend-1}"
    local remote
    for remote in \
      /tmp/khitmee-seeder/seed_manifest.json \
      /home/frappe/frappe-bench/sites/bench-loadtest.localhost/private/files/seed_manifest.json; do
      if docker cp "$container:$remote" "$MANIFEST_STAGING" >/dev/null 2>&1; then
        echo "Refreshed seed manifest from $container:$remote"
        return 0
      fi
    done
  fi
  local candidate
  for candidate in "$PERF_ROOT/seed_manifest.json" "$PERF_ROOT/seeder/seed_manifest.json" "$SCRIPT_DIR/seed_manifest.json"; do
    if [[ -f "$candidate" ]]; then
      cp "$candidate" "$MANIFEST_STAGING"
      echo "WARNING: using possibly stale host seed manifest at $candidate; the backend container copy is authoritative." >&2
      return 0
    fi
  done
  echo "WARNING: no seed_manifest.json could be refreshed; k6 will report the paths it tried." >&2
  return 1
}

if refresh_seed_manifest; then
  SEED_MANIFEST_EFFECTIVE="$MANIFEST_STAGING"
fi
rm -f "$STATS_CSV" "$K6_SUMMARY" "$K6_LOG" "$STATS_LOG" "$SCORECARD"
start_stats_collector

if [[ "$SCENARIO" == "seasonal" || "$SCENARIO" == "seasonal_preflight" ]]; then
  python3 -B "$PERF_ROOT/seasonal/attribution.py" start --artifact-dir "$ATTRIBUTION_DIR" --docker-dir "$PERF_ROOT/docker" >"$RESULT_DIR/${RUN_ID}-attribution-start.log" 2>&1 || echo "WARNING: attribution start collection failed" >&2
fi

K6_STATUS=0
if [[ "$USE_DOCKER" == "1" ]]; then
  NETWORK=""
  if ! NETWORK="$(select_network)"; then
    echo "Docker network khitmee-perf or khitmee-perf_default was not found." >&2
    echo "Start the isolated benchmark stack first, set K6_NETWORK, or use K6_RUNTIME=host." >&2
    exit 5
  fi
  K6_IMAGE="${K6_IMAGE:-grafana/k6:latest}"
  DOCKER_TARGET_URL="$TARGET_URL"
  if [[ "$TARGET_HOST" == "127.0.0.1" || "$TARGET_HOST" == "localhost" || "$TARGET_HOST" == "::1" ]]; then
    # The k6 container shares the khitmee-perf network, so it can reach the
    # frontend service directly; loopback only makes sense for host-mode k6.
    DOCKER_TARGET_URL="http://frontend:8080"
  fi
  EFFECTIVE_HOST_HEADER="${HOST_HEADER:-${SITE_NAME:-bench-loadtest.localhost}}"

  CONTAINER_SUMMARY="/perf-suite/results/$(basename "$K6_SUMMARY")"
  DOCKER_CMD=(
    run --rm
    --user "${K6_DOCKER_USER:-$(id -u):$(id -g)}"
    --network "$NETWORK"
    --add-host host.docker.internal:host-gateway
    -v "$PERF_ROOT:/perf-suite"
    -w /perf-suite/k6
    -e "TARGET_URL=$DOCKER_TARGET_URL"
    -e "HOST_HEADER=$EFFECTIVE_HOST_HEADER"
  )
  if [[ -n "$SEED_MANIFEST_EFFECTIVE" ]]; then
    DOCKER_CMD+=( -e "SEED_MANIFEST_PATH=$CONTAINER_MANIFEST_PATH" )
  fi
  PASSTHROUGH_VARS=(
    SITE_NAME MODE PERF_MODE SIMULATED_PING_MS DURATION SHOP_VUS MANAGER_VUS RUN_ID
    VOUCHERS_PER_5MIN REPORT_INTERVAL_SECONDS REPORT_NAME REPORT_LOOKBACK_DAYS REPORT_FROM_DATE REPORT_TO_DATE REPORT_GROUP_BY
    EXPORT_FROM_DATE
    PERF_USER PERF_PASSWORD FRAPPE_USER FRAPPE_PASSWORD WAREHOUSE_SEQUENCE PRICE_LIST CUSTOMER COMPANY
    PAYMENT_MODE PRINTER_TYPE TRANSFER_MODE HOT_SKU_POOL ZIPF_EXPONENT MIN_LINES MAX_LINES STOCK_LOOKUP_RATIO
    DIST_TRANSFER_RATIO RETAIL_SALE_RATIO RUN_DOC_METHOD_ENDPOINT CURRENCY
    SEASONAL_PROFILE SEASONAL_SEED SEASONAL_BASELINE_DURATION SEASONAL_MARGIN_DURATION
    SEASONAL_OVERLOAD_STEP_DURATION SEASONAL_RECOVERY_DURATION SEASONAL_OVERLOAD_MULTIPLIERS
    SEASONAL_MAX_RETRIES SEASONAL_RETRY_BACKOFF_MS SEASONAL_DETAIL_TIMEOUT_SECONDS SEASONAL_DETAIL_PAGE_COUNT
    SEASONAL_VOUCHER_PREALLOCATED_VUS SEASONAL_VOUCHER_MAX_VUS
    SEASONAL_SEARCH_PREALLOCATED_VUS SEASONAL_SEARCH_MAX_VUS
    SEASONAL_ENABLE_MANAGERS SEASONAL_ENABLE_EXPORT SEASONAL_EXPORT_TIMEOUT_SECONDS SEASONAL_EXPORT_POLL_SECONDS
    SEASONAL_EXPORT_ENQUEUE_METHOD SEASONAL_EXPORT_DOWNLOAD_METHOD SEASONAL_SUMMARY_REPORT SEASONAL_DETAIL_REPORT
    SEASONAL_REQUIRE_RETRY_EVIDENCE
    SEASONAL_MANAGER_WAREHOUSE_USER SEASONAL_MANAGER_WAREHOUSE_PASSWORD
    SEASONAL_MANAGER_MONTHLY_USER SEASONAL_MANAGER_MONTHLY_PASSWORD
    SEASONAL_MANAGER_DETAIL_USER SEASONAL_MANAGER_DETAIL_PASSWORD
    SEASONAL_MANAGER_EXPORT_USER SEASONAL_MANAGER_EXPORT_PASSWORD
  )
  for var_name in "${PASSTHROUGH_VARS[@]}"; do
    DOCKER_CMD+=( -e "$var_name" )
  done
  DOCKER_CMD+=(
    "$K6_IMAGE" run
    --summary-export "$CONTAINER_SUMMARY"
    --tag "runid=$RUN_ID"
    --tag "scenario=$SCENARIO"
    "scenarios/$SCENARIO.js"
  )

  echo "Running k6 scenario '$SCENARIO' in Docker image $K6_IMAGE on network $NETWORK"
  set +e
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' k6_docker "${DOCKER_CMD[@]}" 2>&1 | tee "$K6_LOG"
  K6_STATUS=${PIPESTATUS[0]}
  set -e
else
  echo "Running k6 scenario '$SCENARIO' with host k6"
  HOST_K6_ENV=()
  if [[ -n "$SEED_MANIFEST_EFFECTIVE" ]]; then
    HOST_K6_ENV+=( "SEED_MANIFEST_PATH=$SEED_MANIFEST_EFFECTIVE" )
  fi
  set +e
  env TARGET_URL="$TARGET_URL" "${HOST_K6_ENV[@]}" k6 run \
    --summary-export "$K6_SUMMARY" \
    --tag "runid=$RUN_ID" \
    --tag "scenario=$SCENARIO" \
    "$SCENARIO_FILE" 2>&1 | tee "$K6_LOG"
  K6_STATUS=${PIPESTATUS[0]}
  set -e
fi

sleep "${STATS_FLUSH_SECONDS:-1}"
stop_stats_collector

if [[ "$SCENARIO" == "seasonal" || "$SCENARIO" == "seasonal_preflight" ]]; then
  python3 -B "$PERF_ROOT/seasonal/attribution.py" finish --artifact-dir "$ATTRIBUTION_DIR" --docker-dir "$PERF_ROOT/docker" >"$RESULT_DIR/${RUN_ID}-attribution-finish.log" 2>&1 || echo "WARNING: attribution finish collection failed" >&2
fi

EVAL_STATUS=0
if [[ -f "$K6_SUMMARY" ]]; then
  if [[ "$SCENARIO" == "seasonal" ]]; then
    rm -f "$SEASONAL_VERIFICATION_JSON"
    if [[ "${SEASONAL_VERIFY:-1}" != "0" ]]; then
      echo "Running independent seasonal database verifier"
      set +e
      "$PYTHON_BIN" "$PERF_ROOT/seasonal/verify_run.py" \
        --k6-log "$K6_LOG" \
        --output "$SEASONAL_VERIFICATION_JSON" \
        --docker-dir "$PERF_ROOT/docker" \
        --compose-project "$COMPOSE_PROJECT_NAME" \
        --site-name "${SITE_NAME:-bench-loadtest.localhost}"
      VERIFY_STATUS=$?
      set -e
      if [[ "$VERIFY_STATUS" != "0" && ! -f "$SEASONAL_VERIFICATION_JSON" ]]; then
        printf '%s\n' '{"passed":false,"defects":[{"kind":"verifier_error","detail":"verify_run.py exited before writing JSON"}]}' > "$SEASONAL_VERIFICATION_JSON"
      fi
    else
      printf '%s\n' '{"passed":false,"defects":[{"kind":"verifier_disabled","detail":"SEASONAL_VERIFY=0; independent database evidence intentionally missing"}]}' > "$SEASONAL_VERIFICATION_JSON"
    fi
    EVAL_ARGS=(
      "$SCRIPT_DIR/evaluate_seasonal.py"
      --k6-summary "$K6_SUMMARY"
      --k6-log "$K6_LOG"
      --stats-csv "$STATS_CSV"
      --verification "$SEASONAL_VERIFICATION_JSON"
      --attribution "$ATTRIBUTION_DIR/attribution-summary.json"
      --output "$SCORECARD"
      --run-label "$RUN_ID"
    )
    if [[ "${SEASONAL_ENABLE_MANAGERS:-1}" == "0" ]]; then
      EVAL_ARGS+=(--cashier-only)
    fi
  elif [[ "$SCENARIO" == "seasonal_preflight" ]]; then
    EVAL_ARGS=(
      "$PERF_ROOT/seasonal/evaluate_preflight.py"
      --k6-log "$K6_LOG"
      --output "$SCORECARD"
      --json-output "$RESULT_DIR/${RUN_ID}-preflight-result.json"
    )
  else
    EVAL_ARGS=(
      "$SCRIPT_DIR/evaluate_run.py"
      --k6-summary "$K6_SUMMARY"
      --stats-csv "$STATS_CSV"
      --output "$SCORECARD"
      --run-label "$RUN_ID"
      --strict
    )
    if [[ -n "$MEMORY_BUDGET_GB" ]]; then
      EVAL_ARGS+=(--memory-budget-gb "$MEMORY_BUDGET_GB")
    fi
    if [[ "$SCENARIO" != "report_stress" ]]; then
      EVAL_ARGS+=(--allow-scenario-skips)
    fi
  fi
  set +e
  "$PYTHON_BIN" "${EVAL_ARGS[@]}"
  EVAL_STATUS=$?
  set -e
else
  echo "k6 summary was not created at $K6_SUMMARY; scorecard cannot be generated." >&2
  EVAL_STATUS=6
fi

echo "k6_exit=$K6_STATUS evaluate_exit=$EVAL_STATUS"
echo "k6 log: $K6_LOG"
echo "container stats: $STATS_CSV"
if [[ -f "$SEASONAL_VERIFICATION_JSON" && "$SCENARIO" == "seasonal" ]]; then
  echo "seasonal verification: $SEASONAL_VERIFICATION_JSON"
fi
if [[ -f "$SCORECARD" ]]; then
  echo "scorecard: $SCORECARD"
fi

if [[ "$K6_STATUS" != "0" ]]; then
  exit "$K6_STATUS"
fi
if [[ "$EVAL_STATUS" != "0" ]]; then
  exit "$EVAL_STATUS"
fi
exit 0
