use std::fmt;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;

use crate::event::{Event, Side};
use crate::features::{BookSnapshot, Depth};
use crate::orderbook::{OrderBook, Trade};

// ── public types ─────────────────────────────────────────────────────────────

/// The result of processing one event: the trades it generated plus a
/// full book snapshot taken immediately after.
#[derive(Debug, Clone)]
pub struct ReplayStep {
    pub event: Event,
    pub trades: Vec<Trade>,
    pub snapshot: BookSnapshot,
    pub depth: Depth,
}

#[derive(Debug)]
pub enum ReplayError {
    Io(std::io::Error),
    Parse { line: usize, reason: &'static str },
}

impl fmt::Display for ReplayError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ReplayError::Io(e) => write!(f, "IO error: {e}"),
            ReplayError::Parse { line, reason } => write!(f, "parse error on line {line}: {reason}"),
        }
    }
}

impl From<std::io::Error> for ReplayError {
    fn from(e: std::io::Error) -> Self {
        ReplayError::Io(e)
    }
}

// ── ReplayEngine ─────────────────────────────────────────────────────────────

#[derive(Debug, Default)]
pub struct ReplayEngine {
    book: OrderBook,
}

impl ReplayEngine {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn book(&self) -> &OrderBook {
        &self.book
    }

    /// Process one event and return the resulting step.
    pub fn step(&mut self, event: Event) -> ReplayStep {
        let trades = self.book.process_event(event);
        ReplayStep {
            event,
            trades,
            snapshot: self.book.snapshot(),
            depth: self.book.depth(5),
        }
    }

    /// Replay an iterator of events, returning one step per event.
    pub fn run(&mut self, events: impl IntoIterator<Item = Event>) -> Vec<ReplayStep> {
        events.into_iter().map(|e| self.step(e)).collect()
    }

    /// Load events from a CSV file and replay them.
    ///
    /// CSV format (one event per line, `#` comments and blank lines ignored):
    /// ```text
    /// add,{id},{bid|ask},{price},{qty}
    /// cancel,{id}
    /// execute,{id},{qty}
    /// modify,{id},{price},{qty}
    /// ```
    pub fn run_file(&mut self, path: &Path) -> Result<Vec<ReplayStep>, ReplayError> {
        let events = parse_csv(path)?;
        Ok(self.run(events))
    }
}

// ── CSV parser ────────────────────────────────────────────────────────────────

/// Parse a CSV event file into a list of events.
/// Public so other modules (e.g. `harness`) can reuse it.
pub fn load_events_from_file(path: &Path) -> Result<Vec<Event>, ReplayError> {
    parse_csv(path)
}

fn parse_csv(path: &Path) -> Result<Vec<Event>, ReplayError> {
    let reader = BufReader::new(File::open(path)?);
    let mut events = Vec::new();

    for (line_idx, line) in reader.lines().enumerate() {
        let line_num = line_idx + 1;
        let raw = line?;
        let trimmed = raw.trim();

        // Skip blank lines and comments.
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }

        let event = parse_line(trimmed, line_num)?;
        events.push(event);
    }

    Ok(events)
}

fn parse_line(line: &str, line_num: usize) -> Result<Event, ReplayError> {
    let err = |reason| ReplayError::Parse { line: line_num, reason };
    let fields: Vec<&str> = line.split(',').collect();

    match fields.first().copied().unwrap_or("") {
        "add" => {
            if fields.len() != 5 {
                return Err(err("add requires: add,id,side,price,qty"));
            }
            let id    = parse_u64(fields[1]).ok_or_else(|| err("invalid id"))?;
            let side  = parse_side(fields[2]).ok_or_else(|| err("side must be 'bid' or 'ask'"))?;
            let price = parse_u64(fields[3]).ok_or_else(|| err("invalid price"))?;
            let qty   = parse_u64(fields[4]).ok_or_else(|| err("invalid qty"))?;
            Ok(Event::Add { id, side, price, qty })
        }
        "cancel" => {
            if fields.len() != 2 {
                return Err(err("cancel requires: cancel,id"));
            }
            let id = parse_u64(fields[1]).ok_or_else(|| err("invalid id"))?;
            Ok(Event::Cancel { id })
        }
        "execute" => {
            if fields.len() != 3 {
                return Err(err("execute requires: execute,id,qty"));
            }
            let id  = parse_u64(fields[1]).ok_or_else(|| err("invalid id"))?;
            let qty = parse_u64(fields[2]).ok_or_else(|| err("invalid qty"))?;
            Ok(Event::Execute { id, qty })
        }
        "modify" => {
            if fields.len() != 4 {
                return Err(err("modify requires: modify,id,price,qty"));
            }
            let id    = parse_u64(fields[1]).ok_or_else(|| err("invalid id"))?;
            let price = parse_u64(fields[2]).ok_or_else(|| err("invalid price"))?;
            let qty   = parse_u64(fields[3]).ok_or_else(|| err("invalid qty"))?;
            Ok(Event::Modify { id, price, qty })
        }
        _ => Err(err("unknown event type (expected add/cancel/execute/modify)")),
    }
}

