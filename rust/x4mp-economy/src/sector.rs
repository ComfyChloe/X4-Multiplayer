//! The per-sector economy world and a deterministic parallel runner.
//!
//! A [`SectorEconomy`] owns the stations of one sector (in engine tree order) and that
//! sector's [`OfferBook`]. One frame of the economy is:
//!
//! 1. **parallel phase**: every sector job, on any thread, catches up production of its own
//!    stations to the frame time, refreshes the offers of stations whose production asked for
//!    it (0x4070e0), and emits [`EconMsg`]s for effects on other sectors (reservations at
//!    another sector's station). Jobs read other sectors only through the [`OfferSnapshot`] of
//!    the last barrier;
//! 2. **barrier** (serial): messages of all sectors are applied in stamp order to the owners'
//!    reservation lists ([`crate::msg::apply_in_order`]), the owners whose reservations
//!    changed refresh their offers (once each, first request first), then the next snapshot
//!    is published.
//!
//! [`step_frame`] is the stand-in for `x4mp_sim::Universe::step` (rewrite-coord/interfaces.md)
//! used by the thread-invariance tests: the result is identical for any thread count.
//!
//! The storage model here ([`SimpleStorage`]: one volume capacity per station) is a stand-in
//! for the engine's storage checks (0x424b90 / 0x356010 / 0x420aa0), and the offer refresh
//! ([`Station::refresh_offers`]) a stand-in for `UpdateTradeOffers` 0x4079e0 (one sell offer
//! per ware in stock, target = capacity share). The production state machine
//! ([`crate::production`]), the reservation lists ([`crate::reservations`]), the
//! reservation totals and the price curve ([`crate::prices::stock_price`]) are the engine's.

use std::collections::HashMap;

use crate::ids::{OrderKey, SectorIx, Stamp, TradeId, UniverseId, WareIx};
use crate::market::{Offer, OfferBook, OfferSnapshot};
use crate::msg::{apply_in_order, BarrierResult, BarrierWorld, EconMsg, EconOp};
use crate::prices::{stock_price, WarePrices};
use crate::production::{update_production, ContainerTimes, Factors, Module, ProductionRng, StationEnv, WareAmount};
use crate::reservations::{Flags, ReservationEnv, ReservationList};

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

/// Ware data shared by all sectors (read-only during a round).
#[derive(Clone, Debug, Default)]
pub struct Wares {
    /// Volume per unit (dense [`WareIx`]); 1 when missing.
    pub volume: Vec<u32>,
    /// Reference prices (dense [`WareIx`]).
    pub prices: Vec<WarePrices>,
}

impl Wares {
    fn vol(&self, w: WareIx) -> i64 {
        self.volume.get(w.0 as usize).copied().unwrap_or(1).max(1) as i64
    }
}

/// One station: production modules, cargo, storage capacity, reservations.
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
    /// Container+0x980.
    pub reservations: ReservationList,
    /// Offers must be refreshed in the next sector job.
    pub offers_dirty: bool,
}

impl Station {
    /// Offer refresh: one sell offer per ware in stock. Stand-in for `UpdateTradeOffers`
    /// 0x4079e0 built from engine pieces: the stock it prices is `cargo + incoming -
    /// outgoing` reservations (0x4126d0, flag classes 0/0, as 0x403a60 asks), the price is
    /// the engine curve 0x34cde0; the target (engine: allocation and production needs) is a
    /// capacity share here and the amount `cargo - outgoing`.
    pub fn refresh_offers(
        &mut self,
        sector: SectorIx,
        now: f64,
        wares: &Wares,
        env: &dyn ReservationEnv,
        book: &mut OfferBook,
    ) {
        let (totals, _) = self.reservations.totals(now, false, false, env);
        let n = self.cargo.len().max(1) as i64;
        for &(w, a) in &self.cargo {
            let out = totals.outgoing.iter().find(|e| e.0 == w).map_or(0, |e| e.1 as i64);
            let inc = totals.incoming.iter().find(|e| e.0 == w).map_or(0, |e| e.1 as i64);
            let stock = (a as i64 + inc - out).clamp(i32::MIN as i64, i32::MAX as i64) as i32;
            let target = (self.capacity / n / wares.vol(w)).clamp(0, i32::MAX as i64) as i32;
            let unit_price = wares.prices.get(w.0 as usize).map_or(100, |p| stock_price(p, stock, target));
            book.upsert(Offer {
                id: TradeId((self.uid.0 << 16) | w.0 as u64),
                owner: self.uid,
                sector,
                key: self.key,
                list: 0,
                pos: w.0 as u32,
                ware: w,
                buy: false,
                amount: (a as i64 - out).max(0) as u32,
                unit_price,
                flags: 0,
                version: 0,
            });
        }
        self.offers_dirty = false;
    }
}

/// Stand-in storage model: one volume capacity, cargo as plain amounts.
pub struct SimpleStorage<'a> {
    pub cargo: &'a mut Vec<WareAmount>,
    pub capacity: i64,
    pub wares: &'a Wares,
    /// Production asked for an offer refresh (0x4070e0).
    pub refresh: bool,
}

