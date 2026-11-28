#!/usr/bin/env python3
"""Deploy the exact archived khitmee_ops tree, migrate, and grant actor roles."""

import argparse
import datetime
import json
import pathlib
import shutil
import subprocess
import sys

SERVICES = ["backend", "cashier", "queue", "scheduler", "websocket"]
MANAGER_KEYS = [
    "SEASONAL_MANAGER_WAREHOUSE_USER",
    "SEASONAL_MANAGER_MONTHLY_USER",
    "SEASONAL_MANAGER_DETAIL_USER",
    "SEASONAL_MANAGER_EXPORT_USER",
]

APPS_STATE_SCRIPT = r'''
import json
import pathlib
import sys
import frappe

site = sys.argv[1]
apps_txt_path = pathlib.Path("/home/frappe/frappe-bench/sites/apps.txt")
apps_txt = []
try:
    apps_txt = [line.strip() for line in apps_txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
except Exception:
    apps_txt = []
frappe.init(site=site, sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
try:
    rows = frappe.db.sql("SELECT app_name FROM `tabInstalled Application` ORDER BY app_name")
    installed = sorted(str(row[0]) for row in rows if row and row[0])
finally:
    frappe.destroy()
missing = sorted(set(installed) - set(apps_txt))
extra = sorted(set(apps_txt) - set(installed))
print("APPS_STATE_EVIDENCE " + json.dumps({
    "apps_txt": apps_txt,
    "installed_apps": installed,
    "missing_from_apps_txt": missing,
    "extra_in_apps_txt": extra,
}, sort_keys=True))
'''

MIGRATION_STATE_SCRIPT = r'''
import json
import sys
import frappe

site = sys.argv[1]
tree_hash = sys.argv[2]
frappe.init(site=site, sites_path="/home/frappe/frappe-bench/sites")
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
        {"tree_hash": tree_hash},
    )
    frappe.db.commit()
    rows = frappe.db.sql("SELECT tree_hash FROM `__khitmee_ops_migration_state` WHERE name='state'")
    recorded = rows[0][0] if rows else ""
finally:
    frappe.destroy()
print("MIGRATION_STATE_EVIDENCE " + json.dumps({"tree_hash": recorded}, sort_keys=True))
'''

ROLE_SCRIPT = r'''
import json
import sys
import frappe

site = sys.argv[1]
actors = json.loads(sys.argv[2])
frappe.init(site=site, sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
result = {}
try:
    for email in actors:
        if not frappe.db.exists("User", email):
            raise RuntimeError("manager actor user does not exist: " + email)
        user = frappe.get_doc("User", email)
        before = sorted(str(row.role) for row in user.get("roles") or [])
        user.add_roles("Accounts User", "Accounts Manager")
        after = sorted(str(row.role) for row in user.get("roles") or [])
        result[email] = {"before": before, "after": after, "granted": sorted(set(after) - set(before))}
    frappe.db.commit()
finally:
    frappe.destroy()
print("SEASONAL_ROLE_EVIDENCE " + json.dumps(result, sort_keys=True))
'''


def run(cmd, timeout=None, input_text=None):
    result = subprocess.run(cmd, input=input_text, text=True, capture_output=True, timeout=timeout, check=False)
    return result


def require_ok(result, description):
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{description} failed with exit {result.returncode}: {detail[-3000:]}")
    return result


def compose_base(args):
    command = [
        "docker", "compose",
        "--project-name", args.compose_project,
        "--project-directory", args.docker_dir,
        "--env-file", str(pathlib.Path(args.docker_dir) / ".env"),
    ]
    for file_name in args.compose_file:
        command.extend(["-f", file_name])
    return command


