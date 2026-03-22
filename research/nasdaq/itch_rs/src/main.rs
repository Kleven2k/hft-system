//! itch_parser — NASDAQ ITCH 5.0 parser + order book reconstructor + symbol scanner.
//!
//! Two modes:
//!
//! 1. Parse mode (--symbol): reconstruct order book for one symbol → BookTick CSV
//!    itch_parser --file 01302020.NASDAQ_ITCH50.gz --symbol AAPL --out aapl_ticks.csv
//!
//! 2. Scan mode (--scan): score all symbols in one pass → ranked CSV
//!    itch_parser --file 01302020.NASDAQ_ITCH50.gz --scan --out scan.csv

use std::collections::BTreeMap;
use std::collections::HashMap;
use std::env;
use std::fs::File;
use std::io::{BufWriter, Read, Write};
use flate2::read::GzDecoder;

// ---------------------------------------------------------------------------
// Binary helpers (big-endian)
// ---------------------------------------------------------------------------

#[inline] fn u16be(b: &[u8], o: usize) -> u16 { u16::from_be_bytes([b[o], b[o+1]]) }
#[inline] fn u32be(b: &[u8], o: usize) -> u32 { u32::from_be_bytes([b[o], b[o+1], b[o+2], b[o+3]]) }
#[inline] fn u64be(b: &[u8], o: usize) -> u64 {
    u64::from_be_bytes([b[o],b[o+1],b[o+2],b[o+3],b[o+4],b[o+5],b[o+6],b[o+7]])
}
#[inline] fn ts6(b: &[u8], o: usize) -> u64 {
    (u16be(b, o) as u64) << 32 | u32be(b, o+2) as u64
}
#[inline] fn stock8(b: &[u8], o: usize) -> [u8; 8] {
    let mut s = [0u8; 8]; s.copy_from_slice(&b[o..o+8]); s
}
#[inline] fn stock_eq(raw: &[u8; 8], sym: &[u8]) -> bool {
    let n = sym.len().min(8);
    raw[..n] == sym[..n] && raw[n..].iter().all(|&c| c == b' ')
}
fn stock_str(raw: &[u8; 8]) -> String {
    String::from_utf8_lossy(raw).trim().to_string()
}

// ---------------------------------------------------------------------------
// Order record
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
struct Order {
    side:      u8,
    price_raw: u32,
    shares:    u32,
}

// ---------------------------------------------------------------------------
// Parse mode: full order book → BookTick CSV
// ---------------------------------------------------------------------------

struct Book {
    orders:         HashMap<u64, Order>,
    bids:           BTreeMap<u32, i64>,
    asks:           BTreeMap<u32, i64>,
    last_trade_raw: Option<u32>,
    timestamp_ns:   u64,
    rows_written:   u64,
}

impl Book {
    fn new() -> Self {
        Book {
            orders: HashMap::with_capacity(1 << 20),
            bids: BTreeMap::new(), asks: BTreeMap::new(),
            last_trade_raw: None, timestamp_ns: 0, rows_written: 0,
        }
    }
    fn add_shares(&mut self, side: u8, price_raw: u32, shares: u32) {
        if side == b'B' { *self.bids.entry(price_raw).or_insert(0) += shares as i64; }
        else            { *self.asks.entry(price_raw).or_insert(0) += shares as i64; }
    }
    fn remove_shares(&mut self, side: u8, price_raw: u32, shares: u32) {
        let lv = if side == b'B' { &mut self.bids } else { &mut self.asks };
        if let Some(s) = lv.get_mut(&price_raw) {
            *s -= shares as i64;
            if *s <= 0 { lv.remove(&price_raw); }
        }
    }
    fn emit<W: Write>(&mut self, out: &mut W, event: &str) {
        let (bid_p, bid_s) = self.bids.iter().next_back()
            .map(|(&p,&s)| (p as f64/10000.0, s)).unwrap_or((0.0,0));
        let (ask_p, ask_s) = self.asks.iter().next()
            .map(|(&p,&s)| (p as f64/10000.0, s)).unwrap_or((0.0,0));
        let lt = self.last_trade_raw.map(|p| p as f64/10000.0).unwrap_or(0.0);
        if bid_p > 0.0 && ask_p > 0.0 {
            writeln!(out, "{},{},{:.4},{:.4},{},{},{:.4}",
                self.timestamp_ns, event, bid_p, ask_p, bid_s, ask_s, lt).unwrap();
            self.rows_written += 1;
        }
    }
}

