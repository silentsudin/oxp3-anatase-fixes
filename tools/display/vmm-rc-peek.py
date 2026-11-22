#!/usr/bin/python3 -I
# SPDX-License-Identifier: MIT
"""vmm-rc-peek - read the dock's Synaptics VMM62xx (Cayenne) converter state over DP AUX.

Read-only capture of what the converter's MSA watchdog compares (VMM6210 fw 6.05,
see docs/VMM6210-FIRMWARE-RE.md): the live incoming MSA registers, the firmware's
saved copy, its "MSA changed" counter, and the source-vendor type it derived from
the host's source OUI. Uses the Synaptics "remote control" (RC) DPCD interface the
way fwupd's synaptics-mst plugin does: ENABLE_RC ("PRIUS"), READ_FROM_MEMORY in
32-byte units, DISABLE_RC on exit. Nothing else is written.

    sudo tools/display/vmm-rc-peek.py [seconds]

While it runs, set the TV's Adaptive Sync to Always for ~15 s and back. Every change
of the sampled values is logged with a timestamp to ~<user>/oxp3-vmm-rc-<time>.log.
"""
import glob, os, struct, sys, time

RC_CMD, RC_RESULT, RC_LEN, RC_OFFSET, RC_DATA = 0x4B2, 0x4B3, 0x4B8, 0x4BC, 0x4C0
ENABLE_RC, DISABLE_RC, READ_FROM_MEMORY = 0x01, 0x02, 0x31

# (label, address, length) - firmware 6.05 addresses, see the RE doc
WATCH = [
    ("vendor_type", 0x90003DE8, 1),          # 1 = Intel, 2 = AMD, 4 = Apple ...
    ("msa_live_p0", 0x20220830, 20),         # 5 dwords compared by the watchdog
    ("msa_live_p1", 0x20220C30, 20),
    ("msa_saved_p0", 0x90000A1C, 20),
    ("msa_saved_p1", 0x90000A30, 20),
    ("msa_chg_cnt", 0x90006728, 2),          # per-port "MSA changed" debounce counters
]


def find_aux():
    for d in sorted(glob.glob("/sys/class/drm_dp_aux_dev/drm_dp_aux*")):
        try:
            name = open(f"{d}/name").read().strip()
        except OSError:
            continue
        if "USBC" in name or "TC" in name:
            return "/dev/" + os.path.basename(d), name
    sys.exit("no USB-C DP AUX device found (is the dock connected?)")


class Aux:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR)

    def rd(self, addr, n):
        return os.pread(self.fd, n, addr)

    def wr(self, addr, data):
        os.pwrite(self.fd, bytes(data), addr)

    def cmd(self, c):
        self.wr(RC_CMD, [c | 0x80])
        for _ in range(30):
            b = self.rd(RC_CMD, 2)
            if not b[0] & 0x80:
                return b[1]
            time.sleep(0.1)
        raise TimeoutError(f"RC command {c:#x} did not complete")

    def enable_rc(self):
        self.cmd(DISABLE_RC)  # fwupd disables first; "disabled" result is fine
        self.wr(RC_DATA, b"PRIUS")
        self.wr(RC_OFFSET, struct.pack("<I", 0))
        self.wr(RC_LEN, struct.pack("<I", 5))
        rc = self.cmd(ENABLE_RC)
        if rc != 0:
            raise RuntimeError(f"ENABLE_RC failed, result {rc}")

    def disable_rc(self):
        try:
            self.cmd(DISABLE_RC)
        except Exception:
            pass

    def mem(self, addr, n):
        out = b""
        while n > 0:
            k = min(n, 32)
            self.wr(RC_OFFSET, struct.pack("<I", addr))
            self.wr(RC_LEN, struct.pack("<I", k))
            rc = self.cmd(READ_FROM_MEMORY)
            if rc != 0:
                raise RuntimeError(f"READ_FROM_MEMORY {addr:#x} failed, result {rc}")
            out += self.rd(RC_DATA, k)
            addr += k
            n -= k
        return out


def dump_dpcd(aux, log, out):
    """Read-only dump of the source-ID and AS-SDP capability registers."""
    out("dpcd 0x300-0x30F (source OUI + device ID):")
    for base in range(0x300, 0x310, 16):
        b = aux.rd(base, 16)
        out(f"  {base:03x}: {b.hex(' ')}")
    oui = aux.rd(0x300, 3)
    out(f"  source OUI as seen by the converter: {oui.hex(':')}"
        f" (Intel=00:aa:01, AMD=00:00:1a, none/empty means never written)")
    ascap = aux.rd(0x2214, 1)
    bit0, bit2 = ascap[0] & 1, ascap[0] & 4
    out(f"dpcd 0x2214 DPRX_FEATURE_ENUMERATION_LIST_CONT_1 = {ascap[0]:#04x}:"
        f" AS-SDP supported={bool(bit0)}, FAVT-payload parsing={bool(bit2)}")
    out("dpcd 0x2200-0x223F (AS-SDP / DPRX feature area):")
    for base in range(0x2200, 0x2240, 16):
        b = aux.rd(base, 16)
        out(f"  {base:03x}: {b.hex(' ')}")


def main():
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 45
    path, name = find_aux()
    aux = Aux(path)
    chip = aux.rd(0x507, 2)
    user = os.environ.get("SUDO_USER") or "root"
    home = os.path.expanduser(f"~{user}")
    logp = os.path.join(home, time.strftime("oxp3-vmm-rc-%Y%m%d-%H%M%S.log"))
    log = open(logp, "w")

    def out(s):
        print(s)
        log.write(s + "\n")
        log.flush()

    out(f"aux {path} ({name}), chip id {chip.hex()} (6210 = VMM6210), {secs:.0f} s")
    if chip != b"\x62\x10":
        out("warning: not a VMM6210 - addresses are for VMM6210 fw 6.05")
    aux.enable_rc()
    out("RC enabled; sampling. Enter Gamemode and run a game with VRR for most of the window.")
    dump_dpcd(aux, log, out)
    last = {}
    t0 = time.monotonic()
    samples = 0
    warned = False
    try:
        while time.monotonic() - t0 < secs:
            t = time.monotonic() - t0
            try:
                for label, addr, n in WATCH:
                    v = aux.mem(addr, n).hex(" ")
                    if last.get(label) != v:
                        out(f"{t:7.2f}s {label:13s} {v}")
                        last[label] = v
                samples += 1
                warned = False
            except Exception as e:
                # AUX/RC failures are normal while the source re-trains (e.g. the
                # Gamemode modeset): wait and bring RC back up, keep sampling.
                if not warned:
                    out(f"{t:7.2f}s aux/RC unavailable ({e}); waiting for the link")
                    warned = True
                try:
                    time.sleep(0.5)
                    aux.enable_rc()
                    out(f"{t:7.2f}s RC re-enabled, resuming")
                except Exception:
                    pass
    finally:
        try:
            dump_dpcd(aux, log, out)
        finally:
            aux.disable_rc()
            out(f"done: {samples} samples in {time.monotonic() - t0:.1f} s; RC disabled. Log: {logp}")
            log.close()
        if user != "root":
            try:
                import pwd
                pw = pwd.getpwnam(user)
                os.chown(logp, pw.pw_uid, pw.pw_gid)
            except Exception:
                pass


if __name__ == "__main__":
    main()
