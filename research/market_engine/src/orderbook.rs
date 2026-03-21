use std::collections::{BTreeMap, HashMap, VecDeque};

use crate::event::{Event, Side};
use crate::features::{BookSnapshot, Depth};

#[derive(Debug, Clone, Copy)]
pub struct Order {
    pub id: u64,
    pub side: Side,
    pub price: u64,
    pub qty: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Trade {
    pub aggressor_id: u64,
    pub passive_id: u64,
    pub price: u64,
    pub qty: u64,
}

/// Price levels store queues of order IDs in arrival order (price-time priority).
/// The orders HashMap holds the actual order data including remaining quantity.
#[derive(Debug, Default)]
pub struct OrderBook {
    bids: BTreeMap<u64, VecDeque<u64>>,
    asks: BTreeMap<u64, VecDeque<u64>>,
    orders: HashMap<u64, Order>,
}

impl OrderBook {
    pub fn new() -> Self {
        Self::default()
    }

    /// Process an event. Returns any trades generated (only Add can produce trades).
    pub fn process_event(&mut self, event: Event) -> Vec<Trade> {
        match event {
            Event::Add { id, side, price, qty } => self.add_order(id, side, price, qty),
            Event::Cancel { id } => {
                self.cancel_order(id);
                vec![]
            }
            Event::Execute { id, qty } => {
                self.execute_order(id, qty);
                vec![]
            }
            Event::Modify { id, price, qty } => {
                self.modify_order(id, price, qty);
                vec![]
            }
        }
    }

    pub fn best_bid(&self) -> Option<(u64, u64)> {
        let price = *self.bids.keys().next_back()?;
        Some((price, self.level_qty(Side::Bid, price)))
    }

    pub fn best_ask(&self) -> Option<(u64, u64)> {
        let price = *self.asks.keys().next()?;
        Some((price, self.level_qty(Side::Ask, price)))
    }

    pub fn spread(&self) -> Option<u64> {
        let (bid, _) = self.best_bid()?;
        let (ask, _) = self.best_ask()?;
        Some(ask.saturating_sub(bid))
    }

    pub fn mid_price(&self) -> Option<f64> {
        let (bid, _) = self.best_bid()?;
        let (ask, _) = self.best_ask()?;
        Some((bid as f64 + ask as f64) / 2.0)
    }

    pub fn total_bid_volume(&self) -> u64 {
        self.orders.values().filter(|o| matches!(o.side, Side::Bid)).map(|o| o.qty).sum()
    }

    pub fn total_ask_volume(&self) -> u64 {
        self.orders.values().filter(|o| matches!(o.side, Side::Ask)).map(|o| o.qty).sum()
    }

    pub fn imbalance(&self) -> Option<f64> {
        let bid_vol = self.total_bid_volume();
        let ask_vol = self.total_ask_volume();
        let total = bid_vol + ask_vol;
        if total == 0 { None } else { Some(bid_vol as f64 / total as f64) }
    }

    /// Returns the top `n` price levels on each side.
    /// Bids are ordered high → low; asks low → high.
    pub fn depth(&self, n: usize) -> Depth {
        let bids = self
            .bids
            .iter()
            .rev()
            .take(n)
            .map(|(price, queue)| (*price, queue.iter().map(|id| self.orders[id].qty).sum()))
            .collect();

        let asks = self
            .asks
            .iter()
            .take(n)
            .map(|(price, queue)| (*price, queue.iter().map(|id| self.orders[id].qty).sum()))
            .collect();

        Depth { bids, asks }
    }

    pub fn snapshot(&self) -> BookSnapshot {
        BookSnapshot {
            best_bid: self.best_bid(),
            best_ask: self.best_ask(),
            spread: self.spread(),
            mid_price: self.mid_price(),
            imbalance: self.imbalance(),
        }
    }

    // ── private ──────────────────────────────────────────────────────────────

    fn level_qty(&self, side: Side, price: u64) -> u64 {
        let levels = match side {
            Side::Bid => &self.bids,
            Side::Ask => &self.asks,
        };
        levels.get(&price).map(|q| q.iter().map(|id| self.orders[id].qty).sum()).unwrap_or(0)
    }

