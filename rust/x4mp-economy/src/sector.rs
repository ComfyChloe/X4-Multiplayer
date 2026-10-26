//! The per-sector economy world and a deterministic parallel runner.
//!
//! A [`SectorEconomy`] owns the stations of one sector (in engine tree order) and that
//! sector's [`OfferBook`]. One frame of the economy is:
//!
//! 1. **parallel phase**: every sector job, on any thread, catches up production of its own
//!    stations to the frame time, refreshes its own offers, and emits [`EconMsg`]s for effects
//!    on other sectors. Jobs read other sectors only through the [`OfferSnapshot`] of the last
//!    barrier;
//! 2. **barrier** (serial): messages of all sectors are applied in stamp order
//!    ([`crate::msg::apply_in_order`]), then the next snapshot is published.
//!
//! [`step_frame`] is the stand-in for `x4mp_sim::Universe::step` (rewrite-coord/interfaces.md)
//! used by the thread-invariance tests: the result is identical for any thread count.
//!
//! The storage model here ([`SimpleStorage`]: one volume capacity per station) is a stand-in
//! for the engine's storage checks (0x424b90 / 0x356010 / 0x420aa0), which are not yet
//! reverse engineered; the production state machine itself is the engine's
//! ([`crate::production`]).

use crate::ids::{OrderKey, SectorIx, Stamp, TradeId, UniverseId, WareIx};
use crate::market::{Offer, OfferBook, OfferSnapshot};
use crate::msg::{apply_in_order, EconMsg, Outcome};
use crate::production::{update_production, ContainerTimes, Factors, Module, ProductionRng, StationEnv, WareAmount};

/// Per-entity random stream with the engine generator (ecs-core `EngineLcg` semantics):
/// `s' = ror64(s * 0x5851F42D4C957F2D + 0x14057B7EF767814F, 30)`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EconRng(pub u64);

impl EconRng {
    /// FNV-1a over (session seed, entity id, purpose), like `x4mp_sim::rng::stream_seed`.
    pub fn for_entity(session: u64, uid: UniverseId, purpose: u32) -> Self {
        let mut h: u64 = 0xcbf2_9ce4_8422_2325;
        for b in session.to_le_bytes().iter().chain(uid.0.to_le_bytes().iter()).chain(purpose.to_le_bytes().iter()) {
            h ^= *b as u64;
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
        EconRng(h)
    }

    fn step(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(0x5851_F42D_4C95_7F2D).wrapping_add(0x1405_7B7E_F767_814F).rotate_right(30);
        self.0
    }
}

impl ProductionRng for EconRng {
    fn float(&mut self, max: f32) -> f32 {
        if !(max >= 0.0) {
            return 0.0;
        }
        ((self.step() as u32) as f32) * 2.328_306_4e-10_f32 * max
    }

    fn i32_below(&mut self, n: i32) -> i32 {
        if n <= 0 {
            return 0;
        }
        (self.step() % n as u64) as i32
    }
}

/// Volume per ware unit for the stand-in storage.
#[derive(Clone, Debug, Default)]
pub struct WareVolumes(pub Vec<u32>);

/// One station: production modules, cargo, storage capacity.
#[derive(Clone, Debug, PartialEq)]
pub struct Station {
    pub uid: UniverseId,
    pub key: OrderKey,
    pub times: ContainerTimes,
    pub modules: Vec<Module>,
    /// Cargo amounts, sorted by ware.
    pub cargo: Vec<WareAmount>,
    pub capacity: i64,
    pub rng: EconRng,
}

/// Stand-in storage model: one volume capacity, cargo as plain amounts.
pub struct SimpleStorage<'a> {
    pub cargo: &'a mut Vec<WareAmount>,
    pub capacity: i64,
    pub volumes: &'a WareVolumes,
}

