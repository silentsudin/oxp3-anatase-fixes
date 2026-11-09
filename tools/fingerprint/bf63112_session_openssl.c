/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_session_openssl.c - BF63112 session crypto via OpenSSL.
 *
 * This is the C side of the handshake recovered from
 * BetterlifeFingerprintDevice.dll 9.39.15.26 (FUN_180007C28 keygen,
 * FUN_180001A3C 0xA2 request, FUN_1800185A0 answer handler,
 * FUN_180007AA0 derive/verify, FUN_180004358 / FUN_18001AC60 secure frames).
 * It mirrors tools/fingerprint/fpbf63112/crypto.py, which is the reference
 * implementation and the one covered by the published test vectors.
 *
 * WHY THESE APIS (and not the obvious ones)
 * -----------------------------------------
 * * SM3: EVP_MD_fetch(NULL, "SM3", NULL) + EVP_DigestInit_ex2/Update/Final_ex.
 *   The one-shot EVP_sm3() exists but the KDF needs incremental hashing of a
 *   64-byte prefix plus a 4-byte counter, so the EVP_MD_CTX path is used.
 *   The fetch API (not EVP_get_digestbyname) is required under OpenSSL 3+
 *   because SM3 lives in the default provider, not in the legacy one.
 * * SM4: EVP_CIPHER_fetch(NULL, "SM4-ECB", NULL) with
 *   EVP_CIPHER_CTX_set_padding(ctx, 0) and EVP_EncryptUpdate /
 *   EVP_DecryptUpdate.  ECB is correct here: the vendor re-keys every frame
 *   from H' = E_K(H) and transforms each 16-byte block independently, so this
 *   is not CBC (see docs/hw/fingerprint.md).  Direction is chosen with the
 *   init function, which is the same key schedule the driver uses.
 * * ECDH: EVP_PKEY_derive() on an SM2 EVP_PKEY would be the natural call, but
 *   OpenSSL's SM2 derive returns the 32-byte *x coordinate only*.  The vendor
 *   KDF consumes the full 64-byte shared point X||Y (FUN_18000945C is called
 *   with length 0x40), and the SM3 tag covers X and Y separately, so this
 *   module uses the EC layer instead:
 *     EC_GROUP_new_by_curve_name(NID_sm2)
 *     EC_POINT_set_affine_coordinates(group, peer, x, y, ctx)
 *     EC_POINT_mul(group, shared, NULL, peer, d, ctx)
 *     EC_POINT_get_affine_coordinates(group, shared, x, y, ctx)
 *     BN_bn2binpad(x, out, 32); BN_bn2binpad(y, out + 32, 32)
 *   That yields exactly the 64 bytes the firmware expects.  (If a future
 *   OpenSSL exposes the y coordinate through EVP, this can be simplified.)
 * * Comparison: CRYPTO_memcmp() for the tag, never memcmp().
 *
 * WHAT IS PROVEN AND WHAT IS NOT
 * ------------------------------
 * The algorithms, the KDF construction, the tag construction and the frame
 * transform are proven against the Python reference (which is vector-tested)
 * and against published SM3/SM4/SM2 vectors.  NOT proven: that the firmware
 * accepts this handshake - nothing here has run against the sensor.  The flag
 * BF63112_ENABLE_SESSION must stay off until a live session succeeds; see the
 * experiment plan in docs/hw/fingerprint.md.
 */

#include "bf63112_session_openssl.h"

#include <errno.h>
#include <string.h>

#include <openssl/bn.h>
#include <openssl/core_names.h>
#include <openssl/crypto.h>
#include <openssl/ec.h>
#include <openssl/evp.h>
#include <openssl/obj_mac.h>
#include <openssl/rand.h>

/*
 * Some distributions build OpenSSL without SM2 (Fedora's openssl 4.0.3 sets
 * OPENSSL_NO_SM2, which is why the ONEXPLAYER 3 host cannot use this path).
 * The module still compiles there - so the same source can be syntax-checked
 * on every host - but the two SM2 entry points report -ENOTSUP at runtime
 * instead of pretending the curve exists.  SM3/SM4 are fetched by name and
 * degrade the same way when the provider lacks them.
 */
