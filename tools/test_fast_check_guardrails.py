import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
	sys.path.insert(0, str(TOOLS_DIR))

import fast_check as fc


def write_source(root, rel, source):
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(source, encoding="utf-8", newline="\n")
	return path


class TestMonkeyPatchGuardrail(unittest.TestCase):
	def scan(self, source):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "sample.py"
			path.write_text(source, encoding="utf-8", newline="\n")
			return fc.scan_monkey_patch_file(path, "sample.py")

	def test_detects_attribute_assignment_on_imported_core_modules_and_symbols(self):
		violations = self.scan(
			"import erpnext\n"
			"import erpnext.accounts.report.gross_profit as gross_profit\n"
			"from frappe.utils import flt\n"
			"erpnext.patched = True\n"
			"gross_profit.execute = lambda: None\n"
			"flt.patched = True\n"
		)

		self.assertEqual(len(violations), 3)
		self.assertTrue(all(violation.rule == "anti-monkey-patch" for violation in violations))
		self.assertIn("erpnext.patched", violations[0].message)

	def test_detects_augassign_annassign_and_setattr_on_core_symbols(self):
		violations = self.scan(
			"import frappe\n"
			"import erpnext\n"
			"frappe.counter += 1\n"
			"erpnext.answer: int = 42\n"
			"setattr(frappe, 'whitelist', lambda: None)\n"
			"setattr(erpnext.accounts, 'x', 1)\n"
		)

		self.assertEqual(len(violations), 4)
		self.assertTrue(any("setattr" not in violation.message for violation in violations))

	def test_allows_request_local_flags_and_instance_state(self):
		violations = self.scan(
			"import frappe\n"
			"frappe.local.cart = {}\n"
			"frappe.local.cart.items = []\n"
			"frappe.flags.ignore_permissions = True\n"
			"setattr(frappe.local, 'cart', {})\n"
			"setattr(frappe.flags, 'ignore_permissions', True)\n"
			"self.cart = {}\n"
		)

		self.assertEqual(violations, [])

	def test_rejects_replacing_local_container_itself(self):
		violations = self.scan(
			"import frappe\n"
			"frappe.local = {}\n"
			"setattr(frappe, 'local', {})\n"
		)

		self.assertEqual(len(violations), 2)

	def test_production_scan_excludes_tests_and_setup(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			bad = "import frappe\nfrappe.whitelist = lambda: None\n"
			write_source(root, "apps/khitmee_ops/khitmee_ops/tests/test_x.py", bad)
			write_source(root, "apps/khitmee_ops/khitmee_ops/setup/indexes.py", bad)

			self.assertEqual(fc.scan_monkey_patches(root), [])


class TestCoreAndHooksGuardrails(unittest.TestCase):
	def test_core_file_edit_detection_scoped_to_root_core_packages(self):
		violations = fc.core_change_violations([
			"erpnext/accounts/report/gross_profit/gross_profit.py",
			"frappe/model/document.py",
			"frappe-config/compose.yaml",
			"apps/khitmee_ops/khitmee_ops/hooks.py",
		])

		self.assertEqual([violation.path for violation in violations], [
			"erpnext/accounts/report/gross_profit/gross_profit.py",
			"frappe/model/document.py",
		])

	def scan_hooks(self, source):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "hooks.py"
			path.write_text(source, encoding="utf-8", newline="\n")
			return fc.scan_hooks_overrides_file(path, "hooks.py")

	def test_hooks_override_detection_rejects_unallowlisted_entries(self):
		violations = self.scan_hooks(
			"override_doctype_class = {'Sales Invoice': 'x.Custom'}\n"
			"override_whitelisted_methods = {'frappe.core.x': 'x.y'}\n"
		)

		self.assertEqual(len(violations), 2)
		self.assertTrue(all(violation.rule == "hooks-overrides" for violation in violations))

	def test_hooks_override_detection_accepts_empty_static_dicts(self):
		self.assertEqual(self.scan_hooks("override_doctype_class = {}\n"), [])

	def test_hooks_override_detection_fails_closed_for_nonstatic_values(self):
		violations = self.scan_hooks("import x\noverride_doctype_class = x.OVERRIDES\n")

		self.assertEqual(len(violations), 1)
		self.assertIn("statically inspectable", violations[0].message)


class TestPackageBoundaryGuardrail(unittest.TestCase):
	def test_summary_table_boundary_bypass_detection_and_query_hint(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			term = fc.SUMMARY_DOCTYPES[0]
			table_term = "tab" + term
			write_source(
				root,
				"apps/khitmee_ops/khitmee_ops/api/bad.py",
				"doctype = " + repr(term) + "\n"
				"sql = " + repr("SELECT x FROM " + table_term) + "\n",
			)

			violations = fc.scan_package_boundaries(root)

		self.assertGreaterEqual(len(violations), 2)
		self.assertTrue(all(violation.rule == "package-boundary" for violation in violations))
		self.assertTrue(any(fc.QUERY_PROFIT_HINT in violation.message for violation in violations))

	def test_summary_boundary_allows_store_doctype_setup_and_tests(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			term = fc.SUMMARY_DOCTYPES[1]
			source = "name = " + repr(term) + "\n"
			for rel in (
				"apps/khitmee_ops/khitmee_ops/profit_summary/store.py",
				"apps/khitmee_ops/khitmee_ops/khitmee_ops/doctype/example/example.py",
				"apps/khitmee_ops/khitmee_ops/setup/indexes.py",
				"apps/khitmee_ops/khitmee_ops/tests/test_example.py",
			):
				write_source(root, rel, source)

			self.assertEqual(fc.scan_package_boundaries(root), [])

	def test_export_log_boundary_has_separate_service_and_api_allowlist(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			term = fc.EXPORT_LOG_DOCTYPE
			source = "name = " + repr(term) + "\n"
			write_source(root, "apps/khitmee_ops/khitmee_ops/api/other.py", source)

			violations = fc.scan_package_boundaries(root)
			self.assertEqual(len(violations), 1)
			self.assertIn("export service boundary", violations[0].message)

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			for rel in (
				"apps/khitmee_ops/khitmee_ops/profit_summary/export_service.py",
				"apps/khitmee_ops/khitmee_ops/api/profit_export.py",
			):
				write_source(root, rel, source)
			write_source(root, "apps/khitmee_ops/khitmee_ops/profit_summary/store.py", source)

			violations = fc.scan_package_boundaries(root)
			self.assertEqual(len(violations), 1)
			self.assertIn("profit_summary/store.py", violations[0].path)


class TestSqlFootgunGuardrail(unittest.TestCase):
	def scan(self, source, display_path="apps/khitmee_ops/khitmee_ops/api/example.py"):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "example.py"
			path.write_text(source, encoding="utf-8", newline="\n")
			return fc.scan_sql_footguns_file(path, display_path)

	def test_select_star_is_rejected_but_select_one_and_count_star_are_allowed(self):
		violations = self.scan("bad = 'SELECT * FROM `tabX`'\n")
		self.assertEqual(len(violations), 1)
		self.assertIn("SELECT * FROM", violations[0].message)

		allowed = self.scan(
			"one = 'SELECT 1 FROM `tabX` LIMIT 1'\n"
			"count = 'SELECT COUNT(*) FROM `tabX`'\n"
		)
		self.assertEqual(allowed, [])

	def test_unbatched_fstring_dynamic_in_is_rejected(self):
		violations = self.scan(
			"def query(ids):\n"
			"    placeholders = ', '.join(['%s'] * len(ids))\n"
			"    return f'SELECT x FROM t WHERE id IN ({placeholders})'\n"
		)

		self.assertEqual(len(violations), 1)
		self.assertIn("dynamic IN placeholders", violations[0].message)

	def test_unbatched_format_dynamic_in_is_rejected(self):
		violations = self.scan(
			"def query(ids):\n"
			"    placeholders = ', '.join(['%s'] * len(ids))\n"
			"    return 'SELECT x FROM t WHERE id IN ({placeholders})'.format(placeholders=placeholders)\n"
		)

		self.assertEqual(len(violations), 1)

	def test_batched_dynamic_in_and_static_in_are_allowed(self):
		violations = self.scan(
			"def query(ids):\n"
			"    rows = []\n"
			"    for chunk in batched(ids, 100):\n"
			"        placeholders = ', '.join(['%s'] * len(chunk))\n"
			"        rows.append(f'SELECT x FROM t WHERE id IN ({placeholders})')\n"
			"    static = \"SELECT x FROM t WHERE status IN ('a', 'b')\"\n"
			"    return rows, static\n"
		)

		self.assertEqual(violations, [])

	def test_documented_transitional_allowlist_entry_is_accepted(self):
		violations = self.scan(
			"def _fetch_rates_and_quantities(rows):\n"
			"    placeholders = ', '.join(['%s'] * len(rows))\n"
			"    return f'SELECT x FROM t WHERE id IN ({placeholders})'\n",
			fc.SEARCH_PATH,
		)

		self.assertEqual(violations, [])


class TestOracleRegistryGuardrail(unittest.TestCase):
	def test_unregistered_report_module_fails_oracle_registry(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			write_source(
				root,
				"apps/khitmee_ops/khitmee_ops/khitmee_ops/report/new_report/new_report.py",
				"def execute(filters=None):\n    return [], []\n",
			)

			violations = fc.scan_oracle_registry(root)

		self.assertEqual(len(violations), 1)
		self.assertEqual(violations[0].rule, "oracle-registry")
		self.assertIn("ORACLE_REGISTRY", violations[0].message)

	def test_no_oracle_reason_near_top_is_accepted(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			write_source(
				root,
				"apps/khitmee_ops/khitmee_ops/khitmee_ops/report/new_report/new_report.py",
				"# no-oracle-reason: pure presentation adapter with no financial calculation\n"
				"def execute(filters=None):\n    return [], []\n",
			)

			self.assertEqual(fc.scan_oracle_registry(root), [])

	def test_registered_module_requires_existing_parity_test(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			write_source(
				root,
				"apps/khitmee_ops/khitmee_ops/khitmee_ops/report/fast_gross_profit/fast_gross_profit.py",
				"def execute(filters=None):\n    return [], []\n",
			)

			violations = fc.scan_oracle_registry(root)

		self.assertEqual(len(violations), 1)
		self.assertIn("does not exist", violations[0].message)

	def test_profit_summary_modules_are_discovered_when_present(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			write_source(
				root,
				"apps/khitmee_ops/khitmee_ops/profit_summary/query.py",
				"def query_profit(filters):\n    return {}\n",
			)

			violations = fc.scan_oracle_registry(root)

		self.assertEqual(len(violations), 1)
		self.assertIn("profit_summary/query.py", violations[0].path)


class TestDevToolGate(unittest.TestCase):
	def test_missing_dev_tools_are_reported_together(self):
		def failing_runner(*args, **kwargs):
			raise FileNotFoundError

		missing = fc.missing_dev_tools(
			which=lambda name: None,
			find_spec=lambda name: None,
			runner=failing_runner,
			script_finder=lambda name, which=None: None,
		)

		self.assertEqual(missing, ["ruff", "hypothesis", "unittest-parallel", "zizmor"])

	def test_missing_dev_tool_diagnostic_contains_exact_bootstrap_command(self):
		with mock.patch.object(fc, "missing_dev_tools", return_value=["ruff", "hypothesis", "unittest-parallel", "zizmor"]):
			with self.assertRaises(RuntimeError) as raised:
				fc.assert_dev_tools_available()

		self.assertIn("ruff", str(raised.exception))
		self.assertIn("hypothesis", str(raised.exception))
		self.assertIn("unittest-parallel", str(raised.exception))
		self.assertIn("zizmor", str(raised.exception))
		self.assertIn(fc.DEV_BOOTSTRAP_COMMAND, str(raised.exception))
		self.assertIn("bootstrap with exactly:", str(raised.exception))

	def test_python_module_ruff_counts_as_available(self):
		class Result:
			returncode = 0

		missing = fc.missing_dev_tools(
			which=lambda name: None,
			find_spec=lambda name: object(),
			runner=lambda *args, **kwargs: Result(),
			script_finder=lambda name, which=None: "/venv/bin/zizmor",
		)

		self.assertEqual(missing, [])


class TestZizmorGate(unittest.TestCase):
	def test_zizmor_gate_is_explicitly_offline(self):
		with (
			mock.patch.object(fc, "assert_dev_tools_available"),
			mock.patch.object(fc, "installed_script", return_value="/venv/bin/zizmor"),
			mock.patch.object(fc, "run", return_value="") as run_mock,
		):
			fc.zizmor_workflows(Path("/repo"))

		argv = run_mock.call_args.args[0]
		self.assertEqual(argv[0], "/venv/bin/zizmor")
		self.assertIn("--offline", argv)
		self.assertIn("--collect=workflows", argv)


class TestParallelUnittestSelection(unittest.TestCase):
	def test_tools_suite_uses_measured_class_parallelism(self):
		command = fc.unittest_parallel_command(Path("/repo"))

		self.assertIn("--level", command)
		self.assertEqual(command[command.index("--level") + 1], "class")
		self.assertEqual(command[command.index("-j") + 1], "4")
		self.assertEqual(command[command.index("-s") + 1], "/repo/tools")

	def test_financial_and_seeder_suites_keep_sequential_discovery(self):
		self.assertEqual(fc.PYTHON_TEST_SERIAL_SUITES, (
			"apps/khitmee_ops/khitmee_ops/tests",
			"perf-suite/seeder",
			"perf-suite/k6",
			"perf-suite/offload",
		))


class TestVerificationEvidenceSafeguards(unittest.TestCase):
	def test_uv_bootstrap_pin_stays_in_dependency_manifest_for_cache_key_rotation(self):
		root = TOOLS_DIR.parent
		manifest = (root / "apps/khitmee_ops/pyproject.toml").read_text(encoding="utf-8")
		dev_match = re.search(r"(?ms)^dev = \[(.*?)^\]", manifest)
		self.assertIsNotNone(dev_match)
		uv_deps = re.findall(r'["\'](uv==[^"\']+)["\']', dev_match.group(1))
		self.assertEqual(len(uv_deps), 1)

		expected = {
			"ci.yml": (1, ["apps/khitmee_ops/pyproject.toml"]),
			"autoloop-gate.yml": (2, ["apps/khitmee_ops/pyproject.toml", "candidate-repo/apps/khitmee_ops/pyproject.toml"]),
		}
		for workflow, (job_count, dependency_paths) in expected.items():
			text = (root / ".github/workflows" / workflow).read_text(encoding="utf-8")
			bootstrap_pins = re.findall(r"python -m pip install (uv==[^\s]+)", text)
			self.assertEqual(bootstrap_pins, [uv_deps[0]] * job_count)
			self.assertEqual(text.count('UV_CACHE_DIR="$HOME/.cache/pip/uv"'), job_count)
			for dependency_path in dependency_paths:
				self.assertIn(f"cache-dependency-path: {dependency_path}", text)

	def test_benchmark_evidence_reports_exact_test_count_change(self):
		text = (TOOLS_DIR.parent / ".autoloop/experiments/devprod-verification-20261127-benchmarks.md").read_text(encoding="utf-8")

		self.assertIn("472 total; tools suite 149", text)
		self.assertIn("477 total; tools suite 154", text)
		self.assertIn("no baseline tests removed; 5 regression/guardrail tests added", text)


if __name__ == "__main__":
	unittest.main()
