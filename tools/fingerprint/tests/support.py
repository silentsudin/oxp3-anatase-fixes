"""Shared helpers for the fpbf63112 test suite (stdlib only).

The tests deliberately depend on nothing but the standard library so they run
on the target host (Anatase, Python 3.13+) without pip::

    python3 -m unittest discover -s tools/fingerprint/tests -v

``support`` puts ``tools/fingerprint`` on ``sys.path`` and provides the offline
"device" players that replay the synthetic transcripts in ``tests/fixtures``
through the same state machine the real probe/driver use.
"""

from __future__ import annotations

import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                       # tools/fingerprint
REPO = ROOT.parents[1]                   # repository root
FIXTURES = HERE / "fixtures"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fpbf63112 import protocol as P       # noqa: E402  (path set above)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def load_fixture(name: str) -> dict:
    """Load ``tests/fixtures/<name>.json``."""
    import json

    path = FIXTURES / f"{name}.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def fixture_paths() -> list[pathlib.Path]:
    return sorted(FIXTURES.glob("*.json"))


# --------------------------------------------------------------------------
# Replay players: drive the library with a synthetic device transcript
# --------------------------------------------------------------------------

def play_command(fixture: dict,
                 policy: P.RetryPolicy | None = None) -> dict:
    """Replay a ``kind == "command"`` fixture through a :class:`Transaction`.

    Each entry of ``reads`` is one spidev readback.  A read containing a valid
    frame is delivered with :meth:`Transaction.handle_frame`; a read with no
    usable frame counts as a failed attempt (``handle_error``), which is how
    the driver treats a timeout and a bad CRC alike.

    Returns a dict with ``outcome`` (one of ``success``, ``failed``,
    ``pending``, ``mismatch``), ``attempts``, ``payload_hex`` and ``error``.
    """
    req = fixture["request"]
    opcode = int(P.Command[req["op"]])
    tx = P.Transaction(opcode=opcode,
                       phase=req.get("phase", 0),
                       arg=req.get("arg", 0),
                       data=bytes.fromhex(req.get("data_hex", "")),
                       policy=policy or P.RetryPolicy(),
                       check_phase=req.get("check_phase", False))
    tx.start()
    errors: list[str] = []

    for read in fixture["reads"]:
        if tx.done:
            break
        frames, scan_errors = P.scan_frames(bytes.fromhex(read["hex"]))
        if frames:
            try:
                tx.handle_frame(frames[0])
                continue
            except P.ResponseMismatchError as exc:
                return {"outcome": "mismatch", "attempts": tx.attempts,
                        "payload_hex": "", "error": str(exc),
                        "errors": errors}
        err = (scan_errors[0].error if scan_errors
               else P.TruncatedFrameError("read held no frame"))
        errors.append(str(err))
        tx.handle_error(err)

    outcome = {P.TransactionState.SUCCESS: "success",
               P.TransactionState.FAILED: "failed"}.get(tx.state, "pending")
    payload = tx.response.data if tx.response else b""
    return {"outcome": outcome, "attempts": tx.attempts,
            "payload_hex": payload.hex(),
            "error": str(tx.last_error) if tx.last_error else None,
            "errors": errors}


def play_image(fixture: dict) -> dict:
    """Replay a ``kind == "image"`` fixture through :class:`ImageAssembler`.

    The first usable frame is the phase-0 header (9-byte payload); every later
    usable frame is a phase-1 chunk.  Reads with no usable frame are skipped,
    modelling a re-request of the same chunk.
    """
    import hashlib

    assembler: P.ImageAssembler | None = None
    errors: list[str] = []
    for read in fixture["reads"]:
        frames, scan_errors = P.scan_frames(bytes.fromhex(read["hex"]))
        if not frames:
            errors.extend(str(e) for e in scan_errors)
            continue
        frame = frames[0]
        if assembler is None:
            try:
                header = P.ImageHeader.parse(frame.data)
            except P.ImageError as exc:
                return {"outcome": "no-header", "error": str(exc),
                        "received": 0, "total": 0, "errors": errors}
            assembler = P.ImageAssembler(
                header, max_chunk=fixture.get("max_chunk", P.MAX_IMAGE_CHUNK))
            continue
        try:
            assembler.add(frame.data)
        except P.ImageError as exc:
            return {"outcome": "error", "error": type(exc).__name__,
                    "received": assembler.received,
                    "total": assembler.header.total_bytes, "errors": errors}

    if assembler is None:
        return {"outcome": "no-header", "error": "no header frame",
                "received": 0, "total": 0, "errors": errors}
    try:
        image = assembler.finish()
    except P.ImageSizeMismatch:
        return {"outcome": "incomplete", "received": assembler.received,
                "total": assembler.header.total_bytes, "errors": errors}
    except P.ImageSumMismatch as exc:
        return {"outcome": "bad-sum", "received": assembler.received,
                "total": assembler.header.total_bytes, "error": str(exc),
                "errors": errors}
    return {"outcome": "success",
            "sha256": hashlib.sha256(image).hexdigest(),
            "received": len(image),
            "total": assembler.header.total_bytes,
            "sum8": P.sum8(image),
            "image_hex": image.hex(),
            "errors": errors}


