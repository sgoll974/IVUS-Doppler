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

#define DDF_MAGIC "DDF1"
#define DDF_VERSION 1u

struct ddf_header {
    char magic[4];
    uint32_t version;
    uint32_t frame_count;
    uint32_t width;
    uint32_t height;
    uint32_t fps_num;
    uint32_t fps_den;
    uint32_t flags;
};

struct ddf_frame_header {
    uint32_t frame_index;
    uint32_t area_mm2_milli;
    uint32_t pixel_bytes;
    uint32_t crc32;
};

static uint32_t crc32_bytes(const uint8_t *data, size_t length) {
    uint32_t crc = 0xFFFFFFFFu;
    size_t i;
    for (i = 0; i < length; ++i) {
        uint32_t value = (crc ^ data[i]) & 0xFFu;
        int bit;
        for (bit = 0; bit < 8; ++bit)
            value = (value >> 1) ^ (0xEDB88320u & (uint32_t)-(int32_t)(value & 1u));
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

static uint64_t realtime_microseconds(void) {
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    return (uint64_t)now.tv_sec * 1000000u + (uint64_t)now.tv_nsec / 1000u;
}

static void sleep_until(struct timespec *deadline, uint32_t fps_num, uint32_t fps_den) {
    uint64_t increment = (1000000000ull * fps_den) / fps_num;
    deadline->tv_nsec += (long)increment;
    deadline->tv_sec += deadline->tv_nsec / 1000000000L;
    deadline->tv_nsec %= 1000000000L;
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, deadline, NULL) == EINTR) {}
}

static int read_exact(FILE *stream, void *destination, size_t bytes) {
    return fread(destination, 1, bytes, stream) == bytes;
}

static int send_frame(int socket_fd, const struct sockaddr_in *destination,
                      const uint8_t *pixels, uint32_t bytes, uint32_t frame_id,
                      uint32_t width, uint32_t height, uint32_t area_milli,
                      uint32_t crc, int loop_start) {
    uint8_t datagram[sizeof(struct dd_packet_header) + DD_MAX_PAYLOAD];
    uint16_t count = (uint16_t)((bytes + DD_MAX_PAYLOAD - 1u) / DD_MAX_PAYLOAD);
    uint16_t index;
    const struct timespec packet_pause = {0, 100000}; /* 0.1 ms limits bursts. */

    for (index = 0; index < count; ++index) {
        uint32_t offset = (uint32_t)index * DD_MAX_PAYLOAD;
        uint16_t payload = (uint16_t)((bytes - offset) > DD_MAX_PAYLOAD
                                     ? DD_MAX_PAYLOAD : (bytes - offset));
        uint16_t flags = 0;
        struct dd_packet_header header;
        if (index == 0) flags |= DD_FLAG_FRAME_START;
        if (index + 1u == count) flags |= DD_FLAG_FRAME_END;
        if (loop_start && index == 0) flags |= DD_FLAG_LOOP_START;

        memset(&header, 0, sizeof(header));
        header.magic = htonl(DD_PACKET_MAGIC);
        header.version = DD_PROTOCOL_VERSION;
        header.stream_type = DD_STREAM_IVUS_BMODE8;
        header.flags = htons(flags);
        header.frame_id = htonl(frame_id);
        header.timestamp_us = host_to_network_u64(realtime_microseconds());
        header.packet_index = htons(index);
        header.packet_count = htons(count);
        header.payload_bytes = htons(payload);
        header.width = htons((uint16_t)width);
        header.height = htons((uint16_t)height);
        header.area_mm2_milli = htonl(area_milli);
        header.frame_crc32 = htonl(crc);
        memcpy(datagram, &header, sizeof(header));
        memcpy(datagram + sizeof(header), pixels + offset, payload);

        if (sendto(socket_fd, datagram, sizeof(header) + payload, 0,
                   (const struct sockaddr *)destination, sizeof(*destination)) < 0) {
            perror("sendto");
            return 0;
        }
        if (index + 1u < count)
            nanosleep(&packet_pause, NULL);
    }
    return 1;
}

