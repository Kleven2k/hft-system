"""
test_uart_rx_config.py — cocotb tests for uart_rx_config

Sends UART 8N1 frames at BAUD_DIV=40 cycles/bit and checks
that wr_en/wr_slot/wr_base fire correctly.

Frame format: { 0xA0|slot, base[31:24], base[23:16], base[15:8], base[7:0] }
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

BAUD_DIV = 40   # must match wrapper parameter


async def uart_send_byte(dut, byte_val: int):
    """Drive one UART 8N1 byte onto dut.uart_rx."""
    # Start bit
    dut.uart_rx.value = 0
    for _ in range(BAUD_DIV):
        await RisingEdge(dut.clk)
    # 8 data bits, LSB first
    for i in range(8):
        dut.uart_rx.value = (byte_val >> i) & 1
        for _ in range(BAUD_DIV):
            await RisingEdge(dut.clk)
    # Stop bit
    dut.uart_rx.value = 1
    for _ in range(BAUD_DIV):
        await RisingEdge(dut.clk)


async def uart_send_frame(dut, slot: int, base: int):
    """Send one 5-byte price_base config frame."""
    frame = [
        0xA0 | slot,
        (base >> 24) & 0xFF,
        (base >> 16) & 0xFF,
        (base >>  8) & 0xFF,
        (base      ) & 0xFF,
    ]
    for b in frame:
        await uart_send_byte(dut, b)


async def reset(dut):
    dut.rst.value    = 1
    dut.uart_rx.value = 1      # idle high
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


# ── Helpers to capture a wr_en pulse ────────────────────────

async def capture_write(dut, timeout_cycles=5000):
    """Wait up to timeout_cycles for a wr_en=1 pulse; return (slot, base) or None."""
    for _ in range(timeout_cycles):
        await RisingEdge(dut.clk)
        if dut.wr_en.value == 1:
            return int(dut.wr_slot.value), int(dut.wr_base.value)
    return None


# ── Tests ────────────────────────────────────────────────────

@cocotb.test()
async def test_single_frame(dut):
    """Send one frame for slot 0, base=1_000_000; verify wr_en fires."""
    cocotb.start_soon(Clock(dut.clk, 8, units="ns").start())
    await reset(dut)

    TARGET_SLOT = 0
    TARGET_BASE = 1_000_000

    cocotb.start_soon(uart_send_frame(dut, TARGET_SLOT, TARGET_BASE))
    result = await capture_write(dut)

    assert result is not None, "Timed out waiting for wr_en — UART parser stalled"
    slot, base = result
    assert slot == TARGET_SLOT, f"wr_slot wrong: got {slot}, expected {TARGET_SLOT}"
    assert base == TARGET_BASE, f"wr_base wrong: got {base:#010x}, expected {TARGET_BASE:#010x}"
    dut._log.info(f"PASS  slot={slot} base={base:#010x} (${base/10000:.4f})")


@cocotb.test()
async def test_all_slots(dut):
    """Send frames for all 4 slots and verify each write."""
    cocotb.start_soon(Clock(dut.clk, 8, units="ns").start())
    await reset(dut)

    cases = [
        (0, 1_000_000),
        (1, 2_000_000),
        (2,   500_000),
        (3,   150_000),
    ]

    for exp_slot, exp_base in cases:
        cocotb.start_soon(uart_send_frame(dut, exp_slot, exp_base))
        result = await capture_write(dut)
        assert result is not None, f"Timed out on slot {exp_slot}"
        slot, base = result
        assert slot == exp_slot, f"slot wrong: {slot} != {exp_slot}"
        assert base == exp_base,  f"base wrong: {base} != {exp_base}"
        dut._log.info(f"PASS  slot={slot} base={base:#010x}")
        # Idle gap between frames
        for _ in range(BAUD_DIV * 4):
            await RisingEdge(dut.clk)


@cocotb.test()
async def test_bad_sync_ignored(dut):
    """Send a byte with wrong sync nibble; frame must be discarded."""
    cocotb.start_soon(Clock(dut.clk, 8, units="ns").start())
    await reset(dut)

    # Send garbage sync byte (0xB0, not 0xA0–0xA3), then a valid frame
    bad_frame = [0xB0, 0x00, 0x0F, 0x42, 0x40]
    for b in bad_frame:
        await uart_send_byte(dut, b)

    # Idle gap
    for _ in range(BAUD_DIV * 4):
        await RisingEdge(dut.clk)

    # Now send a valid slot-0 frame
    TARGET_BASE = 999_999
    cocotb.start_soon(uart_send_frame(dut, 0, TARGET_BASE))
    result = await capture_write(dut)
    assert result is not None, "Timed out waiting for valid frame after bad sync"
    slot, base = result
    assert slot == 0 and base == TARGET_BASE, f"Unexpected write: slot={slot} base={base}"
    dut._log.info(f"PASS  bad sync discarded, valid frame slot={slot} base={base:#010x}")
