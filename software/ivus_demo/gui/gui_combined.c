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
#define DOPPLER_HISTORY_COLUMNS 512u
#define DOPPLER_MAX_BINS 256u
#define CONNECTION_TIMEOUT_MS 1500u

struct ivus_receiver_state {
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

struct ivus_assembly {
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

struct doppler_receiver_state {
    pthread_mutex_t mutex;
    volatile int stop;
    int socket_fd;
    int listening;
    int connected;
    uint64_t last_packet_ms;
    uint64_t first_packet_ms;
    uint64_t valid_samples;
    uint64_t missing_samples;
    uint64_t crc_failures;
    uint64_t malformed_packets;
    uint32_t loop_starts;
    uint32_t beat_updates;
    uint32_t previous_frame_id;
    int have_previous_frame;
    uint32_t generation;
    uint16_t spectral_bins;
    uint16_t write_column;
    uint8_t *history;
    uint32_t sample_index;
    uint32_t heart_rate_milli_bpm;
    uint32_t lvot_diameter_um;
    uint32_t vti_milli_cm;
    uint32_t transmitted_sv_microliters;
    uint32_t transmitted_co_milli_lpm;
};

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

static uint64_t monotonic_milliseconds(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000u +
           (uint64_t)now.tv_nsec / 1000000u;
}

static void abandon_ivus_assembly(struct ivus_receiver_state *state,
                                  struct ivus_assembly *assembly) {
    if (assembly->active && assembly->packets_received < assembly->packet_count) {
        pthread_mutex_lock(&state->mutex);
        state->damaged_frames++;
        state->missing_packets +=
            assembly->packet_count - assembly->packets_received;
        pthread_mutex_unlock(&state->mutex);
    }
    assembly->active = 0;
}

static int begin_ivus_assembly(struct ivus_receiver_state *state,
                               struct ivus_assembly *assembly,
                               uint32_t frame_id, uint16_t width,
                               uint16_t height, uint16_t packet_count,
                               uint32_t area_milli, uint32_t crc) {
    size_t frame_bytes = (size_t)width * height;
    abandon_ivus_assembly(state, assembly);
    if (!width || !height || frame_bytes > MAX_FRAME_BYTES || !packet_count ||
        packet_count !=
            (frame_bytes + DD_MAX_PAYLOAD - 1u) / DD_MAX_PAYLOAD)
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

static void publish_ivus_frame(struct ivus_receiver_state *state,
                               struct ivus_assembly *assembly) {
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

static void *ivus_receiver_thread(void *argument) {
    struct ivus_receiver_state *state = argument;
    struct sockaddr_in local;
    struct timeval timeout = {0, 250000};
    int receive_buffer_bytes = 4 * 1024 * 1024;
    struct ivus_assembly assembly;
    uint8_t datagram[sizeof(struct dd_packet_header) + DD_MAX_PAYLOAD];
    memset(&assembly, 0, sizeof(assembly));

    state->socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (state->socket_fd < 0)
        return NULL;
    setsockopt(state->socket_fd, SOL_SOCKET, SO_RCVBUF,
               &receive_buffer_bytes, sizeof(receive_buffer_bytes));
    setsockopt(state->socket_fd, SOL_SOCKET, SO_RCVTIMEO,
               &timeout, sizeof(timeout));
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
        ssize_t received = recvfrom(state->socket_fd, datagram,
                                    sizeof(datagram), 0, NULL, NULL);
        struct dd_packet_header header;
        uint32_t frame_id, area_milli, crc;
        uint16_t index, count, payload, width, height;
        size_t offset, frame_bytes;

        if (received < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
                continue;
            break;
        }
        if ((size_t)received < sizeof(header)) {
            pthread_mutex_lock(&state->mutex);
            state->malformed_packets++;
            pthread_mutex_unlock(&state->mutex);
            continue;
        }
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
            pthread_mutex_lock(&state->mutex);
            state->malformed_packets++;
            pthread_mutex_unlock(&state->mutex);
            continue;
        }

        pthread_mutex_lock(&state->mutex);
        state->last_packet_ms = monotonic_milliseconds();
        state->connected = 1;
        pthread_mutex_unlock(&state->mutex);

        if (!assembly.active || assembly.frame_id != frame_id) {
            if (!begin_ivus_assembly(state, &assembly, frame_id, width,
                                     height, count, area_milli, crc)) {
                pthread_mutex_lock(&state->mutex);
                state->malformed_packets++;
                pthread_mutex_unlock(&state->mutex);
                continue;
            }
        }
        if (assembly.width != width || assembly.height != height ||
            assembly.packet_count != count || assembly.expected_crc != crc) {
            pthread_mutex_lock(&state->mutex);
            state->malformed_packets++;
            pthread_mutex_unlock(&state->mutex);
            continue;
        }
        if (assembly.packet_seen[index]) {
            pthread_mutex_lock(&state->mutex);
            state->duplicate_packets++;
            pthread_mutex_unlock(&state->mutex);
            continue;
        }
        memcpy(assembly.pixels + offset, datagram + sizeof(header), payload);
        assembly.packet_seen[index] = 1;
        assembly.packets_received++;
        if (assembly.packets_received == assembly.packet_count)
            publish_ivus_frame(state, &assembly);
    }

    abandon_ivus_assembly(state, &assembly);
    free(assembly.pixels);
    free(assembly.packet_seen);
    if (state->socket_fd >= 0)
        close(state->socket_fd);
    return NULL;
}

static void *doppler_receiver_thread(void *argument) {
    struct doppler_receiver_state *state = argument;
    struct sockaddr_in local;
    struct timeval timeout = {0, 250000};
    int receive_buffer_bytes = 4 * 1024 * 1024;
    uint8_t datagram[sizeof(struct dd_packet_header) + DD_MAX_PAYLOAD];

    state->socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (state->socket_fd < 0)
        return NULL;
    setsockopt(state->socket_fd, SOL_SOCKET, SO_RCVBUF,
               &receive_buffer_bytes, sizeof(receive_buffer_bytes));
    setsockopt(state->socket_fd, SOL_SOCKET, SO_RCVTIMEO,
               &timeout, sizeof(timeout));
    memset(&local, 0, sizeof(local));
    local.sin_family = AF_INET;
    local.sin_port = htons(DD_DOPPLER_UDP_PORT);
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
        ssize_t received = recvfrom(state->socket_fd, datagram,
                                    sizeof(datagram), 0, NULL, NULL);
        struct dd_packet_header header;
        struct dd_doppler_payload_header metadata;
        const uint8_t *payload;
        const uint8_t *spectrum;
        uint16_t flags, payload_bytes, width, height, packet_index,
                 packet_count;
        uint32_t frame_id, expected_crc, calculated_crc;

        if (received < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
                continue;
            break;
        }
        if ((size_t)received < sizeof(header)) {
            pthread_mutex_lock(&state->mutex);
            state->malformed_packets++;
            pthread_mutex_unlock(&state->mutex);
            continue;
        }
        memcpy(&header, datagram, sizeof(header));
        flags = ntohs(header.flags);
        frame_id = ntohl(header.frame_id);
        packet_index = ntohs(header.packet_index);
        packet_count = ntohs(header.packet_count);
        payload_bytes = ntohs(header.payload_bytes);
        width = ntohs(header.width);
        height = ntohs(header.height);
        expected_crc = ntohl(header.frame_crc32);
        payload = datagram + sizeof(header);

        if (ntohl(header.magic) != DD_PACKET_MAGIC ||
            header.version != DD_PROTOCOL_VERSION ||
            header.stream_type != DD_STREAM_DOPPLER_PW8 ||
            packet_index != 0 || packet_count != 1 || height != 1 ||
            !width || width > DOPPLER_MAX_BINS ||
            payload_bytes != sizeof(metadata) + width ||
            (size_t)received != sizeof(header) + payload_bytes) {
            pthread_mutex_lock(&state->mutex);
            state->malformed_packets++;
            pthread_mutex_unlock(&state->mutex);
            continue;
        }
        calculated_crc = crc32_bytes(payload, payload_bytes);
        if (calculated_crc != expected_crc) {
            pthread_mutex_lock(&state->mutex);
            state->crc_failures++;
            pthread_mutex_unlock(&state->mutex);
            continue;
        }

        memcpy(&metadata, payload, sizeof(metadata));
        spectrum = payload + sizeof(metadata);

        pthread_mutex_lock(&state->mutex);
        if (state->have_previous_frame &&
            frame_id > state->previous_frame_id + 1u)
            state->missing_samples +=
                frame_id - state->previous_frame_id - 1u;
        state->previous_frame_id = frame_id;
        state->have_previous_frame = 1;
        state->last_packet_ms = monotonic_milliseconds();
        if (state->valid_samples == 0u)
            state->first_packet_ms = state->last_packet_ms;
        state->connected = 1;
        state->valid_samples++;
        if (flags & DD_FLAG_LOOP_START)
            state->loop_starts++;
        if (flags & DD_FLAG_BEAT_UPDATE)
            state->beat_updates++;
        state->spectral_bins = width;
        memcpy(state->history +
                   (size_t)state->write_column * DOPPLER_MAX_BINS,
               spectrum, width);
        if (width < DOPPLER_MAX_BINS)
            memset(state->history +
                       (size_t)state->write_column * DOPPLER_MAX_BINS + width,
                   0, DOPPLER_MAX_BINS - width);
        state->write_column =
            (uint16_t)((state->write_column + 1u) % DOPPLER_HISTORY_COLUMNS);
        state->sample_index = ntohl(metadata.sample_index);
        state->heart_rate_milli_bpm =
            ntohl(metadata.heart_rate_milli_bpm);
        state->lvot_diameter_um = ntohl(metadata.lvot_diameter_um);
        state->vti_milli_cm = ntohl(metadata.vti_milli_cm);
        state->transmitted_sv_microliters =
            ntohl(metadata.stroke_volume_microliters);
        state->transmitted_co_milli_lpm =
            ntohl(metadata.cardiac_output_milli_lpm);
        state->generation++;
        pthread_mutex_unlock(&state->mutex);
    }

