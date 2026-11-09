#!/usr/bin/env python3
"""oxp3-fp-probe - Betterlife BF63112 (ONEXPLAYER 3) fingerprint probe.

Read-only bring-up tool for the fingerprint sensor that Anatase/Linux currently
does not drive.  It has two transports:

``--transport spi`` (default) - the one this unit actually uses.  It speaks the
SPB/SPI wire format recovered from ``BetterlifeFingerprintDevice.dll``
(``FUN_180017020`` read / ``FUN_180017404`` write): a one-byte selector
``(opcode & 0x3f) << 1`` (bit 7 set for host->device writes), then the driver's
two-entry sequence (write the selector, then clock out the fixed-size answer or
write the payload).  See ``fpbf63112/spi.py`` and
docs/hw/fingerprint-transport.md.

``--transport frame`` - the older experiment that puts the USB branch's 0xAA /
CRC-32 frame on the SPI wire.  Kept only to test the "is the USB frame behind
the selector?" question; it is not the SPI protocol.

Safety: a real transfer needs an explicit ``--spidev`` path *and*
``--allow-writes``; the transport is write-gated even for reads.  Enrol/
identify/remove/JTAG/restore/firmware-update are refused unconditionally, both
by opcode and by the selector those opcodes produce (the selector carries only
six opcode bits, so 0x02 aliases JTAG's 0xC2).  ``--dry-run`` prints the exact
bytes and never opens a device or GPIO.  ``acpi_info`` parses an extracted
``_CRS`` resource template (the only root step) and prints the exact commands to
obtain it.

Requires: python3 (stdlib only, no python3-spidev) plus an spidev-bound node.
The sensor has no in-tree driver, so bind spidev by hand:

    sudo modprobe spidev
    echo spidev | sudo tee /sys/bus/spi/devices/spi-BF63112:00/driver_override
    echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/bind
    ls -l /dev/spidev0.0          # created by the bind

Then:

    sudo ./oxp3-fp-probe.py --dry-run fw info   # exact bytes, no hardware
    sudo ./oxp3-fp-probe.py --spidev /dev/spidev0.0 --allow-writes fw info
    sudo ./oxp3-fp-probe.py --spidev /dev/spidev0.0 --gpio 42 \
         --allow-writes fw                      # wait for the IRQ line too
    sudo ./oxp3-fp-probe.py --spidev /dev/spidev0.0 --gpio 18 \
         --gpio-chip gpiochip1 --reset-gpio 16 --allow-reset \
         --allow-writes --try-matrix --matrix-json /tmp/fp-matrix.json
    sudo ./oxp3-fp-probe.py acpi_info --crs /tmp/fprt-crs.bin

Undo:  echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/unbind

This is our own code and it is UNTESTED on hardware (see "Open points" in the
doc): the selector and transfer shapes match the driver byte for byte, but the
SPI command opcodes are data-driven inside the driver and are not yet known.
"""

import argparse
import ctypes
import fcntl
import hashlib
import os
import struct
import sys
import time
import zlib

# ---------------------------------------------------------------- spidev ABI
SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04
SPI_IOC_MESSAGE_1 = 0x40206B00  # _IOW('k', 0, struct spi_ioc_transfer[1])

_XFER = struct.Struct("<QQIIHBBBBBB")  # struct spi_ioc_transfer = 32 bytes


def _addr(buf):
    return ctypes.addressof((ctypes.c_char * len(buf)).from_buffer(buf))


class SpiDev:
    def __init__(self, path, speed_hz=1_000_000, mode=0, bits=8):
        self.fd = open(path, "r+b", buffering=0)
        fcntl.ioctl(self.fd, SPI_IOC_WR_MODE, struct.pack("<B", mode))
        fcntl.ioctl(self.fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("<B", bits))
        fcntl.ioctl(self.fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("<I", speed_hz))

    def transfer(self, tx, rx_len=None, speed_hz=0, delay_us=0):
        """One full-duplex SPI transfer.  `len` covers both directions, so the
        receive buffer is always sized to the outgoing length - the Windows
        driver always pushes and pulls whole frames."""
        tx = bytes(tx)
        n = max(len(tx), rx_len or 0, 1)
        self._tx = bytearray(tx) + bytearray(n - len(tx))
        self._rx = bytearray(n)
        msg = _XFER.pack(_addr(self._tx), _addr(self._rx), n, speed_hz,
                         delay_us, 8, 0, 0, 0, 0, 0)
        fcntl.ioctl(self.fd, SPI_IOC_MESSAGE_1, msg)
        return bytes(self._rx[:rx_len or n])

    def close(self):
        self.fd.close()


# ------------------------------------------------------- recovered protocol
SOF = 0xAA          # first byte of every host frame
CMD_MAGIC = 0x4C    # first byte of the command block
HDR_RESERVED = 16   # zero bytes between the length field and the command block

# opcode byte = descriptor+0x10 in the Windows driver.  Values come from the
# driver's dispatch table (RVA 0x46F00 in BetterlifeFingerprintDevice.dll
# 9.39.15.26), not from the order of its trace strings.  Only the read-only
# commands are wired up; the whole table is in docs/hw/fingerprint.md.
OPS = {
    "fw":     (0xA0, "CMD_QUERY_FIRMWARE_VER"),
    "scheme": (0xA1, "CMD_QUERY_MODULE_SCHEME_INFORMATION"),
    "count":  (0xB3, "CMD_QUERY_FINGERPRINT_COUNT"),
    "uid":    (0xC0, "CMD_QUERY_MODULE_UID"),
    "info":   (0xC1, "CMD_QUERY_SENSOR_INFO"),
}
DESTRUCTIVE = {0xB0: "ENROLL", 0xB1: "IDENTIFY", 0xB2: "REMOVE",
               0xC2: "JTAG", 0xC3: "RESTORE_MCU", 0xD0: "UPDATE_FIRMWARE",
               0xD1: "FW_WORK_MODE_SWITCH", 0xFF: "FW_WORK_MODE_SWITCH"}


def build_frame(opcode, phase=0, arg=0, data=b""):
    """Host->device frame, as assembled by FUN_180004358 + FUN_1800028f8:

        AA | L:u32 | 16 zero bytes | 4C op phase 00 arg:u16 len:u32 data | pad | CRC32
        L    = 20 + align16(10 + len(data))     (bytes following the SOF byte)
        pad  zero-fills the command block to a 16-byte multiple
        CRC  zlib CRC-32 over every byte before it, incl. the 0xAA
    """
    payload = bytes([CMD_MAGIC, opcode & 0xFF, phase & 0xFF, 0x00])
    payload += struct.pack("<H", arg & 0xFFFF)
    payload += struct.pack("<I", len(data))
    payload += data
    aligned = (len(payload) + 15) // 16 * 16
    payload += b"\x00" * (aligned - len(payload))

    frame = bytes([SOF]) + struct.pack("<I", 20 + aligned)
    frame += b"\x00" * HDR_RESERVED + payload
    frame += struct.pack("<I", zlib.crc32(frame) & 0xFFFFFFFF)
    return frame


