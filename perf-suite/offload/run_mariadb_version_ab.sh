#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="khitmee-perf"
BACKEND_CONTAINER="khitmee-perf-backend-1"
DB_CONTAINER="khitmee-perf-db-1"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
RESULT_ROOT="$REPO_ROOT/perf-suite/offload/benchmark-evidence"

MARIADB_1189="mariadb:11.8.9@sha256:e1d5495f445654134f226f55a41ce0da227116f6f27f283101f405c794d841fc"
MARIADB_11810="mariadb:11.8.10@sha256:73c35e8483b56390cc98f8d6d9840244bd03c929aea7109490641b9389a18088"

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/offload/run_mariadb_version_ab.sh --run-id ID --snapshot ARCHIVE --company NAME --from-date YYYY-MM-DD --to-date YYYY-MM-DD [options]

Runs an opt-in, digest-pinned MariaDB 11.8.9 versus 11.8.10 A/B against the
same restored khitmee-perf fixture. Each arm restores the snapshot before
measurement, runs scoped Fast Gross Profit at 100/1,000/10,000 invoice parents,
then runs tools/perf_bench_inner.py for the rollback-safe cashier benches.

Options:
  --profile 2gb|2gb-adr  Compose profile (default: 2gb)
  --iterations N         Timed Fast Gross Profit iterations per case (default: 9)
  --warmups N            Excluded warm-up iterations per case (default: 1)
  --warehouse NAME       Optional Fast Gross Profit warehouse scope
EOF_USAGE
}

RUN_ID=""
SNAPSHOT=""
COMPANY=""
FROM_DATE=""
TO_DATE=""
PROFILE="2gb"
ITERATIONS=9
WARMUPS=1
WAREHOUSE=""

while (($#)); do
  case "$1" in
    --run-id) RUN_ID="${2:?missing --run-id value}"; shift 2 ;;
    --run-id=*) RUN_ID="${1#--run-id=}"; shift ;;
    --snapshot) SNAPSHOT="${2:?missing --snapshot value}"; shift 2 ;;
    --snapshot=*) SNAPSHOT="${1#--snapshot=}"; shift ;;
    --company) COMPANY="${2:?missing --company value}"; shift 2 ;;
    --company=*) COMPANY="${1#--company=}"; shift ;;
    --from-date) FROM_DATE="${2:?missing --from-date value}"; shift 2 ;;
    --from-date=*) FROM_DATE="${1#--from-date=}"; shift ;;
    --to-date) TO_DATE="${2:?missing --to-date value}"; shift 2 ;;
    --to-date=*) TO_DATE="${1#--to-date=}"; shift ;;
    --profile) PROFILE="${2:?missing --profile value}"; shift 2 ;;
    --profile=*) PROFILE="${1#--profile=}"; shift ;;
    --iterations) ITERATIONS="${2:?missing --iterations value}"; shift 2 ;;
    --iterations=*) ITERATIONS="${1#--iterations=}"; shift ;;
    --warmups) WARMUPS="${2:?missing --warmups value}"; shift 2 ;;
    --warmups=*) WARMUPS="${1#--warmups=}"; shift ;;
    --warehouse) WAREHOUSE="${2:?missing --warehouse value}"; shift 2 ;;
    --warehouse=*) WAREHOUSE="${1#--warehouse=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$RUN_ID" || -z "$SNAPSHOT" || -z "$COMPANY" || -z "$FROM_DATE" || -z "$TO_DATE" ]]; then
  echo "--run-id, --snapshot, --company, --from-date and --to-date are required" >&2
  exit 2
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Invalid --run-id; use letters, numbers, dot, underscore or dash" >&2
  exit 2
fi
case "$PROFILE" in
  2gb|2gb-adr) ;;
  *) echo "This A/B harness is bounded to 2 GiB profiles: 2gb or 2gb-adr" >&2; exit 2 ;;
esac
if [[ ! -f "$SNAPSHOT" ]]; then
  echo "Snapshot does not exist: $SNAPSHOT" >&2
  exit 2
fi
if [[ ! "$ITERATIONS" =~ ^[0-9]+$ ]] || (( ITERATIONS < 3 || ITERATIONS > 31 )); then
  echo "--iterations must be an integer from 3 to 31" >&2
  exit 2
fi
if [[ ! "$WARMUPS" =~ ^[0-9]+$ ]] || (( WARMUPS > 3 )); then
  echo "--warmups must be an integer from 0 to 3" >&2
  exit 2
fi

