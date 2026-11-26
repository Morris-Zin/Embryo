set -euo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$script_dir/private-docs/local-environment.sh" ]; then
    . "$script_dir/private-docs/local-environment.sh"
fi
app_root=${EMBRYO_APP_ROOT:-/opt/embryo-erpnext}
cd "$app_root"
source_root=${EMBRYO_DEV_SOURCE_ROOT:-/srv/embryo-dev}
source_dir="$source_root/apps/erpnext"
install -d "$source_root/apps"
if [ ! -f "$source_dir/.embryo-source-ready" ]; then
    install -d "$source_dir"
    backend=$(docker compose ps -q backend)
    test -n "$backend"
    docker exec "$backend" tar -C /home/frappe/frappe-bench/apps/erpnext -cf - . | tar -xf - -C "$source_dir"
    test -s "$source_dir/pyproject.toml"
    touch "$source_dir/.embryo-source-ready"
fi
test -s "$source_dir/pyproject.toml"
python3 - <<'PY'
import os
from pathlib import Path

p = Path("dev-source.yaml")
services = ["backend", "frontend", "configurator", "scheduler", "websocket", "queue-short", "queue-long"]
source_root = os.environ.get("EMBRYO_DEV_SOURCE_ROOT", "/srv/embryo-dev")
mount = f"{source_root}/apps/erpnext:/home/frappe/frappe-bench/apps/erpnext"
p.write_text("services:\n" + "".join(f"  {name}:\n    volumes:\n      - {mount}\n" for name in services))
env = Path(".env")
lines = env.read_text().splitlines()
for i, line in enumerate(lines):
    if line.startswith("COMPOSE_FILE=") and ":dev-source.yaml" not in line:
        lines[i] += ":dev-source.yaml"
env.write_text("\n".join(lines) + "\n")
PY
docker compose config --quiet
docker compose up -d
