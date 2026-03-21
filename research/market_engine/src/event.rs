#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Bid,
    Ask,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    Add {
        id: u64,
        side: Side,
        price: u64,
        qty: u64,
    },
    Cancel {
        id: u64,
    },
    Execute {
        id: u64,
        qty: u64,
    },
    Modify {
        id: u64,
        price: u64,
        qty: u64,
    },
}