int main(int argc, char **argv) {
    const char *path;
    const char *destination_ip;
    unsigned long port = DD_UDP_PORT;
    unsigned long loops = 0;
    FILE *stream;
    struct ddf_header file_header;
    uint8_t *pixels;
    uint64_t expected_bytes;
    int socket_fd;
    struct sockaddr_in destination;
    struct timespec deadline;
    uint32_t transmitted_id = 0;
    unsigned long loop = 0;
    int result = 0;

    if (argc < 3 || argc > 5) {
        fprintf(stderr, "Usage: %s FILE.ddf PI_IP [PORT] [LOOPS]\n", argv[0]);
        fprintf(stderr, "LOOPS=0 repeats until interrupted.\n");
        return 2;
    }
    path = argv[1];
    destination_ip = argv[2];
    if (argc >= 4) port = strtoul(argv[3], NULL, 10);
    if (argc >= 5) loops = strtoul(argv[4], NULL, 10);
    if (port == 0 || port > 65535) {
        fprintf(stderr, "Invalid UDP port\n");
        return 2;
    }

    stream = fopen(path, "rb");
    if (!stream) { perror(path); return 1; }
    if (!read_exact(stream, &file_header, sizeof(file_header)) ||
        memcmp(file_header.magic, DDF_MAGIC, 4) != 0 ||
        file_header.version != DDF_VERSION || !file_header.frame_count ||
        !file_header.width || !file_header.height || !file_header.fps_num ||
        !file_header.fps_den || file_header.width > 2048 || file_header.height > 2048) {
        fprintf(stderr, "Invalid or unsupported DDF header\n");
        fclose(stream);
        return 1;
    }
    expected_bytes = (uint64_t)file_header.width * file_header.height;
    pixels = malloc((size_t)expected_bytes);
    if (!pixels) { fprintf(stderr, "Could not allocate frame buffer\n"); fclose(stream); return 1; }

    socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (socket_fd < 0) { perror("socket"); free(pixels); fclose(stream); return 1; }
    memset(&destination, 0, sizeof(destination));
    destination.sin_family = AF_INET;
    destination.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, destination_ip, &destination.sin_addr) != 1) {
        fprintf(stderr, "Invalid destination IP address\n");
        close(socket_fd); free(pixels); fclose(stream); return 1;
    }

    clock_gettime(CLOCK_MONOTONIC, &deadline);
    printf("Streaming %ux%u, %u frames at %.6f fps to %s:%lu\n",
           file_header.width, file_header.height, file_header.frame_count,
           (double)file_header.fps_num / file_header.fps_den, destination_ip, port);

    while (loops == 0 || loop < loops) {
        uint32_t local;
        if (fseek(stream, (long)sizeof(file_header), SEEK_SET) != 0) {
            perror("fseek");
            result = 1;
            break;
        }
        for (local = 0; local < file_header.frame_count; ++local) {
            struct ddf_frame_header frame;
            uint32_t calculated;
            if (!read_exact(stream, &frame, sizeof(frame)) ||
                frame.pixel_bytes != expected_bytes ||
                !read_exact(stream, pixels, frame.pixel_bytes)) {
                fprintf(stderr, "Truncated or invalid frame %u\n", local);
                result = 1;
                goto done;
            }
            calculated = crc32_bytes(pixels, frame.pixel_bytes);
            if (calculated != frame.crc32) {
                fprintf(stderr, "CRC mismatch in stored frame %u\n", local);
                result = 1;
                goto done;
            }
            if (!send_frame(socket_fd, &destination, pixels, frame.pixel_bytes,
                            transmitted_id++, file_header.width, file_header.height,
                            frame.area_mm2_milli, frame.crc32, local == 0)) {
                result = 1;
                goto done;
            }
            printf("\rLoop %lu frame %u/%u", loop + 1, local + 1, file_header.frame_count);
            fflush(stdout);
            sleep_until(&deadline, file_header.fps_num, file_header.fps_den);
        }
        ++loop;
        putchar('\n');
    }

done:
    putchar('\n');
    close(socket_fd);
    free(pixels);
    fclose(stream);
    return result;
}
