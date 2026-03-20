import socket, struct, threading, time

MY_IP      = "192.168.1.11"
FPGA_IP    = "192.168.1.10"
OUCH_PORT  = 42000
ACK_PORT   = 42001

ACK_FILLED    = 0x00
ACK_PARTIAL   = 0x01
ACK_REJECTED  = 0x02
ACK_CANCELLED = 0x03

# ---- Mode selection -----------------------------------------------
# SKEW_TEST : immediate full fills — position accumulates and skewed
#             prices are verified against the PC-side shadow.
SKEW_TEST  = True
SKEW_SHIFT = 3      # must match hft_top.sv order_engine_inst SKEW_SHIFT=3
ORDER_QTY  = 100    # must match strategy ORDER_QTY

# Phase 21A: per-symbol market prices (must match test_itch.py + set_price_base.py).
# bid = price_base + 15,  ask = price_base + 20.
MARKET = {
    0: {"bid": 1_000_015, "ask": 1_000_020},  # slot 0: $100.00 base
    1: {"bid": 2_000_015, "ask": 2_000_020},  # slot 1: $200.00 base
    2: {"bid":   500_015, "ask":   500_020},  # slot 2:  $50.00 base
    3: {"bid":   150_015, "ask":   150_020},  # slot 3:  $15.00 base
}

ORDER_ACK_DELAY = 0.5   # seconds (used only when SKEW_TEST=False)
# -------------------------------------------------------------------

# PC-side position shadow per symbol (SKEW_TEST only)
_positions = {0: 0, 1: 0, 2: 0, 3: 0}
_pos_lock  = threading.Lock()

rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx_sock.bind((MY_IP, OUCH_PORT))
rx_sock.settimeout(10)

tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

def send_ack(order_id, status, fill_qty=0, delay=0.0):
    """Send a 13-byte OMS ACK (Phase 15 format), optionally after a delay."""
    if delay > 0:
        time.sleep(delay)
    ack = struct.pack('>QBI', order_id & 0xFFFFFFFFFFFFFFFF, status, fill_qty)
    tx_sock.sendto(ack, (FPGA_IP, ACK_PORT))

print(f"Listening for OUCH orders on UDP {OUCH_PORT}...")
if SKEW_TEST:
    print(f"SKEW_TEST mode — immediate fills, tracking inventory skew per symbol")
    print(f"  SKEW_SHIFT={SKEW_SHIFT}  ORDER_QTY={ORDER_QTY}")
    for sid, m in MARKET.items():
        print(f"  slot {sid}: BID=${m['bid']/10000:.4f}  ASK=${m['ask']/10000:.4f}")
else:
    print(f"Delayed-ACK mode ({ORDER_ACK_DELAY*1000:.0f} ms)")
print("(Run test_itch.py in another terminal first)\n")

orders  = 0
cancels = 0
order_lock = threading.Lock()

try:
    while True:
        data, addr = rx_sock.recvfrom(1024)
        if len(data) < 1:
            continue

        msg_type = data[0]

        if msg_type == 0x58 and len(data) >= 9:
            # ---- Cancel ('X') -----------------------------------------
            order_id, = struct.unpack('>q', data[1:9])
            with order_lock:
                cancels += 1
                c = cancels
            print(f"CANCEL #{c}  order_id={order_id}\n")
            threading.Thread(target=send_ack,
                             args=(order_id, ACK_CANCELLED, 0, 0.0),
                             daemon=True).start()

        elif msg_type == 0x4F and len(data) >= 20:
            # ---- New order ('O') --------------------------------------
            _, symbol_id, side, price, qty, order_id = struct.unpack('>BHBIIq', data[:20])
            with order_lock:
                orders += 1
                this_order = orders

            side_str = 'BUY' if side == 0x42 else 'SELL'

            if SKEW_TEST:
                with _pos_lock:
                    pos_before = _positions.get(symbol_id, 0)

                # inv_skew mirrors hardware: arithmetic right-shift.
                inv_skew = pos_before >> SKEW_SHIFT
                mkt = MARKET.get(symbol_id, {"bid": price, "ask": price})
                expected = (mkt["bid"] if side == 0x42 else mkt["ask"]) - inv_skew
                match_str = "OK" if price == expected else \
                            f"MISMATCH expected=${expected/10000:.4f}"

                print(f"ORDER #{this_order:>3d}  slot={symbol_id}  {side_str:4s}  "
                      f"price=${price/10000:.4f}  "
                      f"inv_skew={inv_skew:+d}  pos={pos_before:+d}  "
                      f"{match_str}")

                send_ack(order_id, ACK_FILLED, qty, 0.0)

                with _pos_lock:
                    if side == 0x42:
                        _positions[symbol_id] = _positions.get(symbol_id, 0) + qty
                    else:
                        _positions[symbol_id] = _positions.get(symbol_id, 0) - qty
                    pos_after = _positions[symbol_id]

                print(f"          → filled {qty}  slot {symbol_id} pos={pos_after:+d}\n")

            else:
                print(f"ORDER #{this_order} slot={symbol_id} {side_str} "
                      f"price=${price/10000:.4f} qty={qty} id={order_id}\n")
                threading.Thread(target=send_ack,
                                 args=(order_id, ACK_FILLED, qty, ORDER_ACK_DELAY),
                                 daemon=True).start()

        else:
            print(f"Unknown packet ({len(data)} bytes, type=0x{msg_type:02X}), skipping")

except socket.timeout:
    print(f"Timed out. Orders: {orders}, Cancels: {cancels}")
finally:
    rx_sock.close()
    tx_sock.close()
