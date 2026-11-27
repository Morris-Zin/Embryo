#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="khitmee-perf"
INTERVAL_SECONDS=2

usage() {
  cat <<'EOF_USAGE'
Usage: bash perf-suite/scripts/stats-collector.sh [--output FILE] [--interval SECONDS]

Samples memory for every running khitmee-perf container every 2 seconds by default.
Output CSV columns include RSS/PSS/Private_Dirty from /proc/<pid>/smaps_rollup
and cgroup memory.current, memory.max, memory.peak, memory.events, and swap telemetry where available.

Stop with Ctrl+C. Run inside the KhitmeeERPNext WSL distro; root gives the most
complete smaps data.
EOF_USAGE
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PERF_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RESULTS_DIR="$PERF_DIR/results"
OUTPUT_FILE=""

while (($#)); do
  case "$1" in
    --output)
      [[ $# -ge 2 ]] || { echo "Missing value for --output" >&2; exit 2; }
      OUTPUT_FILE="$2"
      shift 2
      ;;
    --output=*)
      OUTPUT_FILE="${1#--output=}"
      shift
      ;;
    --interval)
      [[ $# -ge 2 ]] || { echo "Missing value for --interval" >&2; exit 2; }
      INTERVAL_SECONDS="$2"
      shift 2
      ;;
    --interval=*)
      INTERVAL_SECONDS="${1#--interval=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || (( INTERVAL_SECONDS < 1 )); then
  echo "--interval must be a positive integer number of seconds" >&2
  exit 2
fi

mkdir -p "$RESULTS_DIR"
if [[ -z "$OUTPUT_FILE" ]]; then
  OUTPUT_FILE="$RESULTS_DIR/memory-$(date +%Y%m%d-%H%M%S).csv"
fi

if [[ ! -f "$OUTPUT_FILE" ]]; then
  printf 'timestamp,container,container_id,pid,rss_kb,pss_kb,private_dirty_kb,cgroup_memory_current_bytes,cgroup_memory_max_bytes,cgroup_oom_kill,docker_oom_killed,restart_count,cgroup_memory_peak_bytes,cgroup_oom_events,cgroup_swap_current_bytes,cgroup_memory_anon_bytes,cgroup_memory_file_bytes,cgroup_memory_kernel_bytes,cgroup_memory_slab_bytes,cgroup_cpu_usage_usec,cgroup_cpu_nr_throttled,cgroup_cpu_throttled_usec\n' > "$OUTPUT_FILE"
fi

echo "Writing memory samples to $OUTPUT_FILE (every ${INTERVAL_SECONDS}s; Ctrl+C to stop)"

read_smaps() {
  local pid="$1"
  local rollup="/proc/$pid/smaps_rollup"

  if [[ -r "$rollup" ]]; then
    awk '
      /^Rss:/ { rss=$2 }
      /^Pss:/ { pss=$2 }
      /^Private_Dirty:/ { private_dirty=$2 }
      END {
        if (rss == "" && pss == "" && private_dirty == "") {
          print "NA,NA,NA"
        } else {
          printf "%d,%d,%d\n", rss + 0, pss + 0, private_dirty + 0
        }
      }
    ' "$rollup" 2>/dev/null || printf 'NA,NA,NA\n'
  else
    printf 'NA,NA,NA\n'
  fi
}

find_cgroup_path() {
  local container_id="$1"
  local pid1="$2"
  local candidate rel

  if [[ "$pid1" =~ ^[0-9]+$ ]] && (( pid1 > 0 )) && [[ -r "/proc/$pid1/cgroup" ]]; then
    rel="$(awk -F: '$1 == "0" { print $3 }' "/proc/$pid1/cgroup" 2>/dev/null | head -n 1)"
    if [[ -n "$rel" ]]; then
      candidate="/sys/fs/cgroup${rel}"
      if [[ -f "$candidate/memory.current" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  fi

  for candidate in \
    "/sys/fs/cgroup/system.slice/docker-$container_id.scope" \
    "/sys/fs/cgroup/docker/$container_id" \
    "/sys/fs/cgroup/system.slice/containerd-$container_id.scope" \
    "/sys/fs/cgroup/containerd/$container_id"; do
    if [[ -f "$candidate/memory.current" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  candidate="$(find /sys/fs/cgroup -maxdepth 5 \( -name "docker-$container_id.scope" -o -name "containerd-$container_id.scope" -o -name "$container_id" \) -type d 2>/dev/null | head -n 1 || true)"
  if [[ -n "$candidate" && -f "$candidate/memory.current" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  return 1
}

sum_smaps() {
  local cgroup_path="$1"
  local procs_file="$cgroup_path/cgroup.procs"
  local total_rss=0 total_pss=0 total_private_dirty=0
  local readable=0 pid values rss_kb pss_kb private_dirty_kb

  if [[ ! -r "$procs_file" ]]; then
    printf 'NA,NA,NA\n'
    return 0
  fi

  while read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    values="$(read_smaps "$pid")"
    if [[ "$values" != "NA,NA,NA" ]]; then
      IFS=, read -r rss_kb pss_kb private_dirty_kb <<< "$values"
      total_rss=$((total_rss + rss_kb))
      total_pss=$((total_pss + pss_kb))
      total_private_dirty=$((total_private_dirty + private_dirty_kb))
      readable=1
    fi
  done < "$procs_file"

  if (( readable )); then
    printf '%d,%d,%d\n' "$total_rss" "$total_pss" "$total_private_dirty"
  else
    printf 'NA,NA,NA\n'
  fi
}

read_memory_current() {
  local cgroup_path="$1"
  if [[ -n "$cgroup_path" && -r "$cgroup_path/memory.current" ]]; then
    tr -d '[:space:]' < "$cgroup_path/memory.current"
    printf '\n'
  else
    printf 'NA\n'
  fi
}

read_memory_max() {
  local cgroup_path="$1"
  if [[ -n "$cgroup_path" && -r "$cgroup_path/memory.max" ]]; then
    tr -d '[:space:]' < "$cgroup_path/memory.max"
    printf '\n'
  else
    printf 'NA\n'
  fi
}

read_memory_events_value() {
  local cgroup_path="$1"
  local event_name="$2"
  if [[ -n "$cgroup_path" && -r "$cgroup_path/memory.events" ]]; then
    awk -v event_name="$event_name" '
      BEGIN { found=0 }
      $1 == event_name { print $2; found=1 }
      END { if (!found) print "NA" }
    ' "$cgroup_path/memory.events" 2>/dev/null || printf 'NA\n'
  else
    printf 'NA\n'
  fi
}

read_oom_kill() {
  read_memory_events_value "$1" "oom_kill"
}

read_oom_events() {
  read_memory_events_value "$1" "oom"
}

read_memory_peak() {
  local cgroup_path="$1"
  if [[ -n "$cgroup_path" && -r "$cgroup_path/memory.peak" ]]; then
    tr -d '[:space:]' < "$cgroup_path/memory.peak"
    printf '\n'
  else
    printf 'NA\n'
  fi
}

read_swap_current() {
  local cgroup_path="$1"
  if [[ -n "$cgroup_path" && -r "$cgroup_path/memory.swap.current" ]]; then
    tr -d '[:space:]' < "$cgroup_path/memory.swap.current"
    printf '\n'
  else
    printf 'NA\n'
  fi
}

read_stat_value() {
  local cgroup_path="$1"
  local stat_file="$2"
  local key="$3"
  if [[ -n "$cgroup_path" && -r "$cgroup_path/$stat_file" ]]; then
    awk -v key="$key" '$1 == key { print $2; found=1 } END { if (!found) print "NA" }' "$cgroup_path/$stat_file" 2>/dev/null || printf 'NA\n'
  else
    printf 'NA\n'
  fi
}

read_memory_stat_value() {
  read_stat_value "$1" "memory.stat" "$2"
}

read_cpu_stat_value() {
  read_stat_value "$1" "cpu.stat" "$2"
}

collect_once() {
  local timestamp container_id container_name pid smaps_values rss_kb pss_kb private_dirty_kb
  local cgroup_path memory_current memory_max oom_kill memory_peak oom_events swap_current
  local memory_anon memory_file memory_kernel memory_slab cpu_usage_usec cpu_nr_throttled cpu_throttled_usec
  local docker_state docker_oom_killed restart_count

  timestamp="$(date --iso-8601=seconds)"
  while read -r container_id container_name; do
    [[ -n "$container_id" && -n "$container_name" ]] || continue

    pid="$(docker inspect --format '{{.State.Pid}}' "$container_id" 2>/dev/null || printf '0')"
    if ! [[ "$pid" =~ ^[0-9]+$ ]] || (( pid <= 0 )); then
      pid=0
    fi

    docker_state="$(docker inspect --format '{{.State.OOMKilled}},{{.RestartCount}}' "$container_id" 2>/dev/null || printf 'NA,NA')"
    IFS=, read -r docker_oom_killed restart_count <<< "$docker_state"
    [[ -n "$docker_oom_killed" ]] || docker_oom_killed="NA"
    [[ -n "$restart_count" ]] || restart_count="NA"

    cgroup_path="$(find_cgroup_path "$container_id" "$pid" || true)"
    if [[ -n "$cgroup_path" ]]; then
      smaps_values="$(sum_smaps "$cgroup_path")"
    else
      smaps_values="NA,NA,NA"
    fi
    IFS=, read -r rss_kb pss_kb private_dirty_kb <<< "$smaps_values"

    memory_current="$(read_memory_current "$cgroup_path")"
    memory_max="$(read_memory_max "$cgroup_path")"
    oom_kill="$(read_oom_kill "$cgroup_path")"
    memory_peak="$(read_memory_peak "$cgroup_path")"
    oom_events="$(read_oom_events "$cgroup_path")"
    swap_current="$(read_swap_current "$cgroup_path")"
    memory_anon="$(read_memory_stat_value "$cgroup_path" anon)"
    memory_file="$(read_memory_stat_value "$cgroup_path" file)"
    memory_kernel="$(read_memory_stat_value "$cgroup_path" kernel)"
    memory_slab="$(read_memory_stat_value "$cgroup_path" slab)"
    cpu_usage_usec="$(read_cpu_stat_value "$cgroup_path" usage_usec)"
    cpu_nr_throttled="$(read_cpu_stat_value "$cgroup_path" nr_throttled)"
    cpu_throttled_usec="$(read_cpu_stat_value "$cgroup_path" throttled_usec)"

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$timestamp" \
      "$container_name" \
      "$container_id" \
      "$pid" \
      "$rss_kb" \
      "$pss_kb" \
      "$private_dirty_kb" \
      "$memory_current" \
      "$memory_max" \
      "$oom_kill" \
      "$docker_oom_killed" \
      "$restart_count" \
      "$memory_peak" \
      "$oom_events" \
      "$swap_current" \
      "$memory_anon" \
      "$memory_file" \
      "$memory_kernel" \
      "$memory_slab" \
      "$cpu_usage_usec" \
      "$cpu_nr_throttled" \
      "$cpu_throttled_usec" >> "$OUTPUT_FILE"
  done < <(docker ps --no-trunc --filter "label=com.docker.compose.project=$PROJECT_NAME" --format '{{.ID}} {{.Names}}' | awk '$2 !~ /-run-/' | LC_ALL=C sort -k2)
}

while true; do
  collect_once
  sleep "$INTERVAL_SECONDS"
done
