/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_session_openssl.h - BF63112 session crypto (SM2/SM3/SM4) helper.
 *
 * The Python library (tools/fingerprint/fpbf63112/crypto.py) is the tested
 * reference implementation; this C module is the driver-side equivalent, kept
 * behind BF63112_ENABLE_SESSION so the default driver build stays free of a
 * crypto dependency and cannot accidentally enable a code path that has not
 * been run on hardware.
 *
 * The OpenSSL call sequence it uses (and why) is documented in the .c file and
 * in tools/fingerprint/README.md.  Compile check:
 *
 *   cc -std=gnu11 -Wall -Wextra -Werror -fsyntax-only \
 *      -DBF63112_ENABLE_SESSION bf63112_session_openssl.c
 *
 * needs openssl-devel; the *runtime* needs an OpenSSL provider with SM2/SM3/
 * SM4 (OpenSSL >= 3.0 default provider, or a vendor build that enables them -
 * the ONEXPLAYER 3 host's openssl 4.0.3 does NOT, see the README).
 */

#ifndef BF63112_SESSION_OPENSSL_H
#define BF63112_SESSION_OPENSSL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/** Length of the SM4 session key the module hands over. */
#define BF63112_SESSION_KEY_LEN 16
/** Length of the SM3 integrity tag in the 0xA2 answer. */
#define BF63112_SESSION_TAG_LEN 32
/** Length of the 0xA2 request payload: 0x04 tag + X + Y. */
#define BF63112_SESSION_REQUEST_LEN 65
/** Minimum 0xA2 answer payload (the Windows handler rejects anything shorter). */
#define BF63112_SESSION_ANSWER_MIN 0x71
/** The secure-frame block size (SM4). */
#define BF63112_SESSION_BLOCK 16
/** Domain parameters digest: sm2p256v1 public key + scalar. */
#define BF63112_SESSION_POINT_LEN 64

/** One ephemeral host keypair: X||Y (big endian) and the private scalar. */
typedef struct
{
  uint8_t public_key[BF63112_SESSION_POINT_LEN];
  uint8_t private_key[32];
} Bf63112Keypair;

/** An established session: the 16-byte SM4 key and whether it verified. */
typedef struct
{
  uint8_t key[BF63112_SESSION_KEY_LEN];
  bool ready;
} Bf63112Session;

/* SM3 hash over @len bytes at @data (GB/T 32905-2016). */
int bf63112_sm3 (const uint8_t *data, size_t len, uint8_t out[32]);

/* GM/T 0003 counter-mode KDF over SM3: SM3(z || BE32(ctr)) for ctr = 1, 2, ... */
int bf63112_kdf (const uint8_t *z, size_t zlen, size_t klen, uint8_t *out);

/* SM4-ECB over @len bytes (a multiple of 16).  @decrypt selects the direction. */
int bf63112_sm4_ecb (const uint8_t key[BF63112_SESSION_KEY_LEN],
                     const uint8_t *in, size_t len, uint8_t *out,
                     bool decrypt);

/*
 * Seal/open one 16-byte-aligned command block the way the vendor driver does:
 *   H' = SM4_encrypt (key, header);  block = SM4_{enc,dec} (H', block)
 * @header is the 16-byte frame header (the per-frame nonce).  @block and
 * @out may be the same buffer.
 */
int bf63112_sm4_seal (const Bf63112Session *session,
                      const uint8_t header[BF63112_SESSION_BLOCK],
                      uint8_t *block, size_t len);
int bf63112_sm4_open (const Bf63112Session *session,
                      const uint8_t header[BF63112_SESSION_BLOCK],
                      uint8_t *block, size_t len);

/* Fill @kp with a fresh sm2p256v1 keypair (the 0xA2 ephemeral key). */
int bf63112_keypair_generate (Bf63112Keypair *kp);

/* Build the 65-byte 0xA2 payload: 0x04 || X || Y. */
int bf63112_rootkey_request (const Bf63112Keypair *kp,
                             uint8_t out[BF63112_SESSION_REQUEST_LEN]);

/*
 * Finish the handshake from the 0xA2 answer body (the payload the frame parser
 * hands over): status:u8 | P_dev:64 | tag:32 | klen:u32 LE | wrapped key.
 * Returns 0 and sets session->ready only when the SM3 tag verifies.
 */
int bf63112_session_complete (Bf63112Session *session, const Bf63112Keypair *kp,
                              const uint8_t *answer, size_t answer_len);

/* Stable, human-readable description of a negative return code. */
const char *bf63112_session_strerror (int rc);

/* Overwrite @len bytes at @buf (OPENSSL_cleanse); keeps OpenSSL headers out
 * of the driver, which only includes this file. */
void bf63112_session_wipe (void *buf, size_t len);

#endif /* BF63112_SESSION_OPENSSL_H */
