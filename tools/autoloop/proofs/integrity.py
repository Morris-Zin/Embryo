"""Diff-aware Tier-0 test and evaluator integrity guard.

The guard compares a candidate tree with a trusted base manifest.  It is
conservative rather than judgment-based: semantic evidence that disappears,
new suppression mechanisms, weakened numeric boundaries, changed oracles or
fixtures, and changed command/exit handling fail closed unless an exact,
unexpired owner waiver applies.  Waivers never waive an acceptance criterion.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .digests import sha256_bytes, sha256_json

INTEGRITY_SCHEMA_VERSION = 1

UNWAIVABLE_RULES = frozenset({"invalid-trusted-manifest", "untrusted-base"})


class IntegrityVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class ProtectedKind(str, Enum):
    TEST = "test"
    EVALUATOR = "evaluator"
    ORACLE = "oracle"
    FIXTURE = "fixture"


@dataclass(frozen=True)
class ProtectedPath:
    path: str
    kind: ProtectedKind


@dataclass(frozen=True)
class BaseDigest:
    path: str
    sha256: str


@dataclass(frozen=True)
class AcceptanceCriterion:
    criterion_id: str
    path: str
    token: str = ""


@dataclass(frozen=True)
class OwnerWaiver:
    diff_hash: str
    rule_id: str
    owner: str
    rationale: str
    replacement_evidence: str
    expires_at: str


@dataclass(frozen=True)
class TrustedBaseManifest:
    protected_paths: tuple[ProtectedPath, ...]
    base_digests: tuple[BaseDigest, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    waivers: tuple[OwnerWaiver, ...] = ()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TrustedBaseManifest:
        if payload.get("schema_version") != INTEGRITY_SCHEMA_VERSION:
            raise ValueError("trusted base manifest schema_version must be 1")
        protected = tuple(
            ProtectedPath(str(item["path"]), ProtectedKind(str(item["kind"])))
            for item in payload.get("protected_paths", ())
        )
        digests = tuple(
            BaseDigest(str(item["path"]), str(item["sha256"])) for item in payload.get("base_digests", ())
        )
        criteria = tuple(
            AcceptanceCriterion(str(item["criterion_id"]), str(item["path"]), str(item.get("token", "")))
            for item in payload.get("acceptance_criteria", ())
        )
        waivers = tuple(
            OwnerWaiver(
                diff_hash=str(item["diff_hash"]),
                rule_id=str(item["rule_id"]),
                owner=str(item["owner"]),
                rationale=str(item["rationale"]),
                replacement_evidence=str(item["replacement_evidence"]),
                expires_at=str(item["expires_at"]),
            )
            for item in payload.get("waivers", ())
        )
        return cls(protected, digests, criteria, waivers)

    @classmethod
    def from_json(cls, encoded: str | bytes) -> TrustedBaseManifest:
        return cls.from_dict(json.loads(encoded))

    def kind_by_path(self) -> dict[str, ProtectedKind]:
        return {item.path: item.kind for item in self.protected_paths}

    def digest_by_path(self) -> dict[str, str]:
        return {item.path: item.sha256 for item in self.base_digests}


@dataclass(frozen=True)
class IntegrityViolation:
    rule_id: str
    path: str
    message: str
    line: int = 0
    acceptance_criterion: str | None = None


@dataclass(frozen=True)
class WaiverRejection:
    diff_hash: str
    rule_id: str
    owner: str
    reason: str


@dataclass(frozen=True)
class IntegrityReport:
    verdict: IntegrityVerdict
    diff_hash: str
    violations: tuple[IntegrityViolation, ...]
    waived_violations: tuple[IntegrityViolation, ...] = ()
    applied_waivers: tuple[OwnerWaiver, ...] = ()
    rejected_waivers: tuple[WaiverRejection, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "diff_hash": self.diff_hash,
            "violations": [violation_to_dict(item) for item in self.violations],
            "waived_violations": [violation_to_dict(item) for item in self.waived_violations],
            "applied_waivers": [waiver_to_dict(item) for item in self.applied_waivers],
            "rejected_waivers": [rejection_to_dict(item) for item in self.rejected_waivers],
        }


def violation_to_dict(item: IntegrityViolation) -> dict[str, object]:
    return {
        "rule_id": item.rule_id,
        "path": item.path,
        "message": item.message,
        "line": item.line,
        "acceptance_criterion": item.acceptance_criterion,
    }


def waiver_to_dict(item: OwnerWaiver) -> dict[str, object]:
    return {
        "diff_hash": item.diff_hash,
        "rule_id": item.rule_id,
        "owner": item.owner,
        "rationale": item.rationale,
        "replacement_evidence": item.replacement_evidence,
        "expires_at": item.expires_at,
    }


def rejection_to_dict(item: WaiverRejection) -> dict[str, object]:
    return {
        "diff_hash": item.diff_hash,
        "rule_id": item.rule_id,
        "owner": item.owner,
        "reason": item.reason,
    }


@dataclass(frozen=True)
class _Occurrence:
    key: str
    path: str
    line: int
    text: str
    constants: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ThresholdOccurrence:
    subject: str
    direction: str
    value: float
    inclusive: bool
    path: str
    line: int
    text: str
    constants: tuple[str, ...]


@dataclass
class _SemanticModel:
    assertions: dict[str, list[_Occurrence]]
    cases: dict[str, list[_Occurrence]]
    suppressions: dict[str, list[_Occurrence]]
    thresholds: list[_ThresholdOccurrence]
    exact_comparisons: dict[str, list[_Occurrence]]
    approximations: list[_Occurrence]
    command_events: dict[str, list[_Occurrence]]
    syntax_errors: list[_Occurrence]


def trusted_manifest_for_files(
    files: Mapping[str, bytes | str],
    protected_kinds: Mapping[str, str | ProtectedKind],
    *,
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = (),
    waivers: tuple[OwnerWaiver, ...] = (),
) -> TrustedBaseManifest:
    """Freeze a manifest for already-trusted base files.

    This helper does not establish trust by itself; it merely removes manual
    digest transcription when an owner or test freezes a trusted base.
    """

    normalized = _normalize_files(files)
    protected = tuple(
        ProtectedPath(_posix_path(path), ProtectedKind(kind) if not isinstance(kind, ProtectedKind) else kind)
        for path, kind in sorted(protected_kinds.items())
    )
    digests = []
    for item in protected:
        if item.path not in normalized:
            raise ValueError(f"trusted base is missing protected path {item.path!r}")
        digests.append(BaseDigest(item.path, sha256_bytes(normalized[item.path])))
    return TrustedBaseManifest(protected, tuple(digests), acceptance_criteria, waivers)


def evaluate_integrity(
    base_files: Mapping[str, bytes | str],
    candidate_files: Mapping[str, bytes | str],
    manifest: TrustedBaseManifest,
    *,
    now: str | datetime,
) -> IntegrityReport:
    """Compare protected test/evaluator semantics in base and candidate trees."""

    base = _normalize_files(base_files)
    candidate = _normalize_files(candidate_files)
    observed_at = _parse_time(now, "now")
    diff_hash = protected_diff_hash(base, candidate, manifest)

    violations: list[IntegrityViolation] = []
    violations.extend(_manifest_shape_violations(manifest))
    violations.extend(_trusted_base_violations(base, manifest))
    violations.extend(_oracle_fixture_violations(base, candidate, manifest))

    protected_text_kinds = {ProtectedKind.TEST, ProtectedKind.EVALUATOR}
    semantic_paths = {
        item.path for item in manifest.protected_paths if item.kind in protected_text_kinds
    }
    base_model = _semantic_model(base, semantic_paths)
    candidate_model = _semantic_model(candidate, semantic_paths)
    violations.extend(_syntax_violations(base_model, candidate_model, manifest))
    violations.extend(_compare_suppressions(base, candidate, manifest))
    violations.extend(_compare_assertions(base_model, candidate_model, manifest))
    violations.extend(_compare_cases(base_model, candidate_model, manifest))
    violations.extend(_compare_thresholds(base_model, candidate_model, manifest))
    violations.extend(_compare_exact_comparators(base_model, candidate_model, manifest))
    violations.extend(_compare_command_events(base_model, candidate_model, manifest))

    active, waived, applied, rejected = _apply_waivers(violations, diff_hash, manifest.waivers, observed_at)
    verdict = IntegrityVerdict.PASS if not active else IntegrityVerdict.FAIL
    return IntegrityReport(verdict, diff_hash, tuple(active), tuple(waived), tuple(applied), tuple(rejected))


def protected_diff_hash(
    base_files: Mapping[str, bytes | str],
    candidate_files: Mapping[str, bytes | str],
    manifest: TrustedBaseManifest,
) -> str:
    """Digest the exact unified protected-file diff used for owner waivers."""

    base = _normalize_files(base_files)
    candidate = _normalize_files(candidate_files)
    sections: list[str] = []
    for item in sorted(manifest.protected_paths, key=lambda entry: entry.path):
        path = item.path
        before = base.get(path)
        after = candidate.get(path)
        if before == after:
            continue
        status = "modified"
        if before is None:
            status = "added"
        elif after is None:
            status = "deleted"
        header = f"diff {status} {path}\n"
        try:
            before_lines = (before or b"").decode("utf-8").splitlines(keepends=True)
            after_lines = (after or b"").decode("utf-8").splitlines(keepends=True)
            diff_lines = list(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=f"base/{path}",
                    tofile=f"candidate/{path}",
                    lineterm="",
                )
            )
            body = "\n".join(diff_lines)
        except UnicodeDecodeError:
            body = (
                f"binary diff base_sha256={sha256_bytes(before or b'')} "
                f"candidate_sha256={sha256_bytes(after or b'')}"
            )
        sections.append(header + body)
    return sha256_bytes("\n--\n".join(sections).encode("utf-8"))


def _normalize_files(files: Mapping[str, bytes | str]) -> dict[str, bytes]:
    normalized: dict[str, bytes] = {}
    for path, content in files.items():
        posix = _posix_path(path)
        normalized[posix] = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return normalized


def _posix_path(path: str) -> str:
    return str(path).replace("\\", "/")


def _parse_time(value: str | datetime, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _manifest_shape_violations(manifest: TrustedBaseManifest) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    protected_paths = [item.path for item in manifest.protected_paths]
    digest_paths = [item.path for item in manifest.base_digests]
    if len(protected_paths) != len(set(protected_paths)) or len(digest_paths) != len(set(digest_paths)):
        violations.append(
            IntegrityViolation("invalid-trusted-manifest", "", "protected paths or digest paths contain duplicates")
        )
    if set(protected_paths) != set(digest_paths):
        violations.append(
            IntegrityViolation("invalid-trusted-manifest", "", "protected paths and trusted digest paths differ")
        )
    for item in manifest.base_digests:
        if not re.fullmatch(r"[0-9a-f]{64}", item.sha256):
            violations.append(
                IntegrityViolation("invalid-trusted-manifest", item.path, "trusted base digest is not lowercase sha256")
            )
    return violations


def _trusted_base_violations(base: dict[str, bytes], manifest: TrustedBaseManifest) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    for item in manifest.base_digests:
        content = base.get(item.path)
        if content is None:
            violations.append(IntegrityViolation("untrusted-base", item.path, "trusted base file is missing"))
        elif sha256_bytes(content) != item.sha256:
            violations.append(IntegrityViolation("untrusted-base", item.path, "base file digest differs from trusted manifest"))
    return violations


def _oracle_fixture_violations(
    base: dict[str, bytes], candidate: dict[str, bytes], manifest: TrustedBaseManifest
) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    for item in manifest.protected_paths:
        if item.kind not in {ProtectedKind.ORACLE, ProtectedKind.FIXTURE}:
            continue
        before = base.get(item.path)
        after = candidate.get(item.path)
        if before == after:
            continue
        rule = "changed-oracle-file" if item.kind is ProtectedKind.ORACLE else "changed-fixture-file"
        if before is None:
            message = "protected oracle/fixture file was added"
        elif after is None:
            message = "protected oracle/fixture file was deleted"
        else:
            message = "protected oracle/fixture content changed"
        violations.append(_with_acceptance(manifest, IntegrityViolation(rule, item.path, message), item.path, ""))
    return violations


_SUPPRESSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unittest-skip", re.compile(r"@?\bunittest\.skip(?:If|Unless)?\s*(?:\(|\b)")),
    ("pytest-skip", re.compile(r"@?\bpytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.skip\s*\(")),
    ("js-only", re.compile(r"\b(?:it|test|describe|suite)\.only\s*\(")),
    ("js-skip", re.compile(r"\b(?:it|test|describe|suite)\.skip\s*\(|\b(?:xit|xtest|xdescribe)\s*\(")),
    ("ignore-marker", re.compile(r"@?\bunittest\.ignore\b|@?\bpytest\.mark\.ignore\b|\bignore\s*:\s*true\b", re.IGNORECASE)),
)

_JS_ASSERTION_RE = re.compile(r"\b(?:assert(?:Equal|StrictEqual|DeepEqual)?|expect)\s*\(")
_JS_CASE_RE = re.compile(r"\b(?:it|test)\s*\(")


def _semantic_model(files: dict[str, bytes], paths: set[str]) -> _SemanticModel:
    model = _SemanticModel(
        assertions=defaultdict(list),
        cases=defaultdict(list),
        suppressions=defaultdict(list),
        thresholds=[],
        exact_comparisons=defaultdict(list),
        approximations=[],
        command_events=defaultdict(list),
        syntax_errors=[],
    )
    for path in sorted(paths):
        content = files.get(path)
        if content is None:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            model.syntax_errors.append(_Occurrence("decode", path, 0, "protected file is not UTF-8"))
            continue
        _extract_suppressions(path, text, model)
        if path.endswith(".py"):
            _extract_python_semantics(path, text, model)
        elif path.endswith((".js", ".mjs", ".ts")):
            _extract_javascript_semantics(path, text, model)
    return model


def _extract_suppressions(path: str, text: str, model: _SemanticModel) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        for marker, pattern in _SUPPRESSION_PATTERNS:
            if pattern.search(line):
                occurrence = _Occurrence(marker, path, line_number, line.strip())
                model.suppressions[marker].append(occurrence)


def _extract_python_semantics(path: str, text: str, model: _SemanticModel) -> None:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        model.syntax_errors.append(_Occurrence("syntax", path, exc.lineno or 0, str(exc)))
        return

    source_lines = text.splitlines()
    numeric_values = _numeric_assignments(tree)
    functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name].append(node)

    assertion_nodes = list(_assertion_nodes(tree))
    for key, node in assertion_nodes:
        evidence_text = key + "\n" + _source_line(source_lines, node)
        occurrence = _Occurrence(key, path, getattr(node, "lineno", 0), evidence_text, _constants_in(node))
        model.assertions[key].append(occurrence)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            threshold = _threshold_event(path, node, source_lines, numeric_values)
            if threshold is not None:
                model.thresholds.append(threshold)
            for event in _exact_comparison_events(path, node, source_lines):
                model.exact_comparisons[event.key].append(event)
            if "returncode" in _normalized_dump(node):
                key = "returncode:" + _normalized_dump(node)
                evidence_text = key + "\n" + _source_line(source_lines, node)
                model.command_events[key].append(
                    _Occurrence(key, path, getattr(node, "lineno", 0), evidence_text, _constants_in(node))
                )
        elif isinstance(node, ast.Call):
            exact_assertion = _exact_equality_assertion_event(path, node, source_lines)
            if exact_assertion is not None:
                model.exact_comparisons[exact_assertion.key].append(exact_assertion)
            if _is_approximation_call(node):
                key = "approximation:" + _normalized_dump(node)
                evidence_text = key + "\n" + _source_line(source_lines, node)
                model.approximations.append(
                    _Occurrence(key, path, getattr(node, "lineno", 0), evidence_text, _constants_in(node))
                )
                model.thresholds.extend(_tolerance_events(path, node, source_lines, numeric_values))
            command_key = _command_event_key(node)
            if command_key is not None:
                evidence_text = command_key + "\n" + _source_line(source_lines, node)
                model.command_events[command_key].append(
                    _Occurrence(command_key, path, getattr(node, "lineno", 0), evidence_text, _constants_in(node))
                )
        elif isinstance(node, ast.Raise) and node.exc is not None and "SystemExit" in _normalized_dump(node.exc):
            key = "raise:" + _normalized_dump(node)
            evidence_text = key + "\n" + _source_line(source_lines, node)
            model.command_events[key].append(
                _Occurrence(key, path, getattr(node, "lineno", 0), evidence_text, _constants_in(node))
            )

    for function in [item for items in functions.values() for item in items if item.name.startswith("test")]:
        case = _python_case_signature(path, function, functions, text)
        model.cases[case.key].append(case)


def _extract_javascript_semantics(path: str, text: str, model: _SemanticModel) -> None:
    case_count = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        compact = re.sub(r"\s+", "", line)
        if _JS_ASSERTION_RE.search(line):
            key = "js-assertion:" + re.sub(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b", "_", compact)
            model.assertions[key].append(_Occurrence(key, path, line_number, line.strip()))
        if _JS_CASE_RE.search(line):
            case_count += 1
    for index in range(case_count):
        key = "js-case-presence"
        model.cases[key].append(_Occurrence(key, path, 0, f"javascript case {index + 1}"))


def _assertion_nodes(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    result: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            result.append(("assert:" + _normalized_dump(node.test), node))
        elif isinstance(node, ast.Call) and _is_assertion_call(node):
            result.append(("assert-call:" + _normalized_dump(node), node))
    return result


def _is_assertion_call(node: ast.Call) -> bool:
    chain = _call_chain(node.func)
    if not chain:
        return False
    last = chain[-1]
    unittest_style = last.startswith("assert") and len(last) > 6 and last[6].isupper()
    return unittest_style or last in {"fail", "raises"}


def _python_case_signature(
    path: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]],
    source: str,
) -> _Occurrence:
    assertions: Counter[str] = Counter()
    constants: Counter[str] = Counter()
    visited: set[int] = set()

    def collect(current: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if id(current) in visited:
            return
        visited.add(id(current))
        for node in _walk_function_body(current):
            if isinstance(node, ast.Assert):
                assertions["assert:" + _normalized_dump(node.test)] += 1
                constants.update(_constants_in(node))
            elif isinstance(node, ast.Call):
                if _is_assertion_call(node):
                    assertions["assert-call:" + _normalized_dump(node)] += 1
                constants.update(_constants_in(node))
                for target in _called_function_definitions(node, functions):
                    if target is not current:
                        collect(target)
            elif isinstance(node, ast.Constant):
                constants[repr(node.value)] += 1

    collect(function)
    assertion_items = sorted((key, count) for key, count in assertions.items())
    constant_items = sorted((key, count) for key, count in constants.items())
    signature = sha256_json({"assertions": assertion_items, "constants": constant_items})
    text = "\n".join([key for key, _count in assertion_items] + [key for key, _count in constant_items])
    text += "\n" + (ast.get_source_segment(source, function) or "")
    return _Occurrence(signature, path, function.lineno, text)


def _walk_function_body(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    result: list[ast.AST] = []
    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        result.append(node)
        if node is not function and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return result


def _called_function_definitions(
    node: ast.Call,
    functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]],
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    chain = _call_chain(node.func)
    if not chain:
        return []
    return list(functions.get(chain[-1], ()))


def _threshold_event(
    path: str,
    node: ast.Compare,
    source_lines: list[str],
    numeric_values: dict[str, float],
) -> _ThresholdOccurrence | None:
    if len(node.ops) != 1 or len(node.comparators) != 1:
        return None
    left_number = _numeric_constant(node.left, numeric_values)
    right_number = _numeric_constant(node.comparators[0], numeric_values)
    if (left_number is None) == (right_number is None):
        return None
    op = type(node.ops[0])
    if right_number is not None:
        subject = _normalized_dump(node.left)
        value = right_number
        if op in {ast.Lt, ast.LtE}:
            direction = "upper"
        elif op in {ast.Gt, ast.GtE}:
            direction = "lower"
        else:
            return None
        inclusive = op in {ast.LtE, ast.GtE}
    else:
        subject = _normalized_dump(node.comparators[0])
        value = left_number if left_number is not None else 0.0
        if op in {ast.Lt, ast.LtE}:
            direction = "lower"
        elif op in {ast.Gt, ast.GtE}:
            direction = "upper"
        else:
            return None
        inclusive = op in {ast.LtE, ast.GtE}
    key_text = (
        f"{subject} {direction} {value} {'inclusive' if inclusive else 'exclusive'}\n"
        f"{_source_line(source_lines, node)}"
    )
    return _ThresholdOccurrence(
        subject,
        direction,
        float(value),
        inclusive,
        path,
        getattr(node, "lineno", 0),
        key_text,
        _constants_in(node),
    )


def _numeric_assignments(tree: ast.AST) -> dict[str, float]:
    values: dict[str, list[float]] = defaultdict(list)
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value_node = node.value
        if value_node is None:
            continue
        value = _numeric_literal(value_node)
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id].append(value)
    return {name: items[0] for name, items in values.items() if len(items) == 1 and name.isupper()}


def _numeric_literal(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _numeric_literal(node.operand)
        return -value if value is not None else None
    return None


def _numeric_constant(node: ast.AST, numeric_values: dict[str, float]) -> float | None:
    literal = _numeric_literal(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name):
        return numeric_values.get(node.id)
    return None


def _tolerance_events(
    path: str,
    node: ast.Call,
    source_lines: list[str],
    numeric_values: dict[str, float],
) -> list[_ThresholdOccurrence]:
    if len(node.args) < 2:
        return []
    events: list[_ThresholdOccurrence] = []
    subject = "tolerance:" + _normalized_dump((node.args[0], node.args[1]))
    for keyword in node.keywords:
        if keyword.arg not in {"abs_tol", "rel_tol", "tol", "tolerance", "delta"}:
            continue
        value = _numeric_constant(keyword.value, numeric_values)
        if value is None:
            continue
        text = f"{subject} upper {value} inclusive\n{_source_line(source_lines, node)}"
        events.append(
            _ThresholdOccurrence(
                subject,
                "upper",
                float(value),
                True,
                path,
                getattr(node, "lineno", 0),
                text,
                _constants_in(node),
            )
        )
    return events


def _exact_comparison_events(path: str, node: ast.Compare, source_lines: list[str]) -> list[_Occurrence]:
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return []
    key = "exact:" + _normalized_dump(node)
    evidence_text = key + "\n" + _source_line(source_lines, node)
    return [_Occurrence(key, path, getattr(node, "lineno", 0), evidence_text, _constants_in(node))]


def _exact_equality_assertion_event(path: str, node: ast.Call, source_lines: list[str]) -> _Occurrence | None:
    chain = _call_chain(node.func)
    if not chain or chain[-1] != "assertEqual" or len(node.args) < 2:
        return None
    key = "exact-call:" + _normalized_dump((node.args[0], node.args[1]))
    evidence_text = key + "\n" + _source_line(source_lines, node)
    return _Occurrence(key, path, getattr(node, "lineno", 0), evidence_text, _constants_in(node))


def _is_approximation_call(node: ast.Call) -> bool:
    chain = _call_chain(node.func)
    if not chain:
        return False
    last = chain[-1]
    return last in {"isclose", "approx", "assertAlmostEqual", "assertAlmostEquals"} or last.startswith("assertAlmost")


def _command_event_key(node: ast.Call) -> str | None:
    chain = _call_chain(node.func)
    if not chain:
        return None
    last = chain[-1]
    command_call = (
        last in {"check_call", "check_output", "Popen"}
        or (last in {"run", "call"} and "subprocess" in chain)
        or chain[-2:] == ["os", "system"]
        or chain[-2:] == ["sys", "exit"]
        or last == "exit"
    )
    if command_call:
        return "command:" + _normalized_dump(node)
    return None


def _call_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        return []
    else:
        return []
    return list(reversed(parts))


def _source_line(lines: list[str], node: ast.AST) -> str:
    line_number = getattr(node, "lineno", 0)
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1].strip()
    return ""


def _constants_in(node: ast.AST) -> tuple[str, ...]:
    constants = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant):
            constants.append(repr(child.value))
    return tuple(sorted(constants))


def _normalized_dump(node: Any) -> str:
    """Alpha-normalize one AST fragment so consistent renames do not matter."""

    return _normalized_dump_with_names(node, {})


def _normalized_dump_with_names(node: Any, names: dict[str, str]) -> str:
    if isinstance(node, ast.AST):
        parts = []
        for field, value in ast.iter_fields(node):
            if field in {"lineno", "col_offset", "end_lineno", "end_col_offset"}:
                continue
            if isinstance(node, (ast.Name, ast.arg)) and field in {"id", "arg"} and isinstance(value, str):
                value = names.setdefault(value, f"_v{len(names) + 1}")
                parts.append(f"{field}={value}")
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and field == "name":
                value = "_"
            parts.append(f"{field}={_normalized_dump_with_names(value, names)}")
        return f"{type(node).__name__}({','.join(parts)})"
    if isinstance(node, (list, tuple)):
        return "[" + ",".join(_normalized_dump_with_names(item, names) for item in node) + "]"
    return repr(node)


def _syntax_violations(
    base_model: _SemanticModel, candidate_model: _SemanticModel, manifest: TrustedBaseManifest
) -> list[IntegrityViolation]:
    del base_model
    violations = []
    for occurrence in candidate_model.syntax_errors:
        violations.append(
            _with_acceptance(
                manifest,
                IntegrityViolation(
                    "unparseable-protected-file",
                    occurrence.path,
                    f"protected candidate file cannot be parsed: {occurrence.text}",
                    occurrence.line,
                ),
                occurrence.path,
                occurrence.text,
            )
        )
    return violations


def _compare_suppressions(
    base: dict[str, bytes], candidate: dict[str, bytes], manifest: TrustedBaseManifest
) -> list[IntegrityViolation]:
    """Flag suppression markers present in added diff lines after move pairing.

    Exact normalized removed lines are paired first, so renaming a file or moving
    an already-skipped test does not itself count as a new suppression.  A truly
    added skip/only/ignore marker, or one whose arguments changed, remains flagged.
    """

    added: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    occurrences: dict[str, _Occurrence] = {}
    protected_text_kinds = {ProtectedKind.TEST, ProtectedKind.EVALUATOR}
    for item in sorted(manifest.protected_paths, key=lambda entry: entry.path):
        if item.kind not in protected_text_kinds:
            continue
        before = base.get(item.path)
        after = candidate.get(item.path)
        if before == after:
            continue
        try:
            before_lines = (before or b"").decode("utf-8").splitlines()
            after_lines = (after or b"").decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for diff_line in difflib.ndiff(before_lines, after_lines):
            if diff_line.startswith("+ "):
                line = diff_line[2:]
                for marker, key in _suppression_keys_in_line(line):
                    added[key] += 1
                    occurrences.setdefault(key, _Occurrence(marker, item.path, _line_number(after_lines, line), line.strip()))
            elif diff_line.startswith("- "):
                for _marker, key in _suppression_keys_in_line(diff_line[2:]):
                    removed[key] += 1

    violations: list[IntegrityViolation] = []
    for key, count in sorted((added - removed).items()):
        occurrence = occurrences[key]
        violations.append(
            _with_acceptance(
                manifest,
                IntegrityViolation(
                    "added-skip-only-ignore",
                    occurrence.path,
                    f"candidate adds {count} {occurrence.key} suppression/selection marker(s)",
                    occurrence.line,
                ),
                occurrence.path,
                occurrence.text,
            )
        )
    return violations


def _suppression_keys_in_line(line: str) -> list[tuple[str, str]]:
    compact = re.sub(r"\s+", "", line)
    return [(marker, marker + ":" + compact) for marker, pattern in _SUPPRESSION_PATTERNS if pattern.search(line)]


def _line_number(lines: list[str], needle: str) -> int:
    for index, line in enumerate(lines, start=1):
        if line == needle:
            return index
    return 0


def _compare_assertions(
    base_model: _SemanticModel, candidate_model: _SemanticModel, manifest: TrustedBaseManifest
) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    for key, base_items in sorted(base_model.assertions.items()):
        missing = len(base_items) - len(candidate_model.assertions.get(key, ()))
        if missing <= 0:
            continue
        occurrence = base_items[0]
        violations.append(
            _with_acceptance(
                manifest,
                IntegrityViolation(
                    "deleted-assertion",
                    occurrence.path,
                    f"candidate removes {missing} occurrence(s) of a base assertion",
                    occurrence.line,
                ),
                occurrence.path,
                occurrence.text,
            )
        )
    return violations


def _compare_cases(
    base_model: _SemanticModel, candidate_model: _SemanticModel, manifest: TrustedBaseManifest
) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    for key, base_items in sorted(base_model.cases.items()):
        missing = len(base_items) - len(candidate_model.cases.get(key, ()))
        if missing <= 0:
            continue
        occurrence = base_items[0]
        violations.append(
            _with_acceptance(
                manifest,
                IntegrityViolation(
                    "omitted-case",
                    occurrence.path,
                    f"candidate omits {missing} semantic test case(s) present in the trusted base",
                    occurrence.line,
                ),
                occurrence.path,
                occurrence.text,
            )
        )
    return violations


def _compare_thresholds(
    base_model: _SemanticModel, candidate_model: _SemanticModel, manifest: TrustedBaseManifest
) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    candidate_counter = Counter(_threshold_key(item) for item in candidate_model.thresholds)
    for base_item in base_model.thresholds:
        key = _threshold_key(base_item)
        if candidate_counter[key] > 0:
            candidate_counter[key] -= 1
            continue
        alternatives = [
            item
            for item in candidate_model.thresholds
            if item.subject == base_item.subject and item.direction == base_item.direction
        ]
        weakened = any(_threshold_is_weaker(base_item, item) for item in alternatives)
        if weakened or not alternatives:
            message = (
                "numeric threshold was weakened" if weakened else "numeric threshold was removed without replacement"
            )
            violations.append(
                _with_acceptance(
                    manifest,
                    IntegrityViolation(
                        "weakened-threshold",
                        base_item.path,
                        f"{message}: {base_item.text}",
                        base_item.line,
                    ),
                    base_item.path,
                    base_item.text,
                )
            )
    return violations


def _threshold_key(item: _ThresholdOccurrence) -> tuple[str, str, float, bool]:
    return item.subject, item.direction, item.value, item.inclusive


def _threshold_is_weaker(before: _ThresholdOccurrence, after: _ThresholdOccurrence) -> bool:
    if before.direction == "upper":
        return after.value > before.value or (after.value == before.value and after.inclusive and not before.inclusive)
    return after.value < before.value or (after.value == before.value and after.inclusive and not before.inclusive)


def _compare_exact_comparators(
    base_model: _SemanticModel, candidate_model: _SemanticModel, manifest: TrustedBaseManifest
) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    for key, base_items in sorted(base_model.exact_comparisons.items()):
        if candidate_model.exact_comparisons.get(key):
            continue
        base_item = base_items[0]
        for approximation in candidate_model.approximations:
            if _comparison_events_related(base_item, approximation):
                violations.append(
                    _with_acceptance(
                        manifest,
                        IntegrityViolation(
                            "softened-comparator",
                            base_item.path,
                            "exact equality comparison was replaced by approximate comparison",
                            base_item.line,
                        ),
                        base_item.path,
                        base_item.text,
                    )
                )
                break
    return violations


def _comparison_events_related(exact: _Occurrence, approximation: _Occurrence) -> bool:
    if exact.path == approximation.path and exact.line == approximation.line:
        return True
    if exact.constants and set(exact.constants) & set(approximation.constants):
        return True
    return not exact.constants and not approximation.constants


def _compare_command_events(
    base_model: _SemanticModel, candidate_model: _SemanticModel, manifest: TrustedBaseManifest
) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    for key, base_items in sorted(base_model.command_events.items()):
        missing = len(base_items) - len(candidate_model.command_events.get(key, ()))
        if missing <= 0:
            continue
        occurrence = base_items[0]
        violations.append(
            _with_acceptance(
                manifest,
                IntegrityViolation(
                    "altered-command-or-exit-handling",
                    occurrence.path,
                    f"candidate removes or alters {missing} command/exit handling event(s)",
                    occurrence.line,
                ),
                occurrence.path,
                occurrence.text,
            )
        )
    return violations


def _with_acceptance(
    manifest: TrustedBaseManifest,
    violation: IntegrityViolation,
    path: str,
    evidence_text: str,
) -> IntegrityViolation:
    criterion = _acceptance_criterion_for(manifest, path, evidence_text)
    if criterion is None or violation.acceptance_criterion is not None:
        return violation
    return IntegrityViolation(
        violation.rule_id,
        violation.path,
        violation.message,
        violation.line,
        criterion,
    )


def _acceptance_criterion_for(manifest: TrustedBaseManifest, path: str, evidence_text: str) -> str | None:
    for criterion in manifest.acceptance_criteria:
        if criterion.path != path:
            continue
        if not criterion.token or criterion.token in evidence_text:
            return criterion.criterion_id
    return None


def _apply_waivers(
    violations: list[IntegrityViolation],
    diff_hash: str,
    waivers: tuple[OwnerWaiver, ...],
    observed_at: datetime,
) -> tuple[list[IntegrityViolation], list[IntegrityViolation], list[OwnerWaiver], list[WaiverRejection]]:
    active: list[IntegrityViolation] = []
    waived: list[IntegrityViolation] = []
    applied: list[OwnerWaiver] = []
    rejected: list[WaiverRejection] = []
    for violation in violations:
        matching = [item for item in waivers if item.diff_hash == diff_hash and item.rule_id == violation.rule_id]
        accepted_waiver: OwnerWaiver | None = None
        rejection_reason: str | None = None
        if violation.rule_id in UNWAIVABLE_RULES:
            rejection_reason = "trust-oracle-rule-not-waivable" if matching else None
        elif violation.acceptance_criterion:
            rejection_reason = "acceptance-criterion-not-waivable" if matching else None
        elif matching:
            waiver = matching[0]
            rejection_reason = _waiver_invalid_reason(waiver, observed_at)
            if rejection_reason is None:
                accepted_waiver = waiver
        if accepted_waiver is not None:
            waived.append(violation)
            if accepted_waiver not in applied:
                applied.append(accepted_waiver)
        else:
            active.append(violation)
            if matching and rejection_reason is not None:
                rejection = WaiverRejection(diff_hash, violation.rule_id, matching[0].owner, rejection_reason)
                if rejection not in rejected:
                    rejected.append(rejection)
    return active, waived, applied, rejected


def _waiver_invalid_reason(waiver: OwnerWaiver, observed_at: datetime) -> str | None:
    if not waiver.owner or not waiver.rationale or not waiver.replacement_evidence:
        return "waiver-missing-owner-rationale-or-replacement-evidence"
    if not re.fullmatch(r"[0-9a-f]{64}", waiver.diff_hash):
        return "waiver-diff-hash-not-sha256"
    try:
        expires_at = _parse_time(waiver.expires_at, "waiver expires_at")
    except ValueError:
        return "waiver-expiry-invalid"
    if expires_at <= observed_at:
        return "waiver-expired"
    return None
