"""Red-proof runner for evaluator, gate and product repairs.

The runner executes trusted old/new gate programs in four clean disposable
snapshots: counterexample under incumbent, counterexample under candidate, good
control under incumbent, and good control under candidate.  It is shadow-mode
infrastructure only: it makes no model calls and never mutates the repository.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from .digests import directory_file_map, file_map_digest, sha256_bytes, sha256_json

PROOF_SCHEMA_VERSION = 1


class ProofMode(str, Enum):
    """The two red-proof shapes described by ADR 0005."""

    EVALUATOR_REPAIR = "evaluator-repair"
    PRODUCT_FIX = "product-fix"


class ProofVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class RedProofSpec:
    """Frozen inputs for one repair proof.

    ``expected_diagnostic`` is a regular expression matched with
    :func:`re.fullmatch` against the structured diagnostic emitted for the
    counterexample.  ``actual_tree_digest`` must equal ``bound_tree_digest``;
    otherwise the run may be diagnostic but can never close the finding.
    """

    finding_id: str
    bound_tree_digest: str
    actual_tree_digest: str
    mode: ProofMode
    expected_diagnostic: str
    old_gate: Path
    new_gate: Path
    counterexample: Path
    control: Path
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class GateRun:
    """Objective facts from one disposable-snapshot gate execution."""

    label: str
    argv: tuple[str, ...]
    exit_code: int | None
    verdict: str | None
    diagnostic: str | None
    stdout_sha256: str | None
    stderr_sha256: str | None
    input_sha256: str | None
    gate_sha256: str | None
    snapshot_tree_sha256: str | None
    timed_out: bool = False
    infrastructure_error: str | None = None


@dataclass(frozen=True)
class ProofTree:
    """A tree digest bound to a named role in the proof."""

    role: str
    sha256: str


@dataclass(frozen=True)
class ProofRecord:
    """Typed, content-addressed outcome of a red proof."""

    schema_version: int
    finding_id: str
    mode: ProofMode
    bound_tree_digest: str
    actual_tree_digest: str
    trees: tuple[ProofTree, ...]
    expected_diagnostic: str
    verdict: ProofVerdict
    reasons: tuple[str, ...]
    runs: tuple[GateRun, ...]
    proof_digest: str

    def payload(self) -> dict[str, object]:
        """Return the canonical content addressed by ``proof_digest``."""

        return json.loads(
            json.dumps(
                {
                    "schema_version": self.schema_version,
                    "finding_id": self.finding_id,
                    "mode": self.mode.value,
                    "bound_tree_digest": self.bound_tree_digest,
                    "actual_tree_digest": self.actual_tree_digest,
                    "trees": [{"role": tree.role, "sha256": tree.sha256} for tree in self.trees],
                    "expected_diagnostic": self.expected_diagnostic,
                    "verdict": self.verdict.value,
                    "reasons": list(self.reasons),
                    "runs": [run_to_dict(run) for run in self.runs],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )

    def to_dict(self) -> dict[str, object]:
        result = self.payload()
        result["proof_digest"] = self.proof_digest
        return result


def run_to_dict(run: GateRun) -> dict[str, object]:
    return {
        "label": run.label,
        "argv": list(run.argv),
        "exit_code": run.exit_code,
        "verdict": run.verdict,
        "diagnostic": run.diagnostic,
        "stdout_sha256": run.stdout_sha256,
        "stderr_sha256": run.stderr_sha256,
        "input_sha256": run.input_sha256,
        "gate_sha256": run.gate_sha256,
        "snapshot_tree_sha256": run.snapshot_tree_sha256,
        "timed_out": run.timed_out,
        "infrastructure_error": run.infrastructure_error,
    }


def run_red_proof(spec: RedProofSpec) -> ProofRecord:
    """Execute and assess a frozen red-proof specification."""

    if not spec.finding_id:
        raise ValueError("red proof requires a finding id")
    if not spec.bound_tree_digest or not spec.actual_tree_digest:
        raise ValueError("red proof requires bound and actual tree digests")
    if spec.timeout_seconds <= 0:
        raise ValueError("red proof timeout must be positive")
    try:
        re.compile(spec.expected_diagnostic)
    except re.error as exc:
        raise ValueError(f"invalid expected diagnostic regex: {exc}") from exc
    try:
        spec = replace(spec, mode=ProofMode(spec.mode))
    except ValueError as exc:
        raise ValueError(f"unsupported proof mode: {spec.mode!r}") from exc

    runs = (
        _execute_gate("old-counterexample", spec.old_gate, spec.counterexample, spec.timeout_seconds),
        _execute_gate("new-counterexample", spec.new_gate, spec.counterexample, spec.timeout_seconds),
        _execute_gate("old-control", spec.old_gate, spec.control, spec.timeout_seconds),
        _execute_gate("new-control", spec.new_gate, spec.control, spec.timeout_seconds),
    )
    verdict, reasons = _assess(spec, runs)
    return _make_record(spec, verdict, reasons, runs)


def write_proof_record(record: ProofRecord, directory: Path) -> Path:
    """Write a proof artifact under its content address and return the path."""

    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{record.proof_digest}.json"
    encoded = json.dumps(record.to_dict(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
        handle.write(encoded)
        temporary = Path(handle.name)
    try:
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def proof_digest_matches(record: ProofRecord) -> bool:
    """Verify that a proof record is addressed by its exact canonical content."""

    return record.proof_digest == sha256_json(record.payload())


def _make_record(
    spec: RedProofSpec,
    verdict: ProofVerdict,
    reasons: tuple[str, ...],
    runs: tuple[GateRun, ...],
) -> ProofRecord:
    trees = _tree_digests(spec, runs)
    record = ProofRecord(
        schema_version=PROOF_SCHEMA_VERSION,
        finding_id=spec.finding_id,
        mode=spec.mode,
        bound_tree_digest=spec.bound_tree_digest,
        actual_tree_digest=spec.actual_tree_digest,
        trees=trees,
        expected_diagnostic=spec.expected_diagnostic,
        verdict=verdict,
        reasons=reasons,
        runs=runs,
        proof_digest="",
    )
    return replace(record, proof_digest=sha256_json(record.payload()))


def _tree_digests(spec: RedProofSpec, runs: tuple[GateRun, ...]) -> tuple[ProofTree, ...]:
    entries = [ProofTree("bound", spec.bound_tree_digest), ProofTree("actual", spec.actual_tree_digest)]
    seen = {(entry.role, entry.sha256) for entry in entries}
    for run in runs:
        if run.snapshot_tree_sha256 is None:
            continue
        entry = ProofTree(run.label, run.snapshot_tree_sha256)
        if (entry.role, entry.sha256) not in seen:
            entries.append(entry)
            seen.add((entry.role, entry.sha256))
    return tuple(entries)


def _execute_gate(label: str, gate_path: Path, input_dir: Path, timeout_seconds: float) -> GateRun:
    argv = (sys.executable, "gate.py", "input")
    try:
        gate_bytes = gate_path.read_bytes()
        input_map = directory_file_map(input_dir)
    except (OSError, ValueError) as exc:
        return GateRun(
            label=label,
            argv=argv,
            exit_code=None,
            verdict=None,
            diagnostic=None,
            stdout_sha256=None,
            stderr_sha256=None,
            input_sha256=None,
            gate_sha256=None,
            snapshot_tree_sha256=None,
            infrastructure_error=f"input-preparation-failed:{exc}",
        )

    input_digest = file_map_digest(input_map)
    gate_digest = sha256_bytes(gate_bytes)
    with tempfile.TemporaryDirectory(prefix="khitmee-redproof-") as temporary:
        work = Path(temporary)
        input_root = work / "input"
        input_root.mkdir()
        try:
            for relative, content in input_map.items():
                destination = input_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            (work / "gate.py").write_bytes(gate_bytes)
        except OSError as exc:
            return GateRun(
                label=label,
                argv=argv,
                exit_code=None,
                verdict=None,
                diagnostic=None,
                stdout_sha256=None,
                stderr_sha256=None,
                input_sha256=input_digest,
                gate_sha256=gate_digest,
                snapshot_tree_sha256=None,
                infrastructure_error=f"snapshot-preparation-failed:{exc}",
            )

        stdout = b""
        stderr = b""
        exit_code: int | None = None
        timed_out = False
        infrastructure_error: str | None = None
        try:
            completed = subprocess.run(
                list(argv),
                cwd=work,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            infrastructure_error = "timeout"
        except OSError as exc:
            infrastructure_error = f"gate-spawn-failed:{exc}"

        verdict: str | None = None
        diagnostic: str | None = None
        if infrastructure_error is None:
            verdict, diagnostic, infrastructure_error = _parse_gate_protocol(stdout, exit_code)

        try:
            snapshot_digest = file_map_digest(directory_file_map(work))
        except (OSError, ValueError) as exc:
            snapshot_digest = None
            if infrastructure_error is None:
                infrastructure_error = f"snapshot-digest-failed:{exc}"

        return GateRun(
            label=label,
            argv=argv,
            exit_code=exit_code,
            verdict=verdict,
            diagnostic=diagnostic,
            stdout_sha256=sha256_bytes(stdout),
            stderr_sha256=sha256_bytes(stderr),
            input_sha256=input_digest,
            gate_sha256=gate_digest,
            snapshot_tree_sha256=snapshot_digest,
            timed_out=timed_out,
            infrastructure_error=infrastructure_error,
        )


def _parse_gate_protocol(stdout: bytes, exit_code: int | None) -> tuple[str | None, str | None, str | None]:
    if exit_code is None:
        return None, None, "missing-exit-code"
    try:
        decoded = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "gate-output-not-utf8"
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        return None, None, "invalid-gate-output"
    if not isinstance(payload, dict):
        return None, None, "gate-output-not-object"
    if "verdict" not in payload or "diagnostic" not in payload:
        return None, None, "missing-gate-field"
    verdict = payload["verdict"]
    diagnostic = payload["diagnostic"]
    if verdict not in {"GO", "NO-GO"} or not isinstance(diagnostic, str):
        return None, None, "invalid-gate-field"
    if exit_code == 0 and verdict != "GO":
        return None, None, "contradictory-gate-exit"
    if exit_code != 0 and verdict != "NO-GO":
        return None, None, "contradictory-gate-exit"
    if verdict == "NO-GO" and not diagnostic:
        return None, None, "missing-gate-diagnostic"
    return verdict, diagnostic, None


def _assess(spec: RedProofSpec, runs: tuple[GateRun, ...]) -> tuple[ProofVerdict, tuple[str, ...]]:
    infrastructure = tuple(f"{run.label}:{run.infrastructure_error}" for run in runs if run.infrastructure_error)
    if infrastructure:
        return ProofVerdict.INCONCLUSIVE, infrastructure

    by_label = {run.label: run for run in runs}
    old_bad = by_label["old-counterexample"]
    new_bad = by_label["new-counterexample"]
    old_control = by_label["old-control"]
    new_control = by_label["new-control"]
    reasons: list[str] = []

    if spec.actual_tree_digest != spec.bound_tree_digest:
        reasons.append("stale-tree")
        return ProofVerdict.INCONCLUSIVE, tuple(reasons)

    controls_accepted = _accepted(old_control) and _accepted(new_control)
    if not controls_accepted:
        reasons.append("good-control-rejected")

    diagnostic_matches = bool(
        new_bad.diagnostic is not None and re.fullmatch(spec.expected_diagnostic, new_bad.diagnostic)
    )
    if spec.mode == ProofMode.EVALUATOR_REPAIR:
        if not _accepted(old_bad):
            reasons.append("counterexample-not-false-go")
        if not _rejected(new_bad):
            reasons.append("repaired-gate-did-not-reject")
        elif not diagnostic_matches:
            reasons.append("diagnostic-mismatch")
    elif spec.mode == ProofMode.PRODUCT_FIX:
        incumbent_diagnostic_matches = bool(
            old_bad.diagnostic is not None and re.fullmatch(spec.expected_diagnostic, old_bad.diagnostic)
        )
        if not _rejected(old_bad) or not incumbent_diagnostic_matches:
            reasons.append("incumbent-defect-not-reproduced")
        if not _accepted(new_bad):
            reasons.append("candidate-did-not-satisfy-oracle")
    else:
        raise ValueError(f"unsupported proof mode: {spec.mode!r}")

    if reasons:
        return ProofVerdict.FAIL, tuple(reasons)
    return ProofVerdict.PASS, ()


def _accepted(run: GateRun) -> bool:
    return run.infrastructure_error is None and run.exit_code == 0 and run.verdict == "GO"


def _rejected(run: GateRun) -> bool:
    return run.infrastructure_error is None and run.exit_code not in (None, 0) and run.verdict == "NO-GO"
