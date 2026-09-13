# ADR-003: SoupBinTCP Session Layer as Separate Module

**Date:** 2026-03-23
**Status:** Accepted
**Phase:** 27

---

## Context

OUCH 4.2 orders must be framed in SoupBinTCP before being sent to the exchange:

```
[len_hi][len_lo][type='U'][ouch_payload...]
```

In addition, the session requires:
- A Login Request (`'L'`) on TCP connect, before any orders can be sent
- Periodic Heartbeats (`'R'`) to keep the session alive
- Parsing of inbound Login Accepted (`'A'`) and Sequenced Data (`'S'`) execution reports

This logic could have been embedded directly in `tcp_engine.sv` (one large module) or
kept in a separate `soup_session.sv` that sits on top of `tcp_engine`'s AXI-Stream app
interface.

---

## Decision

SoupBinTCP session logic lives in a **separate `soup_session.sv` module** that connects
to `tcp_engine.sv` via AXI-Stream (`app_tx_*` / `app_rx_*`).

`tcp_engine.sv` is a pure TCP client: it handles ARP, 3-way handshake, TCP sequence/ack
numbers, checksums, and raw byte TX/RX. It has no knowledge of SoupBinTCP or OUCH.

`soup_session.sv` drives `tcp_engine` through its AXI-Stream app interface and handles:
- A 416-bit shift register (`ctrl_sr`) for Login Request and Heartbeat frames
- OUCH framing: prepend 3-byte SoupBinTCP header (`len_hi`, `len_lo`, `'U'`), then
  pass OUCH bytes through directly from `order_engine`
- Inbound parsing: byte-by-byte state machine for `'A'` (Login Accepted) and `'S'`
  (Sequenced Data / execution reports) frames
- Heartbeat timer: `HB_CYC` parameter (default 125,000,000 = 1 s at 125 MHz)

The `ctrl_sr` shift register uses a "left-justified" convention: the byte currently being
transmitted is always at `ctrl_sr[415:408]`. Each clock cycle where the byte is accepted
(app_tx_tready=1) the register shifts left 8 bits. `ctrl_last` tracks how many bytes
remain.

---

## Consequences

**Positive:**
- `tcp_engine.sv` is independently reusable for any TCP application (not OUCH-specific)
- `soup_session.sv` can be unit-tested against a mock TCP layer without bringing up
  the full Ethernet/IP stack
- The AXI-Stream interface between the two modules is a natural simulation boundary —
  cocotb can inject raw app_rx bytes to test session parsing in isolation
- Adding or changing the session protocol (e.g. FIX, ITCH 5.0 upstream) only requires
  replacing `soup_session.sv`

**Negative:**
- Two modules with an AXI-Stream handshake between them introduces the possibility of
  backpressure deadlock if either side stalls unexpectedly
- The `ctrl_sr` shift register is 416 bits (52 bytes) — sized for the longest control
  frame (Login Request). Heartbeat only uses 3 bytes; the remaining 49 are wasted each
  heartbeat cycle
- SoupBinTCP frame length must be known before the first byte is sent. For OUCH Enter
  Order (49 bytes) vs Cancel (15 bytes), `soup_session` inspects `ouch_tdata` on the
  first byte to determine which length to use. This requires the sender (order_engine)
  to hold the first byte stable until `ouch_tready` is asserted — an implicit protocol
  constraint not enforced by AXI-Stream alone

**Constraint documented:** `order_engine` must not advance OUCH output until
`ouch_tready` goes high. `soup_session` holds `ouch_tready=0` while transmitting the
3-byte SoupBinTCP header, during which time `ouch_tdata` must remain stable.

---

## Alternatives Considered

**Inline session logic in `tcp_engine.sv`:** Rejected. Would make tcp_engine
application-specific and untestable in isolation. Also increases module complexity
significantly (tcp_engine is already ~400 lines).

**Software-generated SoupBinTCP framing:** Rejected for Phase 27. The goal of Phase 27
is to eliminate software from the order critical path. Keeping framing in software
re-introduces a CPU dependency.

**Parameterised generic session framer:** Over-engineering. The system targets one
exchange protocol. A generic framer would require a runtime-programmable frame format
which adds significant RTL complexity for no current benefit.
