#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="khitmee-perf"
FORBIDDEN_PROJECT="khitmee-erpnext"
DEFAULT_SITE_NAME="bench-loadtest.localhost"
DEFAULT_PROFILE="2gb"

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/scripts/up.sh [--profile 2gb|2gb-adr|2gb-guest|4gb] [--with-khitmee-ops] [--with-optimus] [--with-binlog]

Starts the disposable khitmee-perf benchmark stack on http://127.0.0.1:18086
(or PERF_HTTP_PORT from the env file).
The script never targets the live khitmee-erpnext project.

Options:
  --profile PROFILE   Memory profile: 2gb, 2gb-adr, 2gb-guest, or 4gb (default: 2gb)
  --with-khitmee-ops  Sync and mount apps/khitmee_ops, then install it on the site
  --with-optimus      Install the Optimus profiler app into the isolated bench
  --with-binlog       Enable MariaDB ROW binlogging for CDC overhead experiments
  --skip-asset-precompress  Skip optional static gzip precompression for API-only benchmark runs
  -h, --help          Show this help

MARIADB_IMAGE may be exported or set in the local .env to a digest-pinned image
for an opt-in database A/B run. The default mariadb:11.8 image is unchanged.
For --profile 2gb-guest, CASHIER_GUNICORN_WORKERS/CASHIER_GUNICORN_THREADS
and GENERAL_GUNICORN_WORKERS may be exported or set in .env; ADR defaults are
1x3 gthread cashier workers/threads and one sync general worker.
EOF_USAGE
}

PROFILE="$DEFAULT_PROFILE"
WITH_KHITMEE_OPS=0
WITH_OPTIMUS=0
WITH_BINLOG=0
SKIP_ASSET_PRECOMPRESS=0

while (($#)); do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || { echo "Missing value for --profile" >&2; exit 2; }
      PROFILE="$2"
      shift 2
      ;;
    --profile=*)
      PROFILE="${1#--profile=}"
      shift
      ;;
    --with-khitmee-ops)
      WITH_KHITMEE_OPS=1
      shift
      ;;
    --with-optimus)
      WITH_OPTIMUS=1
      shift
      ;;
    --with-binlog)
      WITH_BINLOG=1
      shift
      ;;
    --skip-asset-precompress)
      SKIP_ASSET_PRECOMPRESS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$PROFILE" in
  2gb|2gb-adr|2gb-guest|4gb) ;;
  *)
    echo "Invalid --profile '$PROFILE'; expected 2gb, 2gb-adr, 2gb-guest, or 4gb" >&2
    exit 2
    ;;
esac

if [[ "$WITH_BINLOG" == "1" && "$PROFILE" != "2gb" ]]; then
  echo "--with-binlog currently mirrors only the 2gb MariaDB command; use --profile 2gb" >&2
  exit 2
fi

if [[ "$PROJECT_NAME" == "$FORBIDDEN_PROJECT" ]]; then
  echo "Refusing to target the live $FORBIDDEN_PROJECT project" >&2
  exit 2
fi

if [[ -n "${COMPOSE_PROJECT_NAME:-}" && "$COMPOSE_PROJECT_NAME" != "$PROJECT_NAME" ]]; then
  echo "Refusing to start: COMPOSE_PROJECT_NAME is '$COMPOSE_PROJECT_NAME', expected '$PROJECT_NAME'" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PERF_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd -- "$PERF_DIR/.." && pwd)"
DOCKER_DIR="$PERF_DIR/docker"
ENV_FILE="$DOCKER_DIR/.env"

random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
    printf '\n'
  fi
}

generate_env_file() {
  local db_password admin_password
  db_password="$(random_hex)"
  admin_password="$(random_hex)"

  (umask 077; cat > "$ENV_FILE" <<EOF_ENV
COMPOSE_PROJECT_NAME=khitmee-perf
PULL_POLICY=missing
CUSTOM_IMAGE=frappe/erpnext
CUSTOM_TAG=v16.60.0
SITE_NAME=bench-loadtest.localhost
DB_PASSWORD=$db_password
ADMIN_PASSWORD=$admin_password
# Optional digest-pinned A/B override, for example:
# MARIADB_IMAGE=mariadb:11.8.10@sha256:73c35e8483b56390cc98f8d6d9840244bd03c929aea7109490641b9389a18088
EOF_ENV
  )
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  echo "Created local $ENV_FILE (not committed; passwords are never printed)."
}

