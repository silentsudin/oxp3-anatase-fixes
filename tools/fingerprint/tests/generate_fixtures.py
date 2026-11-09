#!/usr/bin/env python3
"""Generate the checked-in synthetic test data for the BF63112 protocol library.

Everything this script writes is *our own* synthetic data derived from the
recovered wire format - no vendor code, no firmware, no captures from the
Windows driver.  The generated files are committed so the tests need no
generation step; ``test_generated_data.py`` re-runs the generators in memory
and fails if the checked-in files have drifted.

What is produced under ``tools/fingerprint/tests/``:

``fixtures/*.json``
    Small device-transcript fixtures: one request and a list of spidev
    readbacks.  They cover a clean answer, leading junk, truncated frames, a
    bad CRC, a wrong-opcode answer, retry exhaustion, and GET_IMG image
    transfer (good, partial, bad sum, a retried chunk, an oversized chunk).

``vectors.json`` / ``vectors.h``
    Frame build vectors shared by the Python tests and the independent C
    implementation in ``bf63112_proto.c``.  ``vectors.h`` is compiled into
    ``tests/test_proto.c``; the two implementations must agree byte for byte.

Run from the repository root::

    python3 tools/fingerprint/tests/generate_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURES = HERE / "fixtures"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fpbf63112 import protocol as P  # noqa: E402

ZERO_PAD = "00" * 32      # spidev read padding after a frame
NOISE = "ff" * 8 + "00" * 4


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def frame_hex(opcode: int, phase: int = 0, arg: int = 0,
              data: bytes = b"", status: int = 0) -> str:
    return P.build_frame(opcode, phase, arg, data, status=status).hex()


def read(frame: str, *, pad: bool = True) -> dict:
    return {"hex": frame + (ZERO_PAD if pad else "")}


def junk(n: int = 64, byte: str = "ff") -> dict:
    return {"hex": byte * n}


def corrupt(frame_hex_str: str) -> str:
    """Flip the last CRC byte of a frame (structural fields stay valid)."""
    raw = bytearray.fromhex(frame_hex_str)
    raw[-1] ^= 0xFF
    return bytes(raw).hex()


def image_8x8() -> bytes:
    """Deterministic 8x8 8-bit test image (64 bytes)."""
    return bytes((x * 8 + y) % 256 for y in range(8) for x in range(8))


# --------------------------------------------------------------------------
# command fixtures
# --------------------------------------------------------------------------

def command_fixtures() -> list[dict]:
    info_payload = struct.pack("<HHHHB", 1, 2, 208, 80, 0x03)
    fw_payload = bytes([0x00, 0x01, 0x02, 0x09, 0x27, 0x0F, 0x1A])

    fixtures = []

    fixtures.append({
        "schema": 1,
        "name": "cmd_sensor_info_ok",
        "kind": "command",
        "description": "CMD_QUERY_SENSOR_INFO answer behind bus noise",
        "request": {"op": "QUERY_SENSOR_INFO", "phase": 0, "arg": 0,
                    "data_hex": ""},
        "reads": [
            {"hex": NOISE + frame_hex(0xC2, data=info_payload),
             "note": "preamble junk, then the answer"},
            {"hex": ZERO_PAD, "note": "trailing padding only"},
        ],
        "expect": {"outcome": "success", "attempts": 1,
                   "payload_hex": info_payload.hex()},
    })

    fixtures.append({
        "schema": 1,
        "name": "cmd_fw_version_ok",
        "kind": "command",
        "description": "CMD_QUERY_FIRMWARE_VER answer, 7-byte payload",
        "request": {"op": "QUERY_FIRMWARE_VER", "phase": 0, "arg": 0,
                    "data_hex": ""},
        "reads": [read(frame_hex(0xA1, data=fw_payload))],
        "expect": {"outcome": "success", "attempts": 1,
                   "payload_hex": fw_payload.hex()},
    })

    fixtures.append({
        "schema": 1,
        "name": "cmd_timeout_then_ok",
        "kind": "command",
        "description": "first read is idle bus (no IRQ seen yet), second has "
                       "the answer: the transaction must re-send and succeed",
        "request": {"op": "QUERY_MODULE_UID", "phase": 0, "arg": 0,
                    "data_hex": ""},
        "reads": [junk(), read(frame_hex(0xC1, data=bytes(range(1, 9))))],
        "expect": {"outcome": "success", "attempts": 2,
                   "payload_hex": bytes(range(1, 9)).hex()},
    })

    bad = corrupt(frame_hex(0xB4, data=b"\x02\x00\x00\x00"))
    good = frame_hex(0xB4, data=b"\x02\x00\x00\x00")
    fixtures.append({
        "schema": 1,
        "name": "cmd_bad_crc_then_ok",
        "kind": "command",
        "description": "a structurally valid answer with a broken CRC counts "
                       "as a failed attempt; the re-sent frame is answered",
        "request": {"op": "QUERY_FINGERPRINT_COUNT", "phase": 0, "arg": 0,
                    "data_hex": ""},
        "reads": [read(bad), read(good)],
        "expect": {"outcome": "success", "attempts": 2,
                   "payload_hex": "02000000"},
    })

    fixtures.append({
        "schema": 1,
        "name": "cmd_bad_crc_exhausted",
        "kind": "command",
        "description": "every answer has a bad CRC; retries run out",
        "request": {"op": "QUERY_MODULE_SCHEME_INFORMATION", "phase": 0,
                    "arg": 0, "data_hex": ""},
        "reads": [read(bad), read(bad), read(bad)],
        "expect": {"outcome": "failed", "attempts": 3, "payload_hex": ""},
    })

    fixtures.append({
        "schema": 1,
        "name": "cmd_truncated_then_ok",
        "kind": "command",
        "description": "half a frame (length field but no body) then a "
                       "complete answer",
        "request": {"op": "QUERY_SENSOR_INFO", "phase": 0, "arg": 0,
                    "data_hex": ""},
        "reads": [{"hex": frame_hex(0xC2, data=info_payload)[:50]},
                  read(frame_hex(0xC2, data=info_payload))],
        "expect": {"outcome": "success", "attempts": 2,
                   "payload_hex": info_payload.hex()},
    })

    fixtures.append({
        "schema": 1,
        "name": "cmd_timeout_exhausted",
        "kind": "command",
        "description": "no answer at all: three idle reads exhaust the "
                       "default retry policy",
        "request": {"op": "QUERY_SENSOR_INFO", "phase": 0, "arg": 0,
                    "data_hex": ""},
        "reads": [junk(), junk(), junk()],
        "expect": {"outcome": "failed", "attempts": 3, "payload_hex": ""},
    })

    fixtures.append({
        "schema": 1,
        "name": "cmd_wrong_opcode",
        "kind": "command",
        "description": "answer belongs to another command (0xC1 while 0xC2 is "
                       "in flight): the transaction refuses it",
        "request": {"op": "QUERY_SENSOR_INFO", "phase": 0, "arg": 0,
                    "data_hex": ""},
        "reads": [read(frame_hex(0xC1, data=b"\x00\x01"))],
        "expect": {"outcome": "mismatch", "attempts": 1, "payload_hex": ""},
    })

    return fixtures


# --------------------------------------------------------------------------
# GET_IMG fixtures
# --------------------------------------------------------------------------

def image_fixtures() -> list[dict]:
    img = image_8x8()
    total = len(img)
    checksum = P.sum8(img)
    header_ok = frame_hex(0xB5, phase=0, data=P.ImageHeader.build(total, checksum))
    header_bad_sum = frame_hex(0xB5, phase=0,
                               data=P.ImageHeader.build(total, checksum ^ 0x01))
    chunks = [img[i:i + 16] for i in range(0, total, 16)]
    chunk_frames = [frame_hex(0xB5, phase=1, data=c) for c in chunks]

    fixtures = []

    fixtures.append({
        "schema": 1,
        "name": "img_ok_8x8",
        "kind": "image",
        "description": "64-byte 8x8 image in four 16-byte chunks",
        "chunk_size": 16,
        "image_hex": img.hex(),
        "reads": [read(header_ok)] + [read(f) for f in chunk_frames],
        "expect": {"outcome": "success", "received": total, "total": total,
                   "sha256": hashlib.sha256(img).hexdigest(),
                   "sum8": checksum},
    })

    fixtures.append({
        "schema": 1,
        "name": "img_partial",
        "kind": "image",
        "description": "only two of four chunks arrive; finish() must report "
                       "an incomplete image instead of returning short data",
        "chunk_size": 16,
        "reads": [read(header_ok)] + [read(f) for f in chunk_frames[:2]],
        "expect": {"outcome": "incomplete", "received": 32, "total": total},
    })

    fixtures.append({
        "schema": 1,
        "name": "img_bad_sum",
        "kind": "image",
        "description": "all chunks arrive but the announced 8-bit sum is "
                       "wrong; the global sum check must catch it",
        "chunk_size": 16,
        "reads": [read(header_bad_sum)] + [read(f) for f in chunk_frames],
        "expect": {"outcome": "bad-sum", "received": total, "total": total},
    })

    truncated_chunk = {"hex": chunk_frames[1][:40]}
    fixtures.append({
        "schema": 1,
        "name": "img_chunk_retry",
        "kind": "image",
        "description": "chunk 1 is cut short, is re-requested, then the "
                       "transfer continues: a per-chunk retry, not image "
                       "corruption",
        "chunk_size": 16,
        "reads": [read(header_ok), read(chunk_frames[0]), truncated_chunk,
                  read(chunk_frames[1]), read(chunk_frames[2]),
                  read(chunk_frames[3])],
        "expect": {"outcome": "success", "received": total, "total": total,
                   "sha256": hashlib.sha256(img).hexdigest(),
                   "sum8": checksum},
    })

    big = bytes((i * 7) % 256 for i in range(144))
    big_header = frame_hex(0xB5, phase=0,
                           data=P.ImageHeader.build(len(big), P.sum8(big)))
    fixtures.append({
        "schema": 1,
        "name": "img_chunk_too_large",
        "kind": "image",
        "description": "a 144-byte chunk with max_chunk=128: the assembler "
                       "refuses it (the Windows driver's own ceiling is 8140)",
        "chunk_size": 144,
        "max_chunk": 128,
        "reads": [read(big_header), read(frame_hex(0xB5, phase=1, data=big))],
        "expect": {"outcome": "error", "error": "ChunkTooLargeError",
                   "received": 0, "total": 144},
    })

    return fixtures


# --------------------------------------------------------------------------
# shared frame vectors (Python + C)
# --------------------------------------------------------------------------

def frame_vectors() -> list[dict]:
    specs = [
        ("fw_query_empty", 0xA1, 0, 0x0000, 0, b""),
        ("sensor_info_arg", 0xC2, 0, 0x1234, 0, b""),
        ("getimg_aligned6", 0xB5, 1, 0x0001, 0, bytes(range(1, 7))),
        ("getimg_pad7", 0xB5, 1, 0x0002, 0, bytes(range(1, 8))),
        ("enroll_data16", 0xB1, 0, 0x0000, 0, bytes(range(16))),
        ("enroll_phase2_data17", 0xB1, 2, 0x1234, 0, bytes(range(17))),
        ("getimg_data32", 0xB5, 1, 0x0000, 0, bytes(range(32))),
        ("image_header_reply", 0xB5, 0, 0x0000, 0,
         P.ImageHeader.build(64, P.sum8(image_8x8()))),
        ("status_reply_data9", 0xC2, 0, 0x0000, 0x05,
         bytes([0, 1, 2, 208, 0, 80, 0, 3, 0])),
        ("bulk255", 0xB5, 1, 0x0000, 0, bytes(range(255))),
    ]
    out = []
    for name, op, phase, arg, status, data in specs:
        raw = P.build_frame(op, phase, arg, data, status=status)
        out.append({
            "name": name, "opcode": op, "phase": phase, "status": status,
            "arg": arg, "data_hex": data.hex(),
            "frame_hex": raw.hex(),
            "frame_len": len(raw),
            "length_field": P.length_field(len(data)),
            "crc": P.crc32(raw[:-4]),
        })
    return out


def render_vectors_h(vectors: list[dict]) -> str:
    lines = [
        "/* Generated by tools/fingerprint/tests/generate_fixtures.py.",
        " * Synthetic BF63112 frame vectors shared with test_proto.c.",
        " * Do not edit; re-run the generator instead. */",
        "#ifndef BF63112_TEST_VECTORS_H",
        "#define BF63112_TEST_VECTORS_H",
        "",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        "struct bf63112_test_vector",
        "{",
        "  const char *name;",
        "  uint8_t opcode, phase, status;",
        "  uint16_t arg;",
        "  const uint8_t *data;",
        "  size_t data_len;",
        "  const char *frame_hex;",
        "  uint32_t crc, length_field;",
        "  size_t frame_len;",
        "};",
        "",
    ]
    for vec in vectors:
        data = bytes.fromhex(vec["data_hex"])
        if not data:
            continue
        body = ", ".join(f"0x{b:02x}" for b in data)
        lines.append(f"static const uint8_t vec_{vec['name']}[] = {{ {body} }};")
    lines.append("")
    lines.append("static const struct bf63112_test_vector bf63112_vectors[] = {")
    for vec in vectors:
        data = bytes.fromhex(vec["data_hex"])
        data_ref = f"vec_{vec['name']}, {len(data)}" if data else "NULL, 0"
        lines.append(
            f'  {{ "{vec["name"]}", 0x{vec["opcode"]:02x}, {vec["phase"]}, '
            f'0x{vec["status"]:02x}, 0x{vec["arg"]:04x}, {data_ref}, '
            f'"{vec["frame_hex"]}", 0x{vec["crc"]:08x}u, '
            f'0x{vec["length_field"]:08x}u, {vec["frame_len"]} }},')
    lines += [
        "};",
        "",
        "#define BF63112_VECTOR_COUNT \\",
        "  (sizeof (bf63112_vectors) / sizeof (bf63112_vectors[0]))",
        "",
        "#endif /* BF63112_TEST_VECTORS_H */",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# writing / currentness check
# --------------------------------------------------------------------------

def build_artifacts() -> dict[pathlib.Path, str]:
    """Return ``{path: text}`` for every generated file (no disk writes)."""
    artifacts: dict[pathlib.Path, str] = {}
    for fixture in command_fixtures() + image_fixtures():
        path = FIXTURES / f"{fixture['name']}.json"
        artifacts[path] = json.dumps(fixture, indent=2) + "\n"
    vectors = frame_vectors()
    artifacts[HERE / "vectors.json"] = json.dumps(vectors, indent=2) + "\n"
    artifacts[HERE / "vectors.h"] = render_vectors_h(vectors)
    return artifacts


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    artifacts = build_artifacts()
    for path, text in sorted(artifacts.items()):
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({len(text)}B)")
    print(f"done: {len(artifacts)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
