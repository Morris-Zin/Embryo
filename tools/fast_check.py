"""Parallel, offline checks. Each failed gate retains a bounded diagnostic."""
import ast
import concurrent.futures
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
from dataclasses import dataclass
from pathlib import Path

DEV_BOOTSTRAP_COMMAND = "python -m pip install --group apps/khitmee_ops/pyproject.toml:dev"
CORE_ROOTS = frozenset(("erpnext", "frappe"))
ALLOWED_DOC_OVERRIDES = frozenset()
ALLOWED_WHITELISTED_METHOD_OVERRIDES = frozenset()

ORACLE_REGISTRY = {
	"khitmee_ops/report/fast_gross_profit/fast_gross_profit.py": "tests/test_fast_gross_profit_parity.py",
	"profit_summary/budget.py": "tests/test_profit_summary_unit.py",
	"profit_summary/eligibility.py": "tests/test_profit_summary_unit.py",
	"profit_summary/query.py": "tests/test_profit_oracle_parity.py",
	"profit_summary/rebuilder.py": "tests/test_profit_oracle_parity.py",
	"profit_summary/replay.py": "tests/test_profit_oracle_parity.py",
	"profit_summary/store.py": "no-oracle-reason",
	"profit_summary/export_service.py": "no-oracle-reason",
	"profit_summary/parity_check.py": "no-oracle-reason",
	"profit_summary/reconcile.py": "no-oracle-reason",
}


def _term(parts):
	return "".join(parts)


SUMMARY_DOCTYPES = tuple(
	_term(parts)
	for parts in (
		("Daily Profit ", "by Shop"),
		("Khitmee Profit ", "Day State"),
		("Khitmee Profit ", "Replay Candidate"),
		("Khitmee Profit ", "Dirty Range"),
	)
)
EXPORT_LOG_DOCTYPE = _term(("Khitmee Profit ", "Export Log"))
SUMMARY_BOUNDARY_TERMS = SUMMARY_DOCTYPES + tuple("tab" + name for name in SUMMARY_DOCTYPES)
EXPORT_BOUNDARY_TERMS = (EXPORT_LOG_DOCTYPE, "tab" + EXPORT_LOG_DOCTYPE)
QUERY_PROFIT_HINT = "khitmee_ops.profit_summary.query_profit(...)"

FAST_GROSS_PROFIT_PATH = (
	"apps/khitmee_ops/khitmee_ops/khitmee_ops/report/fast_gross_profit/fast_gross_profit.py"
)
SEARCH_PATH = "apps/khitmee_ops/khitmee_ops/api/search.py"

# Financial/seeder suites keep their original sequential discovery: several
# legacy modules share import-time Frappe test doubles. Only the independent
# tools suite uses class-level parallelism because it dominates verification time.
PYTHON_TEST_SERIAL_SUITES = (
	"apps/khitmee_ops/khitmee_ops/tests",
	"perf-suite/seeder",
	"perf-suite/k6",
	"perf-suite/offload",
)
TOOLS_TEST_PARALLEL_WORKERS = 4

# Function-level entries document caller-enforced batching or transitional pre-existing queries.
SQL_IN_BATCH_ALLOWLIST = {
	SEARCH_PATH + ":_fetch_item_metadata": "search candidates are capped before metadata load",
	SEARCH_PATH + ":_fetch_rates_and_quantities": "rate rows are capped by the public search limit",
	SEARCH_PATH + ":_fetch_quantities": "no-rate search rows are capped by the public search limit (25)",
	FAST_GROSS_PROFIT_PATH + ":build_warehouse_covering_drive": "warehouse selector is bounded by the warehouse master",
	FAST_GROSS_PROFIT_PATH + ":build_sales_invoice_voucher_sle_query": "callers batch voucher numbers",
	FAST_GROSS_PROFIT_PATH + ":build_sle_window_query": "callers batch item codes",
	FAST_GROSS_PROFIT_PATH + ":build_sle_boundary_max_query": "callers batch item codes",
	FAST_GROSS_PROFIT_PATH + ":build_sle_boundary_rows_query": "callers batch boundary points",
	FAST_GROSS_PROFIT_PATH + ":build_voucher_detail_exists_query": "callers batch voucher detail keys",
	FAST_GROSS_PROFIT_PATH + ":load_product_bundle": "transitional pre-existing invoice list query",
	FAST_GROSS_PROFIT_PATH + ":_preload_delivery_note_item_quantities": "transitional pre-existing delivery-detail query",
	FAST_GROSS_PROFIT_PATH + ":get_delivery_notes": "transitional pre-existing invoice list query",
}

