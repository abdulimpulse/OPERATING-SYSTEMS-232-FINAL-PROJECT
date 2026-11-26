#!/bin/sh
# Start a command inside a cgroup, with a nice value and a CPU affinity list.
# Usage: launch.sh <cgroup-dir> <nice> <cpu-list> <command> [args...]
# Everything the command starts (all threads, all children) inherits all three settings.
cg="$1"; n="$2"; cpus="$3"; shift 3
echo $$ > "$cg/cgroup.procs" || { echo "launch.sh: cannot join $cg" >&2; exit 1; }
exec taskset -c "$cpus" nice -n "$n" "$@"
