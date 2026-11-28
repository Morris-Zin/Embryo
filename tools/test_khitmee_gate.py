"""Unit tests for the Linux gate-host orchestration tooling."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import khitmee_gate as kg


class TestPathTranslation(unittest.TestCase):
    def test_windows_drive_paths_translate_to_wsl_mounts(self):
        self.assertEqual(
            kg.windows_to_wsl_path(r"E:\Khitmee-ERPNext\apps"),
            "/mnt/e/Khitmee-ERPNext/apps",
        )
        self.assertEqual(kg.windows_to_wsl_path("C:/Users/User/x y"), "/mnt/c/Users/User/x y")
        self.assertEqual(kg.windows_to_wsl_path("E:/"), "/mnt/e")
        self.assertEqual(kg.windows_to_wsl_path(r"\\?\E:\Khitmee-ERPNext"), "/mnt/e/Khitmee-ERPNext")

    def test_posix_paths_pass_through_and_unc_paths_fail_closed(self):
        self.assertEqual(kg.windows_to_wsl_path("/root/gate"), "/root/gate")
        with self.assertRaises(kg.GateError):
            kg.windows_to_wsl_path(r"\\server\share\repo")

    def test_gitbash_converted_root_is_interpreted_as_linux_absolute(self):
        with mock.patch.object(kg, "is_windows", return_value=True):
            self.assertEqual(kg.validate_root("C:/Program Files/Git/root/gate-test"), "/root/gate-test")


class TestPathFiltering(unittest.TestCase):
    def test_windows_interop_entries_are_removed(self):
        source = "/usr/bin:/mnt/c/Windows/system32:/mnt/e/Khitmee-ERPNext:/bin::/mnt"
        self.assertEqual(kg.filter_interop_path(source), "/usr/bin:/bin")

    def test_gate_path_prepends_linux_tools_and_keeps_no_windows_entries(self):
        value = kg.gate_path("/root/gate", "/mnt/c/Windows:/usr/bin:/mnt/d/x:/bin")
        self.assertEqual(value, "/root/gate/bin:/root/gate/venv/bin:/usr/bin:/bin")
        self.assertNotIn("/mnt/", value)


class TestWrapperAndScripts(unittest.TestCase):
    def test_npx_wrapper_is_a_script_with_exact_exec_shape(self):
        content = kg.node_wrapper_content(
            "/wheel/bin/node",
            "/wheel/lib/node_modules/npm/bin/npx-cli.js",
        )
        self.assertEqual(
            content,
            '#!/usr/bin/env bash\nexec /wheel/bin/node /wheel/lib/node_modules/npm/bin/npx-cli.js "$@"\n',
        )

    def test_setup_script_contains_idempotent_manual_gatehost_steps(self):
        script = kg.build_setup_script("/root/gate-test", "/mnt/e/Khitmee-ERPNext", "/mnt/c/tools/khitmee_gate.py")
        self.assertIn('git clone "$WINDOWS_REPO" "$ROOT/Embryo"', script)
        self.assertIn('git -C "$ROOT/Embryo" config core.autocrlf false', script)
        self.assertIn("'+refs/heads/feat/*:refs/heads/feat/*'", script)
        self.assertIn("'+refs/heads/shard/*:refs/heads/shard/*'", script)
        self.assertIn("apt-get install -y python3.12-venv", script)
        self.assertIn('git -C "$ROOT/Embryo" checkout --detach', script)
        self.assertIn('git -C "$wt" checkout --detach', script)
        self.assertIn("--group apps/khitmee_ops/pyproject.toml:dev nodejs-wheel-binaries", script)
        self.assertIn("pip install pip==26.3", script)
        self.assertIn('ln -sfn "$NODE_BIN" "$ROOT/bin/node"', script)
        self.assertIn('printf \'exec %s %s "$@"\\n\' "$NODE_BIN" "$NPX_CLI"', script)
        self.assertIn('"$ROOT/bin/npx" --yes knip@6.46.1 --version', script)
        self.assertIn('not p.startswith("/mnt/")', script)
        self.assertNotIn("bash -c", script)

    def test_linux_job_script_copies_job_and_uses_script_file_not_inline_arguments(self):
        script = kg.build_linux_job_script(
            "/root/gate-test",
            "/mnt/c/tools/khitmee_gate.py",
            "/mnt/c/job with spaces.json",
            "linux-check",
        )
        self.assertIn("cp -- '/mnt/c/job with spaces.json' \"$ROOT/tmp/gate-job.json\"", script)
        self.assertIn('exec "$ROOT/venv/bin/python" "$ROOT/bin/khitmee_gate.py"', script)
        self.assertIn("'linux-check'", script)
        self.assertNotIn("bash -c", script)


class TestCommandPlans(unittest.TestCase):
    def test_wsl_invocation_executes_a_script_file_without_shell_reparse(self):
        invocation = kg.build_wsl_invocation("KhitmeeERPNext", "/mnt/c/tmp/gate.sh")
        self.assertEqual(
            invocation,
            ["wsl.exe", "--distribution", "KhitmeeERPNext", "--exec", "/bin/bash", "/mnt/c/tmp/gate.sh"],
        )
        self.assertNotIn("bash -c", " ".join(invocation))

    def test_fetch_and_check_command_plans(self):
        fetch = kg.build_fetch_command("/root/gate/Embryo", "/mnt/e/Khitmee-ERPNext")
        self.assertEqual(
            fetch,
            [
                "git",
                "-C",
                "/root/gate/Embryo",
                "fetch",
                "/mnt/e/Khitmee-ERPNext",
                "+refs/heads/feat/*:refs/heads/feat/*",
                "+refs/heads/shard/*:refs/heads/shard/*",
            ],
        )
        self.assertEqual(
            kg.build_worktree_add_command("/root/gate/Embryo", "/root/gate/wt/docs", "shard/docs"),
            [
                "git",
                "-C",
                "/root/gate/Embryo",
                "worktree",
                "add",
                "--detach",
                "/root/gate/wt/docs",
                "shard/docs",
            ],
        )
        check = kg.build_check_command(
            "/root/gate/venv/bin/python",
            "/root/gate/Embryo/tools/autoloop/cli.py",
            "/root/gate/Embryo",
            "run-1",
            "verify_tooling",
            "/root/gate/wt/verify_tooling",
        )
        self.assertEqual(
            check,
            [
                "/root/gate/venv/bin/python",
                "/root/gate/Embryo/tools/autoloop/cli.py",
                "--repo",
                "/root/gate/Embryo",
                "check",
                "--run-id",
                "run-1",
                "--shard",
                "verify_tooling",
                "--worktree",
                "/root/gate/wt/verify_tooling",
            ],
        )

    def test_execute_wsl_script_writes_lf_script_and_invokes_wsl(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = mock.Mock(returncode=0)
            with mock.patch.object(kg.subprocess, "run", return_value=completed) as run:
                code = kg.execute_wsl_script("echo hi\r\n", "KhitmeeERPNext", Path(tmp), "plan.sh")
            self.assertEqual(code, 0)
            script = Path(tmp) / "plan.sh"
            self.assertEqual(script.read_bytes(), b"echo hi\n")
            argv = run.call_args.args[0]
            self.assertEqual(argv[:4], ["wsl.exe", "--distribution", "KhitmeeERPNext", "--exec"])
            self.assertEqual(argv[4], "/bin/bash")
            self.assertTrue(argv[5].startswith("/mnt/"))
            self.assertNotIn("-c", argv)

    def test_make_job_translates_windows_inputs_for_linux_worker(self):
        args = argparse.Namespace(
            root="/root/gate-test",
            windows_integration=r"E:\khitmee-2gb-proof",
            windows_repo=r"E:\Khitmee-ERPNext",
            integration_branch="feat/backend-2gb-proof",
            run_id="run-1",
            shard=["verify_tooling", "docs"],
            cli=r"E:\khitmee-2gb-proof\tools\autoloop\cli.py",
        )
        with tempfile.TemporaryDirectory() as tmp:
            job = kg.make_job(args, "check", Path(tmp) / "result.json")
        self.assertEqual(job["windows_integration"], "/mnt/e/khitmee-2gb-proof")
        self.assertEqual(job["windows_repo"], "/mnt/e/Khitmee-ERPNext")
        self.assertEqual(job["cli"], "/mnt/e/khitmee-2gb-proof/tools/autoloop/cli.py")
        self.assertEqual(job["shards"], ["verify_tooling", "docs"])
        self.assertTrue(job["result_path"].startswith("/mnt/"))


class TestRunStateHandling(unittest.TestCase):
    def write_run(self, path, updated, worktree):
        payload = {
            "run_id": "run-1",
            "updated_at": updated,
            "shards": {"verify_tooling": {"worktree_path": worktree}},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_rewrite_run_state_points_requested_shards_at_linux_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            integration = Path(tmp)
            run_path = integration / ".autoloop/runs/run-1.json"
            self.write_run(run_path, "2026-01-01T00:00:00Z", r"C:\old\worktree")
            changed = kg.rewrite_run_state(integration, "run-1", "/root/gate", ["verify_tooling"])
            payload = json.loads(run_path.read_text(encoding="utf-8"))
        self.assertEqual(changed, ["verify_tooling"])
        self.assertEqual(
            payload["shards"]["verify_tooling"]["worktree_path"],
            "/root/gate/wt/verify_tooling",
        )

    def test_newer_linux_run_state_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            win = root / "win/.autoloop/runs/run-1.json"
            linux = root / "linux/.autoloop/runs/run-1.json"
            self.write_run(win, "2026-01-01T00:00:00Z", "windows")
            self.write_run(linux, "2026-01-02T00:00:00Z", "linux")
            self.assertFalse(kg.should_copy_run_state(win, linux))
            action = kg.copy_run_state(root / "win", root / "linux", "run-1")
            payload = json.loads(linux.read_text(encoding="utf-8"))
        self.assertEqual(action, "kept-newer-linux")
        self.assertEqual(payload["shards"]["verify_tooling"]["worktree_path"], "linux")

    def test_sync_status_partitions_run_state_from_other_edits(self):
        status = " M .autoloop/runs/run-1.json\n?? .autoloop/runs/new.json\n M tools/x.py"
        with mock.patch.object(kg, "git_status", return_value=status):
            allowed, disallowed = kg.sync_status_paths(Path("repo"))
        self.assertEqual(allowed, [".autoloop/runs/run-1.json", ".autoloop/runs/new.json"])
        self.assertEqual(disallowed, ["tools/x.py"])

    def test_git_status_preserves_porcelain_status_columns(self):
        with mock.patch.object(kg.subprocess, "check_output", return_value=" M file.py\n") as check_output:
            self.assertEqual(kg.git_status(Path("repo")), " M file.py")
        self.assertIn("--porcelain", check_output.call_args.args[0])

    def test_windows_sync_refuses_a_dirty_integration_worktree(self):
        with (
            mock.patch.object(kg, "run_text", return_value=" M .autoloop/runs/run-1.json"),
            self.assertRaises(kg.GateError),
        ):
            kg.ensure_windows_integration_clean(Path("integration"))

    def test_integration_refresh_rejects_unsynced_detached_commits(self):
        def git_output(repo, *args):
            values = {
                ("rev-parse", "--verify", "feat/x"): "target",
                ("rev-parse", "HEAD"): "head",
                ("rev-list", "--count", "target..head"): "1",
            }
            return values[args]

        with (
            mock.patch.object(kg, "git_output", side_effect=git_output),
            mock.patch.object(kg.subprocess, "run", return_value=mock.Mock(returncode=1)),
            self.assertRaisesRegex(kg.GateError, "1 commit"),
        ):
            kg.ensure_integration_checkout(Path("integration"), "feat/x")

    def test_integration_refresh_checks_out_clean_ancestor_head(self):
        def git_output(repo, *args):
            values = {
                ("rev-parse", "--verify", "feat/x"): "target",
                ("rev-parse", "HEAD"): "head",
                ("rev-list", "--count", "target..head"): "0",
            }
            return values[args]

        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(returncode=1 if "merge-base" in cmd else 0)

        with (
            mock.patch.object(kg, "git_output", side_effect=git_output),
            mock.patch.object(kg, "git_status", return_value=""),
            mock.patch.object(kg.subprocess, "run", side_effect=run),
        ):
            kg.ensure_integration_checkout(Path("integration"), "feat/x")
        self.assertEqual(calls[-1][-3:], ["checkout", "--detach", "feat/x"])


if __name__ == "__main__":
    unittest.main()
