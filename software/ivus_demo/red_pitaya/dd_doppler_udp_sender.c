#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "../protocol.h"

#define DDP_MAGIC "DDP1"
#define DDP_VERSION 1u
#define DDP_FLAG_BEAT_UPDATE 0x00000001u

#if defined(__GNUC__)
#define DDP_PACKED __attribute__((packed))
#else
#define DDP_PACKED
#endif

struct DDP_PACKED ddp_header {
    char magic[4];
    uint32_t version;
    uint32_t sample_count;
    uint32_t spectral_bins;
    uint32_t rate_num;
    uint32_t rate_den;
    uint32_t heart_rate_milli_bpm;
    uint32_t lvot_diameter_um;
    uint32_t lvot_area_mm2_milli;
    uint32_t mean_vti_milli_cm;
    uint32_t mean_stroke_volume_microliters;
    uint32_t mean_cardiac_output_milli_lpm;
};

struct DDP_PACKED ddp_sample_header {
    uint32_t sample_index;
    uint32_t time_offset_us;
    uint32_t vti_milli_cm;
    uint32_t stroke_volume_microliters;
    uint32_t cardiac_output_milli_lpm;
    uint32_t spectral_crc32;
    uint32_t flags;
};

typedef char ddp_header_must_be_48_bytes[
    sizeof(struct ddp_header) == 48 ? 1 : -1
];
typedef char ddp_sample_header_must_be_28_bytes[
    sizeof(struct ddp_sample_header) == 28 ? 1 : -1
];

static uint32_t crc32_bytes(const uint8_t *data, size_t length) {
    uint32_t crc = 0xFFFFFFFFu;
    size_t i;
    for (i = 0; i < length; ++i) {
        uint32_t value = (crc ^ data[i]) & 0xFFu;
        int bit;
        for (bit = 0; bit < 8; ++bit)
            value = (value >> 1) ^
                    (0xEDB88320u & (uint32_t)-(int32_t)(value & 1u));
        crc = (crc >> 8) ^ value;
    }
    return crc ^ 0xFFFFFFFFu;
}

static uint64_t host_to_network_u64(uint64_t value) {
    const uint16_t one = 1;
    if (*(const uint8_t *)&one == 0)
        return value;
    return ((uint64_t)htonl((uint32_t)(value & 0xFFFFFFFFu)) << 32) |
           htonl((uint32_t)(value >> 32));
}

static int host_is_little_endian(void) {
    const uint16_t value = 1;
    return *(const uint8_t *)&value == 1;
}

static uint64_t realtime_microseconds(void) {
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    return (uint64_t)now.tv_sec * 1000000u + (uint64_t)now.tv_nsec / 1000u;
}

static int read_exact(FILE *stream, void *destination, size_t bytes) {
    return fread(destination, 1, bytes, stream) == bytes;
}

static void sleep_until_offset(const struct timespec *origin,
                               uint32_t time_offset_us) {
    struct timespec deadline = *origin;
    uint64_t nanoseconds = (uint64_t)time_offset_us * 1000u;
    deadline.tv_sec += (time_t)(nanoseconds / 1000000000u);
    deadline.tv_nsec += (long)(nanoseconds % 1000000000u);
    if (deadline.tv_nsec >= 1000000000L) {
        deadline.tv_sec += 1;
        deadline.tv_nsec -= 1000000000L;
    }
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, NULL) == EINTR) {}
}

static int send_sample(int socket_fd, const struct sockaddr_in *destination,
                       const struct ddp_header *file_header,
                       const struct ddp_sample_header *sample,
                       const uint8_t *spectrum, uint32_t frame_id,
                       int loop_start) {
    uint8_t payload[DD_MAX_PAYLOAD];
    uint8_t datagram[sizeof(struct dd_packet_header) + DD_MAX_PAYLOAD];
    struct dd_doppler_payload_header metadata;
    struct dd_packet_header header;
    uint16_t payload_bytes;
    uint16_t flags = DD_FLAG_FRAME_START | DD_FLAG_FRAME_END;
    uint32_t network_crc;

    if (sizeof(metadata) + file_header->spectral_bins > sizeof(payload)) {
        fprintf(stderr, "Doppler payload exceeds DD_MAX_PAYLOAD\n");
        return 0;
    }
    if (loop_start)
        flags |= DD_FLAG_LOOP_START;
    if (sample->flags & DDP_FLAG_BEAT_UPDATE)
        flags |= DD_FLAG_BEAT_UPDATE;

    metadata.sample_index = htonl(sample->sample_index);
    metadata.time_offset_us = htonl(sample->time_offset_us);
    metadata.heart_rate_milli_bpm = htonl(file_header->heart_rate_milli_bpm);
    metadata.lvot_diameter_um = htonl(file_header->lvot_diameter_um);
    metadata.vti_milli_cm = htonl(sample->vti_milli_cm);
    metadata.stroke_volume_microliters =
        htonl(sample->stroke_volume_microliters);
    metadata.cardiac_output_milli_lpm =
        htonl(sample->cardiac_output_milli_lpm);

    memcpy(payload, &metadata, sizeof(metadata));
    memcpy(payload + sizeof(metadata), spectrum, file_header->spectral_bins);
    payload_bytes = (uint16_t)(sizeof(metadata) + file_header->spectral_bins);
    network_crc = crc32_bytes(payload, payload_bytes);

    memset(&header, 0, sizeof(header));
    header.magic = htonl(DD_PACKET_MAGIC);
    header.version = DD_PROTOCOL_VERSION;
    header.stream_type = DD_STREAM_DOPPLER_PW8;
    header.flags = htons(flags);
    header.frame_id = htonl(frame_id);
    header.timestamp_us = host_to_network_u64(realtime_microseconds());
    header.packet_index = htons(0);
    header.packet_count = htons(1);
    header.payload_bytes = htons(payload_bytes);
    header.width = htons((uint16_t)file_header->spectral_bins);
    header.height = htons(1);
    header.area_mm2_milli = htonl(file_header->lvot_area_mm2_milli);
    header.frame_crc32 = htonl(network_crc);

    memcpy(datagram, &header, sizeof(header));
    memcpy(datagram + sizeof(header), payload, payload_bytes);
    if (sendto(socket_fd, datagram, sizeof(header) + payload_bytes, 0,
               (const struct sockaddr *)destination, sizeof(*destination)) < 0) {
        perror("sendto");
        return 0;
    }
    return 1;
}

