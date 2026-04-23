/*
 * File: ivus_dma_udp_ps_v2.c
 * Description: MTU-Safe UDP Streamer. Handles both IVUS B-Mode (DMA0) 
 * and Arjo Doppler (DMA1) streams, chunking data to prevent IP fragmentation.
 * Course: UTSA ECE 4812 (Design 1)
 * Team 1 - Doppler Dynamics
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>

#define RPI_IP "[IP_ADDRESS]"
#define RPI_PORT 8080

// --- Engineering Specifications ---
// Standard Ethernet MTU is 1500 bytes. We restrict payload to 1000 bytes 
// to ensure safety margins for UDP/IP headers.
#define MAX_UDP_PAYLOAD_BYTES 1000 
#define SAMPLES_PER_PACKET    (MAX_UDP_PAYLOAD_BYTES / 4) // 250 samples (32-bit words)

// Packet Types
#define PKT_TYPE_IVUS_BMODE   0x01
#define PKT_TYPE_DOPPLER_IQ   0x02

struct UdpHeader {
    uint32_t frame_number;
    uint16_t packet_sequence; // For reassembly on the RPi 5
    uint8_t  packet_type;     // 0x01 = B-Mode, 0x02 = Doppler
    uint8_t  element_id;      // Only used if packet_type == B-Mode
};

int main() {
    int sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in rpi_addr;
    rpi_addr.sin_family = AF_INET;
    rpi_addr.sin_port = htons(RPI_PORT);
    inet_pton(AF_INET, RPI_IP, &rpi_addr.sin_addr);

    size_t packet_size = sizeof(struct UdpHeader) + MAX_UDP_PAYLOAD_BYTES;
    void *udp_buffer = malloc(packet_size);
    struct UdpHeader *header = (struct UdpHeader *)udp_buffer;
    uint32_t *payload = (uint32_t *)((uint8_t*)udp_buffer + sizeof(struct UdpHeader));

    uint32_t bmode_frame_count = 0;
    uint16_t bmode_seq_count = 0;

    printf("Starting Dual-Stream MTU-Safe UDP Bridge...\n");

    while (1) {
        // --- Pseudo-logic for polling DMA ---
        // int dma_ready = poll_dma_channels();
        // if (dma_ready == DMA0_IVUS) { ... }
        // else if (dma_ready == DMA1_DOPPLER) { ... }

        // Example: Processing a chunk of B-Mode IVUS data
        // Assume `dma_bmode_buffer` contains 250 samples pulled from FPGA
        uint32_t *dma_bmode_buffer; /* = get_dma_data() */
        
        uint8_t current_element = (dma_bmode_buffer[0] >> 16) & 0x3F;
        if (current_element == 0 && bmode_seq_count == 0) {
            bmode_frame_count++;
        }

        header->frame_number    = bmode_frame_count;
        header->packet_sequence = bmode_seq_count++;
        header->packet_type     = PKT_TYPE_IVUS_BMODE;
        header->element_id      = current_element;

        memcpy(payload, dma_bmode_buffer, MAX_UDP_PAYLOAD_BYTES);

        sendto(sockfd, udp_buffer, packet_size, 0, 
               (struct sockaddr *)&rpi_addr, sizeof(rpi_addr));

        // Reset sequence count if we hit the end of a scanline 
        // (Assuming 1000 samples/scanline, we send 4 packets of 250 samples)
        if (bmode_seq_count >= 4) {
            bmode_seq_count = 0;
        }

        // --- Doppler Processing would look identical, but with PKT_TYPE_DOPPLER_IQ ---
    }

    close(sockfd);
    free(udp_buffer);
    return 0;
}
