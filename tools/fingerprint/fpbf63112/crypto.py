"""BF63112 session crypto: SM3, SM4, SM2, the GM/T 0003 KDF and the handshake.

This is a dependency-free (Python standard library only) implementation of the
exact cryptographic session layer of the Betterlife BF63112 module, recovered
from ``BetterlifeFingerprintDevice.dll`` 9.39.15.26 (see
``docs/hw/fingerprint.md`` for the function-by-function provenance).  It is our
own code: no vendor source, no vendor binary and no third-party library is
included or required.

The algorithms are the public Chinese national standards:

* SM3 hash - GB/T 32905-2016 (64-round Merkle-Damgard, 256-bit digest);
* SM4 block cipher - GB/T 32907-2016 (32 rounds, 128-bit block/key);
* SM2 elliptic curve - GB/T 32918, curve ``sm2p256v1``;
* KDF - GM/T 0003.4 counter-mode key derivation over SM3.

What the driver does (``FUN_180007AA0``, ``FUN_1800185A0``, ``FUN_180001A3C``),
which is what :func:`open_session` / :class:`Session` reproduce:

1. generate an ephemeral SM2 keypair ``(d, P = d*G)`` per open
   (``FUN_180007C28``: random ``d`` in ``[1, n-1]``, ``P`` stored as X||Y,
   32-byte big-endian each);
2. send ``0xA2`` with a 65-byte payload ``04 || X_P || Y_P``;
3. parse the answer ``status:u8 | P_dev:64 | tag:32 | klen:u32 LE | wrapped``;
4. compute ``S = d * P_dev`` and encode it as ``X_S || Y_S`` (64 bytes, BE);
5. ``K = KDF(S, klen)`` - counter mode over SM3, counter as a 32-bit
   big-endian integer starting at 1 (``FUN_18000945C``), and reject an
   all-zero KDF result (``FUN_180007D08``);
6. recover the session key as ``K xor wrapped`` (``FUN_180007A6C``);
7. verify ``tag == SM3(X_S || key || Y_S)`` (``__security_check_cookie``-framed
   SM3 calls in ``FUN_180007AA0``: X, then the key, then Y).

None of the steps depend on a static secret: the only long-lived key material
is inside the module.

What is *not* proven and is therefore parameterised (see :class:`SessionParams`
and :func:`search_variants`): the KDF input (full shared point vs x only) and
the tag layout.  The defaults are the recovered driver behaviour; the variant
search exists so a live session can settle the question in one run if the
module disagrees.

Wire encryption of secure frames (``FUN_180004358`` send, ``FUN_18001AC60``
receive) is also reproduced::

    H  = 16 random bytes          (the frame's header / per-frame nonce)
    H' = SM4_encrypt(K, H)        (key = session key, single ECB block)
    C  = SM4_encrypt_blocks(H', aligned command block)

and the receive direction is the same with the SM4 *decryption* key schedule
under the same key ``H'`` - i.e. it is ECB, not CBC.  The earlier note in
``docs/hw/fingerprint.md`` that the two paths were asymmetric was a
misreading of ``FUN_18000BFD0`` (which is the SM4 decryption key schedule,
not an IV load).

The functions here are deliberately small and side-effect free so the offline
tests can prove them against the published vectors; :func:`selftest` and
:func:`benchmark` are what ``oxp3-fp-probe.py --crypto-selftest`` runs.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass

__all__ = [
    # SM3
    "sm3", "SM3", "SM3_DIGEST_SIZE", "SM3_BLOCK_SIZE",
    # SM4
    "sm4_encrypt_block", "sm4_decrypt_block", "sm4_ecb_encrypt",
    "sm4_ecb_decrypt", "sm4_cbc_encrypt", "sm4_cbc_decrypt", "SM4_BLOCK_SIZE",
    # SM2
    "SM2Keypair", "SM2Point", "CURVE", "sm2_decode_point", "sm2_encode_point",
    "sm2_compress_point", "sm2_uncompress_point", "sm2_ecdh", "sm2_z",
    "SM2_ID_DEFAULT",
    # KDF / session
    "sm3_kdf", "KDFAllZeroError", "SessionParams", "SharedSecret",
    "derive_session_key", "session_tag", "HandshakeResult",
    "complete_handshake", "build_rootkey_payload", "rootkey_frame",
    "candidate_params", "search_variants", "SecureSession",
    # self-test / benchmark
    "SELFTEST_VECTORS", "selftest", "benchmark", "CryptoError",
]

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class CryptoError(Exception):
    """Base class for every cryptographic/layout failure in this module."""


class KDFAllZeroError(CryptoError):
    """The GM/T 0003 KDF produced an all-zero key (the standard forbids it)."""


class SM2Error(CryptoError):
    """Invalid SM2 point, scalar or encoding."""


class SM4Error(CryptoError):
    """Invalid SM4 input length or key."""


class HandshakeError(CryptoError):
    """The session handshake failed (bad tag, bad length, bad status)."""


# ==========================================================================
# SM3 - GB/T 32905-2016
# ==========================================================================

SM3_DIGEST_SIZE = 32
SM3_BLOCK_SIZE = 64

_SM3_IV = (0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
           0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E)

#: T_j = 0x79CC4519 for 0 <= j <= 15, else 0x7A879D8A (GB/T 32905 5.3.3),
#: pre-rotated by j (mod 32) as the standard specifies, so the round loop only
#: has to rotate the state word A.
_SM3_T = tuple(
    (((0x79CC4519 if j < 16 else 0x7A879D8A) << (j % 32)) |
     ((0x79CC4519 if j < 16 else 0x7A879D8A) >> (32 - (j % 32) if j % 32 else 32)))
    & 0xFFFFFFFF
    for j in range(64)
)

_M32 = 0xFFFFFFFF


def _rotl32(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & _M32 if n else x & _M32


def _sm3_compress(state: list[int], block: bytes) -> None:
    """One SM3 compression: 64 rounds over one 64-byte block (GB/T 32905 5.3).

    Written with inline shifts and no per-round helper calls: this is the hot
    loop for every hash the session layer computes (KDF blocks, the handshake
    tag, ZA), and ``benchmark()`` measures it on 4 KiB.
    """
    m = _M32
    w = list(struct.unpack(">16I", block))
    for j in range(16, 68):
        x = (w[j - 16] ^ w[j - 9] ^ ((w[j - 3] << 15) | (w[j - 3] >> 17))) & m
        x = (x ^ ((x << 15) | (x >> 17)) ^ ((x << 23) | (x >> 9))) & m
        w.append((x ^ ((w[j - 13] << 7) | (w[j - 13] >> 25)) ^ w[j - 6]) & m)
    wp = [w[i] ^ w[i + 4] for i in range(64)]

    a, b, c, d, e, f, g, h = state
    t = _SM3_T
    for j in range(16):
        a12 = ((a << 12) | (a >> 20)) & m
        tmp = (a12 + e + t[j]) & m
        ss1 = ((tmp << 7) | (tmp >> 25)) & m
        ss2 = ss1 ^ a12
        tt1 = ((a ^ b ^ c) + d + ss2 + wp[j]) & m
        tt2 = ((e ^ f ^ g) + h + ss1 + w[j]) & m
        d = c
        c = ((b << 9) | (b >> 23)) & m
        b = a
        a = tt1
        h = g
        g = ((f << 19) | (f >> 13)) & m
        f = e
        e = (tt2 ^ ((tt2 << 9) | (tt2 >> 23)) ^
             ((tt2 << 17) | (tt2 >> 15))) & m
    for j in range(16, 64):
        a12 = ((a << 12) | (a >> 20)) & m
        tmp = (a12 + e + t[j]) & m
        ss1 = ((tmp << 7) | (tmp >> 25)) & m
        ss2 = ss1 ^ a12
        tt1 = (((a & b) | (a & c) | (b & c)) + d + ss2 + wp[j]) & m
        tt2 = (((e & f) | ((e ^ m) & g)) + h + ss1 + w[j]) & m
        d = c
        c = ((b << 9) | (b >> 23)) & m
        b = a
        a = tt1
        h = g
        g = ((f << 19) | (f >> 13)) & m
        f = e
        e = (tt2 ^ ((tt2 << 9) | (tt2 >> 23)) ^
             ((tt2 << 17) | (tt2 >> 15))) & m
    state[0] ^= a
    state[1] ^= b
    state[2] ^= c
    state[3] ^= d
    state[4] ^= e
    state[5] ^= f
    state[6] ^= g
    state[7] ^= h


class SM3:
    """Streaming SM3 hash (GB/T 32905-2016).

    ``SM3(b"abc").digest()`` == :func:`sm3`.  ``copy()`` is supported so the
    driver's "hash this prefix twice" pattern stays cheap.
    """

    __slots__ = ("_state", "_buf", "_len")

    def __init__(self, data: bytes = b"") -> None:
        self._state = list(_SM3_IV)
        self._buf = b""
        self._len = 0
        if data:
            self.update(data)

    def copy(self) -> "SM3":
        dup = SM3()
        dup._state = list(self._state)
        dup._buf = self._buf
        dup._len = self._len
        return dup

    def update(self, data: bytes | bytearray | memoryview) -> "SM3":
        data = bytes(data)
        self._len += len(data)
        buf = self._buf + data
        pos = 0
        n = len(buf)
        state = self._state
        while n - pos >= SM3_BLOCK_SIZE:
            _sm3_compress(state, buf[pos:pos + SM3_BLOCK_SIZE])
            pos += SM3_BLOCK_SIZE
        self._buf = buf[pos:]
        return self

    def digest(self) -> bytes:
        state = list(self._state)
        buf = self._buf
        bitlen = (self._len * 8) & 0xFFFFFFFFFFFFFFFF
        pad = b"\x80" + b"\x00" * ((55 - len(buf)) % 64) + struct.pack(">Q", bitlen)
        data = buf + pad
        for off in range(0, len(data), SM3_BLOCK_SIZE):
            _sm3_compress(state, data[off:off + SM3_BLOCK_SIZE])
        return struct.pack(">8I", *state)

    def hexdigest(self) -> str:
        return self.digest().hex()


def sm3(data: bytes | bytearray | memoryview) -> bytes:
    """SM3 digest of *data* (32 bytes)."""
    return SM3(data).digest()


# ==========================================================================
# SM4 - GB/T 32907-2016
# ==========================================================================

SM4_BLOCK_SIZE = 16

_SM4_SBOX = (
    0xD6, 0x90, 0xE9, 0xFE, 0xCC, 0xE1, 0x3D, 0xB7, 0x16, 0xB6, 0x14, 0xC2,
    0x28, 0xFB, 0x2C, 0x05, 0x2B, 0x67, 0x9A, 0x76, 0x2A, 0xBE, 0x04, 0xC3,
    0xAA, 0x44, 0x13, 0x26, 0x49, 0x86, 0x06, 0x99, 0x9C, 0x42, 0x50, 0xF4,
    0x91, 0xEF, 0x98, 0x7A, 0x33, 0x54, 0x0B, 0x43, 0xED, 0xCF, 0xAC, 0x62,
    0xE4, 0xB3, 0x1C, 0xA9, 0xC9, 0x08, 0xE8, 0x95, 0x80, 0xDF, 0x94, 0xFA,
    0x75, 0x8F, 0x3F, 0xA6, 0x47, 0x07, 0xA7, 0xFC, 0xF3, 0x73, 0x17, 0xBA,
    0x83, 0x59, 0x3C, 0x19, 0xE6, 0x85, 0x4F, 0xA8, 0x68, 0x6B, 0x81, 0xB2,
    0x71, 0x64, 0xDA, 0x8B, 0xF8, 0xEB, 0x0F, 0x4B, 0x70, 0x56, 0x9D, 0x35,
    0x1E, 0x24, 0x0E, 0x5E, 0x63, 0x58, 0xD1, 0xA2, 0x25, 0x22, 0x7C, 0x3B,
    0x01, 0x21, 0x78, 0x87, 0xD4, 0x00, 0x46, 0x57, 0x9F, 0xD3, 0x27, 0x52,
    0x4C, 0x36, 0x02, 0xE7, 0xA0, 0xC4, 0xC8, 0x9E, 0xEA, 0xBF, 0x8A, 0xD2,
    0x40, 0xC7, 0x38, 0xB5, 0xA3, 0xF7, 0xF2, 0xCE, 0xF9, 0x61, 0x15, 0xA1,
    0xE0, 0xAE, 0x5D, 0xA4, 0x9B, 0x34, 0x1A, 0x55, 0xAD, 0x93, 0x32, 0x30,
    0xF5, 0x8C, 0xB1, 0xE3, 0x1D, 0xF6, 0xE2, 0x2E, 0x82, 0x66, 0xCA, 0x60,
    0xC0, 0x29, 0x23, 0xAB, 0x0D, 0x53, 0x4E, 0x6F, 0xD5, 0xDB, 0x37, 0x45,
    0xDE, 0xFD, 0x8E, 0x2F, 0x03, 0xFF, 0x6A, 0x72, 0x6D, 0x6C, 0x5B, 0x51,
    0x8D, 0x1B, 0xAF, 0x92, 0xBB, 0xDD, 0xBC, 0x7F, 0x11, 0xD9, 0x5C, 0x41,
    0x1F, 0x10, 0x5A, 0xD8, 0x0A, 0xC1, 0x31, 0x88, 0xA5, 0xCD, 0x7B, 0xBD,
    0x2D, 0x74, 0xD0, 0x12, 0xB8, 0xE5, 0xB4, 0xB0, 0x89, 0x69, 0x97, 0x4A,
    0x0C, 0x96, 0x77, 0x7E, 0x65, 0xB9, 0xF1, 0x09, 0xC5, 0x6E, 0xC6, 0x84,
    0x18, 0xF0, 0x7D, 0xEC, 0x3A, 0xDC, 0x4D, 0x20, 0x79, 0xEE, 0x5F, 0x3E,
    0xD7, 0xCB, 0x39, 0x48,
)

#: FK = (0xA3B1BAC6, 0x56AA3350, 0x677D9197, 0xB27022DC) - GB/T 32907 7.3.
_SM4_FK = (0xA3B1BAC6, 0x56AA3350, 0x677D9197, 0xB27022DC)

#: CK_i = (ck_i,0, ck_i,1, ck_i,2, ck_i,3), ck_i,j = (4i + j) * 7 mod 256.
_SM4_CK = tuple(
    ((4 * i + 0) * 7 % 256) << 24 | ((4 * i + 1) * 7 % 256) << 16 |
    ((4 * i + 2) * 7 % 256) << 8 | ((4 * i + 3) * 7 % 256)
    for i in range(32)
)


def _sm4_tau(b: int) -> int:
    """tau: the SM4 S-box applied to each byte of a 32-bit word."""
    return (_SM4_SBOX[b >> 24] << 24 | _SM4_SBOX[(b >> 16) & 0xFF] << 16 |
            _SM4_SBOX[(b >> 8) & 0xFF] << 8 | _SM4_SBOX[b & 0xFF])


def _sm4_l_linear(x: int) -> int:
    """The linear part of ``L``: rotations 2, 10, 18, 24 (GB/T 32907 7.4.2)."""
    return (x ^ _rotl32(x, 2) ^ _rotl32(x, 10) ^ _rotl32(x, 18) ^
            _rotl32(x, 24)) & _M32


def _sm4_l(b: int) -> int:
    """``L`` = ``L_linear`` o ``tau``: the round-function transform."""
    return _sm4_l_linear(_sm4_tau(b))


def _sm4_l_key(b: int) -> int:
    """``L'``: the *key-schedule* linear transform (rotations 13 and 23).

    GB/T 32907 uses two different linear maps: ``L`` in the 32 rounds and
    ``L'`` to derive the round keys.  Mixing them up still produces a
    self-consistent cipher that fails every published vector, which is exactly
    what the first version of this file did.
    """
    x = _sm4_tau(b)
    return (x ^ _rotl32(x, 13) ^ _rotl32(x, 23)) & _M32


#: Byte-sliced round tables: ``T(b) == T0[b>>24] ^ T1[(b>>16)&0xFF] ^
#: T2[(b>>8)&0xFF] ^ T3[b&0xFF]``.  ``L`` is linear over GF(2) and ``tau``
#: acts per byte, so each table must carry S-box output *only* in its own byte
#: position (the rest zero) before ``L`` is applied - applying ``tau`` to a
#: single-byte word would fill the other three bytes with ``SBOX[0]`` and
#: silently corrupt every block.  Built at import time from the standard
#: S-box, so there is one source of truth.
_SM4_T0 = tuple(_sm4_l_linear(_SM4_SBOX[b] << 24) for b in range(256))
_SM4_T1 = tuple(_sm4_l_linear(_SM4_SBOX[b] << 16) for b in range(256))
_SM4_T2 = tuple(_sm4_l_linear(_SM4_SBOX[b] << 8) for b in range(256))
_SM4_T3 = tuple(_sm4_l_linear(_SM4_SBOX[b]) for b in range(256))


def _sm4_key_schedule(key: bytes) -> list[int]:
    """Encryption round keys ``rk[0..31]`` (GB/T 32907 7.3)."""
    if len(key) != SM4_BLOCK_SIZE:
        raise SM4Error(f"SM4 key must be {SM4_BLOCK_SIZE} bytes, got {len(key)}")
    k = [int.from_bytes(key[i:i + 4], "big") ^ _SM4_FK[i // 4]
         for i in range(0, 16, 4)]
    rk = []
    for i in range(32):
        b = k[i + 1] ^ k[i + 2] ^ k[i + 3] ^ _SM4_CK[i]
        k.append((k[i] ^ _sm4_l_key(b)) & _M32)
        rk.append(k[i + 4])
    return rk


def _sm4_crypt_words(rk: list[int], words: tuple[int, int, int, int]) \
        -> tuple[int, int, int, int]:
    """32 SM4 rounds over four big-endian words, output word order reversed.

    Feeding ``rk`` yields encryption; feeding ``rk[::-1]`` yields decryption
    (SM4's decryption is the same network with the reversed key schedule).
    """
    x0, x1, x2, x3 = words
    t0 = _SM4_T0
    t1 = _SM4_T1
    t2 = _SM4_T2
    t3 = _SM4_T3
    for rk_i in rk:
        b = x1 ^ x2 ^ x3 ^ rk_i
        x0 ^= (t0[b >> 24] ^ t1[(b >> 16) & 0xFF] ^
               t2[(b >> 8) & 0xFF] ^ t3[b & 0xFF])
        x0 &= _M32
        x0, x1, x2, x3 = x1, x2, x3, x0
    return (x3, x2, x1, x0)


def sm4_encrypt_block(key: bytes, block: bytes) -> bytes:
    """One SM4 ECB block encryption."""
    if len(block) != SM4_BLOCK_SIZE:
        raise SM4Error(f"SM4 block must be {SM4_BLOCK_SIZE} bytes")
    words = struct.unpack(">4I", block)
    return struct.pack(">4I", *_sm4_crypt_words(_sm4_key_schedule(key), words))


def sm4_decrypt_block(key: bytes, block: bytes) -> bytes:
    """One SM4 ECB block decryption."""
    if len(block) != SM4_BLOCK_SIZE:
        raise SM4Error(f"SM4 block must be {SM4_BLOCK_SIZE} bytes")
    words = struct.unpack(">4I", block)
    return struct.pack(">4I", *_sm4_crypt_words(_sm4_key_schedule(key)[::-1], words))


def _sm4_ecb(key: bytes, data: bytes, decrypt: bool) -> bytes:
    if not data or len(data) % SM4_BLOCK_SIZE:
        raise SM4Error(
            f"SM4 ECB input must be a non-zero multiple of {SM4_BLOCK_SIZE} bytes,"
            f" got {len(data)}")
    rk = _sm4_key_schedule(key)
    if decrypt:
        rk = rk[::-1]
    out = bytearray(len(data))
    for off in range(0, len(data), SM4_BLOCK_SIZE):
        words = struct.unpack(">4I", data[off:off + SM4_BLOCK_SIZE])
        struct.pack_into(">4I", out, off, *_sm4_crypt_words(rk, words))
    return bytes(out)


def sm4_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """SM4-ECB over *data* (a multiple of 16 bytes)."""
    return _sm4_ecb(key, data, False)


def sm4_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """SM4-ECB decryption over *data* (a multiple of 16 bytes)."""
    return _sm4_ecb(key, data, True)


def sm4_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """SM4-CBC encryption, PKCS#7-padding-free (caller supplies whole blocks).

    The BF63112 wire format does **not** use CBC - see
    :meth:`SecureSession.encrypt` - but the variant is provided because it is
    the usual guess for an unknown vendor chaining mode and the live-session
    experiment plan may need it.
    """
    if len(iv) != SM4_BLOCK_SIZE:
        raise SM4Error("SM4-CBC IV must be 16 bytes")
    rk = _sm4_key_schedule(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), SM4_BLOCK_SIZE):
        block = bytes(a ^ b for a, b in zip(data[off:off + SM4_BLOCK_SIZE], prev))
        prev = struct.pack(">4I", *_sm4_crypt_words(rk, struct.unpack(">4I", block)))
        out += prev
    return bytes(out)


def sm4_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """SM4-CBC decryption (whole blocks, no padding removal)."""
    if len(iv) != SM4_BLOCK_SIZE:
        raise SM4Error("SM4-CBC IV must be 16 bytes")
    rk = _sm4_key_schedule(key)[::-1]
    out = bytearray()
    prev = iv
    for off in range(0, len(data), SM4_BLOCK_SIZE):
        block = data[off:off + SM4_BLOCK_SIZE]
        plain = struct.pack(">4I", *_sm4_crypt_words(rk, struct.unpack(">4I", block)))
        out += bytes(a ^ b for a, b in zip(plain, prev))
        prev = block
    return bytes(out)


# ==========================================================================
# SM2 over sm2p256v1 - GB/T 32918
# ==========================================================================

class _Curve:
    """The ``sm2p256v1`` domain parameters (GB/T 32918.5, 5.2)."""

    __slots__ = ("p", "a", "b", "n", "gx", "gy")

    def __init__(self) -> None:
        self.p = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFF
        self.a = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFC
        self.b = 0x28E9FA9E9D9F5E344D5A9E4BCF6509A7F39789F515AB8F92DDBCBD414D940E93
        self.n = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123
        self.gx = 0x32C4AE2C1F1981195F9904466A39C9948FE30BBFF2660BE1715A4589334C74C7
        self.gy = 0xBC3736A2F4F6779C59BDCEE36B692153D0A9877CC62A474002DF32E52139F0A0

    def on_curve(self, x: int, y: int) -> bool:
        p = self.p
        return (y * y - (x * x * x + self.a * x + self.b)) % p == 0


CURVE = _Curve()

#: Point at infinity, represented as ``None`` throughout this module.
_INF = None

SM2Point = tuple[int, int]


def _jac_double(p_: _Curve, X: int, Y: int, Z: int):
    if Z == 0 or Y == 0:
        return _INF
    p = p_.p
    yy = Y * Y % p
    s = 4 * X * yy % p
    zz = Z * Z % p
    m = (3 * X * X + p_.a * zz * zz) % p
    x3 = (m * m - 2 * s) % p
    y3 = (m * (s - x3) - 8 * yy * yy) % p
    z3 = 2 * Y * Z % p
    return x3, y3, z3


def _jac_add(p_: _Curve, P_, Q_):
    if P_ is _INF:
        return Q_
    if Q_ is _INF:
        return P_
    p = p_.p
    x1, y1, z1 = P_
    x2, y2, z2 = Q_
    z1z1 = z1 * z1 % p
    z2z2 = z2 * z2 % p
    u1 = x1 * z2z2 % p
    u2 = x2 * z1z1 % p
    s1 = y1 * z2 * z2z2 % p
    s2 = y2 * z1 * z1z1 % p
    if u1 == u2:
        if s1 != s2:
            return _INF
        return _jac_double(p_, x1, y1, z1)
    h = (u2 - u1) % p
    r = (s2 - s1) % p
    hh = h * h % p
    hhh = h * hh % p
    v = u1 * hh % p
    x3 = (r * r - hhh - 2 * v) % p
    y3 = (r * (v - x3) - s1 * hhh) % p
    z3 = h * z1 * z2 % p
    return x3, y3, z3


def _to_affine(p_: _Curve, P_):
    if P_ is _INF:
        return _INF
    x, y, z = P_
    if z == 0:
        return _INF
    p = p_.p
    zinv = pow(z, p - 2, p)
    zinv2 = zinv * zinv % p
    return x * zinv2 % p, y * zinv2 * zinv % p


def _point_mul(p_: _Curve, k: int, point: SM2Point) -> SM2Point:
    """``k * point`` (double-and-add, MSB first)."""
    k %= p_.n
    if k == 0:
        return _INF
    x, y = point
    acc = _INF
    addend = (x, y, 1)
    while k:
        if k & 1:
            acc = _jac_add(p_, acc, addend)
        addend = _jac_double(p_, *addend)
        k >>= 1
    return _to_affine(p_, acc)


def sm2_encode_point(point: SM2Point | None) -> bytes:
    """Uncompressed 64-byte encoding ``X || Y`` (no ``0x04`` prefix).

    The module's public keys travel as exactly these 64 bytes; the ``0x04``
    tag in the 0xA2 payload is a separate length tag, not an SEC1 prefix.
    """
    if point is _INF:
        raise SM2Error("cannot encode the point at infinity")
    x, y = point
    return x.to_bytes(32, "big") + y.to_bytes(32, "big")


def sm2_decode_point(data: bytes | bytearray | memoryview, *,
                     check_on_curve: bool = True) -> SM2Point:
    """Decode a 64-byte ``X || Y`` point (or 65 bytes with a 0x04 prefix)."""
    data = bytes(data)
    if len(data) == 65:
        if data[0] != 0x04:
            raise SM2Error(
                f"65-byte point must start with 0x04, got 0x{data[0]:02x}")
        data = data[1:]
    if len(data) != 64:
        raise SM2Error(f"SM2 point must be 64 bytes (X||Y), got {len(data)}")
    x = int.from_bytes(data[:32], "big")
    y = int.from_bytes(data[32:], "big")
    if x >= CURVE.p or y >= CURVE.p:
        raise SM2Error("SM2 point coordinate out of range")
    if check_on_curve and not CURVE.on_curve(x, y):
        raise SM2Error("SM2 point is not on sm2p256v1")
    return x, y


def sm2_compress_point(point: SM2Point) -> bytes:
    """SEC1 compressed encoding of a public key (33 bytes)."""
    x, y = point
    return bytes((0x02 | (y & 1),)) + x.to_bytes(32, "big")


def sm2_uncompress_point(data: bytes | bytearray | memoryview) -> SM2Point:
    """Decompress a 33-byte SEC1 point (``0x02``/``0x03 || X``)."""
    data = bytes(data)
    if len(data) != 33 or data[0] not in (0x02, 0x03):
        raise SM2Error("compressed SM2 point must be 33 bytes starting 0x02/0x03")
    p = CURVE.p
    x = int.from_bytes(data[1:], "big")
    if x >= p:
        raise SM2Error("SM2 x coordinate out of range")
    v = (x * x * x + CURVE.a * x + CURVE.b) % p
    y = pow(v, (p + 1) // 4, p)  # p % 4 == 3
    if y * y % p != v:
        raise SM2Error("x is not on sm2p256v1")
    if (y & 1) != (data[0] & 1):
        y = p - y
    return x, y


def sm2_ecdh(private_scalar: int, peer_point: SM2Point) -> SharedSecret:
    """SM2-ECDH: the shared point ``d * P_peer`` (as :class:`SharedSecret`)."""
    if not 1 <= private_scalar < CURVE.n:
        raise SM2Error("SM2 private scalar out of range [1, n-1]")
    if peer_point is _INF:
        raise SM2Error("cannot ECDH against the point at infinity")
    shared = _point_mul(CURVE, private_scalar, peer_point)
    if shared is _INF:
        raise SM2Error("ECDH produced the point at infinity")
    return SharedSecret(shared)


#: Default SM2 user identifier used by GB/T 32918 for signatures.
SM2_ID_DEFAULT = b"1234567812345678"


def sm2_z(public_point: SM2Point, uid: bytes = SM2_ID_DEFAULT) -> bytes:
    """SM2 ``Z`` value: ``SM3(ENTL || ID || a || b || xG || yG || xA || yA)``.

    The BF63112 handshake does **not** use this (its tag is a plain SM3 over
    the shared point and the key - see :func:`session_tag`); it is implemented
    because the module's ``SM3`` is also used elsewhere in the stack and
    because it gives an independent, standard-vector-checked exercise of the
    curve constants and point encoding.
    """
    entl = (len(uid) * 8) & 0xFFFF
    xa, ya = public_point
    parts = [entl.to_bytes(2, "big"), bytes(uid)]
    for value in (CURVE.a, CURVE.b, CURVE.gx, CURVE.gy, xa, ya):
        parts.append(value.to_bytes(32, "big"))
    return sm3(b"".join(parts))


class SM2Keypair:
    """An SM2 keypair (big-endian 32-byte scalar, affine public point)."""

    __slots__ = ("scalar", "point")

    def __init__(self, scalar: int, point: SM2Point | None = None) -> None:
        if not 1 <= scalar < CURVE.n:
            raise SM2Error("SM2 private scalar out of range [1, n-1]")
        self.scalar = scalar
        self.point = point if point is not None else \
            _point_mul(CURVE, scalar, (CURVE.gx, CURVE.gy))

    # ---- construction ----------------------------------------------------
    @classmethod
    def generate(cls, rng=None) -> "SM2Keypair":
        """Generate a keypair the way ``FUN_180007C28`` does (random d in [1,n-1])."""
        if rng is None:
            rng = os.urandom
        while True:
            d = int.from_bytes(rng(32), "big")
            if 1 <= d < CURVE.n:
                return cls(d)

    @classmethod
    def from_private_bytes(cls, scalar: bytes | bytearray | memoryview) \
            -> "SM2Keypair":
        scalar = bytes(scalar)
        if len(scalar) != 32:
            raise SM2Error(f"SM2 private key must be 32 bytes, got {len(scalar)}")
        return cls(int.from_bytes(scalar, "big"))

    # ---- accessors -------------------------------------------------------
    @property
    def private_bytes(self) -> bytes:
        return self.scalar.to_bytes(32, "big")

    @property
    def public_bytes(self) -> bytes:
        """64-byte ``X || Y`` public key, as sent in the 0xA2 payload."""
        return sm2_encode_point(self.point)

    def compressed_public_bytes(self) -> bytes:
        return sm2_compress_point(self.point)

    def ecdh(self, peer_point: SM2Point) -> "SharedSecret":
        return sm2_ecdh(self.scalar, peer_point)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SM2Keypair(x={self.point[0]:064x}, y={self.point[1]:064x})"


@dataclass(frozen=True)
class SharedSecret:
    """One SM2 shared point plus its canonical encodings."""

    point: SM2Point

    @property
    def x(self) -> int:
        return self.point[0]

    @property
    def y(self) -> int:
        return self.point[1]

    @property
    def x_bytes(self) -> bytes:
        return self.x.to_bytes(32, "big")

    @property
    def y_bytes(self) -> bytes:
        return self.y.to_bytes(32, "big")

    @property
    def encoded(self) -> bytes:
        """``X || Y`` - the 64-byte KDF input and tag input."""
        return self.x_bytes + self.y_bytes


# ==========================================================================
# GM/T 0003 KDF (counter mode over SM3)
# ==========================================================================

def sm3_kdf(z: bytes, klen: int) -> bytes:
    """GM/T 0003.4 KDF: ``SM3(z || BE32(ctr))`` for ``ctr = 1, 2, ...``.

    ``klen`` bytes are produced; the final block is truncated.  The all-zero
    result is *returned*, not silently rejected here: the standard puts that
    check on the caller, and the driver does it separately
    (``FUN_180007D08``).  :func:`derive_session_key` performs it.
    """
    if klen < 0:
        raise ValueError("klen must be non-negative")
    out = bytearray()
    ctr = 1
    while len(out) < klen:
        if ctr > 0xFFFFFFFF:
            raise ValueError("KDF output too long (counter overflow)")
        out += sm3(bytes(z) + ctr.to_bytes(4, "big"))
        ctr += 1
    return bytes(out[:klen])


# ==========================================================================
# Handshake parameters, key derivation and the tag
# ==========================================================================

@dataclass(frozen=True)
class SessionParams:
    """Knobs that are *recovered* (defaults) but kept switchable.

    The defaults reproduce ``FUN_180007AA0`` exactly:

    * ``kdf_input="shared_point"`` - the KDF consumes all 64 bytes ``X||Y`` of
      the shared point (the driver passes length ``0x40``);
    * ``tag_layout="x_key_y"`` - ``SM3(X_S || key || Y_S)``;
    * ``key_len=16`` - the module's SM4 key is 16 bytes.

    The alternatives exist only so a live session can identify the variant if
    the module ever disagrees with the recovered constants; they are never
    used by default and :func:`search_variants` bounds the search.
    """

    kdf_input: str = "shared_point"      # "shared_point" | "x_only"
    tag_layout: str = "x_key_y"          # x_key_y | key_x_y | point_key | key_point
    key_len: int | None = None           # None: take the wire klen
    verify_tag: bool = True

    def __post_init__(self) -> None:
        if self.kdf_input not in ("shared_point", "x_only"):
            raise ValueError(f"unknown kdf_input {self.kdf_input!r}")
        if self.tag_layout not in ("x_key_y", "key_x_y", "point_key", "key_point"):
            raise ValueError(f"unknown tag_layout {self.tag_layout!r}")
        if self.key_len is not None and not 1 <= self.key_len <= 64:
            raise ValueError("key_len must be in [1, 64]")

    @property
    def kdf_z(self):
        return lambda shared: (shared.encoded if self.kdf_input == "shared_point"
                               else shared.x_bytes)


#: The recovered driver behaviour - what :func:`open_session` uses by default.
PARAMS_RECOVERED = SessionParams()


def derive_session_key(shared: SharedSecret, wrapped: bytes,
                       params: SessionParams = PARAMS_RECOVERED) -> bytes:
    """``key = KDF(z, klen) xor wrapped``, rejecting an all-zero KDF result."""
    klen = params.key_len if params.key_len is not None else len(wrapped)
    if len(wrapped) < klen:
        raise HandshakeError(
            f"wrapped key is {len(wrapped)}B, need {klen}B")
    z = params.kdf_z(shared)
    mask = sm3_kdf(z, klen)
    if not any(mask):
        raise KDFAllZeroError("KDF produced an all-zero mask (GM/T 0003 rejects it)")
    return bytes(a ^ b for a, b in zip(mask, wrapped[:klen]))


def session_tag(shared: SharedSecret, key: bytes,
                params: SessionParams = PARAMS_RECOVERED) -> bytes:
    """The 32-byte integrity tag the module sends back (default ``X||key||Y``)."""
    x, y = shared.x_bytes, shared.y_bytes
    layout = params.tag_layout
    if layout == "x_key_y":
        data = x + key + y
    elif layout == "key_x_y":
        data = key + x + y
    elif layout == "point_key":
        data = shared.encoded + key
    elif layout == "key_point":
        data = key + shared.encoded
    else:  # pragma: no cover - guarded by SessionParams
        raise ValueError(layout)
    return sm3(data)


@dataclass(frozen=True)
class HandshakeResult:
    """Outcome of one 0xA2 exchange."""

    key: bytes
    tag: bytes
    tag_ok: bool
    shared: SharedSecret
    params: SessionParams
    answer_status: int
    key_len: int

    def require_tag(self) -> bytes:
        if not self.tag_ok:
            raise HandshakeError("SM3 tag mismatch: the answer was not accepted")
        return self.key


def build_rootkey_payload(keypair: SM2Keypair) -> bytes:
    """The 65-byte 0xA2 payload: tag ``0x04`` then the host public point.

    ``FUN_180001A3C`` points the descriptor at the 65-byte key blob at device
    context ``+0x16A78`` (``0x04`` at +0, X at +1, Y at +0x21) and sends
    ``len = 0x41``.
    """
    return b"\x04" + keypair.public_bytes


def complete_handshake(keypair: SM2Keypair, answer,
                       params: SessionParams = PARAMS_RECOVERED) \
        -> HandshakeResult:
    """Finish the handshake from a :class:`~.protocol.RootKeyAnswer`.

    *answer* may be the parsed object or the raw answer payload; a raw payload
    is parsed with :meth:`fpbf63112.protocol.RootKeyAnswer.parse`.
    """
    from .protocol import ProtocolError, RootKeyAnswer

    if not isinstance(answer, RootKeyAnswer):
        try:
            answer = RootKeyAnswer.parse(answer)
        except ProtocolError as exc:
            raise HandshakeError(f"cannot parse the 0xA2 answer: {exc}") from exc
    if answer.status not in (0, 1):
        raise HandshakeError(
            f"module refused the root-key request (status 0x{answer.status:02x})")
    peer = sm2_decode_point(answer.public_key)
    shared = keypair.ecdh(peer)
    wrapped = answer.wrapped_key
    klen = params.key_len if params.key_len is not None else len(wrapped)
    if len(wrapped) < klen:
        raise HandshakeError(f"answer carries {len(wrapped)}B, need {klen}B")
    key = derive_session_key(shared, wrapped[:klen], params)
    tag = session_tag(shared, key, params)
    return HandshakeResult(key=key, tag=tag, tag_ok=(tag == answer.tag),
                           shared=shared, params=params,
                           answer_status=answer.status, key_len=klen)


# --------------------------------------------------------------------------
# Variant search: the bounded fallback if the module disagrees
# --------------------------------------------------------------------------

def candidate_params() -> list[SessionParams]:
    """The small, bounded set of session variants worth trying live."""
    out: list[SessionParams] = []
    for kdf_input in ("shared_point", "x_only"):
        for tag_layout in ("x_key_y", "key_x_y", "point_key", "key_point"):
            for key_len in (16, 32):
                out.append(SessionParams(kdf_input=kdf_input,
                                         tag_layout=tag_layout,
                                         key_len=key_len,
                                         verify_tag=False))
    return out


def search_variants(keypair: SM2Keypair, answer,
                    candidates: list[SessionParams] | None = None) \
        -> list[HandshakeResult]:
    """Try every candidate parameter set against one captured answer.

    Returns the results whose SM3 tag matched, cheapest first (16-byte keys
    before 32-byte ones - the module's key is 16 bytes).  Bounded: at most
    ``len(candidate_params())`` trials, no network and no hardware access.
    Empty list means none of the variants fit, i.e. the answer was not a valid
    exchange or the algorithm differs in a way this file has not modelled.
    """
    from .protocol import ProtocolError, RootKeyAnswer

    if not isinstance(answer, RootKeyAnswer):
        try:
            answer = RootKeyAnswer.parse(answer)
        except ProtocolError as exc:
            raise HandshakeError(f"cannot parse the 0xA2 answer: {exc}") from exc
    if candidates is None:
        candidates = candidate_params()
    peer = sm2_decode_point(answer.public_key)
    shared = keypair.ecdh(peer)
    hits: list[HandshakeResult] = []
    for params in candidates:
        klen = params.key_len if params.key_len is not None else len(answer.wrapped_key)
        if len(answer.wrapped_key) < klen:
            continue
        try:
            key = derive_session_key(shared, answer.wrapped_key[:klen], params)
        except CryptoError:
            continue
        if session_tag(shared, key, params) == answer.tag:
            hits.append(HandshakeResult(key=key, tag=answer.tag, tag_ok=True,
                                        shared=shared, params=params,
                                        answer_status=answer.status,
                                        key_len=klen))
    hits.sort(key=lambda r: r.key_len)
    return hits


# ==========================================================================
# Secure frame transform (the module's "SM4 over the command block")
# ==========================================================================

class SecureSession:
    """An established session key plus the frame transform it drives.

    ``key`` is the 16-byte SM4 session key the module handed over.  The
    transform is exactly ``FUN_180004358``::

        H' = SM4_encrypt(key, H)                 # H = per-frame 16-byte header
        C  = SM4_encrypt(H', plaintext)          # blockwise, 16-byte blocks

    and the receive direction uses the SM4 *decryption* key schedule under the
    same ``H'``.  ``encrypt``/``decrypt`` operate on 16-byte-aligned buffers;
    :func:`seal_frame` / :func:`open_frame` wrap a whole protocol frame.
    """

    __slots__ = ("key", "_rng")

    def __init__(self, key: bytes, rng=None) -> None:
        key = bytes(key)
        if len(key) != SM4_BLOCK_SIZE:
            raise CryptoError(
                f"session key must be {SM4_BLOCK_SIZE} bytes, got {len(key)}")
        self.key = key
        self._rng = rng or os.urandom

    # ---- per-frame header ------------------------------------------------
    def frame_header(self) -> bytes:
        """A fresh random 16-byte per-frame header nonce."""
        header = self._rng(SM4_BLOCK_SIZE)
        if not any(header):  # the receiver treats an all-zero header as plaintext
            header = b"\x01" + header[1:]
        return header

    def _derived_key(self, header: bytes) -> bytes:
        """``H' = SM4_encrypt(key, H)`` - the per-frame block key."""
        return sm4_encrypt_block(self.key, bytes(header))

    # ---- block-level API (what the wire uses) ----------------------------
    def encrypt(self, plain: bytes, header: bytes | None = None) \
            -> tuple[bytes, bytes]:
        """Encrypt a 16-byte-aligned buffer; returns ``(header, ciphertext)``."""
        if not plain or len(plain) % SM4_BLOCK_SIZE:
            raise CryptoError("secure payload must be a non-zero multiple of 16B")
        if header is None:
            header = self.frame_header()
        if len(header) != SM4_BLOCK_SIZE:
            raise CryptoError("frame header must be 16 bytes")
        return header, sm4_ecb_encrypt(self._derived_key(header), bytes(plain))

    def decrypt(self, header: bytes, cipher: bytes) -> bytes:
        """Decrypt a 16-byte-aligned buffer using its frame header."""
        if not cipher or len(cipher) % SM4_BLOCK_SIZE:
            raise CryptoError("secure payload must be a non-zero multiple of 16B")
        return sm4_ecb_decrypt(self._derived_key(bytes(header)), bytes(cipher))

    def encrypt_cbc(self, plain: bytes, iv: bytes | None = None) \
            -> tuple[bytes, bytes]:
        """Non-default variant: SM4-CBC under the session key.

        Only useful if a live capture shows the module using CBC; the driver
        does not.
        """
        iv = self.frame_header() if iv is None else bytes(iv)
        return iv, sm4_cbc_encrypt(self.key, iv, bytes(plain))

    def decrypt_cbc(self, iv: bytes, cipher: bytes) -> bytes:
        return sm4_cbc_decrypt(self.key, bytes(iv), bytes(cipher))

    # ---- frame-level API -------------------------------------------------
    def seal_frame(self, frame: bytes) -> bytes:
        """Turn a plaintext BF63112 frame into a secure frame.

        The 16-byte header is replaced by a random nonce, the command block
        (``magic .. padding``, ``L - 20`` bytes) is encrypted, and the CRC-32 is
        recomputed over the modified frame.
        """
        from .protocol import FRAME_OFF_MAGIC, crc32, parse_frame

        frame = bytes(frame)
        parsed = parse_frame(frame)
        if parsed.length - 20 != parsed.align:
            raise CryptoError("frame length is inconsistent")
        block = frame[FRAME_OFF_MAGIC:FRAME_OFF_MAGIC + parsed.align]
        header, cipher = self.encrypt(block)
        out = bytearray(frame)
        out[5:5 + SM4_BLOCK_SIZE] = header
        out[FRAME_OFF_MAGIC:FRAME_OFF_MAGIC + parsed.align] = cipher
        del out[-4:]
        out += struct.pack("<I", crc32(bytes(out)))
        return bytes(out)

    def open_frame(self, frame: bytes) -> bytes:
        """Inverse of :meth:`seal_frame` (answer decryption).

        The frame's CRC-32 is verified on the *received* (still enciphered)
        bytes first - that is exactly what ``FUN_18001AC60`` does before it
        decrypts - and the returned plaintext frame gets a fresh CRC so it can
        go straight through :func:`~.protocol.parse_frame`.
        """
        from .protocol import (ALIGN, CRCMismatchError, FRAME_MIN_SIZE,
                               FRAME_OFF_MAGIC, SOF, crc32)

        frame = bytes(frame)
        # Only the structural envelope can be read while the command block is
        # still ciphertext: SOF, L, CRC (this is the driver's order too - the
        # 'L' magic is checked *after* decryption).
        if len(frame) < FRAME_MIN_SIZE or frame[0] != SOF:
            raise CryptoError("not a BF63112 frame")
        length = struct.unpack_from("<I", frame, 1)[0]
        if len(frame) != length + 5:
            raise CryptoError(f"L={length} implies {length + 5}B, got {len(frame)}B")
        if struct.unpack_from("<I", frame, len(frame) - 4)[0] != crc32(frame[:-4]):
            raise CRCMismatchError(crc32(frame[:-4]),
                                   struct.unpack_from("<I", frame, len(frame) - 4)[0])
        align = length - 20
        if align < ALIGN or align % ALIGN:
            raise CryptoError(f"command block length {align} is not aligned")
        header = frame[5:5 + SM4_BLOCK_SIZE]
        if not any(header):
            return frame            # plaintext answer
        cipher = frame[FRAME_OFF_MAGIC:FRAME_OFF_MAGIC + align]
        block = self.decrypt(header, cipher)
        out = bytearray(frame)
        out[FRAME_OFF_MAGIC:FRAME_OFF_MAGIC + align] = block
        # The header was a random per-frame nonce; a plaintext frame carries
        # zeros there, so restoring them makes the round trip byte-exact and
        # keeps the result a canonical request/answer.
        out[5:5 + SM4_BLOCK_SIZE] = b"\x00" * SM4_BLOCK_SIZE
        del out[-4:]
        out += struct.pack("<I", crc32(bytes(out)))
        return bytes(out)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SecureSession(key={self.key.hex()[:8]}...)"


# ==========================================================================
# Self-test vectors (public standards) and benchmarks
# ==========================================================================

#: Official vectors.  Each entry carries where it comes from; the test suite
#: and ``oxp3-fp-probe.py --crypto-selftest`` both consume this one table.
SELFTEST_VECTORS = {
    # GB/T 32905-2016 Appendix A (and the well-known "abc" vector).
    "sm3_abc":
        (b"abc",
         "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"),
    "sm3_abcd64":
        (b"abcd" * 16,
         "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732"),
    # GB/T 32907-2016 Appendix A.1: key == plaintext.
    "sm4_ecb":
        (bytes.fromhex("0123456789abcdeffedcba9876543210"),
         "681edf34d206965e86b3e94f536e4246"),
    # The published SM2 sample private key (the one reused by the GB/T 32918 /
    # GM/T 0003 examples) and the public point it must produce - an independent
    # check of the curve arithmetic, since the pair was not derived here.
    "sm2_keypair":
        ("3945208f7b2144b13f36e38ac6d39f95889393692860b51a42fb81ef4df7c5b8",
         "09f9df311e5421a150dd7d161e4bc5c672179fad1833fc076bb08ff356f35020"
         "ccea490ce26775a52dc6ea718cc1aa600aed05fbf35e084a6632f6072da9ad13"),
}


def selftest(verbose: bool = False) -> dict:
    """Run the published-vector checks; returns a result dict (raises on failure).

    Deliberately mirrors the unittest suite so the CLI self-test can run on the
    target host without importing the tests.
    """
    results: dict[str, str] = {}

    for name in ("sm3_abc", "sm3_abcd64"):
        data, want = SELFTEST_VECTORS[name]
        got = sm3(data).hex()
        if got != want:
            raise CryptoError(f"{name}: SM3 gave {got}, expected {want}")
        results[name] = got

    key, want = SELFTEST_VECTORS["sm4_ecb"]
    got = sm4_encrypt_block(key, key).hex()
    if got != want:
        raise CryptoError(f"sm4_ecb: got {got}, expected {want}")
    if sm4_decrypt_block(key, bytes.fromhex(want)) != key:
        raise CryptoError("sm4_ecb: decryption does not invert encryption")
    results["sm4_ecb"] = got

    priv_hex, pub_hex = SELFTEST_VECTORS["sm2_keypair"]
    kp = SM2Keypair.from_private_bytes(bytes.fromhex(priv_hex))
    if kp.public_bytes.hex() != pub_hex:
        raise CryptoError("sm2_keypair: public point does not match the vector")
    results["sm2_keypair"] = pub_hex

    # ZA is not used by the handshake, so there is no published vector to
    # compare against here; check the construction instead (GB/T 32918 5.5).
    kp = SM2Keypair.from_private_bytes(bytes.fromhex(priv_hex))
    uid = b"ALICE123@YAHOO.COM"
    entl = (len(uid) * 8).to_bytes(2, "big")
    want_za = sm3(entl + uid + CURVE.a.to_bytes(32, "big") +
                  CURVE.b.to_bytes(32, "big") + CURVE.gx.to_bytes(32, "big") +
                  CURVE.gy.to_bytes(32, "big") + kp.public_bytes)
    if sm2_z(kp.point, uid) != want_za:
        raise CryptoError("sm2_za: ZA does not match its definition")
    results["sm2_za"] = want_za.hex()

    # Round-trips that need no published vector.
    point = kp.point
    if sm2_decode_point(sm2_encode_point(point)) != point:
        raise CryptoError("SM2 point encode/decode round-trip failed")
    if sm2_uncompress_point(sm2_compress_point(point)) != point:
        raise CryptoError("SM2 compress/uncompress round-trip failed")
    a, b = SM2Keypair.generate(), SM2Keypair.generate()
    if a.ecdh(b.point).point != b.ecdh(a.point).point:
        raise CryptoError("SM2 ECDH is not symmetric")
    results["sm2_ecdh"] = "symmetric"

    if verbose:  # pragma: no cover - CLI aid
        for name, value in results.items():
            print(f"  {name}: {value}")
    return results


def benchmark(rounds: int = 8, size: int = 4096) -> dict:
    """Measure SM3/SM4 throughput on *size* bytes (default 4 KiB)."""
    payload = bytes((i * 37 + 11) & 0xFF for i in range(size))
    key = bytes(range(16))

    def _best(fn) -> float:
        best = float("inf")
        for _ in range(rounds):
            start = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - start)
        return best

    sm3_s = _best(lambda: sm3(payload))
    sm4_s = _best(lambda: sm4_ecb_encrypt(key, payload))
    sm2_s = _best(lambda: SM2Keypair.generate())
    return {
        "size_bytes": size,
        "sm3_seconds": sm3_s,
        "sm4_seconds": sm4_s,
        "sm2_keygen_seconds": sm2_s,
        "sm3_mbps": size / sm3_s / 1e6,
        "sm4_mbps": size / sm4_s / 1e6,
    }