#if defined(OPENSSL_NO_SM2) || !defined(NID_sm2)
# define BF63112_HAVE_SM2 0
#else
# define BF63112_HAVE_SM2 1
#endif

/* Wire layout of the 0xA2 answer body (FUN_1800185A0). */
#define ANS_OFF_STATUS 0x00
#define ANS_OFF_POINT 0x01
#define ANS_OFF_TAG 0x41
#define ANS_OFF_KLEN 0x61
#define ANS_OFF_WRAPPED 0x65

static int
bf63112_sm3_ctx_init (EVP_MD_CTX **out)
{
  EVP_MD *md = EVP_MD_fetch (NULL, "SM3", NULL);
  EVP_MD_CTX *ctx;
  int rc;

  if (md == NULL)
    return -ENOTSUP; /* provider without SM3 */

  ctx = EVP_MD_CTX_new ();
  if (ctx == NULL)
    {
      EVP_MD_free (md);
      return -ENOMEM;
    }
  rc = EVP_DigestInit_ex2 (ctx, md, NULL);
  EVP_MD_free (md); /* the context keeps its own reference */
  if (rc != 1)
    {
      EVP_MD_CTX_free (ctx);
      return -EIO;
    }
  *out = ctx;
  return 0;
}

static int
bf63112_sm3_final (EVP_MD_CTX *ctx, uint8_t out[32])
{
  unsigned int len = 0;
  int rc = EVP_DigestFinal_ex (ctx, out, &len);

  EVP_MD_CTX_free (ctx);
  if (rc != 1 || len != BF63112_SESSION_TAG_LEN)
    return -EIO;
  return 0;
}

int
bf63112_sm3 (const uint8_t *data, size_t len, uint8_t out[32])
{
  EVP_MD_CTX *ctx = NULL;
  int rc;

  if (out == NULL || (data == NULL && len != 0))
    return -EINVAL;

  rc = bf63112_sm3_ctx_init (&ctx);
  if (rc != 0)
    return rc;
  if (len != 0 && EVP_DigestUpdate (ctx, data, len) != 1)
    {
      EVP_MD_CTX_free (ctx);
      return -EIO;
    }
  return bf63112_sm3_final (ctx, out);
}

int
bf63112_kdf (const uint8_t *z, size_t zlen, size_t klen, uint8_t *out)
{
  uint8_t block[32];
  uint32_t counter = 1;
  size_t done = 0;
  int rc;

  if (out == NULL || (z == NULL && zlen != 0))
    return -EINVAL;
  if (klen == 0)
    return 0;
  if (klen > 32u * 0xFFFFFFFFu)
    return -EINVAL;

  while (done < klen)
    {
      uint8_t ctr[4] = {
        (uint8_t) (counter >> 24),
        (uint8_t) (counter >> 16),
        (uint8_t) (counter >> 8),
        (uint8_t) counter,
      };
      EVP_MD_CTX *ctx = NULL;
      size_t take;

      rc = bf63112_sm3_ctx_init (&ctx);
      if (rc != 0)
        return rc;
      if ((zlen != 0 && EVP_DigestUpdate (ctx, z, zlen) != 1) ||
          EVP_DigestUpdate (ctx, ctr, sizeof ctr) != 1)
        {
          EVP_MD_CTX_free (ctx);
          return -EIO;
        }
      rc = bf63112_sm3_final (ctx, block);
      if (rc != 0)
        return rc;

      take = klen - done < sizeof block ? klen - done : sizeof block;
      memcpy (out + done, block, take);
      done += take;
      if (counter == 0xFFFFFFFFu)
        break;
      counter++;
    }
  OPENSSL_cleanse (block, sizeof block);
  return (int) 0;
}

