//! `X4ECON` capture records and the production replay.
//!
//! The engine-side sampler (native/engine/econcap.h, data-only reads on the MoveWorker in the
//! fixed-tick probe's slot callback) writes one text line per record. Floating-point values
//! are written as the hex bits of the engine's value so the replay compares bit for bit.
//!
//! ```text
//! X4ECON 1 build=23660954                      header
//! W <ware> <volume> [<ntags> {<tag>}*]        ware table entry (index = order of W lines;
//!                                              volume 0 in captures before the allocation
//!                                              record; tags = WareClass+0x100 in newer ones)
//! D <method#> <time:f64> <product-ware#> <amount> <special> <n1> {<ware#> <amount>}*n1 <n2> .. <n3> ..
//! P 0x5130 {<qword>}*18                       economy settings block (*(0x6cf1538) +0x5130..)
//! F <frame> <time:f64> <rng:u64>               frame marker; rng = MoveWorker TLS+0x318 at the sample
//! C <uid> <last:f64> <a28:f64> <a30:f64> <a4c> <nmodules> <ncargo> {<ware#> <amount>}*ncargo
//! A <uid> <valid> <n> {<ware#> <amount>}*n    production storage allocation (Container+0x940,
//!                                              valid = +0x931); newer captures only
//! S <uid> <module> <capacity> <ntags> {<tag>}* <n> {<ware#> <amount>}*
//!                                              one operational storage module: capacity and cargo
//!                                              tags of its storage defaults, its own contents
//! O <uid> <list> <trade> <ware#> <seller> <buyer> <amount> <min> <desired> <unit cents> <flags>
//!                                              trade offer (Container+0x8f8 list 0 / +0x910 list 1;
//!                                              seller = Trade+0x10, buyer = +0x18)
//! V <uid> <word> <reserver> <order> <ware#> <amount> <expiry:f64> [<order amount|-1> <order flags>]
//!                                              reservation (Container+0x980); the bracket for linked
//!                                              entries in newer captures
//! M <uid> <addr> <functional> <state> <start:f64> <next:f64> <psince:f64> <ptotal:f64> <item> <cycle>
//!   <ftime:f64> <fproduct:f64> <fprimary:f64> <fsecondary:f64> <ftertiary:f64>
//!   <npending> {<ware#> <amount>}* <nitems> {<ware#> <method#|-1> <tf> <pf> <prf> <sf> <tef> <cycles> <repeat>}*
//! ```
//!
//! A `C` record is followed by its `A`, `O`, `V` and `M` records. The sampler writes a station
//! only when one of its fields changed since its last record.
//!
//! **Replay.** For two consecutive records of a station whose `last` changed, the replay runs
//! [`crate::production::update_production`] from the earlier state at the later `last` and
//! compares every module field and the cargo with the later record. Storage decisions use the
//! captured allocation (`A`); captures without it take them from the recording. Random retry
//! draws are taken from the recording when exactly one draw explains the recorded time
//! ([`OracleRng`]).

use crate::ids::WareIx;
use crate::production::{
    update_production, ContainerTimes, Factors, Item, Method, Module, ProductionRng, State, StationEnv, WareAmount,
};
use std::fmt::Write as _;

/// Parsed capture.
#[derive(Clone, Debug, Default)]
pub struct Capture {
    pub build: u32,
    pub ware_volumes: Vec<u32>,
    /// Ware table (W records): id, volume, tags.
    pub wares: Vec<WareInfo>,
    /// Economy settings block from +0x5130 as raw qwords (P record), when captured.
    pub settings: Vec<u64>,
    pub methods: Vec<Method>,
    pub frames: Vec<Frame>,
}

/// A ware as the capture describes it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct WareInfo {
    pub id: String,
    /// WareClass+0xc0.
    pub volume: u32,
    /// WareClass+0x100 (sorted), empty in older captures.
    pub tags: Vec<u32>,
}

/// One operational storage module of a station (`S` record).
#[derive(Clone, Debug, PartialEq)]
pub struct StorageRecord {
    pub addr: u64,
    /// Storage defaults +0x9c0 (volume).
    pub capacity: i32,
    /// Storage defaults +0x9c8 cargo tags (sorted).
    pub tags: Vec<u32>,
    pub contents: Vec<WareAmount>,
}