if [[ ! -f "$ENV_FILE" ]]; then
  generate_env_file
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [[ -n "${COMPOSE_PROJECT_NAME:-}" && "$COMPOSE_PROJECT_NAME" != "$PROJECT_NAME" ]]; then
  echo "Refusing to start: $ENV_FILE sets COMPOSE_PROJECT_NAME='$COMPOSE_PROJECT_NAME'" >&2
  exit 2
fi
export COMPOSE_PROJECT_NAME="$PROJECT_NAME"

: "${DB_PASSWORD:?DB_PASSWORD is missing in $ENV_FILE}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is missing in $ENV_FILE}"
SITE_NAME="${SITE_NAME:-$DEFAULT_SITE_NAME}"
PERF_HTTP_PORT="${PERF_HTTP_PORT:-18086}"
if [[ ! "$PERF_HTTP_PORT" =~ ^[0-9]+$ ]] || (( PERF_HTTP_PORT < 1024 || PERF_HTTP_PORT > 65535 )); then
  echo "PERF_HTTP_PORT must be a port between 1024 and 65535" >&2
  exit 2
fi
site_name_lower="${SITE_NAME,,}"
if [[ "$site_name_lower" == "khitmee.localhost" || "$site_name_lower" == *.khitmee.localhost ]]; then
  echo "Refusing to use live-stack site name $SITE_NAME in the benchmark" >&2
  exit 2
fi

COMPOSE_FILE_ARGS=(
  -f "$DOCKER_DIR/compose.base.yaml"
  -f "$DOCKER_DIR/profile-$PROFILE.yaml"
)

OPS_OVERLAY_PROFILE="$PROFILE"
if [[ "$PROFILE" == 2gb* ]]; then
  OPS_OVERLAY_PROFILE="2gb"
fi

if [[ "$WITH_KHITMEE_OPS" == "1" ]]; then
  COMPOSE_FILE_ARGS+=( -f "$DOCKER_DIR/khitmee-ops-$OPS_OVERLAY_PROFILE.yaml" )
  if [[ "$PROFILE" == "2gb-guest" ]]; then
    COMPOSE_FILE_ARGS+=( -f "$DOCKER_DIR/khitmee-ops-guest.yaml" )
  fi
fi

if [[ "$WITH_BINLOG" == "1" ]]; then
  COMPOSE_FILE_ARGS+=( -f "$DOCKER_DIR/compose.binlog.yaml" )
fi

OPTIMUS_REPO="${OPTIMUS_REPO:-https://github.com/Aerele-RnD/optimus.git}"
OPTIMUS_REF="${OPTIMUS_REF:-main}"

compose() {
  docker compose \
    --project-name "$PROJECT_NAME" \
    --project-directory "$DOCKER_DIR" \
    --env-file "$ENV_FILE" \
    "${COMPOSE_FILE_ARGS[@]}" \
    "$@"
}

compose_transient() {
  docker compose \
    --project-name "$PROJECT_NAME" \
    --project-directory "$DOCKER_DIR" \
    --env-file "$ENV_FILE" \
    "${COMPOSE_FILE_ARGS[@]}" \
    -f "$DOCKER_DIR/khitmee-migration-transient.yaml" \
    "$@"
}

