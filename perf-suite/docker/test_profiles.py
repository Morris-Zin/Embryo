#!/usr/bin/env python3
"""Offline resource-budget checks for the isolated perf Compose profiles.

The trial 2 GiB budget reserves 480 MiB for the VPS operating system, container
runtime, page cache and operational headroom. That leaves 1568 MiB for the
steady-state application containers. The short-lived configurator is bounded
separately because dependent services wait for service_completed_successfully;
even if every container were somehow present at once, the profile must still
leave at least 256 MiB for the kernel and runtime.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
import unittest

DOCKER_DIR = Path(__file__).resolve().parent
TOTAL_MEMORY_MIB = 2048
OS_HEADROOM_MIB = 480
STEADY_STATE_MEMORY_BUDGET_MIB = TOTAL_MEMORY_MIB - OS_HEADROOM_MIB
ABSOLUTE_MINIMUM_HEADROOM_MIB = 256
ALL_CONTAINER_MEMORY_CEILING_MIB = TOTAL_MEMORY_MIB - ABSOLUTE_MINIMUM_HEADROOM_MIB
CPU_BUDGET = 2.0
TRANSIENT_CONFIGURATOR_BUDGET_MIB = 64

GUEST_SERVICES = {
    "configurator",
    "db",
    "backend",
    "cashier",
    "queue",
    "scheduler",
    "redis-cache",
    "redis-queue",
    "websocket",
    "frontend",
}
GUEST_MEMORY_MIB = {
    "configurator": 64,
    "db": 544,
    "backend": 224,
    "cashier": 416,
    "queue": 256,
    "scheduler": 96,
    "redis-cache": 64,
    "redis-queue": 32,
    "websocket": 72,
    "frontend": 24,
}
GUEST_CPU_SHARES = {
    "configurator": 1024,
    "db": 4050,
    "backend": 1024,
    "cashier": 4050,
    "queue": 416,
    "scheduler": 212,
    "redis-cache": 1703,
    "redis-queue": 1284,
    "websocket": 311,
    "frontend": 1024,
}
GUEST_CPU_WEIGHTS = {
    "db": 300,
    "cashier": 300,
    "backend": 100,
    "queue": 50,
    "scheduler": 30,
    "redis-cache": 150,
    "redis-queue": 120,
    "websocket": 40,
    "frontend": 100,
}

# ADR trial allocations. Configurator is intentionally absent because it exits
# before backend, queue, websocket and scheduler start; it is capped separately.
COMPONENT_MEMORY_BUDGETS_MIB = {
    ("db",): 512,
    ("backend",): 544,
    ("queue", "scheduler"): 320,
    ("redis-cache", "redis-queue"): 96,
    ("frontend",): 24,
    ("websocket",): 72,
}

REQUIRED_SERVICES = {
    "configurator",
    "db",
    "backend",
    "queue",
    "scheduler",
    "redis-cache",
    "redis-queue",
    "websocket",
    "frontend",
}
STEADY_STATE_SERVICES = REQUIRED_SERVICES - {"configurator"}

_SERVICE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+:$")


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_compose_services(path: Path) -> dict[str, dict]:
    """Parse the small Compose YAML subset used by these fixture files.

    This intentionally avoids a PyYAML dependency so the offline guardrail can
    run in a bare Python installation. It understands only top-level services,
    scalar service properties, environment maps and list-form commands.
    """
    services: dict[str, dict] = {}
    in_services = False
    service_name = None
    nested_key = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if indent == 0:
            in_services = stripped == "services:"
            service_name = None
            nested_key = None
            continue
        if not in_services:
            continue
        if indent == 2 and _SERVICE_KEY_RE.match(stripped):
            service_name = stripped[:-1]
            services.setdefault(service_name, {})
            nested_key = None
            continue
        if service_name is None:
            continue
        service = services[service_name]
        if indent == 4:
            if stripped.endswith(":"):
                nested_key = stripped[:-1]
                if nested_key == "command":
                    service[nested_key] = []
                elif nested_key == "environment":
                    service[nested_key] = {}
            elif ":" in stripped:
                key, value = stripped.split(":", 1)
                service[key.strip()] = _scalar(value)
                nested_key = None
            continue
        if indent == 6 and nested_key == "command" and stripped.startswith("- "):
            service["command"].append(_scalar(stripped[2:]))
        elif indent == 6 and nested_key == "environment" and ":" in stripped:
            key, value = stripped.split(":", 1)
            service["environment"][key.strip()] = _scalar(value)
    return services


def merge_service(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_service(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_profile(profile_name: str) -> dict[str, dict]:
    paths = [
        DOCKER_DIR / "compose.base.yaml",
        DOCKER_DIR / f"profile-{profile_name}.yaml",
    ]
    ops_overlay = DOCKER_DIR / ("khitmee-ops-2gb.yaml" if profile_name.startswith("2gb") else f"khitmee-ops-{profile_name}.yaml")
    if ops_overlay.is_file():
        paths.append(ops_overlay)
    guest_ops_overlay = DOCKER_DIR / "khitmee-ops-guest.yaml"
    if profile_name == "2gb-guest" and guest_ops_overlay.is_file():
        paths.append(guest_ops_overlay)
    services: dict[str, dict] = {}
    for path in paths:
        for name, service in parse_compose_services(path).items():
            services[name] = merge_service(services.get(name, {}), service)
    return services


def memory_mib(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(mib|mb|m|gib|gb|g)?", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "b"
    if unit.startswith("g"):
        return number * 1024.0
    if unit.startswith("m"):
        return number
    return number / (1024.0 * 1024.0)


def cpu_count(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def runc_132_cgroup_v2_weight(shares: int) -> int:
    """runc >=1.3.2 conversion used by the Ubuntu runc 1.3.4 runtime."""
    if shares == 0:
        return 0
    if shares <= 2:
        return 1
    if shares >= 262144:
        return 10000
    log2 = math.log2(shares)
    exponent = (log2 * log2 + 125 * log2) / 612.0 - 7.0 / 34.0
    return math.ceil(math.pow(10, exponent))


def command_list(service: dict) -> list[str]:
    command = service.get("command", [])
    if isinstance(command, list):
        return [str(item) for item in command]
    return [str(command)] if command else []


def command_equals_option(command: list[str], name: str) -> str | None:
    prefix = f"--{name}="
    for token in command:
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def command_value_option(command: list[str], name: str) -> str | None:
    wanted = f"--{name}"
    for index, token in enumerate(command):
        if token == wanted and index + 1 < len(command):
            return command[index + 1]
        if token.startswith(wanted + "="):
            return token[len(wanted) + 1 :]
    return None


def int_option(command: list[str], name: str) -> int | None:
    value = command_equals_option(command, name) or command_value_option(command, name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def assess_profile(profile_name: str) -> dict:
    services = load_profile(profile_name)
    violations: list[str] = []
    missing = sorted(REQUIRED_SERVICES - set(services))
    if missing:
        violations.append("missing service definitions: " + ", ".join(missing))

    memory = {name: memory_mib(services.get(name, {}).get("mem_limit")) for name in sorted(REQUIRED_SERVICES & set(services))}
    cpus = {name: cpu_count(services.get(name, {}).get("cpus")) for name in sorted(REQUIRED_SERVICES & set(services))}
    missing_memory = sorted(name for name in REQUIRED_SERVICES if memory.get(name) is None)
    missing_cpus = sorted(name for name in REQUIRED_SERVICES if cpus.get(name) is None)
    if missing_memory:
        violations.append("missing memory limits: " + ", ".join(missing_memory))
    if missing_cpus:
        violations.append("missing CPU limits: " + ", ".join(missing_cpus))

    steady_memory = sum(memory.get(name) or 0.0 for name in STEADY_STATE_SERVICES)
    all_memory = sum(value or 0.0 for value in memory.values())
    total_cpus = sum(value or 0.0 for value in cpus.values())
    if steady_memory > STEADY_STATE_MEMORY_BUDGET_MIB:
        violations.append(
            f"steady-state memory {steady_memory:.0f} MiB exceeds {STEADY_STATE_MEMORY_BUDGET_MIB} MiB "
            f"(2 GiB minus {OS_HEADROOM_MIB} MiB OS headroom)"
        )
    if all_memory > ALL_CONTAINER_MEMORY_CEILING_MIB:
        violations.append(
            f"all-container memory sum {all_memory:.0f} MiB exceeds {ALL_CONTAINER_MEMORY_CEILING_MIB} MiB "
            f"(leaves under {ABSOLUTE_MINIMUM_HEADROOM_MIB} MiB even in the impossible all-at-once case)"
        )
    if not missing_cpus and total_cpus > CPU_BUDGET + 1e-9:
        violations.append(f"CPU limit sum {total_cpus:.2f} exceeds {CPU_BUDGET:.2f} vCPU")

    for names, budget in COMPONENT_MEMORY_BUDGETS_MIB.items():
        actual = sum(memory.get(name) or 0.0 for name in names)
        if actual > budget:
            violations.append(f"{'+'.join(names)} memory {actual:.0f} MiB exceeds ADR allocation {budget} MiB")
    if (memory.get("configurator") or 0.0) > TRANSIENT_CONFIGURATOR_BUDGET_MIB:
        violations.append(
            f"transient configurator memory {memory['configurator']:.0f} MiB exceeds {TRANSIENT_CONFIGURATOR_BUDGET_MIB} MiB"
        )

    db_command = command_list(services.get("db", {}))
    innodb_pool = memory_mib(command_equals_option(db_command, "innodb-buffer-pool-size"))
    max_connections = int_option(db_command, "max-connections")
    db_memory = memory.get("db")
    if innodb_pool is None:
        violations.append("db command does not pin innodb-buffer-pool-size")
    elif db_memory is not None and innodb_pool > db_memory * 0.65:
        violations.append(
            f"InnoDB buffer pool {innodb_pool:.0f} MiB exceeds 65% of the db container limit {db_memory:.0f} MiB"
        )
    if max_connections is None:
        violations.append("db command does not pin max-connections")
    elif db_memory is not None and innodb_pool is not None:
        # Four MiB per connection is a conservative upper planning number for the
        # small ERPNext workload plus a 32 MiB allowance for global non-pool buffers.
        connection_allowance = 32 + (4 * max_connections)
        db_headroom = db_memory - innodb_pool
        if db_headroom < connection_allowance:
            violations.append(
                f"db non-pool headroom {db_headroom:.0f} MiB is below planned allowance "
                f"{connection_allowance} MiB for max-connections={max_connections}"
            )

    for service_name in ("redis-cache", "redis-queue"):
        redis_memory = memory.get(service_name)
        redis_command = command_list(services.get(service_name, {}))
        maxmemory = memory_mib(command_value_option(redis_command, "maxmemory"))
        if maxmemory is None:
            violations.append(f"{service_name} does not pin Redis maxmemory")
        elif redis_memory is not None and maxmemory > redis_memory * 0.75:
            violations.append(
                f"{service_name} maxmemory {maxmemory:.0f} MiB exceeds 75% of container limit {redis_memory:.0f} MiB"
            )

    backend_command = command_list(services.get("backend", {}))
    workers = int_option(backend_command, "workers")
    threads = int_option(backend_command, "threads")
    backend_cpus = cpus.get("backend")
    worker_threads = workers * threads if workers is not None and threads is not None else None
    if workers is None or threads is None:
        violations.append("backend command does not pin Gunicorn workers and threads")
    elif backend_cpus is not None:
        # gthread workers are I/O-heavy, so four threads per allocated vCPU is the
        # upper bound; two concurrent threads are always allowed for the ADR floor.
        max_consistent_threads = max(2, math.ceil(backend_cpus * 4))
        if worker_threads > max_consistent_threads:
            violations.append(
                f"Gunicorn workers*threads {worker_threads} exceeds consistency bound "
                f"{max_consistent_threads} for backend CPU limit {backend_cpus:.2f}"
            )

    return {
        "profile": profile_name,
        "services": services,
        "memory_mib": memory,
        "cpus": cpus,
        "steady_memory_mib": steady_memory,
        "all_memory_mib": all_memory,
        "total_cpus": total_cpus,
        "gunicorn_workers": workers,
        "gunicorn_threads": threads,
        "gunicorn_worker_threads": worker_threads,
        "violations": violations,
    }


class ProfileBudgetTest(unittest.TestCase):
    def test_adr_profile_satisfies_trial_2gb_budget(self):
        assessment = assess_profile("2gb-adr")
        self.assertEqual([], assessment["violations"])
        self.assertEqual(1568.0, assessment["steady_memory_mib"])
        self.assertEqual(1632.0, assessment["all_memory_mib"])
        self.assertAlmostEqual(2.0, assessment["total_cpus"])
        self.assertEqual(2, assessment["gunicorn_workers"])
        self.assertEqual(1, assessment["gunicorn_threads"])
        self.assertEqual(2, assessment["gunicorn_worker_threads"])

    def test_existing_2gb_profile_is_explicitly_over_budget(self):
        # Keep this as executable evidence rather than silently changing the
        # incumbent profile. The seasonal smoke run can compare it with 2gb-adr.
        assessment = assess_profile("2gb")
        self.assertTrue(assessment["violations"])
        joined = "\n".join(assessment["violations"])
        self.assertIn("steady-state memory", joined)
        self.assertAlmostEqual(2.0, assessment["total_cpus"])
        self.assertIn("db memory 920 MiB exceeds ADR allocation 512 MiB", joined)
        self.assertIn("backend memory 576 MiB exceeds ADR allocation 544 MiB", joined)
        self.assertIn("frontend memory 120 MiB exceeds ADR allocation 24 MiB", joined)

    def test_guest_profile_matches_adr0004_topology_budgets_and_weights(self):
        services = load_profile("2gb-guest")
        self.assertEqual(GUEST_SERVICES, set(services))
        for service in sorted(GUEST_SERVICES):
            self.assertEqual("khitmeeperf.slice", services[service].get("cgroup_parent"), service)
            self.assertNotIn("cpus", services[service], service)
            self.assertEqual(GUEST_MEMORY_MIB[service], memory_mib(services[service].get("mem_limit")), service)
            self.assertEqual(GUEST_CPU_SHARES[service], int(str(services[service].get("cpu_shares"))), service)

        running_memory = sum(GUEST_MEMORY_MIB[name] for name in GUEST_SERVICES - {"configurator"})
        self.assertEqual(1728, running_memory)
        self.assertEqual(1792, sum(GUEST_MEMORY_MIB.values()))
        for service, weight in GUEST_CPU_WEIGHTS.items():
            self.assertEqual(weight, runc_132_cgroup_v2_weight(GUEST_CPU_SHARES[service]), service)

        db_command = command_list(services["db"])
        self.assertEqual("320M", command_equals_option(db_command, "innodb-buffer-pool-size"))
        self.assertEqual("32", command_equals_option(db_command, "max-connections"))

        backend_command = command_list(services["backend"])
        self.assertIn("--worker-class=sync", backend_command)
        self.assertIn("--workers=${GENERAL_GUNICORN_WORKERS:-1}", backend_command)
        self.assertIn("--preload", backend_command)
        self.assertNotIn("--threads=1", backend_command)

        cashier_command = command_list(services["cashier"])
        self.assertIn("--worker-class=gthread", cashier_command)
        self.assertIn("--workers=${CASHIER_GUNICORN_WORKERS:-1}", cashier_command)
        self.assertIn("--threads=${CASHIER_GUNICORN_THREADS:-3}", cashier_command)
        self.assertIn("--preload", cashier_command)

    def test_guest_nginx_implements_adr0004_classes_routes_and_observability(self):
        template = (DOCKER_DIR / "nginx-frappe-guest.conf.template").read_text(encoding="utf-8")
        main_conf = (DOCKER_DIR / "nginx-guest.conf").read_text(encoding="utf-8")

        self.assertIn("upstream cashier-server", template)
        self.assertIn("server cashier:8000", template)
        self.assertIn("upstream backend-server", template)
        for zone in ("adr4_cashier_write", "adr4_cashier_search", "adr4_general", "adr4_login_conn", "adr4_login_rate"):
            self.assertIn(zone, template)
        self.assertIn("limit_conn adr4_cashier_write 2;", template)
        self.assertIn("limit_conn adr4_cashier_search 1;", template)
        self.assertIn("limit_conn adr4_general 1;", template)
        self.assertIn("limit_req zone=adr4_login_rate burst=6 nodelay;", template)
        self.assertIn("limit_conn_status 429;", template)
        self.assertIn("limit_req_status 429;", template)

        self.assertIn(r"^/api/method/khitmee_ops\.api\.vouchers\.[A-Za-z0-9_]+$", template)
        self.assertIn("location = /api/method/khitmee_ops.api.search.search_items", template)
        self.assertIn("location = /api/method/frappe.desk.form.save.savedocs", template)
        self.assertIn("^/api/resource/Purchase(?:%20| )(Receipt|Invoice)", template)
        self.assertIn("location = /api/method/login", template)
        self.assertIn("proxy_pass http://cashier-server;", template)
        self.assertIn("proxy_pass http://backend-server;", template)
        self.assertIn("proxy_request_buffering on;", template)
        self.assertIn("proxy_next_upstream off;", template)
        self.assertIn("try_files /${FRAPPE_SITE_NAME_HEADER}/public/$uri @webserver;", template)

        for variable in (
            "$request_time",
            "$upstream_connect_time",
            "$upstream_header_time",
            "$upstream_response_time",
            "$status",
            "$upstream_pool",
        ):
            self.assertIn(variable, main_conf)

    def test_guest_cgroup_scripts_cover_cpuset_limits_and_process_tree_membership(self):
        setup = (DOCKER_DIR.parent / "scripts" / "guest_cgroup_setup.sh").read_text(encoding="utf-8")
        verify = (DOCKER_DIR.parent / "scripts" / "verify_guest_cgroup.sh").read_text(encoding="utf-8")
        self.assertIn("AllowedCPUs=", setup)
        self.assertIn("CPUQuota=200%", setup)
        self.assertIn("MemoryMax=2G", setup)
        self.assertIn("MemorySwapMax=0", setup)
        self.assertIn("cpuset.cpus.effective", setup)
        self.assertIn("init.scope", setup)
        self.assertIn("system.slice", setup)
        self.assertIn("user.slice", setup)
        self.assertIn("non-systemd", setup)

        self.assertIn("cpuset.cpus.effective", verify)
        self.assertIn("non-systemd", verify)
        self.assertIn("populated cgroup", verify)
        self.assertIn("cpu.weight", verify)
        self.assertIn("process_tree", verify)
        self.assertIn("cgroup.procs", verify)
        self.assertIn("host PIDs outside container scope", verify)

    def test_guest_startup_migration_and_cashier_ops_overlay_are_wired(self):
        up = (DOCKER_DIR.parent / "scripts" / "up.sh").read_text(encoding="utf-8")
        overlay = (DOCKER_DIR / "khitmee-ops-guest.yaml").read_text(encoding="utf-8")
        self.assertIn("run_khitmee_ops_migration_if_needed", up)
        self.assertIn('bench --site "$SITE_NAME" migrate', up)
        self.assertIn("startup-migrations.log", up)
        self.assertIn("KHITMEE_OPS_SYNC_CHANGED", up)
        self.assertIn("__khitmee_ops_migration_state", up)
        self.assertIn("record_khitmee_ops_migrated_tree_hash", up)
        self.assertIn("python_services+=(cashier)", up)
        self.assertIn("wait_for_service_running cashier 180", up)
        self.assertIn("khitmee-ops-guest.yaml", up)
        self.assertIn("verify_guest_cgroup.sh", up)
        routes = (DOCKER_DIR.parent / "scripts" / "check_guest_routes.sh").read_text(encoding="utf-8")
        self.assertIn("upstream_name=general", routes)
        self.assertIn("upstream_name=cashier", routes)
        down = (DOCKER_DIR.parent / "scripts" / "down.sh").read_text(encoding="utf-8")
        self.assertIn("profile-2gb-guest.yaml", down)
        self.assertIn("cashier:", overlay)
        self.assertIn("PYTHONPATH: /home/frappe/frappe-bench/apps/khitmee_ops", overlay)

    def test_mariadb_image_is_opt_in_overridable(self):
        db = parse_compose_services(DOCKER_DIR / "compose.base.yaml")["db"]
        self.assertEqual("${MARIADB_IMAGE:-mariadb:11.8}", db.get("image"))

    def test_budget_arithmetic_matches_adr_table(self):
        self.assertEqual(1568, sum(budget for budget in COMPONENT_MEMORY_BUDGETS_MIB.values()))
        self.assertEqual(2048, STEADY_STATE_MEMORY_BUDGET_MIB + OS_HEADROOM_MIB)


if __name__ == "__main__":
    unittest.main()