    if (state->socket_fd >= 0)
        close(state->socket_fd);
    return NULL;
}

static void grayscale_to_rgb(uint8_t *rgb, const uint8_t *gray,
                             size_t pixels) {
    size_t i;
    for (i = 0; i < pixels; ++i) {
        rgb[3u * i] = gray[i];
        rgb[3u * i + 1u] = gray[i];
        rgb[3u * i + 2u] = gray[i];
    }
}

static void doppler_color(uint8_t value, uint8_t *red, uint8_t *green,
                          uint8_t *blue) {
    if (value < 64u) {
        *red = 0;
        *green = 0;
        *blue = (uint8_t)(value * 4u);
    } else if (value < 128u) {
        *red = (uint8_t)((value - 64u) * 4u);
        *green = 0;
        *blue = 255u;
    } else if (value < 192u) {
        *red = 255u;
        *green = (uint8_t)((value - 128u) * 4u);
        *blue = (uint8_t)(255u - (value - 128u) * 4u);
    } else {
        *red = 255u;
        *green = 255u;
        *blue = (uint8_t)((value - 192u) * 4u);
    }
}

static void history_to_rgb(uint8_t *rgb, const uint8_t *history,
                           uint16_t bins) {
    uint32_t x, y;
    for (y = 0; y < bins; ++y) {
        uint32_t source_bin = bins - 1u - y;
        for (x = 0; x < DOPPLER_HISTORY_COLUMNS; ++x) {
            uint8_t value =
                history[(size_t)x * DOPPLER_MAX_BINS + source_bin];
            size_t pixel =
                ((size_t)y * DOPPLER_HISTORY_COLUMNS + x) * 3u;
            doppler_color(value, &rgb[pixel], &rgb[pixel + 1u],
                          &rgb[pixel + 2u]);
        }
    }
}

static const char *connection_status(int listening, int connected,
                                     uint64_t last_packet_ms) {
    if (!listening)
        return "RECEIVER ERROR";
    if (connected &&
        monotonic_milliseconds() - last_packet_ms <= CONNECTION_TIMEOUT_MS)
        return "STREAMING";
    return "WAITING";
}

static void colored_status_label(struct nk_context *context,
                                 const char *label, const char *value,
                                 int positive) {
    struct nk_color previous = context->style.text.color;
    context->style.text.color = positive ? nk_rgb(91, 222, 158)
                                         : nk_rgb(255, 191, 71);
    nk_labelf(context, NK_TEXT_LEFT, "%s  %s", label, value);
    context->style.text.color = previous;
}

static void apply_dark_medical_theme(struct nk_context *context) {
    struct nk_color colors[NK_COLOR_COUNT];
    colors[NK_COLOR_TEXT] = nk_rgb(225, 235, 242);
    colors[NK_COLOR_WINDOW] = nk_rgb(8, 18, 30);
    colors[NK_COLOR_HEADER] = nk_rgb(13, 36, 58);
    colors[NK_COLOR_BORDER] = nk_rgb(42, 91, 112);
    colors[NK_COLOR_BUTTON] = nk_rgb(20, 91, 112);
    colors[NK_COLOR_BUTTON_HOVER] = nk_rgb(27, 127, 147);
    colors[NK_COLOR_BUTTON_ACTIVE] = nk_rgb(20, 153, 158);
    colors[NK_COLOR_TOGGLE] = nk_rgb(18, 42, 60);
    colors[NK_COLOR_TOGGLE_HOVER] = nk_rgb(25, 69, 87);
    colors[NK_COLOR_TOGGLE_CURSOR] = nk_rgb(51, 207, 196);
    colors[NK_COLOR_SELECT] = nk_rgb(18, 42, 60);
    colors[NK_COLOR_SELECT_ACTIVE] = nk_rgb(20, 153, 158);
    colors[NK_COLOR_SLIDER] = nk_rgb(17, 37, 52);
    colors[NK_COLOR_SLIDER_CURSOR] = nk_rgb(51, 207, 196);
    colors[NK_COLOR_SLIDER_CURSOR_HOVER] = nk_rgb(77, 225, 213);
    colors[NK_COLOR_SLIDER_CURSOR_ACTIVE] = nk_rgb(255, 191, 71);
    colors[NK_COLOR_PROPERTY] = nk_rgb(14, 30, 45);
    colors[NK_COLOR_EDIT] = nk_rgb(14, 30, 45);
    colors[NK_COLOR_EDIT_CURSOR] = nk_rgb(225, 235, 242);
    colors[NK_COLOR_COMBO] = nk_rgb(14, 30, 45);
    colors[NK_COLOR_CHART] = nk_rgb(5, 13, 23);
    colors[NK_COLOR_CHART_COLOR] = nk_rgb(51, 207, 196);
    colors[NK_COLOR_CHART_COLOR_HIGHLIGHT] = nk_rgb(255, 191, 71);
    colors[NK_COLOR_SCROLLBAR] = nk_rgb(8, 18, 30);
    colors[NK_COLOR_SCROLLBAR_CURSOR] = nk_rgb(42, 91, 112);
    colors[NK_COLOR_SCROLLBAR_CURSOR_HOVER] = nk_rgb(51, 135, 151);
    colors[NK_COLOR_SCROLLBAR_CURSOR_ACTIVE] = nk_rgb(51, 207, 196);
    colors[NK_COLOR_TAB_HEADER] = nk_rgb(13, 36, 58);
    nk_style_from_table(context, colors);
    context->style.window.padding = nk_vec2(14, 10);
    context->style.window.group_padding = nk_vec2(12, 10);
    context->style.window.spacing = nk_vec2(8, 8);
}

int main(void) {
    const int window_width = 1280;
    const int window_height = 800;
    const int display_fps = 30;
    SDL_Window *window = NULL;
    SDL_Renderer *renderer = NULL;
    SDL_Texture *placeholder = NULL;
    SDL_Texture *ivus_texture = NULL;
    SDL_Texture *doppler_texture = NULL;
    struct nk_context *context = NULL;
    struct ivus_receiver_state ivus;
    struct doppler_receiver_state doppler;
    pthread_t ivus_thread_id, doppler_thread_id;
    uint8_t *ivus_gray = malloc(MAX_FRAME_BYTES);
    uint8_t *ivus_rgb = malloc(MAX_FRAME_BYTES * 3u);
    uint8_t *doppler_history =
        calloc(DOPPLER_HISTORY_COLUMNS * DOPPLER_MAX_BINS, 1u);
    uint8_t *doppler_rgb =
        malloc(DOPPLER_HISTORY_COLUMNS * DOPPLER_MAX_BINS * 3u);
    uint32_t ivus_generation = 0, doppler_generation = 0;
    uint32_t ivus_damaged = 0;
    uint64_t ivus_missing = 0, ivus_last_packet_ms = 0;
    uint16_t ivus_width = 0, ivus_height = 0, doppler_bins = 0;
    uint32_t ivus_area_milli = 0;
    int ivus_listening = 0, ivus_connected = 0;
    uint64_t doppler_valid = 0, doppler_missing = 0, doppler_crc = 0;
    uint64_t doppler_malformed = 0, doppler_last_packet_ms = 0;
    uint64_t doppler_first_packet_ms = 0;
    uint32_t hr_milli = 0, diameter_um = 0, vti_milli = 0;
    int doppler_listening = 0, doppler_connected = 0;
    uint64_t health_window_start = monotonic_milliseconds();
    uint64_t ivus_missing_baseline = 0, doppler_missing_baseline = 0;
    uint64_t doppler_crc_baseline = 0, doppler_malformed_baseline = 0;
    uint32_t ivus_damaged_baseline = 0;
    int ivus_health_ok = 1, doppler_health_ok = 1;
    SDL_Event event;
    struct nk_font *body_font = NULL;
    struct nk_font *metric_font = NULL;

    if (!ivus_gray || !ivus_rgb || !doppler_history || !doppler_rgb)
        return 1;
    memset(&ivus, 0, sizeof(ivus));
    memset(&doppler, 0, sizeof(doppler));
    ivus.socket_fd = -1;
    doppler.socket_fd = -1;
    ivus.published_pixels = malloc(MAX_FRAME_BYTES);
    doppler.history = calloc(
        DOPPLER_HISTORY_COLUMNS * DOPPLER_MAX_BINS, 1u);
    if (!ivus.published_pixels || !doppler.history ||
        pthread_mutex_init(&ivus.mutex, NULL) != 0 ||
        pthread_mutex_init(&doppler.mutex, NULL) != 0)
        return 1;

    if (!SDL_Init(SDL_INIT_VIDEO) ||
        !SDL_CreateWindowAndRenderer("Doppler Dynamics", window_width,
                                    window_height, SDL_WINDOW_FULLSCREEN,
                                    &window, &renderer)) {
        fprintf(stderr, "SDL initialization failed: %s\n", SDL_GetError());
        return 1;
    }
    SDL_HideCursor();
    context = nk_sdl_init(window, renderer, nk_sdl_allocator());
    if (!context)
        return 1;
    {
        struct nk_font_atlas *atlas;
        struct nk_font_config config = nk_font_config(0);
        atlas = nk_sdl_font_stash_begin(context);
        body_font = nk_font_atlas_add_from_file(atlas, "DejaVuSans.ttf", 21,
                                                &config);
        metric_font = nk_font_atlas_add_from_file(atlas, "DejaVuSans.ttf", 39,
                                                  &config);
        nk_sdl_font_stash_end(context);
        if (body_font)
            nk_style_set_font(context, &body_font->handle);
    }
    apply_dark_medical_theme(context);
    placeholder = IMG_LoadTexture(renderer, "heart_donor.png");
    if (pthread_create(&ivus_thread_id, NULL, ivus_receiver_thread, &ivus) !=
            0 ||
        pthread_create(&doppler_thread_id, NULL, doppler_receiver_thread,
                       &doppler) != 0) {
        fprintf(stderr, "Could not start receiver threads\n");
        return 1;
    }

    for (;;) {
        uint64_t frame_start = SDL_GetTicks();
        int should_quit = 0;
        double diameter_cm, area_cm2, vti_cm, heart_rate_bpm;
        double calculated_sv_ml, calculated_co_lpm;
        double doppler_rate_hz = 0.0;
        uint64_t now_ms;
        const char *ivus_status;
        const char *doppler_status;

        nk_input_begin(context);
        while (SDL_PollEvent(&event)) {
            if (event.type == SDL_EVENT_QUIT)
                should_quit = 1;
            if (event.type == SDL_EVENT_KEY_DOWN &&
                event.key.key == SDLK_ESCAPE)
                should_quit = 1;
            nk_sdl_handle_event(context, &event);
        }
        nk_input_end(context);
        if (should_quit)
            break;

        pthread_mutex_lock(&ivus.mutex);
        ivus_listening = ivus.listening;
        ivus_connected = ivus.connected;
        ivus_last_packet_ms = ivus.last_packet_ms;
        ivus_damaged = ivus.damaged_frames;
        ivus_missing = ivus.missing_packets;
        if (ivus.published_generation != ivus_generation) {
            size_t bytes =
                (size_t)ivus.published_width * ivus.published_height;
            memcpy(ivus_gray, ivus.published_pixels, bytes);
            ivus_generation = ivus.published_generation;
            ivus_width = ivus.published_width;
            ivus_height = ivus.published_height;
            ivus_area_milli = ivus.published_area_milli;
        }
        pthread_mutex_unlock(&ivus.mutex);

        pthread_mutex_lock(&doppler.mutex);
        doppler_listening = doppler.listening;
        doppler_connected = doppler.connected;
        doppler_last_packet_ms = doppler.last_packet_ms;
        doppler_first_packet_ms = doppler.first_packet_ms;
        doppler_valid = doppler.valid_samples;
        doppler_missing = doppler.missing_samples;
        doppler_crc = doppler.crc_failures;
        doppler_malformed = doppler.malformed_packets;
        hr_milli = doppler.heart_rate_milli_bpm;
        diameter_um = doppler.lvot_diameter_um;
        vti_milli = doppler.vti_milli_cm;
        if (doppler.generation != doppler_generation) {
            uint32_t column;
            doppler_bins = doppler.spectral_bins;
            for (column = 0; column < DOPPLER_HISTORY_COLUMNS; ++column) {
                uint32_t source =
                    (doppler.write_column + column) %
                    DOPPLER_HISTORY_COLUMNS;
                memcpy(doppler_history +
                           (size_t)column * DOPPLER_MAX_BINS,
                       doppler.history +
                           (size_t)source * DOPPLER_MAX_BINS,
                       DOPPLER_MAX_BINS);
            }
            doppler_generation = doppler.generation;
        }
        pthread_mutex_unlock(&doppler.mutex);

        if (doppler_valid > 1u &&
            doppler_last_packet_ms > doppler_first_packet_ms) {
            doppler_rate_hz =
                (double)(doppler_valid - 1u) * 1000.0 /
                (double)(doppler_last_packet_ms - doppler_first_packet_ms);
        }
        now_ms = monotonic_milliseconds();
        if (now_ms - health_window_start >= 5000u) {
            ivus_health_ok =
                ivus_missing == ivus_missing_baseline &&
                ivus_damaged == ivus_damaged_baseline;
            doppler_health_ok =
                doppler_missing == doppler_missing_baseline &&
                doppler_crc == doppler_crc_baseline &&
                doppler_malformed == doppler_malformed_baseline;
            ivus_missing_baseline = ivus_missing;
            ivus_damaged_baseline = ivus_damaged;
            doppler_missing_baseline = doppler_missing;
            doppler_crc_baseline = doppler_crc;
            doppler_malformed_baseline = doppler_malformed;
            health_window_start = now_ms;
        }

        if (ivus_width && ivus_height && ivus_generation) {
            size_t pixels = (size_t)ivus_width * ivus_height;
            if (!ivus_texture) {
                ivus_texture = SDL_CreateTexture(
                    renderer, SDL_PIXELFORMAT_RGB24,
                    SDL_TEXTUREACCESS_STREAMING, ivus_width, ivus_height);
            }
            grayscale_to_rgb(ivus_rgb, ivus_gray, pixels);
            SDL_UpdateTexture(ivus_texture, NULL, ivus_rgb,
                              ivus_width * 3);
        }
        if (doppler_bins && doppler_generation) {
            if (!doppler_texture) {
                doppler_texture = SDL_CreateTexture(
                    renderer, SDL_PIXELFORMAT_RGB24,
                    SDL_TEXTUREACCESS_STREAMING,
                    DOPPLER_HISTORY_COLUMNS, doppler_bins);
            }
            history_to_rgb(doppler_rgb, doppler_history, doppler_bins);
            SDL_UpdateTexture(doppler_texture, NULL, doppler_rgb,
                              DOPPLER_HISTORY_COLUMNS * 3u);
        }

        diameter_cm = diameter_um / 10000.0;
        area_cm2 = 3.14159265358979323846 * diameter_cm * diameter_cm / 4.0;
        vti_cm = vti_milli / 1000.0;
        heart_rate_bpm = hr_milli / 1000.0;
        calculated_sv_ml = area_cm2 * vti_cm;
        calculated_co_lpm = calculated_sv_ml * heart_rate_bpm / 1000.0;
        ivus_status = connection_status(ivus_listening, ivus_connected,
                                        ivus_last_packet_ms);
        doppler_status = connection_status(
            doppler_listening, doppler_connected, doppler_last_packet_ms);

        if (nk_begin(context, "Main",
                     nk_rect(0, 0, window_width, window_height),
                     NK_WINDOW_NO_SCROLLBAR)) {
            nk_layout_row_dynamic(context, 48, 1);
            if (metric_font)
                nk_style_push_font(context, &metric_font->handle);
            nk_label(context, "DOPPLER DYNAMICS", NK_TEXT_CENTERED);
            if (metric_font)
                nk_style_pop_font(context);
            nk_layout_row_dynamic(context, 22, 1);
            nk_label(context,
                     "SIMULTANEOUS IVUS IMAGING + DOPPLER HEMODYNAMICS",
                     NK_TEXT_CENTERED);

            nk_layout_row_dynamic(context, 392, 2);
            if (nk_group_begin(context, "IVUS ANATOMICAL VIEW",
                               NK_WINDOW_BORDER | NK_WINDOW_NO_SCROLLBAR)) {
                nk_layout_row_dynamic(context, 24, 1);
                nk_label(context, "IVUS ANATOMICAL VIEW",
                         NK_TEXT_CENTERED);
                nk_layout_row_begin(context, NK_STATIC, 325, 3);
                nk_layout_row_push(context, 112);
                nk_spacing(context, 1);
                nk_layout_row_push(context, 325);
                nk_image(context,
                         nk_image_ptr(ivus_texture ? ivus_texture :
                                                    placeholder));
                nk_layout_row_push(context, 112);
                nk_spacing(context, 1);
                nk_layout_row_end(context);
                nk_group_end(context);
            }
            if (nk_group_begin(context, "PULSED-WAVE DOPPLER SPECTRUM",
                               NK_WINDOW_BORDER | NK_WINDOW_NO_SCROLLBAR)) {
                nk_layout_row_dynamic(context, 24, 1);
                nk_label(context, "PULSED-WAVE DOPPLER SPECTRUM",
                         NK_TEXT_CENTERED);
                nk_layout_row_dynamic(context, 305, 1);
                if (doppler_texture)
                    nk_image(context, nk_image_ptr(doppler_texture));
                else
                    nk_label(context, "WAITING FOR DOPPLER STREAM",
                             NK_TEXT_CENTERED);
                nk_layout_row_dynamic(context, 24, 1);
                nk_label(context, "TIME  ->       DOPPLER SPECTRAL INTENSITY",
                         NK_TEXT_CENTERED);
                nk_group_end(context);
            }

            nk_layout_row_dynamic(context, 225, 3);
            if (nk_group_begin(context, "LIVE SYSTEM HEALTH",
                               NK_WINDOW_BORDER | NK_WINDOW_NO_SCROLLBAR)) {
                nk_layout_row_dynamic(context, 29, 1);
                colored_status_label(context, "IVUS", ivus_status,
                                     strcmp(ivus_status, "STREAMING") == 0);
                colored_status_label(context, "DOPPLER", doppler_status,
                                     strcmp(doppler_status, "STREAMING") == 0);
                nk_labelf(context, NK_TEXT_LEFT, "Doppler rate: %.1f lines/s",
                          doppler_rate_hz);
                colored_status_label(context, "IVUS LIVE HEALTH",
                                     ivus_health_ok ? "PASS" : "CHECK",
                                     ivus_health_ok);
                colored_status_label(context, "DOPPLER LIVE HEALTH",
                                     doppler_health_ok ? "PASS" : "CHECK",
                                     doppler_health_ok);
                nk_label(context, "Transport: Ethernet / DDSP",
                         NK_TEXT_LEFT);
                nk_label(context, "Processor: Red Pitaya ARM",
                         NK_TEXT_LEFT);
                nk_group_end(context);
            }
            if (nk_group_begin(context, "HEMODYNAMIC MEASUREMENTS",
                               NK_WINDOW_BORDER | NK_WINDOW_NO_SCROLLBAR)) {
                nk_layout_row_dynamic(context, 29, 1);
                nk_labelf(context, NK_TEXT_LEFT, "IVUS lumen area: %.3f mm^2",
                          ivus_area_milli / 1000.0);
                nk_labelf(context, NK_TEXT_LEFT, "LVOT diameter: %.3f cm",
                          diameter_cm);
                nk_labelf(context, NK_TEXT_LEFT, "LVOT area: %.3f cm^2",
                          area_cm2);
                nk_labelf(context, NK_TEXT_LEFT, "Heart rate: %.1f bpm",
                          heart_rate_bpm);
                nk_labelf(context, NK_TEXT_LEFT, "VTI: %.3f cm", vti_cm);
                nk_label(context, "Mode: real-time playback",
                         NK_TEXT_LEFT);
                nk_label(context, "Dataset: EchoXFlow LVOT",
                         NK_TEXT_LEFT);
                nk_group_end(context);
            }
            if (nk_group_begin(context, "CARDIAC OUTPUT",
                               NK_WINDOW_BORDER | NK_WINDOW_NO_SCROLLBAR)) {
                nk_layout_row_dynamic(context, 27, 1);
                nk_label(context, "CO = Area x VTI x HR / 1000",
                         NK_TEXT_CENTERED);
                nk_labelf(context, NK_TEXT_CENTERED,
                          "%.3f x %.3f x %.1f",
                          area_cm2, vti_cm, heart_rate_bpm);
                nk_layout_row_dynamic(context, 35, 1);
                nk_labelf(context, NK_TEXT_CENTERED, "SV  %.2f mL",
                          calculated_sv_ml);
                nk_layout_row_dynamic(context, 62, 1);
                if (metric_font)
                    nk_style_push_font(context, &metric_font->handle);
                nk_style_push_color(context, &context->style.text.color,
                                    nk_rgb(91, 222, 158));
                nk_labelf(context, NK_TEXT_CENTERED, "%.3f L/min",
                          calculated_co_lpm);
                nk_style_pop_color(context);
                if (metric_font)
                    nk_style_pop_font(context);
                nk_layout_row_dynamic(context, 24, 1);
                nk_label(context, "CALCULATED CARDIAC OUTPUT",
                         NK_TEXT_CENTERED);
                nk_group_end(context);
            }

            nk_layout_row_dynamic(context, 36, 1);
            nk_label(context,
                     "RETROSPECTIVE DATA PLAYBACK  |  RESEARCH PROTOTYPE  |  "
                     "NOT FOR CLINICAL USE",
                     NK_TEXT_CENTERED);
        }
        nk_end(context);

        SDL_RenderClear(renderer);
        nk_sdl_render(context, NK_ANTI_ALIASING_ON);
        SDL_RenderPresent(renderer);
        {
            uint64_t elapsed = SDL_GetTicks() - frame_start;
            uint64_t target = 1000u / display_fps;
            if (elapsed < target)
                SDL_Delay((Uint32)(target - elapsed));
        }
    }

    ivus.stop = 1;
    doppler.stop = 1;
    pthread_join(ivus_thread_id, NULL);
    pthread_join(doppler_thread_id, NULL);
    if (ivus_texture)
        SDL_DestroyTexture(ivus_texture);
    if (doppler_texture)
        SDL_DestroyTexture(doppler_texture);
    if (placeholder)
        SDL_DestroyTexture(placeholder);
    nk_sdl_shutdown(context);
    SDL_DestroyRenderer(renderer);
    SDL_DestroyWindow(window);
    SDL_Quit();
    pthread_mutex_destroy(&ivus.mutex);
    pthread_mutex_destroy(&doppler.mutex);
    free(ivus.published_pixels);
    free(doppler.history);
    free(ivus_gray);
    free(ivus_rgb);
    free(doppler_history);
    free(doppler_rgb);
    return 0;
}
