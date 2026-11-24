#!/bin/bash
# SPDX-License-Identifier: MIT
# Capture what the xHCI driver programs for a USB device (e.g. the Steam Controller puck) and which
# command the controller rejects. Runs for 40 s, then restores everything it touched.
# Usage: sudo bash tools/usb/xhci-trace.sh <output file>
set -u
out=${1:?output file}
T=/sys/kernel/tracing
DD=/sys/kernel/debug/dynamic_debug/control
events=(xhci_add_endpoint xhci_configure_endpoint xhci_configure_endpoint_ctrl_ctx
        xhci_handle_cmd_config_ep xhci_setup_device_slot xhci_address_ctx xhci_handle_command
        xhci_queue_trb xhci_handle_event xhci_alloc_virt_device xhci_setup_addressable_virt_device)

cleanup() {
    echo 0 > "$T/tracing_on" 2>/dev/null
    for e in "${events[@]}"; do
        [ -w "$T/events/xhci-hcd/$e/enable" ] && echo 0 > "$T/events/xhci-hcd/$e/enable"
    done
    echo 'module xhci_hcd -p' > "$DD" 2>/dev/null
    echo 'module usbcore -p' > "$DD" 2>/dev/null
}
trap cleanup EXIT

echo 0 > "$T/tracing_on"; echo > "$T/trace"
echo 16384 > "$T/buffer_size_kb"
for e in "${events[@]}"; do
    [ -w "$T/events/xhci-hcd/$e/enable" ] && echo 1 > "$T/events/xhci-hcd/$e/enable"
done
echo 'module xhci_hcd +p' > "$DD"
echo 'module usbcore +p' > "$DD"
echo 1 > "$T/tracing_on"

echo ">>> Capturing for 40 s: unplug the puck, wait 3 s, plug it back into the dock port."
for i in $(seq 40 -5 5); do echo "    $i s left"; sleep 5; done

echo 0 > "$T/tracing_on"
cp "$T/trace" "$out"
journalctl -k -b --no-pager -o short-iso --since "-60s" >> "$out.kernel"
chown "${SUDO_UID:-0}:${SUDO_GID:-0}" "$out" "$out.kernel" 2>/dev/null
echo ">>> Saved $(wc -l < "$out") trace lines to $out (+ $out.kernel)"
