"""Calibrated full-tree static rules for Embryo (Issue #10 V3 section 4).

Pure Python 3.11+ stdlib (`ast`, `tomllib`, `re`, `bytes`). Zero candidate code execution.
Every rule receives the full target file map `dict[posix_path, bytes]` and returns
`list[dict]` with keys `rule_id`, `path`, `line`, `message`.
"""

from __future__ import annotations

import ast
import re
import tomllib
from typing import Any

FileMap = dict[str, bytes]
Violation = dict[str, Any]

_PRELOAD_HOOK_NAMES = {"on_starting", "when_ready", "pre_fork", "post_fork", "preload", "preload_app"}
_WSGI_TARGET_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+):([A-Za-z_][A-Za-z0-9_]*)\b")
_OVERRIDE_RATE_RE = re.compile(r"\boverride_rate\b")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_DISABLE_MODEL_INVOCATION_RE = re.compile(r"^disable-model-invocation:\s*true\s*$", re.MULTILINE)
_WEBVIEW_CRATES = {"tauri", "wry", "webkit2gtk", "webview2-com"}

_LEDGER_ZERO_SCOPES: dict[str, set[str]] = {
    "tools/perf_eval_ledger.py": {"parse_float", "parse_size_mib", "observed_ms", "empty_metrics"},
    "tools/perf_parity.py": {"as_number", "scalar", "normalize_for_compare"},
    "perf-suite/offload/offload_spec.py": {"normalize_number"},
}


def _v(rule_id: str, path: str, line: int, message: str) -> Violation:
    return {"rule_id": rule_id, "path": path, "line": int(line), "message": message}


def _parse_python(raw: bytes) -> ast.AST | None:
    try:
        return ast.parse(raw.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return None


def _dotted_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _resolve_repo_local_module_paths(module_dotted: str, file_map: FileMap) -> list[str]:
    """Map a Python dotted module name to repo-local `.py` paths if present in `file_map`."""
    rel = module_dotted.replace(".", "/")
    candidates = [
        f"{rel}.py",
        f"{rel}/__init__.py",
    ]
    top = module_dotted.split(".", 1)[0]
    candidates.extend([
        f"apps/{top}/{rel}.py",
        f"apps/{top}/{rel}/__init__.py",
    ])
    return [p for p in candidates if p in file_map]


def rule_no_frappe_init_in_preload(file_map: FileMap) -> list[Violation]:
    """Rule 1: Scan `*preload*.py` and repo-local WSGI modules referenced in `perf-suite/docker/*.yaml`."""
    rule_id = "no-frappe-init-in-preload"
    targets: set[str] = set()

    for path in file_map:
        basename = path.rsplit("/", 1)[-1].lower()
        if basename.endswith(".py") and "preload" in basename:
            targets.add(path)

    for path, raw in file_map.items():
        if not (path.startswith("perf-suite/docker/") and (path.endswith(".yaml") or path.endswith(".yml"))):
            continue
        text = raw.decode("utf-8", errors="replace")
        for match in _WSGI_TARGET_RE.finditer(text):
            module_name, attr_name = match.group(1), match.group(2)
            # Explicitly preserve external upstream Frappe WSGI entrypoint in compose.base.yaml.
            if f"{module_name}:{attr_name}" == "frappe.app:application":
                continue
            for local_path in _resolve_repo_local_module_paths(module_name, file_map):
                targets.add(local_path)

    violations: list[Violation] = []
    for path in sorted(targets):
        tree = _parse_python(file_map[path])
        if tree is None:
            continue

        # Check top-level statements and preload hook functions.
        def _scan_nodes(nodes: list[ast.stmt], context: str) -> None:
            for stmt in nodes:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if stmt.name in _PRELOAD_HOOK_NAMES:
                        _scan_nodes(stmt.body, f"preload hook {stmt.name}()")
                    continue
                if isinstance(stmt, ast.ClassDef):
                    continue
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Call):
                        name = _dotted_call_name(sub.func)
                        if name in ("frappe.init", "frappe.connect"):
                            violations.append(
                                _v(
                                    rule_id,
                                    path,
                                    getattr(sub, "lineno", 1),
                                    f"forbidden {name}() in {context} (breaks gunicorn --preload worker isolation)",
                                )
                            )

        if isinstance(tree, ast.Module):
            _scan_nodes(tree.body, "preload/WSGI module import scope")

    return violations


def _is_fatal_signal_call(node: ast.Call) -> bool:
    name = _dotted_call_name(node.func)
    if name not in ("os.kill", "signal.raise_signal", "raise_signal"):
        return False
    sig_arg = node.args[1] if name == "os.kill" and len(node.args) >= 2 else (
        node.args[0] if name != "os.kill" and len(node.args) >= 1 else None
    )
    if sig_arg is None:
        return False
    if isinstance(sig_arg, ast.Constant) and sig_arg.value in (9, 15):
        return True
    sig_name = _dotted_call_name(sig_arg)
    return sig_name in ("signal.SIGTERM", "signal.SIGKILL", "SIGTERM", "SIGKILL")


def _is_multi_worker_expr(node: ast.AST) -> bool:
    """Return True iff `node` checks `workers >= 2` or `workers > 1` (or an AND-conjunction with it)."""
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left_text = _dotted_call_name(node.left).lower()
        if "worker" not in left_text:
            return False
        op = node.ops[0]
        comp = node.comparators[0]
        if isinstance(comp, ast.Constant) and isinstance(comp.value, (int, float)):
            if isinstance(op, ast.GtE) and comp.value >= 2:
                return True
            if isinstance(op, ast.Gt) and comp.value >= 1:
                return True
        return False
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return any(_is_multi_worker_expr(v) for v in node.values)
    return False


