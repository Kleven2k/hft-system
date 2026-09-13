use serde::Serialize;
use std::path::Path;

/// Mirrors StrategyParams in research/backtest/backtest.py
pub struct StrategyParams {
    pub quote_offset: f64,   // in price units (ticks * tick_size), simplified vs Python's tick-based version
    pub stale_thresh: f64,
    pub spread_max: f64,
    pub max_inventory: i64,
    pub order_qty: i64,
    pub cooldown_rows: usize,
}

impl Default for StrategyParams {
    // Note: Python's backtest.py defaults to offset=2000/stale=2000, which (combined with
    // the tick sizes in tick_size_for) produces zero fills on real crypto tick data — see
    // the avaxusdt tick-size bug fixed 2026-08-28. Using the best sweep result instead.
    fn default() -> Self {
        Self {
            quote_offset: 1.0,
            stale_thresh: 2.0,
            spread_max: 20000.0,
            max_inventory: 500,
            order_qty: 100,
            cooldown_rows: 1,
        }
    }
}

/// One row of tick data: (timestamp_ns, bid, ask, bar_low, bar_high)
pub struct Row {
    pub timestamp_ns: i64,
    pub bid: f64,
    pub ask: f64,
    pub bar_low: f64,
    pub bar_high: f64,
}

#[derive(Serialize)]
pub struct EquityPoint {
    /// 1-based index of this fill (strictly increasing, unlike wall-clock time which can
    /// have multiple fills within the same second)
    pub fill_number: u64,
    /// Cumulative P&L after this fill
    pub pnl: f64,
}

#[derive(Serialize)]
pub struct BacktestResult {
    pub symbol: String,
    pub n_rows: usize,
    pub n_orders: u64,
    pub n_fills: u64,
    pub n_cancels: u64,
    pub total_pnl: f64,
    pub fill_rate: f64,
    pub avg_edge: f64,
    pub sharpe: f64,
    pub max_long: i64,
    pub max_short: i64,
    pub equity_curve: Vec<EquityPoint>,
}

const MAKER_REBATE_BPS: f64 = 1.0;

pub fn run_backtest(symbol: &str, tick_size: f64, rows: &[Row], params: &StrategyParams) -> BacktestResult {
    let mut position: i64 = 0;
    let mut pending = false;
    let mut pending_is_buy = false;
    let mut pending_price = 0.0;
    let mut pending_bid = 0.0;
    let mut pending_ask = 0.0;
    let mut pending_row: usize = 0;
    let mut cooldown_until: i64 = -1;

    let mut n_orders: u64 = 0;
    let mut n_fills: u64 = 0;
    let mut n_cancels: u64 = 0;
    let mut total_pnl: f64 = 0.0;
    let mut fill_edges: Vec<f64> = Vec::new();
    let mut max_long: i64 = 0;
    let mut max_short: i64 = 0;
    let mut equity_curve: Vec<EquityPoint> = Vec::new();

    let fill_window_rows: usize = 300;

    for (i, row) in rows.iter().enumerate() {
        let mid = (row.bid + row.ask) / 2.0;
        let spread = row.ask - row.bid;
        let spread_tick = if spread > 0.0 { (spread / tick_size).round() } else { 0.0 };

        if pending {
            let stale = if pending_is_buy {
                (row.bid - pending_bid).abs() > params.stale_thresh * tick_size
            } else {
                (row.ask - pending_ask).abs() > params.stale_thresh * tick_size
            };

            if stale || (i - pending_row) > fill_window_rows {
                pending = false;
                n_cancels += 1;
                continue;
            }

            if pending_is_buy && row.bar_low <= pending_price {
                position += params.order_qty;
                let edge = mid - pending_price;
                let notional = pending_price * params.order_qty as f64;
                let rebate = notional * MAKER_REBATE_BPS / 10_000.0;
                fill_edges.push(edge);
                total_pnl += edge + rebate;
                n_fills += 1;
                pending = false;
                cooldown_until = (i + params.cooldown_rows) as i64;
                max_long = max_long.max(position);
                max_short = max_short.max(-position);
                equity_curve.push(EquityPoint { fill_number: n_fills, pnl: total_pnl });
                continue;
            }

            if !pending_is_buy && row.bar_high >= pending_price {
                position -= params.order_qty;
                let edge = pending_price - mid;
                let notional = pending_price * params.order_qty as f64;
                let rebate = notional * MAKER_REBATE_BPS / 10_000.0;
                fill_edges.push(edge);
                total_pnl += edge + rebate;
                n_fills += 1;
                pending = false;
                cooldown_until = (i + params.cooldown_rows) as i64;
                max_long = max_long.max(position);
                max_short = max_short.max(-position);
                equity_curve.push(EquityPoint { fill_number: n_fills, pnl: total_pnl });
                continue;
            }

            continue; // waiting for fill
        }

        // Try to place a new order
        if (i as i64) < cooldown_until {
            continue;
        }
        if spread_tick > params.spread_max {
            continue;
        }

        let buy_price = row.bid - params.quote_offset * tick_size;
        let sell_price = row.ask + params.quote_offset * tick_size;

        let (is_buy, price) = if position <= 0 && position > -params.max_inventory {
            (true, buy_price)
        } else if position >= 0 && position < params.max_inventory {
            (false, sell_price)
        } else {
            continue; // at inventory limit
        };

        if price <= 0.0 {
            continue;
        }

        pending = true;
        pending_is_buy = is_buy;
        pending_price = price;
        pending_bid = row.bid;
        pending_ask = row.ask;
        pending_row = i;
        n_orders += 1;
    }

    let fill_rate = if n_orders > 0 { n_fills as f64 / n_orders as f64 } else { 0.0 };
    let avg_edge = if !fill_edges.is_empty() {
        fill_edges.iter().sum::<f64>() / fill_edges.len() as f64
    } else {
        0.0
    };
    let sharpe = sharpe_of(&fill_edges);

    BacktestResult {
        symbol: symbol.to_string(),
        n_rows: rows.len(),
        n_orders,
        n_fills,
        n_cancels,
        total_pnl,
        fill_rate,
        avg_edge,
        equity_curve,
        sharpe,
        max_long,
        max_short,
    }
}

