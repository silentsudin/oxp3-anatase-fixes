/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_proto.h - dependency-free BF63112 frame protocol.
 *
 * A small C mirror of tools/fingerprint/fpbf63112/protocol.py for the
 * Betterlife BF63112 fingerprint module on Intel SPI (ONEXPLAYER 3,
 * ACPI\BF63112).  Only <stdint.h>/<stddef.h> are needed, so this file is shared
 * by the libfprint driver and by the offline tests (tests/test_proto.c).
 *
 * Frame layout, recovered byte for byte from the Windows UMDF driver
 * (see docs/hw/fingerprint.md):
 *
 *   AA | L:u32 LE | 16 x 0 | 4C op phase 00 arg:u16 len:u32 data | pad16 | CRC-32
 *   L = 20 + align16 (10 + data_len)          (bytes after the SOF, no CRC)
 *   CRC-32/ISO-HDLC over everything before it, including the 0xAA.
 *
 * This is our own code.  It does not touch hardware.
 */

#ifndef BF63112_PROTO_H
#define BF63112_PROTO_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- recovered constants ------------------------------------------------ */

#define BF63112_SOF             0xAAu
#define BF63112_CMD_MAGIC       0x4Cu
#define BF63112_HDR_RESERVED    16u
#define BF63112_CMD_BLOCK_FIXED 10u
#define BF63112_ALIGN           16u

#define BF63112_OFF_LEN       1u
#define BF63112_OFF_RESERVED  5u
#define BF63112_OFF_MAGIC     21u
#define BF63112_OFF_OPCODE    22u
#define BF63112_OFF_PHASE     23u
#define BF63112_OFF_STATUS    24u
#define BF63112_OFF_ARG       25u
#define BF63112_OFF_DATA_LEN  27u
#define BF63112_OFF_DATA      31u

/*: 41 bytes for data_len == 0: 25 + align16 (10). */
#define BF63112_FRAME_MIN_SIZE 41u
/*: SOF + L field + CRC-32. */
#define BF63112_FRAME_OVERHEAD 5u

/*: Largest payload this implementation accepts (the wire field is u32). */
#define BF63112_MAX_DATA_LEN (64u * 1024u)

/*: Chunk ceiling the Windows driver uses for GET_IMG_DATA. */
#define BF63112_MAX_IMAGE_CHUNK 0x1FCCu

/*: Phases of a multi-step command. */
enum bf63112_phase
{
  BF63112_PHASE_START = 0,
  BF63112_PHASE_DATA = 1,
  BF63112_PHASE_FINISH = 2,
  BF63112_PHASE_CANCEL = 3,
};

/*: The recovered opcode table (descriptor+0x10 in the Windows driver). */
enum bf63112_opcode
{
  BF63112_OP_QUERY_FIRMWARE_VER = 0xA1,
  BF63112_OP_QUERY_MODULE_SCHEME_INFORMATION = 0xA2,
  BF63112_OP_QUERY_ROOT_KEY = 0xB0,
  BF63112_OP_ENROLL_FINGERPRINT = 0xB1,
  BF63112_OP_IDENTIFY_FINGERPRINT = 0xB2,
  BF63112_OP_REMOVE_FINGERPRINT = 0xB3,
  BF63112_OP_QUERY_FINGERPRINT_COUNT = 0xB4,
  BF63112_OP_GET_IMG_DATA = 0xB5,
  BF63112_OP_DETECT_FINGERPRINT_PRESS = 0xB6,
  BF63112_OP_SLEEP = 0xC0,
  BF63112_OP_QUERY_MODULE_UID = 0xC1,
  BF63112_OP_QUERY_SENSOR_INFO = 0xC2,
  BF63112_OP_JTAG_CONTROL = 0xC3,
  BF63112_OP_RESTORE_MCU = 0xD0,
  BF63112_OP_UPDATE_FIRMWARE = 0xD1,
  BF63112_OP_FW_WORK_MODE_SWITCH = 0xFF,
};

/* ---- frame arithmetic and checksums ------------------------------------ */

size_t   bf63112_align16 (size_t n);
size_t   bf63112_command_block_len (size_t data_len);
size_t   bf63112_length_field (size_t data_len);
size_t   bf63112_frame_size (size_t data_len);

uint32_t bf63112_crc32 (const uint8_t *data, size_t len);
uint8_t  bf63112_sum8 (const uint8_t *data, size_t len);

/* ---- build -------------------------------------------------------------- */

/*!
 * Build a complete frame into @out.
 *
 * @status is the byte at offset 24 (0x00 in every request; answers carry a
 * status/parameter there - the answer layout is inferred, see the docs).
 *
 * Returns the frame size written, or 0 if the arguments are invalid or @out is
 * too small.  @data may be NULL when @data_len is 0.
 */
size_t bf63112_build_frame (uint8_t        opcode,
                            uint8_t        phase,
                            uint8_t        status,
                            uint16_t       arg,
                            const uint8_t *data,
                            size_t         data_len,
                            uint8_t       *out,
                            size_t         out_size);

