//! Cross-sector economy effects as ordered messages, applied at the frame barrier.
//!
//! A sector job never writes another sector's economy state. A reservation against (or a
//! release of) an offer owned by another sector becomes an [`EconMsg`] carrying
//!
//! - the [`Stamp`] of the effect in the original sequential execution, so the barrier can
//!   apply all messages in exactly that order whatever thread produced them, and
//! - the offer `version` the actor saw in the [`crate::market::OfferSnapshot`].
//!
//! The barrier applies messages sorted by stamp. A message whose offer changed since the
//! actor's snapshot (version mismatch) is **not** applied: in the original engine that actor
//! would have run after the change and might have decided differently, so the result is a
//! [`Outcome::Conflict`] that the caller must resolve by re-running the actor serially (the
//! migration path of the AI driver). Counting conflicts is part of every replay report.
//! Messages that do not depend on what the actor saw (releases of the actor's own
//! reservation) carry `seen_version: None` and are always applied in order.

use crate::ids::{SectorIx, Stamp, TradeId, UniverseId};
use crate::market::OfferBook;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EconOp {
    /// Reserve `amount` of an offer for `actor` (engine: trade order creation, MD/AI
    /// `add_ware_reservation`).
    Reserve { offer: TradeId, amount: u32 },
    /// Release up to `amount` previously reserved by `actor`.
    Release { offer: TradeId, amount: u32 },
}

impl EconOp {
    pub fn offer(&self) -> TradeId {
        match *self {
            EconOp::Reserve { offer, .. } | EconOp::Release { offer, .. } => offer,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EconMsg {
    pub stamp: Stamp,
    pub actor: UniverseId,
    /// Offer version the decision was based on; `None` for effects independent of it.
    pub seen_version: Option<u32>,
    pub op: EconOp,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    Applied,
    /// The offer changed after the actor's snapshot.
    Conflict {
        seen: u32,
        current: u32,
    },
    /// The offer no longer exists.
    Missing,
    /// Not enough left to reserve.
    Insufficient {
        available: u32,
    },
}

/// Where an offer lives at the barrier.
pub trait Locate {
    fn sector_of(&self, offer: TradeId) -> Option<SectorIx>;
}

impl<F: Fn(TradeId) -> Option<SectorIx>> Locate for F {
    fn sector_of(&self, offer: TradeId) -> Option<SectorIx> {
        self(offer)
    }
}

/// Barrier step: applies `msgs` to the sector books in stamp order and returns every message
/// with its outcome, in that order. `books[i]` is the book of `SectorIx(i)`.
///
/// Equal stamps are a producer bug (two effects claiming the same position in the original
/// order); they are kept in input order and reported by `debug_assert`.
pub fn apply_in_order(mut msgs: Vec<EconMsg>, books: &mut [OfferBook], locate: &dyn Locate) -> Vec<(EconMsg, Outcome)> {
    msgs.sort_by(|a, b| a.stamp.cmp(&b.stamp));
    debug_assert!(msgs.windows(2).all(|w| w[0].stamp != w[1].stamp), "duplicate stamps");
    let mut out = Vec::with_capacity(msgs.len());
    for m in msgs {
        let outcome = apply_one(&m, books, locate);
        out.push((m, outcome));
    }
    out
}

fn apply_one(m: &EconMsg, books: &mut [OfferBook], locate: &dyn Locate) -> Outcome {
    let id = m.op.offer();
    let Some(sector) = locate.sector_of(id) else { return Outcome::Missing };
    let Some(book) = books.get_mut(sector.0 as usize) else { return Outcome::Missing };
    let Some(offer) = book.find_by_id_mut(id) else { return Outcome::Missing };
    if let Some(seen) = m.seen_version {
        if seen != offer.version {
            return Outcome::Conflict { seen, current: offer.version };
        }
    }
    match m.op {
        EconOp::Reserve { amount, .. } => {
            let available = offer.available();
            if amount > available {
                return Outcome::Insufficient { available };
            }
            offer.reserved += amount;
        }
        EconOp::Release { amount, .. } => {
            offer.reserved = offer.reserved.saturating_sub(amount);
        }
    }
    offer.version = offer.version.wrapping_add(1);
    Outcome::Applied
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ids::{OrderKey, WareIx};
    use crate::market::Offer;

    fn book_with(id: u64, amount: u32) -> OfferBook {
        let mut b = OfferBook::new();
        b.upsert(Offer {
            id: TradeId(id),
            owner: UniverseId(5),
            sector: SectorIx(0),
            key: OrderKey(1),
            ware: WareIx(0),
            buy: false,
            amount,
            reserved: 0,
            unit_price: 100,
            version: 0,
        });
        b
    }

    fn stamp(rank: u64) -> Stamp {
        Stamp { frame: 1, phase: 2, rank, seq: 0 }
    }

    #[test]
    fn applies_in_stamp_order_and_detects_stale_decisions() {
        let mut books = vec![book_with(1, 100)];
        let locate = |_: TradeId| Some(SectorIx(0));
        // Produced out of order (worker 2 finished first); both saw version 0.
        let msgs = vec![
            EconMsg {
                stamp: stamp(9),
                actor: UniverseId(20),
                seen_version: Some(0),
                op: EconOp::Reserve { offer: TradeId(1), amount: 30 },
            },
            EconMsg {
                stamp: stamp(3),
                actor: UniverseId(10),
                seen_version: Some(0),
                op: EconOp::Reserve { offer: TradeId(1), amount: 50 },
            },
        ];
        let out = apply_in_order(msgs, &mut books, &locate);
        assert_eq!(out[0].0.actor, UniverseId(10));
        assert_eq!(out[0].1, Outcome::Applied);
        assert_eq!(out[1].1, Outcome::Conflict { seen: 0, current: 1 });
        assert_eq!(books[0].find_by_id(TradeId(1)).unwrap().reserved, 50);
    }

    #[test]
    fn version_free_release_always_applies() {
        let mut books = vec![book_with(1, 100)];
        let locate = |_: TradeId| Some(SectorIx(0));
        let msgs = vec![
            EconMsg {
                stamp: stamp(1),
                actor: UniverseId(10),
                seen_version: Some(0),
                op: EconOp::Reserve { offer: TradeId(1), amount: 60 },
            },
            EconMsg {
                stamp: stamp(2),
                actor: UniverseId(10),
                seen_version: None,
                op: EconOp::Release { offer: TradeId(1), amount: 20 },
            },
            EconMsg {
                stamp: stamp(3),
                actor: UniverseId(11),
                seen_version: Some(2),
                op: EconOp::Reserve { offer: TradeId(1), amount: 70 },
            },
        ];
        let out = apply_in_order(msgs, &mut books, &locate);
        assert_eq!(out[1].1, Outcome::Applied);
        assert_eq!(out[2].1, Outcome::Insufficient { available: 60 });
    }
}
