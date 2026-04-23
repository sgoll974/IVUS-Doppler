`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: Team IVUS (UTSA ECE 4812 - Design 1)
// Engineer: Lead Hardware Engineer
// 
// Create Date: 2026
// Module Name: top_blink
// Target Devices: STEMlab 125-14 (Zynq 7010 SoC)
// Tool Versions: Vivado 
// Description: "Proof of Life" module. Toggles the 8 onboard LEDs to verify
//              FPGA clocking, logic synthesis, and pin constraints.
//
// Best Practices Highlighted:
// - Parameterization for easy reuse and testing.
// - Synchronous, active-low reset (preferred for DSP/Zynq PL blocks).
// - Clear separation of sequential and combinational logic.
//////////////////////////////////////////////////////////////////////////////////

module top_blink #(
    // STEMlab 125-14 standard ADC clock is 125 MHz. 
    // We parameterize this so we can easily change it if we use a 50MHz PS clock later.
    parameter SYS_CLK_FREQ = 125_000_000 
)(
    input  wire       adc_clk_p_i, // 125 MHz Differential Clock (Positive)
    input  wire       adc_clk_n_i, // 125 MHz Differential Clock (Negative)
    input  wire       rst_n,       // Active-low reset (can map to a board button or tie high)
    output wire [7:0] led_o        // 8 onboard LEDs
);

    // -------------------------------------------------------------------------
    // Clock Buffering (Best Practice for Differential Clocks)
    // -------------------------------------------------------------------------
    // The STEMlab uses a differential clock from the ADC. We must use an 
    // IBUFDS (Input Buffer Differential Signaling) primitive to convert it 
    // into a single-ended clock for our logic.
    wire clk;
    
    IBUFDS i_clk_buf (
        .I (adc_clk_p_i),
        .IB(adc_clk_n_i),
        .O (clk)
    );

    // -------------------------------------------------------------------------
    // Internal Signals & Registers
    // -------------------------------------------------------------------------
    // Using $clog2 automatically calculates the required bit-width for the counter
    // based on the clock frequency, preventing wasted flip-flops.
    localparam MAX_COUNT = SYS_CLK_FREQ - 1;
    reg [$clog2(SYS_CLK_FREQ)-1:0] counter_reg;
    
    reg [7:0] led_reg;

    // -------------------------------------------------------------------------
    // Sequential Logic (Synchronous to Clock)
    // -------------------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            // Reset condition: Clear counter and LEDs
            counter_reg <= 0;
            led_reg     <= 8'b00000001; // Start with the first LED on
        end else begin
            if (counter_reg == MAX_COUNT) begin
                // One second has passed (at 125 MHz)
                counter_reg <= 0;
                
                // Shift the LED left. If it hits the end, wrap around.
                // This creates a "Knight Rider" or sweeping effect.
                led_reg <= {led_reg[6:0], led_reg[7]}; 
            end else begin
                // Increment counter
                counter_reg <= counter_reg + 1;
            end
        end
    end

    // -------------------------------------------------------------------------
    // Output Assignment
    // -------------------------------------------------------------------------
    assign led_o = led_reg;

endmodule