#[derive(Clone, Debug, Default)]
pub struct Frame {
    pub frame: u64,
    pub time: f64,
    pub rng: u64,
    pub stations: Vec<StationRecord>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct StationRecord {
    pub uid: u64,
    pub times: ContainerTimes,
    pub cargo: Vec<WareAmount>,
    pub modules: Vec<(u64, Module)>,
    /// Trade offers of the station (`O` records), in list order.
    pub offers: Vec<OfferRecord>,
    /// Ware reservations of the station (`R`... `V` records), in engine order.
    pub reservations: Vec<ReservationRecord>,
    /// Production storage allocation (`A` record), when captured.
    pub allocation: Option<Allocation>,
    /// Operational storage modules (`S` records), when captured.
    pub storage: Vec<StorageRecord>,
}

/// Container+0x940 (per-ware production storage limits) and its valid flag +0x931.
#[derive(Clone, Debug, PartialEq)]
pub struct Allocation {
    pub valid: bool,
    pub amounts: Vec<WareAmount>,
}

/// One trade offer as captured from Container+0x8f8 (list 0) or +0x910 (list 1).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OfferRecord {
    pub list: u8,
    pub trade: u64,
    pub ware: Option<WareIx>,
    /// Trade+0x10: the selling component (the station for its sell offers), 0 otherwise.
    pub seller: u64,
    /// Trade+0x18: the buying component (the station for its buy offers), 0 otherwise.
    pub buyer: u64,
    pub amount: i32,
    pub min_amount: i32,
    pub desired: i32,
    /// Unit price in cents.
    pub unit_price: i64,
    pub flags: u32,
}

/// One reservation entry as captured from Container+0x980.
#[derive(Clone, Debug, PartialEq)]
pub struct ReservationRecord {
    pub word: u32,
    pub reserver: u64,
    pub order: u64,
    pub ware: Option<WareIx>,
    pub amount: u32,
    pub expiry: f64,
    /// Linked trade order's amount (Trade+0x40) and flags (+0x54), when the capture has them
    /// and the order exists.
    pub linked: Option<(u32, u32)>,
}

#[derive(Debug)]
pub struct ParseError {
    pub line: usize,
    pub msg: String,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "line {}: {}", self.line, self.msg)
    }
}

impl std::error::Error for ParseError {}

struct Tok<'a> {
    it: std::str::SplitWhitespace<'a>,
    line: usize,
}

impl<'a> Tok<'a> {
    fn next(&mut self) -> Result<&'a str, ParseError> {
        self.it.next().ok_or_else(|| ParseError { line: self.line, msg: "missing field".into() })
    }
    fn err(&self, msg: &str) -> ParseError {
        ParseError { line: self.line, msg: msg.into() }
    }
    fn u64(&mut self) -> Result<u64, ParseError> {
        let s = self.next()?;
        let r = if let Some(h) = s.strip_prefix("0x") { u64::from_str_radix(h, 16) } else { s.parse() };
        r.map_err(|_| self.err("bad integer"))
    }
    fn i32(&mut self) -> Result<i32, ParseError> {
        self.next()?.parse().map_err(|_| self.err("bad i32"))
    }
    fn bool(&mut self) -> Result<bool, ParseError> {
        Ok(self.u64()? != 0)
    }
    /// f64 written as hex bits (`0x...`) or as a decimal literal (tests).
    fn f64(&mut self) -> Result<f64, ParseError> {
        let s = self.next()?;
        let v = if let Some(h) = s.strip_prefix("0x") {
            f64::from_bits(u64::from_str_radix(h, 16).map_err(|_| self.err("bad f64 bits"))?)
        } else {
            s.parse().map_err(|_| self.err("bad f64"))?
        };
        if v.is_nan() {
            return Err(self.err("NaN time or factor"));
        }
        Ok(v)
    }
    fn ware(&mut self) -> Result<WareIx, ParseError> {
        let v = self.u64()?;
        u16::try_from(v).map(WareIx).map_err(|_| self.err("ware index"))
    }
    fn list(&mut self) -> Result<Vec<WareAmount>, ParseError> {
        let n = self.u64()? as usize;
        let mut v = Vec::with_capacity(n);
        for _ in 0..n {
            v.push((self.ware()?, self.i32()?));
        }
        Ok(v)
    }
}

