"""Session crypto tests: SM2/SM3/SM4, the GM/T 0003 KDF and the 0xA2 handshake.

Everything here runs offline with the standard library only; the "device" in
the handshake tests is our own implementation, which proves the protocol logic
and the wire layout, not the module's firmware.

    python3 -m unittest discover -s tools/fingerprint/tests -v
"""

import hashlib
import pathlib
import struct
import time
import unittest

import support
from fpbf63112 import crypto as C
from fpbf63112 import protocol as P

# --------------------------------------------------------------------------
# Published vectors
# --------------------------------------------------------------------------

SM3_ABC = "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
SM3_ABCD64 = "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732"
SM4_KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")
SM4_CT = "681edf34d206965e86b3e94f536e4246"
#: The published SM2 sample private key and the public point it must produce
#: (an *independent* check of the curve arithmetic; not derived here).
SM2_D = "3945208f7b2144b13f36e38ac6d39f95889393692860b51a42fb81ef4df7c5b8"
SM2_P = ("09f9df311e5421a150dd7d161e4bc5c672179fad1833fc076bb08ff356f35020"
         "ccea490ce26775a52dc6ea718cc1aa600aed05fbf35e084a6632f6072da9ad13")


class TestSM3(unittest.TestCase):
    def test_abc_vector(self):
        self.assertEqual(C.sm3(b"abc").hex(), SM3_ABC)

    def test_64_byte_vector(self):
        self.assertEqual(C.sm3(b"abcd" * 16).hex(), SM3_ABCD64)

    def test_empty(self):
        self.assertEqual(
            C.sm3(b"").hex(),
            "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b")

    def test_streaming_matches_oneshot(self):
        data = bytes((i * 91 + 7) & 0xFF for i in range(1000))
        h = C.SM3()
        for off in range(0, len(data), 37):
            h.update(data[off:off + 37])
        self.assertEqual(h.digest(), C.sm3(data))
        self.assertEqual(h.hexdigest(), C.sm3(data).hex())

    def test_block_boundaries(self):
        for n in (63, 64, 65, 119, 120, 128, 192):
            data = b"\x5a" * n
            self.assertEqual(len(C.sm3(data)), C.SM3_DIGEST_SIZE)
        # 55/56/57 are the padding boundaries of MD-style hashes
        for n in (54, 55, 56, 57):
            self.assertEqual(C.sm3(b"x" * n),
                             C.SM3(b"x" * n).digest())

    def test_copy_is_independent(self):
        h = C.SM3(b"abc")
        copy = h.copy()
        h.update(b"tail")
        self.assertEqual(copy.digest().hex(), SM3_ABC)
        self.assertNotEqual(h.digest(), copy.digest())

    def test_digest_is_repeatable(self):
        h = C.SM3(b"abc")
        self.assertEqual(h.digest(), h.digest())