if docker inspect "$BACKEND_CONTAINER" >/dev/null 2>&1; then
  project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$BACKEND_CONTAINER" 2>/dev/null || true)"
  if [[ "$project" != "$PROJECT_NAME" ]]; then
    echo "Refusing to run: $BACKEND_CONTAINER is not part of $PROJECT_NAME" >&2
    exit 3
  fi
fi

verify_container_set() {
  local output_file="$1"
  docker ps -a --filter name=khitmee-perf --format '{{.Names}}' | LC_ALL=C sort > "$output_file"
  cat > "$output_file.expected" <<'EOF_EXPECTED'
khitmee-perf-backend-1
khitmee-perf-configurator-1
khitmee-perf-db-1
khitmee-perf-frontend-1
khitmee-perf-queue-1
khitmee-perf-redis-cache-1
khitmee-perf-redis-queue-1
khitmee-perf-scheduler-1
khitmee-perf-websocket-1
EOF_EXPECTED
  if ! diff -u "$output_file.expected" "$output_file" > "$output_file.diff" 2>&1; then
    echo "khitmee-perf container set is not exactly one container per service; see $output_file.diff" >&2
    exit 5
  fi
}

RUN_DIR="$RESULT_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR"
(git rev-parse HEAD 2>/dev/null || echo unknown) > "$RUN_DIR/git-head.txt"
(git status --porcelain 2>/dev/null || echo unavailable) > "$RUN_DIR/git-status-porcelain.txt"
printf '%s\n' "$MARIADB_1189" > "$RUN_DIR/mariadb-11.8.9.ref.txt"
printf '%s\n' "$MARIADB_11810" > "$RUN_DIR/mariadb-11.8.10.ref.txt"

wait_for_ping() {
  local host_header="${SITE_NAME:-bench-loadtest.localhost}"
  local port="${PERF_HTTP_PORT:-18086}"
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 5 -H "Host: $host_header" "http://127.0.0.1:${port}/api/method/ping" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "benchmark site did not answer ping after database restore" >&2
  return 1
}

run_arm() {
  local arm="$1"
  local image_ref="$2"
  local arm_dir="$RUN_DIR/$arm"
  mkdir -p "$arm_dir"

  echo "[$arm] starting stack with $image_ref"
  export MARIADB_IMAGE="$image_ref"
  bash "$REPO_ROOT/perf-suite/scripts/up.sh" --profile "$PROFILE" --with-khitmee-ops --skip-asset-precompress > "$arm_dir/up.log" 2>&1
  verify_container_set "$arm_dir/container-set-after-up.txt"

  echo "[$arm] restoring immutable snapshot $(basename "$SNAPSHOT")"
  bash "$REPO_ROOT/perf-suite/scripts/restore_db_snapshot.sh" "$SNAPSHOT" > "$arm_dir/restore.log" 2>&1
  wait_for_ping

  docker inspect "$DB_CONTAINER" | python3 "$SCRIPT_DIR/sanitize_docker_inspect.py" > "$arm_dir/docker-inspect-db.json"
  docker image inspect "$image_ref" > "$arm_dir/docker-image-inspect.json" 2>/dev/null || true
  docker inspect --format '{{.Id}}' "$DB_CONTAINER" > "$arm_dir/db-container-id.txt"
  docker inspect --format '{{.Config.Image}}' "$DB_CONTAINER" > "$arm_dir/db-config-image.txt"
  docker inspect --format '{{.Image}}' "$DB_CONTAINER" > "$arm_dir/db-image-id.txt"

  docker cp "$SCRIPT_DIR/fast_gp_invoice_scaling.py" "$BACKEND_CONTAINER:/tmp/fast_gp_invoice_scaling.py" >/dev/null
  local remote_json="/tmp/fast-gp-invoice-scaling-$arm.json"
  local extra_args=()
  if [[ -n "$WAREHOUSE" ]]; then
    extra_args+=(--warehouse "$WAREHOUSE")
  fi

  echo "[$arm] measuring scoped Fast Gross Profit"
  docker exec -i -e PYTHONDONTWRITEBYTECODE=1 \
    -w /home/frappe/frappe-bench/sites \
    "$BACKEND_CONTAINER" \
    /home/frappe/frappe-bench/env/bin/python /tmp/fast_gp_invoice_scaling.py \
    --arm "$arm" \
    --company "$COMPANY" \
    --from-date "$FROM_DATE" \
    --to-date "$TO_DATE" \
    --iterations "$ITERATIONS" \
    --warmups "$WARMUPS" \
    "${extra_args[@]}" \
    --output "$remote_json" \
    > "$arm_dir/fastgp-stdout.json"
  docker cp "$BACKEND_CONTAINER:$remote_json" "$arm_dir/fastgp-invoice-scaling.json" >/dev/null
  docker exec --user root "$BACKEND_CONTAINER" rm -f "$remote_json" /tmp/fast_gp_invoice_scaling.py 2>/dev/null || true

  echo "[$arm] measuring rollback-safe repeated cashier benchmarks"
  docker cp "$SCRIPT_DIR/cashier_sale_repeat.py" "$BACKEND_CONTAINER:/tmp/cashier_sale_repeat.py" >/dev/null
  local remote_cashier_json="/tmp/cashier-sale-repeat-$arm.json"
  docker exec -i -e PYTHONDONTWRITEBYTECODE=1 \
    -w /home/frappe/frappe-bench/sites \
    "$BACKEND_CONTAINER" \
    /home/frappe/frappe-bench/env/bin/python /tmp/cashier_sale_repeat.py \
    --arm "$arm" \
    --iterations 5 \
    --warmups 1 \
    --output "$remote_cashier_json" \
    > "$arm_dir/cashier-sale-repeat.stdout.json"
  docker cp "$BACKEND_CONTAINER:$remote_cashier_json" "$arm_dir/cashier-sale-repeat.json" >/dev/null
  docker exec --user root "$BACKEND_CONTAINER" rm -f "$remote_cashier_json" /tmp/cashier_sale_repeat.py 2>/dev/null || true
}

