/// Strategy simulation — market maker demo.
///
/// Usage:
///   cargo run --bin sim data/market.csv
///   cargo run --bin sim data/market.csv --fees          # enable US equity fees
///   cargo run --bin sim                                 # built-in demo

use std::fs::File;
use std::io::BufWriter;
use std::path::Path;

use market_engine::event::{Event, Side};
use market_engine::harness::{
    FeeModel, Harness, OpenOrder, Position, RunOutput, Strategy, StrategyAction,
};
use market_engine::replay::ReplayStep;

// ── Strategy ──────────────────────────────────────────────────────────────────

struct SimpleMarketMaker {
    half_spread:   u64,
    qty:           u64,
    max_inventory: i64,
}

impl Strategy for SimpleMarketMaker {
    fn on_step(
        &mut self,
        step: &ReplayStep,
        position: &Position,
        open_orders: &[OpenOrder],
    ) -> Vec<StrategyAction> {
        let mid = match step.snapshot.mid_price {
            Some(m) => m.round() as u64,
            None => return vec![],
        };

        let mut actions: Vec<StrategyAction> =
            open_orders.iter().map(|o| StrategyAction::Cancel { id: o.id }).collect();

        if position.qty < self.max_inventory {
            actions.push(StrategyAction::Submit {
                side: Side::Bid,
                price: mid.saturating_sub(self.half_spread),
                qty: self.qty,
            });
        }
        if position.qty > -self.max_inventory {
            actions.push(StrategyAction::Submit {
                side: Side::Ask,
                price: mid + self.half_spread,
                qty: self.qty,
            });
        }
        actions
    }
}

// ── Output helpers ────────────────────────────────────────────────────────────

fn write_output(output: &RunOutput, dir: &str) {
    // snapshots.csv
    let snap_path = format!("{}/snapshots.csv", dir);
    let mut f = BufWriter::new(File::create(&snap_path).expect("cannot create snapshots.csv"));
    output.write_snapshots(&mut f).expect("write snapshots");
    eprintln!("wrote {}", snap_path);

    // fills.csv
    let fills_path = format!("{}/fills.csv", dir);
    let mut f = BufWriter::new(File::create(&fills_path).expect("cannot create fills.csv"));
    output.write_fills(&mut f).expect("write fills");
    eprintln!("wrote {}", fills_path);
}

// ── Runners ───────────────────────────────────────────────────────────────────

fn make_strategy() -> SimpleMarketMaker {
    SimpleMarketMaker { half_spread: 1, qty: 5, max_inventory: 50 }
}

fn run_file(path: &Path, fee_model: FeeModel) {
    println!("replaying: {}\n", path.display());
    let mut harness = Harness::new(make_strategy()).with_fees(fee_model);
    let output = harness.run_file_with_output(path).expect("failed to load CSV");

    output.write_summary(&mut std::io::stdout()).unwrap();

    // Write CSVs next to the input file (or into data/ if no parent).
    let dir = path.parent()
        .and_then(|p| p.to_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("data");
    write_output(&output, dir);
}

fn run_demo(fee_model: FeeModel) {
    println!("running built-in demo\n");
    let events = vec![
        Event::Add { id:  1, side: Side::Bid, price: 10000, qty: 20 },
        Event::Add { id:  2, side: Side::Ask, price: 10002, qty: 20 },
        Event::Add { id:  3, side: Side::Bid, price:  9999, qty:  8 },
        Event::Add { id:  4, side: Side::Ask, price: 10001, qty:  8 },
        Event::Add { id:  5, side: Side::Ask, price:  9998, qty:  4 }, // aggressive sell
        Event::Add { id:  6, side: Side::Bid, price: 10000, qty: 10 },
        Event::Add { id:  7, side: Side::Ask, price: 10002, qty: 10 },
        Event::Add { id:  8, side: Side::Bid, price: 10003, qty:  3 }, // aggressive buy
        Event::Add { id:  9, side: Side::Bid, price:  9999, qty:  5 },
        Event::Add { id: 10, side: Side::Ask, price: 10001, qty:  5 },
        Event::Add { id: 11, side: Side::Ask, price:  9997, qty:  7 },
        Event::Add { id: 12, side: Side::Bid, price: 10004, qty:  7 },
        Event::Add { id: 13, side: Side::Bid, price:  9999, qty:  3 },
        Event::Add { id: 14, side: Side::Ask, price: 10001, qty:  3 },
        Event::Add { id: 15, side: Side::Ask, price:  9996, qty:  6 },
        Event::Add { id: 16, side: Side::Bid, price: 10005, qty:  6 },
        Event::Add { id: 17, side: Side::Bid, price:  9999, qty:  2 },
        Event::Add { id: 18, side: Side::Ask, price: 10001, qty:  2 },
        Event::Add { id: 19, side: Side::Ask, price:  9995, qty: 10 },
        Event::Add { id: 20, side: Side::Bid, price: 10006, qty: 10 },
    ];

    let mut harness = Harness::new(make_strategy()).with_fees(fee_model);
    let output = harness.run_with_output(events);

    output.write_summary(&mut std::io::stdout()).unwrap();
    std::fs::create_dir_all("data").ok();
    write_output(&output, "data");
}

// ── main ──────────────────────────────────────────────────────────────────────

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let use_fees = args.iter().any(|a| a == "--fees");
    let fee_model = if use_fees { FeeModel::us_equity() } else { FeeModel::zero() };

    if use_fees { eprintln!("fees: US equity (rebate=$0.002/unit, taker=$0.003/unit)"); }

    let file_arg = args.iter().skip(1).find(|a| !a.starts_with('-'));
    match file_arg {
        Some(path) => run_file(Path::new(path), fee_model),
        None       => run_demo(fee_model),
    }
}
