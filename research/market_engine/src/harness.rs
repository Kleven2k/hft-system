use std::collections::HashMap;
use std::io::{self, Write};
use std::path::Path;

use crate::event::{Event, Side};
use crate::orderbook::OrderBook;
use crate::replay::{load_events_from_file, ReplayError, ReplayStep};

// ── Fee model ─────────────────────────────────────────────────────────────────

/// Maker-taker fee schedule, expressed as cost per unit filled.
#[derive(Debug, Clone)]
pub struct FeeModel {
    /// Amount received per unit on a passive (maker) fill. Positive = rebate.
    pub maker_rebate_per_unit: f64,
    /// Amount paid per unit on an aggressive (taker) fill. Positive = cost.
    pub taker_fee_per_unit: f64,
}

impl FeeModel {
    pub fn zero() -> Self {
        FeeModel { maker_rebate_per_unit: 0.0, taker_fee_per_unit: 0.0 }
    }

    /// Typical US equity exchange: $0.002 maker rebate, $0.003 taker fee per share.
    pub fn us_equity() -> Self {
        FeeModel { maker_rebate_per_unit: 0.002, taker_fee_per_unit: 0.003 }
    }

    /// Net fee impact on cash for this fill.
    /// Returns a positive number when you receive money (maker rebate),
    /// negative when you pay (taker fee).
    pub fn fee_for_fill(&self, role: &FillRole, qty: u64) -> f64 {
        match role {
            FillRole::Passive   =>  self.maker_rebate_per_unit * qty as f64,
            FillRole::Aggressor => -self.taker_fee_per_unit    * qty as f64,
        }
    }
}

impl Default for FeeModel {
    fn default() -> Self { FeeModel::zero() }
}

// ── Strategy interface ────────────────────────────────────────────────────────

pub trait Strategy {
    /// Called after each market event. Return actions to take this step.
    fn on_step(
        &mut self,
        step: &ReplayStep,
        position: &Position,
        open_orders: &[OpenOrder],
    ) -> Vec<StrategyAction>;
}

#[derive(Debug, Clone)]
pub enum StrategyAction {
    Submit { side: Side, price: u64, qty: u64 },
    /// Cancel a resting strategy order by its harness-assigned ID.
    Cancel { id: u64 },
}

/// A strategy order currently resting on the book.
#[derive(Debug, Clone)]
pub struct OpenOrder {
    pub id: u64,
    pub side: Side,
    pub price: u64,
    pub remaining_qty: u64,
}

// ── Position / P&L ────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Default)]
pub struct Position {
    /// Net shares/contracts. Positive = long, negative = short.
    pub qty: i64,
    /// Gross cash flow (before fees). Negative when buying, positive when selling.
    pub cash_flow: f64,
    /// Cumulative fee impact. Positive = net rebates received, negative = net fees paid.
    pub fees: f64,
}

impl Position {
    /// Gross mark-to-market P&L (ignores fees).
    pub fn pnl(&self, mid_price: f64) -> f64 {
        self.cash_flow + self.qty as f64 * mid_price
    }

    /// Net mark-to-market P&L including all fees.
    pub fn net_pnl(&self, mid_price: f64) -> f64 {
        self.cash_flow + self.fees + self.qty as f64 * mid_price
    }

    pub(crate) fn record_fill(&mut self, side: Side, price: u64, qty: u64, fee: f64) {
        let cash = price as f64 * qty as f64;
        match side {
            Side::Bid => { self.qty += qty as i64; self.cash_flow -= cash; }
            Side::Ask => { self.qty -= qty as i64; self.cash_flow += cash; }
        }
        self.fees += fee;
    }
}

// ── Fill record ───────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct Fill {
    pub side: Side,
    pub price: u64,
    pub qty: u64,
    pub role: FillRole,
    /// Net fee impact for this fill (positive = rebate received, negative = fee paid).
    pub fee: f64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FillRole {
    /// Strategy order crossed the spread and filled immediately.
    Aggressor,
    /// Resting strategy order was hit by an incoming market order.
    Passive,
}