def parse_response(buf):
    """Split a device answer into (header dict, body).  Best effort.

    Assumed layout, mirroring the request:
        AA | L:u32 | 16-byte device header | 4C op phase status | body
    The Windows handlers receive `body` directly (their first byte is the
    status/parameter field, not the 0x4C magic), which is why the body starts
    after the 4-byte command block.
    """
    if not buf:
        return None, b""
    if buf[0] != SOF:
        return None, buf
    hdr = {"len": struct.unpack_from("<I", buf, 1)[0] if len(buf) >= 5 else None}
    if len(buf) < 25:
        return hdr, b""
    blk = buf[21:25]
    hdr.update(magic=blk[0], op=blk[1], phase=blk[2], status=blk[3])
    return hdr, buf[25:]


def query(spi, opcode, arg=0, data=b"", wait_ms=40, rx_len=64, retries=3,
          verbose=False):
    frame = build_frame(opcode, 0, arg, data)
    if verbose:
        print(f"    tx {len(frame):3d}B: {frame.hex(' ')}")
    spi.transfer(frame)                    # push the command
    if wait_ms:
        time.sleep(wait_ms / 1000.0)
    resp = b""
    for _ in range(max(retries, 1)):       # the device raises an IRQ we cannot
        resp = spi.transfer(b"\x00" * rx_len, rx_len=rx_len)  # see without the
        if resp and resp[0] == SOF:        # GPIO line, so poll instead
            break
        time.sleep(wait_ms / 1000.0)
    return parse_response(resp), resp


# ------------------------------------------------------------- interpretations
def show_info(body):
    if len(body) < 9:
        return "short sensor-info body"
    a, b = struct.unpack_from("<HH", body, 0)
    w, h = struct.unpack_from("<HH", body, 4)
    return (f"fields a={a} b={b}  sensor={w}x{h}  image={w * h} B  "
            f"flags=0x{body[8]:02x}")


def show_fw(body):
    if len(body) < 7:
        return "short firmware body"
    projects = {1: "BOE MOC", 2: "HUAHONG MOUSE MOC", 3: "DYW MOH",
                4: "NATION TEST", 6: "NATIONALCHIP"}
    ver = ".".join(str(x) for x in body[3:7])
    return (f"fw={ver} project={projects.get(body[1], 'unknown')} "
            f"variant={body[2]} mode={body[0]} raw={body[3:7].hex()}")


# --------------------------------------------------------------- session crypto
def _load_crypto():
    """Import the session-crypto helper (same directory) on demand.

    The read-only queries below need nothing but the standard library; only
    ``--crypto-selftest`` and ``--session`` touch the SM2/SM3/SM4 module, so a
    copy of this single file still works for bring-up.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from fpbf63112 import crypto
    return crypto


def _load_protocol():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from fpbf63112 import protocol
    return protocol


def _load_spi():
    """Import the SPI transport (no I/O happens at import)."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from fpbf63112 import spi
    return spi


def _load_acpi():
    """Import the ACPI resource parser (pure byte parsing, no I/O)."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from fpbf63112 import acpi
    return acpi


def _load_matrix():
    """Import the polarity/settle/edge matrix planner and runner."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from fpbf63112 import matrix
    return matrix


class SpiTransport:
    """The ``protocol.Session`` transport contract over an ``SpiDev``.

    ``exchange()`` sends one frame and polls the readback until a complete
    ``0xAA`` frame (``L + 5`` bytes) has arrived or the timeout expires.  The
    module raises a GPIO we cannot see without the ACPI ``GpioInt``, so this
    polls - the same compromise as :func:`query`.
    """

    def __init__(self, spi, rx_len=256, wait_ms=40, retries=8, verbose=False):
        self.spi = spi
        self.rx_len = rx_len
        self.wait_ms = wait_ms
        self.retries = retries
        self.verbose = verbose
        self.last_raw = b""

    def exchange(self, frame, *, timeout_ms=2000):
        if self.verbose:
            print(f"    tx {len(frame):3d}B: {frame.hex(' ')}")
        self.spi.transfer(frame)
        deadline = time.monotonic() + max(timeout_ms, self.wait_ms) / 1000.0
        buf = b""
        tries = 0
        while time.monotonic() < deadline and tries < max(self.retries, 1):
            tries += 1
            if self.wait_ms:
                time.sleep(self.wait_ms / 1000.0)
            chunk = self.spi.transfer(b"\x00" * self.rx_len, rx_len=self.rx_len)
            if not chunk:
                continue
            if not buf:
                start = chunk.find(bytes([SOF]))
                if start < 0:
                    continue
                buf = chunk[start:]
            else:
                buf += chunk
            if len(buf) >= 5:
                need = struct.unpack_from("<I", buf, 1)[0] + 5
                if len(buf) >= need:
                    self.last_raw = buf[:need]
                    return self.last_raw
        self.last_raw = buf
        return buf


def crypto_selftest():
    """``--crypto-selftest``: prove the SM2/SM3/SM4/KDF code, then benchmark."""
    crypto = _load_crypto()
    print("BF63112 session crypto self-test (stdlib only; no hardware involved)")
    try:
        results = crypto.selftest()
    except crypto.CryptoError as exc:
        print(f"  FAILED: {exc}")
        return 1
    for name in sorted(results):
        value = results[name]
        print(f"  ok  {name:<12} {value[:48]}{'...' if len(value) > 48 else ''}")
    perf = crypto.benchmark()
    size = perf["size_bytes"]
    print(f"  benchmark on {size} B (best of 8 runs):")
    print(f"    SM3  {perf['sm3_seconds'] * 1000:7.2f} ms  "
          f"({perf['sm3_mbps']:.2f} MB/s)")
    print(f"    SM4  {perf['sm4_seconds'] * 1000:7.2f} ms  "
          f"({perf['sm4_mbps']:.2f} MB/s)")
    print(f"    SM2 keygen {perf['sm2_keygen_seconds'] * 1000:.2f} ms")
    print("  all published vectors passed")
    return 0


def session_attempt(spi, args):
    """``--session``: one 0xA2 key exchange, then print the derived-key digest.

    The session key itself is never printed - only a SHA-256 fingerprint - so a
    pasted log cannot be replayed.  ``0xA2`` is a key exchange: it writes no
    template, changes no mode and cannot brick the module.
    """
    crypto = _load_crypto()
    protocol = _load_protocol()
    transport = SpiTransport(spi, rx_len=args.session_rx, wait_ms=args.wait_ms,
                             retries=args.retries, verbose=args.raw_info)
    session = protocol.Session(transport=transport, secure=args.session_secure,
                               timeout_ms=max(args.session_timeout, 100))
    print(f"session: 0xA2 key exchange, {'secure' if args.session_secure else 'plaintext'}"
          f" request, rx={args.session_rx}B, timeout={session.timeout_ms}ms")
    try:
        secure = session.open_session()
    except (protocol.ProtocolError, crypto.CryptoError) as exc:
        print(f"session: FAILED - {exc}")
        raw = transport.last_raw
        if raw:
            print(f"session: raw answer ({len(raw)}B): {raw.hex(' ')}")
            answer = _try_variant_search(crypto, protocol, session, raw)
            if answer:
                print(f"session: {answer}")
        print("session: no key was accepted; nothing on the module was changed")
        return 1
    fingerprint = hashlib.sha256(secure.key).hexdigest()[:16]
    print(f"session: established, key fingerprint sha256[:16]={fingerprint}")
    print("session: this session object can now seal/unseal secure frames")
    return 0


