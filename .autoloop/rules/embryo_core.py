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


def _has_multi_worker_guard(func_node: ast.AST) -> bool:
    """Return True if function explicitly checks `workers >= 2` or `workers > 1`."""
    for sub in ast.walk(func_node):
        if isinstance(sub, ast.Compare) and len(sub.ops) == 1 and len(sub.comparators) == 1:
            left_text = _dotted_call_name(sub.left).lower()
            if "worker" not in left_text:
                continue
            op = sub.ops[0]
            comp = sub.comparators[0]
            if isinstance(comp, ast.Constant) and isinstance(comp.value, (int, float)):
                if isinstance(op, ast.GtE) and comp.value >= 2:
                    return True
                if isinstance(op, ast.Gt) and comp.value >= 1:
                    return True
    return False


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
            if _has_multi_worker_guard(fnode):
                continue
            for sub in ast.walk(fnode):
                if isinstance(sub, ast.Call) and _is_fatal_signal_call(sub):
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

        for node in ast.walk(tree):
            # Direct keyword arg: export_in_background=True / 1
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
                # Unstripped forwarding of form_dict / kwargs into export helpers
                call_leaf = _dotted_call_name(node.func).rsplit(".", 1)[-1]
                if call_leaf in export_call_names:
                    for kw in node.keywords:
                        if kw.arg is None:
                            forwarded = _dotted_call_name(kw.value)
                            if forwarded in ("frappe.form_dict", "form_dict", "kwargs"):
                                violations.append(
                                    _v(
                                        rule_id,
                                        path,
                                        getattr(node, "lineno", 1),
                                        f"unstripped **{forwarded} forwarding into {call_leaf}() may enable export_in_background",
                                    )
                                )
            # Dict literal: {"export_in_background": True/1}
            if isinstance(node, ast.Dict):
                for k, val in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "export_in_background" and _is_truthy_const(val):
                        violations.append(
                            _v(
                                rule_id,
                                path,
                                getattr(k, "lineno", node.lineno),
                                "truthy 'export_in_background' dict entry is forbidden in report handlers",
                            )
                        )
            # Subscript assignment: payload["export_in_background"] = True
            if isinstance(node, ast.Assign):
                if _is_truthy_const(node.value):
                    for tgt in node.targets:
                        if (
                            isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value == "export_in_background"
                        ):
                            violations.append(
                                _v(
                                    rule_id,
                                    path,
                                    getattr(node, "lineno", 1),
                                    "assigning truthy export_in_background is forbidden in report handlers",
                                )
                            )
    return violations


def _is_client_override_scope(path: str) -> bool:
    if path.startswith("desktop-client/") or path.startswith("desktop-frontend/"):
        return True
    if path.startswith("apps/khitmee_ops/") and ("/public/js/" in path or "/www/" in path):
        return True
    return False


def rule_no_client_override_rate(file_map: FileMap) -> list[Violation]:
    """Rule 4: Ban client UI/desktop payloads from sending `override_rate`."""
    rule_id = "no-client-override-rate"
    violations: list[Violation] = []
    for path in sorted(file_map):
        if not _is_client_override_scope(path):
            continue
        text = file_map[path].decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _OVERRIDE_RATE_RE.search(line):
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
