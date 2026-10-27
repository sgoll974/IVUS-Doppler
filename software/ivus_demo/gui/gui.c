#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <math.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include <SDL3/SDL.h>
#include <SDL3_image/SDL_image.h>

#define NK_INCLUDE_STANDARD_IO
#define NK_INCLUDE_STANDARD_VARARGS
#define NK_INCLUDE_DEFAULT_ALLOCATOR
#define NK_INCLUDE_FONT_BAKING
#define NK_INCLUDE_DEFAULT_FONT
#define NK_INCLUDE_COMMAND_USERDATA
#define NK_INCLUDE_VERTEX_BUFFER_OUTPUT

#ifndef NK_INCLUDE_FIXED_TYPES
#define NK_INT8 Sint8
#define NK_UINT8 Uint8
#define NK_INT16 Sint16
#define NK_UINT16 Uint16
#define NK_INT32 Sint32
#define NK_UINT32 Uint32
#define NK_SIZE_TYPE uintptr_t
#define NK_POINTER_TYPE uintptr_t
#endif

#define NK_ASSERT(condition) SDL_assert(condition)
#define NK_STATIC_ASSERT(exp) SDL_COMPILE_TIME_ASSERT(, exp)
#define NK_MEMSET(dst, c, len) SDL_memset(dst, c, len)
#define NK_MEMCPY(dst, src, len) SDL_memcpy(dst, src, len)
#define NK_VSNPRINTF(s, n, f, a) SDL_vsnprintf(s, n, f, a)
#define NK_STRTOD(str, endptr) SDL_strtod(str, endptr)

#define NK_IMPLEMENTATION
#include "nuklear.h"
#define NK_SDL3_RENDERER_IMPLEMENTATION
#include "nuklear_sdl3_renderer.h"

#include "../protocol.h"

#define MAX_FRAME_BYTES (2048u * 2048u)

struct receiver_state {
    pthread_mutex_t mutex;
    volatile int stop;
    int socket_fd;
    int listening;
    int connected;
    uint64_t last_packet_ms;
    uint32_t completed_frames;
    uint32_t damaged_frames;
    uint64_t missing_packets;
    uint64_t duplicate_packets;
    uint64_t malformed_packets;
    uint32_t published_generation;
    uint32_t published_frame_id;
    uint16_t published_width;
    uint16_t published_height;
    uint32_t published_area_milli;
    uint8_t *published_pixels;
};