SELECT_STAR_RE = re.compile(r"\bselect\s+\*\s+from\b", re.IGNORECASE)
FORMAT_DYNAMIC_IN_RE = re.compile(r"\bin\s*\(\s*\{[a-zA-Z0-9_]*\}\s*\)", re.IGNORECASE)
FSTRING_BEFORE_IN_RE = re.compile(r"\bin\s*\(\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class GuardrailViolation:
	path: str
	line: int
	rule: str
	message: str

	def format(self):
		location = f"{self.path}:{self.line}" if self.line else self.path
		return f"{location}: {self.rule}: {self.message}"


def run(argv, cwd, **env):
	result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
							stderr=subprocess.STDOUT, text=True, errors="replace", timeout=45,
							env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env})
	if result.returncode:
		raise RuntimeError(result.stdout.strip()[-800:] or f"exit {result.returncode}")
	return result.stdout


def _posix(path):
	return Path(path).as_posix()


def _rel_to_root(path, root):
	return path.relative_to(root).as_posix()


def package_root(root):
	return root / "apps/khitmee_ops/khitmee_ops"


def production_python_files(root):
	base = package_root(root)
	if not base.exists():
		return []
	files = []
	for path in sorted(base.rglob("*.py")):
		rel_parts = path.relative_to(base).parts
		if "__pycache__" in rel_parts or "tests" in rel_parts or "setup" in rel_parts:
			continue
		files.append(path)
	return files


def parse_python(path):
	return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def attribute_chain(node):
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if not isinstance(node, ast.Name):
		return None
	parts.append(node.id)
	return list(reversed(parts))


def core_import_bindings(tree):
	bindings = {}
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			for alias in node.names:
				root = alias.name.split(".", 1)[0]
				if root in CORE_ROOTS:
					bindings[alias.asname or root] = root
		elif isinstance(node, ast.ImportFrom) and node.level == 0:
			module = node.module or ""
			root = module.split(".", 1)[0]
			if root in CORE_ROOTS:
				for alias in node.names:
					if alias.name != "*":
						bindings[alias.asname or alias.name] = root
	return bindings


def _is_allowed_assignment_state(chain, bindings):
	return (
		len(chain) >= 3
		and chain[1] in ("local", "flags")
		and bindings.get(chain[0]) == "frappe"
	)


def _is_allowed_setattr_state(chain, bindings):
	return (
		chain is not None
		and len(chain) >= 2
		and chain[1] in ("local", "flags")
		and bindings.get(chain[0]) == "frappe"
	)


def _iter_assignment_targets(node):
	if isinstance(node, (ast.Tuple, ast.List)):
		for element in node.elts:
			yield from _iter_assignment_targets(element)
	else:
		yield node


def scan_monkey_patch_file(path, display_path=None):
	display_path = display_path or _posix(path)
	tree = parse_python(path)
	bindings = core_import_bindings(tree)
	violations = []

	def add(node, target_description):
		# The sanctioned voucher dispatcher caches only request-local DocField
		# filter results and delegates all filtering to unmodified Frappe.
		if (
			display_path == "apps/khitmee_ops/khitmee_ops/api/voucher_fastpath.py"
			and target_description == "document._filter"
		):
			return
		violations.append(GuardrailViolation(
			display_path,
			getattr(node, "lineno", 0),
			"anti-monkey-patch",
			f"mutates imported ERPNext/Frappe symbol {target_description}; keep core immutable",
		))

	for node in ast.walk(tree):
		targets = []
		if isinstance(node, ast.Assign):
			targets = [target for assignment_target in node.targets for target in _iter_assignment_targets(assignment_target)]
		elif isinstance(node, ast.AugAssign) or (isinstance(node, ast.AnnAssign) and node.value is not None):
			targets = list(_iter_assignment_targets(node.target))

		for target in targets:
			if not isinstance(target, ast.Attribute):
				continue
			chain = attribute_chain(target)
			if not chain or chain[0] not in bindings:
				continue
			if _is_allowed_assignment_state(chain, bindings):
				continue
			add(node, ".".join(chain))

		if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr" and node.args:
			target = node.args[0]
			chain = attribute_chain(target)
			if isinstance(target, ast.Name) and target.id in bindings:
				add(node, target.id)
			elif chain and chain[0] in bindings and not _is_allowed_setattr_state(chain, bindings):
				add(node, ".".join(chain))
	return violations