impl Capture {
    pub fn parse(text: &str) -> Result<Capture, ParseError> {
        let mut cap = Capture::default();
        let mut header = false;
        // Records embedded in an X4 debug log or stdout follow a "[X4LAN] ECONCAP " prefix; in
        // such a file every other line is log text and skipped.
        let embedded = text.contains("ECONCAP ");
        for (ln, line) in text.lines().enumerate() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let line = match line.find("ECONCAP ") {
                Some(p) => &line[p + 8..],
                None if embedded => continue,
                None => line,
            };
            let mut t = Tok { it: line.split_whitespace(), line: ln + 1 };
            let kind = t.next()?;
            match kind {
                "X4ECON" => {
                    if t.u64()? != 1 {
                        return Err(t.err("unsupported X4ECON version"));
                    }
                    for kv in t.it.by_ref() {
                        if let Some(b) = kv.strip_prefix("build=") {
                            cap.build = b.parse().map_err(|_| ParseError { line: ln + 1, msg: "build".into() })?;
                        }
                    }
                    header = true;
                }
                _ if !header => return Err(t.err("record before X4ECON header")),
                "W" => {
                    let id = t.next()?.to_string();
                    let volume = t.u64()? as u32;
                    let mut tags = Vec::new();
                    if let Some(n) = t.it.next() {
                        let n: usize = n.parse().map_err(|_| t.err("tag count"))?;
                        for _ in 0..n {
                            tags.push(t.u64()? as u32);
                        }
                    }
                    cap.ware_volumes.push(volume);
                    cap.wares.push(WareInfo { id, volume, tags });
                }
                "P" => {
                    let _base = t.next()?;
                    for q in t.it.by_ref() {
                        let v = u64::from_str_radix(q.trim_start_matches("0x"), 16)
                            .map_err(|_| ParseError { line: ln + 1, msg: "settings qword".into() })?;
                        cap.settings.push(v);
                    }
                }
                "D" => {
                    let _id = t.u64()?;
                    let time = t.f64()?;
                    let product = t.ware()?;
                    let amount = t.i32()?;
                    let special = t.bool()?;
                    let primary = t.list()?;
                    let secondary = t.list()?;
                    let tertiary = t.list()?;
                    cap.methods.push(Method { time, product, amount, primary, secondary, tertiary, special });
                }
                "F" => {
                    let frame = t.u64()?;
                    let time = t.f64()?;
                    let rng = t.u64()?;
                    cap.frames.push(Frame { frame, time, rng, stations: Vec::new() });
                }
                "C" => {
                    let fr = cap.frames.last_mut().ok_or_else(|| t.err("C before F"))?;
                    let uid = t.u64()?;
                    let last_update = t.f64()?;
                    let a28 = t.f64()?;
                    let a30 = t.f64()?;
                    let a4c = t.bool()?;
                    let _n = t.u64()?;
                    let cargo = t.list()?;
                    fr.stations.push(StationRecord {
                        uid,
                        times: ContainerTimes { last_update, a28, a30, a4c },
                        cargo,
                        modules: Vec::new(),
                        offers: Vec::new(),
                        reservations: Vec::new(),
                        allocation: None,
                        storage: Vec::new(),
                    });
                }
                "S" => {
                    let fr = cap.frames.last_mut().ok_or_else(|| t.err("S before F"))?;
                    let st = fr.stations.last_mut().ok_or_else(|| t.err("S before C"))?;
                    let _uid = t.u64()?;
                    let addr = t.u64()?;
                    let capacity = t.i32()?;
                    let n = t.u64()? as usize;
                    let mut tags = Vec::with_capacity(n);
                    for _ in 0..n {
                        tags.push(t.u64()? as u32);
                    }
                    let contents = t.list()?;
                    st.storage.push(StorageRecord { addr, capacity, tags, contents });
                }
                "A" => {
                    let fr = cap.frames.last_mut().ok_or_else(|| t.err("A before F"))?;
                    let st = fr.stations.last_mut().ok_or_else(|| t.err("A before C"))?;
                    let _uid = t.u64()?;
                    let valid = t.bool()?;
                    let amounts = t.list()?;
                    st.allocation = Some(Allocation { valid, amounts });
                }
                "O" => {
                    let fr = cap.frames.last_mut().ok_or_else(|| t.err("O before F"))?;
                    let st = fr.stations.last_mut().ok_or_else(|| t.err("O before C"))?;
                    let _uid = t.u64()?;
                    let list = t.u64()? as u8;
                    let trade = t.u64()?;
                    let w = t.next()?;
                    let ware =
                        if w == "-1" { None } else { Some(WareIx(w.parse::<u16>().map_err(|_| t.err("ware index"))?)) };
                    let seller = t.u64()?;
                    let buyer = t.u64()?;
                    let amount = t.i32()?;
                    let min_amount = t.i32()?;
                    let desired = t.i32()?;
                    let unit_price = t.next()?.parse::<i64>().map_err(|_| t.err("price"))?;
                    let flags = t.u64()? as u32;
                    st.offers.push(OfferRecord {
                        list,
                        trade,
                        ware,
                        seller,
                        buyer,
                        amount,
                        min_amount,
                        desired,
                        unit_price,
                        flags,
                    });
                }
                "V" => {
                    let fr = cap.frames.last_mut().ok_or_else(|| t.err("V before F"))?;
                    let st = fr.stations.last_mut().ok_or_else(|| t.err("V before C"))?;
                    let _uid = t.u64()?;
                    let word = t.u64()? as u32;
                    let reserver = t.u64()?;
                    let order = t.u64()?;
                    let w = t.next()?;
                    let ware =
                        if w == "-1" { None } else { Some(WareIx(w.parse::<u16>().map_err(|_| t.err("ware index"))?)) };
                    let amount = t.u64()? as u32;
                    let expiry = t.f64()?;
                    // Newer captures: the linked order's amount and flags (-1 = order not found).
                    let linked = match t.it.next() {
                        Some(a) => {
                            let a: i64 = a.parse().map_err(|_| t.err("order amount"))?;
                            let f = t.u64()? as u32;
                            (a >= 0).then_some((a as u32, f))
                        }
                        None => None,
                    };
                    st.reservations.push(ReservationRecord { word, reserver, order, ware, amount, expiry, linked });
                }
                "M" => {
                    let methods = cap.methods.clone();
                    let fr = cap.frames.last_mut().ok_or_else(|| t.err("M before F"))?;
                    let st = fr.stations.last_mut().ok_or_else(|| t.err("M before C"))?;
                    let uid = t.u64()?;
                    let addr = t.u64()?;
                    let functional = t.bool()?;
                    let raw = t.i32()?;
                    let state = State::from_raw(raw).ok_or_else(|| t.err("unknown production state"))?;
                    let start = t.f64()?;
                    let next = t.f64()?;
                    let paused_since = t.f64()?;
                    let paused_total = t.f64()?;
                    let item = t.i32()?;
                    let cycle = t.i32()?;
                    let factors = Factors {
                        time: t.f64()?,
                        product: t.f64()?,
                        primary: t.f64()?,
                        secondary: t.f64()?,
                        tertiary: t.f64()?,
                    };
                    let pending = t.list()?;
                    let n = t.u64()? as usize;
                    let mut items = Vec::with_capacity(n);
                    for _ in 0..n {
                        let ware = t.ware()?;
                        let m = t.next()?;
                        let method = if m == "-1" {
                            None
                        } else {
                            let k: usize = m.parse().map_err(|_| t.err("method index"))?;
                            Some(methods.get(k).cloned().ok_or_else(|| t.err("unknown method"))?)
                        };
                        items.push(Item {
                            ware,
                            method,
                            time_factor: t.f64()?,
                            product_factor: t.f64()?,
                            primary_factor: t.f64()?,
                            secondary_factor: t.f64()?,
                            tertiary_factor: t.f64()?,
                            cycles: t.i32()?,
                            repeat: t.bool()?,
                        });
                    }
                    st.modules.push((
                        uid,
                        Module {
                            addr,
                            functional,
                            state,
                            start,
                            next,
                            paused_since,
                            paused_total,
                            item,
                            cycle,
                            factors,
                            items,
                            pending,
                        },
                    ));
                }
                _ => {} // unknown record kinds are skipped (forward compatibility)
            }
        }
        if !header {
            return Err(ParseError { line: 0, msg: "no X4ECON header".into() });
        }
        Ok(cap)
    }
}

