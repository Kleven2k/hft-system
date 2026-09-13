use serde::Serialize;
use std::sync::{Arc, Mutex};
use tokio::net::UdpSocket;

const TELEM_PORT: u16 = 42002;
const MAGIC: [u8; 4] = [0x48, 0x46, 0x54, 0x01];
const PKT_BYTES: usize = 80;
const MAX_BURST: u8 = 5; // must match strategy.sv MAX_BURST parameter

/// Mirrors the per-slot fields in monitor.py's telemetry packet.
#[derive(Serialize, Clone)]
pub struct SlotTelemetry {
    pub position: i32,
    pub pnl_usd: f64,
    pub rejects: u8,
    pub token: u8,
    pub max_burst: u8,
    pub bid_valid: bool,
    pub ask_valid: bool,
    pub stop_loss: bool,
}

#[derive(Serialize, Clone)]
pub struct LatencyStats {
    pub min_ns: u64,
    pub max_ns: u64,
    pub last_ns: u64,
    pub count: u32,
}

#[derive(Serialize, Clone)]
pub struct TelemetrySnapshot {
    pub connected: bool,
    pub seq: u32,
    pub total_orders: u64,
    pub orders_per_sec: f64,
    pub slots: Vec<SlotTelemetry>,
    pub latency: LatencyStats,
    pub age_secs: f64,
}

impl Default for TelemetrySnapshot {
    fn default() -> Self {
        Self {
            connected: false,
            seq: 0,
            total_orders: 0,
            orders_per_sec: 0.0,
            slots: Vec::new(),
            latency: LatencyStats { min_ns: 0, max_ns: 0, last_ns: 0, count: 0 },
            age_secs: 0.0,
        }
    }
}

pub type SharedTelemetry = Arc<Mutex<TelemetrySnapshot>>;

struct PrevPacket {
    total_orders: u64,
    at: std::time::Instant,
}

fn parse_packet(data: &[u8], prev: &Option<PrevPacket>) -> Option<TelemetrySnapshot> {
    if data.len() < PKT_BYTES || data[0..4] != MAGIC {
        return None;
    }

    let seq = u32::from_be_bytes(data[4..8].try_into().ok()?);
    let total_orders = u64::from_be_bytes(data[8..16].try_into().ok()?);

    let mut slots = Vec::with_capacity(4);
    for i in 0..4 {
        let off = 16 + i * 12;
        let position = i32::from_be_bytes(data[off..off + 4].try_into().ok()?);
        let pnl_ticks = i32::from_be_bytes(data[off + 4..off + 8].try_into().ok()?);
        let rejects = data[off + 8];
        let token = data[off + 9];
        let flags = data[off + 10];

        slots.push(SlotTelemetry {
            position,
            pnl_usd: pnl_ticks as f64 / 10_000.0,
            rejects,
            token,
            max_burst: MAX_BURST,
            bid_valid: flags & 0b001 != 0,
            ask_valid: flags & 0b010 != 0,
            stop_loss: flags & 0b100 != 0,
        });
    }

    let cyc_to_ns = |c: u32| c as u64 * 8;
    let latency = LatencyStats {
        min_ns: cyc_to_ns(u32::from_be_bytes(data[64..68].try_into().ok()?)),
        max_ns: cyc_to_ns(u32::from_be_bytes(data[68..72].try_into().ok()?)),
        last_ns: cyc_to_ns(u32::from_be_bytes(data[72..76].try_into().ok()?)),
        count: u32::from_be_bytes(data[76..80].try_into().ok()?),
    };

    let orders_per_sec = match prev {
        Some(p) => {
            let dt = p.at.elapsed().as_secs_f64();
            if dt > 0.0 { (total_orders.saturating_sub(p.total_orders)) as f64 / dt } else { 0.0 }
        }
        None => 0.0,
    };

    Some(TelemetrySnapshot {
        connected: true,
        seq,
        total_orders,
        orders_per_sec,
        slots,
        latency,
        age_secs: 0.0,
    })
}

/// Listens for FPGA telemetry UDP packets and keeps the latest snapshot
/// available for the HTTP API to serve. Runs for the lifetime of the process.
pub fn spawn_listener() -> SharedTelemetry {
    let shared: SharedTelemetry = Arc::new(Mutex::new(TelemetrySnapshot::default()));
    let shared_task = shared.clone();

    tokio::spawn(async move {
        let socket = match UdpSocket::bind(("0.0.0.0", TELEM_PORT)).await {
            Ok(s) => s,
            Err(e) => {
                eprintln!("telemetry: failed to bind UDP {TELEM_PORT}: {e} (is monitor.py already running?)");
                return;
            }
        };

        println!("telemetry: listening on UDP {TELEM_PORT}");

        let mut buf = [0u8; 256];
        let mut prev: Option<PrevPacket> = None;
        let mut last_seen = std::time::Instant::now();

        loop {
            let recv = tokio::time::timeout(std::time::Duration::from_secs(1), socket.recv_from(&mut buf)).await;

            match recv {
                Ok(Ok((n, _addr))) => {
                    if let Some(mut snap) = parse_packet(&buf[..n], &prev) {
                        last_seen = std::time::Instant::now();
                        snap.age_secs = 0.0;
                        prev = Some(PrevPacket { total_orders: snap.total_orders, at: last_seen });
                        *shared_task.lock().unwrap() = snap;
                    }
                }
                Ok(Err(e)) => {
                    eprintln!("telemetry: recv error: {e}");
                }
                Err(_timeout) => {
                    // No packet in the last second — update staleness on the existing snapshot.
                    let mut snap = shared_task.lock().unwrap();
                    if snap.connected {
                        snap.age_secs = last_seen.elapsed().as_secs_f64();
                    }
                }
            }
        }
    });

    shared
}