class TestSM4(unittest.TestCase):
    def test_standard_vector(self):
        self.assertEqual(C.sm4_encrypt_block(SM4_KEY, SM4_KEY).hex(), SM4_CT)

    def test_decrypt_inverts(self):
        ct = C.sm4_encrypt_block(SM4_KEY, SM4_KEY)
        self.assertEqual(C.sm4_decrypt_block(SM4_KEY, ct), SM4_KEY)

    def test_ecb_multi_block(self):
        ct = C.sm4_ecb_encrypt(SM4_KEY, SM4_KEY * 4)
        self.assertEqual(len(ct), 64)
        for i in range(4):
            self.assertEqual(ct[16 * i:16 * i + 16].hex(), SM4_CT)
        self.assertEqual(C.sm4_ecb_decrypt(SM4_KEY, ct), SM4_KEY * 4)

    def test_cbc_round_trip_and_differs_from_ecb(self):
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        data = bytes(range(32))
        ct = C.sm4_cbc_encrypt(SM4_KEY, iv, data)
        self.assertEqual(len(ct), 32)
        self.assertEqual(C.sm4_cbc_decrypt(SM4_KEY, iv, ct), data)
        self.assertNotEqual(ct[:16], C.sm4_ecb_encrypt(SM4_KEY, data[:16]))
        self.assertNotEqual(ct[16:], C.sm4_ecb_encrypt(SM4_KEY, data[16:]))

    def test_bad_lengths_rejected(self):
        with self.assertRaises(C.SM4Error):
            C.sm4_encrypt_block(b"short", SM4_KEY)
        with self.assertRaises(C.SM4Error):
            C.sm4_encrypt_block(SM4_KEY, b"short")
        with self.assertRaises(C.SM4Error):
            C.sm4_ecb_encrypt(SM4_KEY, b"not-a-multiple")
        with self.assertRaises(C.SM4Error):
            C.sm4_ecb_encrypt(SM4_KEY, b"")
        with self.assertRaises(C.SM4Error):
            C.sm4_cbc_encrypt(SM4_KEY, b"short-iv", b"a" * 16)

    def test_sbox_is_a_permutation(self):
        # The S-box is the public GB/T 32907 constant.  A permutation check plus
        # a pinned digest catches any accidental edit (the first version of
        # crypto.py had the *key-schedule* linear map wrong, not the S-box).
        self.assertEqual(len(C._SM4_SBOX), 256)
        self.assertEqual(len(set(C._SM4_SBOX)), 256)
        self.assertEqual(bytes(C._SM4_SBOX[:8]).hex(), "d690e9fecce13db7")
        self.assertEqual(hashlib.sha256(bytes(C._SM4_SBOX)).hexdigest(),
                         "1674d0631a2e37b60ec748bc3899fee1550c108af30b85ce96a70c5ca4682d03")

    def test_round_keys_match_published_example(self):
        rk = C._sm4_key_schedule(SM4_KEY)
        self.assertEqual([f"{r:08x}" for r in rk[:4]],
                         ["f12186f9", "41662b61", "5a6ab19a", "7ba92077"])


class TestSM2(unittest.TestCase):
    def test_keypair_vector(self):
        kp = C.SM2Keypair.from_private_bytes(bytes.fromhex(SM2_D))
        self.assertEqual(kp.public_bytes.hex(), SM2_P)

    def test_generator_and_small_scalars(self):
        g = (C.CURVE.gx, C.CURVE.gy)
        self.assertEqual(C.SM2Keypair(1).point, g)
        self.assertEqual(C.SM2Keypair(2).point,
                         C._to_affine(C.CURVE, C._jac_double(C.CURVE, *g + (1,))))
        self.assertTrue(C.CURVE.on_curve(*g))

    def test_encode_decode(self):
        point = C.SM2Keypair.from_private_bytes(bytes.fromhex(SM2_D)).point
        raw = C.sm2_encode_point(point)
        self.assertEqual(len(raw), 64)
        self.assertEqual(C.sm2_decode_point(raw), point)
        self.assertEqual(C.sm2_decode_point(b"\x04" + raw), point)
        with self.assertRaises(C.SM2Error):
            C.sm2_decode_point(b"\x02" + raw)
        with self.assertRaises(C.SM2Error):
            C.sm2_decode_point(b"\x00" * 63)

    def test_compress_round_trip(self):
        point = C.SM2Keypair.from_private_bytes(bytes.fromhex(SM2_D)).point
        comp = C.sm2_compress_point(point)
        self.assertEqual(len(comp), 33)
        self.assertIn(comp[0], (0x02, 0x03))
        self.assertEqual(C.sm2_uncompress_point(comp), point)
        other = C.SM2Keypair.generate().point
        self.assertEqual(C.sm2_uncompress_point(C.sm2_compress_point(other)), other)

    def test_rejects_off_curve_point(self):
        with self.assertRaises(C.SM2Error):
            C.sm2_decode_point(bytes.fromhex(SM2_P)[:-1] + b"\x00")
        with self.assertRaises(C.SM2Error):
            C.sm2_decode_point(b"\x00" * 64)

    def test_ecdh_is_symmetric_and_matches_product(self):
        a = C.SM2Keypair.from_private_bytes(bytes(range(1, 33)))
        b = C.SM2Keypair.from_private_bytes(bytes(range(33, 65)))
        ab = a.ecdh(b.point)
        ba = b.ecdh(a.point)
        self.assertEqual(ab.point, ba.point)
        self.assertEqual(ab.encoded, ab.x_bytes + ab.y_bytes)
        # (d_a * d_b) * G must be the same point
        d = (a.scalar * b.scalar) % C.CURVE.n
        self.assertEqual(C.SM2Keypair(d).point, ab.point)

    def test_scalar_range(self):
        with self.assertRaises(C.SM2Error):
            C.SM2Keypair(0)
        with self.assertRaises(C.SM2Error):
            C.SM2Keypair(C.CURVE.n)
        with self.assertRaises(C.SM2Error):
            C.SM2Keypair.from_private_bytes(b"\x01" * 31)

    def test_generate_uses_rng_and_is_valid(self):
        class FixedRng:
            def __init__(self):
                self.calls = 0

            def __call__(self, n):
                self.calls += 1
                return bytes([7]) * n

        rng = FixedRng()
        kp = C.SM2Keypair.generate(rng)
        self.assertEqual(kp.scalar, int.from_bytes(b"\x07" * 32, "big"))
        self.assertTrue(C.CURVE.on_curve(*kp.point))
        self.assertEqual(rng.calls, 1)

    def test_generate_rejects_out_of_range_then_succeeds(self):
        class Rng:
            def __init__(self):
                self.values = [b"\x00" * 32, C.CURVE.n.to_bytes(32, "big"),
                               b"\x00" * 31 + b"\x01"]

            def __call__(self, n):
                return self.values.pop(0)

        kp = C.SM2Keypair.generate(Rng())
        self.assertEqual(kp.scalar, 1)

    def test_za_construction(self):
        kp = C.SM2Keypair.from_private_bytes(bytes.fromhex(SM2_D))
        uid = b"ALICE123@YAHOO.COM"
        za = C.sm2_z(kp.point, uid)
        self.assertEqual(len(za), 32)
        # ENTL is the bit length of the identifier (0x0090 = 18 bytes * 8)
        entl = (len(uid) * 8).to_bytes(2, "big")
        explicit = C.sm3(entl + uid +
                         C.CURVE.a.to_bytes(32, "big") +
                         C.CURVE.b.to_bytes(32, "big") +
                         C.CURVE.gx.to_bytes(32, "big") +
                         C.CURVE.gy.to_bytes(32, "big") +
                         C.sm2_encode_point(kp.point))
        self.assertEqual(za, explicit)
        self.assertNotEqual(za, C.sm2_z(kp.point, b"other-id"))