def _try_variant_search(crypto, protocol, session, raw):
    """Bounded offline retry of the KDF/tag variants against one captured answer."""
    for frame in session._answer_candidates(raw):
        if frame.opcode != int(protocol.Command.QUERY_ROOT_KEY):
            continue
        try:
            answer = protocol.RootKeyAnswer.parse(frame.body)
        except protocol.ProtocolError:
            continue
        hits = crypto.search_variants(session.keypair, answer)
        if not hits:
            return ("variant search: none of the bounded KDF/tag variants matched "
                    "this answer (the module may use a different exchange)")
        best = hits[0]
        return (f"variant search: {len(hits)} match(es); first is "
                f"kdf_input={best.params.kdf_input} "
                f"tag_layout={best.params.tag_layout} "
                f"klen={best.key_len} - re-run with that variant")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("commands", nargs="*",
                    help=", ".join(sorted(set(OPS) | {"acpi_info"})))
    ap.add_argument("--transport", choices=("spi", "frame"), default="spi",
                    help="spi (the recovered SPB transport, default) or the "
                         "old 0xAA/CRC experiment")
    ap.add_argument("--spidev", "--dev", dest="spidev", default=None,
                    help="explicit spidev node, e.g. /dev/spidev0.0; required "
                         "for any real transfer (there is no default)")
    ap.add_argument("--speed", type=int, default=None,
                    help="SPI clock in Hz; omitted means leave the controller "
                         "default (values come from _CRS)")
    ap.add_argument("--mode", type=int, default=None, choices=[0, 1, 2, 3],
                    help="SPI mode; omitted means leave the controller default")
    ap.add_argument("--bits", type=int, default=None,
                    help="bits per word; omitted means leave the default")
    ap.add_argument("--gpio", type=int, default=None,
                    help="GpioInt line number (the sensor interrupt)")
    ap.add_argument("--gpio-chip", default=None,
                    help="gpiochip name (gpiochipN), /dev path, or chip label")
    ap.add_argument("--gpio-backend", default=None,
                    choices=("auto", "gpiod", "chardev", "sysfs", "poll"),
                    help="force the GPIO backend instead of auto "
                         "(gpiod -> chardev -> sysfs -> poll for the IRQ, "
                         "gpiod -> chardev -> sysfs for reset; 'poll' is "
                         "IRQ-only). Default: $FPBF63112_GPIO_BACKEND or auto")
    ap.add_argument("--irq-edge", choices=("rising", "falling", "both"),
                    default=None, help="logical assertion edge")
    ap.add_argument("--reset-polarity", choices=("active-high", "active-low"),
                    default="active-high",
                    help="reset line polarity (the settled value is "
                         "active-high; default active-high)")
    ap.add_argument("--settle-ms", type=int, default=10,
                    help="settle delay in ms after the reset pulse before the "
                         "first transfer (default 10, the known-good value)")
    ap.add_argument("--irq-active-low", action="store_true",
                    help="the interrupt line is asserted low")
    ap.add_argument("--reset-gpio", type=int, default=None,
                    help="GpioIo reset line number (requires --allow-reset)")
    ap.add_argument("--allow-reset", action="store_true",
                    help="permit pulsing the reset line on open")
    ap.add_argument("--allow-writes", action="store_true",
                    help="explicit opt-in required before any SPI transfer "
                         "(even a read): the transport is write-gated")
    ap.add_argument("--allow-op", action="store_true",
                    help="extend the read allowlist with the single --op "
                         "opcode (never a destructive one)")
    ap.add_argument("--crs", default=None,
                    help="binary _CRS resource template; acpi_info parses it, "
                         "and a hardware run uses its SPI/GPIO values")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact plans; never opens a device or GPIO")
    ap.add_argument("--wait-ms", type=int, default=40)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--rx", type=int, default=64, help="bytes to read back")
    ap.add_argument("--op", type=lambda s: int(s, 0),
                    help="send one raw read opcode instead of a named command")
    ap.add_argument("--arg", type=int, default=0)
    ap.add_argument("--data", default="", help="hex payload for --op")
    ap.add_argument("--raw-info", action="store_true", help="hexdump frames")
    ap.add_argument("--crypto-selftest", action="store_true",
                    help="prove SM2/SM3/SM4/KDF against published vectors and "
                         "benchmark; needs no device and touches nothing")
    ap.add_argument("--session", action="store_true",
                    help="attempt the 0xA2 SM2 session handshake (frame "
                         "transport only; read-only key exchange)")
    ap.add_argument("--session-secure", action="store_true",
                    help="send the 0xA2 request in the vendor's secure-frame "
                         "form (SM4 with the initial zero key)")
    ap.add_argument("--session-rx", type=int, default=256,
                    help="readback bytes for --session (a 0xA2 answer needs ~153)")
    ap.add_argument("--session-timeout", type=int, default=2000,
                    help="--session answer timeout in ms")
    ap.add_argument("--try-matrix", action="store_true",
                    help="try reset polarity (active-low/active-high/hold), "
                         "post-reset settle (10/150 ms) and IRQ edge "
                         "(rising/falling/both) in one bounded run; sends only "
                         "0xA0 (then 0xA1/0xC1 on a non-zero answer)")
    ap.add_argument("--matrix-json", default=None,
                    help="write the matrix table to this JSON file (default: "
                         "matrix.json in the current directory)")
    ap.add_argument("--matrix-timeout-ms", type=int, default=1500,
                    help="per-attempt matrix timeout in ms (default 1500)")
    ap.add_argument("--try-write-shape", action="store_true",
                    help="find the correct read write shape: selector bit 0 "
                         "(clear/set) x padding after the selector "
                         "(0..4 zero bytes + the phase/arg hypothesis) x read "
                         "length (layout/64 B), for 0xA0 then 0xA1/0xC1; uses "
                         "--reset-polarity/--settle-ms/--irq-edge and a short "
                         "per-cell timeout")
    ap.add_argument("--write-shape-json", default=None,
                    help="write the write-shape table to this JSON file "
                         "(default: write-shape.json in the current directory)")
    ap.add_argument("--write-shape-timeout-ms", type=int, default=400,
                    help="per-cell write-shape timeout in ms (default 400)")
    ap.add_argument("--try-link", action="store_true",
                    help="sweep the SPI link with the reset settings pinned: "
                         "modes 0/1/2/3 x 1/4 MHz x 10/150 ms (plus 300 ms "
                         "with --link-settles) and read 0xA0/0xC1/0xA1 with "
                         "the DLL-exact read shape; table -> link.json")
    ap.add_argument("--link-json", default=None,
                    help="write the link sweep table to this JSON file "
                         "(default: link.json in the current directory)")
    ap.add_argument("--link-timeout-ms", type=int, default=400,
                    help="per-cell link-sweep timeout in ms (default 400)")
    ap.add_argument("--link-settles", default="10,150",
                    help="comma-separated settle delays after reset in ms "
                         "(default 10,150; add 300 for the optional third "
                         "value)")
    ap.add_argument("--read-shape", choices=("frame", "selector"),
                    default="frame",
                    help="read frame: 'frame' (default) = the selector plus "
                         "the observed lead bytes (3 for 0xA0, 2 for 0xA1, "
                         "0xC1 unknown), so the payload starts at answer "
                         "offset 0; 'selector' = the DLL-exact one-byte "
                         "selector for comparison")
    args = ap.parse_args(argv)

    if args.crypto_selftest:
        return crypto_selftest()

    if args.try_matrix and args.try_write_shape:
        print("refusing: --try-matrix and --try-write-shape are separate "
              "experiments; run one at a time", file=sys.stderr)
        return 2

    if args.try_link:
        if args.transport != "spi":
            print("refusing: --try-link is the SPI link sweep; the frame "
                  "transport has no mode/speed semantics", file=sys.stderr)
            return 2
        if args.try_matrix or args.try_write_shape:
            print("refusing: --try-link is a separate experiment; run one at "
                  "a time", file=sys.stderr)
            return 2
        if args.commands:
            print("refusing: --try-link sends its own allowlisted queries "
                  "(0xA0/0xC1/0xA1); do not name commands", file=sys.stderr)
            return 2
        if args.op is not None:
            print("refusing: --try-link does not take --op", file=sys.stderr)
            return 2
        if not 100 <= args.link_timeout_ms <= 2000:
            print("refusing: --link-timeout-ms must be 100..2000",
                  file=sys.stderr)
            return 2
        return run_spi_link(args)

    if args.try_write_shape:
        if args.transport != "spi":
            print("refusing: --try-write-shape is the SPI write-shape "
                  "experiment; the frame transport has no selector/padding "
                  "shape", file=sys.stderr)
            return 2
        if args.commands:
            print("refusing: --try-write-shape sends its own allowlisted "
                  "queries (0xA0/0xA1/0xC1); do not name commands",
                  file=sys.stderr)
            return 2
        if args.op is not None:
            print("refusing: --try-write-shape does not take --op",
                  file=sys.stderr)
            return 2
        if not 100 <= args.write_shape_timeout_ms <= 2000:
            print("refusing: --write-shape-timeout-ms must be 100..2000",
                  file=sys.stderr)
            return 2
        return run_spi_write_shape(args)

    if args.try_matrix:
        if args.transport != "spi":
            print("refusing: --try-matrix is the SPI bring-up matrix; the "
                  "frame transport has no selector/reset semantics",
                  file=sys.stderr)
            return 2
        if args.commands:
            print("refusing: --try-matrix sends its own allowlisted queries "
                  "(0xA0/0xA1/0xC1); do not name commands",
                  file=sys.stderr)
            return 2
        if args.op is not None:
            print("refusing: --try-matrix does not take --op", file=sys.stderr)
            return 2
        if not 100 <= args.matrix_timeout_ms <= 10000:
            print("refusing: --matrix-timeout-ms must be 100..10000",
                  file=sys.stderr)
            return 2
        return run_spi_matrix(args)

    cmds = args.commands or (["raw"] if args.op is not None else ["info"])
    if args.transport == "spi":
        if "acpi_info" in cmds:
            return acpi_info(args, cmds)
        if args.session:
            print("refusing: --session runs the USB-branch 0xAA frame, which "
                  "is not the SPI transport; use --transport frame",
                  file=sys.stderr)
            return 2
        return run_spi(args, cmds)
    return run_frame(args, cmds)


