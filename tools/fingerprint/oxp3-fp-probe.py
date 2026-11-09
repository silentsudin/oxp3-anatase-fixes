#!/usr/bin/env python3
"""oxp3-fp-probe - Betterlife BF63112 (ONEXPLAYER 3) SPI fingerprint probe.

Read-only bring-up tool for the fingerprint sensor that Anatase/Linux currently
does not drive.  It speaks the frame format recovered from the Windows UMDF
driver `BetterlifeFingerprintDevice.dll` (v9.39.15.26) - see
docs/hw/fingerprint.md for the evidence and the byte-by-byte derivation.

It NEVER enrols, deletes, resets, JTAGs or reflashes anything: the command set
exposed here is limited to the read-only queries (firmware version, module
scheme, module UID, sensor info, fingerprint count).  --op lets you send an
arbitrary single opcode if you extend it yourself; the destructive opcodes are
listed in the doc so you know what to avoid.

Requires: python3 (stdlib only, no python3-spidev) plus an spidev-bound node.
The sensor has no in-tree driver, so bind spidev by hand:

    sudo modprobe spidev
    echo spidev | sudo tee /sys/bus/spi/devices/spi-BF63112:00/driver_override
    echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/bind
    ls -l /dev/spidev0.0          # created by the bind

Then (needs read access to /dev/spidev0.0 - root, or a udev rule of your own):

    sudo ./oxp3-fp-probe.py info          # sensor geometry + flags
    sudo ./oxp3-fp-probe.py fw            # firmware version / module project
    sudo ./oxp3-fp-probe.py uid scheme count
    sudo ./oxp3-fp-probe.py --raw-info info

Undo:  echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/unbind

This is our own code and it is UNTESTED on hardware (see "Open points" in the
doc): the frame builder matches the driver byte for byte, the response parser is
partly inferred because only part of the device->host framing was recovered.
"""

import argparse
import ctypes
import fcntl
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

# opcode byte = descriptor+0x10 in the Windows driver.  Only the read-only
# commands are wired up; the whole table is in docs/hw/fingerprint.md.
OPS = {
    "fw":     (0xA1, "CMD_QUERY_FIRMWARE_VER"),
    "scheme": (0xA2, "CMD_QUERY_MODULE_SCHEME_INFORMATION"),
    "count":  (0xB4, "CMD_QUERY_FINGERPRINT_COUNT"),
    "uid":    (0xC1, "CMD_QUERY_MODULE_UID"),
    "info":   (0xC2, "CMD_QUERY_SENSOR_INFO"),
}
DESTRUCTIVE = {0xB1: "ENROLL", 0xB3: "REMOVE", 0xC3: "JTAG", 0xD0: "RESTORE_MCU",
               0xD1: "UPDATE_FIRMWARE", 0xFF: "FW_WORK_MODE_SWITCH"}


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
    args = ap.parse_args()

    cmds = args.commands or (["raw"] if args.op is not None else ["info"])

    try:
        spi = SpiDev(args.dev, speed_hz=args.speed, mode=args.mode)
    except OSError as e:
        sys.exit(f"cannot open {args.dev}: {e}\n"
                 "bind spidev first - see the header of this file")

    rc = 0
    try:
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