/// Environment for replaying one catch-up: the earlier record's cargo, unlimited storage,
/// efficiency factors from the later record (the factors do not change inside one catch-up
/// unless workforce or sunlight changed in between; such transitions show up as mismatches).
pub struct ReplayEnv {
    pub cargo: Vec<WareAmount>,
    /// `(module address, factors)` from the later record.
    pub factors: Vec<(u64, Factors)>,
    /// Product wares the station cannot store in this catch-up (storage decision taken from
    /// the recording, for captures without the allocation record).
    pub blocked: Vec<WareIx>,
    /// Production storage allocation: with it, can-store is the engine's allocation check
    /// 0x424b90 (`allocation - running amount >= product amount` for every product) and the
    /// pending flush stores `min(pending, allocation - running amount)` (0x8f15b0). The
    /// physical capacity check (see `storage`) comes on top.
    pub allocation: Option<Vec<WareAmount>>,
    /// Operational storage modules `(capacity, cargo tags)` and per-ware `(volume, tags)`
    /// (dense ware index), when captured. The physical check 0x356010 / fit 0x3faff0: a module
    /// accepts a ware when their tag sets intersect (0x806e10); a product fits when the free
    /// volume of the modules accepting it holds `amount * volume`. Modelled with the modules of
    /// one tag group pooled (the engine fits module by module, so the result can differ by the
    /// per-module rounding at the very edge); empty = not checked (older captures).
    pub storage: Vec<(i64, Vec<u32>)>,
    pub ware_info: Vec<(i64, Vec<u32>)>,
}

