#!/usr/bin/env bash
set -euo pipefail

SLICE_NAME="khitmee-perf.slice"
UNIT_PATH="/etc/systemd/system/$SLICE_NAME"
CGROUP_PATH="/sys/fs/cgroup/$SLICE_NAME"
EXPECTED_CPU_MAX="200000 100000"
EXPECTED_MEMORY_MAX="2147483648"
EXPECTED_SWAP_MAX="0"
MANAGED_MARKER="Managed by Khitmee perf-suite guest_cgroup_setup.sh"

fail() {
  echo "guest cgroup setup failed: $*" >&2
  exit 1
}

[[ -f /sys/fs/cgroup/cgroup.controllers ]] || fail "cgroup v2 is not mounted at /sys/fs/cgroup"
[[ "$(ps -p 1 -o comm= 2>/dev/null || true)" == "systemd" ]] || fail "PID 1 is not systemd"
command -v systemctl >/dev/null 2>&1 || fail "systemctl is unavailable"
command -v python3 >/dev/null 2>&1 || fail "python3 is unavailable"
[[ "$(id -u)" == "0" ]] || fail "must run as root inside the WSL distro to create $SLICE_NAME"

mapfile -t cpuset_lines < <(python3 - "${GUEST_CPUSET:-}" <<'PY'
import sys

def expand(spec):
    cpus = set()
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            first, last = part.split('-', 1)
            cpus.update(range(int(first), int(last) + 1))
        else:
            cpus.add(int(part))
    return cpus

def compact(cpus):
    cpus = sorted(cpus)
    if not cpus:
        return ''
    ranges = []
    start = prev = cpus[0]
    for cpu in cpus[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append((start, prev))
        start = prev = cpu
    ranges.append((start, prev))
    return ','.join(str(a) if a == b else f'{a}-{b}' for a, b in ranges)

online_text = open('/sys/devices/system/cpu/online', encoding='utf-8').read().strip()
online = expand(online_text)
requested = sys.argv[1].strip()
if requested:
    try:
        selected = expand(requested)
    except Exception as exc:
        raise SystemExit(f'invalid GUEST_CPUSET {requested!r}: {exc}')
else:
    selected = set()
    seen_cores = set()
    for cpu in sorted(online, reverse=True):
        try:
            core_text = open(f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list', encoding='utf-8').read().strip()
            core_key = tuple(sorted(expand(core_text)))
        except Exception:
            core_key = (cpu,)
        if core_key in seen_cores:
            continue
        seen_cores.add(core_key)
        selected.add(cpu)
        if len(selected) == 2:
            break
    if len(selected) != 2:
        selected = set(sorted(online)[-2:])
if len(selected) != 2:
    raise SystemExit(f'guest cpuset must contain exactly two logical CPUs, got {sorted(selected)}')
if not selected <= online:
    raise SystemExit(f'guest cpuset {sorted(selected)} is not within online CPUs {sorted(online)}')
complement = online - selected
if not complement:
    raise SystemExit('cannot exclude the guest CPUs from host systemd scopes when no other online CPUs remain')
print(compact(selected))
print(compact(complement))
PY
)
((${#cpuset_lines[@]} == 2)) || fail "could not derive guest cpuset: ${cpuset_lines[*]}"
GUEST_CPUSET_EFFECTIVE_REQUEST="${cpuset_lines[0]}"
HOST_COMPLEMENT_CPUSET="${cpuset_lines[1]}"

if [[ -f "$UNIT_PATH" ]] && ! grep -q "$MANAGED_MARKER" "$UNIT_PATH"; then
  fail "$UNIT_PATH already exists and is not marked as managed by this script"
fi

unit_contents() {
  cat <<EOF_UNIT
# $MANAGED_MARKER; manual edits will be overwritten.
[Unit]
Description=Khitmee perf whole-guest cgroup boundary (2 exclusive logical CPUs, 2 GiB, no swap)

[Slice]
AllowedCPUs=$GUEST_CPUSET_EFFECTIVE_REQUEST
CPUQuota=200%
MemoryMax=2G
MemorySwapMax=0
EOF_UNIT
}

if [[ ! -f "$UNIT_PATH" ]] || ! cmp -s <(unit_contents) "$UNIT_PATH"; then
  tmp_unit="$(mktemp /etc/systemd/system/.khitmee-perf.slice.XXXXXX)"
  unit_contents > "$tmp_unit"
  chmod 0644 "$tmp_unit"
  mv "$tmp_unit" "$UNIT_PATH"
  systemctl daemon-reload
fi

systemctl start "$SLICE_NAME"
systemctl set-property "$SLICE_NAME" \
  AllowedCPUs="$GUEST_CPUSET_EFFECTIVE_REQUEST" \
  CPUQuota=200% MemoryMax=2G MemorySwapMax=0 >/dev/null

# A cgroup cpuset is only a lower-bound placement constraint. To approximate
# exclusive VPS CPUs on this larger WSL host, keep systemd itself, system
# services and user sessions on the complement. This is runtime-only and is
# reapplied whenever the guest profile starts.
for unit in init.scope system.slice user.slice; do
  systemctl set-property --runtime "$unit" AllowedCPUs="$HOST_COMPLEMENT_CPUSET" >/dev/null
done

[[ -d "$CGROUP_PATH" ]] || fail "systemd started $SLICE_NAME but $CGROUP_PATH does not exist"

cpu_max="$(cat "$CGROUP_PATH/cpu.max" 2>/dev/null || true)"
memory_max="$(cat "$CGROUP_PATH/memory.max" 2>/dev/null || true)"
swap_max="$(cat "$CGROUP_PATH/memory.swap.max" 2>/dev/null || true)"
cpuset_effective="$(cat "$CGROUP_PATH/cpuset.cpus.effective" 2>/dev/null || true)"

[[ "$cpuset_effective" == "$GUEST_CPUSET_EFFECTIVE_REQUEST" ]] || fail "cpuset.cpus.effective is '$cpuset_effective', expected '$GUEST_CPUSET_EFFECTIVE_REQUEST'"
[[ "$cpu_max" == "$EXPECTED_CPU_MAX" ]] || fail "cpu.max is '$cpu_max', expected '$EXPECTED_CPU_MAX'"
[[ "$memory_max" == "$EXPECTED_MEMORY_MAX" ]] || fail "memory.max is '$memory_max', expected '$EXPECTED_MEMORY_MAX'"
[[ "$swap_max" == "$EXPECTED_SWAP_MAX" ]] || fail "memory.swap.max is '$swap_max', expected '$EXPECTED_SWAP_MAX'"

for unit_path in /sys/fs/cgroup/init.scope /sys/fs/cgroup/system.slice /sys/fs/cgroup/user.slice; do
  if [[ -f "$unit_path/cpuset.cpus.effective" ]]; then
    sibling_cpuset="$(cat "$unit_path/cpuset.cpus.effective")"
    [[ "$sibling_cpuset" == "$HOST_COMPLEMENT_CPUSET" ]] || fail "$unit_path cpuset is '$sibling_cpuset', expected complement '$HOST_COMPLEMENT_CPUSET'"
  fi
done

printf 'guest slice %s ready at %s\n' "$SLICE_NAME" "$CGROUP_PATH"
printf 'cpuset.cpus.effective=%s\ncpu.max=%s\nmemory.max=%s\nmemory.swap.max=%s\n' "$cpuset_effective" "$cpu_max" "$memory_max" "$swap_max"
printf 'host systemd scopes restricted to cpuset.cpus.effective=%s\n' "$HOST_COMPLEMENT_CPUSET"
