//! The economy as an x4mp-sim system: station components and the `economy` system.
//!
//! Every station is an x4mp-sim entity (rewrite-coord/interfaces.md v0.5) with two economy
//! components:
//! - [`EconStation`]: production modules, catch-up times, cargo, storage capacity and the
//!   reservation list (Container+0x980). Sector-local: only the station's own sector job and
//!   the barrier write it.
//! - [`EconOffers`]: the station's trade offers, **published**: other sectors' jobs read them
//!   from the snapshot of the last barrier (`Snapshot::column`), never live.
//!
//! Production draws come from the entity's own engine-generator stream (x4mp-sim `RngState`,
//! seeded `rng::entity_seed(session, uid, 0)` at spawn), so they do not depend on threads.
//!
//! One frame of [`EconomySystem`] (x4mp-sim frame model):
//! 1. **parallel phase** (`run_sector`): each sector job catches up production of its
//!    stations to the frame time, refreshes the offers of stations whose production asked for
//!    it (0x4070e0), and sends a reservation at another sector's station as an [`EconMsg`] to
//!    `Target::Barrier`;
//! 2. **barrier** (serial): the messages arrive in x4mp-sim total order (order key, sector,
//!    emission seq; key 0 = tree order) and are applied to the owners' reservation lists
//!    ([`crate::msg::apply_ordered`], engine semantics). The owners whose lists changed refresh
//!    their offers once each, first request first, before x4mp-sim takes the next snapshot.
//!
//! Stand-ins, until their engine functions are modelled: storage is one volume capacity per
//! station ([`SimpleStorage`]; engine 0x424b90 / 0x356010 / 0x420aa0); the offer refresh
//! ([`refresh_offers`]) puts one sell offer per ware in stock, priced with the engine curve
//! 0x34cde0 at a capacity-share target (engine `UpdateTradeOffers` 0x4079e0); [`Demand`] is a
//! synthetic trade search (the AI trade search belongs to ai-orders). The production state
//! machine, the reservation lists and totals and the price curve are the engine's.

use std::collections::HashMap;
use std::sync::Mutex;

use x4mp_sim::comp::RngState;
use x4mp_sim::rng::EngineLcg;
use x4mp_sim::{BarrierCtx, Component, EntityId, SectorCtx, System, Target, Universe};

use crate::ids::{OrderKey, SectorIx, Stamp, TradeId, UniverseId, WareIx};
use crate::market::{Offer, OfferBook};
use crate::msg::{apply_ordered, BarrierWorld, EconMsg, EconOp};
use crate::prices::{stock_price, WarePrices};
use crate::production::{update_production, ContainerTimes, Factors, Module, ProductionRng, StationEnv, WareAmount};
use crate::reservations::{Flags, ReservationEnv, ReservationList};

/// Production draws from the engine generator of x4mp-sim (an entity's stream).
impl ProductionRng for EngineLcg {
    fn float(&mut self, max: f32) -> f32 {
        EngineLcg::float(self, max)
    }