impl SimpleStorage<'_> {
    fn used(&self) -> i64 {
        self.cargo.iter().map(|&(w, a)| a as i64 * self.vol(w)).sum()
    }
    fn vol(&self, w: WareIx) -> i64 {
        self.volumes.0.get(w.0 as usize).copied().unwrap_or(1) as i64
    }
    fn amount(&self, w: WareIx) -> i32 {
        self.cargo.iter().find(|e| e.0 == w).map_or(0, |e| e.1)
    }
    fn add(&mut self, w: WareIx, a: i32) {
        match self.cargo.binary_search_by_key(&w, |e| e.0) {
            Ok(i) => self.cargo[i].1 += a,
            Err(i) => self.cargo.insert(i, (w, a)),
        }
    }
}

impl StationEnv for SimpleStorage<'_> {
    fn efficiency(&mut self, _m: &Module, _i: usize, _t: f64) -> Factors {
        Factors::default()
    }
    fn can_store(&mut self, products: &[WareAmount]) -> bool {
        let need: i64 = products.iter().map(|&(w, a)| a as i64 * self.vol(w)).sum();
        self.used() + need <= self.capacity
    }
    fn has_resources(&mut self, resources: &[WareAmount]) -> bool {
        resources.iter().all(|&(w, a)| self.amount(w) >= a)
    }
    fn consume(&mut self, resources: &[WareAmount], _t: f64) {
        for &(w, a) in resources {
            self.add(w, -a);
        }
    }
    fn store(&mut self, products: &[WareAmount], _t: f64) -> Vec<WareAmount> {
        for &(w, a) in products {
            self.add(w, a);
        }
        products.to_vec()
    }
}

/// The economy of one sector.
#[derive(Clone, Debug, PartialEq)]
pub struct SectorEconomy {
    pub sector: SectorIx,
    pub stations: Vec<Station>,
    pub book: OfferBook,
}

/// A cross-sector demand used by the tests: every `period` frames each station reserves
/// `amount` of the cheapest snapshot offer of `ware` owned by another sector (a stand-in for
/// the AI trade search that the ai-orders agent owns).
#[derive(Clone, Copy, Debug)]
pub struct Demand {
    pub ware: WareIx,
    pub amount: u32,
    pub period: u64,
}

impl SectorEconomy {
    /// Parallel phase of one frame for this sector only.
    pub fn run(
        &mut self,
        frame: u64,
        now: f64,
        volumes: &WareVolumes,
        snapshot: &OfferSnapshot,
        demand: Option<Demand>,
    ) -> Vec<EconMsg> {
        let mut out = Vec::new();
        for (rank, st) in self.stations.iter_mut().enumerate() {
            let mut env = SimpleStorage { cargo: &mut st.cargo, capacity: st.capacity, volumes };
            let mut rng = st.rng;
            update_production(&mut st.times, now, false, &mut st.modules, &mut env, &mut rng);
            st.rng = rng;
            // Sell offers for everything in stock (stand-in for UpdateTradeOffers 0x4079e0).
            for &(w, a) in st.cargo.iter() {
                let id = TradeId((st.uid.0 << 16) | w.0 as u64);
                let reserved = self.book.find_by_id(id).map_or(0, |o| o.reserved);
                self.book.upsert(Offer {
                    id,
                    owner: st.uid,
                    sector: self.sector,
                    key: st.key,
                    list: 1,
                    pos: w.0 as u32,
                    ware: w,
                    buy: false,
                    amount: a.max(0) as u32,
                    reserved,
                    unit_price: 100 + w.0 as i64,
                    flags: 0,
                    version: 0,
                });
            }
            if let Some(d) = demand {
                if frame % d.period == (st.uid.0 % d.period) {
                    let pick = snapshot
                        .find(d.ware, false)
                        .filter(|o| o.sector != self.sector && o.available() >= d.amount)
                        .min_by_key(|o| (o.unit_price, o.sector, o.key, o.list, o.pos));
                    if let Some(o) = pick {
                        out.push(EconMsg {
                            stamp: Stamp {
                                frame,
                                phase: 3,
                                rank: ((self.sector.0 as u64) << 32) | rank as u64,
                                seq: 0,
                            },
                            actor: st.uid,
                            seen_version: Some(o.version),
                            op: crate::msg::EconOp::Reserve { offer: o.id, amount: d.amount },
                        });
                    }
                }
            }
        }
        out
    }
}

