"""Static safeguards for failures found during backend qualification."""

from __future__ import annotations

import ast
import fnmatch
import re

APP = "apps/khitmee_ops/khitmee_ops/"
FASTPATH = APP + "api/voucher_fastpath.py"
STOCK_LOCKS = APP + "api/stock_locks.py"
PERF_PARITY = "tools/perf_parity.py"
FAST_VERIFY = "tools/fast_verify.py"
PERF_BENCH = "tools/perf_bench_inner.py"
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPE = (*FUNCTION, ast.ClassDef, ast.Lambda)


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


def calls(node, *names):
    return (
        child for child in ast.walk(node)
        if isinstance(child, ast.Call) and dotted(child.func) in names
    )


def parents(tree):
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def enclosing(node, parent_map, types):
    while node in parent_map:
        node = parent_map[node]
        if isinstance(node, types):
            return node
    return None


def python(file_map, path):
    return ast.parse(file_map[path], filename=path)


def violation(path, node, message):
    return {"path": path, "line": getattr(node, "lineno", 1), "message": message}


# Registry for dispatcher factories in voucher_fastpath.py. The stock-ledger
# entry is the validate_* call in upstream update_qty_in_future_sle. The other
# entries memoize reads/lookups or a pure Frappe schema filter.
DISPATCHER_VALIDATORS = {
    "_make_future_sle_dispatcher": ("stock_ledger.validate_negative_qty_in_future_sle",),
    "_make_read_dispatcher": (),
    "_make_accounting_dims_dispatcher": (),
    "_make_pos_profile_dispatcher": (),
    "_make_get_or_make_bin_dispatcher": (),
    "_make_product_bundle_dispatcher": (),
    "_make_bundle_items_dispatcher": (),
    "_make_sre_reserved_qty_dispatcher": (),
    "_make_schema_filter_dispatcher": (),
}

READ_TARGETS = {
    "get_accounting_dimensions": {"_make_accounting_dims_dispatcher", "_make_read_dispatcher"},
    "get_checks_for_pl_and_bs_accounts": {"_make_read_dispatcher"},
    "get_or_make_bin": {"_make_get_or_make_bin_dispatcher"},
    "is_product_bundle": {"_make_product_bundle_dispatcher"},
    "get_product_bundle_items": {"_make_bundle_items_dispatcher"},
    "get_sre_reserved_qty_for_item_and_warehouse": {"_make_sre_reserved_qty_dispatcher"},
    "get_pos_profile": {"_make_pos_profile_dispatcher", "_make_read_dispatcher"},
}


def rule_fastpath_validators(file_map):
    if FASTPATH not in file_map:
        return []
    tree = python(file_map, FASTPATH)
    parent_map = parents(tree)
    factories = {node.name: node for node in tree.body if isinstance(node, FUNCTION)}
    issues = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [dotted(target) for target in node.targets]
        if any(name.endswith(".update_qty_in_future_sle") for name in names) and not (
            isinstance(node.value, ast.Call) and dotted(node.value.func) == "_make_future_sle_dispatcher"
        ):
            issues.append(violation(FASTPATH, node, "install update_qty_in_future_sle through its registered factory"))
        if "_READ_CACHE_TARGETS" in names and isinstance(node.value, (ast.Tuple, ast.List)):
            for entry in node.value.elts:
                if not isinstance(entry, (ast.Tuple, ast.List)) or len(entry.elts) != 4:
                    issues.append(violation(FASTPATH, entry, "declare the upstream target and its dispatcher factory"))
                    continue
                target, _, _, factory = entry.elts
                valid_factories = READ_TARGETS.get(target.value, set()) if isinstance(target, ast.Constant) else set()
                if dotted(factory) not in valid_factories:
                    issues.append(violation(FASTPATH, entry, "register the upstream target and its dispatcher validators"))
    for node in factories.values():
        for dispatch in (child for child in node.body if isinstance(child, FUNCTION)):
            marked = any(
                isinstance(target, ast.Attribute)
                and dotted(target) == f"{dispatch.name}._khitmee_dispatcher"
                and isinstance(target.ctx, ast.Store)
                for target in ast.walk(node)
            )
            if not marked and not node.name.startswith("_make_"):
                continue
            required = DISPATCHER_VALIDATORS.get(node.name)
            if required is None:
                issues.append(violation(FASTPATH, dispatch, "register the dispatcher and its upstream validators"))
                continue
            if required and not isinstance(dispatch.body[-1], (ast.Return, ast.Raise)):
                issues.append(violation(FASTPATH, dispatch, "dispatcher must return through a validated or upstream path"))
            for exit_node in ast.walk(dispatch):
                if not isinstance(exit_node, ast.Return) or enclosing(exit_node, parent_map, SCOPE) is not dispatch:
                    continue
                if isinstance(exit_node.value, ast.Call) and dotted(exit_node.value.func) == "original":
                    continue
                validated = set()
                current = exit_node
                while current is not dispatch:
                    container = parent_map[current]
                    for _, block in ast.iter_fields(container):
                        if not isinstance(block, list) or current not in block:
                            continue
                        for stmt in block[:block.index(current) + 1]:
                            if isinstance(stmt, (ast.Expr, ast.Assign, ast.Return)):
                                value = stmt.value
                                if isinstance(value, ast.Call):
                                    validated.add(dotted(value.func))
                    current = container
                missing = set(required) - validated
                if missing:
                    issues.append(violation(FASTPATH, exit_node, f"fast-path return skips {', '.join(sorted(missing))}"))
    return issues