def copy_tree(source, target):
    source = pathlib.Path(source)
    target = pathlib.Path(target)
    if not source.is_dir():
        raise RuntimeError(f"materialized app path missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    # The target is bind-mounted into already-created containers. Replace its
    # contents in place rather than deleting the directory itself: a bind mount
    # would otherwise keep referencing the old deleted host inode.
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--docker-dir", required=True)
    parser.add_argument("--actors", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--compose-project", default="khitmee-perf")
    parser.add_argument("--compose-file", action="append", required=True)
    parser.add_argument("--target", default="/srv/khitmee-perf/apps/khitmee_ops")
    args = parser.parse_args()

    artifact = pathlib.Path(args.artifact_dir)
    artifact.mkdir(parents=True, exist_ok=True)
    try:
        metadata = json.loads(pathlib.Path(args.metadata).read_text())
        target = copy_tree(metadata["materialized_app_path"], args.target)
        compose = compose_base(args)

        restart_log = artifact / "candidate-service-up.log"
        service_up = run(compose + ["up", "-d", "--no-deps", "--force-recreate", *SERVICES], timeout=180)
        frontend_up = None
        if service_up.returncode == 0:
            # nginx resolves upstream container IPs at worker startup. Recreate it
            # after backend replacement so both the attribution template and the
            # fresh backend address are effective.
            frontend_up = run(compose + ["up", "-d", "--no-deps", "--force-recreate", "frontend"], timeout=120)
        restart_log.write_text(service_up.stdout + service_up.stderr + ((frontend_up.stdout + frontend_up.stderr) if frontend_up else ""))
        require_ok(service_up, "candidate application service deployment")
        if frontend_up is not None:
            require_ok(frontend_up, "candidate frontend attribution deployment")

        apps_rewrite = run(
            compose + [
                "exec", "-T", "backend", "bash", "-lc",
                "cd /home/frappe/frappe-bench && ls -1 apps > sites/apps.txt && cat sites/apps.txt",
            ],
            timeout=120,
        )
        (artifact / "candidate-apps-rewrite.log").write_text(apps_rewrite.stdout + apps_rewrite.stderr)
        require_ok(apps_rewrite, "regenerate apps.txt with the candidate tree mounted")
        apps_state_result = run(
            compose + [
                "exec", "-T", "backend",
                "/home/frappe/frappe-bench/env/bin/python", "-", args.site,
            ],
            input_text=APPS_STATE_SCRIPT,
            timeout=180,
        )
        (artifact / "candidate-apps-state.log").write_text(apps_state_result.stdout + apps_state_result.stderr)
        require_ok(apps_state_result, "verify candidate apps.txt against installed applications")
        marker = "APPS_STATE_EVIDENCE "
        apps_state = None
        for line in apps_state_result.stdout.splitlines():
            if line.startswith(marker):
                apps_state = json.loads(line[len(marker):])
        if apps_state is None:
            raise RuntimeError("apps.txt verification did not emit machine-readable evidence")
        missing_from_apps_txt = sorted(apps_state.get("missing_from_apps_txt") or [])
        extra_in_apps_txt = sorted(apps_state.get("extra_in_apps_txt") or [])
        apps_txt_rows = sorted(apps_state.get("apps_txt") or [])
        apps_state.update({
            "schema_version": "seasonal-candidate-apps-v1",
            "passed": not missing_from_apps_txt and not extra_in_apps_txt and "khitmee_ops" in apps_txt_rows,
            "log": str(artifact / "candidate-apps-state.log"),
        })
        (artifact / "candidate-apps.json").write_text(json.dumps(apps_state, indent=2, sort_keys=True) + "\n")
        if "khitmee_ops" not in apps_txt_rows:
            raise RuntimeError("regenerated apps.txt does not contain khitmee_ops")
        if missing_from_apps_txt:
            raise RuntimeError("regenerated apps.txt lacks restored-site installed apps: " + ",".join(missing_from_apps_txt))
        if extra_in_apps_txt:
            raise RuntimeError("regenerated apps.txt contains apps not installed in the restored site: " + ",".join(extra_in_apps_txt))

        migrate_log = artifact / "candidate-migrate.log"
        migrate_started = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        migration_overlay = pathlib.Path(args.docker_dir) / "khitmee-migration-transient.yaml"
        if not migration_overlay.is_file():
            raise RuntimeError(f"ADR migration overlay missing: {migration_overlay}")
        migrate = run(
            compose + ["-f", str(migration_overlay), "run", "--rm", "--no-deps", "-T", "backend", "bench", "--site", args.site, "migrate"],
            timeout=1800,
        )
        migrate_finished = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        migrate_log.write_text(migrate.stdout + migrate.stderr)
        require_ok(migrate, "bench migrate for candidate tree")
        expected_tree_hash = str(metadata.get("app_tree_hash") or "")
        if not expected_tree_hash:
            raise RuntimeError("candidate metadata lacks app_tree_hash; refusing to record migration state")
        state_result = run(
            compose + [
                "-f", str(migration_overlay), "run", "--rm", "--no-deps", "-T",
                "-w", "/home/frappe/frappe-bench/sites", "backend",
                "/home/frappe/frappe-bench/env/bin/python", "-", args.site, expected_tree_hash,
            ],
            input_text=MIGRATION_STATE_SCRIPT,
            timeout=180,
        )
        (artifact / "candidate-migration-state.log").write_text(state_result.stdout + state_result.stderr)
        require_ok(state_result, "record migrated candidate tree hash")
        marker = "MIGRATION_STATE_EVIDENCE "
        state_evidence = None
        for line in state_result.stdout.splitlines():
            if line.startswith(marker):
                state_evidence = json.loads(line[len(marker):])
        if state_evidence is None:
            raise RuntimeError("migration state script did not emit machine-readable evidence")
        migrated_tree_hash = str(state_evidence.get("tree_hash") or "")
        tree_hash_verified = migrated_tree_hash == expected_tree_hash
        if not tree_hash_verified:
            raise RuntimeError(
                "migrated app tree digest mismatch: expected " + expected_tree_hash + " but recorded " + migrated_tree_hash
            )
        migration = {
            "required": True,
            "reason": "candidate tree changed or fixture restore lacks candidate schema",
            "started_at": migrate_started,
            "finished_at": migrate_finished,
            "exit_code": migrate.returncode,
            "log": str(migrate_log),
            "app_tree_hash_expected": expected_tree_hash,
            "migrated_app_tree_hash": migrated_tree_hash,
            "tree_hash_verified": tree_hash_verified,
            "state_log": str(artifact / "candidate-migration-state.log"),
        }
        (artifact / "candidate-migration.json").write_text(json.dumps(migration, indent=2, sort_keys=True) + "\n")

        actors = json.loads(pathlib.Path(args.actors).read_text())
        manager_users = []
        missing = []
        for key in MANAGER_KEYS:
            value = actors.get(key)
            if value:
                manager_users.append(str(value))
            else:
                missing.append(key)
        if missing:
            raise RuntimeError("actor environment missing manager user keys: " + ",".join(missing))

        role_script_path = artifact / "grant-manager-roles.py"
        role_script_path.write_text(ROLE_SCRIPT)
        backend = f"{args.compose_project}-backend-1"
        require_ok(run(["docker", "cp", str(role_script_path), f"{backend}:/tmp/grant-manager-roles.py"]), "copy role grant script")
        roles = run([
            "docker", "exec", backend,
            "/home/frappe/frappe-bench/env/bin/python", "/tmp/grant-manager-roles.py",
            args.site, json.dumps(sorted(set(manager_users))),
        ], timeout=180)
        (artifact / "candidate-role-grants.log").write_text(roles.stdout + roles.stderr)
        require_ok(roles, "manager actor role grants")
        marker = "SEASONAL_ROLE_EVIDENCE "
        evidence = None
        for line in roles.stdout.splitlines():
            if line.startswith(marker):
                evidence = json.loads(line[len(marker):])
        if evidence is None:
            raise RuntimeError("manager role grant script did not emit machine-readable evidence")
        for email, row in evidence.items():
            roles_after = set(row.get("after") or [])
            if "Accounts User" not in roles_after and "Accounts Manager" not in roles_after:
                raise RuntimeError(f"manager actor {email} lacks Accounts User/Manager after grant")
        (artifact / "candidate-role-grants.json").write_text(json.dumps({
            "granted_roles": ["Accounts User", "Accounts Manager"],
            "actors": evidence,
            "verification": "each manager actor exposes Accounts User or Accounts Manager in ERPNext roles",
        }, indent=2, sort_keys=True) + "\n")

        final = dict(metadata)
        final.update({
            "deployed_target": str(target),
            "migration": migration,
            "role_grants_artifact": str(artifact / "candidate-role-grants.json"),
        })
        (artifact / "candidate-deployment.json").write_text(json.dumps(final, indent=2, sort_keys=True) + "\n")
        print(json.dumps(final, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        failure = {"passed": False, "error": str(exc)}
        (artifact / "candidate-deployment-failed.json").write_text(json.dumps(failure, indent=2, sort_keys=True) + "\n")
        print(f"NO-GO: candidate deployment failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
