#!/bin/bash
# SPDX-License-Identifier: MIT
# Report and repair the HHD-emulated controller in Gamemode.
# status: read-only report. --xbox: switch HHD to the uinput (Xbox 360) output, which
# always presents an input node Steam/SDL detect. --steam: switch back to hori_steam.
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo: sudo $0 status|--xbox|--steam" >&2; exit 2; }
mode_arg=${1:-status}

mode_now() { hhdctl get controllers.oxp.controller_mode.mode --values 2>/dev/null | tr -d '"' | tr '\n' ' '; }
hid_state() {
    local h found28=no
    for h in /sys/bus/hid/devices/0003:28DE:*; do
        [[ -e $h ]] || continue
        found28=yes
    done
    echo "$found28"
}
input_nodes() {
    local d nm
    for d in /sys/class/input/event*/device/name; do
        [[ -e $d ]] || continue
        nm=$(cat "$d" 2>/dev/null)
        case $nm in
            *Steam*|*X-Box*|*Xbox*|*DualSense*|*HHD*) printf '  %s: %s\n' "$(basename "$(dirname "$(dirname "$d")")")" "$nm" ;;
        esac
    done
}

report() {
    echo "== hhd service: $(systemctl is-active hhd.service)"
    echo "== hhd controller mode: $(mode_now)"
    echo "== emulated steam controller (hidraw only unless uinput): $(hid_state)"
    echo "== physical pad (xpad, hidden by hhd's devhide rule in steam mode):"
    local pe
    for pe in /sys/class/input/event*/device/uevent; do
        [[ -e $pe ]] || continue
        if grep -q "PRODUCT=3/45e/28e" "$pe" 2>/dev/null; then
            printf '  %s: physical 045e:028e pad, %s\n' \
                "$(basename "$(dirname "$(dirname "$pe")")")" \
                "$(ls -l "/dev/input/$(basename "$(dirname "$(dirname "$pe")")")" 2>/dev/null | awk '{print $1}')"
        fi
    done
    echo "== gamepad-like input nodes:"; input_nodes
    local n; n=$(ls /dev/hidraw* 2>/dev/null | wc -l)
    echo "== hidraw nodes: $n"
    if [[ -f /var/home/holzr/.steam/steam/logs/controller.txt ]]; then
        echo "== last steam controller log lines:"
        grep -aE "controller device opened|Opted-in Controller Mask|No controller" \
            /var/home/holzr/.steam/steam/logs/controller.txt 2>/dev/null | tail -4 | sed 's/^/  /'
    fi
    if pgrep -x steam >/dev/null; then echo "== steam: running"; else echo "== steam: not running"; fi
}

switch_mode() {
    local want=$1 label=$2
    echo "== switching HHD controller output to $want ($label)"
    hhdctl set "controllers.oxp.controller_mode.mode=$want" 2>&1 | tail -3
    systemctl restart hhd.service
    echo "== waiting for the new device"
    for _ in $(seq 1 15); do
        sleep 1
        if [[ $want == uinput ]] && hhdctl get controllers.oxp.controller_mode.mode --values 2>/dev/null | grep -q uinput; then break; fi
        if [[ $want != uinput ]] && [[ $(hid_state) == yes ]]; then break; fi
    done
    report
    echo
    if [[ $want == uinput ]]; then
        echo "next: start Steam again (exit to desktop and relaunch, or reboot into Gamemode)."
        echo "      a normal 'Microsoft X-Box 360 pad' input node is what Steam and SDL detect."
    else
        echo "next: Steam must be (re)started AFTER hhd; it claims this device through its own"
        echo "      Steam Controller path. If Gamemode still shows nothing, use --xbox instead."
    fi
}

case $mode_arg in
    status) report ;;
    --xbox) switch_mode uinput "Xbox 360 via uinput" ;;
    --steam) switch_mode hori_steam "Steam Controller (HHD)" ;;
    *) echo "usage: sudo $0 status|--xbox|--steam" >&2; exit 2 ;;
esac
