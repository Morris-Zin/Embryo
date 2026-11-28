#!/usr/bin/env bash
set -euo pipefail

# Release the runtime-only host CPU exclusion installed by guest_cgroup_setup.sh.
# This does not stop or remove containers and leaves the managed slice unit in
# place for the next opt-in guest-profile run.

fail() {
  echo "guest cgroup teardown failed: $*" >&2
  exit 1
}

[[ "$(id -u)" == "0" ]] || fail "must run as root inside the WSL distro"
command -v systemctl >/dev/null 2>&1 || fail "systemctl is unavailable"
command -v python3 >/dev/null 2>&1 || fail "python3 is unavailable"

all_cpus="$(python3 - <<'PY'
def expand(spec):
    cpus = set()
    for part in spec.split(','):
        if '-' in part:
            first, last = part.split('-', 1)
            cpus.update(range(int(first), int(last) + 1))
        elif part.strip():
            cpus.add(int(part))
    return cpus

def compact(cpus):
    cpus = sorted(cpus)
    ranges = []
    start = prev = cpus[0]
    for cpu in cpus[1:]:
        if cpu == prev + 1:
            prev = cpu
        else:
            ranges.append((start, prev))
            start = prev = cpu
    ranges.append((start, prev))
    return ','.join(str(a) if a == b else f'{a}-{b}' for a, b in ranges)

print(compact(expand(open('/sys/devices/system/cpu/online', encoding='utf-8').read().strip())))
PY
)"

for unit in init.scope system.slice user.slice; do
  systemctl set-property --runtime "$unit" AllowedCPUs="$all_cpus" >/dev/null
done

printf 'host systemd scopes restored to cpuset.cpus.effective=%s\n' "$all_cpus"
