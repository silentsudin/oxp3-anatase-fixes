#!/bin/bash
# xmx-cpu-sampler.sh [--seconds N] : record what burns CPU while a game starts.
#
# XMX games compile their CM kernels with ocloc on first use (that is a real CPU
# burst). This samples the top processes so the next report can tell a one-time
# compile apart from a spin. Start it, launch the game, then hand over the file
# ~/oxp3-reports/cpu-startup.log.
set -uo pipefail
secs=90
[ "${1:-}" = "--seconds" ] && secs=${2:-90}
out=${XDG_STATE_HOME:-$HOME/.local/state}/oxp3-reports
mkdir -p "$out"
log=$out/cpu-startup.log
: > "$log"
echo "# $(date -Is) sampling ${secs}s; start the game now" >> "$log"
end=$((SECONDS + secs))
while [ $SECONDS -lt $end ]; do
    {
        echo "## $(date -Is) load=$(cut -d' ' -f1-3 /proc/loadavg)"
        ps -eo pcpu,pmem,comm --sort=-pcpu | awk 'NR>1 && $1>5 {printf "  %s%% %s%% %s\n",$1,$2,$3}' | head -6
        for p in ocloc cm-compile clang ld; do
            n=$(pgrep -fc "$p" 2>/dev/null || true)
            [ "${n:-0}" -gt 0 ] && echo "  procs $p: $n"
        done
    } >> "$log"
    sleep 2
done
echo "# done $(date -Is)" >> "$log"
echo "wrote $log"