fn run_parse(file_path: &str, symbol_str: &str, out_path: &str) {
    let sym = symbol_str.as_bytes().to_vec();
    eprintln!("Parsing {} for {} ...", file_path, symbol_str);

    let f  = File::open(file_path).expect("Cannot open input file");
    let gz = GzDecoder::new(f);
    let mut reader = std::io::BufReader::with_capacity(1 << 22, gz);

    let stdout = std::io::stdout();
    let mut out: Box<dyn Write> = if out_path.is_empty() {
        Box::new(BufWriter::new(stdout.lock()))
    } else {
        Box::new(BufWriter::new(File::create(out_path).expect("Cannot create output")))
    };

    writeln!(out, "ts_ns,event,best_bid,best_ask,bid_size,ask_size,last_trade").unwrap();

    let mut book = Book::new();
    let mut len_buf = [0u8; 2];
    let mut msg_buf = vec![0u8; 65536];
    let mut msg_total = 0u64;

    loop {
        if reader.read_exact(&mut len_buf).is_err() { break; }
        let length = u16::from_be_bytes(len_buf) as usize;
        if length == 0 { continue; }
        if msg_buf.len() < length { msg_buf.resize(length, 0); }
        if reader.read_exact(&mut msg_buf[..length]).is_err() { break; }
        let d = &msg_buf[..length];
        msg_total += 1;
        if msg_total % 10_000_000 == 0 {
            eprintln!("  {}M messages, {} book ticks ...", msg_total/1_000_000, book.rows_written);
        }

        match d[0] {
            b'A' if length >= 36 => {
                let stock = stock8(d, 24);
                if !stock_eq(&stock, &sym) { continue; }
                let ref_ = u64be(d,11); let side=d[19]; let sh=u32be(d,20); let pr=u32be(d,32);
                book.orders.insert(ref_, Order{side,price_raw:pr,shares:sh});
                book.add_shares(side,pr,sh); book.timestamp_ns=ts6(d,5); book.emit(&mut out,"ADD");
            }
            b'F' if length >= 40 => {
                let stock = stock8(d, 24);
                if !stock_eq(&stock, &sym) { continue; }
                let ref_=u64be(d,11); let side=d[19]; let sh=u32be(d,20); let pr=u32be(d,32);
                book.orders.insert(ref_, Order{side,price_raw:pr,shares:sh});
                book.add_shares(side,pr,sh); book.timestamp_ns=ts6(d,5); book.emit(&mut out,"ADD");
            }
            b'E' if length >= 31 => {
                let ref_=u64be(d,11); let ex=u32be(d,19);
                let ord = match book.orders.get(&ref_).copied() { Some(o)=>o, None=>continue };
                book.remove_shares(ord.side,ord.price_raw,ex);
                if ord.shares<=ex { book.orders.remove(&ref_); }
                else { book.orders.get_mut(&ref_).unwrap().shares -= ex; }
                book.last_trade_raw=Some(ord.price_raw); book.timestamp_ns=ts6(d,5);
                book.emit(&mut out,"EXEC");
            }
            b'C' if length >= 36 => {
                let ref_=u64be(d,11); let ex=u32be(d,19); let pr_=u32be(d,32);
                let ord = match book.orders.get(&ref_).copied() { Some(o)=>o, None=>continue };
                book.remove_shares(ord.side,ord.price_raw,ex);
                if ord.shares<=ex { book.orders.remove(&ref_); }
                else { book.orders.get_mut(&ref_).unwrap().shares -= ex; }
                if d[31]==b'Y' { book.last_trade_raw=Some(pr_); }
                book.timestamp_ns=ts6(d,5); book.emit(&mut out,"EXEC");
            }
            b'X' if length >= 23 => {
                let ref_=u64be(d,11); let cx=u32be(d,19);
                let ord = match book.orders.get(&ref_).copied() { Some(o)=>o, None=>continue };
                book.remove_shares(ord.side,ord.price_raw,cx);
                if ord.shares<=cx { book.orders.remove(&ref_); }
                else { book.orders.get_mut(&ref_).unwrap().shares -= cx; }
                book.timestamp_ns=ts6(d,5); book.emit(&mut out,"CANCEL");
            }
            b'D' if length >= 19 => {
                let ref_=u64be(d,11);
                if let Some(ord)=book.orders.remove(&ref_) {
                    book.remove_shares(ord.side,ord.price_raw,ord.shares);
                    book.timestamp_ns=ts6(d,5); book.emit(&mut out,"DELETE");
                }
            }
            b'U' if length >= 35 => {
                let orig=u64be(d,11); let new_=u64be(d,19); let sh=u32be(d,27); let pr=u32be(d,31);
                if let Some(old)=book.orders.remove(&orig) {
                    book.remove_shares(old.side,old.price_raw,old.shares);
                    book.orders.insert(new_, Order{side:old.side,price_raw:pr,shares:sh});
                    book.add_shares(old.side,pr,sh);
                    book.timestamp_ns=ts6(d,5); book.emit(&mut out,"REPLACE");
                }
            }
            b'P' if length >= 44 => {
                let stock=stock8(d,24);
                if !stock_eq(&stock,&sym) { continue; }
                book.last_trade_raw=Some(u32be(d,32));
                book.timestamp_ns=ts6(d,5); book.emit(&mut out,"TRADE");
            }
            _ => {}
        }
    }
    eprintln!("Done.  {}M messages,  {} book ticks written.", msg_total/1_000_000, book.rows_written);
    if !out_path.is_empty() { eprintln!("Output: {}", out_path); }
}

