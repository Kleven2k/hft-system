# ADR-001: All-`always_ff` TCP Engine Design

**Date:** 2026-03-23
**Status:** Accepted
**Phase:** 27

---

## Context

`tcp_engine.sv` computes Ethernet/IP/TCP checksums and assembles frame headers inline with data
transmission. A naive implementation would use `always_comb` blocks to continuously re-compute
these values from live wire signals — a standard RTL pattern for combinatorial logic.

The problem is timing closure. A 125 MHz Artix-7 budget gives 8 ns per cycle. Computing IP and
TCP checksums combinatorially while also muxing 54 header bytes creates deep logic chains that
fail timing analysis. This was observed in an earlier prototype where `always_comb` checksum
logic created 14+ LUT levels on the critical path.

There is a secondary correctness concern: Vivado does not support expression slicing like
`(a + b)[31:16]`. Intermediate combinatorial results must be assigned to a named signal before
slicing, which forces registration anyway.

---

## Decision

`tcp_engine.sv` uses **only `always_ff` blocks** — no `always_comb` for any logic that touches
the transmit datapath.

Checksums are computed **once per frame**, in a dedicated `S_TX_PREP` state, before any byte
is emitted. The state machine holds transmission until `S_TX_PREP` completes (one cycle):

```
S_IDLE → S_ARP_WAIT → S_TCP_SYN → … → S_TX_PREP → S_TX_HDR → S_TX_BUF → S_TX_EMIT
```

Header bytes are emitted by a `hdr_byte(pos)` function — a 54-way `case` statement on
**registered values only**. All inputs to `hdr_byte` are flopped before `S_TX_HDR` begins.

Two localparams, `IP_PARTIAL` and `TCP_PARTIAL`, pre-compute the constant portion of each
checksum at elaboration time (from module parameters like `MY_IP`, `SERVER_IP`, `SERVER_PORT`).
The variable portion (payload length, sequence numbers, payload checksum) is accumulated during
`S_TX_PREP` and `S_TX_BUF`.

---

## Consequences

**Positive:**
- Zero combinatorial depth on the transmit path — timing is trivially met
- No Vivado expression-slice workarounds needed
- Checksums are deterministic: computed exactly once from stable registered values
- `hdr_byte` function is purely a function of registered state; synthesis sees a simple mux tree

**Negative:**
- `S_TX_PREP` adds one dead cycle before each frame. At 125 MHz this is 8 ns — negligible for
  a TCP session that runs at millisecond timescales
- The code is more verbose than an `always_comb` implementation; the intent of each registered
  intermediate must be documented in comments

**Trade-off accepted:** The 8 ns latency penalty per frame is many orders of magnitude below
the network RTT. Timing safety is worth it.

---

## Alternatives Considered

**`always_comb` checksum + registered header buffer:** Compute checksums combinatorially, then
register the result before emitting. Rejected because the combinatorial chain still exists and
still fails timing — it just fails earlier in the pipeline.

**Pre-computed header RAM:** Store fully assembled headers in a block RAM, fill them in ahead
of time. Rejected as over-engineering for a single-connection TCP client.
