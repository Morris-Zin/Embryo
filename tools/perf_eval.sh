#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="khitmee-perf"
FORBIDDEN_PROJECT="khitmee-erpnext"
REQUESTED_TARGET_URL="${TARGET_URL:-}"
TARGET_URL="http://127.0.0.1:18086"

usage() {
  cat <<'EOF_USAGE'
Usage: bash tools/perf_eval.sh --pass-id <id> --profile <4gb|2gb> --label "<desc>" [options]

Runs one standardized isolated khitmee-perf evaluation pass and records it in
perf-suite/results/eval-history.json plus OVERNIGHT-EVAL-LEDGER.md.

Options:
  --pass-id <id>          Stable pass identifier, for example pass-1-sql-indexes
  --profile <4gb|2gb>     Memory profile for khitmee-perf
  --label "<desc>"        Human description for ledger tables
  --duration <dur>        k6 duration for sustained, burst, and 30d report runs (default 3m)
  --daily-duration <dur>  k6 duration for the 1d report run (default 2m)
  --skip-switch[=bool]    Do not run perf-suite/scripts/up.sh
  --skip-profile[=bool]   Do not run tools/perf_profile.py
  --skip-bench[=bool]     Do not run the four k6 scenarios
  --parity-pass[=bool]    Record whether the parity proof passed (bare flag means true)
  --require-parity[=bool] Exit nonzero unless parity_pass is true
  --no-overwrite[=bool]   Refuse to remove stale artifacts for this run prefix
  --replenish-stock[=bool]
                          Apply perf-suite/seeder/replenish_stock.py before k6 (default false)
  --require-stock-preflight[=bool]
                          Run replenish_stock.py --check-only before k6 and abort unless stock is sufficient (default false)
  --notes <text>          Additional notes to record in the eval ledger
  --strict-clean-state[=bool]
                          Require absolute zero post-pass restarts and OOM kills
  --dry-run[=bool]        Validate and print the normalized configuration without Docker or benchmarks
  -h, --help              Show this help

The project is hardcoded to khitmee-perf. The live khitmee-erpnext stack is never targeted.
EOF_USAGE
}

parse_bool01() {
  case "${1,,}" in
    1|true|yes|y|on) printf '%s\n' 1 ;;
    0|false|no|n|off) printf '%s\n' 0 ;;
    *) return 2 ;;
  esac
}

parse_bool_word() {
  local parsed
  parsed="$(parse_bool01 "$1")" || return 2
  if [[ "$parsed" == "1" ]]; then
    printf '%s\n' true
  else
    printf '%s\n' false
  fi
}

parse_optional_bool() {
  local value="$1" parsed
  if ! parsed="$(parse_bool01 "$value" 2>/dev/null)"; then
    return 2
  fi
  printf '%s\n' "$parsed"
}

normalize_run_prefix() {
  local pass_id="$1" profile="$2"
  if [[ "$pass_id" == *"-$profile" ]]; then
    printf '%s\n' "$pass_id"
  else
    printf '%s\n' "$pass_id-$profile"
  fi
}

PASS_ID=""
PROFILE=""
LABEL=""
DURATION="3m"
DAILY_DURATION="2m"
SKIP_SWITCH=0
SKIP_PROFILE=0
SKIP_BENCH=0
STRICT_CLEAN_STATE=0
DRY_RUN=0
PARITY_PASS=""
REQUIRE_PARITY=1
NO_OVERWRITE=0
REPLENISH_STOCK=0
REQUIRE_STOCK_PREFLIGHT=0
USER_NOTES=""

