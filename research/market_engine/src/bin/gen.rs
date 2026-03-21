/// Synthetic market data generator.
///
/// Simulates a random-walk mid price with configurable spread, volatility,
/// and order flow mix. Writes market events to stdout in the CSV format
/// accepted by the harness.
///
/// Usage:
///   cargo run --bin gen > data/market.csv
///   cargo run --bin gen -- 5000 12000 0.3 > data/tight.csv
///
/// Positional args (all optional, override the defaults below):
///   1  num_events      (default: 10_000)
///   2  initial_price   price in ticks (default: 10_000)
///   3  volatility      std-dev of price step in ticks per event (default: 0.5)

use std::fs::File;
use std::io::{self, BufWriter, Write};

use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};

// ── Config ────────────────────────────────────────────────────────────────────

struct Config {
    seed:            u64,
    num_events:      usize,

    /// Starting mid price in ticks.
    initial_price:   u64,

    /// Std-dev of mid-price random walk per event (in ticks).
    volatility:      f64,

    /// Distance in ticks from mid to each side's best resting quote.
    half_spread:     u64,

    /// Passive orders placed up to this many ticks behind the spread.
    passive_depth:   u64,

    /// Order quantity drawn uniformly from [qty_min, qty_max].
    qty_min:         u64,
    qty_max:         u64,

    /// Fraction of events that are aggressive (cross the spread → immediate match).
    aggressive_frac: f64,

    /// Fraction of events that are cancels of existing resting orders.
    cancel_frac:     f64,

    /// Cap on resting orders per side; once reached, new passives replace cancels.
    max_resting:     usize,
}

impl Config {
    fn default() -> Self {
        Config {
            seed:            42,
            num_events:      10_000,
            initial_price:   10_000,
            volatility:      0.5,
            half_spread:     1,
            passive_depth:   4,
            qty_min:         1,
            qty_max:         20,
            aggressive_frac: 0.10,
            cancel_frac:     0.15,
            max_resting:     20,
        }
    }
}

// ── Resting order record ──────────────────────────────────────────────────────

#[derive(Clone)]
#[allow(dead_code)]
struct Resting {
    id:    u64,
    side:  Side,
    price: u64,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Side { Bid, Ask }

impl Side {
    fn csv(&self) -> &'static str {
        match self { Side::Bid => "bid", Side::Ask => "ask" }
    }
}

// ── Gaussian helper (Box-Muller) ──────────────────────────────────────────────

fn gauss(rng: &mut StdRng, std_dev: f64) -> f64 {
    let u1: f64 = rng.gen_range(f64::MIN_POSITIVE..1.0);
    let u2: f64 = rng.gen_range(0.0..1.0);
    let z = (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos();
    z * std_dev
}

// ── Generator ────────────────────────────────────────────────────────────────

fn run(cfg: &Config, out: &mut dyn Write) {
    let mut rng   = StdRng::seed_from_u64(cfg.seed);
    let mut mid   = cfg.initial_price as f64;
    let mut next_id: u64 = 1;
    let mut bids: Vec<Resting> = Vec::new();
    let mut asks: Vec<Resting> = Vec::new();

    writeln!(out, "# synthetic market — events={} init={} vol={} spread={}t",
        cfg.num_events, cfg.initial_price, cfg.volatility, cfg.half_spread * 2).unwrap();

    for _ in 0..cfg.num_events {
        let roll: f64 = rng.gen_range(0.0..1.0);

        let can_cancel    = !bids.is_empty() || !asks.is_empty();
        let do_cancel     = can_cancel && roll < cfg.cancel_frac;
        let do_aggressive = !do_cancel && roll < cfg.cancel_frac + cfg.aggressive_frac;

        if do_cancel {
            let total = bids.len() + asks.len();
            let idx = rng.gen_range(0..total);
            let id = if idx < bids.len() {
                bids.remove(idx).id
            } else {
                asks.remove(idx - bids.len()).id
            };
            writeln!(out, "cancel,{}", id).unwrap();

        } else if do_aggressive {
            let side = if rng.gen_bool(0.5) { Side::Bid } else { Side::Ask };
            let price = match side {
                Side::Bid => (mid + cfg.half_spread as f64).round() as u64,
                Side::Ask => (mid - cfg.half_spread as f64).round().max(1.0) as u64,
            };
            let qty = rng.gen_range(cfg.qty_min..=cfg.qty_max);
            writeln!(out, "add,{},{},{},{}", next_id, side.csv(), price, qty).unwrap();
            next_id += 1;

        } else {
            let side = if rng.gen_bool(0.5) { Side::Bid } else { Side::Ask };
            let depth_offset = rng.gen_range(0..=cfg.passive_depth) as f64;
            let price = match side {
                Side::Bid => {
                    let p = (mid - cfg.half_spread as f64 - depth_offset)
                        .round().max(1.0) as u64;
                    if bids.len() >= cfg.max_resting {
                        writeln!(out, "cancel,{}", bids.remove(0).id).unwrap();
                    }
                    p
                }
                Side::Ask => {
                    let p = (mid + cfg.half_spread as f64 + depth_offset).round() as u64;
                    if asks.len() >= cfg.max_resting {
                        writeln!(out, "cancel,{}", asks.remove(0).id).unwrap();
                    }
                    p
                }
            };
            let qty = rng.gen_range(cfg.qty_min..=cfg.qty_max);
            writeln!(out, "add,{},{},{},{}", next_id, side.csv(), price, qty).unwrap();
            let r = Resting { id: next_id, side, price };
            match side { Side::Bid => bids.push(r), Side::Ask => asks.push(r) }
            next_id += 1;
        }

        mid += gauss(&mut rng, cfg.volatility);
        mid = mid.max(cfg.half_spread as f64 + 1.0);
    }
}

// ── main ─────────────────────────────────────────────────────────────────────

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut cfg = Config::default();
    let mut out_path: Option<&str> = None;

    // Args: [num_events] [initial_price] [volatility] [-o output.csv]
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "-o" => {
                i += 1;
                out_path = Some(args.get(i).expect("-o requires a filename"));
            }
            s => match i {
                1 => cfg.num_events    = s.parse().expect("num_events must be integer"),
                2 => cfg.initial_price = s.parse().expect("initial_price must be integer"),
                3 => cfg.volatility    = s.parse().expect("volatility must be float"),
                _ => {}
            }
        }
        i += 1;
    }

    if let Some(path) = out_path {
        let file = File::create(path).expect("cannot create output file");
        let mut writer = BufWriter::new(file);
        run(&cfg, &mut writer);
        eprintln!("wrote {}", path);
    } else {
        let stdout = io::stdout();
        let mut writer = BufWriter::new(stdout.lock());
        run(&cfg, &mut writer);
    }
}
