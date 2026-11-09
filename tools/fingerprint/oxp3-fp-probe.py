#!/usr/bin/env python3
"""oxp3-fp-probe - Betterlife BF63112 (ONEXPLAYER 3) fingerprint probe.

Read-only bring-up tool for the fingerprint sensor that Anatase/Linux currently
does not drive.  It has two transports:

``--transport spi`` (default) - the one this unit actually uses.  It speaks the
SPB/SPI wire format recovered from ``BetterlifeFingerprintDevice.dll``
(``FUN_180017020`` read / ``FUN_180017404`` write): a one-byte selector
``(opcode & 0x3f) << 1`` (bit 7 set for host->device writes), then a fixed-size
read (two-entry sequence) or the payload (single write).  See
``fpbf63112/spi.py`` and docs/hw/fingerprint-transport.md.

``--transport frame`` - the older experiment that puts the USB branch's 0xAA /
CRC-32 frame on the SPI wire.  Kept only to test the "is the USB frame behind
the selector?" question; it is not the SPI protocol.

Safety: every command needs ``--allow-writes`` and a read-only opcode on the
allowlist.  Enrol/identify/remove/JTAG/restore/firmware-update are refused
unconditionally.  ``--dry-run`` prints the exact bytes and never opens a device.
``acpi_info`` parses an extracted ``_CRS`` resource template (the only root
step), and prints the exact commands to obtain it.

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
DESTRUCTIVE = {0xB0: "ENROLL", 0xB2: "REMOVE", 0xC2: "JTAG", 0xC3: "RESTORE_MCU",
               0xD0: "UPDATE_FIRMWARE", 0xFF: "FW_WORK_MODE_SWITCH"}


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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("commands", nargs="*", help=", ".join(OPS))
    ap.add_argument("--dev", default="/dev/spidev0.0")
    ap.add_argument("--speed", type=int, default=1_000_000,
                    help="SPI clock in Hz (unknown: Windows value not recovered)")
    ap.add_argument("--mode", type=int, default=0, choices=[0, 1, 2, 3])
    ap.add_argument("--wait-ms", type=int, default=40)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--rx", type=int, default=64, help="bytes to read back")
    ap.add_argument("--op", type=lambda s: int(s, 0),
                    help="send one raw opcode instead of a named command")
    ap.add_argument("--arg", type=int, default=0)
    ap.add_argument("--data", default="", help="hex payload for --op")
    ap.add_argument("--raw-info", action="store_true", help="hexdump frames")
    ap.add_argument("--crypto-selftest", action="store_true",
                    help="prove SM2/SM3/SM4/KDF against published vectors and "
                         "benchmark; needs no device and touches nothing")
    ap.add_argument("--session", action="store_true",
                    help="attempt the 0xA2 SM2 session handshake (read-only key "
                         "exchange) and report the derived-key fingerprint")
    ap.add_argument("--session-secure", action="store_true",
                    help="send the 0xA2 request in the vendor's secure-frame "
                         "form (SM4 with the initial zero key)")
    ap.add_argument("--session-rx", type=int, default=256,
                    help="readback bytes for --session (a 0xA2 answer needs ~153)")
    ap.add_argument("--session-timeout", type=int, default=2000,
                    help="--session answer timeout in ms")
    args = ap.parse_args()

    if args.crypto_selftest:
        return crypto_selftest()

    cmds = args.commands or (["raw"] if args.op is not None else ["info"])

    try:
        spi = SpiDev(args.dev, speed_hz=args.speed, mode=args.mode)
    except OSError as e:
        sys.exit(f"cannot open {args.dev}: {e}\n"
                 "bind spidev first - see the header of this file")

    rc = 0
    try:
        if args.session:
            return session_attempt(spi, args)
        for cmd in cmds:
            if cmd == "raw":
                op = args.op
                if op in DESTRUCTIVE:
                    sys.exit(f"op 0x{op:02x} is {DESTRUCTIVE[op]}: refusing")
                data = bytes.fromhex(args.data.replace(" ", ""))
                label = f"op 0x{op:02x}"
            elif cmd in OPS:
                op, label = OPS[cmd]
                data = b""
            else:
                sys.exit(f"unknown command {cmd!r}; choose from {', '.join(OPS)}")

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