_STOCK_TABLE = re.compile(r"\btab(?:Item|Bin)\b", re.I)
_LOCK_CLAUSE = re.compile(r"\b(?:FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE)\b", re.I)
_LOCK_HELPERS = {"lock_items", "lock_existing_bins"}
_ORDERED_LOCK_SQL = {
    "lock_items": re.compile(r"\bORDER\s+BY\s+name\s+FOR\s+UPDATE\b", re.I),
    "lock_existing_bins": re.compile(r"\bORDER\s+BY\s+item_code\s*,\s*warehouse\s*,\s*name\s+FOR\s+UPDATE\b", re.I),
}
_PLANNED_LOCK_SQL = {
    "tabitem": ("plan.items", re.compile(r"\bitem\.name\s*=\s*%s", re.I)),
    "tabbin": ("plan.pairs", re.compile(
        r"FORCE\s+INDEX\s*\(`unique_item_warehouse`\)\s+WHERE\s+item_code\s*=\s*%s"
        r"\s+AND\s+warehouse\s*=\s*%s\s+FOR\s+UPDATE\b", re.I,
    )),
}


def strings(node):
    return " ".join(
        child.value for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    )


def _sql_text(query, scope):
    parts = [strings(query)]
    references = {node.id for node in ast.walk(query) if isinstance(node, ast.Name)}
    if references:
        for assignment in ast.walk(scope):
            if isinstance(assignment, ast.Assign):
                targets = assignment.targets
            elif isinstance(assignment, ast.AugAssign):
                targets = [assignment.target]
            else:
                continue
            if references.intersection(dotted(target) for target in targets):
                parts.append(strings(assignment.value))
    return " ".join(parts)


def _reachable_functions(functions, entry, parent_map):
    reachable = set()
    pending = [entry]
    while pending:
        name = pending.pop()
        if name not in functions or name in reachable:
            continue
        reachable.add(name)
        function = functions[name]
        pending.extend(
            dotted(call.func) for call in calls(function, *functions)
            if enclosing(call, parent_map, SCOPE) is function
        )
    return reachable


def _planned_lock_issues(functions, reachable, queries, parent_map):
    acquire = functions.get("acquire_stock_plan")
    planner = functions.get("plan_stock_locks")
    entry = functions.get("_lock_stock_adr0003")
    issues = []
    ordered = sorted(queries, key=lambda query: query[1].lineno)
    if "acquire_stock_plan" not in reachable or [table for table, _, _ in ordered] != ["tabitem", "tabbin"]:
        issues.append(violation(STOCK_LOCKS, acquire, "acquire planned Items then Bins through lock_stock_coordinates"))
    for table, query, sql in ordered:
        iterable, point_lock = _PLANNED_LOCK_SQL[table]
        loop = enclosing(query, parent_map, (ast.For, ast.While))
        if not (
            isinstance(loop, ast.For) and loop in acquire.body and dotted(loop.iter) == iterable
            and point_lock.search(sql)
        ):
            issues.append(violation(STOCK_LOCKS, query, f"lock {table} by its exact key in {iterable} order"))
    # Both coordinate lists come from the planner's sorted, deduplicated
    # identities. The hook path plans and acquires once as a single expression.
    plans = list(calls(planner, "StockLockPlan")) if planner else []
    acquisitions = list(calls(entry, "acquire_stock_plan")) if entry else []
    if not plans or any(
        len(call.args) < 2 or any(
            not (isinstance(arg, ast.Tuple) and not arg.elts or list(calls(arg, "sorted")))
            for arg in call.args[:2]
        )
        for call in plans
    ):
        issues.append(violation(STOCK_LOCKS, planner, "construct stock plans with sorted Items and Bin coordinates"))
    if len(acquisitions) != 1 or not list(calls(acquisitions[0], "plan_stock_locks")):
        issues.append(violation(STOCK_LOCKS, entry, "use the stock planner before acquiring its locks"))
    return issues


