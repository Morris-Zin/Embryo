"""Parallel, offline checks. Each failed gate retains a bounded diagnostic."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def run(argv, cwd, **env):
    result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", timeout=45,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env})
    if result.returncode:
        raise RuntimeError(result.stdout.strip()[-800:] or f"exit {result.returncode}")
    return result.stdout


def check(root):
    start = time.monotonic()
    app = root / "apps/khitmee_ops"
    perf = root / "perf-suite"
    jobs = {}

    def python_tests():
        import re
        for folder in (app / "khitmee_ops/tests", perf / "seeder", perf / "k6", perf / "offload", root / "tools"):
            output = run([sys.executable, "-B", "-m", "unittest", "discover", "-s", str(folder), "-p", "test_*.py"], root)
            match = re.search(r"Ran (\d+) tests?", output)
            if not match or int(match.group(1)) < 1:
                raise RuntimeError(f"unittest discovered zero tests in {folder}")

    def bash_syntax():
        files = subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "ls-files",
                                         "--cached", "--others", "--exclude-standard", "-z", "--", "*.sh"],
                                        cwd=root).decode().split("\0")
        for name in sorted(set(filter(None, files))):
            source = (root / name).read_bytes()
            # Match Git's LF repository representation on Windows checkouts.
            if name.startswith("tools/") and (b"\r" in source or source.startswith(b"\xef\xbb\xbf")):
                raise RuntimeError(f"{name} requires LF and UTF-8 without BOM")
            result = subprocess.run(["bash", "-n"], input=source.replace(b"\r\n", b"\n"),
                                    cwd=root, capture_output=True)
            if result.returncode:
                raise RuntimeError(f"{name}: {result.stderr.decode(errors='replace')[:300]}")

    bun = shutil.which("bun")
    node = shutil.which("node") or shutil.which("node.exe")

    def javascript():
        files = subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "ls-files", "-co",
                                         "--exclude-standard", "--", "apps/khitmee_ops/**/*.js",
                                         "perf-suite/k6/**/*.js", "perf-suite/browser/*.mjs"], cwd=root, text=True).splitlines()
        if not files:
            raise RuntimeError("no JavaScript inputs discovered")
        if bun:
            run([bun, "-e", "const t=new Bun.Transpiler({loader:'js'});"
                 "for(const f of Bun.argv.slice(1))t.transformSync(await Bun.file(f).text())", *files], root)
        elif node:
            run([node, "--experimental-vm-modules", "-e",
                 "const fs=require('node:fs'), vm=require('node:vm');"
                 "for(const f of process.argv.slice(1))new vm.SourceTextModule(fs.readFileSync(f,'utf8'),"
                 "{identifier:f})", *files], root)
        else:
            raise RuntimeError("install Bun or Node")

    def knip():
        pinned = os.environ.get("AUTOLOOP_PINNED_KNIP", "").strip()
        version = pinned or json.loads((app / "package.json").read_text())["devDependencies"]["knip"]
        npx = shutil.which("npx")
        pnpm = shutil.which("pnpm")
        if bun and not pinned:
            run([bun, "x", "--bun", "--no-install", "knip@" + version], app)
        elif node and node.lower().endswith(".exe") and (Path(node).parent / "node_modules/npm/bin/npx-cli.js").is_file():
            # Windows npm cache and Node are visible from the Docker distro.
            cli = Path(node).parent / "node_modules/npm/bin/npx-cli.js"
            npx_cli = str(cli)
            if shutil.which("wslpath"):
                npx_cli = subprocess.check_output(["wslpath", "-w", str(cli)], text=True).strip()
            run([node, npx_cli, "--offline", "--yes", "knip@" + version], app)
        elif npx:
            run([npx, "--offline", "--yes", "knip@" + version], app)
        elif pnpm:
            run([pnpm, "dlx", "--offline", "knip@" + version], app)
        else:
            raise RuntimeError("install npx or pnpm")

    def autoloop_lint():
        tools_dir = str(root / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from autoloop.static_rules import lint_repo

        violations = lint_repo(root)
        if violations:
            lines = [f"{v.rule_id}: {v.path}:{v.line}: {v.message}" for v in violations[:10]]
            raise RuntimeError("\n".join(lines))

    def compose():
        for size in ("2gb", "4gb"):
            base = ["docker", "compose", "--env-file", "/dev/null", "-p", "khitmee-perf",
                    "-f", "compose.base.yaml", "-f", f"profile-{size}.yaml"]
            for extra in ([], ["-f", f"khitmee-ops-{size}.yaml"]):
                run(base + extra + ["config", "--quiet"], perf / "docker", DB_PASSWORD="config-only")

    jobs.update(
        python=python_tests,
        javascript=javascript,
        knip=knip,
        bash=bash_syntax,
        compose=compose,
        autoloop_lint=autoloop_lint,
    )
    failed = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(job): name for name, job in jobs.items()}
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
                print("PASS", futures[future])
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                print("FAIL", futures[future], str(error)[:800])
                failed = True
    elapsed = time.monotonic() - start
    print(f"elapsed={elapsed:.2f}s")
    if elapsed >= 60.0:
        print(f"FAIL timing fast-check exceeded 60s: {elapsed:.2f}s")
        failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    check(Path(__file__).resolve().parents[1])