class TestKDF(unittest.TestCase):
    def test_matches_its_definition(self):
        z = bytes((i * 13 + 5) & 0xFF for i in range(64))
        for klen in (1, 15, 16, 31, 32, 33, 40, 64):
            want = b"".join(C.sm3(z + ctr.to_bytes(4, "big"))
                            for ctr in range(1, (klen + 31) // 32 + 1))[:klen]
            self.assertEqual(C.sm3_kdf(z, klen), want)

    def test_counter_is_big_endian_and_starts_at_one(self):
        z = b"\x00" * 64
        self.assertEqual(C.sm3_kdf(z, 32), C.sm3(z + b"\x00\x00\x00\x01"))
        self.assertEqual(C.sm3_kdf(z, 64)[32:],
                         C.sm3(z + b"\x00\x00\x00\x02"))

    def test_zero_length(self):
        self.assertEqual(C.sm3_kdf(b"anything", 0), b"")
        with self.assertRaises(ValueError):
            C.sm3_kdf(b"z", -1)

    def test_all_zero_mask_is_rejected_by_the_caller(self):
        """The driver rejects an all-zero KDF result (FUN_180007D08)."""
        shared = C.SM2Keypair.from_private_bytes(bytes.fromhex(SM2_D)).ecdh(
            C.SM2Keypair(3).point)
        real = C.sm3_kdf
        try:
            C.sm3_kdf = lambda z, klen: b"\x00" * klen
            with self.assertRaises(C.KDFAllZeroError):
                C.derive_session_key(shared, b"\x00" * 16, C.SessionParams())
        finally:
            C.sm3_kdf = real
        # and the normal path still works afterwards
        key = C.derive_session_key(shared, b"\x00" * 16, C.SessionParams())
        self.assertEqual(len(key), 16)


class TestKeyAndTag(unittest.TestCase):
    def _shared(self):
        return C.SM2Keypair.from_private_bytes(bytes(range(9, 41))).ecdh(
            C.SM2Keypair.from_private_bytes(bytes(range(41, 73))).point)

    def test_recovered_variant_is_default(self):
        shared = self._shared()
        key = bytes(range(16))
        mask = C.sm3_kdf(shared.encoded, 16)
        wrapped = bytes(a ^ b for a, b in zip(mask, key))
        self.assertEqual(C.derive_session_key(shared, wrapped), key)
        self.assertEqual(C.session_tag(shared, key),
                         C.sm3(shared.x_bytes + key + shared.y_bytes))

    def test_params_change_the_result(self):
        shared = self._shared()
        key = bytes(range(16))
        default = C.session_tag(shared, key)
        self.assertNotEqual(default, C.session_tag(
            shared, key, C.SessionParams(tag_layout="key_x_y")))
        self.assertNotEqual(default, C.session_tag(
            shared, key, C.SessionParams(tag_layout="point_key")))
        self.assertNotEqual(
            C.derive_session_key(shared, b"\x11" * 16),
            C.derive_session_key(shared, b"\x11" * 16,
                                 C.SessionParams(kdf_input="x_only")))

    def test_key_len_override(self):
        shared = self._shared()
        self.assertEqual(len(C.derive_session_key(shared, b"\x22" * 32,
                                                  C.SessionParams(key_len=32))), 32)
        with self.assertRaises(C.HandshakeError):
            C.derive_session_key(shared, b"\x22" * 8,
                                 C.SessionParams(key_len=16))

    def test_bad_params_rejected(self):
        with self.assertRaises(ValueError):
            C.SessionParams(kdf_input="nonsense")
        with self.assertRaises(ValueError):
            C.SessionParams(tag_layout="nonsense")
        with self.assertRaises(ValueError):
            C.SessionParams(key_len=0)


# --------------------------------------------------------------------------
# A synthetic module: our own "device side" of the handshake
# --------------------------------------------------------------------------

FakeDevice = support.FakeRootKeyDevice


class TestHandshakeSynthetic(unittest.TestCase):
    def test_end_to_end(self):
        device = FakeDevice()
        session = P.open_session(device, rng=lambda n: b"\x11" * n)
        self.assertIsInstance(session, C.SecureSession)
        self.assertEqual(session.key, device.session_key)
        # the request really is the documented 65-byte 0x04-tagged point
        request = P.parse_frame(device.requests[0])
        self.assertEqual(request.opcode, 0xA2)
        self.assertEqual(request.phase, 0)
        self.assertEqual(request.data_len, 65)
        self.assertEqual(request.data[0], 0x04)
        self.assertEqual(len(request.data), 65)

    def test_session_object_api(self):
        device = FakeDevice()
        sess = P.Session(transport=device, rng=lambda n: b"\x22" * n)
        frame = sess.request_frame()
        self.assertEqual(P.parse_frame(frame).data[0], 0x04)
        secure = sess.complete(sess.send(frame))
        self.assertEqual(secure.key, device.session_key)
        self.assertEqual(sess.answer.status, 0)
        self.assertEqual(sess.answer.key_len, 16)

    def test_secure_request_variant(self):
        device = FakeDevice(secure_requests=True)
        session = P.open_session(device, secure=True, rng=lambda n: b"\x33" * n)
        self.assertEqual(session.key, device.session_key)
        # the frame on the wire must not contain the plaintext 0x04 tag
        raw = device.requests[0]
        self.assertNotIn(b"\x04" + b"\x33" * 32, raw)

    def test_secure_answer_is_deciphered(self):
        device = FakeDevice(secure_answers=True)
        session = P.open_session(device, rng=lambda n: b"\x44" * n)
        self.assertEqual(session.key, device.session_key)

    def test_tampered_tag_is_rejected(self):
        device = FakeDevice(tamper_tag=True)
        with self.assertRaises(P.SessionError):
            P.open_session(device, rng=lambda n: b"\x55" * n)

    def test_refused_status_is_rejected(self):
        device = FakeDevice(status=0xF0)
        with self.assertRaises(C.HandshakeError):
            P.open_session(device, rng=lambda n: b"\x66" * n)

    def test_short_answer_is_rejected(self):
        device = FakeDevice(answer_override=b"\x00" * 0x40)
        with self.assertRaises(P.SessionError):
            P.open_session(device, rng=lambda n: b"\x77" * n)

    def test_no_transport(self):
        with self.assertRaises(P.SessionError):
            P.open_session(None)

    def test_junk_readback(self):
        class Junk:
            def exchange(self, frame, *, timeout_ms=1000):
                return b"\xff" * 64

        with self.assertRaises(P.SessionError):
            P.open_session(Junk())

    def test_32_byte_wire_key_variant(self):
        """``klen`` is a wire field; a 32-byte field derives a 32-byte key.

        Such an answer still verifies, but it cannot drive the module's 16-byte
        SM4 transform, so ``SecureSession`` refuses it instead of truncating.
        """
        params = C.SessionParams(key_len=32)
        device = FakeDevice(session_key=b"\xa5" * 32, params=params)
        host = C.SM2Keypair(0x99)
        raw = device.exchange(P.build_frame(P.Command.QUERY_ROOT_KEY, 0, 0,
                                            C.build_rootkey_payload(host)))
        answer = P.RootKeyAnswer.parse(P.parse_frame(raw).body)
        result = C.complete_handshake(host, answer, params)
        self.assertTrue(result.tag_ok)
        self.assertEqual(result.key, b"\xa5" * 32)
        with self.assertRaises(C.CryptoError):
            C.SecureSession(result.key)

    def test_variant_search_finds_non_default_layout(self):
        params = C.SessionParams(tag_layout="key_x_y", key_len=16)
        device = FakeDevice(params=params)
        session = P.open_session(device, rng=lambda n: b"\x99" * n, params=params)
        self.assertEqual(session.key, device.session_key)

        # and the bounded search, given one captured answer, must find it too
        host = C.SM2Keypair(0x1234)
        answer_raw = device.exchange(
            P.build_frame(P.Command.QUERY_ROOT_KEY, 0, 0,
                          C.build_rootkey_payload(host)))
        answer = P.RootKeyAnswer.parse(P.parse_frame(answer_raw).body)
        hits = C.search_variants(host, answer)
        self.assertTrue(
            any((h.params.kdf_input, h.params.tag_layout, h.params.key_len) ==
                (params.kdf_input, params.tag_layout, params.key_len)
                for h in hits), [h.params for h in hits])

    def test_variant_search_default_finds_exactly_the_recovered_layout(self):
        device = FakeDevice()
        host = C.SM2Keypair(0x4321)
        answer_raw = device.exchange(
            P.build_frame(P.Command.QUERY_ROOT_KEY, 0, 0,
                          C.build_rootkey_payload(host)))
        answer = P.RootKeyAnswer.parse(P.parse_frame(answer_raw).body)
        hits = C.search_variants(host, answer)
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0].params.kdf_input, hits[0].params.tag_layout),
                         (C.PARAMS_RECOVERED.kdf_input,
                          C.PARAMS_RECOVERED.tag_layout))
        self.assertEqual(hits[0].key, device.session_key)

    def test_complete_handshake_accepts_raw_payload(self):
        host = C.SM2Keypair(0x777)
        device = FakeDevice()
        raw = device.exchange(P.build_frame(P.Command.QUERY_ROOT_KEY, 0, 0,
                                            C.build_rootkey_payload(host)))
        body = P.parse_frame(raw).body
        result = C.complete_handshake(host, body)
        self.assertTrue(result.tag_ok)
        self.assertEqual(result.key, device.session_key)
        self.assertEqual(result.require_tag(), device.session_key)


