#!/usr/bin/env bash
set -Eeuo pipefail

# Docker Compose commands in this unattended entry point must never inherit
# an open terminal stdin; otherwise docker compose can wait forever.
exec </dev/null

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PERF_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd -- "$PERF_ROOT/.." && pwd)"
DOCKER_DIR="$PERF_ROOT/docker"
PROJECT="khitmee-perf"
PINNED_FIXTURE_SHA="8cc3e00c5bf5c41d7565bced818d718e21fc18d32b8ea651ec227e4b614a6d19"
DEFAULT_FIXTURE="/srv/khitmee-lab/operator/fixtures/${PINNED_FIXTURE_SHA}.tar.gz"
DEFAULT_ACTORS="/srv/khitmee-lab/operator/year-actors/8f17a82b728ccb5b5720c805ac84f0e0986fd497b204652e782296ee2b81e20a/seasonal-actor-env.json"
DEFAULT_MANIFEST="/srv/khitmee-lab/operator/year-actors/8f17a82b728ccb5b5720c805ac84f0e0986fd497b204652e782296ee2b81e20a/seed_manifest.json"

RUNTIME_PROFILE="gunicorn-1x12"
SEASONAL_PROFILE_VALUE="${SEASONAL_PROFILE:-smoke}"
FIXTURE="$DEFAULT_FIXTURE"
FIXTURE_SHA="$PINNED_FIXTURE_SHA"
ACTORS="$DEFAULT_ACTORS"
MANIFEST="$DEFAULT_MANIFEST"
OWNER="seasonal_harness"
APP_TREE="$REPO_ROOT"
APP_REF=""
PREFLIGHT_ONLY=0

usage() {
  cat <<'EOF'
Usage: bash perf-suite/seasonal/qualify_2gb.sh [options]

One-command isolated 2 GiB seasonal qualification on the KhitmeeERPNext WSL stack.

Options:
  --runtime-profile gunicorn-1x12|gunicorn-2x1  Backend runtime shape (default gunicorn-1x12)
  --seasonal-profile smoke|acceptance           Workload length (default smoke)
  --fixture PATH                                Pinned cold-volume fixture archive
  --fixture-sha SHA256                          Expected archive digest
  --actors PATH                                 JSON actor environment for the pinned fixture
  --seed-manifest PATH                          Pinned seed manifest
  --owner NAME                                  Lease owner (default seasonal_harness)
  --app-tree PATH                               Clean git checkout containing apps/khitmee_ops (default this checkout)
  --app-ref REF                                 Commit/ref inside --app-tree to deploy (default HEAD)
  --preflight                                   Run one idle end-to-end request of each class, then stop before load
EOF
}

while (($#)); do
  case "$1" in
    --runtime-profile) RUNTIME_PROFILE="${2:?}"; shift 2 ;;
    --runtime-profile=*) RUNTIME_PROFILE="${1#*=}"; shift ;;
    --seasonal-profile) SEASONAL_PROFILE_VALUE="${2:?}"; shift 2 ;;
    --seasonal-profile=*) SEASONAL_PROFILE_VALUE="${1#*=}"; shift ;;
    --fixture) FIXTURE="${2:?}"; shift 2 ;;
    --fixture=*) FIXTURE="${1#*=}"; shift ;;
    --fixture-sha) FIXTURE_SHA="${2:?}"; shift 2 ;;
    --fixture-sha=*) FIXTURE_SHA="${1#*=}"; shift ;;
    --actors) ACTORS="${2:?}"; shift 2 ;;
    --actors=*) ACTORS="${1#*=}"; shift ;;
    --seed-manifest) MANIFEST="${2:?}"; shift 2 ;;
    --seed-manifest=*) MANIFEST="${1#*=}"; shift ;;
    --owner) OWNER="${2:?}"; shift 2 ;;
    --owner=*) OWNER="${1#*=}"; shift ;;
    --app-tree) APP_TREE="${2:?}"; shift 2 ;;
    --app-tree=*) APP_TREE="${1#*=}"; shift ;;
    --app-ref) APP_REF="${2:?}"; shift 2 ;;
    --app-ref=*) APP_REF="${1#*=}"; shift ;;
    --preflight) PREFLIGHT_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$RUNTIME_PROFILE" in gunicorn-1x12|gunicorn-2x1) ;; *) echo "invalid runtime profile" >&2; exit 2 ;; esac