verify_container_set "$RUN_DIR/container-set-before.txt"

run_arm "mariadb-11.8.9" "$MARIADB_1189"
run_arm "mariadb-11.8.10" "$MARIADB_11810"
verify_container_set "$RUN_DIR/container-set-after.txt"
unset MARIADB_IMAGE

python3 - "$RUN_DIR" <<'PY'
import json
from pathlib import Path
import sys

run_dir = Path(sys.argv[1])
arms = {}
for arm_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
    fastgp_path = arm_dir / "fastgp-invoice-scaling.json"
    cashier_path = arm_dir / "cashier-sale-repeat.json"
    if not cashier_path.exists():
        cashier_path = arm_dir / "cashier-perf-bench.json"
    if not fastgp_path.exists() or not cashier_path.exists():
        continue
    fastgp = json.loads(fastgp_path.read_text(encoding="utf-8"))
    cashier = json.loads(cashier_path.read_text(encoding="utf-8"))
    cashier_cases = cashier.get("cases") or cashier.get("benchmarks", {})
    arms[arm_dir.name] = {
        "database_variables": fastgp.get("database_variables"),
        "buffer_pool_status_before_cases": fastgp.get("buffer_pool_status_before_cases"),
        "invoice_scope_sha256": fastgp.get("invoice_scope_sha256"),
        "fastgp": fastgp.get("cases"),
        "cashier": {
            key: cashier_cases.get(key)
            for key in ("sale_10_lines", "sale_50_lines")
        },
    }

comparison = {"arms": arms, "fastgp_deltas": {}, "parity": {"passed": True, "mismatches": []}}
preferred = ["mariadb-11.8.9", "mariadb-11.8.10"]
names = [name for name in preferred if name in arms] + sorted(set(arms) - set(preferred))
if len(names) == 2:
    baseline, candidate = names
    if arms[baseline].get("invoice_scope_sha256") != arms[candidate].get("invoice_scope_sha256"):
        comparison["parity"]["passed"] = False
        comparison["parity"]["mismatches"].append("invoice_scope_sha256")
    for count in sorted((arms[baseline].get("fastgp") or {}).keys(), key=int):
        left = arms[baseline]["fastgp"].get(count) or {}
        right = arms[candidate]["fastgp"].get(count) or {}
        if left.get("row_hash_sha256") != right.get("row_hash_sha256"):
            comparison["parity"]["passed"] = False
            comparison["parity"]["mismatches"].append(f"fastgp:{count}:row_hash_sha256")
        if left.get("p95_ms") is not None and right.get("p95_ms") is not None:
            comparison["fastgp_deltas"][count] = {
                "baseline_arm": baseline,
                "candidate_arm": candidate,
                "baseline_p95_ms": left.get("p95_ms"),
                "candidate_p95_ms": right.get("p95_ms"),
                "delta_ms": round(right["p95_ms"] - left["p95_ms"], 3),
                "delta_pct": round(((right["p95_ms"] - left["p95_ms"]) / left["p95_ms"]) * 100.0, 2) if left["p95_ms"] else None,
            }

(out := run_dir / "ab-comparison.json").write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({"comparison": str(out), "parity_passed": comparison["parity"]["passed"]}, sort_keys=True))
if not comparison["parity"]["passed"]:
    raise SystemExit(4)
PY

echo "MariaDB version A/B complete: $RUN_DIR/ab-comparison.json"