def rule_stock_locks(file_map):
    issues = []
    for path in sorted(file_map):
        if not path.startswith("apps/khitmee_ops/") or not path.endswith(".py"):
            continue
        tree = python(file_map, path)
        parent_map = parents(tree)
        functions = {node.name: node for node in tree.body if isinstance(node, FUNCTION)} if path == STOCK_LOCKS else {}
        reachable = _reachable_functions(functions, "lock_stock_coordinates", parent_map)
        lock_queries = []
        planned_queries = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and path != STOCK_LOCKS:
                for imported in node.names:
                    if imported.name in _LOCK_HELPERS:
                        issues.append(violation(path, node, "use lock_stock_coordinates for stock row locks"))
            if not isinstance(node, ast.Call):
                continue
            name = dotted(node.func).rsplit(".", 1)[-1]
            scope = enclosing(node, parent_map, SCOPE)
            scope_name = getattr(scope, "name", "")
            if name in _LOCK_HELPERS and (path != STOCK_LOCKS or scope_name != "lock_stock_coordinates"):
                issues.append(violation(path, node, "use lock_stock_coordinates for stock row locks"))
                continue
            # Inspect SQL strings and f-strings at their call sites, including local SQL variables.
            query = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "query"), None)
            if name == "sql" and query is not None:
                statement = _sql_text(query, scope or tree)
                table = _STOCK_TABLE.search(statement)
                if table and _LOCK_CLAUSE.search(statement):
                    if scope_name == "acquire_stock_plan" and scope_name in reachable and scope is functions[scope_name]:
                        planned_queries.append((table[0].lower(), node, statement))
                    else:
                        lock_queries.append(scope_name)
                    order = _ORDERED_LOCK_SQL.get(scope_name)
                    if scope_name not in reachable or scope is not functions[scope_name] or (
                        scope_name != "acquire_stock_plan" and (order is None or not order.search(statement))
                    ):
                        issues.append(violation(path, node, "stock row SQL locks require the ordered stock_locks helpers"))
            locking = name == "for_update" or any(
                kw.arg == "for_update" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
                for kw in node.keywords
            )
            if locking and any(
                isinstance(child, ast.Constant) and isinstance(child.value, str)
                and child.value.lower() in {"item", "bin", "tabitem", "tabbin"}
                for child in ast.walk(node)
            ):
                issues.append(violation(path, node, "use the ordered stock_locks SQL for Item and Bin locks"))
        if path == STOCK_LOCKS:
            if "acquire_stock_plan" in functions:
                issues.extend(_planned_lock_issues(functions, reachable, planned_queries, parent_map))
            if not (_LOCK_HELPERS & functions.keys()) and "acquire_stock_plan" in functions:
                continue
            coordinator = functions.get("lock_stock_coordinates")
            sequence = list(calls(coordinator, *_LOCK_HELPERS)) if coordinator is not None else []
            if sorted(lock_queries) != sorted(_LOCK_HELPERS) or [
                dotted(call.func) for call in sequence
            ] != ["lock_items", "lock_existing_bins"]:
                issues.append(violation(path, coordinator, "acquire Items then Bins once through lock_stock_coordinates"))
            for call in sequence:
                if parent_map[call] not in coordinator.body:
                    issues.append(violation(path, call, "keep both lock phases together, outside conditional or repeated blocks"))
            for name, sorter in (("lock_items", "ordered_item_codes"), ("lock_existing_bins", "ordered_bin_pairs")):
                helper = functions.get(name)
                sorter_def = functions.get(sorter)
                if helper is None or not list(calls(helper, sorter)) or sorter_def is None or not list(calls(sorter_def, "sorted")):
                    issues.append(violation(path, helper, f"{name} must sort its lock inputs via {sorter}"))
    return issues


