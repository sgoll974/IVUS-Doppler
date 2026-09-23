clear;
clc;
close all;

%% ============================================================
%  DOPPLER DYNAMICS - ETHERNET SIMULATION PARAMETERS
%  ============================================================

%% STEMlab & Ethernet BPS
% STEMlab has 500 Mb/s limit
% Ethernet hardware is 1Gb/s
LINK_RATE_BPS = 1e9;

%% Our UDP application protocol
%custom application-layer packet format built on top of UDP to
%identify, sequence, and reconstruct IVUS and Doppler data.”
%1500-byte IP MTU
SENSOR_PAYLOAD_BYTES = 1400;
DDSP_HEADER_BYTES = 32;

%% IPv4 / UDP overhead
UDP_HEADER_BYTES = 8;
IPV4_HEADER_BYTES = 20;

%% Ethernet overhead
% All standard based on IEEE 802.3 ethernet behavior
ETHERNET_HEADER_BYTES = 14;
FCS_BYTES = 4;
PREAMBLE_SFD_BYTES = 8;
IFG_BYTE_TIMES = 12;

%% Total non-sensor overhead per UDP packet
OVERHEAD_BYTE_TIMES = ...
    DDSP_HEADER_BYTES + ...
    UDP_HEADER_BYTES + ...
    IPV4_HEADER_BYTES + ...
    ETHERNET_HEADER_BYTES + ...
    FCS_BYTES + ...
    PREAMBLE_SFD_BYTES + ...
    IFG_BYTE_TIMES;

%% Full-size packet on-wire size
FULL_WIRE_BYTES = ...
    SENSOR_PAYLOAD_BYTES + OVERHEAD_BYTE_TIMES;

FULL_TX_TIME_S = ...
    FULL_WIRE_BYTES * 8 / LINK_RATE_BPS;

%% DMA test block
DMA_WORDS = 4096;
BYTES_PER_WORD = 4;
DMA_BLOCK_BYTES = DMA_WORDS * BYTES_PER_WORD;

PACKETS_PER_DMA_BLOCK = ...
    ceil(DMA_BLOCK_BYTES / SENSOR_PAYLOAD_BYTES);

LAST_PAYLOAD_BYTES = ...
    DMA_BLOCK_BYTES - ...
    SENSOR_PAYLOAD_BYTES * (PACKETS_PER_DMA_BLOCK - 1);

%helps us identify a packet-generation rate from data rate
%for example, link data rate = 70Mb/s so 1365.33×8=10922.67 bits,
%70/ 10922 = 6408 packets/s 1/6408= 156us avg pd btw packets

AVERAGE_PAYLOAD_BYTES = ...
    DMA_BLOCK_BYTES / PACKETS_PER_DMA_BLOCK;

%% Cable
CABLE_LENGTH_M = 2;
PROPAGATION_SPEED_MPS = 2e8;
PROPAGATION_DELAY_S = ...
    CABLE_LENGTH_M / PROPAGATION_SPEED_MPS;

%% ------------------------------------------------------------
%  TEST TRAFFIC LOADS
%
%  IMPORTANT:
%  These are engineering TEST CONDITIONS,
%  not claimed IVUS or Doppler specifications.
%  ------------------------------------------------------------
% Preliminary estimated PROCESSED IVUS network data rate.
% Not a manufacturer-specified catheter interface rate.
IVUS_APP_RATE_BPS = 70e6;
DOPPLER_APP_RATE_BPS = 20e6;

IVUS_PACKET_PERIOD_S = ...
    AVERAGE_PAYLOAD_BYTES * 8 / IVUS_APP_RATE_BPS;

DOPPLER_PACKET_PERIOD_S = ...
    AVERAGE_PAYLOAD_BYTES * 8 / DOPPLER_APP_RATE_BPS;

%% Buffer capacities
TX_QUEUE_CAPACITY = 256;
RX_QUEUE_CAPACITY = 256;

%% Raspberry Pi processing 
% Placeholder for sensitivity testing.
% This is NOT a measured Raspberry Pi value.
% Must be replaced by measured value during hardware testing.
PI_PROCESS_TIME_S = 5e-6;

%% Packet error probability
% Start with zero for a clean direct Ethernet cable.
PACKET_ERROR_RATE = 0;

%% Simulation
SIM_TIME_S = 0.1;

%% Repeatable randomness 
% not really useful now but will be useful once we start injecting packet loss, 
% jitter, or other random behavior
rng(7);

%% Printing useful values
fprintf('Packets per DMA block: %d\n', PACKETS_PER_DMA_BLOCK);
fprintf('Last packet payload: %.0f bytes\n', LAST_PAYLOAD_BYTES);
fprintf('Full Ethernet wire size: %.0f byte-times\n', FULL_WIRE_BYTES);
fprintf('Full packet serialization time: %.3f us\n', ...
    FULL_TX_TIME_S * 1e6);
fprintf('Cable propagation delay: %.3f ns\n', ...
    PROPAGATION_DELAY_S * 1e9);