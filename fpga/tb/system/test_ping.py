# test_ping.py — just verify Ethernet is working
from scapy.all import *

conf.iface = "Ethernet 2"
ans, _ = sr(IP(dst="192.168.1.10")/ICMP(), timeout=2)
if ans:
    print("LINK OK — FPGA responded to ping")
else:
    print("No response — check cable and static IP")