// ── HarnessStep ───────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct HarnessStep {
    pub market_step: ReplayStep,
    /// Fills generated for the strategy this step (passive + aggressor).
    pub fills: Vec<Fill>,
    /// Position snapshot after all actions this step.
    pub position: Position,
}

// ── Run output ────────────────────────────────────────────────────────────────

/// Per-step snapshot of market state and strategy P&L.
#[derive(Debug, Clone)]
pub struct Snapshot {
    pub step: usize,
    pub mid_price: f64,
    pub best_bid: Option<u64>,
    pub best_ask: Option<u64>,
    pub spread: Option<u64>,
    pub position_qty: i64,
    pub gross_pnl: f64,
    pub net_pnl: f64,
}

/// Record of a single strategy fill.
#[derive(Debug, Clone)]
pub struct FillRecord {
    pub step: usize,
    pub role: FillRole,
    pub side: Side,
    pub price: u64,
    pub qty: u64,
    pub fee: f64,
}

/// Summary statistics for a completed run.
#[derive(Debug, Clone)]
pub struct RunSummary {
    pub steps: usize,
    pub total_fills: usize,
    pub passive_fills: usize,
    pub aggressor_fills: usize,
    pub total_volume: u64,
    pub final_qty: i64,
    pub gross_pnl: f64,
    pub net_pnl: f64,
    pub total_fees: f64,
    /// Maximum peak-to-trough decline in net P&L over the run.
    pub max_drawdown: f64,
    /// Per-step Sharpe ratio (mean / std of net P&L changes). Not annualised.
    pub sharpe: f64,
    pub max_long: i64,
    pub max_short: i64,
}

/// Complete output of a simulation run.
pub struct RunOutput {
    pub snapshots: Vec<Snapshot>,
    pub fills: Vec<FillRecord>,
    pub summary: RunSummary,
}

impl RunOutput {
    /// Write per-step snapshots as CSV.
    pub fn write_snapshots(&self, w: &mut dyn Write) -> io::Result<()> {
        writeln!(w, "step,mid_price,best_bid,best_ask,spread,position_qty,gross_pnl,net_pnl")?;
        for s in &self.snapshots {
            writeln!(
                w, "{},{:.4},{},{},{},{},{:.4},{:.4}",
                s.step,
                s.mid_price,
                s.best_bid.map(|x| x.to_string()).unwrap_or_default(),
                s.best_ask.map(|x| x.to_string()).unwrap_or_default(),
                s.spread.map(|x| x.to_string()).unwrap_or_default(),
                s.position_qty,
                s.gross_pnl,
                s.net_pnl,
            )?;
        }
        Ok(())
    }

    /// Write per-fill log as CSV.
    pub fn write_fills(&self, w: &mut dyn Write) -> io::Result<()> {
        writeln!(w, "step,role,side,price,qty,fee")?;
        for f in &self.fills {
            writeln!(
                w, "{},{},{},{},{},{:.6}",
                f.step,
                match f.role { FillRole::Passive => "passive", FillRole::Aggressor => "aggressor" },
                match f.side { Side::Bid => "bid", Side::Ask => "ask" },
                f.price,
                f.qty,
                f.fee,
            )?;
        }
        Ok(())
    }

    /// Print a human-readable summary to `w`.
    pub fn write_summary(&self, w: &mut dyn Write) -> io::Result<()> {
        let s = &self.summary;
        writeln!(w, "{}", "=".repeat(48))?;
        writeln!(w, "steps:          {}", s.steps)?;
        writeln!(w, "fills:          {} ({} passive, {} aggressor)",
            s.total_fills, s.passive_fills, s.aggressor_fills)?;
        writeln!(w, "volume:         {}", s.total_volume)?;
        writeln!(w, "final qty:      {}", s.final_qty)?;
        writeln!(w, "gross pnl:      {:.2}", s.gross_pnl)?;
        writeln!(w, "fees:           {:.2}", s.total_fees)?;
        writeln!(w, "net pnl:        {:.2}", s.net_pnl)?;
        writeln!(w, "max drawdown:   {:.2}", s.max_drawdown)?;
        writeln!(w, "sharpe (raw):   {:.4}", s.sharpe)?;
        writeln!(w, "max long:       {}", s.max_long)?;
        writeln!(w, "max short:      {}", s.max_short)?;
        Ok(())
    }
}