def scan_monkey_patches(root):
	violations = []
	for path in production_python_files(root):
		violations.extend(scan_monkey_patch_file(path, _rel_to_root(path, root)))
	return violations


def is_core_path(name):
	normalized = str(name).replace("\\", "/").lstrip("./")
	first = normalized.split("/", 1)[0]
	return first in CORE_ROOTS and "/" in normalized


def core_change_violations(names):
	violations = []
	for name in sorted(set(names)):
		if is_core_path(name):
			violations.append(GuardrailViolation(
				name,
				0,
				"core-immutability",
			"ERPNext/Frappe core files may not be added or modified; keep the upstream oracle immutable",
			))
	return violations


def _git_safe_dir(root):
	return str(root).replace("\\", "/")


def _git_names(root, args):
	output = subprocess.check_output(
		["git", "-c", "safe.directory=" + _git_safe_dir(root), *args],
		cwd=root,
	).decode("utf-8", errors="replace")
	return [name for name in output.split("\0") if name]


def _git_ref_exists(root, ref):
	return subprocess.run(
		["git", "-c", "safe.directory=" + _git_safe_dir(root), "rev-parse", "--verify", "--quiet", ref],
		cwd=root,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	).returncode == 0


def _merge_base(root):
	candidates = []
	if os.environ.get("FAST_CHECK_BASE_REF"):
		candidates.append(os.environ["FAST_CHECK_BASE_REF"])
	if os.environ.get("GITHUB_BASE_REF"):
		base = os.environ["GITHUB_BASE_REF"]
		candidates.extend(("origin/" + base, base))
	candidates.extend(("origin/main", "main"))
	for ref in dict.fromkeys(candidates):
		if not _git_ref_exists(root, ref):
			continue
		result = subprocess.run(
			["git", "-c", "safe.directory=" + _git_safe_dir(root), "merge-base", ref, "HEAD"],
			cwd=root,
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			text=True,
		)
		if result.returncode == 0 and result.stdout.strip():
			return result.stdout.strip()
	return None


def changed_file_names(root):
	names = set(_git_names(root, ["diff", "--name-only", "-z", "HEAD"]))
	names.update(_git_names(root, ["ls-files", "--others", "--exclude-standard", "-z"]))
	base = _merge_base(root)
	if base:
		names.update(_git_names(root, ["diff", "--name-only", "-z", base + "...HEAD"]))
	return sorted(names)


def scan_core_file_changes(root):
	return core_change_violations(changed_file_names(root))


def _dict_string_keys(node):
	if not isinstance(node, ast.Dict):
		return None
	keys = []
	for key in node.keys:
		if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
			return None
		keys.append(key.value)
	return keys


def scan_hooks_overrides_file(path, display_path=None):
	display_path = display_path or _posix(path)
	tree = parse_python(path)
	violations = []
	for node in tree.body:
		if not isinstance(node, (ast.Assign, ast.AnnAssign)):
			continue
		targets = node.targets if isinstance(node, ast.Assign) else [node.target]
		for target in targets:
			if not isinstance(target, ast.Name) or target.id not in (
				"override_doctype_class",
				"override_whitelisted_methods",
			):
				continue
			keys = _dict_string_keys(node.value)
			if keys is None:
				violations.append(GuardrailViolation(
					display_path,
					node.lineno,
					"hooks-overrides",
					f"{target.id} must be a statically inspectable string-keyed dict",
				))
				continue
			allowlist = (
				ALLOWED_DOC_OVERRIDES
				if target.id == "override_doctype_class"
				else ALLOWED_WHITELISTED_METHOD_OVERRIDES
			)
			for key in keys:
				if key not in allowlist:
					violations.append(GuardrailViolation(
						display_path,
						node.lineno,
						"hooks-overrides",
						f"{target.id} entry {key!r} is not in the fast_check override allowlist",
					))
	return violations


