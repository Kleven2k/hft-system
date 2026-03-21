#[derive(Debug, Clone)]
pub struct BookSnapshot {
    pub best_bid: Option<(u64, u64)>,
    pub best_ask: Option<(u64, u64)>,
    pub spread: Option<u64>,
    pub mid_price: Option<f64>,
    pub imbalance: Option<f64>,
}

/// Top-N levels of the order book.
/// `bids` are sorted high → low (best bid first).
/// `asks` are sorted low → high (best ask first).
#[derive(Debug, Clone, Default)]
pub struct Depth {
    pub bids: Vec<(u64, u64)>,
    pub asks: Vec<(u64, u64)>,
}