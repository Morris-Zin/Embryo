#!/usr/bin/env python3
"""Fail-closed separation of workload success from 2 GiB qualification."""

import argparse
import json
import pathlib
import sys

REQUIRED_ORACLE_CHECKS = {
    'numbering', 'voucher_rows', 'stock_ledger', 'gl_entries', 'bins',
    'reports', 'retries', 'csv', 'perf_parity_require_db',
}


def acceptance_fixture_sha256():
    try:
        data = json.loads((pathlib.Path(__file__).resolve().with_name('acceptance-fixture.json')).read_text(encoding='utf-8'))
        return str(data.get('sha256') or '')
    except Exception:
        return ''


PINNED_ACCEPTANCE_FIXTURE_SHA256 = acceptance_fixture_sha256()


def load_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except Exception:
        return {}


def rows_by_service(proof):
    return {row.get('service'): row for row in proof.get('services') or [] if row.get('service')}


def resource_window_comparison(before, after):
    """Compare service identity/restart/OOM state across the measured window."""
    before_rows = rows_by_service(before or {})
    after_rows = rows_by_service(after or {})
    problems = []
    for service in sorted(set(before_rows) | set(after_rows)):
        old = before_rows.get(service)
        new = after_rows.get(service)
        if old is None or new is None:
            problems.append(f'{service}: missing in one resource proof')
            continue
        if old.get('id') != new.get('id'):
            problems.append(f'{service}: container identity changed {old.get("id")} -> {new.get("id")}')
        old_restart = old.get('restart_count')
        new_restart = new.get('restart_count')
        if old_restart is None or new_restart is None:
            problems.append(f'{service}: restart counter evidence missing in one resource proof')
        else:
            try:
                restart_changed = int(old_restart) != int(new_restart)
            except (TypeError, ValueError):
                restart_changed = True
                problems.append(f'{service}: restart counter evidence is not numeric')
            if restart_changed:
                problems.append(f'{service}: restart count changed {old_restart} -> {new_restart}')
        for key, label in (('started_at', 'start timestamp'), ('pid', 'process id')):
            old_value = old.get(key)
            new_value = new.get(key)
            if old_value in (None, '') or new_value in (None, ''):
                problems.append(f'{service}: {label} evidence missing in one resource proof')
            elif key == 'pid':
                try:
                    changed = int(old_value) != int(new_value)
                except (TypeError, ValueError):
                    changed = True
                    problems.append(f'{service}: process id evidence is not numeric')
                if changed:
                    problems.append(f'{service}: process id changed {old_value} -> {new_value}')
            elif old_value != new_value:
                problems.append(f'{service}: start timestamp changed {old_value} -> {new_value}')
        if bool(old.get('oom_killed')) != bool(new.get('oom_killed')):
            problems.append(f'{service}: OOM state changed {old.get("oom_killed")} -> {new.get("oom_killed")}')
    return {'passed': not problems, 'problems': problems}


def oracle_evidence_ok(evidence, run_id):
    if evidence.get('schema_version') != 'seasonal-stock-oracle-v1':
        return False, 'missing seasonal-stock-oracle-v1 evidence'
    if evidence.get('run_id') != run_id:
        return False, 'oracle evidence run_id does not match this qualification run'
    if evidence.get('passed') is not True:
        return False, 'oracle evidence did not pass'
    checks = evidence.get('checks') or {}
    missing = sorted(key for key in REQUIRED_ORACLE_CHECKS if checks.get(key) is not True)
    if missing:
        return False, 'oracle checks missing/failing: ' + ','.join(missing)
    return True, ''


def acceptance_config_ok(k6_config):
    phases = k6_config.get('phases') or []
    baseline = next((row for row in phases if row.get('phase') == 'baseline' or row.get('name') == 'baseline'), {})
    problems = []
    if k6_config.get('profile') != 'acceptance':
        problems.append('k6 config profile is not acceptance')
    if int(baseline.get('duration_seconds') or 0) < 3600:
        problems.append('acceptance baseline duration is below one full hour')
    if k6_config.get('managers_enabled') is not True:
        problems.append('manager traffic is not enabled in k6 config')
    return not problems, problems