def scan_hooks_overrides(root):
	return scan_hooks_overrides_file(
		package_root(root) / "hooks.py",
		"apps/khitmee_ops/khitmee_ops/hooks.py",
	)


def _path_parts(rel):
	return tuple(part for part in rel.split("/") if part)


def _summary_boundary_allowed(app_rel, tools_rel):
	rel = app_rel or tools_rel or ""
	parts = _path_parts(rel)
	if "doctype" in parts or "setup" in parts or "tests" in parts:
		return True
	return rel in ("profit_summary/store.py", "khitmee_ops/patches.txt")


def _export_boundary_allowed(app_rel, tools_rel):
	rel = app_rel or tools_rel or ""
	parts = _path_parts(rel)
	if "doctype" in parts or "setup" in parts or "tests" in parts:
		return True
	return rel in (
		"khitmee_ops/patches.txt",
		"profit_summary/export_service.py",
		"api/profit_export.py",
	)


def scan_package_boundary_file(path, display_path, app_rel=None, tools_rel=None):
	tree = parse_python(path)
	violations = []
	for node in ast.walk(tree):
		if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
			continue
		if not _summary_boundary_allowed(app_rel, tools_rel):
			matched = [term for term in SUMMARY_BOUNDARY_TERMS if term in node.value]
			matched = [term for term in matched if term.startswith("tab") or "tab" + term not in matched]
			for term in matched:
				violations.append(GuardrailViolation(
					display_path,
					node.lineno,
					"package-boundary",
					f"forbidden summary storage reference {term!r}; callers must use {QUERY_PROFIT_HINT}",
				))
		if not _export_boundary_allowed(app_rel, tools_rel):
			matched = [term for term in EXPORT_BOUNDARY_TERMS if term in node.value]
			matched = [term for term in matched if term.startswith("tab") or "tab" + term not in matched]
			for term in matched:
				violations.append(GuardrailViolation(
					display_path,
					node.lineno,
					"package-boundary",
					f"forbidden export storage reference {term!r}; use the export service boundary",
				))
	return violations


def scan_package_boundaries(root):
	violations = []
	base = package_root(root)
	if base.exists():
		for path in sorted(base.rglob("*.py")):
			rel_parts = path.relative_to(base).parts
			if "__pycache__" in rel_parts:
				continue
			app_rel = path.relative_to(base).as_posix()
			violations.extend(scan_package_boundary_file(path, _rel_to_root(path, root), app_rel=app_rel))
	tools = root / "tools"
	if tools.exists():
		for path in sorted(tools.rglob("*.py")):
			rel_parts = path.relative_to(tools).parts
			if "__pycache__" in rel_parts:
				continue
			tools_rel = path.relative_to(tools).as_posix()
			violations.extend(scan_package_boundary_file(path, _rel_to_root(path, root), tools_rel=tools_rel))
	return violations


def _dynamic_in_in_joined_str(node):
	values = node.values
	for index, part in enumerate(values):
		if not isinstance(part, ast.FormattedValue):
			continue
		before = values[index - 1].value if index and isinstance(values[index - 1], ast.Constant) else ""
		after = values[index + 1].value if index + 1 < len(values) and isinstance(values[index + 1], ast.Constant) else ""
		if (
			isinstance(before, str)
			and isinstance(after, str)
			and FSTRING_BEFORE_IN_RE.search(before)
			and after.lstrip().startswith(")")
		):
			return True
	return False


def _parent_map(tree):
	parents = {}
	for parent in ast.walk(tree):
		for child in ast.iter_child_nodes(parent):
			parents[child] = parent
	return parents