/* ---- parse -------------------------------------------------------------- */

enum bf63112_parse_result
{
  BF63112_PARSE_OK = 0,
  BF63112_PARSE_TRUNCATED,
  BF63112_PARSE_BAD_SOF,
  BF63112_PARSE_BAD_LENGTH,
  BF63112_PARSE_BAD_MAGIC,
  BF63112_PARSE_BAD_DATA_LEN,
  BF63112_PARSE_BAD_CRC,
};

struct bf63112_frame
{
  uint8_t        opcode;
  uint8_t        phase;
  uint8_t        status;
  uint16_t       arg;
  uint32_t       data_len;
  uint32_t       length_field;
  uint32_t       crc;
  size_t         size;              /*: complete frame size in bytes */
  const uint8_t *data;              /*: points into the caller's buffer */
};

const char *bf63112_parse_result_str (enum bf63112_parse_result result);

/*! Validate and decode exactly one frame occupying @len bytes. */
enum bf63112_parse_result
bf63112_parse_frame (const uint8_t *buf, size_t len,
                     struct bf63112_frame *out);

enum bf63112_scan_result
{
  BF63112_SCAN_OK = 0,
  BF63112_SCAN_NO_FRAME,      /*: nothing in the buffer starts with 0xAA */
  BF63112_SCAN_TRUNCATED,     /*: a candidate frame is cut short */
  BF63112_SCAN_CORRUPT,       /*: candidate(s) failed validation */
};

/*!
 * Find the first valid frame in @buf, skipping leading padding/junk.
 *
 * This is the userspace view of a spidev readback: the answer may be preceded
 * by 0xFF/0x00 bytes and followed by irrelevant data.  On success @offset
 * receives the frame start and @out the decoded frame.
 */
enum bf63112_scan_result
bf63112_scan_frame (const uint8_t *buf, size_t len,
                    size_t *offset, struct bf63112_frame *out);

/* ---- GET_IMG_DATA ------------------------------------------------------- */

struct bf63112_image_header
{
  uint8_t  status;
  uint32_t total_bytes;
  uint32_t byte_sum;            /*: only the low 8 bits are checked */
};

enum bf63112_image_result
{
  BF63112_IMAGE_OK = 0,
  BF63112_IMAGE_ERR_HEADER,     /*: payload shorter than 9 bytes */
  BF63112_IMAGE_ERR_STATE,      /*: assembler misused */
  BF63112_IMAGE_ERR_OVERSIZE,   /*: chunk above max_chunk */
  BF63112_IMAGE_ERR_OVERRUN,    /*: chunk would pass total_bytes */
  BF63112_IMAGE_ERR_SHORT,      /*: finish() with too few bytes */
  BF63112_IMAGE_ERR_SUM,        /*: global 8-bit sum mismatch */
  BF63112_IMAGE_ERR_CHUNK_SUM,  /*: optional per-chunk sum mismatch */
};

/*! Parse the 9-byte phase-0 payload: status, total:u32, byte_sum:u32. */
enum bf63112_image_result
bf63112_image_header_parse (const uint8_t *buf, size_t len,
                            struct bf63112_image_header *out);

/*! Serialise a phase-0 payload into @out (at least 9 bytes). */
size_t bf63112_image_header_build (const struct bf63112_image_header *header,
                                   uint8_t *out, size_t out_size);

/*!
 * Reassemble a GET_IMG image into a caller-provided buffer.
 *
 * No allocation happens here: the driver sizes its buffer from the header.
 * @capacity must be at least header->total_bytes.
 */
struct bf63112_image_assembler
{
  uint8_t *buf;
  size_t   capacity;
  size_t   received;
  uint8_t  sum;
  uint32_t total_bytes;
  uint8_t  expected_sum8;
  size_t   max_chunk;
};

void bf63112_image_init (struct bf63112_image_assembler *asm_,
                         uint8_t *buf, size_t capacity,
                         const struct bf63112_image_header *header,
                         size_t max_chunk);

/*! Append one chunk; @chunk_sum is checked only when it is non-NULL. */
enum bf63112_image_result
bf63112_image_add (struct bf63112_image_assembler *asm_,
                   const uint8_t *chunk, size_t len,
                   const uint8_t *chunk_sum);

/*! Validate size and global sum; returns OK only for a complete image. */
enum bf63112_image_result
bf63112_image_finish (const struct bf63112_image_assembler *asm_);

/* ---- safety ------------------------------------------------------------- */

/*! TRUE for opcodes that can erase templates, JTAG or reflash the MCU. */
int bf63112_opcode_is_destructive (uint8_t opcode);

/*! Human-readable command name ("CMD_..."), or NULL for unknown bytes. */
const char *bf63112_opcode_name (uint8_t opcode);

#ifdef __cplusplus
}
#endif

#endif /* BF63112_PROTO_H */
