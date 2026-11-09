"""The fingerprint session crypto, exercised from the general unit suite.

The fingerprint stack has its own detailed suite in
``tools/fingerprint/tests`` (172 tests); this file is the small cross-check that
``make unit`` covers the crypto layer too - published vectors, the KDF/tag
construction and one synthetic end-to-end handshake.
"""

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
FINGERPRINT = REPO / "tools" / "fingerprint"
if str(FINGERPRINT) not in sys.path:
    sys.path.insert(0, str(FINGERPRINT))

from fpbf63112 import crypto as C          # noqa: E402
from fpbf63112 import protocol as P        # noqa: E402


class TestPublishedVectors(unittest.TestCase):
    def test_selftest(self):
        results = C.selftest()
        self.assertEqual(
            results["sm3_abc"],
            "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0")
        self.assertEqual(results["sm4_ecb"],
                         "681edf34d206965e86b3e94f536e4246")
        self.assertTrue(results["sm2_keypair"].startswith("09f9df31"))

    def test_sm4_round_trip_and_sm3_streaming(self):
        key = bytes.fromhex("0123456789abcdeffedcba9876543210")
        self.assertEqual(C.sm4_decrypt_block(key, C.sm4_encrypt_block(key, key)),
                         key)
        self.assertEqual(C.SM3(b"a").update(b"bc").digest(), C.sm3(b"abc"))


class TestHandshake(unittest.TestCase):
    def test_synthetic_handshake_recovers_the_key(self):
        # Build the device half inline so this file does not depend on the
        # fingerprint test package.
        session_key = b"\x5a" * 16
        device_kp = C.SM2Keypair(0x1234)
        host_kp = C.SM2Keypair(0x4321)

        shared = device_kp.ecdh(host_kp.point)
        mask = C.sm3_kdf(shared.encoded, 16)
        wrapped = bytes(a ^ b for a, b in zip(mask, session_key))
        tag = C.session_tag(shared, session_key)
        body = P.RootKeyAnswer.build(device_kp.public_bytes, tag, wrapped)
        answer = P.RootKeyAnswer.parse(body)

        result = C.complete_handshake(host_kp, answer)
        self.assertTrue(result.tag_ok)
        self.assertEqual(result.require_tag(), session_key)

    def test_secure_frame_round_trip(self):
        session = C.SecureSession(bytes(range(16)))
        frame = P.build_frame(P.Command.ENROLL_FINGERPRINT, P.Phase.START, 0,
                             b"\x01\x02")
        self.assertEqual(session.open_frame(session.seal_frame(frame)), frame)


if __name__ == "__main__":
    unittest.main()
