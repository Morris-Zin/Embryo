#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/offload/run_report_stress.sh --run-id ID [--duration 3m] [--vouchers-per-5min 500] [--report-from DATE] [--report-to DATE] [--scenario report_stress]

Runs the existing k6 report_stress scenario against isolated khitmee-perf with
six shop workers and two report managers. When no report dates are passed, the
seed manifest's full-year report window is used. The administrator password is
read from perf-suite/docker/.env without printing it.
EOF_USAGE
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
RUN_ID=""
DURATION="3m"
VOUCHERS_PER_5MIN="500"
REPORT_FROM=""
REPORT_TO=""
SCENARIO="report_stress"

while (($#)); do
  case "$1" in
    --run-id) RUN_ID="${2:?missing --run-id value}"; shift 2 ;;
    --run-id=*) RUN_ID="${1#--run-id=}"; shift ;;
    --duration) DURATION="${2:?missing --duration value}"; shift 2 ;;
    --duration=*) DURATION="${1#--duration=}"; shift ;;
    --vouchers-per-5min) VOUCHERS_PER_5MIN="${2:?missing --vouchers-per-5min value}"; shift 2 ;;
    --vouchers-per-5min=*) VOUCHERS_PER_5MIN="${1#--vouchers-per-5min=}"; shift ;;
    --report-from) REPORT_FROM="${2:?missing --report-from value}"; shift 2 ;;
    --report-from=*) REPORT_FROM="${1#--report-from=}"; shift ;;
    --report-to) REPORT_TO="${2:?missing --report-to value}"; shift 2 ;;
    --report-to=*) REPORT_TO="${1#--report-to=}"; shift ;;
    --scenario) SCENARIO="${2:?missing --scenario value}"; shift 2 ;;
    --scenario=*) SCENARIO="${1#--scenario=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$RUN_ID" ]]; then
  echo "--run-id is required" >&2
  exit 2
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Invalid --run-id; use letters, numbers, dot, underscore or dash" >&2
  exit 2
fi
if [[ -f "$REPO_ROOT/perf-suite/docker/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/perf-suite/docker/.env"
  set +a
fi
PERF_PASSWORD="${PERF_PASSWORD:-${ADMIN_PASSWORD:-}}"
if [[ -z "$PERF_PASSWORD" ]]; then
  echo "PERF_PASSWORD or ADMIN_PASSWORD is required in the environment or perf-suite/docker/.env" >&2
  exit 2
fi

extra_env=()
if [[ -n "$REPORT_FROM" ]]; then
  extra_env+=(REPORT_FROM_DATE="$REPORT_FROM")
fi
if [[ -n "$REPORT_TO" ]]; then
  extra_env+=(REPORT_TO_DATE="$REPORT_TO")
fi

env \
  RUN_ID="$RUN_ID" \
  DURATION="$DURATION" \
  SHOP_VUS=6 \
  MANAGER_VUS=2 \
  VOUCHERS_PER_5MIN="$VOUCHERS_PER_5MIN" \
  REPORT_INTERVAL_SECONDS=12 \
  TARGET_URL="http://127.0.0.1:18086" \
  COMPOSE_PROJECT_NAME="khitmee-perf" \
  PERF_MODE="khitmee_ops" \
  MODE="khitmee_ops" \
  SIMULATED_PING_MS=50 \
  K6_DOCKER_USER="0:0" \
  MEMORY_BUDGET_GB=2 \
  PERF_PASSWORD="$PERF_PASSWORD" \
  MSYS_NO_PATHCONV=1 \
  MSYS2_ARG_CONV_EXCL='*' \
  "${extra_env[@]}" \
  bash "$REPO_ROOT/perf-suite/k6/run_k6.sh" "$SCENARIO"