    /// Insert order into the appropriate price level queue.
    fn enqueue(&mut self, id: u64, side: Side, price: u64) {
        let levels = match side {
            Side::Bid => &mut self.bids,
            Side::Ask => &mut self.asks,
        };
        levels.entry(price).or_default().push_back(id);
    }

    /// Remove a specific order ID from its price level queue. Cleans up empty levels.
    fn dequeue(&mut self, id: u64, side: Side, price: u64) {
        let levels = match side {
            Side::Bid => &mut self.bids,
            Side::Ask => &mut self.asks,
        };
        if let Some(queue) = levels.get_mut(&price) {
            queue.retain(|&oid| oid != id);
            if queue.is_empty() {
                levels.remove(&price);
            }
        }
    }

    fn add_order(&mut self, id: u64, side: Side, price: u64, qty: u64) -> Vec<Trade> {
        if self.orders.contains_key(&id) {
            return vec![];
        }
        self.match_and_rest(id, side, price, qty)
    }

    /// Core matching loop. Matches `qty` of a new order against the opposing book,
    /// then rests any unfilled remainder. Returns all trades generated.
    fn match_and_rest(&mut self, id: u64, side: Side, price: u64, qty: u64) -> Vec<Trade> {
        let mut trades = Vec::new();
        let mut remaining = qty;

        loop {
            if remaining == 0 {
                break;
            }

            // Find best opposing level and check for a cross.
            let (passive_price, passive_id) = match side {
                Side::Bid => {
                    // We are buying — match against the lowest ask.
                    match self.asks.iter().next() {
                        Some((ask_price, queue)) if *ask_price <= price => {
                            (*ask_price, *queue.front().unwrap())
                        }
                        _ => break,
                    }
                }
                Side::Ask => {
                    // We are selling — match against the highest bid.
                    match self.bids.iter().next_back() {
                        Some((bid_price, queue)) if *bid_price >= price => {
                            (*bid_price, *queue.front().unwrap())
                        }
                        _ => break,
                    }
                }
            };

            let passive_qty = self.orders[&passive_id].qty;
            let fill_qty = remaining.min(passive_qty);

            // Update or remove the passive order.
            if fill_qty == passive_qty {
                self.orders.remove(&passive_id);
                let opp_side = side.opposite();
                self.dequeue(passive_id, opp_side, passive_price);
            } else {
                self.orders.get_mut(&passive_id).unwrap().qty -= fill_qty;
            }

            remaining -= fill_qty;
            trades.push(Trade { aggressor_id: id, passive_id, price: passive_price, qty: fill_qty });
        }

        // Rest any unfilled quantity on the book.
        if remaining > 0 {
            self.orders.insert(id, Order { id, side, price, qty: remaining });
            self.enqueue(id, side, price);
        }

        trades
    }

    fn cancel_order(&mut self, id: u64) {
        if let Some(order) = self.orders.remove(&id) {
            self.dequeue(id, order.side, order.price);
        }
    }

    /// External execution (e.g. from a feed). Reduces qty; removes order when fully filled.
    fn execute_order(&mut self, id: u64, exec_qty: u64) {
        let (side, price, should_remove) = match self.orders.get_mut(&id) {
            Some(order) => {
                let actual = exec_qty.min(order.qty);
                order.qty -= actual;
                (order.side, order.price, order.qty == 0)
            }
            None => return,
        };

        if should_remove {
            self.orders.remove(&id);
            self.dequeue(id, side, price);
        }
    }

    /// Modify re-rests the order at the new price/qty. Loses time priority.
    fn modify_order(&mut self, id: u64, new_price: u64, new_qty: u64) {
        let old = match self.orders.get(&id).copied() {
            Some(o) => o,
            None => return,
        };

        self.dequeue(id, old.side, old.price);
        self.orders.insert(id, Order { id, side: old.side, price: new_price, qty: new_qty });
        self.enqueue(id, old.side, new_price);
    }
}

// ── Side helper ──────────────────────────────────────────────────────────────

impl Side {
    fn opposite(self) -> Side {
        match self {
            Side::Bid => Side::Ask,
            Side::Ask => Side::Bid,
        }
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::{Event, Side};

    // ── existing book-state tests (no matching) ───────────────────────────────

    #[test]
    fn add_bid_and_ask_updates_top_of_book() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        book.process_event(Event::Add { id: 2, side: Side::Ask, price: 101, qty: 12 });
        assert_eq!(book.best_bid(), Some((100, 10)));
        assert_eq!(book.best_ask(), Some((101, 12)));
        assert_eq!(book.spread(), Some(1));
    }

