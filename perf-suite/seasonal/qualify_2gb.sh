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
PINNED_FIXTURE_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["sha256"])' "$SCRIPT_DIR/acceptance-fixture.json")"
DEFAULT_FIXTURE="/srv/khitmee-lab/operator/fixtures/${PINNED_FIXTURE_SHA}.tar.gz"
DEFAULT_ACTORS="/srv/khitmee-lab/operator/year-actors/8f17a82b728ccb5b5720c805ac84f0e0986fd497b204652e782296ee2b81e20a/seasonal-actor-env.json"
DEFAULT_MANIFEST="/srv/khitmee-lab/operator/year-actors/8f17a82b728ccb5b5720c805ac84f0e0986fd497b204652e782296ee2b81e20a/seed_manifest.json"

RUNTIME_PROFILE="adr-2gb-guest"
RUNTIME_PROFILE_EXPLICIT=0
SEASONAL_PROFILE_VALUE="${SEASONAL_PROFILE:-smoke}"
FIXTURE="$DEFAULT_FIXTURE"
FIXTURE_SHA="$PINNED_FIXTURE_SHA"
FIXTURE_MANIFEST=""
ACTORS="$DEFAULT_ACTORS"
MANIFEST="$DEFAULT_MANIFEST"
OWNER="seasonal_harness"
APP_TREE="$REPO_ROOT"
APP_REF=""
PREFLIGHT_ONLY=0
ORACLE_COMMAND=""
CGROUP_PARENT="/sys/fs/cgroup/khitmeeperf.slice"
ENVIRONMENT_ATTESTATION=""

usage() {
  cat <<'EOF'
Usage: bash perf-suite/seasonal/qualify_2gb.sh [options]

One-command isolated 2 GiB seasonal qualification on the KhitmeeERPNext WSL stack.

Options:
  --seasonal-profile smoke|acceptance           Workload length (default smoke)
  --fixture PATH                                Pinned cold-volume fixture archive
  --fixture-sha SHA256                          Expected archive digest for diagnostic alternate fixtures; qualification remains pinned
  --fixture-manifest PATH                       Archive manifest (default companion beside --fixture)
  --actors PATH                                 JSON actor environment for the pinned fixture
  --seed-manifest PATH                          Pinned seed manifest
  --owner NAME                                  Lease owner (default seasonal_harness)
  --app-tree PATH                               Clean git checkout containing apps/khitmee_ops (default this checkout)
  --app-ref REF                                 Commit/ref inside --app-tree to deploy (default HEAD)
  --preflight                                   Run one idle end-to-end request of each class, then stop before load
  --oracle-command CMD                          Paired stock-oracle command; must write oracle-evidence.json in the artifact dir
  --cgroup-parent PATH                          ADR parent cgroup to attest (default /sys/fs/cgroup/khitmeeperf.slice)
  --environment-attestation PATH                Owner-attested host environment JSON

Owner environment attestation procedure:
  1. Copy perf-suite/seasonal/environment-attestation.example.json outside the repo.
  2. The environment owner fills guest.host_identity and guest.vm_identity from
     hypervisor/host inventory, and vcpus, assigned_memory_mib and swap_mib from
     the actual VM/WSL assignment. Qualification accepts exactly 2, 2048 and 0.
  3. Fill load_generator.host_identity and load_generator.address from the
     separate machine that runs k6. The address must be the host used in
     K6_DOCKER_HOST (for example tcp://builder.example:2375 uses builder.example).
  4. Pass the completed file with --environment-attestation. Missing,
     inconsistent, or diagnostic-only evidence can never produce GO.
EOF
}

while (($#)); do
  case "$1" in
    --runtime-profile) RUNTIME_PROFILE="${2:?}"; RUNTIME_PROFILE_EXPLICIT=1; shift 2 ;;
    --runtime-profile=*) RUNTIME_PROFILE="${1#*=}"; RUNTIME_PROFILE_EXPLICIT=1; shift ;;
    --seasonal-profile) SEASONAL_PROFILE_VALUE="${2:?}"; shift 2 ;;
    --seasonal-profile=*) SEASONAL_PROFILE_VALUE="${1#*=}"; shift ;;
    --fixture) FIXTURE="${2:?}"; shift 2 ;;
    --fixture=*) FIXTURE="${1#*=}"; shift ;;
    --fixture-sha) FIXTURE_SHA="${2:?}"; shift 2 ;;
    --fixture-sha=*) FIXTURE_SHA="${1#*=}"; shift ;;
    --fixture-manifest) FIXTURE_MANIFEST="${2:?}"; shift 2 ;;
    --fixture-manifest=*) FIXTURE_MANIFEST="${1#*=}"; shift ;;
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
    --oracle-command) ORACLE_COMMAND="${2:?}"; shift 2 ;;
    --oracle-command=*) ORACLE_COMMAND="${1#*=}"; shift ;;
    --cgroup-parent) CGROUP_PARENT="${2:?}"; shift 2 ;;
    --cgroup-parent=*) CGROUP_PARENT="${1#*=}"; shift ;;
    --environment-attestation) ENVIRONMENT_ATTESTATION="${2:?}"; shift 2 ;;
    --environment-attestation=*) ENVIRONMENT_ATTESTATION="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$RUNTIME_PROFILE_EXPLICIT" == "1" && "$RUNTIME_PROFILE" != "adr-2gb-guest" ]]; then
  echo "--runtime-profile values gunicorn-1x12/gunicorn-2x1 are legacy topology overrides; ADR 0004 qualification fixes the 2gb-guest topology" >&2
  exit 2