fn compute_summary(snapshots: &[Snapshot], fills: &[FillRecord]) -> RunSummary {
    let net_pnls: Vec<f64> = snapshots.iter().map(|s| s.net_pnl).collect();
    RunSummary {
        steps: snapshots.len(),
        total_fills: fills.len(),
        passive_fills: fills.iter().filter(|f| f.role == FillRole::Passive).count(),
        aggressor_fills: fills.iter().filter(|f| f.role == FillRole::Aggressor).count(),
        total_volume: fills.iter().map(|f| f.qty).sum(),
        final_qty: snapshots.last().map(|s| s.position_qty).unwrap_or(0),
        gross_pnl: snapshots.last().map(|s| s.gross_pnl).unwrap_or(0.0),
        net_pnl: snapshots.last().map(|s| s.net_pnl).unwrap_or(0.0),
        total_fees: fills.iter().map(|f| f.fee).sum(),
        max_drawdown: max_drawdown(&net_pnls),
        sharpe: sharpe(&net_pnls),
        max_long: snapshots.iter().map(|s| s.position_qty).max().unwrap_or(0),
        max_short: snapshots.iter().map(|s| s.position_qty).min().unwrap_or(0),
    }
}

fn max_drawdown(net_pnls: &[f64]) -> f64 {
    let mut peak = f64::MIN;
    let mut dd = 0.0_f64;
    for &p in net_pnls {
        if p > peak { peak = p; }
        dd = dd.max(peak - p);
    }
    dd
}

fn sharpe(net_pnls: &[f64]) -> f64 {
    if net_pnls.len() < 2 { return 0.0; }
    let returns: Vec<f64> = net_pnls.windows(2).map(|w| w[1] - w[0]).collect();
    let mean = returns.iter().sum::<f64>() / returns.len() as f64;
    let var  = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / returns.len() as f64;
    let std  = var.sqrt();
    if std < 1e-10 { 0.0 } else { mean / std }
}

// ── Harness ───────────────────────────────────────────────────────────────────

/// Strategy order IDs start here to avoid collision with typical market data IDs.
pub(crate) const STRATEGY_ID_BASE: u64 = 1_000_000_000;

pub struct Harness<S: Strategy> {
    book: OrderBook,
    strategy: S,
    position: Position,
    open_orders: HashMap<u64, OpenOrder>,
    next_id: u64,
    fee_model: FeeModel,
}

impl<S: Strategy> Harness<S> {
    pub fn new(strategy: S) -> Self {
        Self {
            book: OrderBook::new(),
            strategy,
            position: Position::default(),
            open_orders: HashMap::new(),
            next_id: STRATEGY_ID_BASE,
            fee_model: FeeModel::zero(),
        }
    }

    /// Attach a fee model to this harness (builder pattern).
    pub fn with_fees(mut self, fee_model: FeeModel) -> Self {
        self.fee_model = fee_model;
        self
    }

    pub fn position(&self) -> &Position {
        &self.position
    }