_PERMISSION_OPS = {"os.chmod", "os.chown", "shutil.chown"}


def has_rejecting_guard(node, markers):
    return any(
        isinstance(child, ast.If)
        and all(marker in ast.unparse(child.test) for marker in markers)
        and any(isinstance(stmt, ast.Raise) for stmt in child.body)
        for child in ast.walk(node)
    )


def approved_permission_scopes(tree):
    functions = {node.name: node for node in tree.body if isinstance(node, FUNCTION)}
    allowed = set()
    validator = functions.get("_validate_dedicated_host_dir")
    cache = functions.get("prepare_sidecar_oracle_cache_dir")
    if (
        validator and cache
        and has_rejecting_guard(validator, ("SHARED_SYSTEM_DIRS", "resolved == resolved.parent"))
        and has_rejecting_guard(validator, ("stat.S_ISVTX",))
        and has_rejecting_guard(validator, ("stat.S_IWOTH",))
        and any(
            isinstance(stmt, ast.Assign) and [dotted(t) for t in stmt.targets] == ["cache_path"]
            and isinstance(stmt.value, ast.Call) and dotted(stmt.value.func) == "_validate_dedicated_host_dir"
            and stmt.value.args and dotted(stmt.value.args[0]) == "host_cache"
            for stmt in cache.body[:2]
        )
    ):
        allowed.add(cache)
    manifest = functions.get("prepare_sidecar_oracle_manifest_dir")
    # The separate sidecar manifest parent must be outside the cache and has its
    # own shared/system, root and sticky checks before adjusting the parent.
    if (
        manifest
        and has_rejecting_guard(manifest, ("SHARED_SYSTEM_DIRS", "manifest_dir == manifest_dir.parent"))
        and has_rejecting_guard(manifest, ("stat.S_ISVTX",))
    ):
        allowed.add(manifest)
    return allowed


def rule_operator_chmod(file_map):
    issues = []
    for path in sorted(file_map):
        if not re.fullmatch(r"tools/[^/]+\.py", path):
            continue
        tree = python(file_map, path)
        parent_map = parents(tree)
        allowed = approved_permission_scopes(tree) if path == PERF_PARITY else set()
        for helper in allowed:
            target = "item" if helper.name == "prepare_sidecar_oracle_cache_dir" else "manifest_dir"
            for attempt in calls(helper, "chmod", "chown"):
                if not attempt.args or dotted(attempt.args[0]) != target:
                    issues.append(violation(path, attempt, "change permissions only on the vetted sidecar target"))
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update({name.asname: name.name for name in node.names if name.asname})
            elif isinstance(node, ast.ImportFrom):
                aliases.update({name.asname or name.name: f"{node.module}.{name.name}" for name in node.names})
        for node in ast.walk(tree):
            op = dotted(node)
            if isinstance(node, ast.Call) and dotted(node.func) == "getattr" and len(node.args) >= 2:
                module, attr = node.args[:2]
                if isinstance(attr, ast.Constant):
                    op = f"{dotted(module)}.{attr.value}"
            if not op:
                continue
            head, _, tail = op.partition(".")
            resolved = ".".join(filter(None, (aliases.get(head, head), tail)))
            if resolved not in _PERMISSION_OPS:
                continue
            scope = enclosing(node, parent_map, SCOPE)
            binding = enclosing(node, parent_map, ast.Assign)
            # Only the injection points in the vetted helpers may take a reference
            # to chmod/chown. A direct call, including one added to a helper, fails.
            if scope in allowed and binding and (
                isinstance(binding.value, ast.IfExp) and binding.value.orelse is node
                and [dotted(t) for t in binding.targets] == [resolved.rsplit(".", 1)[-1]]
            ):
                continue
            issues.append(violation(path, node, "route permission changes through the vetted sidecar directory helpers"))
    return issues


