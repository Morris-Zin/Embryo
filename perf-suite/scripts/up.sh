#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="khitmee-perf"
FORBIDDEN_PROJECT="khitmee-erpnext"
DEFAULT_SITE_NAME="bench-loadtest.localhost"
DEFAULT_PROFILE="2gb"

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/scripts/up.sh [--profile 2gb|4gb] [--with-khitmee-ops] [--with-optimus] [--with-binlog]

Starts the disposable khitmee-perf benchmark stack on http://127.0.0.1:18086
(or PERF_HTTP_PORT from the env file).
The script never targets the live khitmee-erpnext project.

Options:
  --profile 2gb|4gb   Memory profile to use (default: 2gb)
  --with-khitmee-ops  Sync and mount apps/khitmee_ops, then install it on the site
  --with-optimus      Install the Optimus profiler app into the isolated bench
  --with-binlog       Enable MariaDB ROW binlogging for CDC overhead experiments
  -h, --help          Show this help
EOF_USAGE
}

PROFILE="$DEFAULT_PROFILE"
WITH_KHITMEE_OPS=0
WITH_OPTIMUS=0
WITH_BINLOG=0

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
  2gb|4gb) ;;
  *)
    echo "Invalid --profile '$PROFILE'; expected 2gb or 4gb" >&2
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
if [[ ! "$PERF_HTTP_PORT" =~ ^[0-9]+$ ]]; then
  echo "PERF_HTTP_PORT must be numeric" >&2
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

if [[ "$WITH_KHITMEE_OPS" == "1" ]]; then
  COMPOSE_FILE_ARGS+=( -f "$DOCKER_DIR/khitmee-ops-$PROFILE.yaml" )
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

ensure_khitmee_ops_database_objects() {
  [[ "$WITH_KHITMEE_OPS" == "1" ]] || return 0

  echo "Ensuring khitmee_ops composite indexes and fast report settings..."
  if ! compose exec -T -w /home/frappe/frappe-bench/sites backend /home/frappe/frappe-bench/env/bin/python - "$SITE_NAME" <<'PY_KHITMEE_ENSURE'
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

  echo "Waiting up to ${timeout_seconds}s for InnoDB buffer pool preload..."
  while (( elapsed < timeout_seconds )); do
    if status="$(compose exec -T -e MYSQL_PWD="$DB_PASSWORD" db mariadb -uroot -N -B -e "SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_load_status'" 2>/dev/null | tr -d '\r' | cut -f2- | head -n 1)"; then
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
if [[ "$PROFILE" == "2gb" ]]; then
  python_services+=(queue)
else
  python_services+=(queue-short queue-long)
fi

echo "Starting $PROJECT_NAME with profile $PROFILE..."
compose up -d --remove-orphans
wait_for_service_running backend 180

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

echo "Precompressing changed static assets for gzip_static..."
compose exec -T frontend bash -c 'set -e; shopt -s nullglob; roots=(/home/frappe/frappe-bench/sites/assets/*/dist); if ((${#roots[@]})); then find "${roots[@]}" -type f \( -name "*.js" -o -name "*.css" -o -name "*.map" -o -name "*.json" -o -name "*.svg" -o -name "*.html" -o -name "*.txt" -o -name "*.xml" \) -print0 | while IFS= read -r -d "" file; do if [[ ! -f "$file.gz" || "$file" -nt "$file.gz" ]]; then gzip -9 -n -k -f "$file"; fi; done; fi' < /dev/null

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

echo "$PROJECT_NAME is healthy at http://127.0.0.1:${PERF_HTTP_PORT} (site: $SITE_NAME, profile: $PROFILE)."
