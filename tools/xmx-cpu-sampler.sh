#!/bin/bash
# xmx-cpu-sampler.sh [--seconds N] [--wait M] : record what burns CPU when a game starts.
#
# XMX compiles CM kernels with ocloc on first use (a real CPU burst). This waits for
# a game process to appear, then samples instantaneous CPU with top, so a one-time
# compile can be told apart from a persistent spin. Start it, then launch the game.
set -uo pipefail
secs=150 wait_s=600
while [ $# -gt 0 ]; do case $1 in
    --seconds) secs=${2:-150}; shift 2 ;;
    --wait) wait_s=${2:-600}; shift 2 ;;
    *) echo "usage: $0 [--seconds N] [--wait M]" >&2; exit 2 ;;
esac; done
out=${XDG_STATE_HOME:-$HOME/.local/state}/oxp3-reports
mkdir -p "$out"; log=$out/cpu-startup.log; : > "$log"
game_re='(HogwartsLegacy|\.exe$|gamescope)'
echo "# $(date -Is) waiting up to ${wait_s}s for a game, then sampling ${secs}s" >> "$log"
t0=$SECONDS
while [ $((SECONDS - t0)) -lt "$wait_s" ]; do
    if pgrep -af "$game_re" 2>/dev/null | grep -qvE "pgrep|xmx-cpu-sampler"; then break; fi
    sleep 1
done
echo "# $(date -Is) game detected; sampling starts (start the title now if it is still on a launcher)" >> "$log"
end=$((SECONDS + secs))
while [ $SECONDS -lt $end ]; do
    {
        echo "## $(date -Is) load=$(cut -d' ' -f1-3 /proc/loadavg)"
        top -b -n1 -o %CPU 2>/dev/null | sed -n '7,16p' | sed 's/^/  /'
        for p in ocloc clang clang-21 ld.lld lld; do
            n=$(pgrep -xc "$p" 2>/dev/null || true)
            [ "${n:-0}" -gt 0 ] && echo "  procs $p: $n"
        done
        game=$(pgrep -af '\.exe$' 2>/dev/null | head -1)
        [ -n "$game" ] && echo "  game: $game"
    } >> "$log"
    sleep 2
done
echo "# done $(date -Is)" >> "$log"
echo "wrote $log"
