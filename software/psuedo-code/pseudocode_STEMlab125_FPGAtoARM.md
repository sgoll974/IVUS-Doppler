### Level 1: Broad Architecture Overview

At the top level, the handoff uses an asymmetric producer-consumer model over a shared memory bus (typically AXI4 via DMA). The FPGA acts as the bus master writing acoustic data directly into circular RAM buffers, and notifies the ARM core via hardware interrupts.

```pseudocode
// FPGA Hardware Domain (Hardware Producer)
PROCESS FPGA_Stream_Engine:
    LOOP:
        WAIT FOR BMode_Vector_Ready
        Pack_Vector_With_Metadata(Header, Samples)
        Push_To_AXI_Stream_FIFO()
        IF Buffer_Boundary_Reached THEN
            Trigger_AXI_DMA_Write_To_RAM()
            Assert_IRQ_Line_To_ARM()
        END IF
    END LOOP
END PROCESS

// ARM Processor Domain (Software Consumer)
PROCESS ARM_Driver_Engine:
    LOOP:
        WAIT FOR ARM_IRQ_Received()
        bufferDescriptor = Acknowledge_And_Fetch_IRQ_Details()
        Clean_Invalidate_D_Cache(bufferDescriptor.Address, bufferDescriptor.Length)
        Dispatch_Buffer_To_Application(bufferDescriptor)
        Requeue_Buffer_Descriptor_To_DMA_Ring()
    END LOOP
END PROCESS

```

---

### Level 2: Detailed Subsystem Logic

This level details the AXI DMA scatter-gather transaction, ring buffer synchronization, and ARM kernel-space/user-space cache coherence handoff.

#### 1. FPGA Side (AXI4-Stream to AXI4-MM DMA Master)

```pseudocode
MODULE FPGA_AXI_Streaming_Engine:
    INPUTS: 
        rx_sample_clk, raw_bmode_valid, raw_bmode_data[15:0],
        frame_sync, line_idx[5:0]
    OUTPUTS: 
        m_axi_s2mm_tdata[31:0], m_axi_s2mm_tvalid, m_axi_s2mm_tlast, irq_out

    INTERNAL_STATE:
        sample_counter = 0
        current_ring_slot = 0
        fifo = ASYNC_FIFO(DEPTH = 2048)

    // A. Packet Formatting & Header Injection
    ALWAYS @(posedge rx_sample_clk):
        IF frame_sync == HIGH AND line_idx == 0 THEN
            sample_counter <= 0
            // Inject 64-bit metadata header at frame start: [Magic(16b) | FrameID(16b) | Timestamp(32b)]
            fifo.PUSH(PACK_HEADER(0x55AA, current_frame_id, system_timer_ticks))
        END IF

        IF raw_bmode_valid THEN
            // Pack two 16-bit samples into 32-bit AXI word
            fifo.PUSH(raw_bmode_data)
            sample_counter <= sample_counter + 1
        END IF

        // End of single A-line / vector
        IF sample_counter == SAMPLES_PER_VECTOR THEN
            fifo.SET_TLAST()
            sample_counter <= 0
        END IF
    END ALWAYS

    // B. DMA Master Controller (Push to DDR via AXI HP/HPC Port)
    ALWAYS @(posedge aclk):
        IF fifo.COUNT >= DMA_BURST_SIZE OR fifo.HAS_TLAST THEN
            m_axi_s2mm_tvalid <= 1
            m_axi_s2mm_tdata  <= fifo.POP()
        ELSE
            m_axi_s2mm_tvalid <= 0
        END IF

        // Trigger IRQ upon completing full vector set (frame boundary)
        IF frame_completed THEN
            irq_out <= 1
        ELSE
            irq_out <= 0
        END IF
    END ALWAYS
END MODULE

```

#### 2. ARM Side (Driver & Buffer Management Pipeline)

```pseudocode
// Pre-allocated Circular Ring Buffer Structure
STRUCT DMABuffer:
    phys_addr: UINT32
    virt_addr: POINTER
    size_bytes: UINT32
    status: ENUM { EMPTY, FILLING, READY_FOR_ARM, PROCESSING }
END STRUCT

GLOBAL ring_buffers[RING_SIZE]: DMABuffer
GLOBAL head_idx = 0
GLOBAL tail_idx = 0

// A. Interrupt Service Routine (ISR / Top-Half)
FUNCTION On_FPGA_DMA_Interrupt():
    irq_status = READ_REG(AXI_DMA_BASE + S2MM_DMASR)
    
    // Clear Interrupt status flag
    WRITE_REG(AXI_DMA_BASE + S2MM_DMASR, irq_status | 0x1000)

    IF irq_status CONTAINS IOC_IRQ (Interrupt On Complete):
        ring_buffers[head_idx].status = READY_FOR_ARM
        head_idx = (head_idx + 1) MOD RING_SIZE
        
        // Wake up worker thread (Bottom-Half / Event Dispatcher)
        NOTIFY_WAITING_THREAD(dma_worker_thread)
    END IF
END FUNCTION

// B. Worker Thread (Bottom-Half / Consumer Pipeline)
FUNCTION DMA_Worker_Thread_Loop():
    WHILE Running:
        WAIT_FOR_NOTIFICATION()

        WHILE ring_buffers[tail_idx].status == READY_FOR_ARM:
            current_buf = ring_buffers[tail_idx]
            current_buf.status = PROCESSING

            // 1. Enforce Cache Coherency (Crucial for non-ACP ports)
            // Invalidate L1/L2 D-cache so ARM reads fresh DDR data written by FPGA DMA
            CPU_DCACHE_INVALIDATE(current_buf.virt_addr, current_buf.size_bytes)

            // 2. Validate Frame Header
            header = PARSE_HEADER(current_buf.virt_addr)
            IF header.magic != 0x55AA THEN
                LOG_ERROR("Frame desync: corrupt header")
                RECOVER_DMA_ENGINE()
                CONTINUE
            END IF

            // 3. Zero-Copy Pointer Forwarding to Processing Pipeline
            framePayload = current_buf.virt_addr + HEADER_OFFSET
            SubmitToDSPPipeline(framePayload, header.frame_id)

            // 4. Recycle Buffer back to FPGA DMA Engine
            current_buf.status = EMPTY
            REARM_DMA_DESCRIPTOR(current_buf.phys_addr)
            tail_idx = (tail_idx + 1) MOD RING_SIZE
        END WHILE
    END WHILE
END FUNCTION

```