sync_khitmee_ops() {
  local source_dir="$REPO_ROOT/apps/khitmee_ops"
  local target_dir="/srv/khitmee-perf/apps/khitmee_ops"

  if [[ ! -d "$source_dir" ]]; then
    if [[ "$WITH_KHITMEE_OPS" == "1" ]]; then
      echo "--with-khitmee-ops was requested, but $source_dir does not exist" >&2
      exit 2
    fi
    return 0
  fi

  if [[ "$target_dir" != "/srv/khitmee-perf/apps/khitmee_ops" ]]; then
    echo "Unsafe khitmee_ops sync target: $target_dir" >&2
    exit 2
  fi

  mkdir -p "$(dirname "$target_dir")"
  local rsync_args=(
    -a --delete --checksum
    --no-owner --no-group
    --chmod=D755,F644
    --exclude='.git/'
    --exclude='__pycache__/'
    --exclude='*.pyc'
    --exclude='node_modules/'
    --exclude='.venv/'
  )
  local changes
  changes="$(rsync "${rsync_args[@]}" --dry-run --itemize-changes "$source_dir/" "$target_dir/" 2>/dev/null || true)"
  if [[ -n "$changes" ]]; then
    KHITMEE_OPS_SYNC_CHANGED=1
  else
    KHITMEE_OPS_SYNC_CHANGED=0
  fi
  rsync "${rsync_args[@]}" "$source_dir/" "$target_dir/"
  KHITMEE_OPS_TREE_HASH="$(find "$target_dir" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')"
  if [[ "$(id -u)" == "0" ]]; then
    chown -R 1000:0 "$target_dir"
  fi
  echo "Synced khitmee_ops into /srv/khitmee-perf (WSL ext4)."
}

wait_for_service_running() {
  local service="$1"
  local timeout_seconds="${2:-120}"
  local elapsed=0
  local container_id state

  while (( elapsed < timeout_seconds )); do
    container_id="$(compose ps -q "$service" 2>/dev/null | tr -d '\r' || true)"
    if [[ -n "$container_id" ]]; then
      state="$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)"
      if [[ "$state" == "true" ]]; then
        return 0
      fi
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  echo "Timed out waiting for $service to run" >&2
  compose ps || true
  return 1
}

site_has_app() {
  local app="$1"
  compose exec -T backend bench --site "$SITE_NAME" list-apps --format text < /dev/null 2>/dev/null | awk -v app="$app" '$1 == app { found=1 } END { exit !found }'
}

