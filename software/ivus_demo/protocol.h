#ifndef DD_PROTOCOL_H
#define DD_PROTOCOL_H

#include <stdint.h>

#define DD_PACKET_MAGIC 0x44445350u /* ASCII "DDSP" */
#define DD_PROTOCOL_VERSION 1u
#define DD_STREAM_IVUS_BMODE8 1u
#define DD_UDP_PORT 8080u
#define DD_MAX_PAYLOAD 1400u

#define DD_FLAG_FRAME_START 0x0001u
#define DD_FLAG_FRAME_END   0x0002u
#define DD_FLAG_LOOP_START  0x0004u

#if defined(__GNUC__)
#define DD_PACKED __attribute__((packed))
#else
#define DD_PACKED
#endif

/* All multi-byte fields in a UDP header are transmitted in network byte order. */
struct DD_PACKED dd_packet_header {
    uint32_t magic;
    uint8_t version;
    uint8_t stream_type;
    uint16_t flags;
    uint32_t frame_id;
    uint64_t timestamp_us;
    uint16_t packet_index;
    uint16_t packet_count;
    uint16_t payload_bytes;
    uint16_t width;
    uint16_t height;
    uint16_t reserved;
    uint32_t area_mm2_milli;
    uint32_t frame_crc32;
};

typedef char dd_packet_header_must_be_40_bytes[
    sizeof(struct dd_packet_header) == 40 ? 1 : -1
];

#endif
