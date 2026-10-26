//! Cross-sector economy effects as ordered messages, applied at the frame barrier.
//!
//! A sector job never writes another sector's economy state. A reservation at (or a release
//! from) a container owned by another sector becomes an [`EconMsg`] carrying
//!
//! - the [`Stamp`] of the effect in the original sequential execution, so the barrier can
//!   apply all messages in exactly that order whatever thread produced them;
//! - the thread time of the effect (the engine's `TLS+0x300` at the call), which the
//!   reservation list uses for expiry and duration;
//! - the offer the decision was based on and the `version` the actor saw in the
//!   [`crate::market::OfferSnapshot`].
//!
//! The barrier applies messages sorted by stamp to the owner's
//! [`ReservationList`] with the engine's semantics (`AddWareReservation` 0x4128c0,
//! `RemoveWareReservation` 0x413060; [`crate::reservations`]). A message whose offer changed
//! since the actor's snapshot (version mismatch) is **not** applied: in the original engine
//! that actor would have run after the change and might have decided differently, so the
//! result is an [`Outcome::Conflict`] that the caller resolves by re-running the actor
//! serially (the migration path of the AI driver). Counting conflicts is part of every
//! replay report. Messages that do not depend on what the actor saw (releases of the actor's
//! own reservation) carry `seen: None` and are always applied in order.
//!
//! The engine ends both calls with a forced offer refresh of the owner
//! (`UpdateTradeOffers(container, 1, 0)` 0x4079e0), synchronously. The barrier instead
//! collects the owners whose list changed ([`BarrierResult::refresh`], first request first,
//! each once) and the caller refreshes them before it publishes the next snapshot; until then
//! every offer of the owner for the changed ware gets a new version, so a second decision on
//! the same offer in the same round is a conflict. Coalescing is exact for the published
//! state when the refresh is a function of the owner's state at the barrier (it is in this
//! model; for the engine's refresh see reports/REWRITE-ECONOMY.md §6: its workforce update
//! draws random numbers once per due interval, not per call).

use crate::ids::{Stamp, TradeId, UniverseId, WareIx};
use crate::reservations::{Flags, ReservationEnv, ReservationList};

#[derive(Clone, Debug, PartialEq)]
pub enum EconOp {
    /// `AddWareReservation` 0x4128c0 at `owner` for the message's actor (plain reservation:
    /// type mask bit 0 = outgoing from the owner, bit 1 = incoming to it).
    Reserve {
        owner: UniverseId,
        typemask: u32,
        ware: WareIx,
        amount: u32,
        /// Seconds; [`crate::time::UNSET`] selects the default trade duration.
        duration: f64,
        replace: bool,
        flags: Flags,
    },
    /// `RemoveWareReservation` 0x413060 at `owner` for the message's actor.
    Release {
        owner: UniverseId,
        typemask: u32,
        ware: Option<WareIx>,
        /// Positive and below an entry's amount: reduce it; otherwise remove matching entries.
        amount: i64,
        flags: Flags,
    },
}

