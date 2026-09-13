# ADR-004: No TCP Retransmit Timer in Phase 27

**Date:** 2026-03-23
**Status:** Accepted (deferred to Phase 28)
**Phase:** 27

---

## Context

A production TCP implementation retransmits unacknowledged segments after a timeout.
Without this, a lost segment causes the connection to stall indefinitely — the sender
waits for an ACK that will never come; the receiver waits for the missing data.

In the general internet case, packet loss is routine. For a co-located FPGA connected to
an exchange matching engine on a dedicated LAN switch, packet loss is essentially zero
in normal operation.

Implementing a retransmit timer adds significant complexity:
- Need to buffer all unacknowledged payload bytes (up to one full window)
- Need a timer that resets on each ACK and fires on timeout
- On timeout, must re-enter `S_TX_HDR`/`S_TX_BUF`/`S_TX_EMIT` with the old data
- Sequence number tracking must distinguish "next to send" from "oldest unACKed"

---

## Decision

**Phase 27 ships without a TCP retransmit timer.**

`tcp_engine.sv` sends each segment exactly once. If the segment is lost, the session
stalls. Recovery requires a hardware reset (watchdog or power cycle).

This is explicitly scoped to the initial LAN deployment where packet loss is not expected.

Phase 28 is reserved to add a retransmit timer before any WAN or co-location deployment.

---

## Consequences

**Positive:**
- `tcp_engine.sv` is ~400 lines instead of ~700. The datapath and state machine are much
  easier to reason about and test.
- Timing closure is easier — no large retransmit buffer (could be a BRAM) needed.
- All 6 Phase 27 simulation tests pass without retransmit logic, validating the baseline.

**Negative:**
- Any network hiccup — even a single lost packet — permanently stalls the order session
  until hardware reset.
- Cannot deploy to co-location (Equinix LD4) or any WAN path without first implementing
  Phase 28.
- The 10-second OMS watchdog in `order_engine.sv` will fire if the session stalls and
  no ACKs arrive, cancelling open orders. This is the intended safety behaviour but
  means a transient network issue could cause an unintended cancel sweep.

**Risk accepted:** For LAN operation, the probability of a lost packet is negligible.
The operational risk of no retransmit is accepted for Phase 27 with the explicit
understanding that Phase 28 must be completed before co-location.

---

## Alternatives Considered

**Implement retransmit in Phase 27:** Rejected. Would have delayed Phase 27 significantly.
The simulation test harness would also need to inject packet loss, adding testbench
complexity. The LAN deployment target does not require it.

**Software retransmit (CPU monitors TCP and re-injects):** Rejected. Re-introduces CPU
on the critical path, defeating the purpose of Phase 27.
