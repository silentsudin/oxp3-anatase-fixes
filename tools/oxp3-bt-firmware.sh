#!/usr/bin/env bash
# oxp3-bt-firmware.sh - verify/install the Intel CNVi Bluetooth firmware for the
# OneXPlayer 3 (Panther Lake, `btintel_pcie` on PCI 0000:00:14.7 [8086:e476]).
#
# Background and evidence: docs/hw/bluetooth.md.
#
# The files come from upstream linux-firmware (licence LICENCE.ibt_firmware,
# "Redistributable"); nothing is stored in this repository. They are placed in
# /usr/lib/firmware/updates/intel/ because that directory has priority over
# /usr/lib/firmware/intel/ (fw_path order: updates before main), so a pinned
# build can override the packaged one without fighting the distro package.
#
# Modes:
#   --check     read-only report; no root, no writes. exit 0 = radio should work,
#               1 = action needed, 2 = usage error.
#   --dry-run   print the exact privileged commands; no root, no writes.
#   (default)   download -> verify SHA-256 -> install -> fix initramfs -> reload
#               -> verify controller. Needs sudo (prompted per command).
#
# Options:
#   --from PATH|DIR   use local copies instead of downloading (FILE may be given
#                     once per --from; a DIR is searched for the basenames)
#   --cache DIR       download cache, outside the repo
#                     (default: ${XDG_CACHE_HOME:-$HOME/.cache}/oxp3-bt-firmware)
#   --url-base URL    override the linux-firmware plain-file base URL
#   --with-ddc        also install intel/ibt-00a0-0291-pci.ddc (4-byte no-op stub)
#   --persist=MODE    initramfs strategy: items (default) | omit | none
#   --no-initramfs    same as --persist=none
#   --no-reload       do not reload btintel_pcie
#   -h|--help
#
# Why the initramfs step exists: btintel_pcie computes the firmware names at run
# time and therefore ships no MODULE_FIRMWARE()/modinfo `firmware=` entries, so
# dracut cannot copy them into the initramfs. The driver probes inside the
# initramfs (udev coldplug), gets -ENOENT and never retries after switch_root.
# See docs/hw/bluetooth.md for the boot-time evidence.

set -euo pipefail
export LC_ALL=C