def decide(artifact_dir, workload_exit, run_id, seasonal_profile, preflight=False):
    artifact = pathlib.Path(artifact_dir)
    verification = load_json(artifact / 'seasonal-verification.json')
    before = load_json(artifact / 'resource-proof-before.json')
    after = load_json(artifact / 'resource-proof-after.json')
    foreign_start = load_json(artifact / 'foreign-load-start.json')
    foreign_end = load_json(artifact / 'foreign-load-end.json')
    candidate = load_json(artifact / 'candidate-deployment.json')
    migration = load_json(artifact / 'candidate-migration.json')
    candidate_apps = load_json(artifact / 'candidate-apps.json')
    guest_before = load_json(artifact / 'guest-attestation-before.json')
    guest_after = load_json(artifact / 'guest-attestation-after.json')
    oracle = load_json(artifact / 'oracle-evidence.json')
    fixture_restore = load_json(artifact / 'fixture-restore.json')
    load_generator = load_json(artifact / 'load-generator-attestation.json')
    attribution = load_json(artifact / 'attribution' / 'attribution-summary.json')
    environment = load_json(artifact / 'environment-attestation.json')
    k6_log = artifact / 'k6.log'

    # Parse only the public config event; requiring the whole file keeps a missing
    # copied log fail-closed without depending on k6 internals here.
    k6_config = {}
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import verify_run
        k6_config = verify_run.parse_events(k6_log).get('config') or {}
    except Exception:
        k6_config = {}

    workload_passed = workload_exit == 0
    foreign_counts = [foreign_start.get('foreign_count', -1), foreign_end.get('foreign_count', -1)]
    foreign_invalid = any(count != 0 for count in foreign_counts)
    lease_renewal_failed = (artifact / 'lease-renew.failed').exists()
    timing_valid = not (foreign_invalid or lease_renewal_failed)

    try:
        resource_after_exit_ok = (artifact / 'resource-proof-after.exit').read_text(encoding='utf-8').strip() == '0'
    except Exception:
        resource_after_exit_ok = False
    resource_measurement_ok = before.get('measurement_passed') is True and after.get('measurement_passed') is True and resource_after_exit_ok
    resource_budget_ok = before.get('within_budget') is True and after.get('within_budget') is True
    window = resource_window_comparison(before, after)
    resource_window_ok = resource_measurement_ok and resource_budget_ok and window['passed']

    guest_ok = guest_before.get('passed') is True and guest_after.get('passed') is True
    guest_stable = all(
        guest_before.get(key) == guest_after.get(key)
        for key in ('cpuset_cpus_effective', 'cpu_max', 'memory_max_bytes', 'memory_swap_max', 'online_cpu_count', 'nproc_count', 'guest_mem_total_bytes', 'guest_swap_total_bytes')
    ) if guest_before and guest_after else False
    guest_attestation_ok = guest_ok and guest_stable

    try:
        oracle_command_ok = (artifact / 'oracle-command.exit').read_text(encoding='utf-8').strip() == '0'
    except Exception:
        oracle_command_ok = False
    oracle_ok, oracle_problem = oracle_evidence_ok(oracle, run_id)
    if not oracle_command_ok:
        oracle_ok = False
        oracle_problem = 'paired oracle command missing or exited nonzero'
    acceptance_ok, acceptance_problems = acceptance_config_ok(k6_config)
    acceptance_profile_ok = seasonal_profile == 'acceptance' and not preflight and acceptance_ok
    managers_enabled = k6_config.get('managers_enabled') is True

    evidence_problems = []
    if not timing_valid:
        evidence_problems.append('foreign load or lease renewal failure invalidates timing')
    if not resource_window_ok:
        evidence_problems.extend(window['problems'] or ['resource proof did not pass before and after the run'])
    if not resource_measurement_ok:
        evidence_problems.append('pre/post resource measurement or post-run proof command did not pass')
    if not resource_budget_ok:
        evidence_problems.append('pre/post resource budget did not pass')
    if not acceptance_profile_ok:
        evidence_problems.append('run is not the unmodified full-hour acceptance profile with managers enabled')
        evidence_problems.extend(acceptance_problems)
    if not managers_enabled:
        evidence_problems.append('cashier-only/manager-disabled evidence cannot qualify')
    if not guest_attestation_ok:
        evidence_problems.append('ADR guest envelope attestation missing, failed, or changed')
    if not oracle_ok:
        evidence_problems.append(oracle_problem)
    load_generator_ok = load_generator.get('schema_version') == 'seasonal-load-generator-attestation-v1' and load_generator.get('passed') is True and load_generator.get('off_guest') is True
    if not load_generator_ok:
        evidence_problems.append('off-guest k6 load-generator attestation missing, failed, or diagnostic only')
    attribution_ok = attribution.get('collection_passed') is True
    if not attribution_ok:
        evidence_problems.append('request attribution collection evidence missing or failed')
    environment_ok = environment.get('passed') is True and environment.get('attestation') == 'qualifying'
    if not environment_ok:
        evidence_problems.append('owner host-environment attestation missing, inconsistent, or diagnostic only')
    actual_fixture_sha = str(fixture_restore.get('fixture_sha256') or '')
    fixture_pin_ok = bool(PINNED_ACCEPTANCE_FIXTURE_SHA256) and actual_fixture_sha == PINNED_ACCEPTANCE_FIXTURE_SHA256
    fixture_restore_ok = (
        fixture_restore.get('passed') is True
        and len(actual_fixture_sha) == 64
        and fixture_pin_ok
        and fixture_restore.get('archive_format') == 'cold-named-volume-tar/v1'
        and len(str(fixture_restore.get('fixture_manifest_sha256') or '')) == 64
    )
    if not fixture_restore_ok:
        if len(actual_fixture_sha) == 64 and not fixture_pin_ok:
            evidence_problems.append('restored fixture digest is not the pinned acceptance fixture; evidence is diagnostic only')
        else:
            evidence_problems.append('verified cold-volume fixture restore/manifest evidence missing or failed')
    if not candidate.get('app_commit_sha') or not candidate.get('app_tree_hash'):
        evidence_problems.append('candidate commit/tree evidence missing')
    migration_tree_hash_ok = (
        migration.get('exit_code') == 0
        and migration.get('tree_hash_verified') is True
        and migration.get('app_tree_hash_expected') == candidate.get('app_tree_hash')
        and migration.get('migrated_app_tree_hash') == candidate.get('app_tree_hash')
    )
    candidate_apps_ok = (
        candidate_apps.get('passed') is True
        and 'khitmee_ops' in (candidate_apps.get('apps_txt') or [])
        and not (candidate_apps.get('missing_from_apps_txt') or [])
        and not (candidate_apps.get('extra_in_apps_txt') or [])
    )
    if not candidate_apps_ok:
        evidence_problems.append('candidate apps.txt evidence missing or inconsistent with restored installed apps')
    if migration.get('exit_code') != 0:
        evidence_problems.append('candidate migration evidence missing or failed')
    elif not migration_tree_hash_ok:
        evidence_problems.append('migrated app tree digest missing or does not match the selected candidate tree')

    qualification_evidence = not evidence_problems
    qualified_bool = qualification_evidence and workload_passed
    qualified = 'GO' if qualified_bool else 'NO-GO'
    verdict = 'INVALID' if not timing_valid else qualified
    exit_code = workload_exit
    if not timing_valid:
        exit_code = workload_exit or 6
    elif seasonal_profile == 'acceptance' and not preflight and not qualified_bool:
        exit_code = workload_exit or 7

    duplicates = sum((row or {}).get('duplicates', 0) for row in (verification.get('by_type') or {}).values())
    oom = [row.get('service') for row in after.get('services') or [] if row.get('oom_killed')]
    restarts = [
        {'service': row.get('service'), 'restart_count': row.get('restart_count')}
        for row in after.get('services') or [] if row.get('restart_count')
    ]
    return {
        'verdict': verdict,
        'workload_passed': workload_passed,
        'workload_verdict': 'GO' if workload_passed else 'NO-GO',
        'qualification_evidence': qualification_evidence,
        'qualified': qualified,
        'qualified_passed': qualified_bool,
        'qualification_evidence_problems': evidence_problems,
        'valid_for_timing': timing_valid,
        'lease_renewal_failed': lease_renewal_failed,
        'seasonal_profile': seasonal_profile,
        'preflight': bool(preflight),
        'acceptance_profile_ok': acceptance_profile_ok,
        'managers_enabled': managers_enabled,
        'guest_attestation_ok': guest_attestation_ok,
        'oracle_evidence_ok': oracle_ok,
        'load_generator_ok': load_generator_ok,
        'load_generator_attestation': load_generator.get('attestation'),
        'load_generator_endpoint': load_generator.get('generator_endpoint'),
        'load_generator_daemon_identity': load_generator.get('generator_daemon_identity'),
        'attribution_collection_ok': attribution_ok,
        'attribution_collection_problems': attribution.get('collection_problems', []),
        'attribution_join_ratio': attribution.get('join_ratio'),
        'environment_attestation_ok': environment_ok,
        'environment_attestation': environment.get('attestation'),
        'environment_attestation_problems': environment.get('problems', []),
        'resource_window_ok': resource_window_ok,
        'resource_window_problems': window['problems'],
        'qualification_resource_measurement_ok': resource_measurement_ok,
        'qualification_resource_budget_ok': resource_budget_ok,
        'workload_memory_limit_mib_before': before.get('workload_memory_limit_total_mib'),
        'workload_memory_limit_mib_after': after.get('workload_memory_limit_total_mib'),
        'resource_budget_problems_before': before.get('budget_problems', []),
        'resource_budget_problems_after': after.get('budget_problems', []),
        'workload_exit': workload_exit,
        'fixture_sha256': fixture_restore.get('fixture_sha256'),
        'pinned_acceptance_fixture_sha256': PINNED_ACCEPTANCE_FIXTURE_SHA256,
        'fixture_is_acceptance_pin': fixture_pin_ok,
        'fixture_archive': fixture_restore.get('archive'),
        'fixture_archive_format': fixture_restore.get('archive_format'),
        'fixture_manifest_path': fixture_restore.get('fixture_manifest_path'),
        'fixture_manifest_sha256': fixture_restore.get('fixture_manifest_sha256'),
        'fixture_restore_passed': fixture_restore.get('passed') is True,
        'fixture_evidence_ok': fixture_restore_ok,
        'fixture_restore_artifact': str(artifact / 'fixture-restore.json'),
        'app_checkout': candidate.get('app_checkout'),
        'app_ref': candidate.get('app_ref'),
        'app_commit_sha': candidate.get('app_commit_sha'),
        'app_tree_hash': candidate.get('app_tree_hash'),
        'app_tree_path': candidate.get('app_tree_path'),
        'candidate_migration_required': migration.get('required') is True,
        'candidate_migration_exit_code': migration.get('exit_code'),
        'candidate_migration_tree_hash_verified': migration.get('tree_hash_verified') is True,
        'candidate_migrated_app_tree_hash': migration.get('migrated_app_tree_hash'),
        'migration_tree_hash_ok': migration_tree_hash_ok,
        'candidate_apps_ok': candidate_apps_ok,
        'candidate_apps_missing': candidate_apps.get('missing_from_apps_txt', []),
        'candidate_apps_extra': candidate_apps.get('extra_in_apps_txt', []),
        'candidate_migration_log': migration.get('log'),
        'candidate_role_grants': candidate.get('role_grants_artifact'),
        'request_attribution_summary': str(artifact / 'attribution' / 'attribution-summary.json'),
        'duplicates': duplicates,
        'docker_oom_killed_after_run': oom,
        'restart_counts_after_run': restarts,
        'foreign_load_start_count': foreign_start.get('foreign_count'),
        'foreign_load_end_count': foreign_end.get('foreign_count'),
        'foreign_load_start': foreign_start.get('foreign_containers', []),
        'foreign_load_end': foreign_end.get('foreign_containers', []),
        'baseline_containers_start': foreign_start.get('baseline_containers', []),
        'baseline_containers_end': foreign_end.get('baseline_containers', []),
        'scorecard': str(artifact / 'scorecard.md'),
        'resource_proof_before': str(artifact / 'resource-proof-before.json'),
        'resource_proof_after': str(artifact / 'resource-proof-after.json'),
        'guest_attestation_before': str(artifact / 'guest-attestation-before.json'),
        'guest_attestation_after': str(artifact / 'guest-attestation-after.json'),
        'oracle_evidence': str(artifact / 'oracle-evidence.json'),
        'foreign_load_start_artifact': str(artifact / 'foreign-load-start.json'),
        'foreign_load_end_artifact': str(artifact / 'foreign-load-end.json'),
        'exit_code': exit_code,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact-dir', required=True)
    parser.add_argument('--workload-exit', type=int, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--seasonal-profile', required=True)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args(argv)
    result = decide(args.artifact_dir, args.workload_exit, args.run_id, args.seasonal_profile, args.preflight)
    out = pathlib.Path(args.artifact_dir) / 'final-result.json'
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + chr(10), encoding='utf-8')
    print('FINAL ' + result['verdict'] + ' workload=' + result['workload_verdict'] + ' qualified=' + result['qualified'] + ' duplicates=' + str(result['duplicates']))
    print('final result: ' + str(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