fi
case "$RUNTIME_PROFILE" in adr-2gb-guest) ;; *) echo "invalid runtime profile" >&2; exit 2 ;; esac
case "$SEASONAL_PROFILE_VALUE" in smoke|acceptance) ;; *) echo "invalid seasonal profile" >&2; exit 2 ;; esac
[[ -f "$FIXTURE" ]] || { echo "pinned fixture missing: $FIXTURE" >&2; exit 2; }
if [[ -z "$FIXTURE_MANIFEST" ]]; then
  if [[ "$FIXTURE" == *.tar.gz ]]; then
    FIXTURE_MANIFEST="${FIXTURE%.tar.gz}.manifest.json"
  else
    FIXTURE_MANIFEST="${FIXTURE}.manifest.json"
  fi
fi
[[ -f "$FIXTURE_MANIFEST" ]] || { echo "pinned fixture manifest missing: $FIXTURE_MANIFEST" >&2; exit 2; }
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
  --fixture-manifest "$FIXTURE_MANIFEST" \
  --docker-dir "$DOCKER_DIR" \
  --output "$ARTIFACT_DIR/fixture-restore.json" | tee "$ARTIFACT_DIR/fixture-restore.stdout"

echo "Starting isolated base stack; the selected candidate tree is deployed before migration"
# Do not pass --with-khitmee-ops here: startup must not mount or migrate the
# working checkout. deploy_candidate.py below mounts the exact materialized
# tree, recreates the Python services, and performs the only khitmee_ops migration.
run_bash_script "$PERF_ROOT/scripts/up.sh" --profile 2gb-guest >"$ARTIFACT_DIR/up.log" 2>&1

SEASONAL_COMPOSE_FILES=(
  "$DOCKER_DIR/compose.base.yaml"
  "$DOCKER_DIR/profile-2gb-guest.yaml"
  "$DOCKER_DIR/khitmee-ops-2gb.yaml"
  "$DOCKER_DIR/khitmee-ops-guest.yaml"
  "$SCRIPT_DIR/compose-observability.yaml"
)
for compose_file in "${SEASONAL_COMPOSE_FILES[@]}"; do
  [[ -f "$compose_file" ]] || { echo "missing seasonal compose file $compose_file" >&2; exit 2; }
done
DEPLOY_COMPOSE_ARGS=()
for compose_file in "${SEASONAL_COMPOSE_FILES[@]}"; do
  DEPLOY_COMPOSE_ARGS+=(--compose-file "$compose_file")
done

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
  "${DEPLOY_COMPOSE_ARGS[@]}" \
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