fn tags_meet(a: &[u32], b: &[u32]) -> bool {
    a.iter().any(|t| b.contains(t))
}

impl ReplayEnv {
    /// Bit set of the storage modules accepting `w`.
    fn group(&self, w: WareIx) -> u64 {
        let tags = self.ware_info.get(w.0 as usize).map(|i| i.1.as_slice()).unwrap_or(&[]);
        let mut g = 0u64;
        for (k, (_, st)) in self.storage.iter().enumerate().take(64) {
            if tags_meet(st, tags) {
                g |= 1 << k;
            }
        }
        g
    }

    fn volume(&self, w: WareIx) -> i64 {
        self.ware_info.get(w.0 as usize).map_or(1, |i| i.0)
    }

    /// Units of `w` that still fit physically, with `extra` (ware, amount) already placed;
    /// `None` when the capture has no storage modules.
    fn room(&self, w: WareIx, extra: &[WareAmount]) -> Option<i64> {
        if self.storage.is_empty() {
            return None;
        }
        let g = self.group(w);
        if g == 0 {
            return Some(0);
        }
        let capacity: i64 =
            (0..self.storage.len().min(64)).filter(|k| g >> k & 1 == 1).map(|k| self.storage[k].0).sum();
        let used: i64 = self
            .cargo
            .iter()
            .chain(extra.iter())
            .filter(|&&(x, _)| self.group(x) == g)
            .map(|&(x, a)| a as i64 * self.volume(x))
            .sum();
        let v = self.volume(w);
        Some(if v > 0 { (capacity - used).max(0) / v } else { i64::MAX })
    }

    fn free(&self, w: WareIx) -> Option<i32> {
        let alloc = self.allocation.as_ref()?;
        let a = alloc.iter().find(|e| e.0 == w).map_or(0, |e| e.1);
        Some(a.wrapping_sub(self.amount(w)))
    }
}

impl ReplayEnv {
    fn amount(&self, w: WareIx) -> i32 {
        self.cargo.iter().find(|e| e.0 == w).map_or(0, |e| e.1)
    }
    fn add(&mut self, w: WareIx, a: i32) {
        match self.cargo.iter_mut().find(|e| e.0 == w) {
            Some(e) => e.1 += a,
            None => self.cargo.push((w, a)),
        }
    }
}

impl StationEnv for ReplayEnv {
    fn efficiency(&mut self, m: &Module, _item: usize, _t: f64) -> Factors {
        self.factors.iter().find(|f| f.0 == m.addr).map_or(m.factors, |f| f.1)
    }
    fn can_store(&mut self, p: &[WareAmount]) -> bool {
        if self.allocation.is_some() {
            // 0x424b90: every product within the allocation left (signed i32 compare) ...
            if !p.iter().all(|&(w, a)| self.free(w).unwrap_or(0) >= a) {
                return false;
            }
            // ... and 0x356010: everything fits physically.
            let mut placed: Vec<WareAmount> = Vec::new();
            for &(w, a) in p {
                if let Some(room) = self.room(w, &placed) {
                    if room < a as i64 {
                        return false;
                    }
                }
                placed.push((w, a));
            }
            return true;
        }
        !p.iter().any(|&(w, a)| a > 0 && self.blocked.contains(&w))
    }
    fn store_fitting(&mut self, p: &[WareAmount], t: f64) -> Vec<WareAmount> {
        let fits: Vec<WareAmount> = if self.allocation.is_some() {
            let mut placed: Vec<WareAmount> = Vec::new();
            for &(w, a) in p {
                let f = self.free(w).unwrap_or(0);
                let mut take = if f < a { f } else { a };
                if let Some(room) = self.room(w, &placed) {
                    take = take.min(room.min(i32::MAX as i64) as i32);
                }
                if take > 0 {
                    placed.push((w, take));
                }
            }
            placed
        } else {
            p.iter().copied().filter(|&(w, _)| !self.blocked.contains(&w)).collect()
        };
        self.store(&fits, t)
    }
    fn has_resources(&mut self, r: &[WareAmount]) -> bool {
        r.iter().all(|&(w, a)| self.amount(w) >= a)
    }
    fn consume(&mut self, r: &[WareAmount], _t: f64) {
        for &(w, a) in r {
            self.add(w, -a);
        }
    }
    fn store(&mut self, p: &[WareAmount], _t: f64) -> Vec<WareAmount> {
        for &(w, a) in p {
            self.add(w, a);
        }
        p.to_vec()
    }
}