PROG=${0##*/}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# --- pinned firmware set -----------------------------------------------------
# path-relative-to-firmware-root  size-bytes  sha256
FILES=(
  "intel/ibt-00a0-0291-iml.sfi 63056 69beb77387a79a75ee741ff6f88f782a0fc44c8b2efcc7bcb41c413122928026"
  "intel/ibt-00a0-0291-pci.sfi 998916 6ea03de60bdd6231387f561fbae53b1aaedfd906437b3c2216b45df1d4f01e45"
)
DDC_FILE="intel/ibt-00a0-0291-pci.ddc 4 e0fb10fe29f60607d6507ced3f697492d9611f62c4765feaee59c4bd4101d3e7"

# upstream Release name this set belongs to (WHENCE): BT_Scorpius_S_REL102163_24.80.26362.102163
UPSTREAM_VERSION="BT_Scorpius_S_REL102163_24.80.26362.102163"
URL_BASES=(
  "https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/plain"
  "https://gitlab.com/kernel-firmware/linux-firmware/-/raw/main"
)

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/oxp3-bt-firmware"
FW_UPDATES=/usr/lib/firmware/updates/intel
FW_MAIN=/usr/lib/firmware/intel
DRACUT_CONF=/etc/dracut.conf.d/60-oxp3-bt-firmware.conf
MODULES=(btintel_pcie btintel)

WANT=()          # built from FILES (+ optional ddc) after argument parsing
MODE=install
PERSIST=items
WITH_DDC=0
DO_RELOAD=1
URL_BASE=""
FROM_ARGS=()

# --- helpers -----------------------------------------------------------------
say()  { printf '%s\n' "$*"; }
info() { printf '[*] %s\n' "$*"; }
ok()   { printf '[ok] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*" >&2; }
err()  { printf '[err] %s\n' "$*" >&2; }
die()  { err "$*"; exit 2; }
have() { command -v "$1" >/dev/null 2>&1; }

# Write $DRACUT_CONF, keeping the previous content in the undo state first (docs/ROLLBACK.md).
# A file that did not exist is recorded as created, so the uninstall removes it; a pre-existing
# drop-in is restored byte-for-byte instead of being deleted.
conf_write() { # CONTENT
  local ts=""
  # Idempotent: unchanged content means no write and no new backup row.
  if [[ -f $DRACUT_CONF ]] && printf '%s\n' "$1" | cmp -s - "$DRACUT_CONF"; then
    return 0
  fi
  if [[ -x $HERE/oxp3-rollback.sh ]]; then
    ts=$("$HERE/oxp3-rollback.sh" backup "$DRACUT_CONF" 2>/dev/null || true)
  fi
  printf '%s\n' "$1" | sudo tee "$DRACUT_CONF" >/dev/null
  if [[ -n $ts ]]; then
    "$HERE/oxp3-rollback.sh" record --ts "$ts" "$DRACUT_CONF" --tool "$PROG" \
      --note "dracut initramfs drop-in (--persist=$PERSIST)" >/dev/null 2>&1 || true
  fi
}

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

sha_of() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
size_of() { stat -c %s "$1" 2>/dev/null || echo 0; }

is_ostree() { [[ -e /run/ostree-booted ]]; }

# Is the firmware directory writable *as root*? We cannot test root access
# directly, so a non-writable check as the current user plus the mount options
# decide. usroverlay flips /usr to a rw overlay, which is what makes the plain
# `install` path work on this host.
usr_writable() {
  local t=$FW_UPDATES
  [[ -d $t ]] || t=/usr/lib/firmware
  if [[ -w $t ]]; then return 0; fi
  # directory is root-owned: fall back to the mount being rw and warn later
  awk -v d="$t" '$2==d || $2=="/usr" {print $4}' /proc/mounts 2>/dev/null | grep -q '\brw\b'
}

# VERDICT: does the running system have a working controller?
controller_present() {
  compgen -G '/sys/class/bluetooth/hci*' >/dev/null 2>&1
}

# Did this boot's btintel probe fail to load the firmware?
journal_fw_failure() {
  have journalctl || return 1
  journalctl -k -b --no-pager 2>/dev/null | grep -q 'Failed to load Intel firmware file' || return 1
}

journal_fw_error() {
  have journalctl || { echo "(journalctl unavailable)"; return 0; }
  journalctl -k -b --no-pager 2>/dev/null | grep -m1 'Failed to load Intel firmware file' \
    || echo "(no firmware-load failure in this boot)"
}

# installed_state FILE -> prints one of: match | differ:<sha> | absent
installed_state() {
  local rel=$1 want=$2 cand
  for cand in "$FW_UPDATES/${rel##*/}" "$FW_MAIN/${rel##*/}"; do
    if [[ -f $cand ]]; then
      local got; got=$(sha_of "$cand")
      [[ $got == "$want" ]] && { echo "match:$cand"; return; }
      echo "differ:$cand:$got"; return
    fi
    # upstream/distro ship xz-compressed copies in firmware/intel
    if [[ -f "$cand.xz" ]] && have xz; then
      local got; got=$(xz -dc "$cand.xz" 2>/dev/null | sha256sum | cut -d' ' -f1)
      [[ $got == "$want" ]] && { echo "match:$cand.xz"; return; }
      echo "differ:$cand.xz:$got"; return
    fi
  done
  echo "absent"
}

# --- download ----------------------------------------------------------------
fetch_one() {
  local rel=$1 want_size=$2 want_sha=$3 src_list=() f base
  local out="$CACHE_DIR/${rel##*/}"

  for f in "${FROM_ARGS[@]}"; do
    if [[ -d $f && -f "$f/${rel##*/}" ]]; then src_list+=("$f/${rel##*/}"); fi
    if [[ -f $f && ${f##*/} == "${rel##*/}" ]]; then src_list+=("$f"); fi
  done

  if [[ ${#src_list[@]} -gt 0 ]]; then
    info "using local copy ${src_list[0]}"
    if [[ $(readlink -f "${src_list[0]}") != $(readlink -f "$out" 2>/dev/null || echo "$out") ]]; then
      cp -f "${src_list[0]}" "$out"
    fi
  else
    mkdir -p "$CACHE_DIR"
    local bases=()
    if [[ -n $URL_BASE ]]; then bases=("$URL_BASE"); else bases=("${URL_BASES[@]}"); fi
    local fetched=0
    for base in "${bases[@]}"; do
      info "downloading $base/$rel"
      if have curl; then
        if curl -fsSL --retry 3 --connect-timeout 20 -o "$out.part" "$base/$rel"; then
          mv -f "$out.part" "$out"; fetched=1; break
        fi
      elif have wget; then
        if wget -q -T 20 -O "$out.part" "$base/$rel"; then
          mv -f "$out.part" "$out"; fetched=1; break
        fi
      else
        die "need curl or wget"
      fi
      warn "mirror failed: $base/$rel"
    done
    [[ $fetched == 1 ]] || die "could not download $rel from any mirror"
  fi

  # verify before anything is installed
  local got_size got_sha
  got_size=$(size_of "$out"); got_sha=$(sha_of "$out")
  if [[ $got_size != "$want_size" ]]; then
    err "size mismatch for ${rel##*/}: got $got_size, want $want_size"; return 1
  fi
  if [[ $got_sha != "$want_sha" ]]; then
    err "sha256 mismatch for ${rel##*/}: got $got_sha, want $want_sha"; return 1
  fi
  ok "${rel##*/}: size $got_size, sha256 ${want_sha:0:16}... verified"
  VERIFIED+=("$out")
}

# --- reporting ---------------------------------------------------------------
report_state() {
  local rc=0 rel size sha state

  say "== controller =="
  if have lspci; then
    lspci -nn 2>/dev/null | grep -E '\[8086:e476\]' || warn "PCI 8086:e476 not found"
  fi
  local drv; drv=$(readlink -f /sys/bus/pci/drivers/btintel_pcie/0000:00:14.7/driver 2>/dev/null || true)
  say "driver:        ${drv:-<none>}"
  say "hci devices:   $(compgen -G '/sys/class/bluetooth/hci*' >/dev/null && ls -d /sys/class/bluetooth/hci* | xargs -r -n1 basename | tr '\n' ' ' || echo none)"
  say "firmware blob: ${UPSTREAM_VERSION}"

  say
  say "== firmware files =="
  for rel in "${WANT[@]}"; do
    read -r path size sha <<<"$rel"
    state=$(installed_state "$path" "$sha")
    case $state in
      match:*)  ok   "$(printf '%-32s %s' "${path##*/}" "verified ($(printf '%s' "$state" | cut -d: -f2-))")" ;;
      differ:*) warn "$(printf '%-32s %s' "${path##*/}" "present but different content: $(printf '%s' "$state" | cut -d: -f2-)")"; rc=1 ;;
      absent)   warn "$(printf '%-32s %s' "${path##*/}" "ABSENT (want sha256 ${sha:0:16}...)")"; rc=1 ;;
    esac
  done

  say
  say "== boot-time driver state (this boot) =="
  if journal_fw_failure; then
    warn "$(journal_fw_error)"
    if compgen -G '/sys/class/bluetooth/hci*' >/dev/null 2>&1; then
      say "  -> hci0 exists but carries no firmware (radio unusable: btmgmt lists 0 controllers)"
      say "  -> the files are on disk, so the probe ran before the ostree root (initramfs) was mounted"
      say "     fix now:   sudo systemctl stop bluetooth"
      say "                sudo modprobe -r ${MODULES[*]}"
      say "                sudo modprobe btintel_pcie && sudo systemctl start bluetooth"
      say "     fix boot:  sudo $PROG          # installs files + dracut config + reloads"
    fi
    rc=1
  else
    ok "no firmware-load failure in this boot"
  fi

  if ! bluetooth_ok; then rc=1; fi

  say
  say "== verdict =="
  if [[ $rc == 0 ]]; then ok "radio looks healthy"; else warn "action needed (see above)"; fi
  return "$rc"
}

bluetooth_ok() {
  if ! have btmgmt; then return 0; fi
  local out; out=$(timeout 5 btmgmt info 2>/dev/null || true)
  grep -qE '^\s*addr ' <<<"$out"
}

# --- privileged plan ---------------------------------------------------------
plan_and_run() {
  local dry=$1 rel path size sha src dst state
  local ostree=0; is_ostree && ostree=1

  say "== plan =="
  say "  firmware target : $FW_UPDATES/ (priority over $FW_MAIN/)"
  say "  initramfs       : --persist=$PERSIST"
  say "  reload          : ${DO_RELOAD}"
  say

  # 1. /usr writability (rpm-ostree deployments are read-only unless a usroverlay
  #    is active; oxp3-firmware ships these files as a layered local RPM instead).
  if ! usr_writable; then
    if [[ $ostree == 1 ]]; then
      info "sudo rpm-ostree usroverlay     # rw overlay for /usr; lost at reboot"
      warn "a usroverlay install is NOT durable; the durable route on this host is the"
      warn "layered local RPM (rpm-ostree install <pkg>.rpm), see docs/hw/bluetooth.md"
      [[ $dry == 0 ]] && sudo rpm-ostree usroverlay
    else
      die "$FW_UPDATES is not writable and this is not an ostree host"
    fi
  fi

  # 2. copy the verified blobs
  for rel in "${WANT[@]}"; do
    read -r path size sha <<<"$rel"
    state=$(installed_state "$path" "$sha")
    if [[ $state == match:* ]]; then
      ok "${path##*/} already installed at ${state#match:} - no copy needed"
      continue
    fi
    src="$CACHE_DIR/${path##*/}"; dst="$FW_UPDATES/${path##*/}"
    info "sudo install -Dm0644 -o root -g root '$src' '$dst'"
    if [[ $dry == 0 ]]; then
      sudo install -Dm0644 -o root -g root "$src" "$dst"
      if have selinuxenabled && selinuxenabled 2>/dev/null; then
        info "sudo restorecon -F '$dst'"
        sudo restorecon -F "$dst" || warn "restorecon failed (label may be wrong)"
      fi
    fi
  done

  # 3. make the blobs visible inside the initramfs (or keep the driver out of it)
  case $PERSIST in
    items)
      local items=""
      for rel in "${WANT[@]}"; do read -r path _ _ <<<"$rel"; items+=" /usr/lib/firmware/updates/$path"; done
      info "sudo tee $DRACUT_CONF   (install_items${items})"
      if [[ $dry == 0 ]]; then
        conf_write "$(printf '# oxp3-bt-firmware.sh: dracut cannot infer these names from modinfo\ninstall_items+="%s "\n' "$items")"
      fi
      ;;
    omit)
      info "sudo tee $DRACUT_CONF   (omit_drivers btintel_pcie)"
      if [[ $dry == 0 ]]; then
        conf_write "# oxp3-bt-firmware.sh: let udev probe after switch_root, where the blobs exist
omit_drivers+=" btintel_pcie ""
      fi
      ;;
    none) info "skipping initramfs config (--persist=none)" ;;
    *) die "bad --persist=$PERSIST" ;;
  esac

  if [[ $PERSIST != none ]]; then
    if [[ $ostree == 1 ]]; then
      info "sudo rpm-ostree initramfs --enable    # regenerate on next deployment/reboot"
    else
      info "sudo dracut -f --kver $(uname -r)"
    fi
    if [[ $dry == 0 ]]; then
      if [[ $ostree == 1 ]]; then
        sudo rpm-ostree initramfs --enable || warn "rpm-ostree initramfs --enable failed"
      else
        sudo dracut -f --kver "$(uname -r)" || warn "dracut failed"
      fi
    fi
  fi

  # 4. reload the transport so the driver picks the blobs up without a reboot
  if [[ $DO_RELOAD == 1 ]]; then
    info "sudo systemctl stop bluetooth"
    info "sudo modprobe -r ${MODULES[*]}"
    info "sudo modprobe btintel_pcie"
    info "sudo systemctl start bluetooth"
    if [[ $dry == 0 ]]; then
      sudo systemctl stop bluetooth 2>/dev/null || true
      sudo modprobe -r "${MODULES[@]}" 2>/dev/null || warn "modprobe -r failed (module busy?)"
      sudo modprobe btintel_pcie
      sudo systemctl start bluetooth 2>/dev/null || true
    fi
  fi

  # 5. verify
  if [[ $dry == 1 ]]; then
    say
    info "verification after the privileged steps:"
    say "    ls /sys/class/bluetooth/                 # expect hci0"
    say "    btmgmt info                              # expect a controller with an address"
    say "    journalctl -k -b | grep -i 'Bluetooth: hci0'"
    say "    $PROG --check"
    return 0
  fi

  say
  local tries=0
  while [[ $tries -lt 10 ]] && ! controller_present; do sleep 0.5; tries=$((tries + 1)); done

  say "== verification =="
  if controller_present; then
    ok "controller node: $(ls -d /sys/class/bluetooth/hci* | xargs -r -n1 basename | tr '\n' ' ')"
  else
    err "no /sys/class/bluetooth/hci* appeared"
  fi
  if journal_fw_failure; then
    err "driver still reports a firmware load failure:"; journal_fw_error >&2
  else
    ok "no firmware-load failure in the kernel log"
  fi
  if have btmgmt; then
    timeout 5 btmgmt info 2>/dev/null | sed 's/^/    /' || true
  fi
  if have bluetoothctl; then
    timeout 5 bluetoothctl show 2>/dev/null | sed -n '1,8p' | sed 's/^/    /' || true
  fi
  say
  say "next: 'bluetoothctl power on; bluetoothctl scan on' (or your desktop's BT panel)."
  say "note: the initramfs change only takes effect on the next boot/deployment."
}

# --- main --------------------------------------------------------------------
[[ $# -eq 0 ]] && set -- --dry-run

args_files=()
while [[ $# -gt 0 ]]; do
  case $1 in
    --check) MODE=check ;;
    --dry-run) MODE=dryrun ;;
    --from) shift; [[ $# -gt 0 ]] || die "--from needs a path"; FROM_ARGS+=("$1") ;;
    --cache) shift; CACHE_DIR=${1:?--cache needs a directory} ;;
    --url-base) shift; URL_BASE=${1:?--url-base needs a URL} ;;
    --with-ddc) WITH_DDC=1 ;;
    --persist) shift; PERSIST=${1:?--persist needs a mode} ;;
    --persist=*) PERSIST=${1#*=} ;;
    --no-initramfs) PERSIST=none ;;
    --no-reload) DO_RELOAD=0 ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

case $PERSIST in items|omit|none) ;; *) die "--persist must be items, omit or none" ;; esac

WANT=("${FILES[@]}")
[[ $WITH_DDC == 1 ]] && WANT+=("$DDC_FILE")

if [[ $MODE == check ]]; then
  report_state
  exit $?
fi

# fetch + verify (unprivileged, cache outside the repo)
VERIFIED=()
info "firmware set: $UPSTREAM_VERSION"
for rel in "${WANT[@]}"; do
  read -r path size sha <<<"$rel"
  fetch_one "$path" "$size" "$sha"
done

if [[ $MODE == dryrun ]]; then
  plan_and_run 1
  say
  say "dry run: nothing was written, no privilege was used."
  exit 0
fi

plan_and_run 0