int
bf63112_sm4_ecb (const uint8_t key[BF63112_SESSION_KEY_LEN], const uint8_t *in,
                 size_t len, uint8_t *out, bool decrypt)
{
  EVP_CIPHER *cipher = NULL;
  EVP_CIPHER_CTX *ctx = NULL;
  int outl = 0;
  int rc = 0;

  if (key == NULL || in == NULL || out == NULL)
    return -EINVAL;
  if (len == 0 || len % BF63112_SESSION_BLOCK != 0)
    return -EINVAL;
  if (len > (size_t) INT32_MAX)
    return -EINVAL;

  cipher = EVP_CIPHER_fetch (NULL, "SM4-ECB", NULL);
  if (cipher == NULL)
    return -ENOTSUP; /* provider without SM4 */

  ctx = EVP_CIPHER_CTX_new ();
  if (ctx == NULL)
    {
      EVP_CIPHER_free (cipher);
      return -ENOMEM;
    }

  if ((decrypt ? EVP_DecryptInit_ex2 (ctx, cipher, key, NULL, NULL)
               : EVP_EncryptInit_ex2 (ctx, cipher, key, NULL, NULL)) != 1 ||
      EVP_CIPHER_CTX_set_padding (ctx, 0) != 1)
    {
      rc = -EIO;
      goto out;
    }

  if ((decrypt ? EVP_DecryptUpdate (ctx, out, &outl, in, (int) len)
               : EVP_EncryptUpdate (ctx, out, &outl, in, (int) len)) != 1)
    {
      rc = -EIO;
      goto out;
    }
  if ((size_t) outl != len)
    rc = -EIO;

out:
  EVP_CIPHER_CTX_free (ctx);
  EVP_CIPHER_free (cipher);
  return rc;
}

/* H' = SM4_encrypt (key, header) - the per-frame block key. */
static int
bf63112_frame_key (const Bf63112Session *session,
                   const uint8_t header[BF63112_SESSION_BLOCK],
                   uint8_t out[BF63112_SESSION_BLOCK])
{
  if (session == NULL || header == NULL || out == NULL)
    return -EINVAL;
  return bf63112_sm4_ecb (session->key, header, BF63112_SESSION_BLOCK, out,
                          false);
}

int
bf63112_sm4_seal (const Bf63112Session *session,
                  const uint8_t header[BF63112_SESSION_BLOCK], uint8_t *block,
                  size_t len)
{
  uint8_t frame_key[BF63112_SESSION_BLOCK];
  int rc;

  rc = bf63112_frame_key (session, header, frame_key);
  if (rc != 0)
    return rc;
  rc = bf63112_sm4_ecb (frame_key, block, len, block, false);
  OPENSSL_cleanse (frame_key, sizeof frame_key);
  return rc;
}

int
bf63112_sm4_open (const Bf63112Session *session,
                  const uint8_t header[BF63112_SESSION_BLOCK], uint8_t *block,
                  size_t len)
{
  uint8_t frame_key[BF63112_SESSION_BLOCK];
  int rc;

  rc = bf63112_frame_key (session, header, frame_key);
  if (rc != 0)
    return rc;
  rc = bf63112_sm4_ecb (frame_key, block, len, block, true);
  OPENSSL_cleanse (frame_key, sizeof frame_key);
  return rc;
}

/* sm2p256v1 as an EC_GROUP (same curve GmSSL uses in the vendor driver). */
static EC_GROUP *
bf63112_group (void)
{
#if BF63112_HAVE_SM2
  return EC_GROUP_new_by_curve_name (NID_sm2);
#else
  return NULL; /* built without SM2: every SM2 call reports -ENOTSUP */
#endif
}