class TestFrameTransform(unittest.TestCase):
    KEY = bytes(range(16))

    def test_seal_and_open_round_trip(self):
        session = C.SecureSession(self.KEY)
        frame = P.build_frame(P.Command.ENROLL_FINGERPRINT, P.Phase.START, 0,
                              b"\x01\x02\x03")
        sealed = session.seal_frame(frame)
        self.assertEqual(len(sealed), len(frame))
        # header is a randomized 16-byte value, the command block changed
        self.assertNotEqual(sealed[5:21], frame[5:21])
        self.assertNotEqual(sealed[21:-4], frame[21:-4])
        # byte 21 is now ciphertext: the magic, the opcode, data_len and the
        # padding all changed, but SOF, L and the CRC are still readable.
        self.assertNotEqual(sealed[21], 0x4C)
        self.assertEqual(sealed[0], P.SOF)
        self.assertEqual(sealed[1:5], frame[1:5])
        self.assertEqual(P.crc32(sealed[:-4]),
                         struct.unpack_from("<I", sealed, len(sealed) - 4)[0])
        opened = session.open_frame(sealed)
        self.assertEqual(opened, frame)
        self.assertEqual(P.parse_frame(opened).data, b"\x01\x02\x03")

    def test_plaintext_answer_with_zero_header_is_untouched(self):
        session = C.SecureSession(self.KEY)
        frame = P.build_frame(P.Command.QUERY_SENSOR_INFO, 0, 0, b"\x00" * 9)
        self.assertEqual(session.open_frame(frame), frame)

    def test_header_is_per_frame(self):
        session = C.SecureSession(self.KEY)
        frame = P.build_frame(P.Command.QUERY_SENSOR_INFO, 0, 0, b"\x00" * 9)
        a = session.seal_frame(frame)
        b = session.seal_frame(frame)
        self.assertNotEqual(a, b)

    def test_block_transform_round_trip_and_key_schedule(self):
        session = C.SecureSession(self.KEY)
        plain = bytes(range(32))
        header, ct = session.encrypt(plain)
        self.assertEqual(len(header), 16)
        self.assertEqual(session.decrypt(header, ct), plain)
        # H' = SM4(K, H) drives the blocks: re-deriving it must reproduce EM
        h2 = C.sm4_encrypt_block(self.KEY, header)
        self.assertEqual(ct, C.sm4_ecb_encrypt(h2, plain))

    def test_cbc_variant_round_trip(self):
        session = C.SecureSession(self.KEY)
        iv, ct = session.encrypt_cbc(b"z" * 32)
        self.assertEqual(session.decrypt_cbc(iv, ct), b"z" * 32)

    def test_bad_inputs(self):
        session = C.SecureSession(self.KEY)
        with self.assertRaises(C.CryptoError):
            C.SecureSession(b"short")
        with self.assertRaises(C.CryptoError):
            session.encrypt(b"123")
        with self.assertRaises(C.CryptoError):
            session.decrypt(b"123", b"a" * 16)
        with self.assertRaises(C.CryptoError):
            session.encrypt(b"a" * 16, header=b"short")