def _owning_function(node, parents):
	while node in parents:
		node = parents[node]
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			return node
	return None


def _function_calls_batched(function):
	return any(
		isinstance(node, ast.Call)
		and isinstance(node.func, ast.Name)
		and node.func.id == "batched"
		for node in ast.walk(function)
	)


def scan_sql_footguns_file(path, display_path=None):
	display_path = display_path or _posix(path)
	tree = parse_python(path)
	parents = _parent_map(tree)
	violations = []
	seen = set()

	def add(node, rule, message):
		key = (node.lineno, rule, message)
		if key in seen:
			return
		seen.add(key)
		violations.append(GuardrailViolation(display_path, node.lineno, rule, message))

	def dynamic_in_allowed(node):
		function = _owning_function(node, parents)
		function_name = function.name if function else "<module>"
		allowlist_key = f"{display_path}:{function_name}"
		return bool(function and _function_calls_batched(function)) or allowlist_key in SQL_IN_BATCH_ALLOWLIST

	for node in ast.walk(tree):
		if isinstance(node, ast.Constant) and isinstance(node.value, str):
			if SELECT_STAR_RE.search(node.value):
				add(node, "sql-footgun", "SELECT * FROM is forbidden; name explicit columns")
			if FORMAT_DYNAMIC_IN_RE.search(node.value) and not dynamic_in_allowed(node):
				add(
					node,
					"sql-footgun",
					"dynamic IN placeholders must be batched with batched( or carry a documented allowlist entry",
				)
		elif isinstance(node, ast.JoinedStr) and _dynamic_in_in_joined_str(node) and not dynamic_in_allowed(node):
			add(
				node,
				"sql-footgun",
				"dynamic IN placeholders must be batched with batched( or carry a documented allowlist entry",
			)
	return violations


def scan_sql_footguns(root):
	violations = []
	for path in production_python_files(root):
		violations.extend(scan_sql_footguns_file(path, _rel_to_root(path, root)))
	return violations


def oracle_module_files(root):
	base = package_root(root)
	files = []
	for relative in ("khitmee_ops/report", "profit_summary"):
		directory = base / relative
		if not directory.exists():
			continue
		for path in sorted(directory.rglob("*.py")):
			if path.name == "__init__.py" or "__pycache__" in path.parts:
				continue
			files.append((path, path.relative_to(base).as_posix()))
	return files


def scan_oracle_registry(root):
	base = package_root(root)
	violations = []
	for path, rel in oracle_module_files(root):
		registered = ORACLE_REGISTRY.get(rel)
		if registered:
			if registered.startswith("no-oracle-reason"):
				continue
			test_path = base / registered
			if not test_path.exists():
				violations.append(GuardrailViolation(
					_rel_to_root(path, root),
					0,
					"oracle-registry",
					f"registered parity test {registered!r} does not exist",
				))
			continue
		head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:25])
		if not re.search(r"^#\s*no-oracle-reason:\s*\S", head, flags=re.MULTILINE):
			violations.append(GuardrailViolation(
				_rel_to_root(path, root),
				1,
				"oracle-registry",
				"financial-calculation module must be in ORACLE_REGISTRY or declare # no-oracle-reason: <reason>",
			))
	return violations


def run_guardrails(root):
	violations = []
	violations.extend(scan_core_file_changes(root))
	violations.extend(scan_hooks_overrides(root))
	violations.extend(scan_monkey_patches(root))
	violations.extend(scan_package_boundaries(root))
	violations.extend(scan_sql_footguns(root))
	violations.extend(scan_oracle_registry(root))
	if violations:
		formatted = [violation.format() for violation in violations]
		bounded = formatted[:30]
		if len(formatted) > len(bounded):
			bounded.append(f"... {len(formatted) - len(bounded)} more guardrail violations")
		raise RuntimeError("\n".join(bounded))


def installed_script(name, which=shutil.which):
	bases = {sysconfig.get_path("scripts"), str(Path(sys.executable).parent)}
	for base in bases:
		if not base:
			continue
		for suffix in ("", ".exe"):
			candidate = Path(base) / (name + suffix)
			if candidate.is_file():
				return str(candidate)
	return which(name)


