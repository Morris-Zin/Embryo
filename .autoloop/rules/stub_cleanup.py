"""Ratchet for Frappe and ERPNext test stubs shared through Python imports."""

import ast
from collections import Counter
from hashlib import sha256
import json

TESTS = "apps/khitmee_ops/khitmee_ops/tests/"
MODULES = {"frappe", "erpnext"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (*FUNCTIONS, ast.ClassDef, ast.Lambda)

# Existing unscoped assignments, keyed by file, scope, attribute and canonical AST fingerprint.
# When removing a legacy assignment, reduce its ceiling or delete its entry.
LEGACY = {
    ('test_catalog_unit.py', 'TestCatalogEndpoint.setUp', 'catalog.frappe.session.user', '21cb706a0994618f'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.PermissionError', '00e204e7764d8dae'): 1,
    ('test_catalog_unit.py', 'module', 'frappe._', 'e4474466810eb104'): 1,
    ('test_catalog_unit.py', 'module', 'frappe._dict', '168e12c91f9ee7c1'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.db', '145e21e8d8725796'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.get_cached_value', '5d7a7e7576d8c247'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.get_value', 'c3039a9308bf01ad'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.has_permission', '0512dd766e046411'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.local', '88bd2da7aad2e366'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.session', '8cf37f13c41453f7'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.throw', '32a1c2b53114ad64'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.utils.cint', 'bcac4282224b4203'): 1,
    ('test_catalog_unit.py', 'module', 'frappe.whitelist', '6385be25bd7f1ea8'): 1,
    ('test_memory_hooks_unit.py', 'TestMaybeTrimHeap.setUp', 'memory.frappe.flags', 'dcc467657b93e6e6'): 1,
    ('test_memory_hooks_unit.py', 'TestMaybeTrimHeap.setUp', 'memory.frappe.form_dict', 'ed2569ea3110d46e'): 1,
    ('test_memory_hooks_unit.py', 'TestMaybeTrimHeap.setUp', 'memory.frappe.local', '62cf16e27dcbd7e7'): 1,
    ('test_memory_hooks_unit.py', 'module', 'frappe.flags', '9c1cf4cf26005aef'): 1,
    ('test_memory_hooks_unit.py', 'module', 'frappe.form_dict', 'e14eba9c938f2add'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.PermissionError', '56bf6c551fa6e1c0'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.QueryDeadlockError', '889a121ef25f6d02'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.ValidationError', 'ee27b55e5cdc610c'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.db', 'ef6e428419884128'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.get_all', 'ff6261f0dea14b38'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.get_roles', 'd3e786406e94e9d4'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.has_permission', '0512dd766e046411'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.permissions', '20b98ebfeca55b50'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.session', '7da0e27e37d9680e'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.set_user', 'f8e98e8ba031b9f6'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.utils.add_days', '2339c260533e4550'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.utils.cint', 'bcac4282224b4203'): 1,
    ('test_profit_summary_unit.py', 'module', 'frappe.utils.nowdate', '0476fc606b6081d8'): 1,
    ('test_profit_summary_unit.py', 'module', 'module.frappe', 'a95a48f2e065f782'): 1,
    ('test_profit_summary_unit.py', 'setUpModule', 'module.frappe', 'a95a48f2e065f782'): 1,
    ('test_search_unit.py', 'TestItemCatalogCache.setUp', 'search.frappe.cache', '612f5e2fb8e79852'): 1,
    ('test_search_unit.py', 'TestItemCatalogCache.setUp', 'search.frappe.local.site', 'f6cff5dcc68fcc73'): 1,
    ('test_search_unit.py', 'TestPass3SearchOptimizations.setUp', 'search.frappe.cache', '612f5e2fb8e79852'): 1,
    ('test_search_unit.py', 'TestPass3SearchOptimizations.setUp', 'search.frappe.local.site', 'f6cff5dcc68fcc73'): 1,
    ('test_search_unit.py', 'module', 'frappe.PermissionError', '00e204e7764d8dae'): 1,
    ('test_search_unit.py', 'module', 'frappe._', 'e4474466810eb104'): 1,
    ('test_search_unit.py', 'module', 'frappe._dict', '168e12c91f9ee7c1'): 1,
    ('test_search_unit.py', 'module', 'frappe.cache', '3dcafe3aa4ba0bc3'): 1,
    ('test_search_unit.py', 'module', 'frappe.db', '6cf033c9cc1884bd'): 1,
    ('test_search_unit.py', 'module', 'frappe.get_cached_value', 'cbe52b2e4f0c538c'): 1,
    ('test_search_unit.py', 'module', 'frappe.get_hooks', 'e011ce461a53c5ff'): 1,
    ('test_search_unit.py', 'module', 'frappe.get_list', '79624d2bc1bab944'): 1,
    ('test_search_unit.py', 'module', 'frappe.has_permission', '0512dd766e046411'): 1,
    ('test_search_unit.py', 'module', 'frappe.local', '88bd2da7aad2e366'): 1,
    ('test_search_unit.py', 'module', 'frappe.log_error', '79b3221647614333'): 1,
    ('test_search_unit.py', 'module', 'frappe.session', '8cf37f13c41453f7'): 1,
    ('test_search_unit.py', 'module', 'frappe.throw', '32a1c2b53114ad64'): 1,
    ('test_search_unit.py', 'module', 'frappe.utils.cint', 'bcac4282224b4203'): 1,
    ('test_search_unit.py', 'module', 'frappe.whitelist', '6385be25bd7f1ea8'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.PermissionError', '00e204e7764d8dae'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.QueryDeadlockError', '889a121ef25f6d02'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.ValidationError', 'ee27b55e5cdc610c'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe._', 'e4474466810eb104'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.cache', '79e79ab07477f979'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.db', '8fee2760a2d11859'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.get_cached_value', 'a8460384dd6e3055'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.get_doc', '34fce2dd01445dc4'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.has_permission', '62f2f840bcab4b60'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.log_error', '79b3221647614333'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.parse_json', '8882067c648db374'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.session', '8cf37f13c41453f7'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.throw', '32a1c2b53114ad64'): 1,
    ('test_vouchers_unit.py', 'module', 'frappe.whitelist', '6385be25bd7f1ea8'): 1,
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


def _canonical_ast(value):
    """Encode semantic AST fields without Python-version-specific dump formatting."""
    if isinstance(value, ast.AST):
        fields = {
            field: _canonical_ast(child)
            for field, child in ast.iter_fields(value)
            if child is not None and child != []
        }
        return [type(value).__name__, fields]
    if isinstance(value, list):
        return [_canonical_ast(item) for item in value]
    if isinstance(value, (bytes, complex)) or value is Ellipsis:
        return {"type": type(value).__name__, "literal": repr(value)}
    return value


def _assignment_fingerprint(node):
    data = json.dumps(_canonical_ast(node), sort_keys=True, separators=(",", ":"))
    return sha256(data.encode("ascii")).hexdigest()[:16]


def find_unrestored(file_map):
    for path, raw in sorted(file_map.items()):
        if not path.startswith(TESTS) or not path.endswith(".py"):
            continue
        tree = ast.parse(raw.replace(b"\r\n", b"\n"), filename=path)
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
                    yield (path.removeprefix(TESTS), scope, ref), node.lineno, _assignment_fingerprint(node)


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