parse_cli() {
  PASS_ID=""
  PROFILE=""
  LABEL=""
  DURATION="3m"
  DAILY_DURATION="2m"
  SKIP_SWITCH=0
  SKIP_PROFILE=0
  SKIP_BENCH=0
  STRICT_CLEAN_STATE=0
  DRY_RUN=0
  PARITY_PASS=""
  REQUIRE_PARITY=1
  NO_OVERWRITE=0
  REPLENISH_STOCK=0
  REQUIRE_STOCK_PREFLIGHT=0
  USER_NOTES=""

  local parsed
  while (($#)); do
    case "$1" in
      --pass-id)
        [[ $# -ge 2 ]] || { echo "Missing value for --pass-id" >&2; exit 2; }
        PASS_ID="$2"; shift 2 ;;
      --pass-id=*) PASS_ID="${1#--pass-id=}"; shift ;;
      --profile)
        [[ $# -ge 2 ]] || { echo "Missing value for --profile" >&2; exit 2; }
        PROFILE="$2"; shift 2 ;;
      --profile=*) PROFILE="${1#--profile=}"; shift ;;
      --label)
        [[ $# -ge 2 ]] || { echo "Missing value for --label" >&2; exit 2; }
        LABEL="$2"; shift 2 ;;
      --label=*) LABEL="${1#--label=}"; shift ;;
      --duration)
        [[ $# -ge 2 ]] || { echo "Missing value for --duration" >&2; exit 2; }
        DURATION="$2"; shift 2 ;;
      --duration=*) DURATION="${1#--duration=}"; shift ;;
      --daily-duration)
        [[ $# -ge 2 ]] || { echo "Missing value for --daily-duration" >&2; exit 2; }
        DAILY_DURATION="$2"; shift 2 ;;
      --daily-duration=*) DAILY_DURATION="${1#--daily-duration=}"; shift ;;
      --skip-switch)
        if [[ $# -ge 2 ]] && parsed="$(parse_optional_bool "$2" 2>/dev/null)"; then
          SKIP_SWITCH="$parsed"; shift 2
        else
          SKIP_SWITCH=1; shift
        fi ;;
      --skip-switch=*)
        if ! SKIP_SWITCH="$(parse_optional_bool "${1#--skip-switch=}")"; then
          echo "Invalid value for --skip-switch; expected true or false" >&2; exit 2
        fi
        shift ;;
      --skip-profile)
        if [[ $# -ge 2 ]] && parsed="$(parse_optional_bool "$2" 2>/dev/null)"; then
          SKIP_PROFILE="$parsed"; shift 2
        else
          SKIP_PROFILE=1; shift
        fi ;;
      --skip-profile=*)
        if ! SKIP_PROFILE="$(parse_optional_bool "${1#--skip-profile=}")"; then
          echo "Invalid value for --skip-profile; expected true or false" >&2; exit 2
        fi
        shift ;;
      --skip-bench)
        if [[ $# -ge 2 ]] && parsed="$(parse_optional_bool "$2" 2>/dev/null)"; then
          SKIP_BENCH="$parsed"; shift 2
        else
          SKIP_BENCH=1; shift
        fi ;;
      --skip-bench=*)
        if ! SKIP_BENCH="$(parse_optional_bool "${1#--skip-bench=}")"; then
          echo "Invalid value for --skip-bench; expected true or false" >&2; exit 2
        fi
        shift ;;
      --strict-clean-state)
        if [[ $# -ge 2 ]] && parsed="$(parse_optional_bool "$2" 2>/dev/null)"; then
          STRICT_CLEAN_STATE="$parsed"; shift 2
        else
          STRICT_CLEAN_STATE=1; shift
        fi ;;
      --strict-clean-state=*)
        if ! STRICT_CLEAN_STATE="$(parse_optional_bool "${1#--strict-clean-state=}")"; then
          echo "Invalid value for --strict-clean-state; expected true or false" >&2; exit 2
        fi
        shift ;;
      --dry-run)
        if [[ $# -ge 2 ]] && parsed="$(parse_optional_bool "$2" 2>/dev/null)"; then
          DRY_RUN="$parsed"; shift 2
        else
          DRY_RUN=1; shift
        fi ;;
      --dry-run=*)
        if ! DRY_RUN="$(parse_optional_bool "${1#--dry-run=}")"; then
          echo "Invalid value for --dry-run; expected true or false" >&2; exit 2
        fi
        shift ;;
      --parity-pass)
        if [[ $# -ge 2 ]] && parsed="$(parse_bool_word "$2" 2>/dev/null)"; then
          PARITY_PASS="$parsed"; shift 2
        else
          PARITY_PASS="true"; shift
        fi ;;
      --parity-pass=*)
        if ! PARITY_PASS="$(parse_bool_word "${1#--parity-pass=}")"; then
          echo "Invalid value for --parity-pass; expected true or false" >&2; exit 2
        fi
        shift ;;
      --require-parity)
        if [[ $# -ge 2 ]] && parsed="$(parse_optional_bool "$2" 2>/dev/null)"; then
          REQUIRE_PARITY="$parsed"; shift 2
        else
          REQUIRE_PARITY=1; shift
        fi ;;
      --require-parity=*)
        if ! REQUIRE_PARITY="$(parse_optional_bool "${1#--require-parity=}")"; then
          echo "Invalid value for --require-parity; expected true or false" >&2; exit 2
        fi
        shift ;;
      --no-overwrite)
        if [[ $# -ge 2 ]] && parsed="$(parse_optional_bool "$2" 2>/dev/null)"; then
          NO_OVERWRITE="$parsed"; shift 2
        else
          NO_OVERWRITE=1; shift
        fi ;;
      --no-overwrite=*)
        if ! NO_OVERWRITE="$(parse_optional_bool "${1#--no-overwrite=}")"; then
          echo "Invalid value for --no-overwrite; expected true or false" >&2; exit 2
        fi
        shift ;;
      --replenish-stock)
        if [[ $# -ge 2 ]] && parsed="$(parse_optional_bool "$2" 2>/dev/null)"; then
          REPLENISH_STOCK="$parsed"; shift 2
        else
          REPLENISH_STOCK=1; shift
        fi ;;
      --replenish-stock=*)
        if ! REPLENISH_STOCK="$(parse_optional_bool "${1#--replenish-stock=}")"; then
          echo "Invalid value for --replenish-stock; expected true or false" >&2; exit 2
        fi
        shift ;;
      --require-stock-preflight)
        if [[ $# -ge 2 ]] && parsed="$(parse_optional_bool "$2" 2>/dev/null)"; then
          REQUIRE_STOCK_PREFLIGHT="$parsed"; shift 2
        else
          REQUIRE_STOCK_PREFLIGHT=1; shift
        fi ;;
      --require-stock-preflight=*)
        if ! REQUIRE_STOCK_PREFLIGHT="$(parse_optional_bool "${1#--require-stock-preflight=}")"; then
          echo "Invalid value for --require-stock-preflight; expected true or false" >&2; exit 2
        fi
        shift ;;
      --notes)
        [[ $# -ge 2 ]] || { echo "Missing value for --notes" >&2; exit 2; }
        USER_NOTES="$2"; shift 2 ;;
      --notes=*) USER_NOTES="${1#--notes=}"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
  done
}

validate_cli() {
  if [[ -z "$PASS_ID" || -z "$PROFILE" || -z "$LABEL" ]]; then
    echo "--pass-id, --profile and --label are required" >&2
    usage >&2
    exit 2
  fi
  if [[ ! "$PASS_ID" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "Invalid --pass-id '$PASS_ID'; use letters, numbers, dot, underscore or dash" >&2
    exit 2
  fi
  case "$PROFILE" in
    4gb|2gb) ;;
    *) echo "Invalid --profile '$PROFILE'; expected 4gb or 2gb" >&2; exit 2 ;;
  esac
}

validate_targeting() {
  if [[ "$PROJECT_NAME" == "$FORBIDDEN_PROJECT" ]]; then
    echo "Refusing to target $FORBIDDEN_PROJECT" >&2
    exit 2
  fi
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" && "$COMPOSE_PROJECT_NAME" != "$PROJECT_NAME" ]]; then
    echo "Refusing to run: COMPOSE_PROJECT_NAME is '$COMPOSE_PROJECT_NAME', expected '$PROJECT_NAME'" >&2
    exit 2
  fi
  if [[ -n "$REQUESTED_TARGET_URL" && "$REQUESTED_TARGET_URL" != "$TARGET_URL" ]]; then
    echo "Refusing to run: TARGET_URL override '$REQUESTED_TARGET_URL' is not the hardcoded isolated perf target" >&2
    exit 2
  fi
  if [[ "$TARGET_URL" == *"khitmee.localhost"* || "$TARGET_URL" == *":18085"* ]]; then
    echo "Refusing to run near the live khitmee-erpnext target" >&2
    exit 2
  fi
  if [[ "${K6_NETWORK:-}" == *"$FORBIDDEN_PROJECT"* ]]; then
    echo "Refusing to run: K6_NETWORK targets $FORBIDDEN_PROJECT" >&2
    exit 2
  fi
}

python_bin() {
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    printf '%s\n' python3
  elif command -v python >/dev/null 2>&1 && python -c 'import sys' >/dev/null 2>&1; then
    printf '%s\n' python
  else
    echo "python3 or python is required" >&2
    return 2
  fi
}

load_admin_password_from_env_file() {
  "$PYBIN" "$ROOT/tools/perf_env.py" "$ROOT/perf-suite/docker/.env" ADMIN_PASSWORD 2>/dev/null
}

resolve_perf_password() {
  if [[ -z "${PERF_PASSWORD+x}" && -z "${ADMIN_PASSWORD+x}" ]]; then
    local loaded=""
    if loaded="$(load_admin_password_from_env_file)"; then
      ADMIN_PASSWORD="$loaded"
    fi
  fi
  if [[ -z "${PERF_PASSWORD:-}" && -n "${ADMIN_PASSWORD:-}" ]]; then
    PERF_PASSWORD="$ADMIN_PASSWORD"
  fi
  PERF_PASSWORD="${PERF_PASSWORD:-}"
  export PERF_PASSWORD
}

require_perf_password_for_bench() {
  resolve_perf_password
  if [[ "$DRY_RUN" != "1" && "$SKIP_BENCH" == "0" && -z "$PERF_PASSWORD" ]]; then
    echo "PERF_PASSWORD is empty: set PERF_PASSWORD or ADMIN_PASSWORD in the environment, or define ADMIN_PASSWORD in perf-suite/docker/.env before running benchmarks." >&2
    exit 7
  fi
}

live_fingerprint() {
  local ids
  ids="$(docker ps -aq --filter "label=com.docker.compose.project=$FORBIDDEN_PROJECT" 2>/dev/null | tr '\n' ' ' || true)"
  if [[ -z "${ids// /}" ]]; then
    printf '%s\n' "absent"
    return 0
  fi
  # shellcheck disable=SC2086
  docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}}' $ids 2>/dev/null | sort | sha256sum | awk '{print $1}'
}

verify_backend_identity() {
  local backend="khitmee-perf-backend-1"
  local project
  project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$backend" 2>/dev/null || true)"
  if [[ "$project" != "$PROJECT_NAME" ]]; then
    echo "Refusing to use $backend: compose project label is '$project', expected '$PROJECT_NAME'" >&2
    exit 3
  fi
}

snapshot_perf_container_state() {
  local ids id name state oom restarts started_at
  ids="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT_NAME" 2>/dev/null || true)"
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    if read -r name state oom restarts started_at < <(docker inspect -f '{{.Name}} {{.State.Status}} {{.State.OOMKilled}} {{.RestartCount}} {{.State.StartedAt}}' "$id" 2>/dev/null); then
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "${name#/}" "$state" "$oom" "$restarts" "$started_at"
    fi
  done <<< "$ids"
}

valid_started_at() {
  [[ -n "$1" && "$1" != "NA" && "$1" != "unknown" && "$1" != "0001-01-01T00:00:00Z" ]]
}

print_container_snapshot() {
  local title="$1" snapshot="$2" id name state oom restarts started_at
  echo "$title"
  while IFS=$'\t' read -r id name state oom restarts started_at; do
    [[ -n "$name" ]] || continue
    echo "  $name state=$state oom=$oom restarts=$restarts started_at=$started_at"
  done <<< "$snapshot"
}

container_snapshot_recreated() {
  local before="$1" after="$2"
  local id name state oom restarts started_at before_id="" before_started_at=""
  local candidate_id candidate_name candidate_state candidate_oom candidate_restarts candidate_started_at

  [[ -n "$after" ]] || return 1
  if [[ -z "$before" ]]; then
    return 0
  fi
  while IFS=$'\t' read -r id name state oom restarts started_at; do
    [[ -n "$name" ]] || continue
    before_id=""
    before_started_at=""
    while IFS=$'\t' read -r candidate_id candidate_name candidate_state candidate_oom candidate_restarts candidate_started_at; do
      if [[ "$candidate_name" == "$name" ]]; then
        before_id="$candidate_id"
        before_started_at="$candidate_started_at"
        break
      fi
    done <<< "$before"
    if [[ -z "$before_id" || "$before_id" != "$id" ]] || \
       ! valid_started_at "$before_started_at" || ! valid_started_at "$started_at" || \
       [[ "$before_started_at" != "$started_at" ]]; then
      return 0
    fi
  done <<< "$after"
  return 1
}

verify_perf_zero_oom_zero_restarts() {
  local preflight="$1" strict_absolute="$2"
  local strict_reason="${3:-strict clean state was requested or containers were recreated}"
  local post id name state oom restarts started_at
  local pre_id="" pre_name="" pre_state="" pre_oom="" pre_restarts="" pre_started_at=""
  local candidate_id candidate_name candidate_state candidate_oom candidate_restarts candidate_started_at
  local restart_delta=0
  local failures=() notes=()

  post="$(snapshot_perf_container_state)"
  if [[ -z "$post" ]]; then
    echo "No $PROJECT_NAME containers found during post-pass verification" >&2
    return 1
  fi

  echo "Post-pass verification checks: compare each container with the Step 3 preflight snapshot; fail on any RestartCount increase, container replacement, or OOMKilled false->true transition."
  if [[ "$strict_absolute" == "1" ]]; then
    echo "Post-pass verification strict mode: every post-pass container must also have restart_count=0 and oom=false because $strict_reason."
  else
    echo "Post-pass verification non-strict mode: pre-existing restart or OOM state is tolerated only when it does not worsen during this pass."
  fi

  while IFS=$'\t' read -r id name state oom restarts started_at; do
    [[ -n "$name" ]] || continue
    pre_id=""
    pre_name=""
    pre_state=""
    pre_oom=""
    pre_restarts=""
    pre_started_at=""
    while IFS=$'\t' read -r candidate_id candidate_name candidate_state candidate_oom candidate_restarts candidate_started_at; do
      if [[ "$candidate_name" == "$name" ]]; then
        pre_id="$candidate_id"
        pre_name="$candidate_name"
        pre_state="$candidate_state"
        pre_oom="$candidate_oom"
        pre_restarts="$candidate_restarts"
        pre_started_at="$candidate_started_at"
        break
      fi
    done <<< "$preflight"

    if [[ ! "$restarts" =~ ^[0-9]+$ ]]; then
      failures+=("$name has unreadable restart count '$restarts'")
      continue
    fi

    if [[ "$strict_absolute" == "1" && ( "$oom" == "true" || "$restarts" != "0" ) ]]; then
      failures+=("$name strict state violation: status=$state oom=$oom restarts=$restarts")
    fi

    if [[ -z "$pre_name" ]]; then
      notes+=("$name appeared after preflight; treating its post-pass counters as the baseline for delta checks")
      if [[ "$restarts" != "0" ]]; then
        failures+=("$name appeared after preflight with restarts=$restarts")
      fi
      if [[ "$oom" == "true" ]]; then
        failures+=("$name appeared after preflight with oom=true")
      fi
      continue
    fi

    if [[ "$pre_id" != "$id" ]]; then
      failures+=("$name container identity changed during the pass; preflight id=$pre_id post-pass id=$id")
      continue
    fi
    if ! valid_started_at "$pre_started_at" || ! valid_started_at "$started_at"; then
      failures+=("$name container StartedAt telemetry is missing or unparsable; preflight=$pre_started_at post-pass=$started_at")
      continue
    fi
    if [[ "$pre_started_at" != "$started_at" ]]; then
      failures+=("$name container StartedAt changed during the pass; preflight=$pre_started_at post-pass=$started_at")
      continue
    fi

    if [[ "$pre_restarts" =~ ^[0-9]+$ ]]; then
      restart_delta=$((restarts - pre_restarts))
    else
      failures+=("$name has unreadable preflight restart count '$pre_restarts'")
      continue
    fi
    if (( restart_delta > 0 )); then
      failures+=("$name restart count increased by $restart_delta (preflight=$pre_restarts post-pass=$restarts)")
    fi
    if [[ "$pre_oom" == "false" && "$oom" == "true" ]]; then
      failures+=("$name transitioned OOMKilled false->true during this pass")
    fi
    if [[ "$pre_oom" == "true" && "$oom" == "true" ]]; then
      notes+=("$name retains pre-existing oom=true; no new OOM transition was detected")
    fi
    if [[ "$pre_restarts" != "0" && "$restarts" == "$pre_restarts" ]]; then
      notes+=("$name retains pre-existing restarts=$restarts; no new restart was detected")
    fi
  done <<< "$post"

  local note failure
  for note in "${notes[@]}"; do
    echo "Post-pass verification note: $note"
  done
  if ((${#failures[@]})); then
    for failure in "${failures[@]}"; do
      echo "Post-pass verification failed for $PROJECT_NAME: $failure" >&2
    done
    return 1
  fi
  echo "Post-pass verification: 0 new restarts and 0 new OOM kills since the Step 3 preflight snapshot"
}

expected_perf_services() {
  cat <<'EOF_BASE_SERVICES'
backend
db
redis-cache
redis-queue
websocket
scheduler
frontend
EOF_BASE_SERVICES
  if [[ "$PROFILE" == "2gb" ]]; then
    printf '%s\n' queue
  else
    printf '%s\n' queue-short queue-long
  fi
}

find_cgroup_path() {
  local container_id="$1" pid="$2"
  local candidate rel
  if [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 0 )) && [[ -r "/proc/$pid/cgroup" ]]; then
    rel="$(awk -F: '$1 == "0" { print $3 }' "/proc/$pid/cgroup" 2>/dev/null | head -n 1)"
    if [[ -n "$rel" && -f "/sys/fs/cgroup${rel}/memory.events" ]]; then
      printf '%s\n' "/sys/fs/cgroup${rel}"
      return 0
    fi
  fi
  for candidate in \
    "/sys/fs/cgroup/system.slice/docker-$container_id.scope" \
    "/sys/fs/cgroup/docker/$container_id" \
    "/sys/fs/cgroup/system.slice/containerd-$container_id.scope" \
    "/sys/fs/cgroup/containerd/$container_id"; do
    if [[ -f "$candidate/memory.events" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  candidate="$(find /sys/fs/cgroup -maxdepth 5 \( -name "docker-$container_id.scope" -o -name "containerd-$container_id.scope" -o -name "$container_id" \) -type d 2>/dev/null | head -n 1 || true)"
  if [[ -n "$candidate" && -f "$candidate/memory.events" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  return 1
}

cgroup_event_value() {
  local cgroup_path="$1" key="$2"
  if [[ -n "$cgroup_path" && -r "$cgroup_path/memory.events" ]]; then
    awk -v wanted="$key" '
      $1 == wanted { print $2; found=1 }
      END { if (!found) print "NA" }
    ' "$cgroup_path/memory.events" 2>/dev/null || printf 'NA\n'
  else
    printf 'NA\n'
  fi
}

capture_expected_service_state() {
  local service container id state docker_oom restarts started_at pid cgroup_path cgroup_oom_kill cgroup_oom
  local failures=0
  local inspection
  while IFS= read -r service; do
    [[ -n "$service" ]] || continue
    container="$PROJECT_NAME-$service-1"
    inspection="$(docker inspect -f '{{.Id}} {{.State.Status}} {{.State.OOMKilled}} {{.RestartCount}} {{.State.StartedAt}} {{.State.Pid}}' "$container" 2>/dev/null || true)"
    if [[ -z "$inspection" ]]; then
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$service" "$container" "missing" "missing" "NA" "NA" "NA" "NA" "NA"
      failures=1
      continue
    fi
    read -r id state docker_oom restarts started_at pid <<< "$inspection"
    if [[ -z "$id" || -z "$state" || -z "$docker_oom" || -z "$restarts" || -z "$started_at" || -z "$pid" ]]; then
      id="missing"
      state="unknown"
      docker_oom="NA"
      restarts="NA"
      started_at="NA"
      pid="0"
    fi
    if [[ "$id" != "missing" ]]; then
      cgroup_path="$(find_cgroup_path "$id" "$pid" || true)"
    else
      cgroup_path=""
    fi
    cgroup_oom_kill="$(cgroup_event_value "$cgroup_path" oom_kill)"
    cgroup_oom="$(cgroup_event_value "$cgroup_path" oom)"
    if [[ "$state" != "running" ]]; then
      failures=1
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$service" "$container" "$id" "$state" "$docker_oom" "$restarts" "$cgroup_oom_kill" "$cgroup_oom" "$started_at"
  done < <(expected_perf_services)
  return "$failures"
}

state_row_for_service() {
  local snapshot="$1" wanted="$2" row service
  while IFS= read -r row; do
    service="${row%%$'\t'*}"
    if [[ "$service" == "$wanted" ]]; then
      printf '%s\n' "$row"
      return 0
    fi
  done <<< "$snapshot"
  return 1
}

is_nonnegative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

verify_expected_service_safety() {
  local preflight="$1" postflight="$2" strict_absolute="${3:-0}"
  local service pre_row post_row
  local pre_service post_service
  local pre_container pre_id pre_state pre_docker_oom pre_restarts pre_cgroup_oom_kill pre_cgroup_oom pre_started_at
  local post_container post_id post_state post_docker_oom post_restarts post_cgroup_oom_kill post_cgroup_oom post_started_at
  local restart_delta oom_kill_delta oom_delta
  local failures=()

  while IFS= read -r service; do
    [[ -n "$service" ]] || continue
    pre_row="$(state_row_for_service "$preflight" "$service" || true)"
    post_row="$(state_row_for_service "$postflight" "$service" || true)"
    if [[ -z "$post_row" ]]; then
      failures+=("$service expected container is absent after the run")
      continue
    fi
    IFS=$'\t' read -r post_service post_container post_id post_state post_docker_oom post_restarts post_cgroup_oom_kill post_cgroup_oom post_started_at <<< "$post_row"
    if [[ "$post_state" != "running" ]]; then
      failures+=("$service container $post_container is '$post_state', not running")
    fi
    if [[ -z "$pre_row" ]]; then
      failures+=("$service was missing from the pre-run expected-service snapshot")
      continue
    fi
    IFS=$'\t' read -r pre_service pre_container pre_id pre_state pre_docker_oom pre_restarts pre_cgroup_oom_kill pre_cgroup_oom pre_started_at <<< "$pre_row"

    if [[ "$pre_id" != "$post_id" ]]; then
      failures+=("$service container identity changed during the run")
    fi
    if ! valid_started_at "$pre_started_at" || ! valid_started_at "$post_started_at"; then
      failures+=("$service container StartedAt telemetry is missing or unparsable")
    elif [[ "$pre_started_at" != "$post_started_at" ]]; then
      failures+=("$service container StartedAt changed during the run")
    fi
    if ! is_nonnegative_integer "$pre_restarts" || ! is_nonnegative_integer "$post_restarts"; then
      failures+=("$service restart telemetry is missing or unparsable")
    else
      restart_delta=$((post_restarts - pre_restarts))
      if (( restart_delta > 0 )); then
        failures+=("$service restart count increased by $restart_delta")
      fi
      if [[ "$strict_absolute" == "1" && "$post_restarts" != "0" ]]; then
        failures+=("$service strict clean state requires restart count 0, got $post_restarts")
      fi
    fi
    if [[ "$pre_docker_oom" != "true" && "$pre_docker_oom" != "false" ]] || [[ "$post_docker_oom" != "true" && "$post_docker_oom" != "false" ]]; then
      failures+=("$service Docker OOMKilled telemetry is missing or unparsable")
    elif [[ "$pre_docker_oom" == "false" && "$post_docker_oom" == "true" ]]; then
      failures+=("$service Docker OOMKilled transitioned false->true")
    fi
    if [[ "$strict_absolute" == "1" && "$post_docker_oom" == "true" ]]; then
      failures+=("$service strict clean state requires Docker OOMKilled=false")
    fi

    if ! is_nonnegative_integer "$pre_cgroup_oom_kill" || ! is_nonnegative_integer "$post_cgroup_oom_kill"; then
      failures+=("$service cgroup oom_kill telemetry is missing or unparsable")
    else
      oom_kill_delta=$((post_cgroup_oom_kill - pre_cgroup_oom_kill))
      if (( oom_kill_delta > 0 )); then
        failures+=("$service cgroup oom_kill increased by $oom_kill_delta")
      fi
      if [[ "$strict_absolute" == "1" && "$post_cgroup_oom_kill" != "0" ]]; then
        failures+=("$service strict clean state requires cgroup oom_kill 0, got $post_cgroup_oom_kill")
      fi
    fi
    if [[ "$pre_cgroup_oom" != "NA" || "$post_cgroup_oom" != "NA" ]]; then
      if ! is_nonnegative_integer "$pre_cgroup_oom" || ! is_nonnegative_integer "$post_cgroup_oom"; then
        failures+=("$service cgroup oom telemetry is partially missing or unparsable")
      else
        oom_delta=$((post_cgroup_oom - pre_cgroup_oom))
        if (( oom_delta > 0 )); then
          failures+=("$service cgroup oom event count increased by $oom_delta")
        fi
        if [[ "$strict_absolute" == "1" && "$post_cgroup_oom" != "0" ]]; then
          failures+=("$service strict clean state requires cgroup oom event count 0, got $post_cgroup_oom")
        fi
      fi
    fi
  done < <(expected_perf_services)

  if ((${#failures[@]})); then
    local failure
    for failure in "${failures[@]}"; do
      echo "safety failure: $failure" >&2
    done
    return 1
  fi
  echo "post-run safety: every expected service is present and running; 0 new restarts, Docker OOM kills, or cgroup OOM events"
  return 0
}

clean_previous_run_artifacts() {
  local path base
  local existing=()
  shopt -s nullglob
  for path in "$RESULT_DIR/$RUN_PREFIX"-*; do
    [[ -f "$path" ]] || continue
    base="${path##*/}"
    case "$base" in
      *-scorecard.md|*-k6-summary.json|*-summary.json|*-container-stats.csv|*-k6.log|*-stats-collector.log|*-stock-replenish.json|*-stock-preflight.json)
        existing+=("$path") ;;
    esac
  done
  shopt -u nullglob
  if ((${#existing[@]} == 0)); then
    echo "stale artifact cleanup: no preexisting $RUN_PREFIX scorecard/summary/stats/log artifacts"
    return 0
  fi
  if [[ "$NO_OVERWRITE" == "1" ]]; then
    echo "Refusing to overwrite ${#existing[@]} preexisting artifact(s) for $RUN_PREFIX because --no-overwrite was requested:" >&2
    printf '  %s\n' "${existing[@]}" >&2
    return 6
  fi
  if ! rm -f -- "${existing[@]}"; then
    echo "Failed to remove preexisting artifacts for $RUN_PREFIX; aborting before benchmark side effects" >&2
    return 6
  fi
  echo "stale artifact cleanup: removed ${#existing[@]} preexisting $RUN_PREFIX scorecard/summary/stats/log artifact(s)"
}

missing_expected_artifacts() {
  local suffix kind path
  for suffix in sustained burst-2x reports reports-daily; do
    for kind in scorecard.md k6-summary.json container-stats.csv; do
      path="$RESULT_DIR/$RUN_PREFIX-$suffix-$kind"
      if [[ ! -f "$path" ]]; then
        printf '%s\n' "$path"
      fi
    done
  done
}

append_mariadb_effective_config() {
  local output_file="$1" rows row_count status name value
  set +e
  rows="$(docker exec khitmee-perf-db-1 sh -lc "mariadb -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -N -B -e \"SHOW GLOBAL VARIABLES WHERE Variable_name IN ('aria_pagecache_buffer_size','key_buffer_size','innodb_buffer_pool_size')\"" 2>&1)"
  status=$?
  set -e
  row_count=0
  if [[ "$status" == "0" ]]; then
    row_count="$(printf '%s\n' "$rows" | awk 'NF >= 2 { count++ } END { print count + 0 }')"
  fi
  if [[ "$status" != "0" || "$row_count" != "3" ]]; then
    echo "MariaDB effective-config verification failed; expected aria_pagecache_buffer_size, key_buffer_size, and innodb_buffer_pool_size" >&2
    if [[ -n "$rows" ]]; then
      printf '%s\n' "$rows" >&2
    fi
    exit 3
  fi
  local seen_aria=0 seen_key_buffer=0 seen_innodb=0
  while IFS=$'\t' read -r name value; do
    [[ -n "$name" ]] || continue
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
      echo "MariaDB effective-config verification failed: $name has unparsable value '$value'" >&2
      exit 3
    fi
    case "$name" in
      aria_pagecache_buffer_size) seen_aria=1 ;;
      key_buffer_size) seen_key_buffer=1 ;;
      innodb_buffer_pool_size) seen_innodb=1 ;;
    esac
  done <<< "$rows"
  if [[ "$seen_aria" != "1" || "$seen_key_buffer" != "1" || "$seen_innodb" != "1" ]]; then
    echo "MariaDB effective-config verification failed; missing one or more required global variables" >&2
    printf '%s\n' "$rows" >&2
    exit 3
  fi
  {
    printf '\nMariaDB effective global variables (verified even when --skip-switch is used):\n'
    while IFS=$'\t' read -r name value; do
      [[ -n "$name" ]] || continue
      printf '  %s=%s\n' "$name" "$value"
    done <<< "$rows"
  } >> "$output_file"
}

run_stock_replenish_script() {
  local mode="$1" output_file="$2" script_path="$ROOT/perf-suite/seeder/replenish_stock.py" status
  if [[ ! -f "$script_path" ]]; then
    echo "Stock replenish script is missing: $script_path" >&2
    exit 5
  fi
  echo "Running replenish_stock.py $mode inside khitmee-perf-backend-1; JSON report: $output_file"
  set +e
  env MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
    docker exec -i -e PYTHONDONTWRITEBYTECODE=1 \
      -w /home/frappe/frappe-bench/sites \
      khitmee-perf-backend-1 \
      /home/frappe/frappe-bench/env/bin/python - "$mode" \
      < "$script_path" > "$output_file"
  status=$?
  set -e
  if [[ "$status" != "0" ]]; then
    echo "replenish_stock.py $mode failed with exit $status; report: $output_file" >&2
    exit 5
  fi
}

run_k6_scenario() {
  local scenario="$1" run_id="$2" duration="$3" from_date="$4" to_date="$5"
  echo "Running k6 scenario $scenario as $run_id (duration=$duration window=$from_date..$to_date)"
  RUN_ID="$run_id" \
  DURATION="$duration" \
  REPORT_FROM_DATE="$from_date" \
  REPORT_TO_DATE="$to_date" \
  TARGET_URL="$TARGET_URL" \
  COMPOSE_PROJECT_NAME="$PROJECT_NAME" \
  PERF_MODE="khitmee_ops" \
  MODE="khitmee_ops" \
  SIMULATED_PING_MS="50" \
  K6_DOCKER_USER="0:0" \
  MEMORY_BUDGET_GB="$MEMORY_BUDGET_GB" \
  PERF_PASSWORD="$PERF_PASSWORD" \
  MSYS_NO_PATHCONV=1 \
  MSYS2_ARG_CONV_EXCL='*' \
  bash "$ROOT/perf-suite/k6/run_k6.sh" "$scenario"
}

main() {
  parse_cli "$@"
  validate_cli
  validate_targeting

  SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
  RESULT_DIR="$ROOT/perf-suite/results"
  RUN_PREFIX="$(normalize_run_prefix "$PASS_ID" "$PROFILE")"
  LEDGER_JSON="$RESULT_DIR/eval-history.json"
  LEDGER_MD="$RESULT_DIR/OVERNIGHT-EVAL-LEDGER.md"
  PROFILE_JSON="$RESULT_DIR/$RUN_PREFIX-profile.json"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "Dry run configuration: pass_id=$PASS_ID profile=$PROFILE run_prefix=$RUN_PREFIX"
    echo "Dry run options: skip_switch=$SKIP_SWITCH skip_profile=$SKIP_PROFILE skip_bench=$SKIP_BENCH strict_clean_state=$STRICT_CLEAN_STATE parity_pass=${PARITY_PASS:-N/A} require_parity=$REQUIRE_PARITY no_overwrite=$NO_OVERWRITE replenish_stock=$REPLENISH_STOCK require_stock_preflight=$REQUIRE_STOCK_PREFLIGHT"
    echo "Dry run notes: ${USER_NOTES:-N/A}"
    echo "Dry run complete: no Docker commands, benchmarks, credential file reads, or artifact cleanup were performed"
    return 0
  fi

  set +e
  clean_previous_run_artifacts
  CLEAN_STATUS=$?
  set -e
  if [[ "$CLEAN_STATUS" != "0" ]]; then
    exit "$CLEAN_STATUS"
  fi

  mkdir -p "$RESULT_DIR"

  export COMPOSE_PROJECT_NAME="$PROJECT_NAME"
  export PERF_MODE="khitmee_ops"
  export MODE="khitmee_ops"
  export SIMULATED_PING_MS="50"
  export K6_DOCKER_USER="0:0"
  MEMORY_BUDGET_GB="${PROFILE%gb}"
  export MEMORY_BUDGET_GB
  PYBIN="$(python_bin)"

  echo "Step 1/8: offline fast-check gate"
  bash "$ROOT/tools/khitmee-dev.sh" fast-check

  CONTAINERS_RECREATED=0
  if [[ "$SKIP_SWITCH" == "0" ]]; then
    echo "Step 2/8: switching $PROJECT_NAME to $PROFILE with khitmee_ops (no purge)"
    PRE_SWITCH_CONTAINER_SNAPSHOT="$(snapshot_perf_container_state)"
    bash "$ROOT/perf-suite/scripts/up.sh" --profile "$PROFILE" --with-khitmee-ops
    POST_SWITCH_CONTAINER_SNAPSHOT="$(snapshot_perf_container_state)"
    if container_snapshot_recreated "$PRE_SWITCH_CONTAINER_SNAPSHOT" "$POST_SWITCH_CONTAINER_SNAPSHOT"; then
      CONTAINERS_RECREATED=1
      echo "Step 2/8: container creation or replacement detected; strict zero-state verification will apply"
    else
      echo "Step 2/8: existing container identities retained; preflight delta verification will apply"
    fi
  else
    echo "Step 2/8: skipped stack switch"
  fi

  echo "Step 3/8: capturing status, live-stack fingerprint, and container baseline"
  verify_backend_identity
  PREFLIGHT_CONTAINER_SNAPSHOT="$(snapshot_perf_container_state)"
  if [[ -z "$PREFLIGHT_CONTAINER_SNAPSHOT" ]]; then
    echo "No $PROJECT_NAME containers found during preflight verification" >&2
    exit 3
  fi
  printf '%s\n' "$PREFLIGHT_CONTAINER_SNAPSHOT" > "$RESULT_DIR/$RUN_PREFIX-preflight-containers.tsv"
  LIVE_BEFORE="$(live_fingerprint)"
  bash "$ROOT/tools/khitmee-dev.sh" status | tee "$RESULT_DIR/$RUN_PREFIX-preflight-status.txt"
  append_mariadb_effective_config "$RESULT_DIR/$RUN_PREFIX-preflight-status.txt"
  echo "live fingerprint before: $LIVE_BEFORE"
  print_container_snapshot "preflight container state:" "$PREFLIGHT_CONTAINER_SNAPSHOT"

  PROFILE_GENERATED=0
  if [[ "$SKIP_PROFILE" == "0" ]]; then
    echo "Step 4/8: running read-only backend profiler"
    PROFILE_TMP="$PROFILE_JSON.tmp"
    rm -f "$PROFILE_TMP"
    if env MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
      docker exec -i -e PYTHONDONTWRITEBYTECODE=1 \
        -w /home/frappe/frappe-bench/sites \
        khitmee-perf-backend-1 \
        /home/frappe/frappe-bench/env/bin/python - --mode all --json-out - \
        < "$ROOT/tools/perf_profile.py" > "$PROFILE_TMP"; then
      mv "$PROFILE_TMP" "$PROFILE_JSON"
      PROFILE_GENERATED=1
      echo "profile json: $PROFILE_JSON"
    else
      rm -f "$PROFILE_TMP"
      echo "Profiler failed; aborting before benchmark" >&2
      exit 4
    fi
  else
    echo "Step 4/8: skipped backend profiler"
  fi

  if [[ "$SKIP_BENCH" == "0" ]]; then
    if [[ "$REPLENISH_STOCK" == "1" ]]; then
      run_stock_replenish_script "--apply" "$RESULT_DIR/$RUN_PREFIX-stock-replenish.json"
    elif [[ "$REQUIRE_STOCK_PREFLIGHT" == "1" ]]; then
      run_stock_replenish_script "--check-only" "$RESULT_DIR/$RUN_PREFIX-stock-preflight.json"
    fi
  fi

  echo "Step 5/8: capturing fail-closed pre-k6 service baseline"
  if ! PRE_K6_SERVICE_STATE="$(capture_expected_service_state)"; then
    printf '%s\n' "$PRE_K6_SERVICE_STATE" > "$RESULT_DIR/$RUN_PREFIX-pre-k6-services.tsv"
    echo "Pre-k6 safety baseline failed: every expected $PROJECT_NAME service must exist and be running before benchmarks" >&2
    exit 3
  fi
  printf '%s\n' "$PRE_K6_SERVICE_STATE" > "$RESULT_DIR/$RUN_PREFIX-pre-k6-services.tsv"
  echo "pre-k6 expected-service baseline: $RESULT_DIR/$RUN_PREFIX-pre-k6-services.tsv"

  BENCH_FAILURES=0
  if [[ "$SKIP_BENCH" == "0" ]]; then
    require_perf_password_for_bench
    echo "Step 6/8: running standardized four-scenario k6 suite"
    if run_k6_scenario "june_sustained" "$RUN_PREFIX-sustained" "$DURATION" "2026-10-27" "2026-11-26"; then
      echo "completed $RUN_PREFIX-sustained"
    else
      echo "WARNING: k6 command for $RUN_PREFIX-sustained exited nonzero; continuing so ledger captures evidence" >&2
      BENCH_FAILURES=$((BENCH_FAILURES + 1))
    fi
    if run_k6_scenario "june_burst_2x" "$RUN_PREFIX-burst-2x" "$DURATION" "2026-10-27" "2026-11-26"; then
      echo "completed $RUN_PREFIX-burst-2x"
    else
      echo "WARNING: k6 command for $RUN_PREFIX-burst-2x exited nonzero; continuing so ledger captures evidence" >&2
      BENCH_FAILURES=$((BENCH_FAILURES + 1))
    fi
    if run_k6_scenario "report_stress" "$RUN_PREFIX-reports" "$DURATION" "2026-10-27" "2026-11-26"; then
      echo "completed $RUN_PREFIX-reports"
    else
      echo "WARNING: k6 command for $RUN_PREFIX-reports exited nonzero; continuing so ledger captures evidence" >&2
      BENCH_FAILURES=$((BENCH_FAILURES + 1))
    fi
    if run_k6_scenario "report_stress" "$RUN_PREFIX-reports-daily" "$DAILY_DURATION" "2026-11-26" "2026-11-26"; then
      echo "completed $RUN_PREFIX-reports-daily"
    else
      echo "WARNING: k6 command for $RUN_PREFIX-reports-daily exited nonzero; continuing so ledger captures evidence" >&2
      BENCH_FAILURES=$((BENCH_FAILURES + 1))
    fi
  else
    echo "Step 6/8: skipped k6 benchmark suite; this pass is diagnostic evidence and cannot be a valid strict run"
  fi
  K6_ALL_ZERO=true
  if [[ "$SKIP_BENCH" != "0" || "$BENCH_FAILURES" != "0" ]]; then
    K6_ALL_ZERO=false
  fi

  echo "Step 7/8: post-run service, OOM/restart, and live-stack safety verification"
  POST_K6_SERVICE_STATE="$(capture_expected_service_state || true)"
  printf '%s\n' "$POST_K6_SERVICE_STATE" > "$RESULT_DIR/$RUN_PREFIX-post-run-services.tsv"
  SAFETY_PASS=true
  SAFETY_OUTPUT=""
  STRICT_ABSOLUTE_STATE=0
  if [[ "$STRICT_CLEAN_STATE" == "1" || "$CONTAINERS_RECREATED" == "1" ]]; then
    STRICT_ABSOLUTE_STATE=1
  fi
  if SAFETY_OUTPUT="$(verify_expected_service_safety "$PRE_K6_SERVICE_STATE" "$POST_K6_SERVICE_STATE" "$STRICT_ABSOLUTE_STATE" 2>&1)"; then
    SAFETY_PASS=true
  else
    SAFETY_PASS=false
  fi
  if [[ -n "$SAFETY_OUTPUT" ]]; then
    printf '%s\n' "$SAFETY_OUTPUT"
  fi
  LIVE_AFTER="$(live_fingerprint)"
  echo "live fingerprint after:  $LIVE_AFTER"
  if [[ "$LIVE_BEFORE" != "$LIVE_AFTER" ]]; then
    echo "safety failure: live $FORBIDDEN_PROJECT fingerprint changed during the perf pass" >&2
    SAFETY_PASS=false
  else
    echo "Live $FORBIDDEN_PROJECT fingerprint unchanged"
  fi

  echo "Step 8/8: recording eval ledger"
  AUTO_NOTES="duration=$DURATION daily_duration=$DAILY_DURATION skip_switch=$SKIP_SWITCH skip_profile=$SKIP_PROFILE skip_bench=$SKIP_BENCH strict_clean_state=$STRICT_CLEAN_STATE replenish_stock=$REPLENISH_STOCK require_stock_preflight=$REQUIRE_STOCK_PREFLIGHT containers_recreated=$CONTAINERS_RECREATED k6_nonzero=$BENCH_FAILURES k6_all_zero=$K6_ALL_ZERO safety_pass=$SAFETY_PASS"
  if [[ -n "$USER_NOTES" ]]; then
    LEDGER_NOTES="$USER_NOTES; $AUTO_NOTES"
  else
    LEDGER_NOTES="$AUTO_NOTES"
  fi
  LEDGER_ARGS=(
    record
    --ledger "$LEDGER_JSON"
    --markdown "$LEDGER_MD"
    --pass-id "$PASS_ID"
    --label "$LABEL"
    --profile "$PROFILE"
    --results-dir "$RESULT_DIR"
    --prefix "$RUN_PREFIX"
    --notes "$LEDGER_NOTES"
    --require-headroom
  )
  if [[ -n "$PARITY_PASS" ]]; then
    LEDGER_ARGS+=(--parity-pass "$PARITY_PASS")
  fi
  if [[ "$SAFETY_PASS" == "true" ]]; then
    LEDGER_ARGS+=(--safety-pass)
  else
    LEDGER_ARGS+=(--no-safety-pass)
  fi
  if [[ "$K6_ALL_ZERO" != "true" ]]; then
    LEDGER_ARGS+=(--k6-exit-nonzero)
  fi
  if [[ "$PROFILE_GENERATED" == "1" ]]; then
    LEDGER_ARGS+=(--profile-json "$PROFILE_JSON")
  fi
  "$PYBIN" "$ROOT/tools/perf_eval_ledger.py" "${LEDGER_ARGS[@]}"

  STRICT_VERDICT="$("$PYBIN" - "$LEDGER_JSON" "$PASS_ID" "$PROFILE" <<'PY_STRICT_VERDICT'
import json
import sys

path, pass_id, profile = sys.argv[1:4]
with open(path, encoding="utf-8-sig") as handle:
    data = json.load(handle)
entry = next(
    (item for item in data.get("passes", []) if item.get("pass_id") == pass_id and item.get("profile") == profile),
    None,
)
print((entry or {}).get("score", {}).get("strict_verdict") or "missing")
PY_STRICT_VERDICT
  )"

  FAILURE_SUMMARY=()
  if [[ "$STRICT_VERDICT" != "GO" ]]; then
    FAILURE_SUMMARY+=("strict ledger verdict is $STRICT_VERDICT, not GO")
  fi
  if [[ "$K6_ALL_ZERO" != "true" ]]; then
    if [[ "$SKIP_BENCH" != "0" ]]; then
      FAILURE_SUMMARY+=("k6 evidence absent because --skip-bench was used")
    else
      FAILURE_SUMMARY+=("$BENCH_FAILURES k6 command(s) exited nonzero")
    fi
  fi
  if [[ "$SAFETY_PASS" != "true" ]]; then
    FAILURE_SUMMARY+=("post-run safety verification failed")
  fi
  MISSING_ARTIFACTS="$(missing_expected_artifacts)"
  MISSING_COUNT=0
  while IFS= read -r missing_path; do
    [[ -n "$missing_path" ]] || continue
    MISSING_COUNT=$((MISSING_COUNT + 1))
    FAILURE_SUMMARY+=("missing expected artifact: $missing_path")
  done <<< "$MISSING_ARTIFACTS"
  if [[ "$REQUIRE_PARITY" == "1" && "$PARITY_PASS" != "true" ]]; then
    FAILURE_SUMMARY+=("parity proof is not true (use --parity-pass only after tools/khitmee-dev.sh parity passes)")
  fi

  echo "ledger: $LEDGER_JSON"
  echo "markdown ledger: $LEDGER_MD"
  if ((${#FAILURE_SUMMARY[@]})); then
    echo "Eval pass failure summary:" >&2
    printf '  - %s\n' "${FAILURE_SUMMARY[@]}" >&2
    exit 6
  fi
  echo "Eval pass orchestration completed with all fail-closed execution gates green"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