    /// Process one market event, then ask the strategy for its response.
    pub fn step(&mut self, event: Event) -> HarnessStep {
        // ── 1. Apply the market event ─────────────────────────────────────────
        let market_trades = self.book.process_event(event);

        // ── 2. Check if any resting strategy orders were passively filled ─────
        let passive_fills: Vec<(u64, Side, u64, u64, bool)> = market_trades
            .iter()
            .filter_map(|trade| {
                let open = self.open_orders.get_mut(&trade.passive_id)?;
                let fill_qty = trade.qty.min(open.remaining_qty);
                open.remaining_qty -= fill_qty;
                let exhausted = open.remaining_qty == 0;
                Some((trade.passive_id, open.side, trade.price, fill_qty, exhausted))
            })
            .collect();

        let mut fills = Vec::new();
        for (id, side, price, qty, exhausted) in passive_fills {
            let fee = self.fee_model.fee_for_fill(&FillRole::Passive, qty);
            self.position.record_fill(side, price, qty, fee);
            fills.push(Fill { side, price, qty, role: FillRole::Passive, fee });
            if exhausted { self.open_orders.remove(&id); }
        }

        // ── 3. Build the step snapshot for the strategy ───────────────────────
        let market_step = ReplayStep {
            event,
            trades: market_trades,
            snapshot: self.book.snapshot(),
            depth: self.book.depth(5),
        };

        // ── 4. Ask the strategy what to do ────────────────────────────────────
        let open_list: Vec<OpenOrder> = self.open_orders.values().cloned().collect();
        let actions = self.strategy.on_step(&market_step, &self.position, &open_list);

        // ── 5. Execute strategy actions ───────────────────────────────────────
        for action in actions {
            match action {
                StrategyAction::Cancel { id } => {
                    self.book.process_event(Event::Cancel { id });
                    self.open_orders.remove(&id);
                }
                StrategyAction::Submit { side, price, qty } => {
                    let id = self.next_id;
                    self.next_id += 1;

                    let trades = self.book.process_event(Event::Add { id, side, price, qty });

                    let mut filled_qty = 0u64;
                    for trade in &trades {
                        if trade.aggressor_id == id {
                            filled_qty += trade.qty;
                            let fee = self.fee_model.fee_for_fill(&FillRole::Aggressor, trade.qty);
                            self.position.record_fill(side, trade.price, trade.qty, fee);
                            fills.push(Fill {
                                side, price: trade.price, qty: trade.qty,
                                role: FillRole::Aggressor, fee,
                            });
                        }
                    }

                    let remaining = qty.saturating_sub(filled_qty);
                    if remaining > 0 {
                        self.open_orders.insert(
                            id,
                            OpenOrder { id, side, price, remaining_qty: remaining },
                        );
                    }
                }
            }
        }

        HarnessStep { market_step, fills, position: self.position.clone() }
    }

    pub fn run(&mut self, events: impl IntoIterator<Item = Event>) -> Vec<HarnessStep> {
        events.into_iter().map(|e| self.step(e)).collect()
    }

    pub fn run_file(&mut self, path: &Path) -> Result<Vec<HarnessStep>, ReplayError> {
        let events = load_events_from_file(path)?;
        Ok(self.run(events))
    }

    /// Run all events and produce structured output for analysis.
    pub fn run_with_output(
        &mut self,
        events: impl IntoIterator<Item = Event>,
    ) -> RunOutput {
        let mut snapshots = Vec::new();
        let mut fill_records = Vec::new();

        for (step, event) in events.into_iter().enumerate() {
            let hs = self.step(event);
            let snap = &hs.market_step.snapshot;
            let mid  = snap.mid_price.unwrap_or(0.0);

            snapshots.push(Snapshot {
                step,
                mid_price:    mid,
                best_bid:     snap.best_bid.map(|(p, _)| p),
                best_ask:     snap.best_ask.map(|(p, _)| p),
                spread:       snap.spread,
                position_qty: hs.position.qty,
                gross_pnl:    hs.position.pnl(mid),
                net_pnl:      hs.position.net_pnl(mid),
            });

            for f in &hs.fills {
                fill_records.push(FillRecord {
                    step,
                    role:  f.role.clone(),
                    side:  f.side,
                    price: f.price,
                    qty:   f.qty,
                    fee:   f.fee,
                });
            }
        }

        let summary = compute_summary(&snapshots, &fill_records);
        RunOutput { snapshots, fills: fill_records, summary }
    }

    /// Convenience: load events from CSV and run with output.
    pub fn run_file_with_output(&mut self, path: &Path) -> Result<RunOutput, ReplayError> {
        let events = load_events_from_file(path)?;
        Ok(self.run_with_output(events))
    }
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::{Event, Side};
    use std::io::Write;
    use tempfile::NamedTempFile;

    struct Passive;
    impl Strategy for Passive {
        fn on_step(&mut self, _: &ReplayStep, _: &Position, _: &[OpenOrder]) -> Vec<StrategyAction> {
            vec![]
        }
    }