int
bf63112_keypair_generate (Bf63112Keypair *kp)
{
  EC_GROUP *group = NULL;
  EC_POINT *pub = NULL;
  BIGNUM *d = NULL;
  BN_CTX *ctx = NULL;
  int rc = -EIO;

  if (kp == NULL)
    return -EINVAL;

  group = bf63112_group ();
  if (group == NULL)
    return -ENOTSUP;
  ctx = BN_CTX_new ();
  pub = EC_POINT_new (group);
  d = BN_new ();
  if (ctx == NULL || pub == NULL || d == NULL)
    {
      rc = -ENOMEM;
      goto out;
    }

  /* random scalar in [1, n-1], like FUN_180007C28's retry loop */
  for (int attempt = 0; attempt < 16; attempt++)
    {
      BIGNUM *x = NULL;
      BIGNUM *y = NULL;

      if (BN_priv_rand_range (d, EC_GROUP_get0_order (group)) != 1)
        {
          rc = -EIO;
          goto out;
        }
      if (BN_is_zero (d))
        continue;

      if (EC_POINT_mul (group, pub, d, NULL, NULL, ctx) != 1)
        {
          rc = -EIO;
          goto out;
        }
      x = BN_new ();
      y = BN_new ();
      if (x == NULL || y == NULL)
        {
          BN_free (x);
          BN_free (y);
          rc = -ENOMEM;
          goto out;
        }
      if (EC_POINT_get_affine_coordinates (group, pub, x, y, ctx) != 1 ||
          BN_bn2binpad (x, kp->public_key, 32) != 32 ||
          BN_bn2binpad (y, kp->public_key + 32, 32) != 32 ||
          BN_bn2binpad (d, kp->private_key, 32) != 32)
        {
          BN_free (x);
          BN_free (y);
          rc = -EIO;
          goto out;
        }
      BN_free (x);
      BN_free (y);
      rc = 0;
      goto out;
    }
  rc = -EIO;

out:
  BN_clear_free (d);
  EC_POINT_free (pub);
  BN_CTX_free (ctx);
  EC_GROUP_free (group);
  return rc;
}

int
bf63112_rootkey_request (const Bf63112Keypair *kp,
                         uint8_t out[BF63112_SESSION_REQUEST_LEN])
{
  if (kp == NULL || out == NULL)
    return -EINVAL;
  out[0] = 0x04;
  memcpy (out + 1, kp->public_key, BF63112_SESSION_POINT_LEN);
  return 0;
}

/* KDF over the shared point, then key = mask xor wrapped. */
static int
bf63112_derive_key (const uint8_t shared[64], const uint8_t *wrapped,
                    size_t klen, uint8_t out[32])
{
  uint8_t mask[32];
  size_t i;
  int rc;

  if (klen == 0 || klen > sizeof mask)
    return -EINVAL;

  rc = bf63112_kdf (shared, 64, klen, mask);
  if (rc != 0)
    return rc;
  for (i = 0; i < klen; i++)
    out[i] = (uint8_t) (mask[i] ^ wrapped[i]);
  OPENSSL_cleanse (mask, sizeof mask);
  return 0;
}