fn parse_u64(s: &str) -> Option<u64> {
    s.trim().parse().ok()
}

fn parse_side(s: &str) -> Option<Side> {
    match s.trim() {
        "bid" => Some(Side::Bid),
        "ask" => Some(Side::Ask),
        _ => None,
    }
}

// ── tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::Side;
    use std::io::Write;
    use tempfile::NamedTempFile;

    // Write a CSV string to a temp file and return its path.
    fn write_temp(content: &str) -> NamedTempFile {
        let mut f = NamedTempFile::new().unwrap();
        f.write_all(content.as_bytes()).unwrap();
        f
    }

    #[test]
    fn step_returns_snapshot_after_event() {
        let mut engine = ReplayEngine::new();
        let step = engine.step(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        assert_eq!(step.snapshot.best_bid, Some((100, 10)));
        assert!(step.trades.is_empty());
    }

    #[test]
    fn step_returns_trades_on_match() {
        let mut engine = ReplayEngine::new();
        engine.step(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 5 });
        let step = engine.step(Event::Add { id: 2, side: Side::Bid, price: 100, qty: 5 });
        assert_eq!(step.trades.len(), 1);
        assert_eq!(step.trades[0].qty, 5);
        assert_eq!(step.snapshot.best_ask, None);
    }

    #[test]
    fn step_depth_reflects_book_state() {
        let mut engine = ReplayEngine::new();
        engine.step(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        engine.step(Event::Add { id: 2, side: Side::Bid, price: 99,  qty: 5  });
        engine.step(Event::Add { id: 3, side: Side::Ask, price: 101, qty: 8  });

        let step = engine.step(Event::Add { id: 4, side: Side::Ask, price: 102, qty: 3 });
        assert_eq!(step.depth.bids, [(100, 10), (99, 5)]);
        assert_eq!(step.depth.asks, [(101, 8), (102, 3)]);
    }

    #[test]
    fn run_processes_all_events_in_order() {
        let mut engine = ReplayEngine::new();
        let events = vec![
            Event::Add { id: 1, side: Side::Ask, price: 101, qty: 10 },
            Event::Add { id: 2, side: Side::Bid, price: 101, qty: 10 },
        ];
        let steps = engine.run(events);
        assert_eq!(steps.len(), 2);
        assert!(steps[0].trades.is_empty());
        assert_eq!(steps[1].trades.len(), 1);
    }

    #[test]
    fn parse_all_event_types() {
        let csv = "\
# sample feed
add,1,bid,10000,10
add,2,ask,10100,12

execute,1,4
modify,2,10200,8
cancel,2
";
        let f = write_temp(csv);
        let mut engine = ReplayEngine::new();
        let steps = engine.run_file(f.path()).unwrap();
        assert_eq!(steps.len(), 5);
        assert!(matches!(steps[0].event, Event::Add { side: Side::Bid, .. }));
        assert!(matches!(steps[2].event, Event::Execute { id: 1, qty: 4 }));
        assert!(matches!(steps[3].event, Event::Modify { id: 2, price: 10200, qty: 8 }));
        assert!(matches!(steps[4].event, Event::Cancel { id: 2 }));
    }

    #[test]
    fn parse_error_bad_side() {
        let f = write_temp("add,1,buy,100,10\n");
        let mut engine = ReplayEngine::new();
        let err = engine.run_file(f.path()).unwrap_err();
        assert!(matches!(err, ReplayError::Parse { line: 1, .. }));
    }

    #[test]
    fn parse_error_wrong_field_count() {
        let f = write_temp("cancel,1,extra\n");
        let mut engine = ReplayEngine::new();
        let err = engine.run_file(f.path()).unwrap_err();
        assert!(matches!(err, ReplayError::Parse { line: 1, .. }));
    }

    #[test]
    fn parse_error_unknown_type() {
        let f = write_temp("trade,1,2,100,5\n");
        let mut engine = ReplayEngine::new();
        let err = engine.run_file(f.path()).unwrap_err();
        assert!(matches!(err, ReplayError::Parse { line: 1, .. }));
    }
}