class TestSelftestAndBenchmark(unittest.TestCase):
    def test_selftest_passes(self):
        results = C.selftest()
        self.assertEqual(results["sm3_abc"], SM3_ABC)
        self.assertEqual(results["sm4_ecb"], SM4_CT)
        self.assertEqual(results["sm2_keypair"], SM2_P)

    def test_selftest_vectors_are_consistent(self):
        data, want = C.SELFTEST_VECTORS["sm3_abc"]
        self.assertEqual(C.sm3(data).hex(), want)
        key, want = C.SELFTEST_VECTORS["sm4_ecb"]
        self.assertEqual(C.sm4_encrypt_block(key, key).hex(), want)
        priv, pub = C.SELFTEST_VECTORS["sm2_keypair"]
        self.assertEqual(C.SM2Keypair.from_private_bytes(
            bytes.fromhex(priv)).public_bytes.hex(), pub)

    def test_benchmark_is_milliseconds(self):
        result = C.benchmark(rounds=2, size=4096)
        self.assertLess(result["sm3_seconds"], 0.5)
        self.assertLess(result["sm4_seconds"], 0.5)
        self.assertGreater(result["sm3_mbps"], 0.01)
        self.assertGreater(result["sm4_mbps"], 0.01)


class TestDependencyFree(unittest.TestCase):
    """The library must stay importable with nothing but the standard library."""

    def test_crypto_imports_only_stdlib(self):
        import ast

        source = pathlib.Path(C.__file__).read_text(encoding="utf-8")
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module:
                    modules.add(node.module.split(".")[0])
        allowed = {"__future__", "os", "struct", "time", "dataclasses"}
        self.assertEqual(modules - allowed, set(),
                         f"unexpected imports: {sorted(modules - allowed)}")

    def test_protocol_imports_only_stdlib(self):
        import ast

        source = pathlib.Path(P.__file__).read_text(encoding="utf-8")
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module:
                    modules.add(node.module.split(".")[0])
        allowed = {"__future__", "enum", "struct", "zlib", "dataclasses"}
        self.assertEqual(modules - allowed, set())


if __name__ == "__main__":
    unittest.main()
