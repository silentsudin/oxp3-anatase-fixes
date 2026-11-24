#!/usr/bin/env bash
# Capture the KMS state for one physical connector. Run from the host.
set -euo pipefail

connector=${1:-card1-DP-1}
duration=${2:-60}
sysfs=/sys/class/drm/$connector
device=/dev/dri/${connector%%-*}

if [[ ! $connector =~ ^card[0-9]+-[[:alnum:]-]+$ ||
      ! $duration =~ ^[1-9][0-9]{0,3}$ || ! -r $sysfs/connector_id ]]; then
    echo "Usage: $0 [sysfs connector, e.g. card1-DP-1] [seconds, 1–9999]" >&2
    exit 2
fi

drm_info=${DRM_INFO:-drm_info}
if ! command -v "$drm_info" >/dev/null || ! command -v jq >/dev/null || [[ ! -r $device ]]; then
    echo "Run on the host with jq, drm_info (or DRM_INFO=/path/to/drm_info), and access to $device." >&2
    exit 1
fi

end=$((SECONDS + duration))
while (( SECONDS < end )); do
    timestamp=$(date -Ins)
    if status=$(cat "$sysfs/status") && id=$(cat "$sysfs/connector_id") &&
        state=$("$drm_info" -j "$device" | jq -ce --argjson id "$id" '
            .[] | . as $card | .connectors[] | select(.id == $id) |
            .properties as $connector |
            ([$card.crtcs[] | select(.id == $connector.CRTC_ID.value)][0].properties // {}) as $crtc |
            {crtc: $connector.CRTC_ID.value,
             capable: $connector.vrr_capable.value,
             enabled: $crtc.VRR_ENABLED.value,
             active: $crtc.ACTIVE.value,
             max_bpc: $connector["max bpc"].value,
             mode: ($crtc.MODE_ID.data | if . then {name, vrefresh, clock, htotal, vtotal} else null end)}
        '); then
        printf '%s %s %s %s\n' "$timestamp" "$connector" "$status" "$state"
    else
        printf '%s %s %s snapshot unavailable\n' "$timestamp" "$connector" "${status:-missing}"
    fi
    sleep 0.5
done
