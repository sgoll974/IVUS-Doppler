/*
 * Module: ivus_doppler_frontend_pl
 * Description: PL-side logic for the 64-element Visions PV .035 and Parks Medical skinny pencil probe DOPPLER
 * Handles nanosecond timing, DSP, and reverse-engineering diagnostic routing.
 * Target: Xilinx Zynq-7010 (STEMlab 125-14)
 * Course: UTSA ECE 4812 (Design 1)
 * Team 1 - Doppler Dynamics
 */

module ivus_frontend_pl #(
    parameter PROBE_FREQ_MHZ = 8 // Configurable: Parks Medical skinny pencil probe frequency (MHz)
) (
    input  wire        clk_125mhz,      
    input  wire        rst_n,           

    // --- AFE & Catheter Interfaces ---
    input  wire [13:0] adc_ch1_in,      // Volcano IVUS (B-Mode)
    input  wire [13:0] adc_ch2_in,      // ADDED: Parks Medical Doppler Probe
    output wire        tx_pulser_trig,  
    output wire        rx_enable,       
    
    // Volcano ASIC Physical Pins
    inout  wire        asic_cmd_pin,
    inout  wire        asic_clk_pin,

    // --- ARM PS Memory Mapped Control (AXI-Lite) ---
    input  wire [31:0] reg_control,     // Bit 0: Enable, Bit 1: Sniff Mode (1) vs Drive Mode (0)
    input  wire [31:0] reg_prf_timer,   // Configurable PRF delay

    // --- Diagnostic Hooks (To STEMlab Logic Analyzer IPs) ---
    output wire        la_probe_cmd,
    output wire        la_probe_clk,

    // --- AXI-Stream 1: B-Mode to ARM PS (DMA 0) ---
    output wire [31:0] m_axis_tdata,    // [31:16] Element ID & Flags, [15:0] Envelope Mag
    output wire        m_axis_tvalid,   
    input  wire        m_axis_tready,   
    output wire        m_axis_tlast,    // Asserted at end of scanline

    // --- AXI-Stream 2: Doppler to ARM PS (DMA 1) ---
    output wire [31:0] m_axis_doppler_tdata,  // [31:16] I-Channel, [15:0] Q-Channel
    output wire        m_axis_doppler_tvalid, 
    input  wire        m_axis_doppler_tready, 
    output wire        m_axis_doppler_tlast   
);

    // =========================================================================
    // 1. REVERSE-ENGINEERING MULTIPLEXER (SNIFF VS DRIVE)
    // =========================================================================
    wire sniff_mode = reg_control[1];
    reg  fpga_driven_cmd;
    reg  fpga_driven_clk;

    // In Sniff mode, pins are High-Z (inputs). In Drive mode, FPGA drives them.
    assign asic_cmd_pin = sniff_mode ? 1'bZ : fpga_driven_cmd;
    assign asic_clk_pin = sniff_mode ? 1'bZ : fpga_driven_clk;

    // Always route the physical pins to the Logic Analyzer IP for debugging
    assign la_probe_cmd = asic_cmd_pin;
    assign la_probe_clk = asic_clk_pin;

    // =========================================================================
    // 2. TIMING & ACQUISITION STATE MACHINE
    // =========================================================================
    reg [2:0]  state;
    reg [5:0]  element_counter; // 0 to 63
    reg [15:0] sample_counter;  // Depth tracking
    
    localparam STATE_IDLE      = 3'b000;
    localparam STATE_ASIC_PROG = 3'b001; // Shift element_counter to ASIC
    localparam STATE_TX_FIRE   = 3'b010; 
    localparam STATE_BLANK     = 3'b011; 
    localparam STATE_RX_ACQ    = 3'b100; 
    localparam STATE_DOPPLER   = 3'b101; // REPLACED: Doppler Acquisition Window
    localparam STATE_WAIT_PRF  = 3'b110; // ADDED: Final wait for PRF timer

    always @(posedge clk_125mhz) begin
        if (!rst_n) begin
            state <= STATE_IDLE;
            element_counter <= 0;
        end else if (reg_control[0] && !sniff_mode) begin
            // Active Scanning Logic
            case (state)
                STATE_IDLE: begin
                    state <= STATE_ASIC_PROG;
                end
                STATE_ASIC_PROG: begin
                    // Pseudo: Shift out `element_counter` via fpga_driven_cmd/clk
                    // Once shift is complete:
                    state <= STATE_TX_FIRE;
                end
                STATE_TX_FIRE: begin
                    // Assert tx_pulser_trig for exact duration
                    state <= STATE_BLANK;
                end
                STATE_BLANK: begin
                    // Wait for transducer ring-down
                    state <= STATE_RX_ACQ;
                end
                STATE_RX_ACQ: begin
                    // Enable pipeline, count to max depth, assert m_axis_tlast
                    if (sample_counter == MAX_DEPTH) begin
                        state <= STATE_DOPPLER;
                        element_counter <= element_counter + 1; // Move to next element
                        // Reset counters for Doppler...
                    end
                end
                STATE_DOPPLER: begin
                    // Interleaved Mode: Fire Parks Medical skinny pencil probe and mix echoes 
                    // isolated from the IVUS transmit pulse.
                    if (sample_counter == DOPPLER_MAX_DEPTH) begin
                        state <= STATE_WAIT_PRF;
                    end
                end
                STATE_WAIT_PRF: begin
                    // Wait for PRF timer to elapse
                    // if (prf_timer_done) begin
                    //     state <= STATE_ASIC_PROG;
                    // end
                end
            endcase
        end
    end

    // =========================================================================
    // 3. DSP PIPELINE (MIXER -> CIC -> CORDIC)
    // =========================================================================
    // --- PATH A: IVUS B-MODE (ADC CH 1) ---
    wire [15:0] envelope_mag;
    wire        envelope_valid;
    // [Instantiate B-Mode DDC, CIC, and CORDIC IPs here]

    // --- PATH B: PARKS MEDICAL DOPPLER (ADC CH 2) ---
    // The Parks Medical probe needs I/Q demodulation to determine directional blood flow.
    wire signed [15:0] nco_cos;
    wire signed [15:0] nco_sin;
    
    // Placeholder: Xilinx DDS Compiler tuned to PROBE_FREQ_MHZ
    // dds_compiler_freq_mhz nco_inst (...);

    reg signed [29:0] mixer_i, mixer_q;
    always @(posedge clk_125mhz) begin
        // MODIFIED: Interleaved mixing. Only process Parks Medical data during 
        // the isolated Doppler window to prevent IVUS cross-talk.
        if (state == STATE_DOPPLER) begin
            mixer_i <= $signed(adc_ch2_in) * nco_cos;
            mixer_q <= $signed(adc_ch2_in) * nco_sin;
        end else begin
            mixer_i <= 0;
            mixer_q <= 0;
        end
    end

    // Placeholder: CIC Filter to decimate the 125MHz mixed signal down to 
    // an audio-rate sample frequency (e.g., 48 kHz) for the RPi 5.
    wire signed [15:0] doppler_i_decimated;
    wire signed [15:0] doppler_q_decimated;
    wire               doppler_valid;
    // cic_compiler_doppler cic_inst (...);

    // =========================================================================
    // 4. AXI STREAM FORMATTING
    // =========================================================================
    // AXI Stream 1: IVUS B-Mode
    assign m_axis_tdata  = {10'b0, element_counter, envelope_mag};
    assign m_axis_tvalid = envelope_valid;
    assign m_axis_tlast  = (sample_counter == MAX_DEPTH); 

    // AXI Stream 2: Parks Medical Doppler
    assign m_axis_doppler_tdata  = {doppler_i_decimated, doppler_q_decimated};
    assign m_axis_doppler_tvalid = doppler_valid;
    // Doppler is a continuous stream, but we assert tlast periodically to chunk DMA transfers
    reg [9:0] doppler_chunk_counter;
    always @(posedge clk_125mhz) begin
        if (doppler_valid) doppler_chunk_counter <= doppler_chunk_counter + 1;
    end
    assign m_axis_doppler_tlast = (doppler_chunk_counter == 10'd1023);

endmodule
