use serde::{Deserialize, Serialize};

#[derive(Serialize)]
pub struct Candle {
    pub time: i64,   // Unix seconds (open time)
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
}

/// Fetches recent candlestick data for a symbol from Binance's public REST API.
/// No API key required for this endpoint.
pub async fn fetch_klines(symbol: &str, interval: &str, limit: u32) -> Result<Vec<Candle>, String> {
    let url = format!(
        "https://api.binance.com/api/v3/klines?symbol={}&interval={}&limit={}",
        symbol.to_uppercase(),
        interval,
        limit
    );

    let resp = reqwest::get(&url)
        .await
        .map_err(|e| format!("request to Binance failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("Binance returned status {}", resp.status()));
    }

    // Binance returns an array of arrays: [open_time, open, high, low, close, volume, ...]
    let raw: Vec<Vec<serde_json::Value>> = resp
        .json()
        .await
        .map_err(|e| format!("failed to parse Binance response: {e}"))?;

    let candles = raw
        .into_iter()
        .filter_map(|row| {
            let open_time_ms = row.first()?.as_i64()?;
            let open = row.get(1)?.as_str()?.parse::<f64>().ok()?;
            let high = row.get(2)?.as_str()?.parse::<f64>().ok()?;
            let low = row.get(3)?.as_str()?.parse::<f64>().ok()?;
            let close = row.get(4)?.as_str()?.parse::<f64>().ok()?;
            Some(Candle { time: open_time_ms / 1000, open, high, low, close })
        })
        .collect();

    Ok(candles)
}

#[derive(Deserialize)]
pub struct MarketQuery {
    pub symbol: String,
    #[serde(default = "default_interval")]
    pub interval: String,
}

fn default_interval() -> String {
    "1h".to_string()
}