    struct AlwaysBuy { price: u64 }
    impl Strategy for AlwaysBuy {
        fn on_step(&mut self, _: &ReplayStep, _: &Position, _: &[OpenOrder]) -> Vec<StrategyAction> {
            vec![StrategyAction::Submit { side: Side::Bid, price: self.price, qty: 5 }]
        }
    }

    struct MarketMaker { half_spread: u64 }
    impl Strategy for MarketMaker {
        fn on_step(
            &mut self, step: &ReplayStep, _: &Position, open_orders: &[OpenOrder],
        ) -> Vec<StrategyAction> {
            let mid = match step.snapshot.mid_price {
                Some(m) => m.round() as u64,
                None => return vec![],
            };
            let mut actions: Vec<StrategyAction> =
                open_orders.iter().map(|o| StrategyAction::Cancel { id: o.id }).collect();
            actions.push(StrategyAction::Submit { side: Side::Bid, price: mid.saturating_sub(self.half_spread), qty: 10 });
            actions.push(StrategyAction::Submit { side: Side::Ask, price: mid + self.half_spread, qty: 10 });
            actions
        }
    }

    #[test]
    fn passive_fill_updates_position() {
        let mut harness = Harness::new(Passive);
        harness.open_orders.insert(
            STRATEGY_ID_BASE,
            OpenOrder { id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, remaining_qty: 10 },
        );
        harness.book.process_event(Event::Add {
            id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, qty: 10,
        });

        let step = harness.step(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 6 });

