#!/usr/bin/env python3
"""Attest the ADR 0004 guest/cgroup envelope for seasonal qualification.

This collector is intentionally fail-closed. Passing container caps without a
verified two-CPU/2GiB/no-swap parent envelope is diagnostic evidence only.
"""

import argparse
import json
import pathlib
import subprocess
import sys

SERVICES = ("db", "backend", "queue", "scheduler", "redis-cache", "redis-queue", "websocket", "frontend")
EXPECTED_CPU_MAX = "200000 100000"
EXPECTED_MEMORY_MAX = 2 * 1024**3


def read_text(path):
    return pathlib.Path(path).read_text(encoding="utf-8").strip()


def cpuset_count(value):
    total = 0
    for part in str(value).split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            first, last = part.split('-', 1)
            total += int(last) - int(first) + 1
        else:
            total += 1
    return total


def guest_facts():
    cpus = 0
    with open('/proc/cpuinfo', encoding='utf-8') as handle:
        cpus = sum(1 for line in handle if line.startswith('processor'))
    meminfo = {}
    with open('/proc/meminfo', encoding='utf-8') as handle:
        for line in handle:
            key, rest = line.split(':', 1)
            meminfo[key] = int(rest.strip().split()[0]) * 1024
    return {
        'guest_visible_cpu_count': cpus,
        'guest_mem_total_bytes': meminfo.get('MemTotal'),
        'guest_swap_total_bytes': meminfo.get('SwapTotal'),
    }


def docker_json(args):
    result = subprocess.run(['docker', *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('docker ' + args[0] + ' failed: ' + (result.stderr or result.stdout)[:240])
    return json.loads(result.stdout)


def container_cgroup(pid):
    relative = ''
    with open(f'/proc/{pid}/cgroup', encoding='utf-8') as handle:
        for line in handle:
            parts = line.strip().split(':', 2)
            if len(parts) == 3 and parts[0] == '0':
                relative = parts[2]
                break
    return str(pathlib.Path('/sys/fs/cgroup' + relative)) if relative else ''


def service_membership(project, parent):
    result = subprocess.run(
        ['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + project],
        text=True, capture_output=True,
    )
    if result.returncode:
        raise RuntimeError('cannot list project containers for guest membership attestation')
    rows = []
    problems = []
    for container_id in filter(None, result.stdout.splitlines()):
        infos = docker_json(['inspect', container_id])
        info = infos[0] if isinstance(infos, list) else infos
        service = (info.get('Config') or {}).get('Labels', {}).get('com.docker.compose.service')
        if service not in SERVICES:
            continue
        state = info.get('State') or {}
        path = container_cgroup(int(state.get('Pid') or 0)) if state.get('Running') else ''
        member = bool(path) and (path == parent or path.startswith(parent.rstrip('/') + '/'))
        rows.append({'service': service, 'id': (info.get('Id') or '')[:12], 'cgroup_path': path, 'member': member})
        if not member:
            problems.append(service + ' is not inside the attested parent cgroup')
    missing = sorted(set(SERVICES) - {row['service'] for row in rows})
    for service in missing:
        problems.append('missing running service for membership attestation: ' + service)
    return rows, problems


def attest(parent='/sys/fs/cgroup/khitmee-2gb.slice', project='khitmee-perf'):
    problems = []
    root = pathlib.Path(parent)
    values = {'parent': parent}
    if not root.is_dir():
        problems.append('ADR parent cgroup does not exist: ' + parent)
    else:
        try:
            cpuset = read_text(root / 'cpuset.cpus.effective')
            cpu_max = read_text(root / 'cpu.max')
            memory_max = int(read_text(root / 'memory.max'))
            swap_max = read_text(root / 'memory.swap.max')
            values.update({
                'cpuset_cpus_effective': cpuset,
                'cpuset_cpu_count': cpuset_count(cpuset),
                'cpu_max': cpu_max,
                'memory_max_bytes': memory_max,
                'memory_swap_max': swap_max,
            })
            if values['cpuset_cpu_count'] != 2:
                problems.append('parent cpuset does not expose exactly two CPUs')
            if cpu_max != EXPECTED_CPU_MAX:
                problems.append('parent cpu.max is not 200000 100000')
            if memory_max != EXPECTED_MEMORY_MAX:
                problems.append('parent memory.max is not 2147483648')
            if swap_max != '0':
                problems.append('parent memory.swap.max is not 0')
        except (OSError, ValueError) as exc:
            problems.append('cannot read parent cgroup envelope: ' + str(exc))
    try:
        members, membership_problems = service_membership(project, parent)
    except Exception as exc:
        members, membership_problems = [], [str(exc)]
    problems.extend(membership_problems)
    values.update(guest_facts())
    values.update({
        'schema_version': 'seasonal-guest-attestation-v1',
        'project': project,
        'service_membership': members,
        'service_membership_verified': not membership_problems,
        'passed': not problems,
        'problems': problems,
    })
    return values


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--parent', default='/sys/fs/cgroup/khitmee-2gb.slice')
    parser.add_argument('--project', default='khitmee-perf')
    args = parser.parse_args(argv)
    result = attest(args.parent, args.project)
    pathlib.Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