int
bf63112_session_complete (Bf63112Session *session, const Bf63112Keypair *kp,
                          const uint8_t *answer, size_t answer_len)
{
  EC_GROUP *group = NULL;
  EC_POINT *peer = NULL;
  EC_POINT *shared_point = NULL;
  BIGNUM *d = NULL;
  BIGNUM *px = NULL;
  BIGNUM *py = NULL;
  BIGNUM *x = NULL;
  BIGNUM *y = NULL;
  BN_CTX *ctx = NULL;
  uint8_t shared[64];
  uint8_t tag[32];
  uint8_t key[32];
  uint32_t klen;
  int rc = -EIO;

  if (session == NULL || kp == NULL || answer == NULL)
    return -EINVAL;
  memset (session, 0, sizeof *session);
  if (answer_len < BF63112_SESSION_ANSWER_MIN)
    return -EPROTO;

  if (answer[ANS_OFF_STATUS] != 0x00 && answer[ANS_OFF_STATUS] != 0x01)
    return -EACCES; /* the module refused the root-key request */

  klen = (uint32_t) answer[ANS_OFF_KLEN] |
         (uint32_t) answer[ANS_OFF_KLEN + 1] << 8 |
         (uint32_t) answer[ANS_OFF_KLEN + 2] << 16 |
         (uint32_t) answer[ANS_OFF_KLEN + 3] << 24;
  if (klen != BF63112_SESSION_KEY_LEN)
    return -EPROTO; /* the vendor value; a different length needs the Python
                     * library's variant search before this can be trusted */
  if (answer_len < ANS_OFF_WRAPPED + klen)
    return -EPROTO;

  group = bf63112_group ();
  if (group == NULL)
    return -ENOTSUP;
  ctx = BN_CTX_new ();
  peer = EC_POINT_new (group);
  shared_point = EC_POINT_new (group);
  px = BN_bin2bn (answer + ANS_OFF_POINT, 32, NULL);
  py = BN_bin2bn (answer + ANS_OFF_POINT + 32, 32, NULL);
  x = BN_new ();
  y = BN_new ();
  d = BN_bin2bn (kp->private_key, 32, NULL);
  if (ctx == NULL || peer == NULL || shared_point == NULL || px == NULL ||
      py == NULL || x == NULL || y == NULL || d == NULL)
    {
      rc = -ENOMEM;
      goto out;
    }

  if (EC_POINT_set_affine_coordinates (group, peer, px, py, ctx) != 1 ||
      EC_POINT_is_on_curve (group, peer, ctx) <= 0)
    {
      rc = -EPROTO; /* the module's point is not on sm2p256v1 */
      goto out;
    }
  if (EC_POINT_mul (group, shared_point, NULL, peer, d, ctx) != 1 ||
      EC_POINT_get_affine_coordinates (group, shared_point, x, y, ctx) != 1 ||
      BN_bn2binpad (x, shared, 32) != 32 ||
      BN_bn2binpad (y, shared + 32, 32) != 32)
    {
      rc = -EIO;
      goto out;
    }

  rc = bf63112_derive_key (shared, answer + ANS_OFF_WRAPPED, klen, key);
  if (rc != 0)
    goto out;

  /* tag = SM3 (X || key || Y) - the driver hashes X, the key, then Y */
  {
    EVP_MD_CTX *md = NULL;

    rc = bf63112_sm3_ctx_init (&md);
    if (rc != 0)
      goto out;
    if (EVP_DigestUpdate (md, shared, 32) != 1 ||
        EVP_DigestUpdate (md, key, klen) != 1 ||
        EVP_DigestUpdate (md, shared + 32, 32) != 1)
      {
        EVP_MD_CTX_free (md);
        rc = -EIO;
        goto out;
      }
    rc = bf63112_sm3_final (md, tag);
    if (rc != 0)
      goto out;
  }

  if (CRYPTO_memcmp (tag, answer + ANS_OFF_TAG, sizeof tag) != 0)
    {
      rc = -EKEYREJECTED; /* SM3 tag mismatch: wrong module or corrupted read */
      goto out;
    }

  memcpy (session->key, key, BF63112_SESSION_KEY_LEN);
  session->ready = true;
  rc = 0;

out:
  OPENSSL_cleanse (key, sizeof key);
  OPENSSL_cleanse (shared, sizeof shared);
  OPENSSL_cleanse (tag, sizeof tag);
  BN_free (px);
  BN_free (py);
  BN_free (x);
  BN_free (y);
  BN_clear_free (d);
  EC_POINT_free (peer);
  EC_POINT_free (shared_point);
  BN_CTX_free (ctx);
  EC_GROUP_free (group);
  return rc;
}

void
bf63112_session_wipe (void *buf, size_t len)
{
  if (buf != NULL && len != 0)
    OPENSSL_cleanse (buf, len);
}

const char *
bf63112_session_strerror (int rc)
{
  switch (rc)
    {
    case 0:
      return "ok";
    case -ENOTSUP:
      return "the OpenSSL provider has no SM2/SM3/SM4 support";
    case -ENOMEM:
      return "out of memory";
    case -EINVAL:
      return "invalid argument";
    case -EPROTO:
      return "malformed 0xA2 answer";
    case -EACCES:
      return "the module refused the root-key request";
    case -EKEYREJECTED:
      return "SM3 tag mismatch: the session key was not accepted";
    default:
      return "SM2/SM3/SM4 operation failed";
    }
}
