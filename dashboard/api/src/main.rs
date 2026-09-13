use axum::{extract::Query, http::StatusCode, routing::get, Json, Router};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::time::SystemTime;
use tower_http::cors::{Any, CorsLayer};

mod backtest;
mod market;

use backtest::{find_csv_for_date, find_latest_csv, list_dates_for, load_csv, run_backtest, tick_size_for, StrategyParams};
use market::{fetch_klines, MarketQuery};

async fn get_market(Query(q): Query<MarketQuery>) -> Result<Json<Vec<market::Candle>>, (StatusCode, String)> {
    fetch_klines(&q.symbol, &q.interval, 200)
        .await
        .map(Json)
        .map_err(|e| (StatusCode::BAD_GATEWAY, e))
}

#[derive(Deserialize)]
struct BacktestQuery {
    symbol: String,
}

#[derive(Deserialize)]
struct TickDatesQuery {
    symbol: String,
}

#[derive(Deserialize)]
struct TicksQuery {
    symbol: String,
    date: String,
}

#[derive(Serialize)]
struct TickPoint {
    time: i64,
    bid: f64,
    ask: f64,
}

async fn get_tick_dates(Query(q): Query<TickDatesQuery>) -> Json<Vec<String>> {
    let dates = list_dates_for(&data_dir(), &q.symbol.to_lowercase());
    Json(dates)
}

async fn get_ticks(Query(q): Query<TicksQuery>) -> Result<Json<Vec<TickPoint>>, (StatusCode, String)> {
    let symbol = q.symbol.to_lowercase();

    let csv_path = find_csv_for_date(&data_dir(), &symbol, &q.date)
        .ok_or((StatusCode::NOT_FOUND, format!("no data for '{symbol}' on {}", q.date)))?;

    let rows = load_csv(&csv_path)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("failed to read {}: {e}", csv_path.display())))?;

    // Downsample if there are a lot of rows — the chart doesn't need every single tick.
    const MAX_POINTS: usize = 2000;
    let step = (rows.len() / MAX_POINTS).max(1);

    let points: Vec<TickPoint> = rows
        .iter()
        .step_by(step)
        .enumerate()
        .map(|(i, r)| TickPoint { time: i as i64, bid: r.bid, ask: r.ask })
        .collect();

    Ok(Json(points))
}

fn data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../research/data")
}

async fn get_backtest(Query(q): Query<BacktestQuery>) -> Result<Json<backtest::BacktestResult>, (StatusCode, String)> {
    let symbol = q.symbol.to_lowercase();

    let csv_path = find_latest_csv(&data_dir(), &symbol)
        .ok_or((StatusCode::NOT_FOUND, format!("no data file found for symbol '{symbol}'")))?;

    let rows = load_csv(&csv_path)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("failed to read {}: {e}", csv_path.display())))?;

    let tick_size = tick_size_for(&symbol);
    let params = StrategyParams::default();
    let result = run_backtest(&symbol, tick_size, &rows, &params);

    Ok(Json(result))
}

#[derive(Serialize)]
struct StatusResponse {
    data_dir_reachable: bool,
    symbol_files_found: usize,
    most_recent_file: Option<String>,
    most_recent_age_secs: Option<u64>,
}

async fn get_status() -> Json<StatusResponse> {
    let dir = data_dir();
    let entries = std::fs::read_dir(&dir);

    let Ok(entries) = entries else {
        return Json(StatusResponse {
            data_dir_reachable: false,
            symbol_files_found: 0,
            most_recent_file: None,
            most_recent_age_secs: None,
        });
    };

    let mut newest: Option<(String, SystemTime)> = None;
    let mut count = 0usize;

    for entry in entries.filter_map(|e| e.ok()) {
        let path = entry.path();
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
        if !name.ends_with(".csv") || name.contains("klines") {
            continue;
        }
        count += 1;
        if let Ok(meta) = entry.metadata() {
            if let Ok(modified) = meta.modified() {
                if newest.as_ref().map(|(_, t)| modified > *t).unwrap_or(true) {
                    newest = Some((name, modified));
                }
            }
        }
    }

    let (most_recent_file, most_recent_age_secs) = match newest {
        Some((name, time)) => {
            let age = SystemTime::now().duration_since(time).map(|d| d.as_secs()).unwrap_or(0);
            (Some(name), Some(age))
        }
        None => (None, None),
    };

    Json(StatusResponse {
        data_dir_reachable: true,
        symbol_files_found: count,
        most_recent_file,
        most_recent_age_secs,
    })
}

#[tokio::main]
async fn main() {
    let cors = CorsLayer::new().allow_origin(Any);

    let app = Router::new()
        .route("/api/backtest", get(get_backtest))
        .route("/api/status", get(get_status))
        .route("/api/tick-dates", get(get_tick_dates))
        .route("/api/ticks", get(get_ticks))
        .route("/api/market", get(get_market))
        .layer(cors);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:8080").await.unwrap();
    println!("API listening on http://127.0.0.1:8080");
    axum::serve(listener, app).await.unwrap();
}