# ---------------------------------------------------------------- SPI command
def _refuse_destructive(spi, opcode):
    """Raise ``UnsafeCommand`` for a destructive opcode *or its selector."""
    low = spi.selector(opcode) & 0x7F
    if opcode in spi.DESTRUCTIVE_OPS:
        raise spi.UnsafeCommand(
            f"opcode 0x{opcode:02x} is {spi.DESTRUCTIVE_OPS[opcode]}: "
            f"refusing unconditionally")
    aliases = spi.FORBIDDEN_SELECTORS.get(low)
    if aliases:
        names = ", ".join(f"0x{o:02x} {spi.DESTRUCTIVE_OPS[o]}" for o in aliases)
        raise spi.UnsafeCommand(
            f"selector 0x{low:02x} (opcode 0x{opcode:02x}) is reserved for "
            f"{names}: refusing unconditionally")


def _spi_plan(spi, args, cmd):
    """Return ``(label, SpiPlan)`` for one command; never performs I/O."""
    shape = getattr(args, "read_shape", "frame")
    if cmd == "raw":
        if args.op is None:
            raise spi.SpiError("--op is required for the raw command")
        if not 0 <= args.op <= 0xFF:
            raise spi.SpiError(f"--op must be 0..0xff, not {args.op!r}")
        if args.rx <= 0:
            raise spi.SpiError("--rx must be > 0 for a raw read")
        if args.data:
            raise spi.SpiError("SPI writes are deliberately not implemented; "
                               "--data belongs to the frame transport")
        _refuse_destructive(spi, args.op)
        if shape == "frame":
            return (f"op 0x{args.op:02x}",
                    spi.build_read_frame(args.op, args.rx).plan)
        return (f"op 0x{args.op:02x}",
                spi.build_plan(args.op, operation="read",
                               read_len=args.rx))
    if cmd in spi.COMMANDS:
        command = spi.COMMANDS[cmd]
        _refuse_destructive(spi, command.opcode)
        if shape == "frame":
            return (cmd, spi.build_read_frame(
                command.opcode, command.read_len,
                timeout_ms=command.timeout_ms,
                total_ms=command.total_ms).plan)
        return (cmd, spi.plan_command(command))
    raise spi.SpiError(f"unknown command {cmd!r}; choose from "
                       f"{', '.join(spi.COMMANDS)}")