int main(int argc, char **argv) {
    const char *path;
    const char *destination_ip;
    unsigned long port = DD_DOPPLER_UDP_PORT;
    unsigned long loops = 0;
    FILE *stream;
    struct ddp_header file_header;
    uint8_t *spectrum = NULL;
    int socket_fd = -1;
    struct sockaddr_in destination;
    uint32_t transmitted_id = 0;
    unsigned long loop = 0;
    int result = 0;

    if (argc < 3 || argc > 5) {
        fprintf(stderr, "Usage: %s FILE.ddp PI_IP [PORT] [LOOPS]\n", argv[0]);
        fprintf(stderr, "LOOPS=0 repeats until interrupted.\n");
        return 2;
    }
    if (!host_is_little_endian()) {
        fprintf(stderr, "DDP1 files currently require a little-endian host\n");
        return 1;
    }

    path = argv[1];
    destination_ip = argv[2];
    if (argc >= 4)
        port = strtoul(argv[3], NULL, 10);
    if (argc >= 5)
        loops = strtoul(argv[4], NULL, 10);
    if (port == 0 || port > 65535) {
        fprintf(stderr, "Invalid UDP port\n");
        return 2;
    }

    stream = fopen(path, "rb");
    if (!stream) {
        perror(path);
        return 1;
    }
    if (!read_exact(stream, &file_header, sizeof(file_header)) ||
        memcmp(file_header.magic, DDP_MAGIC, 4) != 0 ||
        file_header.version != DDP_VERSION || !file_header.sample_count ||
        !file_header.spectral_bins ||
        file_header.spectral_bins + sizeof(struct dd_doppler_payload_header) >
            DD_MAX_PAYLOAD ||
        !file_header.rate_num || !file_header.rate_den) {
        fprintf(stderr, "Invalid or unsupported DDP header\n");
        fclose(stream);
        return 1;
    }

    spectrum = malloc(file_header.spectral_bins);
    if (!spectrum) {
        fprintf(stderr, "Could not allocate Doppler spectral buffer\n");
        fclose(stream);
        return 1;
    }
    socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (socket_fd < 0) {
        perror("socket");
        free(spectrum);
        fclose(stream);
        return 1;
    }
    memset(&destination, 0, sizeof(destination));
    destination.sin_family = AF_INET;
    destination.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, destination_ip, &destination.sin_addr) != 1) {
        fprintf(stderr, "Invalid destination IP address\n");
        result = 1;
        goto done;
    }

    printf("Streaming %u Doppler samples (%u bins) at %.3f samples/s to %s:%lu\n",
           file_header.sample_count, file_header.spectral_bins,
           (double)file_header.rate_num / file_header.rate_den,
           destination_ip, port);
    printf("LVOT diameter %.3f cm; mean CO %.3f L/min\n",
           file_header.lvot_diameter_um / 10000.0,
           file_header.mean_cardiac_output_milli_lpm / 1000.0);

    while (loops == 0 || loop < loops) {
        struct timespec origin;
        uint32_t local;
        if (fseek(stream, (long)sizeof(file_header), SEEK_SET) != 0) {
            perror("fseek");
            result = 1;
            break;
        }
        clock_gettime(CLOCK_MONOTONIC, &origin);

        for (local = 0; local < file_header.sample_count; ++local) {
            struct ddp_sample_header sample;
            uint32_t calculated_crc;
            if (!read_exact(stream, &sample, sizeof(sample)) ||
                !read_exact(stream, spectrum, file_header.spectral_bins)) {
                fprintf(stderr, "Truncated DDP sample %u\n", local);
                result = 1;
                goto done;
            }
            if (sample.sample_index != local) {
                fprintf(stderr, "Unexpected DDP sample index %u\n", sample.sample_index);
                result = 1;
                goto done;
            }
            calculated_crc = crc32_bytes(spectrum, file_header.spectral_bins);
            if (calculated_crc != sample.spectral_crc32) {
                fprintf(stderr, "Stored CRC mismatch at Doppler sample %u\n", local);
                result = 1;
                goto done;
            }

            sleep_until_offset(&origin, sample.time_offset_us);
            if (!send_sample(socket_fd, &destination, &file_header, &sample,
                             spectrum, transmitted_id++, local == 0)) {
                result = 1;
                goto done;
            }
            if (sample.flags & DDP_FLAG_BEAT_UPDATE) {
                printf("Loop %lu beat update: VTI %.3f cm, SV %.3f mL, CO %.3f L/min\n",
                       loop + 1, sample.vti_milli_cm / 1000.0,
                       sample.stroke_volume_microliters / 1000.0,
                       sample.cardiac_output_milli_lpm / 1000.0);
            }
        }
        ++loop;
        printf("Loop %lu complete\n", loop);
    }

done:
    if (socket_fd >= 0)
        close(socket_fd);
    free(spectrum);
    fclose(stream);
    return result;
}