impl SimpleStorage<'_> {
    fn used(&self) -> i64 {
        self.cargo.iter().map(|&(w, a)| a as i64 * self.vol(w)).sum()
    }
    fn vol(&self, w: WareIx) -> i64 {
        self.wares.vol(w)
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
    fn refresh_offers(&mut self, _reset_build_cache: bool) {
        self.refresh = true;
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
/// `amount` of the cheapest snapshot sell offer of `ware` owned by another sector, for
/// `duration` seconds (a stand-in for the AI trade search and trade order that the ai-orders
/// agent owns).
#[derive(Clone, Copy, Debug)]
pub struct Demand {
    pub ware: WareIx,
    pub amount: u32,
    pub period: u64,
    pub duration: f64,
}

impl SectorEconomy {
    /// Parallel phase of one frame for this sector only.
    pub fn run(
        &mut self,
        frame: u64,
        now: f64,
        wares: &Wares,
        snapshot: &OfferSnapshot,
        demand: Option<Demand>,
        env: &dyn ReservationEnv,
    ) -> Vec<EconMsg> {
        let mut out = Vec::new();
        for (rank, st) in self.stations.iter_mut().enumerate() {
            let mut storage = SimpleStorage { cargo: &mut st.cargo, capacity: st.capacity, wares, refresh: false };
            let mut rng = st.rng;
            update_production(&mut st.times, now, false, &mut st.modules, &mut storage, &mut rng);
            st.rng = rng;
            if storage.refresh || st.offers_dirty {
                st.refresh_offers(self.sector, now, wares, env, &mut self.book);
            }
            if let Some(d) = demand {
                if frame % d.period == (st.uid.0 % d.period) {
                    let pick = snapshot
                        .find(d.ware, false)
                        .filter(|o| o.sector != self.sector && o.amount >= d.amount)
                        .min_by_key(|o| (o.unit_price, o.sector, o.key, o.list, o.pos));
                    if let Some(o) = pick {
                        out.push(EconMsg {
                            stamp: Stamp {
                                frame,
                                phase: 3,
                                rank: ((self.sector.0 as u64) << 32) | rank as u64,
                                seq: 0,
                            },
                            time: now,
                            actor: st.uid,
                            seen: Some((o.id, o.version)),
                            op: EconOp::Reserve {
                                owner: o.owner,
                                typemask: 1,
                                ware: d.ware,
                                amount: d.amount,
                                duration: d.duration,
                                replace: false,
                                flags: Flags::default(),
                            },
                        });
                    }
                }
            }
        }
        out
    }
}

/// Serial view of all sectors for the barrier.
struct World<'a> {
    sectors: &'a mut [SectorEconomy],
    /// uid -> (sector index, station index)
    index: HashMap<UniverseId, (usize, usize)>,
}

impl World<'_> {
    fn new(sectors: &mut [SectorEconomy]) -> World<'_> {
        let mut index = HashMap::new();
        for (si, s) in sectors.iter().enumerate() {
            for (k, st) in s.stations.iter().enumerate() {
                index.insert(st.uid, (si, k));
            }
        }
        World { sectors, index }
    }
}

impl BarrierWorld for World<'_> {
    fn offer_version(&self, offer: TradeId) -> Option<u32> {
        let &(si, _) = self.index.get(&UniverseId(offer.0 >> 16))?;
        self.sectors[si].book.find_by_id(offer).map(|o| o.version)
    }
    fn reservations_mut(&mut self, owner: UniverseId) -> Option<&mut ReservationList> {
        let &(si, k) = self.index.get(&owner)?;
        Some(&mut self.sectors[si].stations[k].reservations)
    }
    fn touch_offers(&mut self, owner: UniverseId, ware: WareIx) {
        if let Some(&(si, _)) = self.index.get(&owner) {
            self.sectors[si].book.touch(owner, ware);
        }
    }
}

/// One frame for all sectors on `threads` worker threads, then the barrier. Returns the
/// barrier result (every message with its outcome in apply order, the refreshed owners) and
/// the new snapshot.
#[allow(clippy::too_many_arguments)]
pub fn step_frame(
    sectors: &mut [SectorEconomy],
    threads: usize,
    frame: u64,
    now: f64,
    wares: &Wares,
    snapshot: &OfferSnapshot,
    demand: Option<Demand>,
    env: &(dyn ReservationEnv + Sync),
) -> (BarrierResult, OfferSnapshot) {
    let threads = threads.max(1);
    let mut outboxes: Vec<Vec<EconMsg>> = vec![Vec::new(); sectors.len()];
    if threads == 1 {
        for (s, out) in sectors.iter_mut().zip(outboxes.iter_mut()) {
            *out = s.run(frame, now, wares, snapshot, demand, env);
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
                        *out = s.run(frame, now, wares, snapshot, demand, env);
                    }
                });
            }
        });
    }
    // Barrier: all messages in stamp order, then the refresh of the touched owners.
    let msgs: Vec<EconMsg> = outboxes.into_iter().flatten().collect();
    let mut world = World::new(sectors);
    let result = apply_in_order(msgs, &mut world, env);
    for owner in &result.refresh {
        if let Some(&(si, k)) = world.index.get(owner) {
            let s = &mut world.sectors[si];
            let sector = s.sector;
            s.stations[k].refresh_offers(sector, now, wares, env, &mut s.book);
        }
    }
    let snap = OfferSnapshot::build(frame, sectors.iter().map(|s| (s.sector, &s.book)));
    (result, snap)
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
        for st in &s.stations {
            for r in st.reservations.entries() {
                eat(((r.word as u64) << 32) | r.amount as u64);
                eat(r.reserver.0);
                eat(r.ware.0 as u64);
                eat(r.expiry.to_bits());
            }
        }
        for o in s.book.offers() {
            eat(o.id.0);
            eat(((o.amount as u64) << 32) | o.version as u64);
            eat(o.unit_price as u64);
        }
    }
    h
}
