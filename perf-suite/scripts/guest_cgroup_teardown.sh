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

NON_SYSTEMD_CGROUP="/sys/fs/cgroup/non-systemd"
NON_SYSTEMD_STATE="/run/khitmee-perf.non-systemd.cpuset"
if [[ -d "$NON_SYSTEMD_CGROUP" && -w "$NON_SYSTEMD_CGROUP/cpuset.cpus" ]]; then
  restore_cpus="$all_cpus"
  if [[ -f "$NON_SYSTEMD_STATE" ]]; then
    saved_cpus="$(cat "$NON_SYSTEMD_STATE")"
    if [[ -n "$saved_cpus" ]]; then
      restore_cpus="$saved_cpus"
    fi
  fi
  # An empty configured value means inherit-all, but the kernel refuses to
  # clear cpuset.cpus while the cgroup is populated. Writing the current full
  # online set restores the same effective placement for this WSL host.
  printf '%s' "$restore_cpus" > "$NON_SYSTEMD_CGROUP/cpuset.cpus"
  rm -f "$NON_SYSTEMD_STATE"
fi

printf 'host systemd scopes restored to cpuset.cpus.effective=%s\n' "$all_cpus"
