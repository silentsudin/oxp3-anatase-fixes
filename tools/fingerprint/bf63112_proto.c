/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_proto.c - dependency-free BF63112 frame protocol (see the header).
 *
 * Mirrors tools/fingerprint/fpbf63112/protocol.py; tests/test_proto.c checks
 * both against the same generated vectors (tests/vectors.h).
 */

#include "bf63112_proto.h"

#include <string.h>

/* ---- arithmetic --------------------------------------------------------- */

size_t
bf63112_align16 (size_t n)
{
  return (n + (BF63112_ALIGN - 1)) & ~(size_t) (BF63112_ALIGN - 1);
}

size_t
bf63112_command_block_len (size_t data_len)
{
  return bf63112_align16 (BF63112_CMD_BLOCK_FIXED + data_len);
}

size_t
bf63112_length_field (size_t data_len)
{
  return 4 + BF63112_HDR_RESERVED + bf63112_command_block_len (data_len);
}

size_t
bf63112_frame_size (size_t data_len)
{
  return BF63112_FRAME_OVERHEAD + bf63112_length_field (data_len);
}

/* ---- checksums ---------------------------------------------------------- */

uint32_t
bf63112_crc32 (const uint8_t *data, size_t len)
{
  uint32_t crc = 0xFFFFFFFFu;
  size_t i;
  int bit;

  for (i = 0; i < len; i++)
    {
      crc ^= data[i];
      for (bit = 0; bit < 8; bit++)
        crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
  return ~crc;
}

uint8_t
bf63112_sum8 (const uint8_t *data, size_t len)
{
  uint32_t sum = 0;
  size_t i;

  for (i = 0; i < len; i++)
    sum += data[i];
  return (uint8_t) sum;
}

static uint32_t
get_le32 (const uint8_t *p)
{
  return (uint32_t) p[0] | ((uint32_t) p[1] << 8) |
         ((uint32_t) p[2] << 16) | ((uint32_t) p[3] << 24);
}

static uint16_t
get_le16 (const uint8_t *p)
{
  return (uint16_t) (p[0] | (p[1] << 8));
}

static void
put_le32 (uint8_t *p, uint32_t v)
{
  p[0] = (uint8_t) (v & 0xFF);
  p[1] = (uint8_t) ((v >> 8) & 0xFF);
  p[2] = (uint8_t) ((v >> 16) & 0xFF);
  p[3] = (uint8_t) ((v >> 24) & 0xFF);
}

static void
put_le16 (uint8_t *p, uint16_t v)
{
  p[0] = (uint8_t) (v & 0xFF);
  p[1] = (uint8_t) ((v >> 8) & 0xFF);
}

/* ---- build -------------------------------------------------------------- */

size_t
bf63112_build_frame (uint8_t        opcode,
                     uint8_t        phase,
                     uint8_t        status,
                     uint16_t       arg,
                     const uint8_t *data,
                     size_t         data_len,
                     uint8_t       *out,
                     size_t         out_size)
{
  size_t block, total, off;
  uint32_t crc;

  if (out == NULL || data_len > BF63112_MAX_DATA_LEN)
    return 0;
  if (data_len > 0 && data == NULL)
    return 0;

  block = bf63112_command_block_len (data_len);
  total = bf63112_frame_size (data_len);
  if (out_size < total)
    return 0;

  memset (out, 0, total);
  off = 0;
  out[off++] = BF63112_SOF;
  put_le32 (out + off, (uint32_t) bf63112_length_field (data_len));
  off += 4;
  off += BF63112_HDR_RESERVED;          /* zeros: request header area */
  out[off++] = BF63112_CMD_MAGIC;
  out[off++] = opcode;
  out[off++] = phase;
  out[off++] = status;
  put_le16 (out + off, arg);
  off += 2;
  put_le32 (out + off, (uint32_t) data_len);
  off += 4;
  if (data_len > 0)
    memcpy (out + off, data, data_len);
  /* the remainder of the aligned block stays zero */
  off += data_len;
  (void) off;
  (void) block;

  crc = bf63112_crc32 (out, total - 4);
  put_le32 (out + total - 4, crc);
  return total;
}

/* ---- parse -------------------------------------------------------------- */

const char *
bf63112_parse_result_str (enum bf63112_parse_result result)
{
  switch (result)
    {
    case BF63112_PARSE_OK:            return "ok";
    case BF63112_PARSE_TRUNCATED:     return "truncated frame";
    case BF63112_PARSE_BAD_SOF:       return "bad start byte";
    case BF63112_PARSE_BAD_LENGTH:    return "bad length field";
    case BF63112_PARSE_BAD_MAGIC:     return "bad command magic";
    case BF63112_PARSE_BAD_DATA_LEN:  return "data_len exceeds block";
    case BF63112_PARSE_BAD_CRC:       return "CRC mismatch";
    }
  return "unknown";
}

enum bf63112_parse_result
bf63112_parse_frame (const uint8_t *buf, size_t len,
                     struct bf63112_frame *out)
{
  uint32_t length, block, data_len, want, got;
  size_t min_size = BF63112_FRAME_MIN_SIZE;

  if (buf == NULL || out == NULL)
    return BF63112_PARSE_TRUNCATED;
  if (len < min_size)
    return BF63112_PARSE_TRUNCATED;
  if (buf[0] != BF63112_SOF)
    return BF63112_PARSE_BAD_SOF;

  length = get_le32 (buf + BF63112_OFF_LEN);
  if (length != len - BF63112_FRAME_OVERHEAD)
    return BF63112_PARSE_BAD_LENGTH;

  block = length - 4 - BF63112_HDR_RESERVED;
  if (block < BF63112_ALIGN || (block % BF63112_ALIGN) != 0)
    return BF63112_PARSE_BAD_LENGTH;

  if (buf[BF63112_OFF_MAGIC] != BF63112_CMD_MAGIC)
    return BF63112_PARSE_BAD_MAGIC;

  data_len = get_le32 (buf + BF63112_OFF_DATA_LEN);
  if (data_len > block - BF63112_CMD_BLOCK_FIXED)
    return BF63112_PARSE_BAD_DATA_LEN;

  want = bf63112_crc32 (buf, len - 4);
  got = get_le32 (buf + len - 4);
  if (want != got)
    return BF63112_PARSE_BAD_CRC;

  out->opcode = buf[BF63112_OFF_OPCODE];
  out->phase = buf[BF63112_OFF_PHASE];
  out->status = buf[BF63112_OFF_STATUS];
  out->arg = get_le16 (buf + BF63112_OFF_ARG);
  out->data_len = data_len;
  out->length_field = length;
  out->crc = got;
  out->size = len;
  out->data = buf + BF63112_OFF_DATA;
  return BF63112_PARSE_OK;
}

enum bf63112_scan_result
bf63112_scan_frame (const uint8_t *buf, size_t len,
                    size_t *offset, struct bf63112_frame *out)
{
  size_t pos = 0;
  size_t first_candidate = (size_t) -1;
  int saw_truncated = 0;
  int saw_corrupt = 0;

  if (offset != NULL)
    *offset = 0;
  if (buf == NULL || out == NULL)
    return BF63112_SCAN_NO_FRAME;

  while (pos < len)
    {
      uint32_t total;
      enum bf63112_parse_result result;

      if (buf[pos] != BF63112_SOF)
        {
          pos++;
          continue;
        }
      if (first_candidate == (size_t) -1)
        first_candidate = pos;
      if (pos + BF63112_FRAME_OVERHEAD > len)
        {
          saw_truncated = 1;
          break;
        }
      total = get_le32 (buf + pos + BF63112_OFF_LEN) + BF63112_FRAME_OVERHEAD;
      if (total < BF63112_FRAME_MIN_SIZE)
        {
          saw_corrupt = 1;
          pos++;
          continue;
        }
      if (pos + total > len)
        {
          saw_truncated = 1;
          break;
        }
      result = bf63112_parse_frame (buf + pos, total, out);
      if (result == BF63112_PARSE_OK)
        {
          if (offset != NULL)
            *offset = pos;
          return BF63112_SCAN_OK;
        }
      saw_corrupt = 1;
      pos++;                            /* resynchronise one byte later */
    }

  if (offset != NULL)
    *offset = (first_candidate == (size_t) -1) ? 0 : first_candidate;
  if (saw_truncated)
    return BF63112_SCAN_TRUNCATED;
  if (saw_corrupt)
    return BF63112_SCAN_CORRUPT;
  return BF63112_SCAN_NO_FRAME;
}

/* ---- GET_IMG_DATA ------------------------------------------------------- */

enum bf63112_image_result
bf63112_image_header_parse (const uint8_t *buf, size_t len,
                            struct bf63112_image_header *out)
{
  if (buf == NULL || out == NULL || len < 9)
    return BF63112_IMAGE_ERR_HEADER;
  out->status = buf[0];
  out->total_bytes = get_le32 (buf + 1);
  out->byte_sum = get_le32 (buf + 5);
  return BF63112_IMAGE_OK;
}

size_t
bf63112_image_header_build (const struct bf63112_image_header *header,
                            uint8_t *out, size_t out_size)
{
  if (header == NULL || out == NULL || out_size < 9)
    return 0;
  out[0] = header->status;
  put_le32 (out + 1, header->total_bytes);
  put_le32 (out + 5, header->byte_sum);
  return 9;
}

void
bf63112_image_init (struct bf63112_image_assembler *asm_,
                    uint8_t *buf, size_t capacity,
                    const struct bf63112_image_header *header,
                    size_t max_chunk)
{
  memset (asm_, 0, sizeof (*asm_));
  asm_->buf = buf;
  asm_->capacity = capacity;
  asm_->total_bytes = header->total_bytes;
  asm_->expected_sum8 = (uint8_t) (header->byte_sum & 0xFF);
  asm_->max_chunk = max_chunk;
}

enum bf63112_image_result
bf63112_image_add (struct bf63112_image_assembler *asm_,
                   const uint8_t *chunk, size_t len,
                   const uint8_t *chunk_sum)
{
  if (asm_ == NULL || chunk == NULL || len == 0)
    return BF63112_IMAGE_ERR_STATE;
  if (len > asm_->max_chunk)
    return BF63112_IMAGE_ERR_OVERSIZE;
  if (chunk_sum != NULL && bf63112_sum8 (chunk, len) != *chunk_sum)
    return BF63112_IMAGE_ERR_CHUNK_SUM;
  if (asm_->received + len > asm_->total_bytes)
    return BF63112_IMAGE_ERR_OVERRUN;
  if (asm_->buf == NULL || asm_->received + len > asm_->capacity)
    return BF63112_IMAGE_ERR_STATE;
  memcpy (asm_->buf + asm_->received, chunk, len);
  asm_->received += len;
  asm_->sum = (uint8_t) (asm_->sum + bf63112_sum8 (chunk, len));
  return BF63112_IMAGE_OK;
}

enum bf63112_image_result
bf63112_image_finish (const struct bf63112_image_assembler *asm_)
{
  if (asm_ == NULL || asm_->buf == NULL)
    return BF63112_IMAGE_ERR_STATE;
  if (asm_->received != asm_->total_bytes)
    return BF63112_IMAGE_ERR_SHORT;
  if (asm_->sum != asm_->expected_sum8)
    return BF63112_IMAGE_ERR_SUM;
  return BF63112_IMAGE_OK;
}

/* ---- safety ------------------------------------------------------------- */

int
bf63112_opcode_is_destructive (uint8_t opcode)
{
  switch (opcode)
    {
    case BF63112_OP_ENROLL_FINGERPRINT:
    case BF63112_OP_REMOVE_FINGERPRINT:
    case BF63112_OP_JTAG_CONTROL:
    case BF63112_OP_RESTORE_MCU:
    case BF63112_OP_UPDATE_FIRMWARE:
    case BF63112_OP_FW_WORK_MODE_SWITCH:
      return 1;
    default:
      return 0;
    }
}

const char *
bf63112_opcode_name (uint8_t opcode)
{
  switch (opcode)
    {
    case BF63112_OP_QUERY_FIRMWARE_VER:
      return "CMD_QUERY_FIRMWARE_VER";
    case BF63112_OP_QUERY_MODULE_SCHEME_INFORMATION:
      return "CMD_QUERY_MODULE_SCHEME_INFORMATION";
    case BF63112_OP_QUERY_ROOT_KEY:
      return "CMD_QUERY_ROOT_KEY";
    case BF63112_OP_ENROLL_FINGERPRINT:
      return "CMD_ENROLL_FINGERPRINT";
    case BF63112_OP_IDENTIFY_FINGERPRINT:
      return "CMD_IDENTIFY_FINGERPRINT";
    case BF63112_OP_REMOVE_FINGERPRINT:
      return "CMD_REMOVE_FINGERPRINT";
    case BF63112_OP_QUERY_FINGERPRINT_COUNT:
      return "CMD_QUERY_FINGERPRINT_COUNT";
    case BF63112_OP_GET_IMG_DATA:
      return "CMD_GET_IMG_DATA";
    case BF63112_OP_DETECT_FINGERPRINT_PRESS:
      return "CMD_DETECT_FINGERPRINT_PRESS";
    case BF63112_OP_SLEEP:
      return "CMD_SLEEP";
    case BF63112_OP_QUERY_MODULE_UID:
      return "CMD_QUERY_MODULE_UID";
    case BF63112_OP_QUERY_SENSOR_INFO:
      return "CMD_QUERY_SENSOR_INFO";
    case BF63112_OP_JTAG_CONTROL:
      return "CMD_JTAG_CONTROL";
    case BF63112_OP_RESTORE_MCU:
      return "CMD_RESTORE_MCU";
    case BF63112_OP_UPDATE_FIRMWARE:
      return "CMD_UPDATE_FIRMWARE";
    case BF63112_OP_FW_WORK_MODE_SWITCH:
      return "CMD_FW_WORK_MODE_SWITCH";
    default:
      return NULL;
    }
}
