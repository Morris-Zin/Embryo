"""Ratchet for Frappe and ERPNext test stubs shared through Python imports."""

import ast
from collections import Counter
from hashlib import sha256

TESTS = "apps/khitmee_ops/khitmee_ops/tests/"
MODULES = {"frappe", "erpnext"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (*FUNCTIONS, ast.ClassDef, ast.Lambda)

# Existing unscoped assignments, keyed by file, scope, attribute and AST fingerprint.
# When removing a legacy assignment, reduce its ceiling or delete its entry.
LEGACY = {
    ('test_catalog_unit.py', 'TestCatalogEndpoint.setUp', 'catalog.frappe.session.user', '1ddedc10a183d99a'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.PermissionError', '37712c5142d49a1b'): 1,
    ('test_catalog_unit.py', 'module', 'frappe._', '6f01ce52ee96b4dc'): 1,
    ('test_catalog_unit.py', 'module', 'frappe._dict', '48e6944f1f272a71'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.db', '9e88e4159b7b8018'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.get_cached_value', 'af6a1df4d3029f14'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.get_value', '249113224f7cc35f'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.has_permission', '46d6ca4398cfbdff'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.local', '58d0fa8b3e12e6a8'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.session', 'd7bbfaf630ccd965'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.throw', 'ec7950b5b384c144'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.utils.cint', '37410f474e9fff0d'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.whitelist', 'e9e454f513f2f348'): 1,
    ('test_memory_hooks_unit.py', 'TestMaybeTrimHeap.setUp', 'memory.frappe.flags', '21ab0a373a8ddba1'): 1,
    ('test_memory_hooks_unit.py', 'TestMaybeTrimHeap.setUp', 'memory.frappe.form_dict', 'e07c7f6e12a75a79'): 1,
    ('test_memory_hooks_unit.py', 'TestMaybeTrimHeap.setUp', 'memory.frappe.local', 'c4fc63bcb8674411'): 1,
    ('test_memory_hooks_unit.py', 'module', 'frappe.flags', '2ced795d7ab03e9c'): 1,
    ('test_memory_hooks_unit.py', 'module', 'frappe.form_dict', '611ef6e2e393c2f3'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.PermissionError', 'da8e9a31261a0f67'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.QueryDeadlockError', 'c5ebccfef4cbc583'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.ValidationError', 'df5f0032b656f8dd'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.db', '9e5f6a30e5afcbec'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.get_all', '5cdc96b355352fc9'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.get_roles', '6849278ae9446827'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.has_permission', '46d6ca4398cfbdff'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.permissions', '9290796b1080b05b'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.session', 'dc1f6a98e04f03f6'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.set_user', '37adcaa2c46a74af'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.utils.add_days', 'd2b6ae5c6b6162b9'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.utils.cint', '37410f474e9fff0d'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.utils.nowdate', 'a5f8b11d8199aedc'): 1,
    ('test_profit_summary_unit.py', 'module', 'module.frappe', '10f5e2fbe5e44dd4'): 1,
    ('test_profit_summary_unit.py', 'setUpModule', 'module.frappe', '10f5e2fbe5e44dd4'): 1,
    ('test_search_unit.py', 'TestItemCatalogCache.setUp', 'search.frappe.cache', '49d2adddad4f993e'): 1,
    ('test_search_unit.py', 'TestItemCatalogCache.setUp', 'search.frappe.local.site', '2b51238b19df4f5e'): 1,
    ('test_search_unit.py', 'TestPass3SearchOptimizations.setUp', 'search.frappe.cache', '49d2adddad4f993e'): 1,
    ('test_search_unit.py', 'TestPass3SearchOptimizations.setUp', 'search.frappe.local.site', '2b51238b19df4f5e'): 1,
    ('test_search_unit.py', 'module', 'frappe.PermissionError', '37712c5142d49a1b'): 1,
    ('test_search_unit.py', 'module', 'frappe._', '6f01ce52ee96b4dc'): 1,
    ('test_search_unit.py', 'module', 'frappe._dict', '48e6944f1f272a71'): 1,
    ('test_search_unit.py', 'module', 'frappe.cache', '7bfab532d1c86a29'): 1,
    ('test_search_unit.py', 'module', 'frappe.db', '07eb6d1de391ae3d'): 1,
    ('test_search_unit.py', 'module', 'frappe.get_cached_value', '26ce7a14771d70a3'): 1,
    ('test_search_unit.py', 'module', 'frappe.get_hooks', '18a9096170e3ebcf'): 1,
    ('test_search_unit.py', 'module', 'frappe.get_list', '25507cad9fef9ff8'): 1,
    ('test_search_unit.py', 'module', 'frappe.has_permission', '46d6ca4398cfbdff'): 1,
    ('test_search_unit.py', 'module', 'frappe.local', '58d0fa8b3e12e6a8'): 1,
    ('test_search_unit.py', 'module', 'frappe.log_error', '1f3db0acb96ac9ee'): 1,
    ('test_search_unit.py', 'module', 'frappe.session', 'd7bbfaf630ccd965'): 1,
    ('test_search_unit.py', 'module', 'frappe.throw', 'ec7950b5b384c144'): 1,
    ('test_search_unit.py', 'module', 'frappe.utils.cint', '37410f474e9fff0d'): 1,
    ('test_search_unit.py', 'module', 'frappe.whitelist', 'e9e454f513f2f348'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.PermissionError', '37712c5142d49a1b'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.QueryDeadlockError', 'c5ebccfef4cbc583'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.ValidationError', 'df5f0032b656f8dd'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe._', '6f01ce52ee96b4dc'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.cache', '424a19a9b8b4454e'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.db', '05c2c6c470d0c511'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.get_cached_value', 'f6e72bd97ba9796f'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.get_doc', '4ed22e6394a64c77'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.has_permission', '3bc707e908ed9079'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.log_error', '1f3db0acb96ac9ee'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.parse_json', '5140fa887285e077'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.session', 'd7bbfaf630ccd965'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.throw', 'ec7950b5b384c144'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.whitelist', 'e9e454f513f2f348'): 1,
}


def name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{name(node.value)}.{node.attr}"
    return ""


def module_key(node):
    if isinstance(node, ast.Subscript) and name(node.value) == "sys.modules":
        node = node.slice
    elif isinstance(node, ast.Call) and name(node.func) in {"sys.modules.get", "_ensure_module"} and node.args:
        node = node.args[0]
    else:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.split(".")[0] in MODULES:
        return node.value
    return ""


def walk_body(body):
    """Visit statements owned by this scope, without walking nested definitions."""
    for node in body:
        if not isinstance(node, SCOPES):
            yield node
            yield from walk_body(ast.iter_child_nodes(node))


def assignments(nodes):
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value, node
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            yield node.target, node.value, node


def module_aliases(tree):
    aliases = {module: module for module in MODULES}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for entry in node.names:
                if entry.name.split(".")[0] in MODULES and entry.asname:
                    aliases[entry.asname] = entry.name
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in MODULES:
            for entry in node.names:
                aliases[entry.asname or entry.name] = f"{node.module}.{entry.name}"
    for target, value, _ in assignments(ast.walk(tree)):
        # Offline suites use both sys.modules[...] and get-or-create expressions.
        source = value.values[0] if isinstance(value, ast.BoolOp) else value
        origin = module_key(source) or aliases.get(name(source))
        if origin and isinstance(target, ast.Name):
            aliases[target.id] = origin
    return aliases


def attribute_ref(node, aliases):
    if not isinstance(node, ast.Attribute):
        return ""
    current = node
    attrs = []
    while isinstance(current, ast.Attribute):
        attrs.append(current.attr)
        current = current.value
    if module := module_key(current):
        return ".".join([module, *reversed(attrs)])
    parts = name(node).split(".")
    if parts[0] in aliases:
        return ".".join([aliases[parts[0]], *parts[1:]])
    if MODULES.intersection(parts):
        return name(node)
    return ""


def call_attribute(args, aliases):
    if len(args) < 2 or not isinstance(args[1], ast.Constant) or not isinstance(args[1].value, str):
        return ""
    obj = args[0]
    prefix = aliases.get(name(obj)) or module_key(obj) or attribute_ref(obj, aliases)
    return f"{prefix}.{args[1].value}" if prefix else ""


def mutations(nodes, aliases):
    for target, _, node in assignments(nodes):
        if ref := attribute_ref(target, aliases):
            yield ref, node
    for node in nodes:
        if isinstance(node, ast.Call) and name(node.func) == "setattr":
            if ref := call_attribute(node.args, aliases):
                yield ref, node


def saved_values(nodes, aliases):
    saved = {}
    for target, value, stmt in assignments(nodes):
        ref = attribute_ref(value, aliases)
        if isinstance(value, ast.Call) and name(value.func) == "getattr":
            ref = call_attribute(value.args, aliases)
        if ref and name(target):
            saved[name(target)] = (ref, stmt.lineno)
    return saved


def calls(nodes):
    return (node for node in nodes if isinstance(node, ast.Call))


def restores(nodes, aliases, saved, functions):
    result = []
    for cleanup in calls(nodes):
        if name(cleanup.func).split(".")[-1] not in {"addCleanup", "addModuleCleanup"} or not cleanup.args:
            continue
        callback, *args = cleanup.args
        if name(callback) == "setattr" and len(args) == 3:
            ref = call_attribute(args, aliases)
            if attribute_ref(args[2], aliases) == ref:
                saved[f"cleanup@{cleanup.lineno}"] = (ref, cleanup.lineno)
                result.append((ref, f"cleanup@{cleanup.lineno}"))
            else:
                result.append((ref, name(args[2])))
        elif name(callback) == "delattr":
            result.append((call_attribute(args, aliases), None))
        elif isinstance(callback, ast.Lambda) and isinstance(callback.body, ast.Call):
            action = callback.body
            if name(action.func) == "setattr" and len(action.args) == 3:
                result.append((call_attribute(action.args, aliases), name(action.args[2])))
        elif function := functions.get(name(callback).removeprefix("self.")):
            result.extend(teardown_restores(list(walk_body(function.body)), aliases))
    return [(ref, saved.get(value)) if value is not None else (ref, (ref, 0)) for ref, value in result]


def teardown_restores(nodes, aliases):
    result = [(attribute_ref(target, aliases), name(value)) for target, value, _ in assignments(nodes)]
    for node in calls(nodes):
        if name(node.func) == "setattr" and len(node.args) == 3:
            result.append((call_attribute(node.args, aliases), name(node.args[2])))
        if name(node.func) == "delattr":
            result.append((call_attribute(node.args, aliases), None))
    return result


def adds_missing_attribute(node, ref, parent_map, aliases):
    while node in parent_map:
        parent = parent_map[node]
        if isinstance(parent, ast.If) and node in parent.body and isinstance(parent.test, ast.UnaryOp):
            check = parent.test.operand
            if (
                isinstance(parent.test.op, ast.Not) and isinstance(check, ast.Call)
                and name(check.func) == "hasattr" and call_attribute(check.args, aliases) == ref
            ):
                return True
        node = parent
    return False


def function_defs(body):
    return {stmt.name: stmt for stmt in body if isinstance(stmt, FUNCTIONS)}


def candidates(body, aliases, functions, teardown=None, visited=None):
    visited = set() if visited is None else visited
    nodes = list(walk_body(body))
    saved = saved_values(nodes, aliases)
    restoration = restores(nodes, aliases, saved, functions)
    if teardown:
        for ref, value in teardown_restores(list(walk_body(teardown.body)), aliases):
            restoration.append((ref, saved.get(value) if value is not None else (ref, 0)))
    for ref, node in mutations(nodes, aliases):
        if not any(
            ref == restored and snapshot and snapshot[0] == ref and snapshot[1] < node.lineno
            for restored, snapshot in restoration
        ):
            yield ref, node
    for call in calls(nodes):
        helper = name(call.func).removeprefix("self.")
        if helper in functions and helper not in visited:
            visited.add(helper)
            yield from candidates(functions[helper].body, aliases, functions, visited=visited)


def find_unrestored(file_map):
    for path, raw in sorted(file_map.items()):
        if not path.startswith(TESTS) or not path.endswith(".py"):
            continue
        tree = ast.parse(raw, filename=path)
        parent_map = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        aliases = module_aliases(tree)
        functions = function_defs(tree.body)
        scopes = [("module", tree.body, functions, functions.get("tearDownModule"))]
        if "setUpModule" in functions:
            scopes.append(("setUpModule", functions["setUpModule"].body, functions, functions.get("tearDownModule")))
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            methods = function_defs(cls.body)
            for scope, teardown in (("setUp", "tearDown"), ("setUpClass", "tearDownClass")):
                if scope in methods:
                    scopes.append((f"{cls.name}.{scope}", methods[scope].body, {**functions, **methods}, methods.get(teardown)))
        for scope, body, helpers, teardown in scopes:
            for ref, node in candidates(body, aliases, helpers, teardown):
                if not adds_missing_attribute(node, ref, parent_map, aliases):
                    fingerprint = sha256(ast.dump(node).encode()).hexdigest()[:16]
                    yield (path.removeprefix(TESTS), scope, ref), node.lineno, fingerprint


def rule_test_stubs_restored(file_map):
    seen = Counter()
    issues = []
    for site, line, fingerprint in find_unrestored(file_map):
        key = (*site, fingerprint)
        seen[key] += 1
        if seen[key] > LEGACY.get(key, 0):
            path, scope, ref = site
            issues.append({
                "path": TESTS + path,
                "line": line,
                "message": f"{scope} replaces {ref} without restoring it; use unittest.mock.patch or register cleanup",
            })
    return issues


RULES = [("test-stubs-restored", rule_test_stubs_restored)]
