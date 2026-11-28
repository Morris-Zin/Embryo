"""Linux gate-host provisioning and autoloop gate runner."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

DEFAULT_DISTRO = "KhitmeeERPNext"
DEFAULT_ROOT = "/root/gate"
FETCH_REFSPECS = (
    "+refs/heads/feat/*:refs/heads/feat/*",
    "+refs/heads/shard/*:refs/heads/shard/*",
)
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SHARD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class GateError(RuntimeError):
    pass


def q(value: object) -> str:
    text = str(value)
    return "'" + text.replace("'", "'\\''") + "'"


def is_windows() -> bool:
    return os.name == "nt"


def windows_to_wsl_path(path: str | Path) -> str:
    text = str(path).strip()
    if text.startswith("/"):
        return text
    text = text.replace("\\", "/")
    if text.startswith("//?/"):
        text = text[4:]
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        drive = text[0].lower()
        rest = text[2:].lstrip("/")
        return f"/mnt/{drive}" + (f"/{rest}" if rest else "")
    raise GateError(f"cannot translate Windows path to WSL: {path}")


def filter_interop_path(value: str) -> str:
    parts = []
    for part in value.split(":"):
        normalized = part.strip()
        if not normalized or normalized == "/mnt" or normalized.startswith("/mnt/"):
            continue
        parts.append(part)
    return ":".join(parts)


def gate_path(root: str, current_path: str) -> str:
    prefix = [posixpath.join(root, "bin"), posixpath.join(root, "venv/bin")]
    clean = filter_interop_path(current_path)
    return ":".join(prefix + ([clean] if clean else []))


def node_wrapper_content(node_path: str, cli_path: str) -> str:
    return f'#!/usr/bin/env bash\nexec {node_path} {cli_path} "$@"\n'


def build_wsl_invocation(distro: str, script_linux_path: str) -> list[str]:
    return ["wsl.exe", "--distribution", distro, "--exec", "/bin/bash", script_linux_path]


def validate_root(root: str) -> str:
    text = str(root)
    if is_windows():
        windows_text = text.replace("\\", "/")
        gitbash_prefix = "c:/program files/git/"
        if windows_text.lower().startswith(gitbash_prefix):
            text = "/" + windows_text[len(gitbash_prefix):]
    root = posixpath.normpath(text)
    if not root.startswith("/") or root == "/":
        raise GateError("--root must be an absolute Linux directory below /")
    return root


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise GateError(f"invalid run id {run_id!r}")
    return run_id


def validate_shard(shard: str) -> str:
    if not SHARD_RE.fullmatch(shard):
        raise GateError(f"invalid shard id {shard!r}")
    return shard


def build_setup_script(root: str, windows_repo: str, module_source: str) -> str:
    root = validate_root(root)
    return f"""#!/usr/bin/env bash
set -euo pipefail
ROOT={q(root)}
WINDOWS_REPO={q(windows_repo)}
MODULE_SOURCE={q(module_source)}
step_begin() {{
  STEP_LABEL="$1"
  STEP_START=$(date +%s)
  echo "[gate] $STEP_LABEL"
}}
step_end() {{
  now=$(date +%s)
  echo "[gate] $STEP_LABEL ok in $((now - STEP_START))s"
}}
mkdir -p "$ROOT/bin" "$ROOT/wt" "$ROOT/tmp"
step_begin "python3.12-venv"
if ! dpkg-query -W -f='${{Status}}' python3.12-venv 2>/dev/null | grep -q 'install ok installed'; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3.12-venv
else
  echo "[gate] python3.12-venv already installed"
fi
step_end
step_begin "integration clone"
if [ ! -d "$ROOT/Embryo/.git" ]; then
  git clone "$WINDOWS_REPO" "$ROOT/Embryo"
else
  echo "[gate] clone already present"
