#!/usr/bin/env python3
"""Quick diagnostic: check if factory contract exists and can be called."""
import traceback
from web3 import Web3

RPC = "https://avalanche-c-chain-rpc.publicnode.com"
TJ_V1_FACTORY = "0x9Ad6C38BE94206cA50bb0d90783181662f0Cfa10"
WAVAX_ADDR    = "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"
USDC_E_ADDR   = "0xA7D7079b0FEaD91F3e65f86E8915Cb59c1a4C664"

w3 = Web3(Web3.HTTPProvider(RPC))
print(f"connected: {w3.is_connected()}  chain_id: {w3.eth.chain_id}")

# Check if there's bytecode at the factory address
code = w3.eth.get_code(Web3.to_checksum_address(TJ_V1_FACTORY))
print(f"factory bytecode length: {len(code)} bytes  ({'OK' if len(code) > 10 else 'NO CODE — wrong address!'})")

if len(code) < 10:
    print("\nFactory address is wrong or not deployed. Try Trader Joe V2 factory:")
    print("  0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac  (Uniswap V2 style)")
    exit(1)

# Try the getPair call with full traceback
FACTORY_ABI = [{"inputs":[{"name":"tokenA","type":"address"},{"name":"tokenB","type":"address"}],
                "name":"getPair","outputs":[{"name":"pair","type":"address"}],
                "stateMutability":"view","type":"function"}]
try:
    factory = w3.eth.contract(address=Web3.to_checksum_address(TJ_V1_FACTORY), abi=FACTORY_ABI)
    pair = factory.functions.getPair(
        Web3.to_checksum_address(WAVAX_ADDR),
        Web3.to_checksum_address(USDC_E_ADDR)
    ).call()
    print(f"getPair result: {pair}")
except Exception as e:
    print(f"getPair FAILED: {e}")
    traceback.print_exc()