// ---------------------------------------------------------------------------
// Scan mode: score every symbol in one pass
// ---------------------------------------------------------------------------

#[derive(Default)]
struct SymStats {
    add_count:    u64,
    exec_count:   u64,
    trade_count:  u64,
    trade_vol:    u64,   // total shares traded (P messages)
    price_sum:    f64,   // for average price
    price_n:      u64,
    price_min:    f64,
    price_max:    f64,
}

fn score(s: &SymStats) -> f64 {
    // Higher = better MM candidate
    // Reward: execution rate, trade activity
    // Penalise: price < $5 (sub-penny risk) or > $600 (large tick value = less fills)
    let avg_price = if s.price_n > 0 { s.price_sum / s.price_n as f64 } else { 0.0 };
    if avg_price < 5.0 || avg_price > 600.0 { return 0.0; }
    if s.exec_count < 500 { return 0.0; }   // too illiquid

    let exec_score  = (s.exec_count  as f64).ln();
    let trade_score = (s.trade_count as f64 + 1.0).ln();
    let add_score   = (s.add_count   as f64 + 1.0).ln();

    // Sweet spot: price $20-$300
    let price_score = if avg_price >= 20.0 && avg_price <= 300.0 { 1.2 } else { 0.9 };

    (exec_score + trade_score * 0.5 + add_score * 0.3) * price_score
}