impl EconOp {
    pub fn owner(&self) -> UniverseId {
        match *self {
            EconOp::Reserve { owner, .. } | EconOp::Release { owner, .. } => owner,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct EconMsg {
    pub stamp: Stamp,
    /// Thread game time of the effect in the original execution.
    pub time: f64,
    /// The reserver.
    pub actor: UniverseId,
    /// Offer and offer version the decision was based on; `None` for effects independent of it.
    pub seen: Option<(TradeId, u32)>,
    pub op: EconOp,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Outcome {
    /// Applied; the per-ware reservation deltas in the engine's order (0x413480 calls).
    Applied { deltas: Vec<(WareIx, i64)> },
    /// The offer changed after the actor's snapshot.
    Conflict { seen: u32, current: u32 },
    /// The offer or the owner no longer exists.
    Missing,
}

/// What the barrier needs from the world (serial access to every sector).
pub trait BarrierWorld {
    /// Live version of an offer, `None` when it is gone.
    fn offer_version(&self, offer: TradeId) -> Option<u32>;
    /// The reservation list of a container, `None` when it is gone.
    fn reservations_mut(&mut self, owner: UniverseId) -> Option<&mut ReservationList>;
    /// A new version for every offer of `owner` for `ware` (its stock changed under them).
    fn touch_offers(&mut self, owner: UniverseId, ware: WareIx);
}

/// Result of one barrier.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BarrierResult {
    /// Every message with its outcome, in apply (stamp) order.
    pub log: Vec<(EconMsg, Outcome)>,
    /// Owners whose offers must be refreshed before the next snapshot, in first-request order.
    pub refresh: Vec<UniverseId>,
}

/// Barrier step: applies `msgs` in stamp order. `env` answers reserver and order lookups with
/// the live (barrier-time) state.
///
/// Equal stamps are a producer bug (two effects claiming the same position in the original
/// order); they are kept in input order and reported by `debug_assert`.
pub fn apply_in_order(mut msgs: Vec<EconMsg>, world: &mut dyn BarrierWorld, env: &dyn ReservationEnv) -> BarrierResult {
    msgs.sort_by(|a, b| a.stamp.cmp(&b.stamp));
    debug_assert!(msgs.windows(2).all(|w| w[0].stamp != w[1].stamp), "duplicate stamps");
    let mut out = BarrierResult { log: Vec::with_capacity(msgs.len()), refresh: Vec::new() };
    for m in msgs {
        let outcome = apply_one(&m, world, env, &mut out.refresh);
        out.log.push((m, outcome));
    }
    out
}

fn apply_one(
    m: &EconMsg,
    world: &mut dyn BarrierWorld,
    env: &dyn ReservationEnv,
    refresh: &mut Vec<UniverseId>,
) -> Outcome {
    if let Some((offer, seen)) = m.seen {
        match world.offer_version(offer) {
            None => return Outcome::Missing,
            Some(current) if current != seen => return Outcome::Conflict { seen, current },
            Some(_) => {}
        }
    }
    let owner = m.op.owner();
    let Some(list) = world.reservations_mut(owner) else { return Outcome::Missing };
    let change = match m.op {
        EconOp::Reserve { typemask, ware, amount, duration, replace, flags, .. } => {
            list.add(m.time, typemask, m.actor, ware, amount, duration, replace, flags, env)
        }
        EconOp::Release { typemask, ware, amount, flags, .. } => {
            list.remove(m.time, typemask, Some(m.actor), ware, amount, flags, env)
        }
    };
    let mut touched: Vec<WareIx> = Vec::new();
    for &(w, _) in &change.deltas {
        if !touched.contains(&w) {
            touched.push(w);
        }
    }
    for w in touched {
        world.touch_offers(owner, w);
    }
    if change.refresh_offers && !refresh.contains(&owner) {
        refresh.push(owner);
    }
    Outcome::Applied { deltas: change.deltas }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::time::UNSET;

    const ORE: WareIx = WareIx(1);

    /// One owner (uid 5) with one offer (id 1) for ORE.
    struct World {
        list: ReservationList,
        version: u32,
    }

    impl BarrierWorld for World {
        fn offer_version(&self, offer: TradeId) -> Option<u32> {
            (offer == TradeId(1)).then_some(self.version)
        }
        fn reservations_mut(&mut self, owner: UniverseId) -> Option<&mut ReservationList> {
            (owner == UniverseId(5)).then_some(&mut self.list)
        }
        fn touch_offers(&mut self, owner: UniverseId, ware: WareIx) {
            if owner == UniverseId(5) && ware == ORE {
                self.version += 1;
            }
        }
    }

    fn all(_: UniverseId) -> bool {
        true
    }

    fn reserve(rank: u64, actor: u64, seen: Option<(TradeId, u32)>, amount: u32) -> EconMsg {
        EconMsg {
            stamp: Stamp { frame: 1, phase: 2, rank, seq: 0 },
            time: 100.0,
            actor: UniverseId(actor),
            seen,
            op: EconOp::Reserve {
                owner: UniverseId(5),
                typemask: 1,
                ware: ORE,
                amount,
                duration: UNSET,
                replace: false,
                flags: Flags::default(),
            },
        }
    }

    #[test]
    fn applies_in_stamp_order_and_detects_stale_decisions() {
        let mut w = World { list: ReservationList::new(), version: 0 };
        // Produced out of order (worker 2 finished first); both saw version 0.
        let msgs = vec![reserve(9, 20, Some((TradeId(1), 0)), 30), reserve(3, 10, Some((TradeId(1), 0)), 50)];
        let r = apply_in_order(msgs, &mut w, &all);
        assert_eq!(r.log[0].0.actor, UniverseId(10));
        assert_eq!(r.log[0].1, Outcome::Applied { deltas: vec![(ORE, 50)] });
        assert_eq!(r.log[1].1, Outcome::Conflict { seen: 0, current: 1 });
        assert_eq!(r.refresh, vec![UniverseId(5)]);
        let e = w.list.entries();
        assert_eq!((e.len(), e[0].reserver, e[0].amount, e[0].expiry), (1, UniverseId(10), 50, 3700.0));
    }

    #[test]
    fn version_free_release_always_applies() {
        let mut w = World { list: ReservationList::new(), version: 0 };
        let release = EconMsg {
            stamp: Stamp { frame: 1, phase: 2, rank: 2, seq: 0 },
            time: 100.0,
            actor: UniverseId(10),
            seen: None,
            op: EconOp::Release {
                owner: UniverseId(5),
                typemask: 1,
                ware: Some(ORE),
                amount: 20,
                flags: Flags::default(),
            },
        };
        let msgs = vec![reserve(1, 10, Some((TradeId(1), 0)), 60), release, reserve(3, 11, Some((TradeId(1), 2)), 70)];
        let r = apply_in_order(msgs, &mut w, &all);
        assert_eq!(r.log[1].1, Outcome::Applied { deltas: vec![(ORE, -20)] });
        assert_eq!(r.log[2].1, Outcome::Applied { deltas: vec![(ORE, 70)] });
        let amounts: Vec<u32> = w.list.entries().iter().map(|e| e.amount).collect();
        assert_eq!(amounts, vec![40, 70]);
        // Unknown owner or offer.
        let mut gone = reserve(4, 12, Some((TradeId(2), 0)), 1);
        assert_eq!(apply_in_order(vec![gone.clone()], &mut w, &all).log[0].1, Outcome::Missing);
        gone.seen = None;
        gone.op = EconOp::Release { owner: UniverseId(6), typemask: 1, ware: None, amount: 0, flags: Flags::default() };
        assert_eq!(apply_in_order(vec![gone], &mut w, &all).log[0].1, Outcome::Missing);
    }
}