fi
git -C "$ROOT/Embryo" config core.autocrlf false
git -C "$ROOT/Embryo" checkout --detach
for wt in "$ROOT"/wt/*; do
  [ -e "$wt/.git" ] || continue
  git -C "$wt" checkout --detach
done
git -C "$ROOT/Embryo" fetch "$WINDOWS_REPO" {q(FETCH_REFSPECS[0])} {q(FETCH_REFSPECS[1])}
step_end
step_begin "venv"
if [ ! -x "$ROOT/venv/bin/python" ]; then
  python3.12 -m venv "$ROOT/venv"
else
  echo "[gate] venv already present"
fi
step_end
step_begin "python dependencies"
if ! "$ROOT/venv/bin/python" -m pip install --help 2>/dev/null | grep -q -- '--group'; then
  "$ROOT/venv/bin/python" -m pip install pip==26.3
fi
(
  cd "$ROOT/Embryo"
  "$ROOT/venv/bin/python" -m pip install --group apps/khitmee_ops/pyproject.toml:dev nodejs-wheel-binaries==24.19.0
)
step_end
step_begin "node wrappers"
NODE_ROOT="$("$ROOT/venv/bin/python" -c 'from nodejs_wheel.executable import ROOT_DIR; print(ROOT_DIR)')"
NODE_BIN="$NODE_ROOT/bin/node"
NPM_CLI="$NODE_ROOT/lib/node_modules/npm/bin/npm-cli.js"
NPX_CLI="$NODE_ROOT/lib/node_modules/npm/bin/npx-cli.js"
[ -x "$NODE_BIN" ] || {{ echo "[gate] missing wheel node at $NODE_BIN" >&2; exit 1; }}
[ -f "$NPM_CLI" ] || {{ echo "[gate] missing npm cli at $NPM_CLI" >&2; exit 1; }}
[ -f "$NPX_CLI" ] || {{ echo "[gate] missing npx cli at $NPX_CLI" >&2; exit 1; }}
ln -sfn "$NODE_BIN" "$ROOT/bin/node"
{{
  echo '#!/usr/bin/env bash'
  printf 'exec %s %s "$@"\\n' "$NODE_BIN" "$NPM_CLI"
}} > "$ROOT/bin/npm"
{{
  echo '#!/usr/bin/env bash'
  printf 'exec %s %s "$@"\\n' "$NODE_BIN" "$NPX_CLI"
}} > "$ROOT/bin/npx"
chmod +x "$ROOT/bin/npm" "$ROOT/bin/npx"
cp -- "$MODULE_SOURCE" "$ROOT/bin/khitmee_gate.py"
chmod +x "$ROOT/bin/khitmee_gate.py"
step_end
step_begin "PATH and knip warm cache"
CLEAN_PATH="$("$ROOT/venv/bin/python" -c 'import os; print(":".join(p for p in os.environ.get("PATH", "").split(":") if p and not p.startswith("/mnt/") and p != "/mnt"))')"
export PATH="$ROOT/bin:$ROOT/venv/bin:$CLEAN_PATH"
"$ROOT/bin/npx" --yes knip@6.46.1 --version
step_end
echo "[gate] setup complete"
"""


def build_linux_job_script(root: str, module_source: str, job_source: str, action: str) -> str:
    root = validate_root(root)
    return f"""#!/usr/bin/env bash
set -euo pipefail
ROOT={q(root)}
mkdir -p "$ROOT/bin" "$ROOT/tmp"
cp -- {q(module_source)} "$ROOT/bin/khitmee_gate.py"
cp -- {q(job_source)} "$ROOT/tmp/gate-job.json"
CLEAN_PATH="$("$ROOT/venv/bin/python" -c 'import os; print(":".join(p for p in os.environ.get("PATH", "").split(":") if p and not p.startswith("/mnt/") and p != "/mnt"))')"
export PATH="$ROOT/bin:$ROOT/venv/bin:$CLEAN_PATH"
exec "$ROOT/venv/bin/python" "$ROOT/bin/khitmee_gate.py" {q(action)} --job "$ROOT/tmp/gate-job.json"
"""


def run_text(cmd: list[str], cwd: str | Path | None = None) -> str:
    try:
        return subprocess.check_output(cmd, cwd=cwd, text=True, stderr=subprocess.PIPE).strip()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        raise GateError(f"command failed: {' '.join(map(str, cmd))}\n{detail}") from error


def git_output(repo: Path, *args: str) -> str:
    return run_text(["git", "-C", str(repo), *args])


def git_status(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            text=True,
            stderr=subprocess.PIPE,
        ).strip(chr(13) + chr(10))
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        raise GateError(f"command failed: git -C {repo} status: {detail}") from error


def discover_common_repo(repo: Path) -> Path:
    common = run_text(["git", "rev-parse", "--git-common-dir"], cwd=repo)
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (repo / common_path).resolve()
    return common_path.parent.resolve()


def discover_branch(repo: Path) -> str:
    branch = run_text(["git", "branch", "--show-current"], cwd=repo)
    if not branch:
        raise GateError(f"integration worktree {repo} is detached; pass --integration-branch")
    return branch


def default_windows_integration() -> Path:
    if is_windows():
        return Path(run_text(["git", "rev-parse", "--show-toplevel"])).resolve()
    for candidate in (Path("/mnt/e/khitmee-2gb-proof"), Path("/mnt/e/Khitmee-ERPNext")):
        if candidate.exists():
            return candidate
    return Path("/mnt/e/Khitmee-ERPNext")


def default_windows_repo(integration: Path) -> str:
    if is_windows():
        try:
            return str(discover_common_repo(integration))
        except GateError:
            return str(integration)
    return "/mnt/e/Khitmee-ERPNext"


def load_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise GateError(f"missing JSON file {path}") from error
    except json.JSONDecodeError as error:
        raise GateError(f"invalid JSON in {path}: {error}") from error


def write_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def updated_at(path: Path) -> str:
    try:
        value = load_json_file(path).get("updated_at")
    except GateError:
        return ""
    return value if isinstance(value, str) else ""


def should_copy_run_state(windows_run: Path, linux_run: Path) -> bool:
    if not linux_run.exists():
        return True
    win_updated = updated_at(windows_run)
    linux_updated = updated_at(linux_run)
    return not (linux_updated and win_updated and linux_updated > win_updated)


def copy_run_state(windows_integration: Path, integration: Path, run_id: str) -> str:
    win_run = windows_integration / ".autoloop/runs" / f"{run_id}.json"
    linux_run = integration / ".autoloop/runs" / f"{run_id}.json"
    if not win_run.is_file():
        raise GateError(f"missing Windows run state {win_run}")
    linux_run.parent.mkdir(parents=True, exist_ok=True)
    if should_copy_run_state(win_run, linux_run):
        shutil.copyfile(win_run, linux_run)
        return "copied"
    return "kept-newer-linux"


def copy_trusted_contracts(windows_integration: Path, integration: Path, run_id: str) -> int:
    source_dir = windows_integration / ".autoloop/trusted-contracts"
    target_dir = integration / ".autoloop/trusted-contracts"
    if not source_dir.is_dir():
        raise GateError(f"missing trusted contracts directory {source_dir}")
    sources = sorted(source_dir.glob(f"{run_id}*.json"))
    files = [source for source in sources if source.is_file()]
    if not files:
        raise GateError(f"no trusted contracts for {run_id} in {source_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    for source in files:
        shutil.copyfile(source, target_dir / source.name)
    return len(files)


def rewrite_run_state(integration: Path, run_id: str, root: str, shards: list[str]) -> list[str]:
    run_path = integration / ".autoloop/runs" / f"{run_id}.json"
    state = load_json_file(run_path)
    state_shards = state.get("shards")
    if not isinstance(state_shards, dict):
        raise GateError(f"run state {run_path} has no shard map")
    changed = []
    for shard in shards:
        if shard not in state_shards:
            raise GateError(f"run {run_id} has no shard {shard!r}")
        linux_worktree = posixpath.join(root, "wt", shard)
        if state_shards[shard].get("worktree_path") != linux_worktree:
            state_shards[shard]["worktree_path"] = linux_worktree
            changed.append(shard)
    if changed:
        write_json_file(run_path, state)
    return changed


def ensure_integration_checkout(integration: Path, branch: str) -> None:
    target = git_output(integration, "rev-parse", "--verify", branch)
    head = git_output(integration, "rev-parse", "HEAD")
    if target == head:
        return
    descendant = subprocess.run(
        ["git", "-C", str(integration), "merge-base", "--is-ancestor", target, head],
        check=False,
    )
    if descendant.returncode == 0:
        return
    unique = git_output(integration, "rev-list", "--count", f"{target}..{head}")
    if unique != "0":
        raise GateError(
            f"Linux integration checkout has {unique} commit(s) not in {branch}; run sync-back before refreshing"
        )
    if git_status(integration):
        raise GateError(
            f"Linux integration checkout is dirty and not at {branch}; run sync-back or clean it first"
        )
    subprocess.run(["git", "-C", str(integration), "checkout", "--detach", branch], check=True)


def build_fetch_command(integration: str | Path, windows_repo: str) -> list[str]:
    return ["git", "-C", str(integration), "fetch", windows_repo, *FETCH_REFSPECS]


def fetch_windows_branches(integration: Path, windows_repo: str) -> None:
    subprocess.run(build_fetch_command(integration, windows_repo), check=True)


def detach_checked_out_branch(repo: Path) -> bool:
    if git_output(repo, "branch", "--show-current"):
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach"], check=True)
        return True
    return False


def detach_managed_worktrees(root: str, integration: Path) -> None:
    detach_checked_out_branch(integration)
    worktrees = Path(root) / "wt"
    if not worktrees.is_dir():
        return
    for child in sorted(worktrees.iterdir()):
        if child.is_dir() and (child / ".git").exists():
            detach_checked_out_branch(child)


def build_worktree_add_command(integration: str | Path, worktree: str | Path, branch: str) -> list[str]:
    return ["git", "-C", str(integration), "worktree", "add", "--detach", str(worktree), branch]


def ensure_linux_worktree(integration: Path, root: str, shard: str) -> Path:
    branch = f"shard/{shard}"
    worktree = Path(root) / "wt" / shard
    git_output(integration, "rev-parse", "--verify", branch)
    if not worktree.exists():
        subprocess.run(build_worktree_add_command(integration, worktree, branch), check=True)
        return worktree
    if not (worktree / ".git").exists():
        raise GateError(f"worktree path {worktree} exists but is not a git worktree")
    if git_status(worktree):
        raise GateError(f"Linux shard worktree {worktree} is dirty; refusing to refresh it")
    subprocess.run(["git", "-C", str(worktree), "checkout", "--quiet", "--detach", branch], check=True)
    return worktree


def build_check_command(
    python: str,
    cli: str | Path,
    integration: str | Path,
    run_id: str,
    shard: str,
    worktree: str | Path,
) -> list[str]:
    return [
        python,
        str(cli),
        "--repo",
        str(integration),
        "check",
        "--run-id",
        run_id,
        "--shard",
        shard,
        "--worktree",
        str(worktree),
    ]


def write_result(job: dict, payload: dict) -> None:
    result = job.get("result_path")
    if result:
        write_json_file(Path(result), payload)


def linux_check(job: dict) -> int:
    root = validate_root(str(job.get("root") or DEFAULT_ROOT))
    run_id = validate_run_id(str(job["run_id"]))
    shards = [validate_shard(str(shard)) for shard in job["shards"]]
    integration = Path(root) / "Embryo"
    windows_integration = Path(str(job["windows_integration"]))
    windows_repo = str(job["windows_repo"])
    branch = str(job.get("integration_branch") or "")
    cli = Path(str(job.get("cli") or integration / "tools/autoloop/cli.py"))
    if not integration.is_dir():
        raise GateError(f"missing Linux integration checkout {integration}; run gate setup first")
    if not cli.is_file():
        raise GateError(f"missing autoloop CLI {cli}")
    if not branch:
        branch = discover_branch(windows_integration)

    detach_managed_worktrees(root, integration)
    fetch_windows_branches(integration, windows_repo)
    ensure_integration_checkout(integration, branch)
    state_action = copy_run_state(windows_integration, integration, run_id)
    contracts = copy_trusted_contracts(windows_integration, integration, run_id)
    rewritten = rewrite_run_state(integration, run_id, root, shards)
    print(f"[gate] state {state_action}; copied {contracts} trusted contract file(s)")
    if rewritten:
        print("[gate] rewrote worktree_path for " + ", ".join(rewritten))

    env = os.environ.copy()
    env["PATH"] = gate_path(root, env.get("PATH", ""))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    python = str(Path(root) / "venv/bin/python")
    failures = []
    for shard in shards:
        print(f"[gate] preparing shard/{shard}")
        try:
            worktree = ensure_linux_worktree(integration, root, shard)
            command = build_check_command(
                python,
                cli,
                integration,
                run_id,
                shard,
                worktree,
            )
            print(f"[gate] running shard {shard}")
            proc = subprocess.run(command, check=False, cwd=integration, env=env)
            if proc.returncode == 0:
                print(f"[gate] PASS {shard}")
            else:
                print(f"[gate] FAIL {shard} exit={proc.returncode}")
                failures.append(shard)
        except (GateError, subprocess.CalledProcessError) as error:
            print(f"[gate] FAIL {shard}: {error}")
            failures.append(shard)
    payload = {"ok": not failures, "failed_shards": failures, "checked_shards": shards}
    write_result(job, payload)
    if failures:
        print("[gate] FAIL " + ", ".join(failures))
        return 1
    print("[gate] PASS all requested shards")
    return 0


def sync_status_paths(integration: Path) -> tuple[list[str], list[str]]:
    raw = git_status(integration)
    allowed = []
    disallowed = []
    for line in raw.splitlines():
        path = line[3:] if len(line) > 3 else ""
        if path.startswith(".autoloop/runs/"):
            allowed.append(path)
        else:
            disallowed.append(path)
    return allowed, disallowed


def linux_sync_back(job: dict) -> int:
    root = validate_root(str(job.get("root") or DEFAULT_ROOT))
    integration = Path(root) / "Embryo"
    windows_repo = str(job["windows_repo"])
    if not integration.is_dir():
        raise GateError(f"missing Linux integration checkout {integration}; run gate setup first")
    allowed, disallowed = sync_status_paths(integration)
    if disallowed:
        raise GateError(
            "Linux integration checkout has non-run-state changes: " + ", ".join(disallowed[:10])
        )
    committed = False
    if allowed:
        subprocess.run(["git", "-C", str(integration), "add", "--", ".autoloop/runs"], check=True)
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "khitmee-gate")
        env.setdefault("GIT_AUTHOR_EMAIL", "khitmee-gate@localhost")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        subprocess.run(
            ["git", "-C", str(integration), "commit", "-m", "gate: sync autoloop state"],
            check=True,
            env=env,
        )
        committed = True
    else:
        print("[gate] no Linux run-state changes to commit")
    head = git_output(integration, "rev-parse", "HEAD")
    if committed:
        subprocess.run(
            ["git", "-C", str(integration), "push", windows_repo, "HEAD:refs/heads/gate/state", "-f"],
            check=True,
        )
        print(f"[gate] pushed gate/state at {head[:12]}")
    write_result(job, {"ok": True, "committed": committed, "head": head})
    return 0


def ensure_windows_integration_clean(path: Path) -> None:
    status = run_text(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"])
    if status:
        raise GateError(f"Windows integration worktree {path} is dirty; refusing sync-back")


def normalize_linux_inputs(args: argparse.Namespace) -> tuple[str, str, str, str]:
    integration = Path(args.windows_integration)
    resolved = integration.resolve() if is_windows() else integration
    windows_integration = windows_to_wsl_path(resolved) if is_windows() else str(resolved)
    windows_repo = args.windows_repo or default_windows_repo(integration)
    windows_repo_wsl = windows_to_wsl_path(windows_repo) if is_windows() else windows_repo
    branch = args.integration_branch
    if not branch and is_windows():
        branch = discover_branch(integration)
    return str(resolved), windows_integration, windows_repo_wsl, branch or ""


def execute_wsl_script(script: str, distro: str, tempdir: Path, name: str) -> int:
    script_path = tempdir / name
    script_path.write_bytes(script.replace("\r\n", "\n").encode("utf-8"))
    invocation = build_wsl_invocation(distro, windows_to_wsl_path(script_path))
    return subprocess.run(invocation, check=False).returncode


def run_windows_setup(args: argparse.Namespace) -> int:
    windows_repo = args.windows_repo or default_windows_repo(Path(args.windows_integration))
    module_source = windows_to_wsl_path(Path(__file__).resolve())
    script = build_setup_script(args.root, windows_to_wsl_path(windows_repo), module_source)
    with tempfile.TemporaryDirectory(prefix="khitmee-gate-") as tmp:
        return execute_wsl_script(script, args.distro, Path(tmp), "gate-setup.sh")


def make_job(args: argparse.Namespace, action: str, result_path: Path | None) -> dict:
    _, windows_integration, windows_repo, branch = normalize_linux_inputs(args)
    job = {
        "action": action,
        "root": validate_root(args.root),
        "windows_integration": windows_integration,
        "windows_repo": windows_repo,
        "integration_branch": branch,
    }
    if result_path is not None:
        job["result_path"] = windows_to_wsl_path(result_path) if is_windows() else str(result_path)
    if action == "check":
        job["run_id"] = validate_run_id(args.run_id)
        job["shards"] = [validate_shard(shard) for shard in args.shard]
        if args.cli:
            job["cli"] = windows_to_wsl_path(args.cli) if is_windows() else str(args.cli)
    return job


def run_windows_job(args: argparse.Namespace, action: str) -> tuple[int, dict]:
    with tempfile.TemporaryDirectory(prefix="khitmee-gate-") as tmp:
        tempdir = Path(tmp)
        job_path = tempdir / "gate-job.json"
        result_path = tempdir / "gate-result.json"
        job = make_job(args, action, result_path)
        write_json_file(job_path, job)
        script = build_linux_job_script(
            args.root,
            windows_to_wsl_path(Path(__file__).resolve()),
            windows_to_wsl_path(job_path),
            f"linux-{action}",
        )
        code = execute_wsl_script(script, args.distro, tempdir, f"gate-{action}.sh")
        result = load_json_file(result_path) if result_path.is_file() else {}
        return code, result


def run_linux_direct(args: argparse.Namespace, action: str, result_path: Path | None = None) -> int:
    job = make_job(args, action, result_path)
    if action == "check":
        return linux_check(job)
    return linux_sync_back(job)


def merge_gate_state_on_windows(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "merge", "--ff-only", "gate/state"], check=True)


def command_setup(args: argparse.Namespace) -> int:
    if is_windows():
        return run_windows_setup(args)
    root = validate_root(args.root)
    windows_repo = args.windows_repo or default_windows_repo(Path(args.windows_integration))
    script = build_setup_script(root, windows_repo, str(Path(__file__).resolve()))
    with tempfile.TemporaryDirectory(prefix="khitmee-gate-") as tmp:
        script_path = Path(tmp) / "gate-setup.sh"
        script_path.write_bytes(script.encode("utf-8"))
        return subprocess.run(["/bin/bash", str(script_path)], check=False).returncode


def command_check(args: argparse.Namespace) -> int:
    if is_windows():
        code, _ = run_windows_job(args, "check")
        return code
    return run_linux_direct(args, "check")


def command_sync_back(args: argparse.Namespace) -> int:
    if is_windows():
        windows_integration = Path(args.windows_integration).resolve()
        ensure_windows_integration_clean(windows_integration)
        code, result = run_windows_job(args, "sync-back")
        if code != 0:
            return code
        if result.get("committed"):
            merge_gate_state_on_windows(windows_integration)
            print("[gate] Windows integration fast-forwarded to gate/state")
        else:
            print("[gate] sync-back complete; nothing to merge")
        return 0
    ensure_windows_integration_clean(Path(args.windows_integration))
    with tempfile.TemporaryDirectory(prefix="khitmee-gate-") as tmp:
        result_path = Path(tmp) / "result.json"
        code = run_linux_direct(args, "sync-back", result_path)
        if code != 0:
            return code
        result = load_json_file(result_path) if result_path.is_file() else {}
    if result.get("committed"):
        integration = Path(args.windows_integration)
        subprocess.run(
            ["git", "-C", str(integration), "merge", "--ff-only", "gate/state"],
            check=True,
        )
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--distro", default=DEFAULT_DISTRO)
    parser.add_argument("--windows-repo", default=None)
    parser.add_argument("--windows-integration", default=None)
    parser.add_argument("--integration-branch", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="khitmee-gate")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{setup,check,sync-back}")
    setup = sub.add_parser("setup", help="provision the Linux gate host")
    add_common(setup)
    check = sub.add_parser("check", help="run autoloop shard gates on the Linux host")
    add_common(check)
    check.add_argument("--run-id", required=True)
    check.add_argument("--shard", action="append", required=True)
    check.add_argument("--cli", default=None)
    sync = sub.add_parser("sync-back", help="commit Linux gate state and fast-forward Windows")
    add_common(sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("linux-check", "linux-sync-back"):
        internal = argparse.ArgumentParser(prog=f"khitmee-gate {argv[0]}")
        internal.add_argument("--job", required=True)
        args = internal.parse_args(argv[1:])
        try:
            if argv[0] == "linux-check":
                return linux_check(load_json_file(Path(args.job)))
            return linux_sync_back(load_json_file(Path(args.job)))
        except (GateError, subprocess.CalledProcessError, OSError) as error:
            print(f"[gate] ERROR {error}", file=sys.stderr)
            return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "windows_integration") and args.windows_integration is None:
        args.windows_integration = str(default_windows_integration())
    try:
        if args.command == "setup":
            return command_setup(args)
        if args.command == "check":
            return command_check(args)
        if args.command == "sync-back":
            return command_sync_back(args)
    except (GateError, subprocess.CalledProcessError, OSError) as error:
        print(f"[gate] ERROR {error}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