fn run_scan(file_path: &str, out_path: &str, top_n: usize) {
    eprintln!("Scanning all symbols in {} ...", file_path);

    let f  = File::open(file_path).expect("Cannot open input file");
    let gz = GzDecoder::new(f);
    let mut reader = std::io::BufReader::with_capacity(1 << 22, gz);

    // symbol_id table
    let mut sym_ids:  HashMap<[u8;8], u32> = HashMap::new();
    let mut sym_keys: Vec<[u8;8]>          = Vec::new();
    let mut stats:    Vec<SymStats>         = Vec::new();
    // order_ref → symbol_id  (only for symbols we've seen)
    let mut order_sym: HashMap<u64, u32>   = HashMap::with_capacity(1 << 21);

    let mut get_id = |sym_ids: &mut HashMap<[u8;8],u32>,
                      sym_keys: &mut Vec<[u8;8]>,
                      stats_v: &mut Vec<SymStats>,
                      key: [u8;8]| -> u32 {
        if let Some(&id) = sym_ids.get(&key) { return id; }
        let id = sym_keys.len() as u32;
        sym_ids.insert(key, id);
        sym_keys.push(key);
        stats_v.push(SymStats{ price_min: f64::MAX, price_max: 0.0, ..Default::default() });
        id
    };

    let mut len_buf  = [0u8; 2];
    let mut msg_buf  = vec![0u8; 65536];
    let mut msg_total = 0u64;

    loop {
        if reader.read_exact(&mut len_buf).is_err() { break; }
        let length = u16::from_be_bytes(len_buf) as usize;
        if length == 0 { continue; }
        if msg_buf.len() < length { msg_buf.resize(length, 0); }
        if reader.read_exact(&mut msg_buf[..length]).is_err() { break; }
        let d = &msg_buf[..length];
        msg_total += 1;
        if msg_total % 20_000_000 == 0 {
            eprintln!("  {}M messages, {} symbols seen ...", msg_total/1_000_000, sym_keys.len());
        }

        match d[0] {
            b'A' if length >= 36 => {
                let key = stock8(d, 24);
                let id  = get_id(&mut sym_ids, &mut sym_keys, &mut stats, key);
                let ref_ = u64be(d, 11);
                let pr   = u32be(d, 32) as f64 / 10000.0;
                order_sym.insert(ref_, id);
                let s = &mut stats[id as usize];
                s.add_count += 1;
                s.price_sum += pr; s.price_n += 1;
                if pr < s.price_min { s.price_min = pr; }
                if pr > s.price_max { s.price_max = pr; }
            }
            b'F' if length >= 40 => {
                let key = stock8(d, 24);
                let id  = get_id(&mut sym_ids, &mut sym_keys, &mut stats, key);
                let ref_ = u64be(d, 11);
                let pr   = u32be(d, 32) as f64 / 10000.0;
                order_sym.insert(ref_, id);
                let s = &mut stats[id as usize];
                s.add_count += 1;
                s.price_sum += pr; s.price_n += 1;
                if pr < s.price_min { s.price_min = pr; }
                if pr > s.price_max { s.price_max = pr; }
            }
            b'E' if length >= 31 => {
                let ref_ = u64be(d, 11);
                if let Some(&id) = order_sym.get(&ref_) {
                    stats[id as usize].exec_count += 1;
                }
            }
            b'C' if length >= 36 => {
                let ref_ = u64be(d, 11);
                if let Some(&id) = order_sym.get(&ref_) {
                    stats[id as usize].exec_count += 1;
                }
            }
            b'P' if length >= 44 => {
                let key = stock8(d, 24);
                let id  = get_id(&mut sym_ids, &mut sym_keys, &mut stats, key);
                let pr  = u32be(d, 32) as f64 / 10000.0;
                let sh  = u32be(d, 20) as u64;
                let s = &mut stats[id as usize];
                s.trade_count += 1;
                s.trade_vol   += sh;
                s.price_sum += pr; s.price_n += 1;
                if pr < s.price_min { s.price_min = pr; }
                if pr > s.price_max { s.price_max = pr; }
            }
            _ => {}
        }
    }

    eprintln!("Done. {}M messages, {} symbols. Ranking ...", msg_total/1_000_000, sym_keys.len());

    // Rank symbols
    let mut ranked: Vec<(u32, f64)> = sym_keys.iter().enumerate()
        .map(|(i, _)| (i as u32, score(&stats[i])))
        .filter(|&(_, sc)| sc > 0.0)
        .collect();
    ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
    ranked.truncate(top_n);

    let stdout = std::io::stdout();
    let mut out: Box<dyn Write> = if out_path.is_empty() {
        Box::new(BufWriter::new(stdout.lock()))
    } else {
        Box::new(BufWriter::new(File::create(out_path).expect("Cannot create output")))
    };

    writeln!(out, "rank,symbol,score,add_count,exec_count,trade_count,trade_vol_shares,avg_price,min_price,max_price").unwrap();
    for (rank, (id, sc)) in ranked.iter().enumerate() {
        let sym = stock_str(&sym_keys[*id as usize]);
        let s   = &stats[*id as usize];
        let avg = if s.price_n > 0 { s.price_sum / s.price_n as f64 } else { 0.0 };
        let min = if s.price_min == f64::MAX { 0.0 } else { s.price_min };
        writeln!(out, "{},{},{:.2},{},{},{},{},{:.2},{:.2},{:.2}",
            rank+1, sym, sc,
            s.add_count, s.exec_count, s.trade_count, s.trade_vol,
            avg, min, s.price_max).unwrap();
    }

    if !out_path.is_empty() { eprintln!("Scan output: {}", out_path); }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn main() {
    let args: Vec<String> = env::args().collect();

    let mut file_path  = String::new();
    let mut symbol_str = String::new();
    let mut out_path   = String::new();
    let mut scan_mode  = false;
    let mut top_n: usize = 50;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--file"   => { i += 1; file_path   = args[i].clone(); }
            "--symbol" => { i += 1; symbol_str  = args[i].to_uppercase(); }
            "--out"    => { i += 1; out_path    = args[i].clone(); }
            "--scan"   => { scan_mode = true; }
            "--top"    => { i += 1; top_n = args[i].parse().unwrap_or(50); }
            "--help"|"-h" => {
                eprintln!("Usage:");
                eprintln!("  itch_parser --file <f.gz> --symbol <SYM> [--out <out.csv>]");
                eprintln!("  itch_parser --file <f.gz> --scan [--top 50] [--out scan.csv]");
                std::process::exit(0);
            }
            _ => {}
        }
        i += 1;
    }

    if file_path.is_empty() {
        eprintln!("--file is required");
        std::process::exit(1);
    }

    if scan_mode {
        run_scan(&file_path, &out_path, top_n);
    } else {
        if symbol_str.is_empty() {
            eprintln!("Provide --symbol <SYM> or use --scan");
            std::process::exit(1);
        }
        run_parse(&file_path, &symbol_str, &out_path);
    }
}