/// Random draws taken from the recording, per module (by engine address): a module that asks
/// once gets the draw that explains its recorded next time; a module that asks more than once
/// in one catch-up cannot be explained from two snapshots and makes the transition
/// RNG-dependent.
#[derive(Default)]
pub struct OracleRng {
    pub floats: std::collections::HashMap<u64, f32>,
    pub ints: std::collections::HashMap<u64, i32>,
    /// `(module address, kind, max)` in ask order; kind 0 = float, 1 = int.
    pub asks: Vec<(u64, u8, f32)>,
    current: u64,
}

impl ProductionRng for OracleRng {
    fn begin(&mut self, addr: u64) {
        self.current = addr;
    }
    fn float(&mut self, max: f32) -> f32 {
        self.asks.push((self.current, 0, max));
        self.floats.get(&self.current).copied().unwrap_or(0.0)
    }
    fn i32_below(&mut self, n: i32) -> i32 {
        self.asks.push((self.current, 1, n as f32));
        self.ints.get(&self.current).copied().unwrap_or(0)
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ReplayReport {
    pub transitions: u64,
    pub exact: u64,
    /// Matched after taking one random draw per retrying module from the recording (each draw
    /// checked to lie in `[0, max)`).
    pub exact_with_recorded_draws: u64,
    /// A module drew more than once (long lazy gap with repeated retries) or a random item
    /// choice: not decidable from two snapshots.
    pub rng_dependent: u64,
    /// Module fields matched, cargo did not: something other than production changed the
    /// cargo between the records (trades, scripts). Not counted as a model error.
    pub external: u64,
    /// Matched after taking the storage decision from the recording: the recorded module
    /// was retrying because its products could not be stored (counted here, not in `exact`).
    pub exact_with_storage_decision: u64,
    pub mismatched: u64,
    pub first_difference: Option<String>,
    /// Every mismatch, one line each (station, module, time, model vs engine fields).
    pub differences: Vec<String>,
}

/// Replays every catch-up in the capture (see the module documentation).
pub fn replay(cap: &Capture) -> ReplayReport {
    let mut rep = ReplayReport::default();
    let mut last: std::collections::HashMap<u64, StationRecord> = std::collections::HashMap::new();
    for fr in &cap.frames {
        for st in &fr.stations {
            if let Some(prev) = last.get(&st.uid) {
                if st.times.last_update != prev.times.last_update {
                    replay_one(prev, st, &cap.wares, &mut rep);
                }
            }
            last.insert(st.uid, st.clone());
        }
    }
    rep
}

struct Outcome {
    modules: Vec<Module>,
    times: ContainerTimes,
    cargo: Vec<WareAmount>,
    asks: Vec<(u64, u8, f32)>,
}

fn run_once(
    prev: &StationRecord,
    next: &StationRecord,
    rng: &mut OracleRng,
    blocked: &[WareIx],
    wares: &[WareInfo],
) -> Outcome {
    let mut times = prev.times;
    let mut modules: Vec<Module> = prev.modules.iter().map(|m| m.1.clone()).collect();
    // The catch-up recomputes an invalid allocation at its start, so the later record holds the
    // allocation it used; older captures have none.
    let allocation = next.allocation.as_ref().or(prev.allocation.as_ref()).map(|a| a.amounts.clone());
    let mut env = ReplayEnv {
        cargo: prev.cargo.clone(),
        factors: next.modules.iter().map(|m| (m.1.addr, m.1.factors)).collect(),
        blocked: blocked.to_vec(),
        allocation,
        storage: next.storage.iter().map(|m| (m.capacity as i64, m.tags.clone())).collect(),
        ware_info: wares.iter().map(|w| (w.volume as i64, w.tags.clone())).collect(),
    };
    update_production(&mut times, next.times.last_update, false, &mut modules, &mut env, rng);
    Outcome { modules, times, cargo: env.cargo, asks: std::mem::take(&mut rng.asks) }
}

enum Attempt {
    Exact { draws: bool },
    External,
    RngDependent,
    Mismatch(String),
}

/// One replay of a transition with the given storage decision.
fn attempt(prev: &StationRecord, next: &StationRecord, blocked: &[WareIx], wares: &[WareInfo]) -> Attempt {
    let mut rng = OracleRng::default();
    let mut out = run_once(prev, next, &mut rng, blocked, wares);
    let mut used_draws = false;
    if !out.asks.is_empty() {
        let mut per_module: std::collections::HashMap<u64, Vec<(u8, f32)>> = std::collections::HashMap::new();
        for &(addr, kind, max) in &out.asks {
            per_module.entry(addr).or_default().push((kind, max));
        }
        if per_module.values().any(|v| v.len() != 1 || v[0].0 != 0) {
            return Attempt::RngDependent;
        }
        // draw = recorded next - model next with a zero draw (exact in f32 here, see report).
        let mut oracle = OracleRng::default();
        for (addr, v) in &per_module {
            let max = v[0].1;
            let (Some(a), Some(b)) =
                (out.modules.iter().find(|m| m.addr == *addr), next.modules.iter().find(|m| m.1.addr == *addr))
            else {
                continue;
            };
            let d = (b.1.next - a.next) as f32;
            if !(d >= 0.0 && d < max.max(f32::MIN_POSITIVE)) {
                return Attempt::Mismatch(format!(
                    "station {:#x} module {:#x} at {}: implied retry draw {d} outside [0, {max})",
                    next.uid, addr, next.times.last_update
                ));
            }
            oracle.floats.insert(*addr, d);
        }
        out = run_once(prev, next, &mut oracle, blocked, wares);
        used_draws = true;
    }
    let mut cargo_model = out.cargo.clone();
    cargo_model.retain(|e| e.1 != 0);
    cargo_model.sort_by_key(|e| e.0);
    let mut cargo_rec = next.cargo.clone();
    cargo_rec.retain(|e| e.1 != 0);
    cargo_rec.sort_by_key(|e| e.0);
    let modules_equal = out.modules.len() == next.modules.len()
        && out.modules.iter().zip(next.modules.iter()).all(|(a, b)| module_eq(a, &b.1));
    let times_equal =
        out.times.a28.to_bits() == next.times.a28.to_bits() && out.times.a30.to_bits() == next.times.a30.to_bits();
    if modules_equal && times_equal && cargo_model == cargo_rec {
        Attempt::Exact { draws: used_draws }
    } else if modules_equal && times_equal {
        Attempt::External
    } else {
        Attempt::Mismatch(first_diff(next.uid, next.times.last_update, &out.modules, next))
    }
}

/// Product wares of the recorded modules that are retrying in "choosing item" because their
/// products could not be stored (state 4, no cycle, next time in the future of the record).
fn storage_blocked_wares(next: &StationRecord) -> Vec<WareIx> {
    let mut out = Vec::new();
    for (_, m) in &next.modules {
        if m.state == State::ChoosingItem && m.cycle == -1 && m.next > next.times.last_update {
            if let Some(item) = usize::try_from(m.item).ok().and_then(|i| m.items.get(i)) {
                if !out.contains(&item.ware) {
                    out.push(item.ware);
                }
            }
        }
    }
    out
}

fn replay_one(prev: &StationRecord, next: &StationRecord, wares: &[WareInfo], rep: &mut ReplayReport) {
    rep.transitions += 1;
    let first = attempt(prev, next, &[], wares);
    let blocked =
        if next.allocation.is_some() || prev.allocation.is_some() { Vec::new() } else { storage_blocked_wares(next) };
    let result = match first {
        Attempt::Exact { .. } | Attempt::External => first,
        _ if blocked.is_empty() => first,
        _ => match (attempt(prev, next, &blocked, wares), first) {
            (Attempt::Exact { .. }, _) => {
                rep.exact_with_storage_decision += 1;
                return;
            }
            (Attempt::External, _) => Attempt::External,
            (second @ Attempt::RngDependent, Attempt::Mismatch(_)) => second,
            (_, first) => first,
        },
    };
    match result {
        Attempt::Exact { draws: true } => rep.exact_with_recorded_draws += 1,
        Attempt::Exact { draws: false } => rep.exact += 1,
        Attempt::External => rep.external += 1,
        Attempt::RngDependent => rep.rng_dependent += 1,
        Attempt::Mismatch(line) => {
            rep.mismatched += 1;
            rep.first_difference.get_or_insert_with(|| line.clone());
            rep.differences.push(line);
        }
    }
}

fn module_eq(a: &Module, b: &Module) -> bool {
    a.state == b.state
        && a.start.to_bits() == b.start.to_bits()
        && a.next.to_bits() == b.next.to_bits()
        && a.paused_since.to_bits() == b.paused_since.to_bits()
        && a.item == b.item
        && a.cycle == b.cycle
        && a.pending == b.pending
        && a.items.len() == b.items.len()
}

fn first_diff(uid: u64, now: f64, model: &[Module], rec: &StationRecord) -> String {
    let mut s = String::new();
    for (i, (a, (muid, b))) in model.iter().zip(rec.modules.iter()).enumerate() {
        if !module_eq(a, b) {
            let _ = write!(
                s,
                "station {uid:#x} module #{i} {muid:#x} at {now}: model state={} start={} next={} item={} cycle={}; engine state={} start={} next={} item={} cycle={}",
                a.state.name(), a.start, a.next, a.item, a.cycle, b.state.name(), b.start, b.next, b.item, b.cycle
            );
            return s;
        }
    }
    let _ = write!(s, "station {uid:#x} at {now}: module count {} vs {}", model.len(), rec.modules.len());
    s
}

/// Writes one station record in the capture format (used by tests and by synthetic captures).
pub fn write_station(out: &mut String, st: &StationRecord, method_index: &dyn Fn(&Method) -> Option<usize>) {
    let b = |v: f64| format!("{:#x}", v.to_bits());
    let _ = write!(
        out,
        "C {:#x} {} {} {} {} {} {}",
        st.uid,
        b(st.times.last_update),
        b(st.times.a28),
        b(st.times.a30),
        st.times.a4c as u8,
        st.modules.len(),
        st.cargo.len()
    );
    for &(w, a) in &st.cargo {
        let _ = write!(out, " {} {}", w.0, a);
    }
    out.push('\n');
    if let Some(al) = &st.allocation {
        let _ = write!(out, "A {:#x} {} {}", st.uid, al.valid as u8, al.amounts.len());
        for &(w, a) in &al.amounts {
            let _ = write!(out, " {} {}", w.0, a);
        }
        out.push('\n');
    }
    for m in &st.storage {
        let _ = write!(out, "S {:#x} {:#x} {} {}", st.uid, m.addr, m.capacity, m.tags.len());
        for t in &m.tags {
            let _ = write!(out, " {t}");
        }
        let _ = write!(out, " {}", m.contents.len());
        for &(w, a) in &m.contents {
            let _ = write!(out, " {} {}", w.0, a);
        }
        out.push('\n');
    }
    for (uid, m) in &st.modules {
        let f = &m.factors;
        let _ = write!(
            out,
            "M {:#x} {:#x} {} {} {} {} {} {} {} {} {} {} {} {} {} {}",
            uid,
            m.addr,
            m.functional as u8,
            m.state as i32,
            b(m.start),
            b(m.next),
            b(m.paused_since),
            b(m.paused_total),
            m.item,
            m.cycle,
            b(f.time),
            b(f.product),
            b(f.primary),
            b(f.secondary),
            b(f.tertiary),
            m.pending.len()
        );
        for &(w, a) in &m.pending {
            let _ = write!(out, " {} {}", w.0, a);
        }
        let _ = write!(out, " {}", m.items.len());
        for it in &m.items {
            let mi = it.method.as_ref().and_then(method_index).map_or(-1i64, |k| k as i64);
            let _ = write!(
                out,
                " {} {} {} {} {} {} {} {} {}",
                it.ware.0,
                mi,
                b(it.time_factor),
                b(it.product_factor),
                b(it.primary_factor),
                b(it.secondary_factor),
                b(it.tertiary_factor),
                it.cycles,
                it.repeat as u8
            );
        }
        out.push('\n');
    }
}

/// Writes a method definition line.
pub fn write_method(out: &mut String, index: usize, m: &Method) {
    let _ = write!(out, "D {} {:#x} {} {} {}", index, m.time.to_bits(), m.product.0, m.amount, m.special as u8);
    for list in [&m.primary, &m.secondary, &m.tertiary] {
        let _ = write!(out, " {}", list.len());
        for &(w, a) in list.iter() {
            let _ = write!(out, " {} {}", w.0, a);
        }
    }
    out.push('\n');
}