/// One frame for all sectors on `threads` worker threads, then the barrier. Returns the
/// barrier log (every message with its outcome, in apply order) and the new snapshot.
pub fn step_frame(
    sectors: &mut [SectorEconomy],
    threads: usize,
    frame: u64,
    now: f64,
    volumes: &WareVolumes,
    snapshot: &OfferSnapshot,
    demand: Option<Demand>,
) -> (Vec<(EconMsg, Outcome)>, OfferSnapshot) {
    let threads = threads.max(1);
    let mut outboxes: Vec<Vec<EconMsg>> = vec![Vec::new(); sectors.len()];
    if threads == 1 {
        for (s, out) in sectors.iter_mut().zip(outboxes.iter_mut()) {
            *out = s.run(frame, now, volumes, snapshot, demand);
        }
    } else {
        // Static interleaved partition; the result must not depend on it.
        std::thread::scope(|scope| {
            let mut chunks: Vec<Vec<(&mut SectorEconomy, &mut Vec<EconMsg>)>> =
                (0..threads).map(|_| Vec::new()).collect();
            for (i, pair) in sectors.iter_mut().zip(outboxes.iter_mut()).enumerate() {
                chunks[i % threads].push(pair);
            }
            for chunk in chunks {
                scope.spawn(move || {
                    for (s, out) in chunk {
                        *out = s.run(frame, now, volumes, snapshot, demand);
                    }
                });
            }
        });
    }
    // Barrier: all messages, applied in stamp order to the sector books.
    let msgs: Vec<EconMsg> = outboxes.into_iter().flatten().collect();
    let mut books: Vec<OfferBook> = sectors.iter_mut().map(|s| std::mem::take(&mut s.book)).collect();
    let locate = |id: TradeId| books_owner(sectors, id);
    let log = apply_in_order(msgs, &mut books, &locate);
    for (s, b) in sectors.iter_mut().zip(books) {
        s.book = b;
    }
    let snap = OfferSnapshot::build(frame, sectors.iter().map(|s| (s.sector, &s.book)));
    (log, snap)
}

fn books_owner(sectors: &[SectorEconomy], id: TradeId) -> Option<SectorIx> {
    let owner = UniverseId(id.0 >> 16);
    sectors.iter().find(|s| s.stations.iter().any(|st| st.uid == owner)).map(|s| s.sector)
}

/// FNV-1a over every economy field of all sectors in sector and tree order (thread-invariance).
pub fn state_hash(sectors: &[SectorEconomy]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    let mut eat = |v: u64| {
        for b in v.to_le_bytes() {
            h ^= b as u64;
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
    };
    for s in sectors {
        eat(s.sector.0 as u64);
        for st in &s.stations {
            eat(st.uid.0);
            eat(st.times.last_update.to_bits());
            eat(st.times.a28.to_bits());
            eat(st.times.a30.to_bits());
            eat(st.rng.0);
            for &(w, a) in &st.cargo {
                eat(((w.0 as u64) << 32) | a as u32 as u64);
            }
            for m in &st.modules {
                eat(m.state as i32 as u64);
                eat(m.start.to_bits());
                eat(m.next.to_bits());
                eat(m.item as u32 as u64);
                eat(m.cycle as u32 as u64);
                for &(w, a) in &m.pending {
                    eat(((w.0 as u64) << 32) | a as u32 as u64);
                }
            }
        }
        for o in s.book.offers() {
            eat(o.id.0);
            eat(((o.amount as u64) << 32) | o.reserved as u64);
            eat(o.version as u64);
        }
    }
    h
}
