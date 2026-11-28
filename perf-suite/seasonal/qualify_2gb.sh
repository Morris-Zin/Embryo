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

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="qualify-${RUNTIME_PROFILE}-${SEASONAL_PROFILE_VALUE}-${STAMP}"
ARTIFACT_DIR="$PERF_ROOT/results/${RUN_ID}"
mkdir -p "$ARTIFACT_DIR"
exec > >(tee -a "$ARTIFACT_DIR/qualify.log") 2>&1

echo "[$STAMP] seasonal qualification run $RUN_ID"
echo "Artifacts: $ARTIFACT_DIR"

LEASE_TOKEN=""
cleanup() {
  status=$?
  if [[ -n "$LEASE_TOKEN" ]]; then
    python3 -B "$REPO_ROOT/tools/autoloop/cli.py" lease release --resource khitmee-perf --owner "$OWNER" --token "$LEASE_TOKEN" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

LEASE_JSON="$ARTIFACT_DIR/lease.json"
if ! python3 -B "$REPO_ROOT/tools/autoloop/cli.py" lease acquire --resource khitmee-perf --owner "$OWNER" --ttl 7200 --holder-pid $$ > "$LEASE_JSON"; then
  echo "NO-GO: khitmee-perf lease is unavailable" >&2
  exit 5
fi
LEASE_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$LEASE_JSON")"
echo "Acquired khitmee-perf lease as $OWNER"

# Refuse to touch the live project; down.sh performs its own live-state verification.
bash "$PERF_ROOT/scripts/down.sh" >"$ARTIFACT_DIR/down.log" 2>&1

echo "Restoring pinned fixture $FIXTURE_SHA"
python3 -B "$SCRIPT_DIR/fixture_restore.py" "$FIXTURE" \
  --expected-sha256 "$FIXTURE_SHA" \
  --docker-dir "$DOCKER_DIR" \
  --output "$ARTIFACT_DIR/fixture-restore.json" | tee "$ARTIFACT_DIR/fixture-restore.stdout"

echo "Starting isolated stack with khitmee_ops"
bash "$PERF_ROOT/scripts/up.sh" --profile 2gb --with-khitmee-ops >"$ARTIFACT_DIR/up.log" 2>&1

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
    "$@"
}

echo "Applying runtime profile $RUNTIME_PROFILE"
# Apply cgroup caps in place. Recreating the one-shot configurator with compose
# can hit a Docker Engine race ("No such container") after it exits; cgroup
# updates are the effective constraint being proven and avoid that race.
while IFS='=' read -r service cpus; do
  docker update --cpus="$cpus" "khitmee-perf-${service}-1" >/dev/null
done <<EOF_CAPS
db=0.70
backend=0.75
queue=0.15
scheduler=0.05
redis-cache=0.05
redis-queue=0.05
websocket=0.05
frontend=0.15
configurator=0.05
EOF_CAPS
docker update --memory=48m --memory-swap=48m khitmee-perf-configurator-1 >/dev/null
if [[ "$RUNTIME_PROFILE" == "gunicorn-2x1" ]]; then
  compose up -d --no-deps backend >"$ARTIFACT_DIR/runtime-up.log" 2>&1
fi

SITE_NAME_VALUE="$(python3 - <<'PY'
import os
for line in open('perf-suite/docker/.env', encoding='utf-8'):
    if line.startswith('SITE_NAME='):
        print(line.strip().split('=',1)[1]); break
else:
    print('bench-loadtest.localhost')
PY
)"
for _ in $(seq 1 90); do
  if curl -fsS --max-time 5 -H "Host: $SITE_NAME_VALUE" http://127.0.0.1:18086/api/method/ping >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS --max-time 10 -H "Host: $SITE_NAME_VALUE" http://127.0.0.1:18086/api/method/ping >/dev/null

echo "Verifying effective container resource limits"
python3 -B "$SCRIPT_DIR/resource_proof.py" --output "$ARTIFACT_DIR/resource-proof-before.json"

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

echo "Running seasonal $SEASONAL_PROFILE_VALUE workload"
set +e
bash "$PERF_ROOT/k6/run_k6.sh" seasonal | tee "$ARTIFACT_DIR/workload.log"
WORKLOAD_STATUS=${PIPESTATUS[0]}
set -e

python3 -B "$SCRIPT_DIR/resource_proof.py" --output "$ARTIFACT_DIR/resource-proof-after.json" || true
cp -f "$PERF_ROOT/results/${RUN_ID}-scorecard.md" "$ARTIFACT_DIR/scorecard.md" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-seasonal-verification.json" "$ARTIFACT_DIR/seasonal-verification.json" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-container-stats.csv" "$ARTIFACT_DIR/container-stats.csv" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-k6-summary.json" "$ARTIFACT_DIR/k6-summary.json" 2>/dev/null || true
cp -f "$PERF_ROOT/results/${RUN_ID}-k6.log" "$ARTIFACT_DIR/k6.log" 2>/dev/null || true

python3 - "$ARTIFACT_
DIR" "$WORKLOAD_STATUS" <<'PY'
import json, pathlib, sys
artifact = pathlib.Path(sys.argv[1])
status = int(sys.argv[2])
verification = {}
proof = {}
try:
    verification = json.loads((artifact / 'seasonal-verification.json').read_text())
except Exception:
    pass
try:
    proof = json.loads((artifact / 'resource-proof-before.json').read_text())
except Exception:
    pass
duplicates = sum((row or {}).get('duplicates', 0) for row in (verification.get('by_type') or {}).values())
oom = [row.get('service') for row in proof.get('services') or [] if row.get('oom_killed')]
restarts = [
    {'service': row.get('service'), 'restart_count': row.get('restart_count')}
    for row in proof.get('services') or [] if row.get('restart_count')
]
result = {
    'verdict': 'GO' if status == 0 else 'NO-GO',
    'workload_exit': status,
    'duplicates': duplicates,
    'docker_oom_killed_before_run': oom,
    'restart_counts_before_run': restarts,
    'scorecard': str(artifact / 'scorecard.md'),
    'resource_proof': str(artifact / 'resource-proof-before.json'),
}
(artifact / 'final-result.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
print('FINAL ' + result['verdict'] + ' duplicates=' + str(duplicates) + ' workload_exit=' + str(status))
print('final result: ' + str(artifact / 'final-result.json'))
PY
exit "$WORKLOAD_STATUS"