def play(fixture: dict) -> dict:
    """Replay any fixture, dispatching on its ``kind``."""
    kind = fixture.get("kind")
    if kind == "command":
        return play_command(fixture)
    if kind == "image":
        return play_image(fixture)
    raise ValueError(f"unknown fixture kind {kind!r}")


# --------------------------------------------------------------------------
# Cross-check against the standalone probe
# --------------------------------------------------------------------------

def load_probe():
    """Import ``oxp3-fp-probe.py`` (hyphenated filename) as a module."""
    import importlib.util

    path = ROOT / "oxp3-fp-probe.py"
    spec = importlib.util.spec_from_file_location("oxp3_fp_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# The synthetic "device" half of the SM2/SM3/SM4 session handshake
# --------------------------------------------------------------------------

class FakeRootKeyDevice:
    """A transport-shaped fake of the module's ``0xA2`` answer.

    Built on our own crypto implementation, so it proves the handshake logic
    and the wire layout (nothing more): the real firmware's behaviour is what a
    live session has to establish.  ``exchange(frame, timeout_ms=...)`` matches
    the ``protocol.TRANSPORT_METHOD`` contract.
    """

    def __init__(self, session_key=b"\x5a" * 16, params=None,
                 device_keypair=None, secure_requests=False,
                 secure_answers=False, status=0, tamper_tag=False,
                 answer_override=None):
        from fpbf63112 import crypto as C

        self.crypto = C
        self.session_key = session_key
        self.params = params if params is not None else C.PARAMS_RECOVERED
        self.device_keypair = device_keypair or C.SM2Keypair(0x1234)
        self.secure_requests = secure_requests
        self.secure_answers = secure_answers
        self.status = status
        self.tamper_tag = tamper_tag
        self.answer_override = answer_override
        self.requests = []

    def exchange(self, frame, *, timeout_ms=1000):
        C = self.crypto
        self.requests.append(frame)
        parsed = self.parse_request(frame)
        payload = parsed.data
        if not payload or payload[0] != 0x04 or len(payload) != 65:
            raise AssertionError(f"unexpected 0xA2 payload {payload.hex()}")
        host_point = C.sm2_decode_point(payload[1:])
        shared = self.device_keypair.ecdh(host_point)
        z = self.params.kdf_z(shared)
        klen = self.params.key_len or len(self.session_key)
        mask = C.sm3_kdf(z, klen)
        wrapped = bytes(a ^ b for a, b in zip(mask, self.session_key[:klen]))
        tag = C.session_tag(shared, self.session_key[:klen], self.params)
        if self.tamper_tag:
            tag = bytes([tag[0] ^ 0xFF]) + tag[1:]
        body = P.RootKeyAnswer.build(self.device_keypair.public_bytes, tag,
                                     wrapped, status=self.status)
        if self.answer_override is not None:
            body = self.answer_override
        answer = P.build_frame(P.Command.QUERY_ROOT_KEY, 0, 0, body)
        if self.secure_answers:
            answer = C.SecureSession(b"\x00" * 16).seal_frame(answer)
        return answer

    def parse_request(self, frame):
        if self.secure_requests:
            from fpbf63112 import crypto as C

            plain = C.SecureSession(b"\x00" * 16).open_frame(frame)
            return P.parse_frame(plain)
        return P.parse_frame(frame)
