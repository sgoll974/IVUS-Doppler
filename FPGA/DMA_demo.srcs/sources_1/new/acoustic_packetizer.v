`timescale 1ns / 1ps

module acoustic_packetizer #(
    parameter SAMPLES_PER_LINE = 512, // e.g., 512 depth samples per vector
    parameter LINES_PER_FRAME  = 64   // 64 vectors for cylindrical array
)(
    input  wire        rx_clk,
    input  wire        rst_n,

    // Front-End Acoustic Interface
    input  wire        sample_valid,
    input  wire [15:0] sample_data,
    input  wire        frame_sync,    // Pulsed high on line 0 sample 0
    input  wire [31:0] sys_timestamp, // Free-running 32-bit timestamp counter

    // AXI4-Stream Master Output (to CDC FIFO)
    output reg  [63:0] m_axis_tdata,
    output reg         m_axis_tvalid,
    output reg         m_axis_tlast,
    output reg         m_axis_tuser,   // High on first beat of frame (header)
    input  wire        m_axis_tready
);

    localparam TOTAL_SAMPLES = SAMPLES_PER_LINE * LINES_PER_FRAME;

    reg [1:0]  pack_cnt;
    reg [47:0] sample_shift;
    reg [15:0] sample_counter;
    reg [15:0] frame_id;

    localparam ST_HEADER = 1'b0;
    localparam ST_DATA   = 1'b1;
    reg state;

    always @(posedge rx_clk or negedge rst_n) begin
        if (!rst_n) begin
            pack_cnt       <= 2'b00;
            sample_shift   <= 48'h0;
            sample_counter <= 16'd0;
            frame_id       <= 16'd0;
            m_axis_tdata   <= 64'h0;
            m_axis_tvalid  <= 1'b0;
            m_axis_tlast   <= 1'b0;
            m_axis_tuser   <= 1'b0;
            state          <= ST_DATA;
        end else begin
            // Default de-assertions
            if (m_axis_tready) begin
                m_axis_tvalid <= 1'b0;
                m_axis_tlast  <= 1'b0;
                m_axis_tuser  <= 1'b0;
            end

            // Frame synchronization reset
            if (frame_sync) begin
                pack_cnt       <= 2'b00;
                sample_counter <= 16'd0;
                frame_id       <= frame_id + 1'b1;
                
                // Inject 64-bit metadata header: [Magic(16b) | FrameID(16b) | Timestamp(32b)]
                m_axis_tdata   <= {16'h55AA, frame_id, sys_timestamp};
                m_axis_tvalid  <= 1'b1;
                m_axis_tuser   <= 1'b1;
                state          <= ST_DATA;
            end 
            else if (sample_valid) begin
                sample_counter <= sample_counter + 1'b1;
                
                case (pack_cnt)
                    2'b00: begin
                        sample_shift[15:0] <= sample_data;
                        pack_cnt <= 2'b01;
                    end
                    2'b01: begin
                        sample_shift[31:16] <= sample_data;
                        pack_cnt <= 2'b10;
                    end
                    2'b10: begin
                        sample_shift[47:32] <= sample_data;
                        pack_cnt <= 2'b11;
                    end
                    2'b11: begin
                        // Emit 64-bit word (4x 16-bit samples)
                        m_axis_tdata  <= {sample_data, sample_shift[47:0]};
                        m_axis_tvalid <= 1'b1;
                        pack_cnt      <= 2'b00;

                        // Check frame boundary
                        if (sample_counter == (TOTAL_SAMPLES - 1)) begin
                            m_axis_tlast   <= 1'b1;
                            sample_counter <= 16'd0;
                        end
                    end
                endcase
            end
        end
    end

endmodule