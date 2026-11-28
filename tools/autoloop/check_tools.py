"""Local check diagnostics and the subprocess adapter for fast_check timing."""

from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import current_thread
from time import monotonic
from typing import Any

def _run_check_command(
    command: list[str] | str, cwd: Path, *, shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=cwd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace",
    )
    if result.stdout:
        print(result.stdout, end="", file=sys.stderr)
    return result


def _adapter_args(action: str, tooling: Path, worktree: Path) -> list[str]:
    return [sys.executable, "-E", "-P", "-B", str(Path(__file__).resolve()), action, str(tooling), str(worktree)]


def verify_with_trusted_tooling(
    tooling: Path, worktree: Path, repo: Path, run_id: str, *,
    shard_id: str | None, base_ref: str | None, head_sha: str,
) -> dict[str, Any]:
    from autoloop.guard import GuardError

    request = {"run_id": run_id, "shard_id": shard_id, "base_ref": base_ref, "head_ref": head_sha}
    proc = subprocess.run(
        [*_adapter_args("verify", tooling, worktree), str(repo)], cwd=tooling,
        input=json.dumps(request), encoding="utf-8", capture_output=True,
    )
    if proc.returncode:
        message = proc.stderr.strip()
        if "changes outside allowed_paths:" in message and "--base-ref" not in message:
            message += ". For an already-integrated dependency PR, pass --base-ref <integration-commit> (or --base-sha)."
        raise GuardError(message or f"trusted verify-untampered failed with exit {proc.returncode}")
    return json.loads(proc.stdout)


def time_check_commands(
    tooling: Path, worktree: Path, commands: list[tuple[str, str]], steps: dict[str, float], *, skip_fast_check: bool,
) -> bool:
    from autoloop.guard import CheckCommandError, GuardError

    ran_fast_check = not skip_fast_check
    if ran_fast_check:
        if not (tooling / "tools/fast_check.py").is_file():
            raise GuardError("trusted tooling is missing fast_check.py")
        with TemporaryDirectory(prefix="autoloop-timing-") as temp:
            timing_file = Path(temp) / "jobs.json"
            argv = [*_adapter_args("fast-check", tooling, worktree), str(timing_file)]
            started = monotonic()
            try:
                proc = _run_check_command(argv, tooling)
            finally:
                steps["fast_check.py"] = round(monotonic() - started, 3)
                if timing_file.is_file():
                    steps.update(json.loads(timing_file.read_text(encoding="utf-8")))
        if proc.returncode:
            raise CheckCommandError("fast_check.py", f"fast_check.py failed with exit {proc.returncode}", proc.stdout)

    for index, (shard_id, command) in enumerate(commands, 1):
        name = f"{shard_id} #{index}: {command}"
        started = monotonic()
        try:
            proc = _run_check_command(command, worktree, shell=True)
        finally:
            steps[name] = round(monotonic() - started, 3)
        if proc.returncode:
            raise CheckCommandError(
                f"{shard_id}-check-{index}", f"shard {shard_id!r} check {command!r} failed with exit {proc.returncode}",
                proc.stdout,
            )
    return ran_fast_check


def warn_slow_checks(steps: dict[str, float], previous: dict[str, float]) -> None:
    slowest = sorted(
        ((name, seconds) for name, seconds in steps.items() if seconds > 60),
        key=lambda item: item[1], reverse=True,
    )
    if not slowest:
        return
    print("WARNING: check steps exceeded 60s (slowest first)", file=sys.stderr)
    for name, seconds in slowest:
        before = previous.get(name)
        detail = f" (regressed from {before:.1f}s to {seconds:.1f}s)" if before is not None and before < seconds else ""
        print(f"  {seconds:.1f}s {name}{detail}", file=sys.stderr)


