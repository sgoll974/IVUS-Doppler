`timescale 1ns / 1ps

module axis_to_axi_hp_master #(
    parameter AXI_ADDR_WIDTH = 32,
    parameter AXI_DATA_WIDTH = 64,
    parameter BURST_LEN      = 16, // 16 beats * 8 bytes = 128 bytes per burst
    parameter NUM_SLOTS      = 4   // Circular buffer depth
)(
    input  wire                      aclk,
    input  wire                      aresetn,

    // Base address in DDR (e.g. from udmabuf phys_addr) and frame size
    input  wire [AXI_ADDR_WIDTH-1:0] base_address,
    input  wire [31:0]               slot_size_bytes,
    input  wire                      enable_writer,

    // AXI4-Stream Slave Interface (from CDC FIFO)
    input  wire [AXI_DATA_WIDTH-1:0] s_axis_tdata,
    input  wire                      s_axis_tvalid,
    output wire                      s_axis_tready,
    input  wire                      s_axis_tlast,

    // AXI4-Full Master Write Channel (to Zynq S_AXI_HP0)
    output reg  [AXI_ADDR_WIDTH-1:0] m_axi_awaddr,
    output wire [7:0]                m_axi_awlen,
    output wire [2:0]                m_axi_awsize,
    output wire [1:0]                m_axi_awburst,
    output reg                       m_axi_awvalid,
    input  wire                      m_axi_awready,

    output wire [AXI_DATA_WIDTH-1:0] m_axi_wdata,
    output wire [7:0]                m_axi_wstrb,
    output reg                       m_axi_wlast,
    output wire                      m_axi_wvalid,
    input  wire                      m_axi_wready,

    input  wire [1:0]                m_axi_bresp,
    input  wire                      m_axi_bvalid,
    output wire                      m_axi_bready,

    // Interrupt to Processing System (GIC SPI)
    output reg                       irq_frame_done,
    input  wire                      irq_ack
);

    // Static AXI parameters
    assign m_axi_awlen   = BURST_LEN - 1; // 16 beats
    assign m_axi_awsize  = 3'b011;        // 8 bytes (64 bits)
    assign m_axi_awburst = 2'b01;         // INCR burst
    assign m_axi_wstrb   = 8'hFF;         // All bytes valid
    assign m_axi_bready  = 1'b1;

    // Buffer state tracking
    reg [1:0]  slot_idx;
    reg [AXI_ADDR_WIDTH-1:0] current_wr_addr;
    reg [7:0]  beat_cnt;

    localparam IDLE       = 3'd0;
    localparam AW_WAIT    = 3'd1;
    localparam WRITE_DATA = 3'd2;
    localparam B_WAIT     = 3'd3;
    reg [2:0] state;

    // Stream throttling: pass through tready only when actively driving W channel
    assign s_axis_tready = (state == WRITE_DATA) && m_axi_wready;
    assign m_axi_wvalid  = (state == WRITE_DATA) && s_axis_tvalid;
    assign m_axi_wdata   = s_axis_tdata;

    always @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            state           <= IDLE;
            m_axi_awvalid   <= 1'b0;
            m_axi_awaddr    <= 32'h0;
            m_axi_wlast     <= 1'b0;
            slot_idx        <= 2'd0;
            current_wr_addr <= 32'h0;
            beat_cnt        <= 8'd0;
            irq_frame_done  <= 1'b0;
        end else begin
            // Clear interrupt once acknowledged
            if (irq_ack) begin
                irq_frame_done <= 1'b0;
            end

            case (state)
                IDLE: begin
                    if (enable_writer && s_axis_tvalid) begin
                        m_axi_awaddr  <= base_address + (slot_idx * slot_size_bytes);
                        current_wr_addr <= base_address + (slot_idx * slot_size_bytes);
                        m_axi_awvalid <= 1'b1;
                        state         <= AW_WAIT;
                    end
                end

                AW_WAIT: begin
                    if (m_axi_awready && m_axi_awvalid) begin
                        m_axi_awvalid <= 1'b0;
                        beat_cnt      <= 8'd0;
                        state         <= WRITE_DATA;
                    end
                end

                WRITE_DATA: begin
                    // Assert wlast on the 16th beat (BURST_LEN - 1)
                    if (beat_cnt == (BURST_LEN - 2) && s_axis_tvalid && m_axi_wready) begin
                        m_axi_wlast <= 1'b1;
                    end

                    if (s_axis_tvalid && m_axi_wready) begin
                        beat_cnt <= beat_cnt + 1'b1;

                        if (m_axi_wlast) begin
                            m_axi_wlast <= 1'b0;
                            state       <= B_WAIT;
                        end

                        // Frame boundary reached
                        if (s_axis_tlast) begin
                            irq_frame_done <= 1'b1;
                            slot_idx       <= (slot_idx + 1'b1) % NUM_SLOTS;
                        end
                    end
                end

                B_WAIT: begin
                    if (m_axi_bvalid) begin
                        // Advance address by burst byte length (16 beats * 8 bytes = 128 bytes)
                        current_wr_addr <= current_wr_addr + (BURST_LEN * 8);

                        // If new data is ready in FIFO, launch next burst
                        if (s_axis_tvalid) begin
                            m_axi_awaddr  <= current_wr_addr + (BURST_LEN * 8);
                            m_axi_awvalid <= 1'b1;
                            state         <= AW_WAIT;
                        end else begin
                            state <= IDLE;
                        end
                    end
                end

                default: state <= IDLE;
            endcase
        end
    end

endmodule