read_khitmee_ops_migrated_tree_hash() {
  compose_transient run --rm --no-deps -T -w /home/frappe/frappe-bench/sites backend /home/frappe/frappe-bench/env/bin/python - "$SITE_NAME" <<'PY' 2>/dev/null
import sys

import frappe

frappe.init(site=sys.argv[1], sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
try:
    try:
        rows = frappe.db.sql("SELECT tree_hash FROM `__khitmee_ops_migration_state` WHERE name='state'")
    except Exception:
        rows = []
    print(rows[0][0] if rows else "")
finally:
    frappe.destroy()
PY
}

record_khitmee_ops_migrated_tree_hash() {
  local tree_hash="$1"
  compose_transient run --rm --no-deps -T -w /home/frappe/frappe-bench/sites backend /home/frappe/frappe-bench/env/bin/python - "$SITE_NAME" "$tree_hash" <<'PY'
import sys

import frappe

frappe.init(site=sys.argv[1], sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
try:
    frappe.db.sql("""
        CREATE TABLE IF NOT EXISTS `__khitmee_ops_migration_state` (
          `name` varchar(32) NOT NULL PRIMARY KEY,
          `tree_hash` varchar(64) NOT NULL,
          `migrated_at` datetime(6) NOT NULL
        ) ENGINE=InnoDB
    """)
    frappe.db.sql(
        """
        INSERT INTO `__khitmee_ops_migration_state` (`name`, `tree_hash`, `migrated_at`)
        VALUES ('state', %(tree_hash)s, UTC_TIMESTAMP(6))
        ON DUPLICATE KEY UPDATE `tree_hash`=VALUES(`tree_hash`), `migrated_at`=VALUES(`migrated_at`)
        """,
        {"tree_hash": sys.argv[2]},
    )
    frappe.db.commit()
finally:
    frappe.destroy()
PY
}

run_khitmee_ops_migration_if_needed() {
  [[ "$WITH_KHITMEE_OPS" == "1" ]] || return 0

  local evidence_file="/srv/khitmee-perf/startup-migrations.log"
  local occurred_at status tree_hash migrated_hash
  tree_hash="${KHITMEE_OPS_TREE_HASH:-unknown}"
  migrated_hash="$(read_khitmee_ops_migrated_tree_hash | tail -n 1 | tr -d '\r')"
  if [[ -n "$tree_hash" && "$tree_hash" != "unknown" && "$migrated_hash" == "$tree_hash" ]]; then
    echo "khitmee_ops migrations already match deployed tree hash $tree_hash."
    return 0
  fi

  occurred_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$(dirname "$evidence_file")"

  echo "khitmee_ops deployed tree differs from the fixture migration state; running bench migrate outside the measured window..."
  status=success
  if ! compose_transient run --rm --no-deps -T backend bench --site "$SITE_NAME" migrate < /dev/null; then
    status=migrate_failed
  elif [[ "$tree_hash" != "unknown" ]] && ! record_khitmee_ops_migrated_tree_hash "$tree_hash" < /dev/null; then
    status=marker_failed
  fi
  printf '{"at":"%s","site":"%s","profile":"%s","tree_sha256":"%s","prior_tree_sha256":"%s","reason":"fixture migration state mismatch","status":"%s"}\n' \
    "$occurred_at" "$SITE_NAME" "$PROFILE" "$tree_hash" "$migrated_hash" "$status" >> "$evidence_file"

  if [[ "$status" != "success" ]]; then
    echo "bench migrate or migration-state recording failed after khitmee_ops changed; see $evidence_file" >&2
    return 1
  fi
  echo "Recorded startup migration in $evidence_file"
}

ensure_khitmee_ops_database_objects() {
  [[ "$WITH_KHITMEE_OPS" == "1" ]] || return 0

  echo "Ensuring khitmee_ops composite indexes and fast report settings..."
  if ! compose_transient run --rm --no-deps -T -w /home/frappe/frappe-bench/sites backend /home/frappe/frappe-bench/env/bin/python - "$SITE_NAME" <<'PY_KHITMEE_ENSURE'
import sys

import frappe
from khitmee_ops.setup import indexes

site = sys.argv[1]
frappe.init(site=site, sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
try:
    ensure_all = getattr(indexes, "ensure_all", None)
    if ensure_all is not None:
        ensure_all()
    else:
        indexes.setup_database_objects(rebuild_item_search=False)
    if indexes.sync_fast_report_settings() is False:
        raise RuntimeError("sync_fast_report_settings returned False")
    frappe.db.commit()
finally:
    frappe.destroy()

print("khitmee_ops database objects ensured and fast report settings are enabled")
PY_KHITMEE_ENSURE
  then
    echo "khitmee_ops.setup.indexes.ensure_all or fast report settings sync failed; refusing to start without required indexes/settings." >&2
    exit 1
  fi
}

wait_for_url() {
  local url="$1"
  local host_header="$2"
  local attempts="${3:-60}"
  local attempt

  for ((attempt=1; attempt<=attempts; attempt++)); do
    if curl -fsS --max-time 3 -H "Host: $host_header" "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_innodb_buffer_pool_preload() {
  local timeout_seconds=30
  local elapsed=0
  local status=""
  local last_status="__unset__"
  local printable_status

  local auth_user="root"
  local auth_password="$DB_PASSWORD"
  local site_auth=""
  if ! compose exec -T -e MYSQL_PWD="$DB_PASSWORD" db mariadb -uroot -N -B -e "SELECT 1" >/dev/null 2>&1; then
    if site_auth="$(compose exec -T backend python -c "import json; c=json.load(open('/home/frappe/frappe-bench/sites/$SITE_NAME/site_config.json')); print(str(c.get('db_name') or c.get('db_user') or '')+' '+str(c.get('db_password') or ''))" 2>/dev/null | tr -d '\r' | head -n 1)"; then
      auth_user="${site_auth%% *}"
      auth_password="${site_auth#* }"
    fi
  fi

  echo "Waiting up to ${timeout_seconds}s for InnoDB buffer pool preload..."
  while (( elapsed < timeout_seconds )); do
    if [[ -n "$auth_user" && -n "$auth_password" ]] && status="$(compose exec -T -e MYSQL_PWD="$auth_password" db mariadb -u"$auth_user" -N -B -e "SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_load_status'" 2>/dev/null | tr -d '\r' | cut -f2- | head -n 1)"; then
      printable_status="${status:-no load in progress}"
      if [[ "$printable_status" != "$last_status" ]]; then
        echo "InnoDB buffer pool load status: $printable_status"
        last_status="$printable_status"
      fi
      local status_lower="${status,,}"
      if [[ -z "$status" || "$status_lower" == *"load completed"* || "$status_lower" == *"not started"* ]]; then
        return 0
      fi
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  echo "Timed out waiting for InnoDB buffer pool preload to complete; last status: ${last_status/__unset__/unavailable}" >&2
  return 1
}

warmup_cookie_jar=""
cleanup_warmup_files() {
  if [[ -n "$warmup_cookie_jar" ]]; then
    rm -f -- "$warmup_cookie_jar"
    warmup_cookie_jar=""
  fi
}

warmup_read_only_http() {
  local base_url="http://127.0.0.1:${PERF_HTTP_PORT}"
  local ping_url="$base_url/api/method/ping"
  local attempt

  echo "Performing read-only HTTP warm-up..."
  for ((attempt=1; attempt<=4; attempt++)); do
    if ! curl -fsS --max-time 5 -H "Host: $SITE_NAME" "$ping_url" >/dev/null; then
      echo "Warm-up ping attempt $attempt failed; continuing." >&2
    fi
  done

  if [[ "$WITH_KHITMEE_OPS" == "1" && -n "${ADMIN_PASSWORD:-}" ]]; then
    warmup_cookie_jar="$(mktemp)"
    trap cleanup_warmup_files EXIT
    chmod 600 "$warmup_cookie_jar" 2>/dev/null || true

    if ! printf '%s' "$ADMIN_PASSWORD" | curl -fsS --max-time 10 -X POST \
      -H "Host: $SITE_NAME" \
      -c "$warmup_cookie_jar" \
      --data-urlencode "usr=Administrator" \
      --data-urlencode "pwd@-" \
      "$base_url/api/method/login" >/dev/null; then
      echo "Warm-up login failed; skipping authenticated search warm-up." >&2
      cleanup_warmup_files
      return 0
    fi

    local search_url="$base_url/api/method/khitmee_ops.api.search.search_items?q=KM-00001&warehouse=Shop%201%20-%20KMD&price_list=Standard%20Selling&limit=5"
    for ((attempt=1; attempt<=2; attempt++)); do
      if ! curl -fsS --max-time 10 \
        -H "Host: $SITE_NAME" \
        -b "$warmup_cookie_jar" \
        "$search_url" >/dev/null; then
        echo "Warm-up search attempt $attempt failed; continuing." >&2
      fi
    done
    cleanup_warmup_files
  fi
}

sync_khitmee_ops

python_services=(backend scheduler)
if [[ "$PROFILE" == "2gb-guest" ]]; then
  python_services+=(cashier)
fi
if [[ "$PROFILE" == 2gb* ]]; then
  python_services+=(queue)
else
  python_services+=(queue-short queue-long)
fi

if [[ "$PROFILE" == "2gb-guest" ]]; then
  echo "Ensuring whole-guest parent cgroup before container creation..."
  bash "$SCRIPT_DIR/guest_cgroup_setup.sh"
fi

echo "Starting $PROJECT_NAME with profile $PROFILE..."
compose up -d --remove-orphans
wait_for_service_running backend 180
if [[ "$PROFILE" == "2gb-guest" ]]; then
  wait_for_service_running cashier 180
fi

if [[ "$WITH_KHITMEE_OPS" == "1" ]]; then
  if [[ "${KHITMEE_OPS_SYNC_CHANGED:-0}" == "1" ]]; then
    echo "khitmee_ops source changed; restarting Python services to discard stale modules..."
    for service in "${python_services[@]}"; do
      compose restart "$service"
      wait_for_service_running "$service" 120
    done
  fi
  for service in "${python_services[@]}"; do
    compose exec -T "$service" /home/frappe/frappe-bench/env/bin/python -c "import khitmee_ops" < /dev/null
    echo "Verified khitmee_ops imports in $service through the mounted PYTHONPATH."
  done
fi

if compose exec -T backend test -d "sites/$SITE_NAME" < /dev/null; then
  echo "Site $SITE_NAME already exists; reusing it."
else
  echo "Creating disposable site $SITE_NAME..."
  compose exec -T backend bench new-site \
    --mariadb-user-host-login-scope=% \
    --db-root-password "$DB_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD" \
    --install-app erpnext \
    "$SITE_NAME" < /dev/null
fi

compose exec -T backend bench --site "$SITE_NAME" set-config developer_mode 0 < /dev/null

if [[ "$WITH_KHITMEE_OPS" == "1" ]]; then
  if ! site_has_app khitmee_ops; then
    echo "Installing khitmee_ops on $SITE_NAME..."
    compose exec -T backend bench --site "$SITE_NAME" install-app khitmee_ops < /dev/null
  else
    echo "khitmee_ops is already installed on $SITE_NAME."
  fi
  run_khitmee_ops_migration_if_needed
  ensure_khitmee_ops_database_objects
fi

if [[ "$WITH_OPTIMUS" == "1" ]]; then
  for service in "${python_services[@]}"; do
    echo "Installing Optimus Python code and requirements in isolated service $service..."
    compose exec -T \
      -e OPTIMUS_REPO="$OPTIMUS_REPO" \
      -e OPTIMUS_REF="$OPTIMUS_REF" \
      "$service" bash -lc '
        set -euo pipefail
        cd /home/frappe/frappe-bench
        if [ ! -d apps/optimus ]; then
          if [ -n "$OPTIMUS_REF" ]; then
            bench get-app --branch "$OPTIMUS_REF" "$OPTIMUS_REPO"
          else
            bench get-app "$OPTIMUS_REPO"
          fi
        fi
        bench setup requirements
      ' < /dev/null
  done

  if ! site_has_app optimus; then
    echo "Installing Optimus on $SITE_NAME..."
    compose exec -T backend bench --site "$SITE_NAME" install-app optimus < /dev/null
  fi

  for service in "${python_services[@]}"; do
    compose restart "$service"
    wait_for_service_running "$service" 120
  done
fi

echo "Setting benchmark site defaults..."
compose exec -T backend bench --site "$SITE_NAME" execute frappe.db.set_single_value --args "('Print Settings','pdf_generator','PlutoPrint')" < /dev/null
compose exec -T backend bench --site "$SITE_NAME" execute frappe.db.set_single_value --args "('Global Defaults','enable_item_search_index',1)" < /dev/null
compose exec -T backend bench --site "$SITE_NAME" scheduler enable < /dev/null
compose exec -T backend bench --site "$SITE_NAME" scheduler resume < /dev/null

if [[ "$SKIP_ASSET_PRECOMPRESS" == "1" ]]; then
  echo "Skipping optional static asset precompression for this API-only benchmark run."
else
  echo "Precompressing changed static assets for gzip_static..."
  compose exec -T frontend bash -c 'set -e; shopt -s nullglob; roots=(/home/frappe/frappe-bench/sites/assets/*/dist); if ((${#roots[@]})); then find "${roots[@]}" -type f \( -name "*.js" -o -name "*.css" -o -name "*.map" -o -name "*.json" -o -name "*.svg" -o -name "*.html" -o -name "*.txt" -o -name "*.xml" \) -print0 | while IFS= read -r -d "" file; do if [[ ! -f "$file.gz" || "$file" -nt "$file.gz" ]]; then gzip -9 -n -k -f "$file"; fi; done; fi' < /dev/null
fi

health_url="http://127.0.0.1:${PERF_HTTP_PORT}/api/method/ping"
echo "Waiting for $health_url ..."
if ! wait_for_url "$health_url" "$SITE_NAME" 60; then
  echo "Health check failed for $health_url" >&2
  compose ps || true
  exit 1
fi

if ! wait_for_innodb_buffer_pool_preload; then
  echo "Refusing to run HTTP warm-up while InnoDB buffer pool preload is still active." >&2
  exit 1
fi

warmup_read_only_http

if [[ "$PROFILE" == "2gb-guest" ]]; then
  echo "Verifying whole-guest cgroup limits and container process membership..."
  bash "$SCRIPT_DIR/verify_guest_cgroup.sh"
fi

echo "$PROJECT_NAME is healthy at http://127.0.0.1:${PERF_HTTP_PORT} (site: $SITE_NAME, profile: $PROFILE)."
