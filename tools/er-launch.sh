#!/bin/sh
# SPDX-License-Identifier: MIT
# er-launch.sh - Elden Ring launch wrapper for Intel hybrid CPUs (EAC stays on).
#
# Steam Launch Options:
#   /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/er-launch.sh %command%
#
# Sets the known Linux workaround for EAC's CPU-count handling on many-core
# CPUs (WINE_CPU_TOPOLOGY) and keeps Proton's DXVK settings sane.  Override the
# visible CPU set with ER_TOPOLOGY, e.g. ER_TOPOLOGY=10:0,1,2,3,4,5,6,7,8,9.
set -eu
export WINE_CPU_TOPOLOGY="${ER_TOPOLOGY:-8:0,1,2,3,4,5,6,7}"
export PROTON_VKD3D_HEAP="${PROTON_VKD3D_HEAP:-1}"
export DXVK_CONFIG="${DXVK_CONFIG:-dxgi.syncInterval=0}"
exec "$@"