def _crs_connection(args, spi, acpi):
    """Parse ``--crs`` if given; return ``(template, connection)`` or Nones."""
    if not args.crs:
        return None, None
    try:
        with open(args.crs, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise spi.SpiError(f"cannot read --crs {args.crs}: {exc}") from exc
    try:
        template = acpi.parse_resource_template(raw)
    except acpi.AcpiError as exc:
        raise spi.SpiError(f"--crs {args.crs}: {exc}") from exc
    return template, acpi.spi_connection(template)


def _spi_parameters(args, spi, acpi):
    """Resolve CLI values, falling back to the parsed _CRS (no guessing)."""
    _template, connection = _crs_connection(args, spi, acpi)
    speed = args.speed
    mode = args.mode
    bits = args.bits
    gpio = args.gpio
    reset_gpio = args.reset_gpio
    edge = args.irq_edge
    active_low = args.irq_active_low
    chip_select = None
    if connection is not None:
        if speed is None:
            speed = connection.speed_hz
        if mode is None:
            mode = connection.mode
        if bits is None:
            bits = connection.bits_per_word
        if gpio is None:
            gpio = connection.irq_pin
        if reset_gpio is None:
            reset_gpio = connection.reset_pin
        if edge is None and connection.irq_triggering is not None:
            edge = connection.irq_edge
        if not active_low and connection.irq_active_low is not None:
            active_low = connection.irq_active_low
        chip_select = connection.chip_select
    return dict(speed=speed, mode=mode, bits=bits, gpio=gpio,
                reset_gpio=reset_gpio, edge=edge or "rising",
                active_low=active_low, chip_select=chip_select)


def run_spi_matrix(args):
    """``--try-matrix``: polarity x settle x edge, read-only.

    The matrix is gated exactly like a normal read (explicit device path,
    ``--allow-writes``, and ``--allow-reset`` for the cells that pulse the
    reset) and it can only send 0xA0/0xA1/0xC1.  ``--dry-run`` prints the
    matrix without opening a device or a GPIO.
    """
    spi = _load_spi()
    acpi = _load_acpi()
    matrix = _load_matrix()
    try:
        params = _spi_parameters(args, spi, acpi)
        cells = matrix.plan_matrix()
    except (spi.SpiError, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"dry-run: matrix spidev={args.spidev or '(unset)'} "
              f"allow_writes={args.allow_writes} "
              f"gpio_backend={args.gpio_backend or 'auto'}")
        print(f"         speed={params['speed']} mode={params['mode']} "
              f"bits={params['bits']} chip={args.gpio_chip or '(unset)'} "
              f"irq={params['gpio']} reset={params['reset_gpio']} "
              f"timeout={args.matrix_timeout_ms}ms")
        print(f"matrix: {len(cells)} cells "
              f"(polarity x settle x edge); queries 0xA0, then 0xA1/0xC1 "
              f"only on a non-zero answer")
        for cell in cells:
            reset = "pulse" if cell.pulse else "hold"
            print(f" {cell.index:>2} {cell.polarity:<12} {reset:<5} "
                  f"settle={cell.settle_ms:>3}ms edge={cell.edge:<7} "
                  f"-> 0xA0 {matrix.MATRIX_QUERIES[0].read_len}B")
        print("matrix: success = a parsed answer (0xAA/L/CRC-32 frame or the "
              "recovered bare layout), not merely non-zero")
        print("dry-run: no device or GPIO is opened; no JSON is written")
        return 0

    if not args.spidev:
        print("refusing: --spidev /dev/spidevN.M is required for a real "
              "matrix (there is no default device)", file=sys.stderr)
        return 2
    if not args.allow_writes:
        print("refusing: --allow-writes is required before any SPI transfer "
              "(even a read)", file=sys.stderr)
        return 2
    if params["gpio"] is None:
        print("refusing: --try-matrix needs the GpioInt line (--gpio N or a "
              "_CRS that carries it); the edge is part of the matrix",
              file=sys.stderr)
        return 2
    if params["reset_gpio"] is None:
        print("refusing: --try-matrix needs the GpioIo reset line "
              "(--reset-gpio N or a _CRS that carries it)", file=sys.stderr)
        return 2
    if not args.allow_reset:
        print("refusing: --try-matrix pulses the reset line in the two "
              "polarity combos; pass --allow-reset", file=sys.stderr)
        return 2
    if args.gpio_backend == "poll":
        print("refusing: --gpio-backend poll cannot drive the reset line; "
              "use auto, chardev, gpiod or sysfs", file=sys.stderr)
        return 2

    out_path = os.path.abspath(args.matrix_json or "matrix.json")
    parent = os.path.dirname(out_path) or "."
    if not os.path.isdir(parent):
        print(f"refusing: the --matrix-json directory does not exist: "
              f"{parent}", file=sys.stderr)
        return 2

    print(f"matrix: {len(cells)} cells, per-attempt timeout "
          f"{args.matrix_timeout_ms}ms, read-only queries 0xA0/0xA1/0xC1")
    print(f"matrix: spidev={args.spidev} chip={args.gpio_chip or '(unset)'} "
          f"irq={params['gpio']} reset={params['reset_gpio']} "
          f"irq_active_low={bool(params['active_low'])}")

    def connect(cell):
        return matrix.SpiMatrixSession(
            cell, spidev_path=args.spidev,
            speed_hz=params["speed"], mode=params["mode"],
            bits_per_word=params["bits"], gpio_chip=args.gpio_chip,
            reset_pin=params["reset_gpio"], irq_pin=params["gpio"],
            irq_active_low=bool(params["active_low"]),
            timeout_ms=args.matrix_timeout_ms,
            gpio_backend=args.gpio_backend)

    context = {
        "spidev": args.spidev,
        "gpio_chip": args.gpio_chip,
        "irq_pin": params["gpio"],
        "reset_pin": params["reset_gpio"],
        "irq_active_low": bool(params["active_low"]),
        "speed_hz": params["speed"],
        "mode": params["mode"],
        "bits_per_word": params["bits"],
        "gpio_backend": args.gpio_backend or "auto",
    }
    try:
        report = matrix.run_matrix(
            cells, connect=connect, timeout_ms=args.matrix_timeout_ms,
            context=context)
    except matrix.MatrixRefused as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2

    for line in report.render_table():
        print(line)
    print(f"matrix: {report.verdict}")
    try:
        matrix.write_report(report, out_path)
    except OSError as exc:
        print(f"matrix: could not write {out_path}: {exc}", file=sys.stderr)
        return 1
    print(f"matrix: table written to {out_path}")
    return 0 if report.winning is not None else 1


def run_spi_write_shape(args):
    """``--try-write-shape``: find the selector/padding/read-length shape.

    The electrical settings are pinned (not swept): ``--reset-polarity``,
    ``--settle-ms`` and ``--irq-edge``.  Every cell is a read-only 0xA0 (then
    0xA1/0xC1) with a short timeout, so the whole experiment stays bounded.
    """
    spi = _load_spi()
    acpi = _load_acpi()
    matrix = _load_matrix()
    try:
        params = _spi_parameters(args, spi, acpi)
        cells = matrix.plan_write_shape()
    except (spi.SpiError, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"dry-run: write-shape spidev={args.spidev or '(unset)'} "
              f"allow_writes={args.allow_writes} "
              f"gpio_backend={args.gpio_backend or 'auto'}")
        print(f"         speed={params['speed']} mode={params['mode']} "
              f"bits={params['bits']} chip={args.gpio_chip or '(unset)'} "
              f"irq={params['gpio']} reset={params['reset_gpio']} "
              f"polarity={args.reset_polarity} settle={args.settle_ms}ms "
              f"edge={params['edge']} timeout={args.write_shape_timeout_ms}ms")
        print(f"write-shape: {len(cells)} cells (0xA0 then 0xA1/0xC1; "
              f"selector bit0 x padding x read length)")
        for cell in cells:
            print(f" {cell.index:>2} op=0x{cell.query.opcode:02x} "
                  f"selector=0x{cell.selector:02x} "
                  f"padding={cell.padding_label:<5} read={cell.read_len:>2}B")
        print("write-shape: success = a 0xAA/L/CRC-32 frame at any offset, or "
              "the recovered bare layout at read offset 0")
        print("dry-run: no device or GPIO is opened; no JSON is written")
        return 0

    if not args.spidev:
        print("refusing: --spidev /dev/spidevN.M is required for a real "
              "write-shape experiment (there is no default device)",
              file=sys.stderr)
        return 2
    if not args.allow_writes:
        print("refusing: --allow-writes is required before any SPI transfer "
              "(even a read)", file=sys.stderr)
        return 2
    if params["gpio"] is None:
        print("refusing: --try-write-shape needs the GpioInt line (--gpio N "
              "or a _CRS that carries it)", file=sys.stderr)
        return 2
    if params["reset_gpio"] is None:
        print("refusing: --try-write-shape needs the GpioIo reset line "
              "(--reset-gpio N or a _CRS that carries it)", file=sys.stderr)
        return 2
    if not args.allow_reset:
        print("refusing: --try-write-shape pulses the reset line; pass "
              "--allow-reset", file=sys.stderr)
        return 2
    if args.gpio_backend == "poll":
        print("refusing: --gpio-backend poll cannot drive the reset line; "
              "use auto, chardev, gpiod or sysfs", file=sys.stderr)
        return 2

    out_path = os.path.abspath(args.write_shape_json or "write-shape.json")
    parent = os.path.dirname(out_path) or "."
    if not os.path.isdir(parent):
        print(f"refusing: the --write-shape-json directory does not exist: "
              f"{parent}", file=sys.stderr)
        return 2

    print(f"write-shape: {len(cells)} cells, per-cell timeout "
          f"{args.write_shape_timeout_ms}ms, read-only queries 0xA0/0xA1/0xC1")
    print(f"write-shape: spidev={args.spidev} chip={args.gpio_chip or '(unset)'} "
          f"irq={params['gpio']} reset={params['reset_gpio']} "
          f"polarity={args.reset_polarity} settle={args.settle_ms}ms "
          f"edge={params['edge']}")

    active_low = args.reset_polarity == "active-low"

    def connect(cell):
        return matrix.SpiWriteShapeSession(
            cell, spidev_path=args.spidev,
            reset_active_low=active_low, settle_ms=args.settle_ms,
            edge=params["edge"], speed_hz=params["speed"],
            mode=params["mode"], bits_per_word=params["bits"],
            gpio_chip=args.gpio_chip, reset_pin=params["reset_gpio"],
            irq_pin=params["gpio"],
            irq_active_low=bool(params["active_low"]),
            timeout_ms=args.write_shape_timeout_ms,
            gpio_backend=args.gpio_backend)

    context = {
        "spidev": args.spidev,
        "gpio_chip": args.gpio_chip,
        "irq_pin": params["gpio"],
        "reset_pin": params["reset_gpio"],
        "reset_polarity": args.reset_polarity,
        "settle_ms": args.settle_ms,
        "irq_edge": params["edge"],
        "irq_active_low": bool(params["active_low"]),
        "speed_hz": params["speed"],
        "mode": params["mode"],
        "bits_per_word": params["bits"],
        "gpio_backend": args.gpio_backend or "auto",
    }
    try:
        report = matrix.run_write_shape(
            cells, connect=connect, timeout_ms=args.write_shape_timeout_ms,
            context=context)
    except matrix.MatrixRefused as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2

    for line in report.render_table():
        print(line)
    print(f"write-shape: {report.verdict}")
    try:
        matrix.write_shape_report(report, out_path)
    except OSError as exc:
        print(f"write-shape: could not write {out_path}: {exc}",
              file=sys.stderr)
        return 1
    print(f"write-shape: table written to {out_path}")
    return 0 if report.winning is not None else 1


def _parse_settles(text):
    """Parse ``--link-settles`` ("10,150,300") into a tuple of ms."""
    items = [item for item in str(text).replace(",", " ").split() if item]
    if not items:
        raise ValueError("--link-settles needs at least one delay in ms")
    settles = []
    for item in items:
        try:
            value = int(item)
        except ValueError:
            raise ValueError(f"--link-settles: {item!r} is not an integer "
                             f"number of milliseconds") from None
        if not 0 <= value <= 5000:
            raise ValueError(f"--link-settles: {value} ms is outside 0..5000")
        settles.append(value)
    return tuple(settles)


def run_spi_link(args):
    """``--try-link``: sweep SPI mode x clock x reset settle, read-only.

    The reset settings are pinned (polarity from ``--reset-polarity``, IRQ edge
    from ``--irq-edge``); only the three link parameters move.  Every cell reads
    the DLL-exact frame for the allowlisted 0xA0 (7 B), 0xC1 (9 B) and 0xA1
    (64 B).
    """
    spi = _load_spi()
    acpi = _load_acpi()
    matrix = _load_matrix()
    try:
        params = _spi_parameters(args, spi, acpi)
        settles = _parse_settles(args.link_settles)
        cells = matrix.plan_link(settles_ms=settles)
    except (spi.SpiError, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    # The recovered link is 8 bits per word; the runbook passes the _CRS value
    # explicitly, a direct probe run gets the ACPI default here.
    bits = params["bits"] if params["bits"] is not None else 8

    if args.dry_run:
        print(f"dry-run: link sweep spidev={args.spidev or '(unset)'} "
              f"allow_writes={args.allow_writes} "
              f"gpio_backend={args.gpio_backend or 'auto'}")
        print(f"         pinned reset polarity={args.reset_polarity} "
              f"edge={params['edge']} bits={bits} "
              f"chip={args.gpio_chip or '(unset)'} irq={params['gpio']} "
              f"reset={params['reset_gpio']} "
              f"timeout={args.link_timeout_ms}ms")
        print(f"link: {len(cells)} cells (mode 0/1/2/3 x 1/4 MHz x settle "
              f"{'/'.join(str(ms) for ms in settles)} ms); each cell reads "
              f"0xA0 7B + 0xC1 9B + 0xA1 64B with the DLL-exact selector-only "
              f"frame")
        for cell in cells:
            print(f" {cell.index:>2} mode={cell.mode} "
                  f"speed={matrix._speed_label(cell.speed_hz)} "
                  f"settle={cell.settle_ms:>3}ms -> 0xA0 7B, 0xC1 9B, 0xA1 64B")
        print("link: verdicts are plausible-fw / plausible-info / "
              "byte-shift-detected / all-zeros / all-0xff / timeout / "
              "error / malformed")
        print("dry-run: no device or GPIO is opened; no JSON is written")
        return 0

    if not args.spidev:
        print("refusing: --spidev /dev/spidevN.M is required for a real link "
              "sweep (there is no default device)", file=sys.stderr)
        return 2
    if not args.allow_writes:
        print("refusing: --allow-writes is required before any SPI transfer "
              "(even a read)", file=sys.stderr)
        return 2
    if params["gpio"] is None:
        print("refusing: --try-link needs the GpioInt line (--gpio N or a "
              "_CRS that carries it); the known-good edge is rising",
              file=sys.stderr)
        return 2
    if params["reset_gpio"] is None:
        print("refusing: --try-link needs the GpioIo reset line (--reset-gpio "
              "N or a _CRS that carries it)", file=sys.stderr)
        return 2
    if not args.allow_reset:
        print("refusing: --try-link pulses the reset line once per cell; pass "
              "--allow-reset", file=sys.stderr)
        return 2
    if args.gpio_backend == "poll":
        print("refusing: --gpio-backend poll cannot drive the reset line; "
              "use auto, chardev, gpiod or sysfs", file=sys.stderr)
        return 2

    out_path = os.path.abspath(args.link_json or "link.json")
    parent = os.path.dirname(out_path) or "."
    if not os.path.isdir(parent):
        print(f"refusing: the --link-json directory does not exist: {parent}",
              file=sys.stderr)
        return 2

    if args.speed is not None or args.mode is not None:
        print("link: --speed/--mode are ignored; the sweep sets both per cell",
              file=sys.stderr)
    print(f"link: {len(cells)} cells, per-cell timeout "
          f"{args.link_timeout_ms}ms, read-only queries 0xA0/0xC1/0xA1")
    print(f"link: spidev={args.spidev} chip={args.gpio_chip or '(unset)'} "
          f"irq={params['gpio']} reset={params['reset_gpio']} "
          f"reset_polarity={args.reset_polarity} edge={params['edge']} "
          f"bits={bits}")

    active_low = args.reset_polarity == "active-low"

    def connect(cell):
        return matrix.SpiLinkSession(
            cell, spidev_path=args.spidev,
            reset_active_low=active_low, edge=params["edge"],
            bits_per_word=bits, gpio_chip=args.gpio_chip,
            reset_pin=params["reset_gpio"], irq_pin=params["gpio"],
            irq_active_low=bool(params["active_low"]),
            timeout_ms=args.link_timeout_ms, gpio_backend=args.gpio_backend)

    context = {
        "spidev": args.spidev,
        "gpio_chip": args.gpio_chip,
        "irq_pin": params["gpio"],
        "reset_pin": params["reset_gpio"],
        "reset_polarity": args.reset_polarity,
        "irq_edge": params["edge"],
        "irq_active_low": bool(params["active_low"]),
        "bits_per_word": bits,
        "gpio_backend": args.gpio_backend or "auto",
        "settles_ms": list(settles),
    }
    try:
        report = matrix.run_link(
            cells, connect=connect, timeout_ms=args.link_timeout_ms,
            context=context)
    except matrix.MatrixRefused as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2

    for line in report.render_table():
        print(line)
    print(f"link: {report.verdict}")
    try:
        matrix.write_link_report(report, out_path)
    except OSError as exc:
        print(f"link: could not write {out_path}: {exc}", file=sys.stderr)
        return 1
    print(f"link: table written to {out_path}")
    return 0 if report.outcome in (matrix.PLAUSIBLE_FW,
                                   matrix.PLAUSIBLE_INFO) else 1


def run_spi(args, cmds):
    """Hardware SPI path: every gate is checked before any open()."""
    spi = _load_spi()
    acpi = _load_acpi()

    try:
        params = _spi_parameters(args, spi, acpi)
        plans = [_spi_plan(spi, args, cmd) for cmd in cmds]
    except spi.SpiError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"dry-run: transport=spi spidev={args.spidev or '(unset)'} "
              f"allow_writes={args.allow_writes} "
              f"gpio_backend={args.gpio_backend or 'auto'} "
              f"read_shape={args.read_shape}")
        print(f"         speed={params['speed']} mode={params['mode']} "
              f"bits={params['bits']} chip-select={params['chip_select']} "
              f"gpio={params['gpio']} reset={params['reset_gpio']}")
        for label, plan in plans:
            lead = plan.shape.padding if plan.shape is not None else b""
            print(f"{label}: opcode 0x{plan.opcode:02x} "
                  f"selector 0x{plan.selector:02x} "
                  f"{plan.operation} {plan.answer_len} B, "
                  f"lead {len(lead)} B{(': ' + lead.hex(' ')) if lead else ''}, "
                  f"timeout {plan.timeout_ms}/{plan.total_ms} ms")
            print(f"    {plan.hexdump()}")
        return 0

    if not args.spidev:
        print("refusing: --spidev /dev/spidevN.M is required for a real "
              "transfer (there is no default device)", file=sys.stderr)
        return 2
    if not args.allow_writes:
        print("refusing: --allow-writes is required before any SPI transfer "
              "(even a read); use --dry-run to inspect the bytes",
              file=sys.stderr)
        return 2
    if params["reset_gpio"] is not None and not args.allow_reset:
        print("refusing: --reset-gpio needs --allow-reset (the default is to "
              "leave the module alone)", file=sys.stderr)
        return 2
    if params["reset_gpio"] is not None and args.gpio_backend == "poll":
        print("refusing: --gpio-backend poll cannot drive the reset line; "
              "use auto, chardev, gpiod or sysfs", file=sys.stderr)
        return 2
    if params["chip_select"] not in (None, 0):
        print(f"note: _CRS says chip select {params['chip_select']}; spidev "
              "chooses the chip select by device node, not by this value",
              file=sys.stderr)

    read_allowlist = set(spi.SAFE_READ_OPS)
    if args.allow_op and args.op is not None:
        _refuse_destructive(spi, args.op)
        read_allowlist.add(args.op)
    irq = None
    if params["gpio"] is not None:
        irq = spi.GpioIrqWaiter(
            params["gpio"], chip=args.gpio_chip, edge=params["edge"],
            active_low=params["active_low"], backend=args.gpio_backend)
    reset = None
    if params["reset_gpio"] is not None:
        reset = spi.GpioResetLine(
            params["reset_gpio"], chip=args.gpio_chip,
            active_low=(args.reset_polarity == "active-low"),
            backend=args.gpio_backend)
    transport = spi.SpiTransport(
        args.spidev, speed_hz=params["speed"], mode=params["mode"],
        bits_per_word=params["bits"], allow_writes=True,
        read_allowlist=read_allowlist, irq=irq, reset=reset,
        allow_reset=args.allow_reset,
        reset_settle_ms=args.settle_ms)
    print(f"spi: {transport.describe()}")
    rc = 0
    try:
        for label, plan in plans:
            try:
                answer = transport.execute(plan)
            except spi.SpiError as exc:
                print(f"{label}: FAILED - {exc}", file=sys.stderr)
                return 1
            lead = plan.shape.padding if plan.shape is not None else b""
            print(f"{label}: opcode 0x{plan.opcode:02x} "
                  f"selector 0x{plan.selector:02x} answer {len(answer)} B")
            if answer:
                print(f"    {answer.hex(' ')}")
            payload = answer
            parsed = None
            if args.read_shape == "frame" and answer:
                try:
                    parsed = spi.parse_read_answer(plan.opcode, answer,
                                                   lead=lead)
                except spi.AnswerError as exc:
                    print(f"{label}: answer rejected - {exc}", file=sys.stderr)
                    return 1
                payload = parsed.payload
                if parsed.framing == "frame":
                    print(f"    0xAA frame, CRC-32 ok, payload "
                          f"{len(payload)} B: {payload.hex(' ')}")
                elif parsed.skipped:
                    print(f"    skipped {parsed.skipped} lead byte(s): "
                          f"{answer[:parsed.skipped].hex(' ')}")
            if label == "fw":
                print("   ", show_fw(payload))
            elif label == "info":
                print("   ", show_info(payload))
            if args.raw_info:
                for transfer, data in zip(
                        getattr(transport._io, "last_transfers", ()),
                        getattr(transport._io, "last_message", ())):
                    print(f"    tx {transfer.tx.hex(' ') or '(clock)'} "
                          f"rx {data.hex(' ') or '(none)'}")
    finally:
        transport.close()
    return rc


def _extraction_help():
    return """\
Extract the FPRT _CRS on the host (root; no state change):
  sudo cat /sys/firmware/acpi/tables/DSDT > /tmp/dsdt.aml
  iasl -d /tmp/dsdt.aml
  grep -n -A 40 'FPRT' /tmp/dsdt.dsl
Then take the ResourceTemplate bytes of the matching _CRS and save them:
  python3 - <<'PY'
  # paste the hex from the DSL (the bytes inside ResourceTemplate () {...})
  open('/tmp/fprt-crs.bin','wb').write(bytes.fromhex('...'))
  PY
  sudo tools/fingerprint/oxp3-fp-probe.py acpi_info --crs /tmp/fprt-crs.bin
"""


def acpi_info(args, cmds):
    """``acpi_info``: parse an extracted _CRS template.  Offline, no device."""
    if len(cmds) > 1:
        print(f"refusing: acpi_info does not take other commands: "
              f"{', '.join(cmds[1:])}", file=sys.stderr)
        return 2
    acpi = _load_acpi()
    if not args.crs:
        print("acpi_info: no --crs file supplied.")
        print(_extraction_help())
        return 0
    try:
        with open(args.crs, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        print(f"acpi_info: cannot read {args.crs}: {exc}", file=sys.stderr)
        return 1
    try:
        template = acpi.parse_resource_template(raw)
    except acpi.AcpiError as exc:
        print(f"acpi_info: malformed resource template: {exc}", file=sys.stderr)
        return 1
    print(f"acpi_info: {len(raw)} B, {len(template.resources)} resource item(s)"
          + (f", {len(template.trailing)} trailing B" if template.trailing
             else ""))
    for line in template.describe():
        print(f"  {line}")
    connection = acpi.spi_connection(template)
    print("SpiConnection:")
    for key, value in connection.as_dict().items():
        print(f"  {key:<18}: {value}")
    if connection.irq_pin is None:
        print(f"  {'irq edge':<18}: n/a (no GpioInt)")
        print(f"  {'irq active_low':<18}: n/a")
    else:
        print(f"  {'irq edge':<18}: {connection.irq_edge}")
        print(f"  {'irq active_low':<18}: {connection.irq_active_low}")
    print("Suggested probe (after binding spidev):")
    print("  sudo tools/fingerprint/oxp3-fp-probe.py --crs "
          f"{args.crs} --spidev /dev/spidev0.0 --allow-writes --dry-run fw")
    return 0


def run_frame(args, cmds):
    """The old ``0xAA``/CRC-32 frame experiment over spidev."""
    if args.dry_run:
        print(f"dry-run: transport=frame spidev={args.spidev or '(unset)'}")
        for cmd in cmds:
            if cmd == "acpi_info":
                print("acpi_info: use --transport spi (or no --transport)")
                return 2
            if cmd == "raw":
                if args.op is None:
                    print("refusing: --op is required for raw", file=sys.stderr)
                    return 2
                if not 0 <= args.op <= 0xFF:
                    print(f"refusing: --op must be 0..0xff, not {args.op!r}",
                          file=sys.stderr)
                    return 2
                if args.op in DESTRUCTIVE:
                    print(f"op 0x{args.op:02x} is {DESTRUCTIVE[args.op]}: "
                          "refusing", file=sys.stderr)
                    return 2
                try:
                    data = bytes.fromhex(args.data.replace(" ", ""))
                except ValueError as exc:
                    print(f"refusing: bad --data hex: {exc}", file=sys.stderr)
                    return 2
                op, label = args.op, f"op 0x{args.op:02x}"
            elif cmd in OPS:
                op, label = OPS[cmd]
                data = b""
            else:
                print(f"unknown command {cmd!r}; choose from {', '.join(OPS)}",
                      file=sys.stderr)
                return 2
            frame = build_frame(op, 0, args.arg, data)
            print(f"{label}: {len(frame)} B frame, opcode 0x{op:02x}, "
                  f"phase 0, arg 0x{args.arg:04x}")
            print(f"    {frame.hex(' ')}")
        return 0
    if not args.spidev:
        print("refusing: --spidev /dev/spidevN.M is required (there is no "
              "default device)", file=sys.stderr)
        return 2
    if args.data:
        try:
            bytes.fromhex(args.data.replace(" ", ""))
        except ValueError as exc:
            print(f"refusing: bad --data hex: {exc}", file=sys.stderr)
            return 2
    try:
        spi = SpiDev(args.spidev, speed_hz=args.speed or 1_000_000,
                     mode=args.mode or 0, bits=args.bits or 8)
    except OSError as e:
        print(f"cannot open {args.spidev}: {e}\n"
              "bind spidev first - see the header of this file",
              file=sys.stderr)
        return 2

    rc = 0
    try:
        if args.session:
            return session_attempt(spi, args)
        for cmd in cmds:
            if cmd == "raw":
                op = args.op
                if op is None:
                    print("refusing: --op is required for raw", file=sys.stderr)
                    return 2
                if op in DESTRUCTIVE:
                    print(f"op 0x{op:02x} is {DESTRUCTIVE[op]}: refusing",
                          file=sys.stderr)
                    return 2
                data = bytes.fromhex(args.data.replace(" ", ""))
                label = f"op 0x{op:02x}"
            elif cmd in OPS:
                op, label = OPS[cmd]
                data = b""
            else:
                print(f"unknown command {cmd!r}; choose from {', '.join(OPS)}",
                      file=sys.stderr)
                return 2

            (hdr, body), resp = query(spi, op, arg=args.arg, data=data,
                                      wait_ms=args.wait_ms, rx_len=args.rx,
                                      retries=args.retries,
                                      verbose=args.raw_info)
            if hdr is None:
                print(f"{label}: no answer (buf {len(resp)}B, "
                      f"first={resp[:1].hex() or 'none'})")
                rc = 1
            else:
                print(f"{label}: hdr={hdr} body={body[:24].hex(' ')}")
                if cmd == "info":
                    print("   ", show_info(body))
                elif cmd == "fw":
                    print("   ", show_fw(body))
                if hdr.get("magic") != CMD_MAGIC:
                    print("    (no 0x4C command magic - framing assumption wrong)")
            if args.raw_info:
                print(f"    rx {len(resp):3d}B: {resp.hex(' ')}")
    finally:
        spi.close()
    return rc



if __name__ == "__main__":
    sys.exit(main())