def _is_single_worker_guard_expr(node: ast.AST) -> bool:
    """Return True iff `node` checks single-worker mode (`workers < 2`, `workers <= 1`, `workers == 1`, `not (workers >= 2)`)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _is_multi_worker_expr(node.operand)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left_text = _dotted_call_name(node.left).lower()
        if "worker" not in left_text:
            return False
        op = node.ops[0]
        comp = node.comparators[0]
        if isinstance(comp, ast.Constant) and isinstance(comp.value, (int, float)):
            # Must evaluate True when workers == 1!
            if isinstance(op, ast.Lt) and comp.value >= 2:
                return True
            if isinstance(op, ast.LtE) and comp.value >= 1:
                return True
            if isinstance(op, ast.Eq) and comp.value == 1:
                return True
        return False
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return any(_is_single_worker_guard_expr(v) for v in node.values)
    return False


def _always_exits_block(stmts: list[ast.stmt]) -> bool:
    return any(isinstance(s, (ast.Return, ast.Raise)) for s in stmts)


def _find_unguarded_fatal_signals(nodes: list[ast.stmt], *, guarded: bool = False) -> list[ast.Call]:
    """Walk statements respecting enclosing `workers >= 2` and early-return `if workers < 2: return` guards."""
    hits: list[ast.Call] = []
    currently_guarded = guarded

    def _collect_expr_signals(expr: ast.AST) -> None:
        if currently_guarded:
            return
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Call) and _is_fatal_signal_call(sub):
                hits.append(sub)

    for stmt in nodes:
        if isinstance(stmt, ast.If):
            _collect_expr_signals(stmt.test)
            is_mw = _is_multi_worker_expr(stmt.test)
            is_sw = _is_single_worker_guard_expr(stmt.test)
            hits.extend(_find_unguarded_fatal_signals(stmt.body, guarded=(currently_guarded or is_mw)))
            hits.extend(_find_unguarded_fatal_signals(stmt.orelse, guarded=(currently_guarded or is_sw)))
            if not currently_guarded and is_sw and _always_exits_block(stmt.body):
                currently_guarded = True
            if not currently_guarded and is_mw and _always_exits_block(stmt.orelse) and stmt.orelse:
                currently_guarded = True
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hits.extend(_find_unguarded_fatal_signals(stmt.body, guarded=currently_guarded))
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            _collect_expr_signals(stmt.iter if hasattr(stmt, "iter") else stmt.test)
            hits.extend(_find_unguarded_fatal_signals(stmt.body, guarded=currently_guarded))
            hits.extend(_find_unguarded_fatal_signals(stmt.orelse, guarded=currently_guarded))
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                _collect_expr_signals(item.context_expr)
            hits.extend(_find_unguarded_fatal_signals(stmt.body, guarded=currently_guarded))
        elif isinstance(stmt, ast.Try):
            hits.extend(_find_unguarded_fatal_signals(stmt.body, guarded=currently_guarded))
            for handler in stmt.handlers:
                hits.extend(_find_unguarded_fatal_signals(handler.body, guarded=currently_guarded))
            hits.extend(_find_unguarded_fatal_signals(stmt.orelse, guarded=currently_guarded))
            hits.extend(_find_unguarded_fatal_signals(stmt.finalbody, guarded=currently_guarded))
        elif not currently_guarded:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call) and _is_fatal_signal_call(sub):
                    hits.append(sub)
    return hits


def _extract_hook_functions(file_map: FileMap) -> set[tuple[str, str]]:
    """Parse `apps/khitmee_ops/khitmee_ops/hooks.py` for request/doc_event hook targets."""
    hooks_path = "apps/khitmee_ops/khitmee_ops/hooks.py"
    raw = file_map.get(hooks_path)
    if not raw:
        return set()
    tree = _parse_python(raw)
    if tree is None:
        return set()
    targets: set[tuple[str, str]] = set()

    def _collect_strings(node: ast.AST) -> list[str]:
        found: list[str] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "." in sub.value:
                found.append(sub.value)
        return found

    if isinstance(tree, ast.Module):
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign):
                continue
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id in ("after_request", "before_request", "doc_events"):
                    for dotted in _collect_strings(stmt.value):
                        mod, func = dotted.rsplit(".", 1)
                        for local_path in _resolve_repo_local_module_paths(mod, file_map):
                            targets.add((local_path, func))
    return targets


def rule_no_self_sigterm_single_worker(file_map: FileMap) -> list[Violation]:
    """Rule 2: Ban os.kill/signal.raise_signal(SIGTERM/SIGKILL) reachable from request/after_response hooks without workers >= 2 guard."""
    rule_id = "no-self-sigterm-single-worker"
    hook_targets = _extract_hook_functions(file_map)
    violations: list[Violation] = []

    for path in sorted(file_map):
        if not (path.startswith("apps/khitmee_ops/") and path.endswith(".py")):
            continue
        if "/tests/" in path:
            continue
        tree = _parse_python(file_map[path])
        if not isinstance(tree, ast.Module):
            continue

        funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs[node.name] = node

        # Build intra-module call graph (including callbacks passed as arguments, e.g. after_response.add(cb)).
        calls_map: dict[str, set[str]] = {name: set() for name in funcs}
        entry_funcs: set[str] = set()
        for name, fnode in funcs.items():
            if (path, name) in hook_targets or "after_response" in name or "after_request" in name:
                entry_funcs.add(name)
            for dec in fnode.decorator_list:
                dec_name = _dotted_call_name(dec.func if isinstance(dec, ast.Call) else dec)
                if dec_name in ("frappe.whitelist", "whitelist"):
                    entry_funcs.add(name)
            for sub in ast.walk(fnode):
                if isinstance(sub, ast.Attribute) and sub.attr == "after_response":
                    entry_funcs.add(name)
                if isinstance(sub, ast.Call):
                    callee = _dotted_call_name(sub.func)
                    if callee in funcs:
                        calls_map[name].add(callee)
                    for arg in sub.args:
                        if isinstance(arg, ast.Name) and arg.id in funcs:
                            calls_map[name].add(arg.id)

        # Traverse reachable functions from entry hooks
        reachable: set[str] = set()
        stack = list(entry_funcs)
        while stack:
            cur = stack.pop()
            if cur in reachable:
                continue
            reachable.add(cur)
            for nxt in calls_map.get(cur, ()):
                if nxt not in reachable:
                    stack.append(nxt)

        for fname in sorted(reachable):
            fnode = funcs[fname]
            for sub in _find_unguarded_fatal_signals(fnode.body, guarded=False):
                violations.append(
                    _v(
                        rule_id,
                        path,
                        getattr(sub, "lineno", fnode.lineno),
                        f"fatal signal call in request/after_response hook {fname}() without workers >= 2 guard",
                    )
                )
    return violations


def _is_truthy_const(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return node.value
        if isinstance(node.value, (int, float)):
            return node.value != 0
        if isinstance(node.value, str):
            return node.value.strip().lower() in ("1", "true", "yes")
    return False


def _dict_comp_strips_key(node: ast.DictComp, key_name: str) -> bool:
    if not isinstance(node.key, ast.Name):
        return False
    emitted_key = node.key.id
    for gen in node.generators:
        for cond in gen.ifs:
            if isinstance(cond, ast.Compare) and len(cond.ops) == 1 and len(cond.comparators) == 1:
                if isinstance(cond.ops[0], ast.NotEq):
                    left, right = cond.left, cond.comparators[0]
                    if (
                        isinstance(left, ast.Name)
                        and left.id == emitted_key
                        and isinstance(right, ast.Constant)
                        and right.value == key_name
                    ):
                        return True
                    if (
                        isinstance(right, ast.Name)
                        and right.id == emitted_key
                        and isinstance(left, ast.Constant)
                        and left.value == key_name
                    ):
                        return True
    return False


def _is_falsy_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in (False, 0, None, "")


def _stmt_always_exits(stmts: list[ast.stmt]) -> bool:
    for stmt in stmts:
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return True
        if isinstance(stmt, ast.If) and stmt.orelse:
            if _stmt_always_exits(stmt.body) and _stmt_always_exits(stmt.orelse):
                return True
    return False


def _dict_known_safe_omits_key(node: ast.Dict, key_name: str) -> bool:
    """Return True iff the dict literal is guaranteed not to carry a truthy `key_name`."""
    safe = True
    for dk, dv in zip(node.keys, node.values):
        if dk is None:
            safe = False
        elif isinstance(dk, ast.Constant):
            if dk.value == key_name:
                safe = _is_falsy_const(dv)
        else:
            # Computed key expression may evaluate to `key_name` at runtime
            if not _is_falsy_const(dv):
                safe = False
    return safe


def _dict_unsanitized_forward(node: ast.Dict, sanitized: set[str]) -> str | None:
    """Return an unsanitized source name if `**{...}` forwards an unstripped mapping or unsafe override."""
    unsafe_src: str | None = None
    saw_unpack_or_flag = False
    for dk, dv in zip(node.keys, node.values):
        if dk is None:
            saw_unpack_or_flag = True
            src = _dotted_call_name(dv)
            if src in ("frappe.form_dict", "form_dict", "kwargs") and src not in sanitized:
                unsafe_src = src
        elif isinstance(dk, ast.Constant):
            if dk.value == "export_in_background":
                saw_unpack_or_flag = True
                if _is_falsy_const(dv):
                    unsafe_src = None
                elif not _is_truthy_const(dv):
                    unsafe_src = unsafe_src or "export_in_background"
        else:
            # Computed key after an unpack or explicit false override can reinstate export_in_background
            if saw_unpack_or_flag and not _is_falsy_const(dv):
                unsafe_src = unsafe_src or "computed_key"
    return unsafe_src


def rule_no_background_email_export(file_map: FileMap) -> list[Violation]:
    """Rule 3: Ban truthy `export_in_background` or unstripped forwarding in apps/khitmee_ops report handlers."""
    rule_id = "no-background-email-export"
    violations: list[Violation] = []
    export_call_names = {"export_query", "background_enqueue_run", "enqueue_export", "run_export"}

    for path in sorted(file_map):
        if not (path.startswith("apps/khitmee_ops/") and path.endswith(".py")):
            continue
        if "/tests/" in path:
            continue
        tree = _parse_python(file_map[path])
        if not isinstance(tree, ast.Module):
            continue

        is_report_handler = "/report/" in path or "/api/" in path
        if not is_report_handler:
            continue

        def _check_expr(expr: ast.AST, sanitized: set[str]) -> None:
            stack: list[ast.AST] = [expr]
            while stack:
                node = stack.pop()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    if isinstance(node, ast.Lambda):
                        for default in list(node.args.defaults) + [
                            d for d in node.args.kw_defaults if d is not None
                        ]:
                            _check_expr(default, sanitized)
                        _check_expr(node.body, set())
                    continue
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "export_in_background" and _is_truthy_const(kw.value):
                            violations.append(
                                _v(
                                    rule_id,
                                    path,
                                    getattr(kw, "lineno", node.lineno),
                                    "truthy export_in_background is forbidden in report handlers",
                                )
                            )
                    call_leaf = _dotted_call_name(node.func).rsplit(".", 1)[-1]
                    if call_leaf in export_call_names:
                        for kw in node.keywords:
                            if kw.arg is None:
                                if isinstance(kw.value, ast.DictComp) and _dict_comp_strips_key(
                                    kw.value, "export_in_background"
                                ):
                                    continue
                                if isinstance(kw.value, ast.Dict):
                                    unsafe_src = _dict_unsanitized_forward(kw.value, sanitized)
                                    if unsafe_src is not None:
                                        violations.append(
                                            _v(
                                                rule_id,
                                                path,
                                                getattr(node, "lineno", 1),
                                                f"unstripped **{unsafe_src} forwarding into {call_leaf}() may enable export_in_background",
                                            )
                                        )
                                    continue
                                forwarded = _dotted_call_name(kw.value)
                                if forwarded in ("frappe.form_dict", "form_dict", "kwargs") and (
                                    forwarded not in sanitized
                                ):
                                    violations.append(
                                        _v(
                                            rule_id,
                                            path,
                                            getattr(node, "lineno", 1),
                                            f"unstripped **{forwarded} forwarding into {call_leaf}() may enable export_in_background",
                                        )
                                    )
                elif isinstance(node, ast.Dict):
                    for k, val in zip(node.keys, node.values):
                        if (
                            isinstance(k, ast.Constant)
                            and k.value == "export_in_background"
                            and _is_truthy_const(val)
                        ):
                            violations.append(
                                _v(
                                    rule_id,
                                    path,
                                    getattr(k, "lineno", node.lineno),
                                    "truthy 'export_in_background' dict entry is forbidden in report handlers",
                                )
                            )
                for child in ast.iter_child_nodes(node):
                    stack.append(child)

        def _pop_target(expr: ast.AST) -> str | None:
            if (
                isinstance(expr, ast.Call)
                and isinstance(expr.func, ast.Attribute)
                and expr.func.attr == "pop"
                and expr.args
                and isinstance(expr.args[0], ast.Constant)
                and expr.args[0].value == "export_in_background"
            ):
                return _dotted_call_name(expr.func.value)
            return None

        def _cond_pop_target(expr: ast.AST) -> str | None:
            """Return pop target if `expr` unconditionally pops `export_in_background` before branching."""
            direct = _pop_target(expr)
            if direct:
                return direct
            if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
                return _cond_pop_target(expr.operand)
            if isinstance(expr, ast.BoolOp) and expr.values:
                # Only the first operand is guaranteed to execute without short-circuiting.
                return _cond_pop_target(expr.values[0])
            return None

        def _check_target_exprs(targets: list[ast.expr], sanitized: set[str]) -> None:
            for tgt in targets:
                if isinstance(tgt, ast.Subscript):
                    _check_expr(tgt.value, sanitized)
                    _check_expr(tgt.slice, sanitized)
                elif isinstance(tgt, ast.Attribute):
                    _check_expr(tgt.value, sanitized)
                elif isinstance(tgt, (ast.Tuple, ast.List)):
                    _check_target_exprs(list(tgt.elts), sanitized)

        def _dirty_mutation_target(expr: ast.AST) -> str | None:
            """Return mapping name if `expr` mutates a mapping in a way that may insert truthy export_in_background."""
            if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)):
                return None
            target = _dotted_call_name(expr.func.value)
            if not target:
                return None
            if expr.func.attr == "update":
                # Safe only if all positional args are provably flag-free dict literals / stripping comprehensions
                # and no keyword arg sets export_in_background to a non-falsy value.
                for kw in expr.keywords:
                    if kw.arg == "export_in_background" and not _is_falsy_const(kw.value):
                        return target
                    if kw.arg is None:
                        return target
                for arg in expr.args:
                    if isinstance(arg, ast.Dict) and _dict_known_safe_omits_key(arg, "export_in_background"):
                        continue
                    if isinstance(arg, ast.DictComp) and _dict_comp_strips_key(arg, "export_in_background"):
                        continue
                    return target
                return None
            if expr.func.attr == "setdefault":
                if expr.args and isinstance(expr.args[0], ast.Constant) and expr.args[0].value == "export_in_background":
                    if len(expr.args) >= 2 and _is_falsy_const(expr.args[1]):
                        return None
                    return target
                if expr.args and not isinstance(expr.args[0], ast.Constant):
                    return target
            return None

        def _update_sanitized_from_assign(
            targets: list[ast.expr],
            value: ast.expr,
            sanitized: set[str],
            *,
            emit_violations: bool = True,
        ) -> None:
            popped = _pop_target(value)
            if popped:
                sanitized.add(popped)
            mutated = _dirty_mutation_target(value)
            if mutated:
                sanitized.discard(mutated)
            is_safe_comp = isinstance(value, ast.DictComp) and _dict_comp_strips_key(value, "export_in_background")
            is_safe_dict = isinstance(value, ast.Dict) and _dict_known_safe_omits_key(value, "export_in_background")
            is_falsy_val = _is_falsy_const(value)
            is_truthy_val = _is_truthy_const(value)

            for tgt in targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.slice, ast.Constant)
                    and tgt.slice.value == "export_in_background"
                ):
                    if is_truthy_val and emit_violations:
                        violations.append(
                            _v(
                                rule_id,
                                path,
                                getattr(tgt, "lineno", 1),
                                "assigning truthy export_in_background is forbidden in report handlers",
                            )
                        )
                    target_name = _dotted_call_name(tgt.value)
                    if is_falsy_val:
                        sanitized.add(target_name)
                    else:
                        sanitized.discard(target_name)
                elif isinstance(tgt, (ast.Name, ast.Attribute)):
                    target_name = _dotted_call_name(tgt)
                    if is_safe_comp or is_safe_dict:
                        sanitized.add(target_name)
                    else:
                        sanitized.discard(target_name)
                elif isinstance(tgt, (ast.Tuple, ast.List)):
                    for elt in tgt.elts:
                        if isinstance(elt, (ast.Name, ast.Attribute)):
                            sanitized.discard(_dotted_call_name(elt))

        def _intersect_all(states: list[set[str]]) -> set[str]:
            if not states:
                return set()
            merged = set(states[0])
            for st_item in states[1:]:
                merged &= st_item
            return merged

        def _is_pure_sanitizing_stmt(stmt: ast.stmt) -> bool:
            if isinstance(stmt, ast.Expr) and _pop_target(stmt.value) is not None:
                return True
            if isinstance(stmt, ast.Delete):
                return all(
                    isinstance(dtgt, ast.Subscript)
                    and isinstance(dtgt.slice, ast.Constant)
                    and dtgt.slice.value == "export_in_background"
                    for dtgt in stmt.targets
                )
            if isinstance(stmt, ast.Assign) and _is_falsy_const(stmt.value):
                return all(
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.slice, ast.Constant)
                    and tgt.slice.value == "export_in_background"
                    for tgt in stmt.targets
                )
            return False

        def _scan_block(
            stmts: list[ast.stmt],
            sanitized: set[str],
            *,
            exit_states: list[set[str]] | None = None,
            except_states: list[set[str]] | None = None,
            emit_violations: bool = True,
        ) -> set[str]:
            cur = set(sanitized)
            for stmt in stmts:
                if except_states is not None and not _is_pure_sanitizing_stmt(stmt):
                    except_states.append(set(cur))
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if emit_violations:
                        for dec in stmt.decorator_list:
                            _check_expr(dec, cur)
                        for default in list(stmt.args.defaults) + [d for d in stmt.args.kw_defaults if d is not None]:
                            _check_expr(default, cur)
                        _scan_block(stmt.body, set(), emit_violations=True)
                    continue
                if isinstance(stmt, ast.ClassDef):
                    if emit_violations:
                        for dec in stmt.decorator_list:
                            _check_expr(dec, cur)
                        for base in stmt.bases:
                            _check_expr(base, cur)
                        _scan_block(stmt.body, set(), emit_violations=True)
                    continue
                if isinstance(stmt, ast.Expr):
                    if emit_violations:
                        _check_expr(stmt.value, cur)
                    popped = _pop_target(stmt.value)
                    if popped:
                        cur.add(popped)
                    mutated = _dirty_mutation_target(stmt.value)
                    if mutated:
                        cur.discard(mutated)
                elif isinstance(stmt, ast.Assign):
                    if emit_violations:
                        _check_expr(stmt.value, cur)
                        _check_target_exprs(stmt.targets, cur)
                    _update_sanitized_from_assign(stmt.targets, stmt.value, cur, emit_violations=emit_violations)
                elif isinstance(stmt, ast.AnnAssign):
                    if stmt.value is not None:
                        if emit_violations:
                            _check_expr(stmt.value, cur)
                            _check_target_exprs([stmt.target], cur)
                        _update_sanitized_from_assign(
                            [stmt.target], stmt.value, cur, emit_violations=emit_violations
                        )
                    elif emit_violations:
                        _check_target_exprs([stmt.target], cur)
                elif isinstance(stmt, ast.AugAssign):
                    if emit_violations:
                        _check_expr(stmt.value, cur)
                        _check_target_exprs([stmt.target], cur)
                    if isinstance(stmt.target, (ast.Name, ast.Attribute)):
                        is_safe_rhs = (
                            isinstance(stmt.op, ast.BitOr)
                            and (
                                (isinstance(stmt.value, ast.Dict) and _dict_known_safe_omits_key(stmt.value, "export_in_background"))
                                or (isinstance(stmt.value, ast.DictComp) and _dict_comp_strips_key(stmt.value, "export_in_background"))
                            )
                        )
                        if not is_safe_rhs:
                            cur.discard(_dotted_call_name(stmt.target))
                elif isinstance(stmt, ast.Delete):
                    for dtgt in stmt.targets:
                        if emit_violations and isinstance(dtgt, ast.Subscript):
                            _check_expr(dtgt.value, cur)
                            _check_expr(dtgt.slice, cur)
                        if (
                            isinstance(dtgt, ast.Subscript)
                            and isinstance(dtgt.slice, ast.Constant)
                            and dtgt.slice.value == "export_in_background"
                        ):
                            cur.add(_dotted_call_name(dtgt.value))
                        elif isinstance(dtgt, (ast.Name, ast.Attribute)):
                            cur.discard(_dotted_call_name(dtgt))
                elif isinstance(stmt, ast.If):
                    if emit_violations:
                        _check_expr(stmt.test, cur)
                    cond_popped = _cond_pop_target(stmt.test)
                    if cond_popped:
                        cur.add(cond_popped)
                    then_san = _scan_block(
                        stmt.body, set(cur), exit_states=exit_states, except_states=except_states,
                        emit_violations=emit_violations,
                    )
                    else_san = _scan_block(
                        stmt.orelse, set(cur), exit_states=exit_states, except_states=except_states,
                        emit_violations=emit_violations,
                    )
                    then_exits = _stmt_always_exits(stmt.body)
                    else_exits = _stmt_always_exits(stmt.orelse) if stmt.orelse else False
                    if then_exits and not else_exits:
                        cur = else_san
                    elif else_exits and not then_exits:
                        cur = then_san
                    else:
                        cur = then_san & else_san
                elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                    if emit_violations:
                        _check_expr(stmt.iter, cur)
                    body_san = _scan_block(
                        stmt.body, set(cur), exit_states=exit_states, except_states=except_states,
                        emit_violations=emit_violations,
                    )
                    else_san = (
                        _scan_block(
                            stmt.orelse, set(cur), exit_states=exit_states, except_states=except_states,
                            emit_violations=emit_violations,
                        )
                        if stmt.orelse
                        else set(cur)
                    )
                    cur = cur & body_san & else_san
                elif isinstance(stmt, ast.While):
                    if emit_violations:
                        _check_expr(stmt.test, cur)
                    body_san = _scan_block(
                        stmt.body, set(cur), exit_states=exit_states, except_states=except_states,
                        emit_violations=emit_violations,
                    )
                    else_san = (
                        _scan_block(
                            stmt.orelse, set(cur), exit_states=exit_states, except_states=except_states,
                            emit_violations=emit_violations,
                        )
                        if stmt.orelse
                        else set(cur)
                    )
                    cur = cur & body_san & else_san
                elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                    for item in stmt.items:
                        if emit_violations:
                            _check_expr(item.context_expr, cur)
                        if item.optional_vars is not None and isinstance(item.optional_vars, (ast.Name, ast.Attribute)):
                            cur.discard(_dotted_call_name(item.optional_vars))
                    cur = _scan_block(
                        stmt.body, cur, exit_states=exit_states, except_states=except_states,
                        emit_violations=emit_violations,
                    )
                elif isinstance(stmt, ast.Try):
                    try_exits: list[set[str]] = []
                    try_excepts: list[set[str]] = []
                    body_san = _scan_block(
                        stmt.body, set(cur), exit_states=try_exits, except_states=try_excepts,
                        emit_violations=emit_violations,
                    )
                    if not try_excepts:
                        try_excepts.append(set(cur))
                    except_in = _intersect_all(try_excepts)

                    else_exits: list[set[str]] = []
                    else_san = (
                        _scan_block(
                            stmt.orelse, set(body_san), exit_states=else_exits, except_states=except_states,
                            emit_violations=emit_violations,
                        )
                        if stmt.orelse
                        else body_san
                    )
                    fallthrough_paths: list[set[str]] = []
                    finally_inputs: list[set[str]] = []
                    if not _stmt_always_exits(stmt.body) and not _stmt_always_exits(stmt.orelse):
                        fallthrough_paths.append(else_san)
                        finally_inputs.append(else_san)
                    finally_inputs.extend(try_exits)
                    finally_inputs.extend(else_exits)

                    for handler in stmt.handlers:
                        h_exits: list[set[str]] = []
                        h_san = _scan_block(
                            handler.body, set(except_in), exit_states=h_exits, except_states=except_states,
                            emit_violations=emit_violations,
                        )
                        if not _stmt_always_exits(handler.body):
                            fallthrough_paths.append(h_san)
                            finally_inputs.append(h_san)
                        finally_inputs.extend(h_exits)

                    if not stmt.handlers:
                        finally_inputs.append(except_in)

                    if exit_states is not None:
                        exit_states.extend(try_exits)
                        exit_states.extend(else_exits)

                    fallthrough_cur = _intersect_all(fallthrough_paths) if fallthrough_paths else set(cur)
                    if stmt.finalbody:
                        # Check expressions INSIDE finally using ALL paths that can reach finally
                        fin_in = _intersect_all(finally_inputs) if finally_inputs else fallthrough_cur
                        _scan_block(
                            stmt.finalbody, fin_in, exit_states=exit_states, except_states=except_states,
                            emit_violations=emit_violations,
                        )
                        # Outgoing normal fallthrough after finally only receives normal fallthrough paths!
                        cur = _scan_block(stmt.finalbody, fallthrough_cur, emit_violations=False)
                    else:
                        cur = fallthrough_cur
                elif isinstance(stmt, (ast.Return, ast.Raise)):
                    if emit_violations:
                        for child in ast.iter_child_nodes(stmt):
                            _check_expr(child, cur)
                    if exit_states is not None:
                        exit_states.append(set(cur))
                else:
                    if emit_violations:
                        for child in ast.iter_child_nodes(stmt):
                            _check_expr(child, cur)
                if except_states is not None:
                    except_states.append(set(cur))
            return cur

        _scan_block(tree.body, set())
    return violations


_CLIENT_CODE_EXTS = (
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
    ".html",
    ".htm",
    ".py",
    ".rs",
    ".slint",
    ".json",
)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/|<!--.*?-->", re.DOTALL)


def _is_client_override_scope(path: str) -> bool:
    if not path.lower().endswith(_CLIENT_CODE_EXTS):
        return False
    if path.startswith("desktop-client/") or path.startswith("desktop-frontend/"):
        return True
    if path.startswith("apps/khitmee_ops/") and ("/public/js/" in path or "/www/" in path):
        return True
    return False


def _strip_single_line_comment(line: str, is_python: bool) -> str:
    if is_python:
        # Strip # comments outside quotes
        in_sq = in_dq = escaped = False
        for idx, ch in enumerate(line):
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
            elif ch == "'" and not in_dq:
                in_sq = not in_sq
            elif ch == '"' and not in_sq:
                in_dq = not in_dq
            elif ch == "#" and not in_sq and not in_dq:
                return line[:idx]
        return line
    # Strip // comments outside quotes
    in_sq = in_dq = in_bt = escaped = False
    for idx in range(len(line)):
        ch = line[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        elif ch == "'" and not in_dq and not in_bt:
            in_sq = not in_sq
        elif ch == '"' and not in_sq and not in_bt:
            in_dq = not in_dq
        elif ch == "`" and not in_sq and not in_dq:
            in_bt = not in_bt
        elif ch == "/" and not in_sq and not in_dq and not in_bt and idx + 1 < len(line) and line[idx + 1] == "/":
            return line[:idx]
    return line


def rule_no_client_override_rate(file_map: FileMap) -> list[Violation]:
    """Rule 4: Ban client UI/desktop payloads from sending `override_rate` (ignores docs/comments)."""
    rule_id = "no-client-override-rate"
    violations: list[Violation] = []
    for path in sorted(file_map):
        if not _is_client_override_scope(path):
            continue
        raw_text = file_map[path].decode("utf-8", errors="replace")
        # Preserve line numbers by replacing block comment contents with equivalent newlines
        cleaned_text = _BLOCK_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), raw_text)
        is_python = path.lower().endswith(".py")
        for lineno, line in enumerate(cleaned_text.splitlines(), start=1):
            code_part = _strip_single_line_comment(line, is_python)
            if _OVERRIDE_RATE_RE.search(code_part):
                violations.append(
                    _v(
                        rule_id,
                        path,
                        lineno,
                        "client UI/desktop payloads must not reference or send override_rate",
                    )
                )
    return violations


def rule_no_webview_in_native_desktop(file_map: FileMap) -> list[Violation]:
    """Rule 5: Inapplicable when `desktop-frontend/` is absent; otherwise ban webview crates in Cargo.toml/Cargo.lock."""
    rule_id = "no-webview-in-native-desktop"
    desktop_files = [p for p in file_map if p.startswith("desktop-frontend/")]
    if not desktop_files:
        return []

    violations: list[Violation] = []
    cargo_tomls: list[str] = [p for p in sorted(file_map) if p.startswith("desktop-frontend/") and p.endswith("Cargo.toml")]
    cargo_locks: list[str] = [p for p in sorted(file_map) if p.startswith("desktop-frontend/") and p.endswith("Cargo.lock")]

    # Include root workspace Cargo.toml / Cargo.lock if workspace references desktop-frontend
    if "Cargo.toml" in file_map:
        try:
            root_toml = tomllib.loads(file_map["Cargo.toml"].decode("utf-8"))
            members = (root_toml.get("workspace") or {}).get("members") or []
            if any("desktop-frontend" in str(m) for m in members):
                cargo_tomls.append("Cargo.toml")
                if "Cargo.lock" in file_map:
                    cargo_locks.append("Cargo.lock")
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            pass

    dep_sections = ("dependencies", "dev-dependencies", "build-dependencies")

    def _check_dep_table(path: str, table: dict[str, Any]) -> None:
        if not isinstance(table, dict):
            return
        for dep_key, spec in table.items():
            real_pkg = dep_key
            if isinstance(spec, dict) and isinstance(spec.get("package"), str):
                real_pkg = spec["package"]
            if dep_key in _WEBVIEW_CRATES or real_pkg in _WEBVIEW_CRATES:
                violations.append(
                    _v(
                        rule_id,
                        path,
                        1,
                        f"forbidden webview crate {real_pkg!r} (key={dep_key!r}) in native desktop-frontend",
                    )
                )

    for path in sorted(set(cargo_tomls)):
        try:
            data = tomllib.loads(file_map[path].decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            violations.append(_v(rule_id, path, 1, f"failed to parse Cargo.toml: {exc}"))
            continue
        for sec in dep_sections:
            _check_dep_table(path, data.get(sec) or {})
        ws = data.get("workspace") or {}
        if isinstance(ws, dict):
            _check_dep_table(path, ws.get("dependencies") or {})
        targets = data.get("target") or {}
        if isinstance(targets, dict):
            for tcfg in targets.values():
                if isinstance(tcfg, dict):
                    for sec in dep_sections:
                        _check_dep_table(path, tcfg.get(sec) or {})

    for path in sorted(set(cargo_locks)):
        try:
            data = tomllib.loads(file_map[path].decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            violations.append(_v(rule_id, path, 1, f"failed to parse Cargo.lock: {exc}"))
            continue
        for pkg in data.get("package") or []:
            if isinstance(pkg, dict) and pkg.get("name") in _WEBVIEW_CRATES:
                violations.append(
                    _v(
                        rule_id,
                        path,
                        1,
                        f"forbidden webview crate {pkg['name']!r} resolved in Cargo.lock for desktop-frontend",
                    )
                )

    return violations


def _is_zero_literal(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
        and float(node.value) == 0.0
    )


def _is_none_check(test: ast.AST) -> bool:
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
        op = test.ops[0]
        comp = test.comparators[0]
        if isinstance(op, (ast.Is, ast.Eq)) and isinstance(comp, ast.Constant) and comp.value is None:
            return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Name):
        return test.operand.id in ("value", "val", "raw", "text")
    return False


def rule_no_silent_zero_coercion_in_ledgers(file_map: FileMap) -> list[Violation]:
    """Rule 6: Ban silent None/error -> 0 coercion in scoped ledger/parity/offload parser functions."""
    rule_id = "no-silent-zero-coercion-in-ledgers"
    violations: list[Violation] = []

    for path, target_funcs in sorted(_LEDGER_ZERO_SCOPES.items()):
        raw = file_map.get(path)
        if raw is None:
            continue
        tree = _parse_python(raw)
        if not isinstance(tree, ast.Module):
            continue

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in target_funcs:
                continue

            # 1) empty_metrics must initialize to None, never 0 / 0.0
            if node.name == "empty_metrics":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.DictComp) and _is_zero_literal(sub.value):
                        violations.append(
                            _v(rule_id, path, sub.lineno, "empty_metrics() must initialize missing metrics to None, not 0")
                        )
                    if isinstance(sub, ast.Dict):
                        for val in sub.values:
                            if _is_zero_literal(val):
                                violations.append(
                                    _v(rule_id, path, getattr(val, "lineno", sub.lineno), "empty_metrics() must use None, not 0")
                                )

            for sub in ast.walk(node):
                # 2) `except ...: return 0 / 0.0`
                if isinstance(sub, ast.ExceptHandler):
                    for estmt in sub.body:
                        if isinstance(estmt, ast.Return) and _is_zero_literal(estmt.value):
                            violations.append(
                                _v(
                                    rule_id,
                                    path,
                                    estmt.lineno,
                                    f"{node.name}() must not silently return 0 on exception",
                                )
                            )
                # 3) `if value is None: return 0`
                if isinstance(sub, ast.If) and _is_none_check(sub.test):
                    for istmt in sub.body:
                        if isinstance(istmt, ast.Return) and _is_zero_literal(istmt.value):
                            violations.append(
                                _v(
                                    rule_id,
                                    path,
                                    istmt.lineno,
                                    f"{node.name}() must not coerce None/missing evidence to 0",
                                )
                            )
                # 4) `return 0 if value is None else ...`
                if isinstance(sub, ast.IfExp) and _is_none_check(sub.test) and _is_zero_literal(sub.body):
                    violations.append(
                        _v(
                            rule_id,
                            path,
                            sub.lineno,
                            f"{node.name}() must not coerce None/missing evidence to 0",
                        )
                    )
                # 5) `value or 0` / `float(value or 0)`
                if isinstance(sub, ast.BoolOp) and isinstance(sub.op, ast.Or):
                    if any(_is_zero_literal(v) for v in sub.values[1:]):
                        violations.append(
                            _v(
                                rule_id,
                                path,
                                sub.lineno,
                                f"{node.name}() must not coerce falsy/missing value to 0 with `or 0`",
                            )
                        )
    return violations


def rule_autoloop_skill_parity_and_optin(file_map: FileMap) -> list[Violation]:
    """Rule 7: Verify synced SKILL.md byte-parity, LF without BOM, and disable-model-invocation: true."""
    rule_id = "autoloop-skill-parity-and-optin"
    src_path = "tools/autoloop/skill/SKILL.md"
    dst_path = ".devin/skills/autoloop/SKILL.md"
    violations: list[Violation] = []

    src_raw = file_map.get(src_path)
    dst_raw = file_map.get(dst_path)

    if src_raw is None and dst_raw is None:
        return violations
    if src_raw is None:
        violations.append(_v(rule_id, src_path, 1, "missing canonical tools/autoloop/skill/SKILL.md"))
    if dst_raw is None:
        violations.append(_v(rule_id, dst_path, 1, "missing synced .devin/skills/autoloop/SKILL.md"))
    if src_raw is None or dst_raw is None:
        return violations

    for p, raw in ((src_path, src_raw), (dst_path, dst_raw)):
        if raw.startswith(b"\xef\xbb\xbf"):
            violations.append(_v(rule_id, p, 1, "SKILL.md must not contain UTF-8 BOM"))
        if b"\r" in raw:
            violations.append(_v(rule_id, p, 1, "SKILL.md must use LF line endings only"))

    if src_raw != dst_raw:
        violations.append(
            _v(
                rule_id,
                dst_path,
                1,
                ".devin/skills/autoloop/SKILL.md is out of sync with tools/autoloop/skill/SKILL.md",
            )
        )

    text = src_raw.decode("utf-8", errors="replace")
    fm = _FRONTMATTER_RE.match(text)
    if not fm or not _DISABLE_MODEL_INVOCATION_RE.search(fm.group(1)):
        violations.append(
            _v(
                rule_id,
                src_path,
                1,
                "SKILL.md YAML frontmatter must declare `disable-model-invocation: true`",
            )
        )

    return violations


RULES = [
    ("no-frappe-init-in-preload", rule_no_frappe_init_in_preload),
    ("no-self-sigterm-single-worker", rule_no_self_sigterm_single_worker),
    ("no-background-email-export", rule_no_background_email_export),
    ("no-client-override-rate", rule_no_client_override_rate),
    ("no-webview-in-native-desktop", rule_no_webview_in_native_desktop),
    ("no-silent-zero-coercion-in-ledgers", rule_no_silent_zero_coercion_in_ledgers),
    ("autoloop-skill-parity-and-optin", rule_autoloop_skill_parity_and_optin),
]