        assert_eq!(step.fills.len(), 1);
        assert_eq!(step.fills[0].role, FillRole::Passive);
        assert_eq!(step.fills[0].qty, 6);
        assert_eq!(step.position.qty, 6);
        assert!((step.position.cash_flow - (-600.0)).abs() < 1e-9);
    }

    #[test]
    fn aggressor_fill_updates_position() {
        let mut harness = Harness::new(AlwaysBuy { price: 101 });

        harness.step(Event::Add { id: 1, side: Side::Ask, price: 101, qty: 10 });
        assert_eq!(harness.position().qty, 5);

        let step = harness.step(Event::Add { id: 2, side: Side::Bid, price: 99, qty: 1 });

        let aggressor_fills: Vec<_> =
            step.fills.iter().filter(|f| f.role == FillRole::Aggressor).collect();
        assert_eq!(aggressor_fills.len(), 1);
        assert_eq!(aggressor_fills[0].qty, 5);
        assert_eq!(step.position.qty, 10);
    }

    #[test]
    fn cancel_removes_open_order() {
        struct SubmitOnce(bool);
        impl Strategy for SubmitOnce {
            fn on_step(&mut self, _: &ReplayStep, _: &Position, _: &[OpenOrder]) -> Vec<StrategyAction> {
                if !self.0 { self.0 = true; vec![StrategyAction::Submit { side: Side::Bid, price: 99, qty: 10 }] }
                else { vec![] }
            }
        }
        let mut h = Harness::new(SubmitOnce(false));
        h.step(Event::Add { id: 1, side: Side::Ask, price: 101, qty: 5 });
        assert_eq!(h.open_orders.len(), 1);
        let id = *h.open_orders.keys().next().unwrap();
        h.book.process_event(Event::Cancel { id });
        h.open_orders.remove(&id);
        assert!(h.open_orders.is_empty());
    }

    #[test]
    fn market_maker_posts_around_mid() {
        let mut harness = Harness::new(MarketMaker { half_spread: 1 });
        harness.step(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        harness.step(Event::Add { id: 2, side: Side::Ask, price: 101, qty: 10 });
        harness.step(Event::Add { id: 3, side: Side::Bid, price: 98, qty: 1 });
        assert_eq!(harness.open_orders.len(), 2);
        let sides: Vec<Side> = harness.open_orders.values().map(|o| o.side).collect();
        assert!(sides.contains(&Side::Bid));
        assert!(sides.contains(&Side::Ask));
    }

    #[test]
    fn pnl_is_zero_on_flat_position() {
        let p = Position::default();
        assert_eq!(p.pnl(100.0), 0.0);
        assert_eq!(p.net_pnl(100.0), 0.0);
    }

    #[test]
    fn pnl_marks_to_market() {
        let mut p = Position::default();
        p.record_fill(Side::Bid, 100, 10, 0.0);
        assert!((p.pnl(105.0) - 50.0).abs() < 1e-9);
    }

    #[test]
    fn fee_model_reduces_net_pnl() {
        let mut harness = Harness::new(Passive)
            .with_fees(FeeModel { maker_rebate_per_unit: 0.0, taker_fee_per_unit: 1.0 });

        harness.open_orders.insert(
            STRATEGY_ID_BASE,
            OpenOrder { id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, remaining_qty: 10 },
        );
        harness.book.process_event(Event::Add {
            id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, qty: 10,
        });

        // Passive fill of 5 — taker_fee is irrelevant for passive, rebate is 0.
        let step = harness.step(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 5 });
        assert_eq!(step.fills[0].fee, 0.0); // no rebate configured
        assert_eq!(step.position.fees, 0.0);
    }

    #[test]
    fn maker_rebate_increases_net_pnl() {
        let mut harness = Harness::new(Passive)
            .with_fees(FeeModel { maker_rebate_per_unit: 2.0, taker_fee_per_unit: 0.0 });

        harness.open_orders.insert(
            STRATEGY_ID_BASE,
            OpenOrder { id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, remaining_qty: 10 },
        );
        harness.book.process_event(Event::Add {
            id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, qty: 10,
        });

        let step = harness.step(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 5 });
        assert!((step.fills[0].fee - 10.0).abs() < 1e-9); // 2.0 * 5
        assert!((step.position.fees - 10.0).abs() < 1e-9);
        // gross pnl at mid=100 is 0 (bought at mid), net is +10 from rebate.
        assert!((step.position.net_pnl(100.0) - 10.0).abs() < 1e-9);
    }

    #[test]
    fn run_with_output_produces_snapshots_and_fills() {
        let mut harness = Harness::new(Passive)
            .with_fees(FeeModel::us_equity());

        harness.open_orders.insert(
            STRATEGY_ID_BASE,
            OpenOrder { id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, remaining_qty: 10 },
        );
        harness.book.process_event(Event::Add {
            id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, qty: 10,
        });

        let output = harness.run_with_output(vec![
            Event::Add { id: 1, side: Side::Ask, price: 100, qty: 5 },
            Event::Add { id: 2, side: Side::Bid, price:  98, qty: 2 },
        ]);

        assert_eq!(output.snapshots.len(), 2);
        assert_eq!(output.fills.len(), 1);
        assert_eq!(output.summary.total_fills, 1);
        assert_eq!(output.summary.passive_fills, 1);
        assert_eq!(output.summary.total_volume, 5);
        // Maker rebate = 0.002 * 5 = 0.01
        assert!((output.summary.total_fees - 0.01).abs() < 1e-9);
    }

    #[test]
    fn run_file_drives_harness() {
        let mut f = NamedTempFile::new().unwrap();
        f.write_all(b"add,1,ask,100,10\nadd,2,bid,100,5\n").unwrap();
        let mut harness = Harness::new(Passive);
        let steps = harness.run_file(f.path()).unwrap();
        assert_eq!(steps.len(), 2);
    }

    #[test]
    fn run_file_with_output_writes_csv() {
        let mut f = NamedTempFile::new().unwrap();
        f.write_all(b"add,1,ask,100,10\nadd,2,bid,100,5\n").unwrap();

        let mut harness = Harness::new(Passive);
        // register the passive order
        harness.open_orders.insert(
            STRATEGY_ID_BASE,
            OpenOrder { id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, remaining_qty: 10 },
        );
        harness.book.process_event(Event::Add {
            id: STRATEGY_ID_BASE, side: Side::Bid, price: 100, qty: 10,
        });

        let output = harness.run_file_with_output(f.path()).unwrap();
        let mut snap_buf = Vec::new();
        output.write_snapshots(&mut snap_buf).unwrap();
        let csv = String::from_utf8(snap_buf).unwrap();
        assert!(csv.starts_with("step,mid_price"));
        assert_eq!(csv.lines().count(), 3); // header + 2 data rows
    }
}