    #[test]
    fn partial_execute_reduces_quantity() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        book.process_event(Event::Execute { id: 1, qty: 4 });
        assert_eq!(book.best_bid(), Some((100, 6)));
    }

    #[test]
    fn full_execute_removes_order_and_level() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        book.process_event(Event::Execute { id: 1, qty: 10 });
        assert_eq!(book.best_bid(), None);
    }

    #[test]
    fn cancel_removes_order_and_level() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 101, qty: 12 });
        book.process_event(Event::Cancel { id: 1 });
        assert_eq!(book.best_ask(), None);
    }

    #[test]
    fn modify_moves_order_to_new_price() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 101, qty: 12 });
        book.process_event(Event::Modify { id: 1, price: 103, qty: 8 });
        assert_eq!(book.best_ask(), Some((103, 8)));
    }

    #[test]
    fn mid_price_is_computed_correctly() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        book.process_event(Event::Add { id: 2, side: Side::Ask, price: 102, qty: 20 });
        assert_eq!(book.mid_price(), Some(101.0));
    }

    #[test]
    fn imbalance_is_computed_correctly() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 30 });
        book.process_event(Event::Add { id: 2, side: Side::Ask, price: 101, qty: 10 });
        assert_eq!(book.imbalance(), Some(0.75));
    }

    // ── matching engine tests ─────────────────────────────────────────────────

    #[test]
    fn aggressor_bid_fully_fills_resting_ask() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 10 });

        let trades = book.process_event(Event::Add { id: 2, side: Side::Bid, price: 100, qty: 10 });

        assert_eq!(trades, [Trade { aggressor_id: 2, passive_id: 1, price: 100, qty: 10 }]);
        assert_eq!(book.best_ask(), None);
        assert_eq!(book.best_bid(), None);
    }

    #[test]
    fn aggressor_ask_fully_fills_resting_bid() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 8 });

        let trades = book.process_event(Event::Add { id: 2, side: Side::Ask, price: 100, qty: 8 });

        assert_eq!(trades, [Trade { aggressor_id: 2, passive_id: 1, price: 100, qty: 8 }]);
        assert_eq!(book.best_bid(), None);
    }

    #[test]
    fn partial_fill_leaves_aggressor_on_book() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 5 });

        // Aggressor wants 10, only 5 available — rests 5 as bid@100.
        let trades = book.process_event(Event::Add { id: 2, side: Side::Bid, price: 100, qty: 10 });

        assert_eq!(trades, [Trade { aggressor_id: 2, passive_id: 1, price: 100, qty: 5 }]);
        assert_eq!(book.best_ask(), None);
        assert_eq!(book.best_bid(), Some((100, 5)));
    }

    #[test]
    fn partial_fill_leaves_passive_on_book() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 10 });

        // Aggressor only wants 4 — passive keeps 6.
        let trades = book.process_event(Event::Add { id: 2, side: Side::Bid, price: 100, qty: 4 });

        assert_eq!(trades, [Trade { aggressor_id: 2, passive_id: 1, price: 100, qty: 4 }]);
        assert_eq!(book.best_ask(), Some((100, 6)));
        assert_eq!(book.best_bid(), None);
    }

    #[test]
    fn aggressor_sweeps_multiple_levels() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 5 });
        book.process_event(Event::Add { id: 2, side: Side::Ask, price: 101, qty: 5 });

        // Bid@102 for 8 — sweeps level 100 (5), then partial at 101 (3).
        let trades = book.process_event(Event::Add { id: 3, side: Side::Bid, price: 102, qty: 8 });

        assert_eq!(trades.len(), 2);
        assert_eq!(trades[0], Trade { aggressor_id: 3, passive_id: 1, price: 100, qty: 5 });
        assert_eq!(trades[1], Trade { aggressor_id: 3, passive_id: 2, price: 101, qty: 3 });
        assert_eq!(book.best_ask(), Some((101, 2)));
        assert_eq!(book.best_bid(), None);
    }

    #[test]
    fn price_time_priority_within_level() {
        let mut book = OrderBook::new();
        // Two resting asks at the same price — id:1 arrived first.
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 6 });
        book.process_event(Event::Add { id: 2, side: Side::Ask, price: 100, qty: 6 });

        // Bid@100 for 9 — should fill id:1 fully (6), then partial id:2 (3).
        let trades = book.process_event(Event::Add { id: 3, side: Side::Bid, price: 100, qty: 9 });

        assert_eq!(trades.len(), 2);
        assert_eq!(trades[0], Trade { aggressor_id: 3, passive_id: 1, price: 100, qty: 6 });
        assert_eq!(trades[1], Trade { aggressor_id: 3, passive_id: 2, price: 100, qty: 3 });
        assert_eq!(book.best_ask(), Some((100, 3)));
    }

    #[test]
    fn non_crossing_order_does_not_match() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 101, qty: 10 });

        let trades = book.process_event(Event::Add { id: 2, side: Side::Bid, price: 100, qty: 10 });

        assert!(trades.is_empty());
        assert_eq!(book.best_bid(), Some((100, 10)));
        assert_eq!(book.best_ask(), Some((101, 10)));
    }

    // ── depth tests ───────────────────────────────────────────────────────────

    #[test]
    fn depth_returns_levels_in_correct_order() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        book.process_event(Event::Add { id: 2, side: Side::Bid, price: 99,  qty: 5  });
        book.process_event(Event::Add { id: 3, side: Side::Bid, price: 98,  qty: 8  });
        book.process_event(Event::Add { id: 4, side: Side::Ask, price: 101, qty: 12 });
        book.process_event(Event::Add { id: 5, side: Side::Ask, price: 102, qty: 6  });
        book.process_event(Event::Add { id: 6, side: Side::Ask, price: 103, qty: 3  });

        let d = book.depth(3);
        assert_eq!(d.bids, [(100, 10), (99, 5), (98, 8)]);
        assert_eq!(d.asks, [(101, 12), (102, 6), (103, 3)]);
    }

    #[test]
    fn depth_aggregates_multiple_orders_at_same_level() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        book.process_event(Event::Add { id: 2, side: Side::Bid, price: 100, qty: 7  });

        let d = book.depth(1);
        assert_eq!(d.bids, [(100, 17)]);
    }

    #[test]
    fn depth_n_larger_than_available_levels() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Bid, price: 100, qty: 10 });
        book.process_event(Event::Add { id: 2, side: Side::Ask, price: 101, qty: 5  });

        let d = book.depth(10);
        assert_eq!(d.bids.len(), 1);
        assert_eq!(d.asks.len(), 1);
    }

    #[test]
    fn depth_empty_book() {
        let book = OrderBook::new();
        let d = book.depth(5);
        assert!(d.bids.is_empty());
        assert!(d.asks.is_empty());
    }

    #[test]
    fn depth_updates_after_match() {
        let mut book = OrderBook::new();
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 5 });
        book.process_event(Event::Add { id: 2, side: Side::Ask, price: 101, qty: 8 });

        // Aggressor takes all of level 100 and 3 from level 101.
        book.process_event(Event::Add { id: 3, side: Side::Bid, price: 102, qty: 8 });

        let d = book.depth(5);
        assert_eq!(d.asks, [(101, 5)]);
        assert!(d.bids.is_empty());
    }

    #[test]
    fn fill_at_passive_price_not_aggressor_price() {
        let mut book = OrderBook::new();
        // Resting ask at 100, aggressor bids at 105 (willing to pay more).
        // Trade should execute at the passive (maker) price: 100.
        book.process_event(Event::Add { id: 1, side: Side::Ask, price: 100, qty: 5 });
        let trades = book.process_event(Event::Add { id: 2, side: Side::Bid, price: 105, qty: 5 });

        assert_eq!(trades[0].price, 100);
    }
}