fn sharpe_of(edges: &[f64]) -> f64 {
    if edges.len() < 2 {
        return f64::NAN;
    }
    let mean = edges.iter().sum::<f64>() / edges.len() as f64;
    let var = edges.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / edges.len() as f64;
    if var > 0.0 {
        mean / var.sqrt()
    } else {
        f64::NAN
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_python_reference_on_synthetic_data() {
        let rows = load_csv(Path::new("tests/fixtures/synthetic_ticks.csv"))
            .expect("failed to load tests/fixtures/synthetic_ticks.csv");
        let params = StrategyParams {
            quote_offset: 1.0,
            stale_thresh: 20.0,
            spread_max: 20000.0,
            max_inventory: 500,
            order_qty: 100,
            cooldown_rows: 1,
        };
        let result = run_backtest("TEST", 0.01, &rows, &params);

        // Reference values from: python backtest.py --file test_data.csv --offset 1 --stale 20
        assert_eq!(result.n_orders, 4);
        assert_eq!(result.n_fills, 3);
        assert_eq!(result.n_cancels, 0);
        assert_eq!(result.max_long, 100);
        assert_eq!(result.max_short, 0);
        assert!((result.total_pnl - 2.8601).abs() < 0.001, "total_pnl was {}", result.total_pnl);
        assert!((result.sharpe - (-1.98)).abs() < 0.01, "sharpe was {}", result.sharpe);
    }
}

/// Mirrors SYMBOL_TICK_SIZES in research/backtest/backtest.py
pub fn tick_size_for(symbol: &str) -> f64 {
    match symbol {
        "avaxusdt" => 0.001,
        "linkusdt" => 0.001,
        "aaveusdt" => 0.01,
        "injusdt" => 0.001,
        "ethusdt" => 0.01,
        "solusdt" => 0.001,
        "bnbusdt" => 0.01,
        "btcusdt" => 0.01,
        _ => 0.01,
    }
}

/// Finds the most recent daily CSV for a symbol under research/data/, e.g. avaxusdt_20260828.csv.
pub fn find_latest_csv(data_dir: &Path, symbol: &str) -> Option<std::path::PathBuf> {
    let prefix = format!("{symbol}_");
    let mut matches: Vec<_> = std::fs::read_dir(data_dir)
        .ok()?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with(&prefix) && n.ends_with(".csv") && !n.contains("klines"))
                .unwrap_or(false)
        })
        .collect();
    matches.sort();
    matches.pop()
}

/// Lists YYYYMMDD dates available for a symbol under research/data/, newest first.
pub fn list_dates_for(data_dir: &Path, symbol: &str) -> Vec<String> {
    let prefix = format!("{symbol}_");
    let mut dates: Vec<String> = std::fs::read_dir(data_dir)
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let name = e.file_name().to_str()?.to_string();
            if !name.starts_with(&prefix) || !name.ends_with(".csv") || name.contains("klines") {
                return None;
            }
            name.strip_prefix(&prefix)?.strip_suffix(".csv").map(str::to_string)
        })
        .collect();
    dates.sort();
    dates.reverse();
    dates
}

/// Finds the CSV for a symbol on a specific YYYYMMDD date.
pub fn find_csv_for_date(data_dir: &Path, symbol: &str, date: &str) -> Option<std::path::PathBuf> {
    let path = data_dir.join(format!("{symbol}_{date}.csv"));
    path.exists().then_some(path)
}

/// Loads a collector CSV: columns timestamp_ns, bid, ask (bar_low/bar_high default to bid/ask).
pub fn load_csv(path: &Path) -> Result<Vec<Row>, csv::Error> {
    let mut reader = csv::Reader::from_path(path)?;
    let headers = reader.headers()?.clone();
    let mut rows = Vec::new();

    for result in reader.records() {
        let record = result?;
        let get = |name: &str| -> Option<f64> {
            headers.iter().position(|h| h == name)
                .and_then(|idx| record.get(idx))
                .and_then(|v| v.parse::<f64>().ok())
        };

        let bid = match get("bid") { Some(v) => v, None => continue };
        let ask = match get("ask") { Some(v) => v, None => continue };
        let bar_low = get("bar_low").unwrap_or(bid);
        let bar_high = get("bar_high").unwrap_or(ask);
        let timestamp_ns = get("timestamp_ns").unwrap_or(0.0) as i64;

        rows.push(Row { timestamp_ns, bid, ask, bar_low, bar_high });
    }

    Ok(rows)
}
