"""Host-side entry point for the isolated perf stack."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from http.cookies import SimpleCookie
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "khitmee-perf"
LABEL = 'index .Config.Labels "com.docker.compose.project"'
HELP = """khitmee-dev commands (WSL Bash or tools/khitmee-dev.ps1)
  fast-check                         local Python, JS, knip, Bash and Compose gate
  browser-perf-check                 Playwright cashier frontend performance regression gate
  status                             containers, live fingerprint, report flags
  sql|explain|analyze 'SELECT ...'    one statement, up to 40 output rows
  py [--write] 'print(...)'           trusted Frappe snippet; DB read-only by default
  upstream read APP/PATH.py [A B]     installed source lines (inclusive)
  upstream grep APP/PATH.py REGEX     matching source lines
  report-probe REPORT '{"key":"value"}'   synchronous perf HTTP timing
  parity [--profile 2gb|4gb] [--only gp|sb|search|voucher]   isolated live parity suite
For SQL or Python pass - to read UTF-8 stdin."""


def docker(*args, **kwargs):
    return subprocess.check_output(["docker", *args], text=True, **kwargs).strip()


def container(service):
    name = f"{PROJECT}-{service}-1"
    project = docker("inspect", "-f", "{{" + LABEL + "}}", name)
    if project == "khitmee-erpnext" or project != PROJECT:
        raise ValueError("expected a khitmee-perf container")
    return name


def session(command, args):
    if command != "upstream":
        container("db")
    backend = container("backend")
    source = (ROOT / "tools/perf_session.py").read_bytes()
    subprocess.run(["docker", "exec", "-i", "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "-w", "/home/frappe/frappe-bench/sites", backend,
                    "/home/frappe/frappe-bench/env/bin/python", "-", command, *args], input=source, check=True)


def status():
    for project in (PROJECT, "khitmee-erpnext"):
        ids = docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}").split()
        if not ids:
            print(project, "absent")
            continue
        if project == PROJECT:
            fmt = ('{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}'
                   ' restarts={{.RestartCount}} oom={{.State.OOMKilled}} bytes={{.HostConfig.Memory}}')
            print(docker("inspect", "-f", fmt, *ids))
        else:
            state = docker("inspect", "-f", "{{.Id}} {{.State.StartedAt}} {{.RestartCount}}", *sorted(ids))
            print("live containers", len(ids), "fingerprint", hashlib.sha256(state.encode()).hexdigest()[:16])
    sys.stdout.flush()
    session("sql", ["SELECT name, prepared_report FROM tabReport WHERE name IN "
                    "('Stock Balance','Fast Gross Profit','Gross Profit') ORDER BY name"])


def probe(name, filters):
    if not isinstance(json.loads(filters), dict):
        raise ValueError("expected JSON filter object")
    if docker("port", container("frontend"), "8080/tcp") != "127.0.0.1:18086":
        raise ValueError("unexpected perf port")
    # Parse the generated key=value file as data. Keep credentials in memory.
    entries = dict(line.split("=", 1) for line in
                   (ROOT / "perf-suite/docker/.env").read_text().splitlines()
                   if "=" in line and not line.startswith("#"))
    conn = http.client.HTTPConnection("127.0.0.1", 18086, timeout=130)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        conn.request("POST", "/api/method/login", urlencode({
            "usr": "Administrator", "pwd": entries["ADMIN_PASSWORD"]}), headers)
        login = conn.getresponse()
        cookies = SimpleCookie()
        for key, value in login.getheaders():
            if key.lower() == "set-cookie":
                cookies.load(value)
        login.read()
        if login.status != 200 or "sid" not in cookies:
            raise ValueError("perf login failed")
        headers["Cookie"] = "sid=" + cookies["sid"].value
        query = urlencode(dict(report_name=name, filters=filters, ignore_prepared_report=1))
        start = time.monotonic()
        conn.request("GET", "/api/method/frappe.desk.query_report.run?" + query, headers=headers)
        response = conn.getresponse()
        body = response.read()
        elapsed = time.monotonic() - start
        data = json.loads(body).get("message") if response.status == 200 else None
        rows = data.get("result") if isinstance(data, dict) else None
        prepared = data.get("prepared_report", False) if isinstance(data, dict) else None
        print(f"HTTP={response.status} TIME={elapsed:.3f}s BYTES={len(body)} "
              f"ROWS={len(rows) if isinstance(rows, list) else '-'} prepared_report={prepared}")
        if not isinstance(rows, list) or prepared:
            raise ValueError("expected synchronous report rows")
    finally:
        conn.close()


def main(args):
    if args[:1] == ["--windows-args"]:
        args = json.loads(base64.b64decode(args[1]).decode("utf-8-sig"))
    if not args or args == ["--help"] or args == ["-h"]:
        print(HELP)
        return
    if os.getenv("COMPOSE_PROJECT_NAME", PROJECT) != PROJECT:
        raise ValueError("expected khitmee-perf project")
    cmd, *args = args
    if cmd in ("sql", "explain", "analyze", "py", "upstream"):
        if cmd != "upstream" and args[-1:] == ["-"]:
            args[-1] = sys.stdin.buffer.read().decode("utf-8-sig").replace("\r\n", "\n")
        session(cmd, args)
    elif cmd == "report-probe":
        probe(*args)
    elif cmd == "status" and not args:
        status()
    elif cmd == "browser-perf-check" and not args:
        if docker("port", container("frontend"), "8080/tcp") != "127.0.0.1:18086":
            raise ValueError("unexpected perf port")
        browser_dir = ROOT / "perf-suite/browser"
        subprocess.run(["pnpm", "install", "--frozen-lockfile", "--ignore-scripts"], cwd=browser_dir, check=True)
        subprocess.run(["pnpm", "exec", "playwright", "test"], cwd=browser_dir, check=True)
    elif cmd == "fast-check" and not args:
        from fast_check import check
        check(ROOT)
    elif cmd == "parity":
        import perf_parity
        sys.exit(perf_parity.main(args))
    else:
        sys.exit(HELP)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException) as error:
        sys.exit(f"khitmee-dev failed ({type(error).__name__}): check arguments, perf services and configuration")
