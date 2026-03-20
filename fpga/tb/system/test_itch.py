import socket, struct, time

FPGA_IP   = "192.168.1.10"
MY_IP     = "192.168.1.11"
ITCH_PORT = 17010

# msg_type bytes
BID_ADD     = 0x41  # 'A'
ASK_ADD     = 0x42  # 'B'
BID_CANCEL  = 0x43  # 'C'
BID_EXECUTE = 0x45  # 'E'
ASK_CANCEL  = 0x44  # 'D'
ASK_EXECUTE = 0x46  # 'F'

def make_quote(msg_type, price, shares, symbol_id=0):
    # 20-byte ITCH payload: type(1)+timestamp(8)+price(4)+shares(4)+symbol_id(2)+reserved(1)
    return struct.pack('>BQIIHx', msg_type, time.time_ns(), price, shares, symbol_id)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((MY_IP, 0))

# Phase 21A: 4 symbols, each routed to their order book slot by symbol_id.
# price_base for each slot is set via UART (set_price_base.py):
#   slot 0: $100.00  base=1_000_000
#   slot 1: $200.00  base=2_000_000
#   slot 2:  $50.00  base=  500_000
#   slot 3:  $15.00  base=  150_000
# Bid/ask are base+15 / base+20 ticks (same offsets as Phase 17 slot 0).
SYMBOLS = [
    {"id": 0, "bid": 1_000_015, "ask": 1_000_020},
    {"id": 1, "bid": 2_000_015, "ask": 2_000_020},
    {"id": 2, "bid":   500_015, "ask":   500_020},
    {"id": 3, "bid":   150_015, "ask":   150_020},
]

def send(msg_type, price, shares, symbol_id=0, delay=0.01):
    sock.sendto(make_quote(msg_type, price, shares, symbol_id), (FPGA_IP, ITCH_PORT))
    time.sleep(delay)

print("Phase 1: seed bid and ask levels for all 4 symbols")
for sym in SYMBOLS:
    for _ in range(5):
        send(BID_ADD, sym["bid"], 500, sym["id"])
        send(ASK_ADD, sym["ask"], 500, sym["id"])
    print(f"  symbol {sym['id']}: bid={sym['bid']}  ask={sym['ask']}")

print("Phase 2: continuous feed — hold all 4 books valid for strategy")
for _ in range(10):
    for sym in SYMBOLS:
        send(BID_ADD, sym["bid"], 100, sym["id"], delay=0.005)
        send(ASK_ADD, sym["ask"], 100, sym["id"], delay=0.005)

sock.close()
print("Done — all 4 books seeded. Strategy should round-robin across slots 0-3.")
