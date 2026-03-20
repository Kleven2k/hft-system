"""
set_price_base.py — Configure price_base registers on the FPGA via UART

Frame format: { 0xA0|slot, base[31:24], base[23:16], base[15:8], base[7:0] }
  slot   : 0–3 (book slot)
  base   : price_base value in price units (price * 10000)

Examples:
  $100.00  → base = 1_000_000
  $200.00  → base = 2_000_000
  $50.00   → base =   500_000

Usage:
  python set_price_base.py          # set all 4 slots to example values
  python set_price_base.py COM5     # specify COM port explicitly
"""

import sys, struct, time

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)

# ---- Config ------------------------------------------------
PORT     = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD     = 115200

def send_price_base(ser, slot: int, base: int):
    """Send one 5-byte config frame and verify echo (uart_tx = uart_rx loopback)."""
    assert 0 <= slot <= 3, f"slot must be 0-3, got {slot}"
    assert 0 <= base <= 0xFFFFFFFF, "base out of range"
    sync_byte = 0xA0 | slot
    frame = bytes([sync_byte]) + struct.pack('>I', base)
    ser.reset_input_buffer()
    ser.write(frame)
    echo = ser.read(len(frame))   # timeout=1 s (set on Serial open)
    if echo == frame:
        status = "echo OK"
    elif len(echo) == 0:
        status = "NO ECHO — check pin/bitstream"
    else:
        status = f"echo MISMATCH: {echo.hex()}"
    price = base / 10000.0
    print(f"  slot {slot}: price_base = {base:>10,}  (${price:>10.4f})  [{status}]")

# ---- Main --------------------------------------------------
print(f"Opening {PORT} at {BAUD} baud...")
try:
    with serial.Serial(PORT, BAUD, timeout=1) as ser:
        time.sleep(0.1)   # allow USB-UART bridge to settle
        print("Sending price_base config:")

        # Edit these values to match your test symbols (price * 10000)
        send_price_base(ser, slot=0, base=1_000_000)   # slot 0: $100.00
        send_price_base(ser, slot=1, base=2_000_000)   # slot 1: $200.00
        send_price_base(ser, slot=2, base=  500_000)   # slot 2:  $50.00
        send_price_base(ser, slot=3, base=  150_000)   # slot 3:  $15.00

        print("Done.")

except serial.SerialException as e:
    print(f"ERROR: {e}")
    print(f"Available ports:")
    from serial.tools import list_ports
    for p in list_ports.comports():
        print(f"  {p.device}  {p.description}")
