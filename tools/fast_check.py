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
WSL_DOCKER_DISTRO = "KhitmeeERPNext"

# Financial/seeder suites keep their original sequential discovery: several
# legacy modules share import-time Frappe test doubles. Only the independent
# tools suite uses test-level process parallelism because it dominates verification time.
PYTHON_TEST_SERIAL_SUITES = (
	"apps/khitmee_ops/khitmee_ops/tests",
	"perf-suite/seeder",
	"perf-suite/k6",
	"perf-suite/offload",
)
TOOLS_TEST_PARALLEL_WORKERS = 12

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

VOUCHER_FASTPATH_PATH = "apps/khitmee_ops/khitmee_ops/api/voucher_fastpath.py"
FUTURE_SLE_PATCH_JUSTIFICATION = (
	"thread-local dispatcher delegates to the captured ERPNext original unless deterministic locks "
	"prove the voucher has no future SLE rows; installation is idempotent and lock-guarded"
)
READ_CACHE_PATCH_JUSTIFICATION = (
	"thread-local single-transaction read-only metadata cache; outside the voucher transaction "
	"the wrapper delegates directly to the captured ERPNext original"
)
LOCKED_BIN_PATCH_JUSTIFICATION = (
	"inside the voucher transaction returns only Bin names already found by the ordered "
	"stock_locks acquisition; any miss or call outside the transaction delegates to the "
	"captured ERPNext original, which still creates missing Bins"
)
SCHEMA_FILTER_PATCH_JUSTIFICATION = (
	"caches ordered DocField filter results for the posting transaction only and delegates "
	"all filtering to the captured unmodified Frappe Document._filter"
)
INVOICE_SERIES_PATH = "apps/khitmee_ops/khitmee_ops/api/invoice_series.py"
INVOICE_SERIES_PATCH_JUSTIFICATION = (
	"default-OFF experiment khitmee_independent_invoice_series (ADR 0002/0003): stock naming "
	"still selects the rule and parses prefix/width; only the terminal counter commit and "
	"audited-number reuse change, and only when the owner enables the site flag"
)
MONKEY_PATCH_ALLOWLIST = {
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.stock_ledger.update_qty_in_future_sle": FUTURE_SLE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.accounts.doctype.accounting_dimension.accounting_dimension.get_accounting_dimensions": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.accounts.general_ledger.get_accounting_dimensions": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.controllers.accounts_controller.get_accounting_dimensions": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.accounts.doctype.sales_invoice.sales_invoice.get_accounting_dimensions": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.doctype.stock_entry.stock_entry.get_accounting_dimensions": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.controllers.stock_controller.get_accounting_dimensions": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.doctype.packed_item.packed_item.is_product_bundle": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.get_item_details.is_product_bundle": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.controllers.selling_controller.is_product_bundle": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.accounts.doctype.sales_invoice.sales_invoice.is_product_bundle": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_sre_reserved_qty_for_item_and_warehouse": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.utils.get_sre_reserved_qty_for_item_and_warehouse": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.stock_ledger.get_sre_reserved_qty_for_item_and_warehouse": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.doctype.stock_entry.stock_entry.get_sre_reserved_qty_for_item_and_warehouse": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.get_item_details.get_pos_profile": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.accounts.doctype.sales_invoice.sales_invoice.get_pos_profile": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.accounts.doctype.accounting_dimension.accounting_dimension.get_checks_for_pl_and_bs_accounts": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.accounts.doctype.gl_entry.gl_entry.get_checks_for_pl_and_bs_accounts": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.accounts.doctype.payment_ledger_entry.payment_ledger_entry.get_checks_for_pl_and_bs_accounts": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.accounts.general_ledger.get_checks_for_pl_and_bs_accounts": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.doctype.packed_item.packed_item.get_product_bundle_items": READ_CACHE_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.stock_ledger.get_or_make_bin": LOCKED_BIN_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":erpnext.stock.utils.get_or_make_bin": LOCKED_BIN_PATCH_JUSTIFICATION,
	VOUCHER_FASTPATH_PATH + ":document._filter": SCHEMA_FILTER_PATCH_JUSTIFICATION,
	INVOICE_SERIES_PATH + ":naming.NamingSeries.generate_next_name": INVOICE_SERIES_PATCH_JUSTIFICATION,
	INVOICE_SERIES_PATH + ":naming.revert_series_if_last": INVOICE_SERIES_PATCH_JUSTIFICATION,
	INVOICE_SERIES_PATH + ":delete_doc.revert_series_if_last": INVOICE_SERIES_PATCH_JUSTIFICATION,
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


def windows_path_to_wsl(path):
	text = str(path).replace("\\", "/")
	match = re.fullmatch(r"([A-Za-z]):(?:/(.*))?", text)
	if match is None:
		raise RuntimeError(f"cannot translate non-Windows path for WSL docker compose: {path}")
	suffix = match.group(2) or ""
	return f"/mnt/{match.group(1).lower()}" + (f"/{suffix}" if suffix else "")


def docker_compose_config_invocation(cwd, size, overlay=False, which=shutil.which):
	files = ["compose.base.yaml", f"profile-{size}.yaml"]
	if overlay:
		files.append(f"khitmee-ops-{size}.yaml")
	compose_args = ["compose", "--env-file", os.devnull, "-p", "khitmee-perf"]
	for filename in files:
		compose_args.extend(("-f", filename))
	compose_args.extend(("config", "--quiet"))
	if which("docker"):
		return ["docker", *compose_args], cwd
	if not (which("wsl.exe") or which("wsl")):
		raise RuntimeError(
			"docker is not on PATH and wsl.exe is unavailable; install Docker or enable "
			f"the {WSL_DOCKER_DISTRO} WSL distro for the compose gate"
		)
	linux_cwd = windows_path_to_wsl(cwd)
	wsl_args = ["compose", "--env-file", "/dev/null", "-p", "khitmee-perf"]
	for filename in files:
		wsl_args.extend(("-f", windows_path_to_wsl(Path(cwd) / filename)))
	wsl_args.extend(("config", "--quiet"))
	return [
		"wsl.exe", "-d", WSL_DOCKER_DISTRO, "--cd", linux_cwd, "--", "docker", *wsl_args,
	], cwd


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


_STATIC_UNSET = object()


def _flatten_static_strings(value):
	if isinstance(value, str):
		return {value}
	if isinstance(value, (tuple, list, set, frozenset)):
		out = set()
		for item in value:
			out.update(_flatten_static_strings(item))
		return out
	return set()


def _static_eval(node, values):
	if isinstance(node, ast.Constant):
		return node.value
	if isinstance(node, ast.Name):
		return values.get(node.id, _STATIC_UNSET)
	if isinstance(node, (ast.Tuple, ast.List)):
		return tuple(_static_eval(element, values) for element in node.elts)
	return _STATIC_UNSET


def _is_core_module_string(name):
	return str(name).split(".", 1)[0] in CORE_ROOTS


def _dynamic_core_modules(arg, values):
	raw = _static_eval(arg, values)
	strings = _flatten_static_strings(raw)
	if strings:
		core = frozenset(name for name in strings if _is_core_module_string(name))
		return core or None
	# Unresolved dynamic import arguments are fail-closed: they may name a core module.
	return frozenset()


def _call_chain(node):
	if isinstance(node, ast.Name):
		return [node.id]
	return attribute_chain(node)


def _is_dynamic_import_call(chain):
	return bool(chain) and chain[-1] == "import_module" and (len(chain) == 1 or chain[-2] == "importlib")


def _is_frappe_get_attr_call(chain):
	return bool(chain) and chain[-1] == "get_attr" and (len(chain) == 1 or chain[0] == "frappe")


def _is_sys_modules_lookup(node):
	if isinstance(node, ast.Subscript):
		chain = attribute_chain(node.value)
		return chain == ["sys", "modules"]
	return False


def _is_sys_modules_get_call(chain):
	return chain == ["sys", "modules", "get"]


def _collect_module_constants(tree):
	values = {}
	for node in tree.body:
		if not isinstance(node, ast.Assign):
			continue
		value = _static_eval(node.value, values)
		if value is _STATIC_UNSET:
			continue
		for target in node.targets:
			if isinstance(target, ast.Name):
				values[target.id] = value
	return values


def _dynamic_import_helpers(tree):
	helpers = set()
	for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
		params = {arg.arg for arg in function.args.args}
		for node in ast.walk(function):
			if not isinstance(node, ast.Return) or node.value is None:
				continue
			for param in params:
				found = False
				if isinstance(node.value, ast.Call):
					chain = _call_chain(node.value.func)
					found = (
						(_is_dynamic_import_call(chain) or _is_sys_modules_get_call(chain))
						and node.value.args
						and isinstance(node.value.args[0], ast.Name)
						and node.value.args[0].id == param
					)
				elif _is_sys_modules_lookup(node.value):
					found = isinstance(node.value.slice, ast.Name) and node.value.slice.id == param
				if found:
					helpers.add(function.name)
	return helpers


def _setattr_name(node, values):
	if len(node.args) < 2:
		return "<dynamic>"
	raw = _static_eval(node.args[1], values)
	names = sorted(_flatten_static_strings(raw))
	return names[0] if names else "<dynamic>"


def _mutation_root_and_attr(target, values, setattr_node=None):
	if isinstance(target, ast.Name):
		if setattr_node is None:
			return target.id, ""
		return target.id, _setattr_name(setattr_node, values)
	chain = attribute_chain(target)
	if not chain:
		return None, None
	if setattr_node is not None:
		return chain[0], ".".join([*chain[1:], _setattr_name(setattr_node, values)])
	return chain[0], ".".join(chain[1:])


def _collect_function_param_mutations(tree):
	mutations = {}
	for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
		params = [arg.arg for arg in function.args.args]
		entries = []
		for node in ast.walk(function):
			candidates = []
			if isinstance(node, ast.Assign):
				candidates = [target for assignment_target in node.targets for target in _iter_assignment_targets(assignment_target)]
			elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
				candidates = list(_iter_assignment_targets(node.target))
			for target in candidates:
				if not isinstance(target, ast.Attribute):
					continue
				root, attr = _mutation_root_and_attr(target, {})
				if root in params and attr:
					entries.append((root, params.index(root), attr))
			if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr" and node.args:
				root, attr = _mutation_root_and_attr(node.args[0], {}, node)
				if root in params and attr:
					entries.append((root, params.index(root), attr))
		if entries:
			mutations[function.name] = entries
	return mutations


class _MonkeyPatchScopeScanner:
	def __init__(self, display_path, bindings, import_helpers, param_mutations, values, violations):
		self.display_path = display_path
		self.bindings = bindings
		self.import_helpers = import_helpers
		self.param_mutations = param_mutations
		self.values = dict(values)
		self.objects = {}
		self.violations = violations

	def add_direct(self, node, target_description):
		# Direct import-binding patches use the same justified allowlist as dynamic ones.
		if MONKEY_PATCH_ALLOWLIST.get(f"{self.display_path}:{target_description}"):
			return
		self.violations.append(GuardrailViolation(
			self.display_path,
			getattr(node, "lineno", 0),
			"anti-monkey-patch",
			f"mutates imported ERPNext/Frappe symbol {target_description}; keep core immutable",
		))

	def add_dynamic(self, node, modules, attr_path):
		attr_path = attr_path or "<dynamic>"
		for module in sorted(modules) if modules else ("<dynamic>",):
			target = module + "." + attr_path
			allowlist_key = f"{self.display_path}:{target}"
			if MONKEY_PATCH_ALLOWLIST.get(allowlist_key):
				continue
			self.violations.append(GuardrailViolation(
				self.display_path,
				getattr(node, "lineno", 0),
				"anti-monkey-patch",
				f"mutates dynamically imported ERPNext/Frappe symbol {target}; keep core immutable",
			))

	def dynamic_object(self, node):
		if isinstance(node, ast.Name):
			return self.objects.get(node.id)
		if _is_sys_modules_lookup(node):
			return _dynamic_core_modules(node.slice, self.values)
		if not isinstance(node, ast.Call) or not node.args:
			return None
		chain = _call_chain(node.func)
		if _is_dynamic_import_call(chain) or _is_frappe_get_attr_call(chain) or _is_sys_modules_get_call(chain):
			return _dynamic_core_modules(node.args[0], self.values)
		if isinstance(node.func, ast.Name) and node.func.id in self.import_helpers:
			return _dynamic_core_modules(node.args[0], self.values)
		return None

	def bind_target(self, target, value_node):
		value = _static_eval(value_node, self.values)
		obj = self.dynamic_object(value_node)

		def bind_one(inner, inner_value, inner_obj):
			if isinstance(inner, ast.Name):
				if inner_value is _STATIC_UNSET:
					self.values.pop(inner.id, None)
				else:
					self.values[inner.id] = inner_value
				if inner_obj is None:
					self.objects.pop(inner.id, None)
				else:
					self.objects[inner.id] = inner_obj
			elif isinstance(inner, (ast.Tuple, ast.List)):
				items = inner_value if isinstance(inner_value, tuple) else ()
				for index, element in enumerate(inner.elts):
					bind_one(element, items[index] if index < len(items) else _STATIC_UNSET, None)

		bind_one(target, value, obj)

	def handle_assignment_target(self, node, target):
		if not isinstance(target, ast.Attribute):
			return
		chain = attribute_chain(target)
		if not chain:
			return
		if chain[0] in self.bindings and not _is_allowed_assignment_state(chain, self.bindings):
			self.add_direct(node, ".".join(chain))
		obj = self.objects.get(chain[0])
		if obj is not None:
			self.add_dynamic(node, obj, ".".join(chain[1:]))

	def handle_setattr(self, node):
		target = node.args[0]
		chain = attribute_chain(target)
		if isinstance(target, ast.Name) and target.id in self.bindings:
			self.add_direct(node, target.id)
		elif chain and chain[0] in self.bindings and not _is_allowed_setattr_state(chain, self.bindings):
			self.add_direct(node, ".".join(chain))
		root, attr_path = _mutation_root_and_attr(target, self.values, node)
		obj = self.objects.get(root) if root else None
		if obj is not None:
			self.add_dynamic(node, obj, attr_path)

	def handle_helper_call(self, node):
		if not isinstance(node.func, ast.Name):
			return
		entries = self.param_mutations.get(node.func.id, [])
		if not entries:
			return
		keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
		for param_name, param_index, attr_path in entries:
			if param_index < len(node.args):
				arg = node.args[param_index]
			else:
				arg = keywords.get(param_name)
			if arg is None:
				continue
			obj = self.dynamic_object(arg)
			if obj is not None:
				self.add_dynamic(node, obj, attr_path)

	def visit_expr(self, node):
		if isinstance(node, ast.Call):
			if isinstance(node.func, ast.Name) and node.func.id == "setattr" and node.args:
				self.handle_setattr(node)
			self.handle_helper_call(node)
		for child in ast.iter_child_nodes(node):
			if isinstance(child, ast.expr):
				self.visit_expr(child)

	def visit_assignment(self, node, targets, value):
		self.visit_expr(value)
		for assignment_target in targets:
			for target in _iter_assignment_targets(assignment_target):
				self.handle_assignment_target(node, target)
				if isinstance(target, (ast.Name, ast.Tuple, ast.List)):
					self.bind_target(target, value)

	def visit_for(self, node):
		self.visit_expr(node.iter)
		iterable = _static_eval(node.iter, self.values)
		elements = iterable if isinstance(iterable, tuple) else (_STATIC_UNSET,)
		base_values = dict(self.values)
		base_objects = dict(self.objects)
		merged_objects = dict(base_objects)
		for element in elements:
			self.values = dict(base_values)
			self.objects = dict(base_objects)
			if element is not _STATIC_UNSET and isinstance(node.target, (ast.Name, ast.Tuple, ast.List)):
				self.bind_loop_target(node.target, element)
			self.visit_statements(node.body)
			for name, obj in self.objects.items():
				if name in base_objects and base_objects[name] != obj:
					merged_objects[name] = frozenset(base_objects[name] | obj) if base_objects[name] and obj else frozenset()
				else:
					merged_objects[name] = obj
		self.values = base_values
		self.objects = merged_objects
		self.visit_statements(node.orelse)

	def bind_loop_target(self, target, value):
		def bind_one(inner, inner_value):
			if isinstance(inner, ast.Name):
				self.values[inner.id] = inner_value
				self.objects.pop(inner.id, None)
			elif isinstance(inner, (ast.Tuple, ast.List)):
				items = inner_value if isinstance(inner_value, tuple) else ()
				for index, element in enumerate(inner.elts):
					bind_one(element, items[index] if index < len(items) else _STATIC_UNSET)
		bind_one(target, value)

	def visit_statements(self, nodes):
		for node in nodes:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				continue
			if isinstance(node, ast.Assign):
				self.visit_assignment(node, node.targets, node.value)
			elif isinstance(node, ast.AnnAssign):
				if node.value is not None:
					self.visit_assignment(node, [node.target], node.value)
			elif isinstance(node, ast.AugAssign):
				self.visit_assignment(node, [node.target], node.value)
			elif isinstance(node, ast.Expr):
				self.visit_expr(node.value)
			elif isinstance(node, ast.For):
				self.visit_for(node)
			elif isinstance(node, ast.If):
				self.visit_expr(node.test)
				self.visit_statements(node.body)
				self.visit_statements(node.orelse)
			elif isinstance(node, (ast.With, ast.AsyncWith)):
				for item in node.items:
					self.visit_expr(item.context_expr)
				self.visit_statements(node.body)
			elif isinstance(node, ast.While):
				self.visit_expr(node.test)
				self.visit_statements(node.body)
				self.visit_statements(node.orelse)
			elif isinstance(node, ast.Try):
				self.visit_statements(node.body)
				for handler in node.handlers:
					self.visit_statements(handler.body)
				self.visit_statements(node.orelse)
				self.visit_statements(node.finalbody)
			elif isinstance(node, ast.Return) and node.value is not None:
				self.visit_expr(node.value)


def scan_monkey_patch_file(path, display_path=None):
	display_path = display_path or _posix(path)
	tree = parse_python(path)
	bindings = core_import_bindings(tree)
	module_values = _collect_module_constants(tree)
	import_helpers = _dynamic_import_helpers(tree)
	param_mutations = _collect_function_param_mutations(tree)
	violations = []

	def scan_scope(nodes, values):
		scanner = _MonkeyPatchScopeScanner(
			display_path,
			bindings,
			import_helpers,
			param_mutations,
			values,
			violations,
		)
		scanner.visit_statements(nodes)

	scan_scope(tree.body, module_values)
	for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
		scan_scope(function.body, module_values)
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


def unittest_discovery_invocation(root, suite, os_name=os.name, which=shutil.which, find_spec=importlib.util.find_spec):
	suite_path = (Path(root) / suite).as_posix()
	native = [sys.executable, "-B", "-m", "unittest", "discover", "-s", suite_path, "-p", "test_*.py"]
	if suite != "perf-suite/offload" or os_name != "nt":
		return native, root
	try:
		fcntl_available = find_spec("fcntl") is not None
	except (ImportError, ValueError):
		fcntl_available = False
	if fcntl_available:
		return native, root
	if not (which("wsl.exe") or which("wsl")):
		raise RuntimeError(
			"perf-suite/offload requires fcntl on Windows; enable the "
			f"{WSL_DOCKER_DISTRO} WSL distro or run the gate on a POSIX host"
		)
	linux_root = windows_path_to_wsl(root)
	linux_suite = windows_path_to_wsl(Path(root) / suite)
	return [
		"wsl.exe", "-d", WSL_DOCKER_DISTRO, "--cd", linux_root, "--",
		"python3", "-B", "-m", "unittest", "discover", "-s", linux_suite, "-p", "test_*.py",
	], root


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
		"test",
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
				argv, cwd = unittest_discovery_invocation(root, suite)
				output = run(argv, cwd)
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
		names = sorted(set(filter(None, files)))

		def check_file(name):
			source = (root / name).read_bytes()
			# Match Git's LF repository representation on Windows checkouts.
			if name.startswith("tools/") and (b"\r" in source or source.startswith(b"\xef\xbb\xbf")):
				return name, b"requires LF and UTF-8 without BOM"
			result = subprocess.run(["bash", "-n"], input=source.replace(b"\r\n", b"\n"),
									cwd=root, capture_output=True)
			return name, result.stderr if result.returncode else b""

		workers = min(8, len(names)) or 1
		with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
			for name, error in pool.map(check_file, names):
				if error:
					raise RuntimeError(f"{name}: {error.decode(errors='replace')[:300]}")

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
		configs = [(size, overlay) for size in ("2gb", "4gb") for overlay in (False, True)]

		def check_config(config):
			size, overlay = config
			argv, cwd = docker_compose_config_invocation(perf / "docker", size, overlay=overlay)
			env = {"DB_PASSWORD": "config-only"}
			if argv[0] == "wsl.exe":
				wslenv_entries = [entry for entry in os.environ.get("WSLENV", "").split(":") if entry]
				if not any(entry.split("/", 1)[0] == "DB_PASSWORD" for entry in wslenv_entries):
					wslenv_entries.append("DB_PASSWORD")
				env["WSLENV"] = ":".join(wslenv_entries)
			run(argv, cwd, **env)

		with concurrent.futures.ThreadPoolExecutor(max_workers=len(configs)) as pool:
			for _ in pool.map(check_config, configs):
				pass

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
