#!/bin/bash
# Reload the Intel CNVi Bluetooth driver after the real root is up.
# btintel_pcie has no MODULE_FIRMWARE() entries, so dracut cannot copy the
# firmware into the initramfs; the probe before switch_root fails with ENOENT.
# Reloading here picks the firmware up from /usr/lib/firmware/updates.
set -u
log() { echo "oxp3-bt-reload: $*"; }
modprobe -r btintel_pcie btintel 2>/dev/null
sleep 1
if ! modprobe btintel_pcie; then
    log "modprobe btintel_pcie failed"; exit 1
fi
for _ in $(seq 1 20); do
    sleep 0.5
    if bluetoothctl show 2>/dev/null | grep -qi "Controller"; then
        log "controller present"; exit 0
    fi
done
systemctl restart bluetooth 2>/dev/null
for _ in $(seq 1 20); do
    sleep 0.5
    if bluetoothctl show 2>/dev/null | grep -qi "Controller"; then
        log "controller present after bluetooth restart"; exit 0
    fi
done
log "no controller after reload"
exit 2