echo "Attesting ADR guest envelope before workload"
python3 -B "$SCRIPT_DIR/guest_attestation.py" --parent "$CGROUP_PARENT" --project "$PROJECT" --output "$ARTIFACT_DIR/guest-attestation-before.json" >"$ARTIFACT_DIR/guest-attestation-before.stdout" 2>&1 || true

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

echo "Attesting k6 load-generator placement"
python3 -B "$SCRIPT_DIR/load_generator_attestation.py"   --runtime "$K6_RUNTIME"   --docker-host "${K6_DOCKER_HOST:-}"   --output "$ARTIFACT_DIR/load-generator-attestation.json"   >"$ARTIFACT_DIR/load-generator-attestation.stdout" 2>&1 || true

echo "Validating owner host-environment attestation"
python3 -B "$SCRIPT_DIR/environment_attestation.py"   --attestation "$ENVIRONMENT_ATTESTATION"   --guest-evidence "$ARTIFACT_DIR/guest-attestation-before.json"   --generator-evidence "$ARTIFACT_DIR/load-generator-attestation.json"   --output "$ARTIFACT_DIR/environment-attestation.json"   >"$ARTIFACT_DIR/environment-attestation.stdout" 2>&1 || true

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

set +e
python3 -B "$SCRIPT_DIR/resource_proof.py" --output "$ARTIFACT_DIR/resource-proof-after.json"
RESOURCE_PROOF_AFTER_STATUS=$?
set -e
printf '%s\n' "$RESOURCE_PROOF_AFTER_STATUS" >"$ARTIFACT_DIR/resource-proof-after.exit"
python3 -B "$SCRIPT_DIR/guest_attestation.py" --parent "$CGROUP_PARENT" --project "$PROJECT" --output "$ARTIFACT_DIR/guest-attestation-after.json" >"$ARTIFACT_DIR/guest-attestation-after.stdout" 2>&1 || true
cp -f "$PERF_ROOT/results/${RUN_ID}-scorecard.md" "$ARTIFACT_DIR/scorecard.md" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-seasonal-verification.json" "$ARTIFACT_DIR/seasonal-verification.json" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-container-stats.csv" "$ARTIFACT_DIR/container-stats.csv" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-k6-summary.json" "$ARTIFACT_DIR/k6-summary.json" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-k6.log" "$ARTIFACT_DIR/k6.log" 2>/dev/null || true
rm -rf "$ARTIFACT_DIR/attribution"
cp -a "$PERF_ROOT/results/${RUN_ID}-attribution" "$ARTIFACT_DIR/attribution" 2>/dev/null || true

python3 -B "$SCRIPT_DIR/foreign_load.py" --output "$ARTIFACT_DIR/foreign-load-end.json" >"$ARTIFACT_DIR/foreign-load-end.stdout" 2>&1 || true

if [[ -n "$ORACLE_COMMAND" ]]; then
  echo "Running paired stock-oracle evidence command"
  set +e
  ORACLE_RUN_ID="$RUN_ID" \
  ORACLE_ARTIFACT_DIR="$ARTIFACT_DIR" \
  ORACLE_APP_COMMIT_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("app_commit_sha", ""))' "$APP_TREE_METADATA" 2>/dev/null || true)" \
  ORACLE_FIXTURE_SHA256="$FIXTURE_SHA" \
  bash -c "$ORACLE_COMMAND" >"$ARTIFACT_DIR/oracle-command.log" 2>&1
  ORACLE_STATUS=$?
  set -e
  printf '%s\n' "$ORACLE_STATUS" >"$ARTIFACT_DIR/oracle-command.exit"
fi

python3 -B "$SCRIPT_DIR/qualification_decision.py" \
  --artifact-dir "$ARTIFACT_DIR" \
  --workload-exit "$WORKLOAD_STATUS" \
  --run-id "$RUN_ID" \
  --seasonal-profile "$SEASONAL_PROFILE_VALUE" \
  $([[ "$PREFLIGHT_ONLY" == "1" ]] && printf '%s' '--preflight' || true)
FINAL_EXIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$ARTIFACT_DIR/final-result.json" 2>/dev/null || printf '%s' "$WORKLOAD_STATUS")"
exit "$FINAL_EXIT"
