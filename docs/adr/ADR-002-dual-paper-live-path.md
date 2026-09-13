# ADR-002: Dual Paper/Live Order Path

**Date:** 2026-03-23
**Status:** Accepted
**Phase:** 27

---

## Context

Before Phase 27 the FPGA sent orders over UDP to `ack_simulator.py`, which replied with
synthetic ACKs. This was the only order path. Live trading requires a real TCP connection to
the exchange (SoupBinTCP/OUCH 4.2), but the paper-trading UDP path is still needed for
smoke-testing without an exchange connection.

Two design options were considered for how to handle the transition to live:

1. **Replace** the UDP paper path entirely with TCP
2. **Keep both paths active simultaneously**, with a priority mux on MAC TX and an ACK selector

---

## Decision

**Both TCP and UDP order paths remain active simultaneously.**

- `mac_tx_arbiter.sv` arbitrates the single MAC TX port: TCP (`tcp_engine`) has priority 0,
  UDP telemetry / paper-ACK has priority 1.
- `order_engine.sv` exposes OUCH frames as an AXI-Stream port (`ouch_tx_*`), which feeds
  `soup_session.sv` (TCP live path) and also `eth_stack_wrapper.sv` (UDP paper path).
- On the ACK side, `hft_top.sv` implements a priority mux: if `tcp_engine` is in
  `S_ACTIVE` (session established), execution reports from TCP take priority; otherwise
  UDP ACKs from `ack_simulator.py` are used.
- `ack_simulator.py` continues to run unchanged during smoke tests.

The mode is implicit — determined by whether a TCP session is established — not by a
configuration register or compile-time flag.

---

## Consequences

**Positive:**
- Zero RTL changes needed when switching between paper and live operation — just connect
  (or don't connect) to the exchange
- Paper mode still works with no network infrastructure: plug in FPGA, run
  `ack_simulator.py`, done
- Both paths can be exercised in the same run, confirming that the arbiter and ACK mux
  work correctly under concurrent load
- No `ifdef` or parameter guards that could cause a live build to accidentally ship with
  paper logic disabled

**Negative:**
- The OUCH stream is broadcast to both `soup_session` and `eth_stack_wrapper`; they both
  consume the same orders. In a real deployment `ack_simulator.py` would receive the same
  orders the exchange does — confusing if left running
- `mac_tx_arbiter` adds a 1-cycle latency bubble when TCP wins priority over a pending
  UDP frame. Acceptable given UDP is telemetry, not latency-critical

**Operational note:** In live deployment, `ack_simulator.py` should not be running —
it will echo every live order back as a fake fill and corrupt the FPGA position tracker.

---

## Alternatives Considered

**Compile-time flag to select TCP vs UDP:** Rejected. Requires a full rebuild to switch
modes. Increases risk of a "wrong bitstream" incident in production.

**Single path, replace UDP with TCP:** Rejected. Loses paper-trading capability needed
for integration testing without an exchange.

**Software-selectable mode via UART:** Over-engineering for the current stage. The implicit
TCP-session-present mux achieves the same result without adding UART commands.