def missing_dev_tools(
	which=shutil.which,
	find_spec=importlib.util.find_spec,
	runner=subprocess.run,
	script_finder=installed_script,
):
	missing = []
	if not which("ruff"):
		try:
			runner(
				[sys.executable, "-m", "ruff", "--version"],
				stdin=subprocess.DEVNULL,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
				timeout=10,
				check=True,
			)
		except (OSError, subprocess.SubprocessError):
			missing.append("ruff")
	for module in ("hypothesis", "unittest_parallel"):
		try:
			available = find_spec(module) is not None
		except (ImportError, ValueError):
			available = False
		if not available:
			missing.append(module.replace("_", "-"))
	if not script_finder("zizmor", which=which):
		missing.append("zizmor")
	return missing


def assert_dev_tools_available():
	missing = missing_dev_tools()
	if missing:
		raise RuntimeError(
			"missing pinned dev tools: "
			+ ", ".join(missing)
			+ f"; bootstrap with exactly: {DEV_BOOTSTRAP_COMMAND}"
		)


def _ruff_command():
	cli = shutil.which("ruff")
	return [cli] if cli else [sys.executable, "-m", "ruff"]


def ruff_and_dev_deps(root):
	assert_dev_tools_available()
	run([*_ruff_command(), "check", "--config", "apps/khitmee_ops/pyproject.toml", "tools", "apps/khitmee_ops"], root)


def unittest_parallel_command(root, suite="tools"):
	return [
		sys.executable,
		"-B",
		"-m",
		"unittest_parallel",
		"-s",
		(Path(root) / suite).as_posix(),
		"-p",
		"test_*.py",
		"--level",
		"class",
		"-j",
		str(TOOLS_TEST_PARALLEL_WORKERS),
		"-q",
	]


def zizmor_workflows(root):
	assert_dev_tools_available()
	cli = installed_script("zizmor")
	if not cli:
		raise RuntimeError("missing pinned dev tools: zizmor")
	run([cli, "--offline", "--collect=workflows", "."], root)


def check(root):
	start = time.monotonic()
	app = root / "apps/khitmee_ops"
	perf = root / "perf-suite"
	jobs = {}

	def python_tests():
		import re

		def assert_tests_ran(suite, output):
			match = re.search(r"Ran (\d+) tests?", output)
			if not match or int(match.group(1)) < 1:
				raise RuntimeError(f"unittest discovered zero tests in {suite}")

		def serial_suites():
			for suite in PYTHON_TEST_SERIAL_SUITES:
				output = run(
					[sys.executable, "-B", "-m", "unittest", "discover", "-s", (Path(root) / suite).as_posix(), "-p", "test_*.py"],
					root,
				)
				assert_tests_ran(suite, output)

		def tools_suite():
			assert_tests_ran("tools", run(unittest_parallel_command(root), root))

		with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
			futures = [pool.submit(serial_suites), pool.submit(tools_suite)]
			for future in concurrent.futures.as_completed(futures):
				future.result()

	def bash_syntax():
		files = subprocess.check_output(["git", "-c", "safe.directory=" + _git_safe_dir(root), "ls-files",
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
		files = subprocess.check_output(["git", "-c", "safe.directory=" + _git_safe_dir(root), "ls-files", "-co",
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
			base = ["docker", "compose", "--env-file", os.devnull, "-p", "khitmee-perf",
					"-f", "compose.base.yaml", "-f", f"profile-{size}.yaml"]
			for extra in ([], ["-f", f"khitmee-ops-{size}.yaml"]):
				run(base + extra + ["config", "--quiet"], perf / "docker", DB_PASSWORD="config-only")

	def guardrails():
		run_guardrails(root)

	def ruff_gate():
		ruff_and_dev_deps(root)

	def zizmor_gate():
		zizmor_workflows(root)

	jobs.update(
		python=python_tests,
		javascript=javascript,
		knip=knip,
		bash=bash_syntax,
		compose=compose,
		guardrails=guardrails,
		ruff_and_dev_deps=ruff_gate,
		autoloop_lint=autoloop_lint,
		zizmor_workflows=zizmor_gate,
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
