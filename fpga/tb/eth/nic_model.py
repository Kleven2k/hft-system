# nic_model.py

import random
from scapy.all import Ether, IP, UDP, raw 

LOCAL_MAC = "02:00:00:00:00:01"
LOCAL_IP  = "192.168.1.10"

CLIENT_MAC = "aa:bb:cc:dd:ee:ff"
CLIENT_IP  = "192.168.1.11"

ITCH_PORT = 17010

class SimulatedNIC:

    def __init__(self, dut, axis_send, clk):

        self.dut = dut
        self.axis_send = axis_send
        self.clk = clk

    # ------------------------------------------------
    # Build UDP packet
    # ------------------------------------------------

    def build_udp(self, payload):

        pkt = Ether(
            src=CLIENT_MAC,
            dst=LOCAL_MAC
        ) / IP(
            src=CLIENT_IP,
            dst=LOCAL_IP
        ) / UDP(
            sport=random.randint(10000, 60000),
            dport=ITCH_PORT
        ) / payload

        return raw(pkt)
    # ------------------------------------------------
    # Send packet
    # ------------------------------------------------

    async def send(self, payload):

        frame = self.build_udp(payload)

        await self.axis_send(
            self.dut.clk,
            self.dut.mac_rx_tdata,
            self.dut.mac_rx_tvalid,
            self.dut.mac_rx_tready,
            self.dut.mac_rx_tlast,
            self.dut.mac_rx_tuser,
            frame
        )
    
    # ------------------------------------------------
    # Microburst traffic
    # ------------------------------------------------

    async def microburst(self, count):

        for i in range(count):

            payload = f"ITCH{i}".encode()

            await self.send(payload)

    # ------------------------------------------------
    # Jitter traffic
    # ------------------------------------------------

    async def jitter_stream(self, packets):

        for i in range(packets):

            payload = f"MD{i}".encode()

            await self.send(payload)

            # random delay
            delay = random.randint(0, 20)

            for _ in range(delay):
                await self.clk

    # ------------------------------------------------
    # Packet loss simulation
    # ------------------------------------------------

    async def lossy_stream(self, packets, drop_rate=0.1):

        for i in range(packets):

            if random.random() < drop_rate:
                continue

            payload = f"ITCH{i}".encode()

            await self.send(payload)

    # ------------------------------------------------
    # Mixed market feed
    # ------------------------------------------------

    async def market_feed(self, packets):

        symbols = [B"AAPL", b"TSLA", b"NVDA", b"MSFT"]

        for i in range(packets):

            symbol = random.choice(symbols)

            price = random.randint(100, 200)

            payload = symbol + b":" + str(price).encode()

            await self.send(payload)