def rule_report_columns(file_map):
    if FAST_VERIFY not in file_map:
        return []
    tree = python(file_map, FAST_VERIFY)
    parent_map = parents(tree)
    functions = {node.name: node for node in tree.body if isinstance(node, FUNCTION)}
    issues = []
    resolver = functions.get("_column_fieldnames")
    if resolver is None or not (
        any(isinstance(arg, ast.Constant) and arg.value == "label" for call in calls(resolver, "col.get") for arg in call.args)
        and "__col_" in strings(resolver)
    ):
        issues.append(violation(FAST_VERIFY, resolver, "resolve absent fieldnames from labels or column positions"))
    for name, required in (
        ("_row_to_dict", "_column_fieldnames"),
        ("compare_financial_reports", "_row_to_dict"),
    ):
        function = functions.get(name)
        if function is None or not list(calls(function, required)):
            issues.append(violation(FAST_VERIFY, function, f"{name} must use {required}"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or dotted(node.func) != "zip":
            continue
        construction = parent_map[node]
        scope = enclosing(node, parent_map, SCOPE)
        makes_dict = (
            isinstance(construction, ast.Call) and dotted(construction.func) == "dict"
            or isinstance(construction, ast.comprehension) and isinstance(parent_map[construction], ast.DictComp)
        )
        if makes_dict and getattr(scope, "name", "") in {"_row_to_dict", "compare_financial_reports"}:
            issues.append(violation(FAST_VERIFY, node, "build positional row dicts with _row_to_dict, using label-or-position keys"))
    return issues


def rule_bench_results(file_map):
    if PERF_BENCH not in file_map:
        return []
    tree = python(file_map, PERF_BENCH)
    parent_map = parents(tree)
    execute_names = {"fast_gp_execute"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(".fast_gross_profit"):
            execute_names.update(name.asname or name.name for name in node.names if name.name == "execute")
        elif isinstance(node, ast.Import):
            execute_names.update(
                f"{name.asname or name.name}.execute" for name in node.names
                if name.name.endswith(".fast_gross_profit")
            )
    issues = []
    for execute in calls(tree, *execute_names):
        classified = parent_map[execute]
        # Typed form: `result = classify_gross_profit_result(execute(...))` followed, in the same
        # loop and before any `benchmarks[...]` store, by an `isinstance(result, UnavailableGrossProfit)`
        # guard that records an unavailable measurement and skips timing.
        if isinstance(classified, ast.Call) and dotted(classified.func) == "classify_gross_profit_result":
            typed_assignment = parent_map.get(classified)
            loop = enclosing(execute, parent_map, (ast.For, ast.While))
            target_name = (
                dotted(typed_assignment.targets[0])
                if isinstance(typed_assignment, ast.Assign) and len(typed_assignment.targets) == 1
                else None
            )
            guards = [
                guard for guard in (loop.body if loop else [])
                if isinstance(guard, ast.If)
                and target_name
                and ast.unparse(guard.test) == f"isinstance({target_name}, UnavailableGrossProfit)"
                and list(calls(guard, "unavailable_report_measurement"))
                and any(isinstance(stmt, (ast.Continue, ast.Return)) for stmt in guard.body)
                and guard.lineno > execute.lineno
            ]
            early_records = [
                node for node in ast.walk(loop or tree)
                if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
                and dotted(node.value) == "benchmarks" and node.lineno > execute.lineno
                and guards and node.lineno < guards[0].lineno
                and not any(guard in ast_nodes_above(node, parent_map) for guard in guards)
            ]
            if not guards or early_records:
                issues.append(violation(PERF_BENCH, execute, "skip successful timing when the classifier reports an unavailable result"))
            continue
        if not (isinstance(classified, ast.Call) and dotted(classified.func) == "split_query_report_result"):
            assignment = parent_map[execute]
            classified = None
            if isinstance(assignment, ast.Assign) and assignment.value is execute and len(assignment.targets) == 1:
                for _, block in ast.iter_fields(parent_map[assignment]):
                    if isinstance(block, list) and assignment in block and block.index(assignment) + 1 < len(block):
                        next_stmt = block[block.index(assignment) + 1]
                        if isinstance(next_stmt, ast.Assign):
                            matches = list(calls(next_stmt, "split_query_report_result"))
                            if matches and [dotted(arg) for arg in matches[0].args] == [dotted(assignment.targets[0])]:
                                classified = matches[0]
        split_assignment = parent_map.get(classified)
        target = split_assignment.targets[0] if isinstance(split_assignment, ast.Assign) else None
        if not isinstance(target, (ast.Tuple, ast.List)) or len(target.elts) != 3:
            issues.append(violation(PERF_BENCH, execute, "classify Fast Gross Profit execute results before recording timing"))
            continue
        reason = dotted(target.elts[2])
        loop = enclosing(execute, parent_map, (ast.For, ast.While))
        block = loop.body if loop else []
        bypasses = [
            guard for guard in block
            if isinstance(guard, ast.If)
            and ast.unparse(guard.test) == f"{reason} is not None"
            and list(calls(guard, "unavailable_report_measurement"))
            and any(isinstance(stmt, (ast.Continue, ast.Return)) for stmt in guard.body)
            and guard.lineno > split_assignment.lineno
        ]
        records = [
            node for node in ast.walk(loop or tree)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
            and dotted(node.value) == "benchmarks" and node.lineno > execute.lineno
            and not any(guard in ast_nodes_above(node, parent_map) for guard in bypasses)
        ]
        if not bypasses or any(node.lineno < bypasses[0].lineno for node in records):
            issues.append(violation(PERF_BENCH, execute, "skip successful timing when the classifier reports an unavailable result"))
    return issues


def ast_nodes_above(node, parent_map):
    while node in parent_map:
        node = parent_map[node]
        yield node


def attribute_entries(raw):
    for line in raw.decode().splitlines():
        tokens = line.split()
        if tokens and not line.lstrip().startswith("#"):
            yield tokens[0], tokens[1:]


def attribute_matches(path, pattern):
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    regex = "/".join(fnmatch.translate(part)[4:-3] for part in pattern.lstrip("/").split("/"))
    regex = regex.replace("/.*/", "/(?:.*/)?")
    return re.fullmatch(regex, path) is not None


def rule_wsl_lf(file_map):
    attributes = ".gitattributes"
    root_entries = list(attribute_entries(file_map.get(attributes, b"")))
    required = {"*.sh", "*.bash", "*.py", "*.js", "*.mjs", "*.ts"}
    missing = required - {pattern for pattern, attrs in root_entries if "text" in attrs and "eol=lf" in attrs}
    issues = [
        violation(attributes, None, f"declare {pattern} text eol=lf for WSL")
        for pattern in sorted(missing)
    ]
    attr_files = {path: list(attribute_entries(raw)) for path, raw in file_map.items() if path.endswith("/.gitattributes")}
    for path, raw in sorted(file_map.items()):
        if path.startswith(".autoloop/rules/fixtures/"):
            continue
        script_pattern = next((pattern for pattern in required if fnmatch.fnmatchcase(path, pattern)), None)
        if not script_pattern and not raw.startswith(b"#!"):
            continue
        entries = [(pattern, attrs, path, True) for pattern, attrs in root_entries]
        for attr_file, nested in sorted(attr_files.items(), key=lambda entry: entry[0].count("/")):
            prefix = attr_file.removesuffix(".gitattributes")
            if path.startswith(prefix):
                entries.extend((pattern, attrs, path.removeprefix(prefix), False) for pattern, attrs in nested)
        eol = text = None
        eol_override = False
        for pattern, attrs, relative, is_root in entries:
            if attribute_matches(relative, pattern):
                for attr in attrs:
                    if attr.startswith("eol=") or attr in {"-eol", "!eol"}:
                        eol = attr
                        eol_override = not (is_root and pattern == script_pattern)
                    if attr.startswith("text=") or attr in {"text", "-text", "!text", "binary"}:
                        text = attr
        # A missing root directive is one configuration error regardless of
        # how many scripts inherit it. Explicit overrides still affect paths.
        bad_eol = eol != "eol=lf" and (script_pattern not in missing or eol_override)
        if bad_eol or text in {"-text", "binary"}:
            issues.append(violation(path, None, "WSL script must have effective eol=lf in Git attributes"))
    return issues


RULES = [
    ("fastpath-must-keep-upstream-validation", rule_fastpath_validators),
    ("stock-row-locks-only-in-stock-locks", rule_stock_locks),
    ("no-chmod-on-operator-paths", rule_operator_chmod),
    ("report-columns-have-fieldname-or-key", rule_report_columns),
    ("bench-results-typed-gated", rule_bench_results),
    ("wsl-scripts-lf", rule_wsl_lf),
]