def _reachable(command: list[str]) -> bool:
    try:
        return subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def doctor(*, windows: bool = os.name == "nt") -> dict[str, Any]:
    from fast_check import DEV_BOOTSTRAP_COMMAND, missing_dev_tools

    missing = missing_dev_tools()
    python_fix = (
        "winget install -e --id Python.Python.3.14" if windows else "uv python install 3.14"
    )
    checks = {
        "python": {"ok": sys.version_info[:2] == (3, 14), "version": sys.version.split()[0], "fix": python_fix},
        "dev_group": {"ok": not missing, "missing": missing, "fix": DEV_BOOTSTRAP_COMMAND},
        "uv": {"ok": _reachable(["uv", "--version"]), "fix": "python -m pip install uv"},
        "docker": {
            "ok": _reachable(["docker", "info"]),
            "fix": "docker desktop start" if windows else "sudo systemctl start docker",
        },
    }
    if windows:
        wsl = ["wsl.exe", "-d", "KhitmeeERPNext", "--"]
        distro = _reachable([*wsl, "true"])
        checks["KhitmeeERPNext"] = {
            "ok": distro,
            "fix": 'wsl.exe --import KhitmeeERPNext "<install-directory>" "<KhitmeeERPNext-backup.tar>" --version 2',
        }
        checks["docker_in_KhitmeeERPNext"] = {
            "ok": distro and _reachable([*wsl, "docker", "info"]),
            "fix": "wsl.exe -d KhitmeeERPNext -u root -- service docker start",
        }
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}


def _trusted_modules(tooling: Path):
    sys.path.insert(0, str(tooling / "tools"))
    from autoloop import guard, static_rules

    rules = tooling / ".autoloop/rules"
    evaluate = guard.evaluate_file_map
    lint = static_rules.lint_repo
    load_config = guard.load_config

    def trusted_config(_repo):
        config = load_config(tooling)
        config["critical_policy_globs"] = [
            *(config.get("critical_policy_globs") or []),
            ".claude/skills/autoloop/**", ".agents/skills/autoloop/**",
        ]
        return config

    guard.load_config = trusted_config
    guard.evaluate_file_map = lambda _rules, files: evaluate(rules, files)
    static_rules.lint_repo = lambda root, **kwargs: lint(root, **{**kwargs, "rules_dir": rules})
    return guard


def run_timed_fast_check(tooling: Path, repo: Path, timing_file: Path) -> None:
    """Run the trusted gate against the candidate, timing its outer executor jobs."""
    _trusted_modules(tooling)
    script = tooling / "tools/fast_check.py"
    sys.argv = [str(script)]
    check_thread = current_thread()
    timings = {}
    names = {
        "python_tests": "python",
        "bash_syntax": "bash",
        "ruff_gate": "ruff_and_dev_deps",
        "zizmor_gate": "zizmor_workflows",
    }

    executor = concurrent.futures.ThreadPoolExecutor

    class TimedExecutor(executor):
        def submit(self, fn, /, *args, **kwargs):
            code = getattr(fn, "__code__", None)
            if current_thread() is not check_thread or code is None or code.co_filename != str(script):
                return super().submit(fn, *args, **kwargs)

            def timed():
                started = monotonic()
                try:
                    return fn(*args, **kwargs)
                finally:
                    name = names.get(fn.__name__, fn.__name__)
                    timings[f"fast_check.py/{name}"] = round(monotonic() - started, 3)

            return super().submit(timed)

    # The fast-check module accepts an explicit root, so its own source remains outside the candidate.
    concurrent.futures.ThreadPoolExecutor = TimedExecutor
    try:
        gate = runpy.run_path(str(script), run_name="_autoloop_trusted_fast_check")["check"]
        gate(repo)
    finally:
        concurrent.futures.ThreadPoolExecutor = executor
        timing_file.write_text(json.dumps(timings), encoding="utf-8")


if __name__ == "__main__":
    action, tooling, worktree, extra = sys.argv[1:]
    if action == "verify":
        guard = _trusted_modules(Path(tooling))
        target = Path(worktree)
        request = json.load(sys.stdin)
        try:
            guard.assert_clean_worktree_for_check(target, run_id=request["run_id"])
            result = guard.verify_untampered(Path(extra), git_repo=target, **request)
            print(json.dumps(result))
        except guard.GuardError as exc:
            print(exc, file=sys.stderr)
            sys.exit(exc.exit_code)
    else:
        run_timed_fast_check(Path(tooling), Path(worktree), Path(extra))
