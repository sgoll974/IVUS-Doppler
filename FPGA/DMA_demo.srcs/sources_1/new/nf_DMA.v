`timescale 1ns / 1ps

module ivus_dma_subsystem (
    // Transducer / ADC Domain
    input  wire        rx_clk,
    input  wire        rst_n_rx,
    input  wire        sample_valid,
    input  wire [15:0] sample_data,
    input  wire        frame_sync,

    // Processing System (PS) AXI Clock Domain
    input  wire        axi_aclk,
    input  wire        axi_aresetn,
    input  wire [31:0] base_address,
    input  wire [31:0] slot_size_bytes,
    input  wire        enable_dma,

    // AXI HP0 Interconnect (Full Master)
    output wire [31:0] m_axi_hp_awaddr,
    output wire [7:0]  m_axi_hp_awlen,
    output wire [2:0]  m_axi_hp_awsize,
    output wire [1:0]  m_axi_hp_awburst,
    output wire        m_axi_hp_awvalid,
    input  wire        m_axi_hp_awready,
    output wire [63:0] m_axi_hp_wdata,
    output wire [7:0]  m_axi_hp_wstrb,
    output wire        m_axi_hp_wlast,
    output wire        m_axi_hp_wvalid,
    input  wire        m_axi_hp_wready,
    input  wire [1:0]  m_axi_hp_bresp,
    input  wire        m_axi_hp_bvalid,
    output wire        m_axi_hp_bready,

    // PS Interrupt Line
    output wire        irq_f2p,
    input  wire        irq_ack
);

    // Free-running timestamp counter
    reg [31:0] timestamp_counter;
    always @(posedge rx_clk or negedge rst_n_rx) begin
        if (!rst_n_rx) timestamp_counter <= 32'd0;
        else           timestamp_counter <= timestamp_counter + 1'b1;
    end

    // Packetizer -> FIFO AXI-Stream signals
    wire [63:0] pkt_tdata;
    wire        pkt_tvalid;
    wire        pkt_tlast;
    wire        pkt_tuser;
    wire        pkt_tready;

    acoustic_packetizer #(
        .SAMPLES_PER_LINE(512),
        .LINES_PER_FRAME(64)
    ) u_packetizer (
        .rx_clk        (rx_clk),
        .rst_n         (rst_n_rx),
        .sample_valid  (sample_valid),
        .sample_data   (sample_data),
        .frame_sync    (frame_sync),
        .sys_timestamp (timestamp_counter),
        .m_axis_tdata  (pkt_tdata),
        .m_axis_tvalid (pkt_tvalid),
        .m_axis_tlast  (pkt_tlast),
        .m_axis_tuser  (pkt_tuser),
        .m_axis_tready (pkt_tready)
    );

    // CDC FIFO -> AXI HP Master signals
    wire [63:0] fifo_tdata;
    wire        fifo_tvalid;
    wire        fifo_tlast;
    wire        fifo_tready;

    // Asynchronous AXI-Stream Dual-Clock FIFO using XPM Macro
    xpm_fifo_axis #(
        .CLOCKING_MODE     ("independent_clock"),
        .FIFO_DEPTH        (1024), // Uses ~2 BRAMs (36Kb) on Zynq-7010
        .TDATA_WIDTH       (64),
        .FIFO_MEMORY_TYPE  ("block")
    ) u_cdc_fifo (
        .s_aclk          (rx_clk),
        .s_aresetn       (rst_n_rx),
        .s_axis_tdata    (pkt_tdata),
        .s_axis_tvalid   (pkt_tvalid),
        .s_axis_tlast    (pkt_tlast),
        .s_axis_tready   (pkt_tready),
        .s_axis_tstrb    (8'hFF),
        .s_axis_tkeep    (8'hFF),
        .s_axis_tuser    (pkt_tuser),

        .m_aclk          (axi_aclk),
        .m_axis_tdata    (fifo_tdata),
        .m_axis_tvalid   (fifo_tvalid),
        .m_axis_tlast    (fifo_tlast),
        .m_axis_tready   (fifo_tready)
    );

    // AXI HP Master Engine
    axis_to_axi_hp_master #(
        .AXI_ADDR_WIDTH  (32),
        .AXI_DATA_WIDTH  (64),
        .BURST_LEN       (16),
        .NUM_SLOTS       (4)
    ) u_axi_master (
        .aclk            (axi_aclk),
        .aresetn         (axi_aresetn),
        .base_address    (base_address),
        .slot_size_bytes (slot_size_bytes),
        .enable_writer   (enable_dma),
        .s_axis_tdata    (fifo_tdata),
        .s_axis_tvalid   (fifo_tvalid),
        .s_axis_tready   (fifo_tready),
        .s_axis_tlast    (fifo_tlast),
        .m_axi_awaddr    (m_axi_hp_awaddr),
        .m_axi_awlen     (m_axi_hp_awlen),
        .m_axi_awsize    (m_axi_hp_awsize),
        .m_axi_awburst   (m_axi_hp_awburst),
        .m_axi_awvalid   (m_axi_hp_awvalid),
        .m_axi_awready   (m_axi_hp_awready),
        .m_axi_wdata     (m_axi_hp_wdata),
        .m_axi_wstrb     (m_axi_hp_wstrb),
        .m_axi_wlast     (m_axi_hp_wlast),
        .m_axi_wvalid    (m_axi_hp_wvalid),
        .m_axi_wready    (m_axi_hp_wready),
        .m_axi_bresp     (m_axi_hp_bresp),
        .m_axi_bvalid    (m_axi_hp_bvalid),
        .m_axi_bready    (m_axi_hp_bready),
        .irq_frame_done  (irq_f2p),
        .irq_ack         (irq_ack)
    );

endmodule