struct assembly {
    int active;
    uint32_t frame_id;
    uint16_t width;
    uint16_t height;
    uint16_t packet_count;
    uint16_t packets_received;
    uint32_t area_milli;
    uint32_t expected_crc;
    uint8_t *pixels;
    uint8_t *packet_seen;
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

static uint64_t monotonic_milliseconds(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000u + (uint64_t)now.tv_nsec / 1000000u;
}

static void abandon_assembly(struct receiver_state *state, struct assembly *assembly) {
    if (assembly->active && assembly->packets_received < assembly->packet_count) {
        pthread_mutex_lock(&state->mutex);
        state->damaged_frames++;
        state->missing_packets += assembly->packet_count - assembly->packets_received;
        pthread_mutex_unlock(&state->mutex);
    }
    assembly->active = 0;
}

static int begin_assembly(struct receiver_state *state, struct assembly *assembly,
                          uint32_t frame_id, uint16_t width, uint16_t height,
                          uint16_t packet_count, uint32_t area_milli, uint32_t crc) {
    size_t frame_bytes = (size_t)width * height;
    abandon_assembly(state, assembly);
    if (!width || !height || frame_bytes > MAX_FRAME_BYTES || !packet_count ||
        packet_count != (frame_bytes + DD_MAX_PAYLOAD - 1u) / DD_MAX_PAYLOAD)
        return 0;
    assembly->pixels = realloc(assembly->pixels, frame_bytes);
    assembly->packet_seen = realloc(assembly->packet_seen, packet_count);
    if (!assembly->pixels || !assembly->packet_seen)
        return 0;
    memset(assembly->packet_seen, 0, packet_count);
    assembly->active = 1;
    assembly->frame_id = frame_id;
    assembly->width = width;
    assembly->height = height;
    assembly->packet_count = packet_count;
    assembly->packets_received = 0;
    assembly->area_milli = area_milli;
    assembly->expected_crc = crc;
    return 1;
}

static void publish_frame(struct receiver_state *state, struct assembly *assembly) {
    size_t bytes = (size_t)assembly->width * assembly->height;
    uint32_t calculated = crc32_bytes(assembly->pixels, bytes);
    pthread_mutex_lock(&state->mutex);
    if (calculated == assembly->expected_crc) {
        memcpy(state->published_pixels, assembly->pixels, bytes);
        state->published_width = assembly->width;
        state->published_height = assembly->height;
        state->published_frame_id = assembly->frame_id;
        state->published_area_milli = assembly->area_milli;
        state->published_generation++;
        state->completed_frames++;
    } else {
        state->damaged_frames++;
    }
    pthread_mutex_unlock(&state->mutex);
    assembly->active = 0;
}

static void *receiver_thread(void *argument) {
    struct receiver_state *state = argument;
    struct sockaddr_in local;
    struct timeval timeout = {0, 250000};
    int receive_buffer_bytes = 4 * 1024 * 1024;
    struct assembly assembly;
    uint8_t datagram[sizeof(struct dd_packet_header) + DD_MAX_PAYLOAD];
    memset(&assembly, 0, sizeof(assembly));

    state->socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (state->socket_fd < 0) return NULL;
    setsockopt(state->socket_fd, SOL_SOCKET, SO_RCVBUF,
               &receive_buffer_bytes, sizeof(receive_buffer_bytes));
    setsockopt(state->socket_fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    memset(&local, 0, sizeof(local));
    local.sin_family = AF_INET;
    local.sin_port = htons(DD_UDP_PORT);
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(state->socket_fd, (struct sockaddr *)&local, sizeof(local)) != 0) {
        close(state->socket_fd);
        state->socket_fd = -1;
        return NULL;
    }
    pthread_mutex_lock(&state->mutex);
    state->listening = 1;
    pthread_mutex_unlock(&state->mutex);

    while (!state->stop) {
        ssize_t received = recvfrom(state->socket_fd, datagram, sizeof(datagram), 0, NULL, NULL);
        if (received < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue;
            break;
        }
        if ((size_t)received < sizeof(struct dd_packet_header)) {
            pthread_mutex_lock(&state->mutex); state->malformed_packets++; pthread_mutex_unlock(&state->mutex);
            continue;
        }
        {
            struct dd_packet_header header;
            uint32_t frame_id, area_milli, crc;
            uint16_t index, count, payload, width, height;
            size_t offset, frame_bytes;
            memcpy(&header, datagram, sizeof(header));
            frame_id = ntohl(header.frame_id);
            index = ntohs(header.packet_index);
            count = ntohs(header.packet_count);
            payload = ntohs(header.payload_bytes);
            width = ntohs(header.width);
            height = ntohs(header.height);
            area_milli = ntohl(header.area_mm2_milli);
            crc = ntohl(header.frame_crc32);
            frame_bytes = (size_t)width * height;
            offset = (size_t)index * DD_MAX_PAYLOAD;

            if (ntohl(header.magic) != DD_PACKET_MAGIC ||
                header.version != DD_PROTOCOL_VERSION ||
                header.stream_type != DD_STREAM_IVUS_BMODE8 || index >= count ||
                !payload || payload > DD_MAX_PAYLOAD ||
                (size_t)received != sizeof(header) + payload ||
                offset + payload > frame_bytes) {
                pthread_mutex_lock(&state->mutex); state->malformed_packets++; pthread_mutex_unlock(&state->mutex);
                continue;
            }

            pthread_mutex_lock(&state->mutex);
            state->last_packet_ms = monotonic_milliseconds();
            state->connected = 1;
            pthread_mutex_unlock(&state->mutex);

            if (!assembly.active || assembly.frame_id != frame_id) {
                if (!begin_assembly(state, &assembly, frame_id, width, height, count, area_milli, crc)) {
                    pthread_mutex_lock(&state->mutex); state->malformed_packets++; pthread_mutex_unlock(&state->mutex);
                    continue;
                }
            }
            if (assembly.width != width || assembly.height != height ||
                assembly.packet_count != count || assembly.expected_crc != crc) {
                pthread_mutex_lock(&state->mutex); state->malformed_packets++; pthread_mutex_unlock(&state->mutex);
                continue;
            }
            if (assembly.packet_seen[index]) {
                pthread_mutex_lock(&state->mutex); state->duplicate_packets++; pthread_mutex_unlock(&state->mutex);
                continue;
            }
            memcpy(assembly.pixels + offset, datagram + sizeof(header), payload);
            assembly.packet_seen[index] = 1;
            assembly.packets_received++;
            if (assembly.packets_received == assembly.packet_count)
                publish_frame(state, &assembly);
        }
    }

    abandon_assembly(state, &assembly);
    free(assembly.pixels);
    free(assembly.packet_seen);
    if (state->socket_fd >= 0) close(state->socket_fd);
    return NULL;
}

static void grayscale_to_rgb(uint8_t *rgb, const uint8_t *gray, size_t pixels) {
    size_t i;
    for (i = 0; i < pixels; ++i) {
        rgb[3 * i] = gray[i];
        rgb[3 * i + 1] = gray[i];
        rgb[3 * i + 2] = gray[i];
    }
}

int main(void) {
    const int window_width = 1280;
    const int window_height = 800;
    const int display_fps = 60;
    SDL_Window *window = NULL;
    SDL_Renderer *renderer = NULL;
    SDL_Texture *placeholder = NULL;
    SDL_Texture *live_texture = NULL;
    struct nk_context *context = NULL;
    struct receiver_state receiver;
    pthread_t receiver_id;
    uint8_t *local_gray = malloc(MAX_FRAME_BYTES);
    uint8_t *local_rgb = malloc(MAX_FRAME_BYTES * 3u);
    uint32_t shown_generation = 0, frame_id = 0, completed = 0, damaged = 0;
    uint64_t missing = 0, malformed = 0, duplicates = 0, last_packet_ms = 0;
    uint16_t image_width = 0, image_height = 0;
    uint32_t area_milli = 0;
    int listening = 0, connected = 0;
    SDL_Event event;

    if (!local_gray || !local_rgb) return 1;
    memset(&receiver, 0, sizeof(receiver));
    receiver.socket_fd = -1;
    receiver.published_pixels = malloc(MAX_FRAME_BYTES);
    if (!receiver.published_pixels || pthread_mutex_init(&receiver.mutex, NULL) != 0) return 1;

    if (!SDL_Init(SDL_INIT_VIDEO) ||
        !SDL_CreateWindowAndRenderer("Doppler Dynamics IVUS", window_width, window_height,
                                    SDL_WINDOW_FULLSCREEN, &window, &renderer)) {
        fprintf(stderr, "SDL initialization failed: %s\n", SDL_GetError());
        return 1;
    }
    SDL_HideCursor();
    context = nk_sdl_init(window, renderer, nk_sdl_allocator());
    if (!context) return 1;
    {
        struct nk_font_atlas *atlas;
        struct nk_font_config config = nk_font_config(0);
        struct nk_font *font;
        atlas = nk_sdl_font_stash_begin(context);
        font = nk_font_atlas_add_from_file(atlas, "DejaVuSans.ttf", 28, &config);
        nk_sdl_font_stash_end(context);
        if (font) nk_style_set_font(context, &font->handle);
    }
    placeholder = IMG_LoadTexture(renderer, "heart_donor.png");
    if (!placeholder) fprintf(stderr, "Placeholder load failed: %s\n", SDL_GetError());
    if (pthread_create(&receiver_id, NULL, receiver_thread, &receiver) != 0) {
        fprintf(stderr, "Could not start UDP receiver thread\n");
        return 1;
    }

    for (;;) {
        uint64_t frame_start = SDL_GetTicks();
        int should_quit = 0;
        nk_input_begin(context);
        while (SDL_PollEvent(&event)) {
            if (event.type == SDL_EVENT_QUIT) should_quit = 1;
            nk_sdl_handle_event(context, &event);
        }
        nk_input_end(context);
        if (should_quit) break;

        pthread_mutex_lock(&receiver.mutex);
        listening = receiver.listening;
        connected = receiver.connected;
        last_packet_ms = receiver.last_packet_ms;
        completed = receiver.completed_frames;
        damaged = receiver.damaged_frames;
        missing = receiver.missing_packets;
        malformed = receiver.malformed_packets;
        duplicates = receiver.duplicate_packets;
        if (receiver.published_generation != shown_generation) {
            size_t bytes = (size_t)receiver.published_width * receiver.published_height;
            memcpy(local_gray, receiver.published_pixels, bytes);
            shown_generation = receiver.published_generation;
            frame_id = receiver.published_frame_id;
            image_width = receiver.published_width;
            image_height = receiver.published_height;
            area_milli = receiver.published_area_milli;
        }
        pthread_mutex_unlock(&receiver.mutex);
        if (connected && monotonic_milliseconds() - last_packet_ms > 1500u) connected = 0;

        if (image_width && image_height && shown_generation) {
            size_t pixels = (size_t)image_width * image_height;
            if (!live_texture) {
                live_texture = SDL_CreateTexture(renderer, SDL_PIXELFORMAT_RGB24,
                                                 SDL_TEXTUREACCESS_STREAMING,
                                                 image_width, image_height);
            }
            grayscale_to_rgb(local_rgb, local_gray, pixels);
            SDL_UpdateTexture(live_texture, NULL, local_rgb, image_width * 3);
        }

        if (nk_begin(context, "Main", nk_rect(0, 0, window_width, window_height), NK_WINDOW_NO_SCROLLBAR)) {
            const float image_panel = 635.0f;
            const char *status = connected ? "STREAMING" : listening ? "WAITING FOR STREAM" : "RECEIVER ERROR";
            nk_layout_row_dynamic(context, image_panel, 2);
            nk_image(context, nk_image_ptr(live_texture ? live_texture : placeholder));
            if (nk_group_begin(context, "Measurements", NK_WINDOW_BORDER | NK_WINDOW_NO_SCROLLBAR)) {
                nk_layout_row_dynamic(context, 36, 1);
                nk_labelf(context, NK_TEXT_LEFT, "Network: %s", status);
                nk_labelf(context, NK_TEXT_LEFT, "IVUS frame: %u", frame_id);
                nk_labelf(context, NK_TEXT_LEFT, "Completed frames: %u", completed);
                nk_labelf(context, NK_TEXT_LEFT, "Missing packets: %llu", (unsigned long long)missing);
                nk_labelf(context, NK_TEXT_LEFT, "Damaged frames: %u", damaged);
                nk_labelf(context, NK_TEXT_LEFT, "Malformed / duplicate: %llu / %llu",
                          (unsigned long long)malformed, (unsigned long long)duplicates);
                nk_layout_row_dynamic(context, 18, 1);
                nk_label(context, "Lumen area from dataset segmentation", NK_TEXT_LEFT);
                nk_layout_row_dynamic(context, 44, 1);
                nk_labelf(context, NK_TEXT_CENTERED, "%.3f mm^2", area_milli / 1000.0);
                nk_layout_row_dynamic(context, 18, 1);
                nk_label(context, "Doppler velocity", NK_TEXT_LEFT);
                nk_layout_row_dynamic(context, 44, 1);
                nk_label(context, "NOT CONNECTED", NK_TEXT_CENTERED);
                nk_layout_row_dynamic(context, 18, 1);
                nk_label(context, "Cardiac output", NK_TEXT_LEFT);
                nk_layout_row_dynamic(context, 44, 1);
                nk_label(context, "UNAVAILABLE", NK_TEXT_CENTERED);
                nk_group_end(context);
            }
            nk_layout_row_dynamic(context, 55, 1);
            nk_label(context, "Recorded porcine IVUS | 15_10_50_19 | Demo playback", NK_TEXT_CENTERED);
        }
        nk_end(context);

        SDL_RenderClear(renderer);
        nk_sdl_render(context, NK_ANTI_ALIASING_ON);
        SDL_RenderPresent(renderer);
        {
            uint64_t elapsed = SDL_GetTicks() - frame_start;
            uint64_t target = 1000u / display_fps;
            if (elapsed < target) SDL_Delay((Uint32)(target - elapsed));
        }
    }

    receiver.stop = 1;
    pthread_join(receiver_id, NULL);
    if (live_texture) SDL_DestroyTexture(live_texture);
    if (placeholder) SDL_DestroyTexture(placeholder);
    nk_sdl_shutdown(context);
    SDL_DestroyRenderer(renderer);
    SDL_DestroyWindow(window);
    SDL_Quit();
    pthread_mutex_destroy(&receiver.mutex);
    free(receiver.published_pixels);
    free(local_gray);
    free(local_rgb);
    return 0;
}