case "$SEASONAL_PROFILE_VALUE" in smoke|acceptance) ;; *) echo "invalid seasonal profile" >&2; exit 2 ;; esac
[[ -f "$FIXTURE" ]] || { echo "pinned fixture missing: $FIXTURE" >&2; exit 2; }
[[ -f "$ACTORS" ]] || { echo "actor environment missing: $ACTORS" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "seed manifest missing: $MANIFEST" >&2; exit 2; }
[[ "${WSL_DISTRO_NAME:-}" == "KhitmeeERPNext" ]] || { echo "run inside WSL distro KhitmeeERPNext" >&2; exit 2; }
command -v docker >/dev/null && docker info >/dev/null 2>&1 || { echo "Docker is unavailable" >&2; exit 2; }

# Lease operations must share the Windows-side autoloop lock directory. WSL's
# native /tmp lock directory is a different coordination domain and must not be
# used for khitmee-perf when this checkout is on a /mnt/<drive> filesystem.
if [[ "$REPO_ROOT" == /mnt/* ]]; then
  command -v wslpath >/dev/null 2>&1 || { echo "NO-GO: wslpath is unavailable; cannot use Windows lease interop" >&2; exit 2; }
  WINDOWS_PYTHON="$(command -v python.exe 2>/dev/null || true)"
  [[ -n "$WINDOWS_PYTHON" ]] || { echo "NO-GO: python.exe is unavailable in WSL; cannot use Windows lease interop" >&2; exit 2; }
  WINDOWS_REPO_ROOT="$(wslpath -w "$REPO_ROOT")" || { echo "NO-GO: cannot convert repo path for Windows lease interop" >&2; exit 2; }
  [[ -n "$WINDOWS_REPO_ROOT" && "$WINDOWS_REPO_ROOT" != /mnt/* ]] || { echo "NO-GO: invalid Windows repo path from wslpath" >&2; exit 2; }
  LEASE_CMD=("$WINDOWS_PYTHON" -B "${WINDOWS_REPO_ROOT}/tools/autoloop/cli.py")
  LEASE_DOMAIN="windows-interop"
else
  LEASE_CMD=(python3 -B "$REPO_ROOT/tools/autoloop/cli.py")
  LEASE_DOMAIN="wsl-native"
fi
lease_cli() {
  "${LEASE_CMD[@]}" lease "$@"
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="qualify-${RUNTIME_PROFILE}-${SEASONAL_PROFILE_VALUE}-${STAMP}"
ARTIFACT_DIR="$PERF_ROOT/results/${RUN_ID}"
mkdir -p "$ARTIFACT_DIR"
exec > >(tee -a "$ARTIFACT_DIR/qualify.log") 2>&1

echo "[$STAMP] seasonal qualification run $RUN_ID"
echo "Artifacts: $ARTIFACT_DIR"

APP_TREE_METADATA="$ARTIFACT_DIR/app-tree.json"
APP_TREE_MATERIALIZED="$ARTIFACT_DIR/app-tree-materialized"
APP_REF_ARGS=()
if [[ -n "$APP_REF" ]]; then
  APP_REF_ARGS=(--ref "$APP_REF")
fi
if ! python3 -B "$SCRIPT_DIR/app_tree.py" \
  --checkout "$APP_TREE" \
  "${APP_REF_ARGS[@]}" \
  --output "$APP_TREE_METADATA" \
  --materialize-dir "$APP_TREE_MATERIALIZED" >"$ARTIFACT_DIR/app-tree.stdout" 2>"$ARTIFACT_DIR/app-tree.stderr"; then
  cat "$ARTIFACT_DIR/app-tree.stderr" >&2
  echo "NO-GO: candidate application tree failed validation/materialization" >&2
  exit 2
fi
echo "Candidate app tree evidence: $APP_TREE_METADATA"

PERF_LEASE_TOKEN=""
TIMING_LEASE_TOKEN=""
RENEW_PID=""
TEMP_SCRIPT_PATHS=()

run_bash_script() {
  local script="$1"
  shift
  if grep -q $'\r' "$script"; then
    # TEMPORARY until the autoloop_meta *.sh text eol=lf normalization reaches
    # this branch: execute an LF-only copy beside the original so scripts that
    # derive paths from BASH_SOURCE still resolve the correct directory.
    echo "TEMPORARY CRLF fallback for $script" >&2
    local tmp="$(dirname "$script")/.$(basename "$script").lf-tmp.$$"
    tr -d '\r' <"$script" >"$tmp"
    chmod +x "$tmp"
    TEMP_SCRIPT_PATHS+=("$tmp")
    bash "$tmp" "$@"
  else
    bash "$script" "$@"
  fi
}

cleanup() {
  status=$?
  if [[ -n "$RENEW_PID" ]]; then
    kill "$RENEW_PID" >/dev/null 2>&1 || true
    wait "$RENEW_PID" >/dev/null 2>&1 || true
  fi
  if [[ -f "$ARTIFACT_DIR/foreign-load-start.json" && ! -f "$ARTIFACT_DIR/foreign-load-end.json" ]]; then
    python3 -B "$SCRIPT_DIR/foreign_load.py" --output "$ARTIFACT_DIR/foreign-load-end.json" >/dev/null 2>&1 || true
  fi
  if [[ -n "$TIMING_LEASE_TOKEN" ]]; then
    lease_cli release --resource host-timing --owner "$OWNER" --token "$TIMING_LEASE_TOKEN" >/dev/null 2>&1 || true
  fi
  if [[ -n "$PERF_LEASE_TOKEN" ]]; then
    lease_cli release --resource khitmee-perf --owner "$OWNER" --token "$PERF_LEASE_TOKEN" >/dev/null 2>&1 || true
  fi
  if ((${#TEMP_SCRIPT_PATHS[@]})); then
    rm -f "${TEMP_SCRIPT_PATHS[@]}" >/dev/null 2>&1 || true
  fi
  rm -f "$PERF_ROOT/scripts/."*.lf-tmp.$$ "$PERF_ROOT/k6/."*.lf-tmp.$$ >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT INT TERM

PERF_LEASE_JSON="$ARTIFACT_DIR/lease-khitmee-perf.json"
TIMING_LEASE_JSON="$ARTIFACT_DIR/lease-host-timing.json"
if ! lease_cli acquire --resource khitmee-perf --owner "$OWNER" --ttl 900 > "$PERF_LEASE_JSON"; then
  echo "NO-GO: khitmee-perf lease is unavailable through $LEASE_DOMAIN" >&2
  exit 5
fi
PERF_LEASE_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$PERF_LEASE_JSON")"
if ! lease_cli acquire --resource host-timing --owner "$OWNER" --ttl 900 > "$TIMING_LEASE_JSON"; then
  echo "NO-GO: host-timing lease is unavailable through $LEASE_DOMAIN after acquiring khitmee-perf" >&2
  exit 5
fi
TIMING_LEASE_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$TIMING_LEASE_JSON")"
(
  while :; do
    sleep 240
    renew_ok=1
    lease_cli renew --resource khitmee-perf --owner "$OWNER" --token "$PERF_LEASE_TOKEN" --ttl 900 >>"$ARTIFACT_DIR/lease-renew.log" 2>&1 || renew_ok=0
    lease_cli renew --resource host-timing --owner "$OWNER" --token "$TIMING_LEASE_TOKEN" --ttl 900 >>"$ARTIFACT_DIR/lease-renew.log" 2>&1 || renew_ok=0
    if ((renew_ok == 0)); then
      : >"$ARTIFACT_DIR/lease-renew.failed"
    fi
  done
) &
RENEW_PID=$!
echo "Acquired khitmee-perf and host-timing leases as $OWNER through $LEASE_DOMAIN"

python3 -B "$SCRIPT_DIR/foreign_load.py" --output "$ARTIFACT_DIR/foreign-load-start.json" >"$ARTIFACT_DIR/foreign-load-start.stdout" 2>&1 || true

# Fail closed on foreign or duplicate compose containers. A hash-prefixed or
# otherwise noncanonical khitmee-perf container is evidence that another actor
# used the same Docker project outside this entry point; this script must not
# repair or adopt that state.
foreign_containers=()
declare -A canonical_seen=()
duplicate_containers=()
while IFS= read -r container_name; do
  [[ -n "$container_name" ]] || continue
  canonical="$container_name"
  if [[ "$container_name" =~ ^[0-9a-f]{12}[_-](khitmee-perf-.+)$ ]]; then
    foreign_containers+=("$container_name")
    canonical="${BASH_REMATCH[1]}"
  elif [[ ! "$container_name" =~ ^khitmee-perf- ]]; then
    foreign_containers+=("$container_name")
  fi
  canonical_seen["$canonical"]=$(( ${canonical_seen["$canonical"]:-0} + 1 ))
  if (( canonical_seen["$canonical"] > 1 )); then
    duplicate_containers+=("$container_name")
  fi
done < <(docker ps -a --filter name=khitmee-perf --format '{{.Names}}')
if ((${#foreign_containers[@]} || ${#duplicate_containers[@]})); then
  echo "NO-GO: refusing to start because foreign or duplicate khitmee-perf containers exist" >&2
  printf 'foreign: %s\n' "${foreign_containers[@]:-none}" >&2
  printf 'duplicate: %s\n' "${duplicate_containers[@]:-none}" >&2
  echo "Inspect ownership/coordination first; this script will not repair another actor's stack." >&2
  exit 3
fi

# Refuse to touch the live project; down.sh performs its own live-state verification.
run_bash_script "$PERF_ROOT/scripts/down.sh" >"$ARTIFACT_DIR/down.log" 2>&1

echo "Restoring pinned fixture $FIXTURE_SHA"
python3 -B "$SCRIPT_DIR/fixture_restore.py" "$FIXTURE" \
  --expected-sha256 "$FIXTURE_SHA" \
  --docker-dir "$DOCKER_DIR" \
  --output "$ARTIFACT_DIR/fixture-restore.json" | tee "$ARTIFACT_DIR/fixture-restore.stdout"

echo "Starting isolated stack with khitmee_ops"
run_bash_script "$PERF_ROOT/scripts/up.sh" --profile 2gb --with-khitmee-ops >"$ARTIFACT_DIR/up.log" 2>&1

OVERLAY="$SCRIPT_DIR/profiles/runtime-${RUNTIME_PROFILE}.yaml"
[[ -f "$OVERLAY" ]] || { echo "missing runtime overlay $OVERLAY" >&2; exit 2; }
compose() {
  docker compose \
    --project-name "$PROJECT" \
    --project-directory "$DOCKER_DIR" \
    --env-file "$DOCKER_DIR/.env" \
    -f "$DOCKER_DIR/compose.base.yaml" \
    -f "$DOCKER_DIR/profile-2gb.yaml" \
    -f "$DOCKER_DIR/khitmee-ops-2gb.yaml" \
    -f "$OVERLAY" \
    -f "$SCRIPT_DIR/compose-observability.yaml" \
    "$@"
}

echo "Applying runtime profile $RUNTIME_PROFILE"
# Apply cgroup caps in place. Recreating the one-shot configurator with compose
# can hit a Docker Engine race ("No such container") after it exits; cgroup
# updates are the effective constraint being proven and avoid that race.
if [[ "$RUNTIME_PROFILE" == "gunicorn-2x1" ]]; then
  CPU_CAPS=$'db=0.60
backend=0.75
queue=0.25
scheduler=0.05
redis-cache=0.02
redis-queue=0.02
websocket=0.05
frontend=0.06
configurator=0.20'
  MEMORY_CAPS=$'db=512m
backend=544m
queue=224m
scheduler=96m
redis-cache=64m
redis-queue=32m
websocket=72m
frontend=24m
configurator=64m'
else
  CPU_CAPS=$'db=0.70
backend=0.75
queue=0.15
scheduler=0.05
redis-cache=0.05
redis-queue=0.05
websocket=0.05
frontend=0.15
configurator=0.05'
  MEMORY_CAPS=$'configurator=48m'
fi
while IFS='=' read -r service cpus; do
  [[ -n "$service" ]] || continue
  docker update --cpus="$cpus" "khitmee-perf-${service}-1" >/dev/null
done <<<"$CPU_CAPS"
while IFS='=' read -r service memory; do
  [[ -n "$service" ]] || continue
  docker update --memory="$memory" --memory-swap="$memory" "khitmee-perf-${service}-1" >/dev/null
done <<<"$MEMORY_CAPS"
if [[ "$RUNTIME_PROFILE" == "gunicorn-2x1" ]]; then
  # Command/environment-bearing services must be recreated for the ADR shape;
  # docker update above cannot change the MariaDB command, Gunicorn command, or
  # websocket NODE_OPTIONS. Frontend is recreated too because nginx caches the
  # backend container IP across backend replacement. --no-deps avoids the
  # one-shot configurator race.
  compose up -d --no-deps db backend websocket frontend >"$ARTIFACT_DIR/runtime-up.log" 2>&1
fi

SITE_NAME_VALUE="$(python3 - "$DOCKER_DIR/.env" <<'PY'
import sys
for line in open(sys.argv[1], encoding='utf-8'):
    if line.startswith('SITE_NAME='):
        print(line.strip().split('=',1)[1]); break
else:
    print('bench-loadtest.localhost')
PY
)"

echo "Deploying candidate khitmee_ops tree and running required migration"
python3 -B "$SCRIPT_DIR/deploy_candidate.py" \
  --metadata "$APP_TREE_METADATA" \
  --docker-dir "$DOCKER_DIR" \
  --actors "$ACTORS" \
  --artifact-dir "$ARTIFACT_DIR" \
  --site "$SITE_NAME_VALUE" \
  --compose-project "$PROJECT" \
  --compose-file "$DOCKER_DIR/compose.base.yaml" \
  --compose-file "$DOCKER_DIR/profile-2gb.yaml" \
  --compose-file "$DOCKER_DIR/khitmee-ops-2gb.yaml" \
  --compose-file "$OVERLAY" \
  --compose-file "$SCRIPT_DIR/compose-observability.yaml" \
  >"$ARTIFACT_DIR/candidate-deployment.stdout" 2>"$ARTIFACT_DIR/candidate-deployment.stderr" || {
    cat "$ARTIFACT_DIR/candidate-deployment.stderr" >&2
    exit 2
  }

for _ in $(seq 1 90); do
  if curl -fsS --max-time 5 -H "Host: $SITE_NAME_VALUE" http://127.0.0.1:18086/api/method/ping >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS --max-time 10 -H "Host: $SITE_NAME_VALUE" http://127.0.0.1:18086/api/method/ping >/dev/null

echo "Verifying effective container resource limits"
python3 -B "$SCRIPT_DIR/resource_proof.py" --output "$ARTIFACT_DIR/resource-proof-before.json" || true
RESOURCE_MEASUREMENT_OK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("measurement_passed") is True)' "$ARTIFACT_DIR/resource-proof-before.json" 2>/dev/null || printf False)"
if [[ "$RESOURCE_MEASUREMENT_OK" != "True" ]]; then
  echo "NO-GO: cannot prove effective container resource limits; refusing workload" >&2
  exit 4
fi
if [[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("within_budget") is True)' "$ARTIFACT_DIR/resource-proof-before.json" 2>/dev/null || printf False)" != "True" ]]; then
  echo "Runtime profile is outside the ADR workload budget; continuing only as informative non-qualification evidence." >&2
fi

# Load actor credentials without writing or printing them. Keys are validated;
# values are shell-quoted by Python and the temporary file is removed immediately.
ENV_EXPORTS="$(mktemp)"
chmod 600 "$ENV_EXPORTS"
python3 - "$ACTORS" >"$ENV_EXPORTS" <<'PY'
import json, shlex, sys
allowed={"PERF_USER","PERF_PASSWORD","SEASONAL_MANAGER_WAREHOUSE_USER","SEASONAL_MANAGER_WAREHOUSE_PASSWORD","SEASONAL_MANAGER_MONTHLY_USER","SEASONAL_MANAGER_MONTHLY_PASSWORD","SEASONAL_MANAGER_DETAIL_USER","SEASONAL_MANAGER_DETAIL_PASSWORD","SEASONAL_MANAGER_EXPORT_USER","SEASONAL_MANAGER_EXPORT_PASSWORD"}
data=json.load(open(sys.argv[1], encoding='utf-8'))
missing=sorted(allowed-set(data))
if missing:
    raise SystemExit('actor environment missing keys: '+','.join(missing))
for key in sorted(allowed):
    print(f"{key}={shlex.quote(str(data[key]))}")
PY
set -a
# shellcheck disable=SC1090
. "$ENV_EXPORTS"
set +a
rm -f "$ENV_EXPORTS"

export RUN_ID SEED_MANIFEST_PATH="$MANIFEST" SEASONAL_PROFILE="$SEASONAL_PROFILE_VALUE" SITE_NAME="$SITE_NAME_VALUE"
export REPORT_TO_DATE="2026-11-26" REPORT_FROM_DATE="2026-01-01" EXPORT_FROM_DATE="2025-11-27"
export MEMORY_BUDGET_GB=2 COMPOSE_PROJECT_NAME="$PROJECT" K6_RUNTIME="${K6_RUNTIME:-docker}" STATS_INTERVAL_SECONDS="${STATS_INTERVAL_SECONDS:-2}"

STATS_COLLECTOR_SOURCE="$PERF_ROOT/scripts/stats-collector.sh"
if grep -q $'\r' "$STATS_COLLECTOR_SOURCE"; then
  # TEMPORARY CRLF fallback; see run_bash_script above.
  STATS_COLLECTOR_LF="$PERF_ROOT/scripts/.stats-collector.sh.lf-tmp.$$"
  tr -d '\r' <"$STATS_COLLECTOR_SOURCE" >"$STATS_COLLECTOR_LF"
  chmod +x "$STATS_COLLECTOR_LF"
  TEMP_SCRIPT_PATHS+=("$STATS_COLLECTOR_LF")
  export STATS_COLLECTOR="$STATS_COLLECTOR_LF"
fi

if [[ -f "$ARTIFACT_DIR/lease-renew.failed" ]]; then
  echo "INVALID: lease renewal failed before workload; exclusive host/perf use cannot be proven" >&2
  exit 6
fi

K6_SCENARIO="seasonal"
if [[ "$PREFLIGHT_ONLY" == "1" ]]; then
  K6_SCENARIO="seasonal_preflight"
  echo "Running idle preflight: one request of each type before any load"
else
  echo "Running seasonal $SEASONAL_PROFILE_VALUE workload"
fi
set +e
run_bash_script "$PERF_ROOT/k6/run_k6.sh" "$K6_SCENARIO" | tee "$ARTIFACT_DIR/workload.log"
WORKLOAD_STATUS=${PIPESTATUS[0]}
set -e

python3 -B "$SCRIPT_DIR/resource_proof.py" --output "$ARTIFACT_DIR/resource-proof-after.json" || true
cp -f "$PERF_ROOT/results/${RUN_ID}-scorecard.md" "$ARTIFACT_DIR/scorecard.md" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-seasonal-verification.json" "$ARTIFACT_DIR/seasonal-verification.json" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-container-stats.csv" "$ARTIFACT_DIR/container-stats.csv" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-k6-summary.json" "$ARTIFACT_DIR/k6-summary.json" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-k6.log" "$ARTIFACT_DIR/k6.log" 2>/dev/null || true
rm -rf "$ARTIFACT_DIR/attribution"
cp -a "$PERF_ROOT/results/${RUN_ID}-attribution" "$ARTIFACT_DIR/attribution" 2>/dev/null || true

python3 -B "$SCRIPT_DIR/foreign_load.py" --output "$ARTIFACT_DIR/foreign-load-end.json" >"$ARTIFACT_DIR/foreign-load-end.stdout" 2>&1 || true

python3 - "$ARTIFACT_DIR" "$WORKLOAD_STATUS" <<'PY'
import json, pathlib, sys
artifact = pathlib.Path(sys.argv[1])
status = int(sys.argv[2])
verification = {}
proof = {}
proof_before = {}
foreign_start = {}
foreign_end = {}
candidate = {}
migration = {}
for name, target in [
    ('seasonal-verification.json', 'verification'),
    ('resource-proof-after.json', 'proof'),
    ('resource-proof-before.json', 'proof_before'),
    ('foreign-load-start.json', 'foreign_start'),
    ('foreign-load-end.json', 'foreign_end'),
    ('candidate-deployment.json', 'candidate'),
    ('candidate-migration.json', 'migration'),
]:
    try:
        value = json.loads((artifact / name).read_text())
    except Exception:
        value = {}
    if target == 'verification':
        verification = value
    elif target == 'proof':
        proof = value
    elif target == 'proof_before':
        proof_before = value
    elif target == 'foreign_start':
        foreign_start = value
    elif target == 'candidate':
        candidate = value
    elif target == 'migration':
        migration = value
    else:
        foreign_end = value
duplicates = sum((row or {}).get('duplicates', 0) for row in (verification.get('by_type') or {}).values())
oom = [row.get('service') for row in proof.get('services') or [] if row.get('oom_killed')]
restarts = [
    {'service': row.get('service'), 'restart_count': row.get('restart_count')}
    for row in proof.get('services') or [] if row.get('restart_count')
]
foreign_counts = [foreign_start.get('foreign_count', -1), foreign_end.get('foreign_count', -1)]
foreign_invalid = any(count != 0 for count in foreign_counts)
lease_renewal_failed = (artifact / 'lease-renew.failed').exists()
resource_budget_ok = proof_before.get('within_budget') is True and proof.get('within_budget') is True
if foreign_invalid or lease_renewal_failed:
    verdict = 'INVALID'
    exit_code = 6 if status == 0 else status
elif not resource_budget_ok:
    verdict = 'NO-GO'
    exit_code = status if status else 7
else:
    verdict = 'GO' if status == 0 else 'NO-GO'
    exit_code = status
result = {
    'verdict': verdict,
    'valid_for_timing': not (foreign_invalid or lease_renewal_failed),
    'lease_renewal_failed': lease_renewal_failed,
    'qualification_resource_budget_ok': resource_budget_ok,
    'qualification_evidence': not (foreign_invalid or lease_renewal_failed) and resource_budget_ok,
    'workload_memory_limit_mib_before': proof_before.get('workload_memory_limit_total_mib'),
    'workload_memory_limit_mib_after': proof.get('workload_memory_limit_total_mib'),
    'resource_budget_problems_before': proof_before.get('budget_problems', []),
    'resource_budget_problems_after': proof.get('budget_problems', []),
    'workload_exit': status,
    'app_checkout': candidate.get('app_checkout'),
    'app_ref': candidate.get('app_ref'),
    'app_commit_sha': candidate.get('app_commit_sha'),
    'app_tree_hash': candidate.get('app_tree_hash'),
    'app_tree_path': candidate.get('app_tree_path'),
    'candidate_migration_required': migration.get('required') is True,
    'candidate_migration_exit_code': migration.get('exit_code'),
    'candidate_migration_log': migration.get('log'),
    'candidate_role_grants': candidate.get('role_grants_artifact'),
    'request_attribution_summary': str(artifact / 'attribution' / 'attribution-summary.json'),
    'duplicates': duplicates,
    'docker_oom_killed_after_run': oom,
    'restart_counts_after_run': restarts,
    'foreign_load_start_count': foreign_start.get('foreign_count'),
    'foreign_load_end_count': foreign_end.get('foreign_count'),
    'foreign_load_start': foreign_start.get('foreign_containers', []),
    'foreign_load_end': foreign_end.get('foreign_containers', []),
    'baseline_containers_start': foreign_start.get('baseline_containers', []),
    'baseline_containers_end': foreign_end.get('baseline_containers', []),
    'scorecard': str(artifact / 'scorecard.md'),
    'resource_proof': str(artifact / 'resource-proof-before.json'),
    'foreign_load_start_artifact': str(artifact / 'foreign-load-start.json'),
    'foreign_load_end_artifact': str(artifact / 'foreign-load-end.json'),
    'exit_code': exit_code,
}
(artifact / 'final-result.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
print('FINAL ' + verdict + ' duplicates=' + str(duplicates) + ' workload_exit=' + str(status) + ' foreign_load=' + str(foreign_counts))
print('final result: ' + str(artifact / 'final-result.json'))
PY
FINAL_EXIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$ARTIFACT_DIR/final-result.json" 2>/dev/null || printf '%s' "$WORKLOAD_STATUS")"
exit "$FINAL_EXIT"
