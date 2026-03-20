"""
set_risk.py — Send Phase 16 pre-trade risk commands via UART.

Usage:
    python set_risk.py kill      # send 0xB0: halt all new orders
    python set_risk.py resume    # send 0xB1: resume order emission
    python set_risk.py status    # just read back echo to confirm UART alive

UART frame (Phase 16 kill switch):
    0xB0 = kill ON
    0xB1 = kill OFF
"""
import sys
import serial
import time

PORT    = "COM5"
BAUD    = 115_200
TIMEOUT = 2.0

KILL_ON  = bytes([0xB0])
KILL_OFF = bytes([0xB1])


def send_and_echo(ser, payload, label):
    ser.write(payload)
    time.sleep(0.05)
    echo = ser.read(len(payload))
    if echo == payload:
        print(f"{label}: sent {payload.hex().upper()} — echo OK")
    else:
        print(f"{label}: sent {payload.hex().upper()} — "
              f"echo mismatch (got {echo.hex().upper() if echo else 'nothing'})")


def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "status"

    with serial.Serial(PORT, BAUD, timeout=TIMEOUT) as ser:
        ser.reset_input_buffer()

        if cmd == "kill":
            send_and_echo(ser, KILL_ON, "KILL ON  (0xB0)")
            print("Kill switch ARMED — FPGA will not emit new orders.")

        elif cmd == "resume":
            send_and_echo(ser, KILL_OFF, "KILL OFF (0xB1)")
            print("Kill switch CLEARED — FPGA will resume quoting.")

        elif cmd == "status":
            # Send a harmless byte that the UART parser ignores (0xFF)
            # and check the loopback echo to confirm UART is alive.
            probe = bytes([0xFF])
            ser.write(probe)
            time.sleep(0.05)
            echo = ser.read(1)
            if echo == probe:
                print("UART loopback OK — FPGA is alive.")
            else:
                print("No echo — check cable and COM port.")

        else:
            print(f"Unknown command '{cmd}'. Use: kill | resume | status")
            sys.exit(1)


if __name__ == "__main__":
    main()