    fn i32_below(&mut self, n: i32) -> i32 {
        EngineLcg::i32_below(self, n)
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

/// Economy state of one station entity.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct EconStation {
    pub times: ContainerTimes,
    pub modules: Vec<Module>,
    /// Cargo amounts, sorted by ware.
    pub cargo: Vec<WareAmount>,
    /// Stand-in storage capacity (volume).
    pub capacity: i64,
    /// Container+0x980.
    pub reservations: ReservationList,
    /// Offers must be refreshed in the next sector job.
    pub offers_dirty: bool,
}
x4mp_sim::impl_fields!(EconStation { times, modules, cargo, capacity, reservations, offers_dirty });
impl Component for EconStation {
    const NAME: &'static str = "econ_station";
}

/// The station's trade offers (published column).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct EconOffers(pub OfferBook);
x4mp_sim::impl_fields!(newtype EconOffers);
impl Component for EconOffers {
    const NAME: &'static str = "econ_offers";
}

/// Registers the economy components on a universe (offers published).
pub fn register(u: &mut Universe) {
    u.register::<EconStation>();
    u.publish::<EconOffers>();
}

/// Offer refresh of one station: one sell offer per ware in stock. Stand-in for
/// `UpdateTradeOffers` 0x4079e0 built from engine pieces: the stock it prices is
/// `cargo + incoming - outgoing` reservations (0x4126d0, flag classes 0/0, as 0x403a60 asks),
/// the price is the engine curve 0x34cde0; the target (engine: allocation and production
/// needs) is a capacity share here and the amount `cargo - outgoing`.
#[allow(clippy::too_many_arguments)]
pub fn refresh_offers(
    st: &mut EconStation,
    uid: UniverseId,
    key: OrderKey,
    sector: SectorIx,
    now: f64,
    wares: &Wares,
    env: &dyn ReservationEnv,
    book: &mut OfferBook,
) {
    let (totals, _) = st.reservations.totals(now, false, false, env);
    let n = st.cargo.len().max(1) as i64;
    for &(w, a) in &st.cargo {
        let out = totals.outgoing.iter().find(|e| e.0 == w).map_or(0, |e| e.1 as i64);
        let inc = totals.incoming.iter().find(|e| e.0 == w).map_or(0, |e| e.1 as i64);
        let stock = (a as i64 + inc - out).clamp(i32::MIN as i64, i32::MAX as i64) as i32;
        let target = (st.capacity / n / wares.vol(w)).clamp(0, i32::MAX as i64) as i32;
        let unit_price = wares.prices.get(w.0 as usize).map_or(100, |p| stock_price(p, stock, target));
        book.upsert(Offer {
            id: TradeId((uid.0 << 16) | w.0 as u64),
            owner: uid,
            sector,
            key,
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
    st.offers_dirty = false;
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

/// The economy system. `env` answers reserver and order lookups (the tests use "everyone
/// exists"); `log` collects one line per barrier message and refresh (tests and replays).
pub struct EconomySystem {
    pub wares: Wares,
    pub demand: Option<Demand>,
    pub env: Box<dyn ReservationEnv + Send + Sync>,
    pub log: Mutex<Vec<String>>,
}

/// Every reserver exists, no linked orders (the synthetic world of the tests).
pub struct EveryoneExists;
impl ReservationEnv for EveryoneExists {
    fn alive(&self, _r: UniverseId) -> bool {
        true
    }
}

impl EconomySystem {
    pub fn new(wares: Wares, demand: Option<Demand>) -> Self {
        EconomySystem { wares, demand, env: Box::new(EveryoneExists), log: Mutex::new(Vec::new()) }
    }
}

impl System for EconomySystem {
    fn name(&self) -> &'static str {
        "economy"
    }

    fn run_sector(&self, ctx: &mut SectorCtx<'_>) {
        let now = ctx.frame.game_time;
        let frame = ctx.frame.frame;
        let sector = ctx.sector;
        let n = ctx.world.len();
        for row in 0..n {
            let uid = ctx.world.uids()[row];
            let key = ctx.world.keys()[row];
            {
                let (stations, offers, rngs) = ctx.world.cols_mut3::<EconStation, EconOffers, RngState>();
                let st = &mut stations[row];
                let rng = rngs[row].lcg();
                let mut storage =
                    SimpleStorage { cargo: &mut st.cargo, capacity: st.capacity, wares: &self.wares, refresh: false };
                update_production(&mut st.times, now, false, &mut st.modules, &mut storage, rng);
                if storage.refresh || st.offers_dirty {
                    refresh_offers(st, uid, key, sector, now, &self.wares, self.env.as_ref(), &mut offers[row].0);
                }
            }
            let Some(d) = self.demand else { continue };
            if frame % d.period != uid.0 % d.period {
                continue;
            }
            // The cheapest sell offer of another sector in the last barrier's snapshot.
            let mut pick: Option<&Offer> = None;
            let mut s = 0u32;
            while let Some(col) = ctx.snapshot.column::<EconOffers>(SectorIx(s)) {
                if SectorIx(s) != sector {
                    for o in col.iter().flat_map(|b| b.0.offers()) {
                        if o.ware == d.ware && !o.buy && o.amount >= d.amount {
                            let better = pick.is_none_or(|p| {
                                (o.unit_price, o.sector, o.key, o.list, o.pos)
                                    < (p.unit_price, p.sector, p.key, p.list, p.pos)
                            });
                            if better {
                                pick = Some(o);
                            }
                        }
                    }
                }
                s += 1;
            }
            if let Some(o) = pick {
                let msg = EconMsg {
                    stamp: Stamp { frame, phase: 3, rank: ((sector.0 as u64) << 32) | row as u64, seq: 0 },
                    time: now,
                    actor: uid,
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
                };
                ctx.out.send(Target::Barrier, msg);
            }
        }
    }

    fn barrier(&self, ctx: &mut BarrierCtx<'_>) {
        let msgs: Vec<&EconMsg> = ctx.messages.iter().filter_map(|e| e.get::<EconMsg>()).collect();
        if msgs.is_empty() {
            return;
        }
        let frame = ctx.frame.frame;
        let now = ctx.frame.game_time;
        let mut world = UniverseWorld::new(ctx.universe);
        let result = apply_ordered(msgs.iter().copied(), &mut world, self.env.as_ref());
        let mut log = self.log.lock().unwrap_or_else(|p| p.into_inner());
        for (m, o) in &result.log {
            log.push(format!("{frame} {:?} {:?} {:?} {:?}", m.stamp, m.actor, m.op, o));
        }
        for owner in &result.refresh {
            if let Some(e) = world.entity(*owner) {
                world.refresh(e, now, &self.wares, self.env.as_ref());
            }
        }
        log.push(format!("{frame} refresh {:?}", result.refresh));
    }
}

/// The barrier's view of the universe.
struct UniverseWorld<'a> {
    universe: &'a mut Universe,
    cache: HashMap<UniverseId, Option<EntityId>>,
}

impl<'a> UniverseWorld<'a> {
    fn new(universe: &'a mut Universe) -> Self {
        UniverseWorld { universe, cache: HashMap::new() }
    }

    fn entity(&mut self, uid: UniverseId) -> Option<EntityId> {
        let u = &*self.universe;
        *self.cache.entry(uid).or_insert_with(|| u.ids().by_uid(uid))
    }

    fn refresh(&mut self, e: EntityId, now: f64, wares: &Wares, env: &dyn ReservationEnv) {
        let Some(loc) = self.universe.ids().loc(e) else { return };
        let uid = self.universe.ids().uid(e).unwrap_or_default();
        let key = self.universe.world(loc.sector).keys()[loc.row as usize];
        let Some(mut st) = self.universe.get::<EconStation>(e).cloned() else { return };
        let Some(mut book) = self.universe.get::<EconOffers>(e).map(|o| o.0.clone()) else { return };
        refresh_offers(&mut st, uid, key, loc.sector, now, wares, env, &mut book);
        if let Some(s) = self.universe.get_mut::<EconStation>(e) {
            *s = st;
        }
        if let Some(o) = self.universe.get_mut::<EconOffers>(e) {
            o.0 = book;
        }
    }
}

impl BarrierWorld for UniverseWorld<'_> {
    fn offer_version(&mut self, owner: UniverseId, offer: TradeId) -> Option<u32> {
        let e = self.entity(owner)?;
        self.universe.get::<EconOffers>(e)?.0.find_by_id(offer).map(|o| o.version)
    }
    fn reservations_mut(&mut self, owner: UniverseId) -> Option<&mut ReservationList> {
        let e = self.entity(owner)?;
        self.universe.get_mut::<EconStation>(e).map(|s| &mut s.reservations)
    }
    fn touch_offers(&mut self, owner: UniverseId, ware: WareIx) {
        if let Some(e) = self.entity(owner) {
            if let Some(o) = self.universe.get_mut::<EconOffers>(e) {
                o.0.touch(owner, ware);
            }
        }
    }
}

/// Outcome counts of a barrier log (tests and reports).
pub fn count_outcomes(log: &[String]) -> (usize, usize) {
    let applied = log.iter().filter(|l| l.contains("Applied")).count();
    let conflicts = log.iter().filter(|l| l.contains("Conflict")).count();
    (applied, conflicts)
}

#[cfg(test)]
mod tests {
    use super::*;
    use x4mp_sim::rng::entity_seed;
    use x4mp_sim::{Bundle, WorldKind};

    #[test]
    fn stations_draw_from_their_x4mp_sim_entity_stream() {
        // interfaces.md v0.5 test vector, pinned here so a seeding change is noticed.
        assert_eq!(entity_seed(0x1081d6b6, UniverseId(0x2000001), 0), 0x70b7_3a84_370b_21a0);
        let mut u = Universe::new(0x1081d6b6);
        register(&mut u);
        let s = u.add_sector(UniverseId(0x7000_0000), WorldKind::Sector);
        let e = u.spawn(s, UniverseId(0x2000001), Bundle::new().with(EconStation::default())).expect("spawn");
        u.commit();
        assert_eq!(u.get::<RngState>(e).map(|r| r.0), Some(0x70b7_3a84_370b_21a0));
        assert!(u.get::<EconOffers>(e).is_some_and(|o| o.0.is_empty()));
    }
}
