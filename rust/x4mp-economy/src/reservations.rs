//! Ware reservations of a container, as the engine keeps them (Container+0x980, 48-byte
//! entries in insertion order).
//!
//! Source (X4 9.00 build 23660954, static, decompile-confirmed; reports/REWRITE-ECONOMY.md §6):
//! `AddWareReservation` 0x4128c0, `RemoveWareReservation` 0x413060, `GetWareReservationAmount`
//! 0x412490 (MD/AI `add_ware_reservation`, `remove_ware_reservation`, `get_ware_reservation`,
//! FFI `GetWareReservationsForWare`). Entry: flags (type bits 0..1 = valid mask 0x3, then three
//! flag bits 2/3/4), reserver id, linked trade order id (0 for plain reservations), ware,
//! amount, expiry time. Every scan drops expired entries (`expiry` set and earlier than now) and
//! entries whose reserver no longer exists; add and remove end with an offer refresh of the
//! container (`UpdateTradeOffers` 0x4079e0), which callers model with
//! [`ReservationChange::refresh_offers`].
//!
//! Entries linked to a trade order take their amount from the order; this model keeps plain
//! reservations (the order-linked kind is created by trade orders, stage 4 of the economy work).

use crate::ids::{UniverseId, WareIx};
use crate::time::is_set;

/// Valid reservation type bits (`*(u32*)0x2f54898` = 3).
pub const TYPE_MASK: u32 = 0x3;
/// Default reservation duration when the caller gives none: the settings block +0x5170
/// (`parameters.xml <economy><reservation trade="3600"/>`).
pub const DEFAULT_TRADE_DURATION: f64 = 3600.0;

/// The three flag bits the engine matches exactly (entry bits 2, 3, 4).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Flags {
    pub bit2: bool,
    pub bit3: bool,
    pub bit4: bool,
}

impl Flags {
    fn bits(self) -> u32 {
        (self.bit2 as u32) << 2 | (self.bit3 as u32) << 3 | (self.bit4 as u32) << 4
    }
    fn of(word: u32) -> Flags {
        Flags { bit2: word >> 2 & 1 != 0, bit3: word >> 3 & 1 != 0, bit4: word >> 4 & 1 != 0 }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Reservation {
    /// Type bits (0..1) and flag bits (2..4) as the engine stores them.
    pub word: u32,
    pub reserver: UniverseId,
    /// Linked trade order (0 for a plain ware reservation).
    pub order: u64,
    pub ware: WareIx,
    pub amount: u32,
    pub expiry: f64,
}

/// What a call changed, for the caller's bookkeeping (per-ware reserved totals, offer refresh).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ReservationChange {
    /// `(ware, delta)` in the order the engine applies them (0x413480 calls).
    pub deltas: Vec<(WareIx, i64)>,
    /// The engine calls `UpdateTradeOffers(container, 1, 0)` at the end of this call.
    pub refresh_offers: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ReservationList {
    entries: Vec<Reservation>,
}

impl ReservationList {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn entries(&self) -> &[Reservation] {
        &self.entries
    }

    fn expired(e: &Reservation, now: f64, alive: &dyn Fn(UniverseId) -> bool) -> bool {
        (is_set(e.expiry) && e.expiry < now) || !alive(e.reserver)
    }

    /// `AddWareReservation` 0x4128c0 for a plain reservation. `duration` [`crate::time::UNSET`]
    /// (or any value not above -0.9999) selects [`DEFAULT_TRADE_DURATION`]. `replace` sets the
    /// amount instead of adding to it; `amount == 0` with `replace` removes the reservation.
    #[allow(clippy::too_many_arguments)]
    pub fn add(
        &mut self,
        now: f64,
        typemask: u32,
        reserver: UniverseId,
        ware: WareIx,
        amount: u32,
        duration: f64,
        replace: bool,
        flags: Flags,
        alive: &dyn Fn(UniverseId) -> bool,
    ) -> ReservationChange {
        let mut ch = ReservationChange::default();
        if reserver.0 == 0 || typemask & TYPE_MASK == 0 {
            return ch;
        }
        if amount == 0 {
            if replace {
                return self.remove(now, typemask, Some(reserver), Some(ware), 0, flags, alive);
            }
            return ch;
        }
        let duration = if is_set(duration) { duration } else { DEFAULT_TRADE_DURATION };
        let mut i = 0;
        let mut found = false;
        while i < self.entries.len() {
            if Self::expired(&self.entries[i], now, alive) {
                let e = self.entries.remove(i);
                ch.deltas.push((e.ware, -(e.amount as i64)));
                continue;
            }
            let e = &mut self.entries[i];
            if e.order == 0 && e.reserver == reserver && e.ware == ware && Flags::of(e.word) == flags {
                let new_amount = if replace {
                    ch.deltas.push((ware, amount as i64 - e.amount as i64));
                    amount
                } else {
                    ch.deltas.push((ware, amount as i64));
                    e.amount + amount
                };
                e.amount = new_amount;
                e.expiry = duration + now;
                found = true;
                break;
            }
            i += 1;
        }
        if !found {
            self.entries.push(Reservation {
                // The engine stores the caller's type word with bits 2..4 replaced by the flags.
                word: (typemask & !0x1c) | flags.bits(),
                reserver,
                order: 0,
                ware,
                amount,
                expiry: duration + now,
            });
            ch.deltas.push((ware, amount as i64));
        }
        ch.refresh_offers = true;
        ch
    }

    /// `RemoveWareReservation` 0x413060: entries matching the type mask, the flag bits and the
    /// optional reserver and ware. A positive `amount` smaller than an entry's amount reduces
    /// that entry and stops; otherwise matching entries are removed. Expired entries go too.
    #[allow(clippy::too_many_arguments)]
    pub fn remove(
        &mut self,
        now: f64,
        typemask: u32,
        reserver: Option<UniverseId>,
        ware: Option<WareIx>,
        amount: i64,
        flags: Flags,
        alive: &dyn Fn(UniverseId) -> bool,
    ) -> ReservationChange {
        let mut ch = ReservationChange::default();
        let mut removed = false;
        let mut i = 0;
        while i < self.entries.len() {
            let e = self.entries[i];
            let typed = typemask == 0 || e.word & typemask != 0;
            let matches = typed
                && Flags::of(e.word) == flags
                && e.order == 0
                && ware.is_none_or(|w| w == e.ware)
                && reserver.is_none_or(|r| r == e.reserver);
            if Self::expired(&e, now, alive) || e.word & TYPE_MASK == 0 {
                self.entries.remove(i);
                ch.deltas.push((e.ware, -(e.amount as i64)));
                removed = true;
                continue;
            }
            if matches {
                let partial = typemask & TYPE_MASK != 0
                    && ware.is_some()
                    && reserver.is_some()
                    && amount >= 1
                    && (e.amount as i64) > amount;
                if partial {
                    ch.deltas.push((e.ware, -amount));
                    self.entries[i].amount = (e.amount as i64 - amount) as u32;
                    ch.refresh_offers = true;
                    return ch;
                }
                self.entries.remove(i);
                ch.deltas.push((e.ware, -(e.amount as i64)));
                removed = true;
                continue;
            }
            i += 1;
        }
        ch.refresh_offers = removed;
        ch
    }

    /// `GetWareReservationAmount` 0x412490 (plain entries; the faction-specific buy rule for a
    /// foreign reserver is not modelled). Expired entries are removed as in the engine.
    pub fn amount(
        &mut self,
        now: f64,
        ware: WareIx,
        typemask: u32,
        reserver: Option<UniverseId>,
        flags: Flags,
        alive: &dyn Fn(UniverseId) -> bool,
    ) -> (u32, ReservationChange) {
        let mut ch = ReservationChange::default();
        if typemask & TYPE_MASK == 0 {
            return (0, ch);
        }
        let mut total: u32 = 0;
        let mut i = 0;
        while i < self.entries.len() {
            let e = self.entries[i];
            if Self::expired(&e, now, alive) {
                self.entries.remove(i);
                ch.deltas.push((e.ware, -(e.amount as i64)));
                continue;
            }
            if e.ware == ware && Flags::of(e.word) == flags && reserver.is_none_or(|r| r == e.reserver) {
                total = total.wrapping_add(e.amount);
            }
            i += 1;
        }
        (total, ch)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ORE: WareIx = WareIx(1);
    const ICE: WareIx = WareIx(2);
    fn all(_: UniverseId) -> bool {
        true
    }

    #[test]
    fn add_merges_by_reserver_ware_and_flags() {
        let mut l = ReservationList::new();
        let f = Flags::default();
        let a = l.add(10.0, 1, UniverseId(7), ORE, 100, crate::time::UNSET, false, f, &all);
        assert!(a.refresh_offers);
        assert_eq!(l.entries()[0].expiry, 3610.0);
        l.add(20.0, 1, UniverseId(7), ORE, 50, 100.0, false, f, &all);
        assert_eq!(l.entries().len(), 1);
        assert_eq!((l.entries()[0].amount, l.entries()[0].expiry), (150, 120.0));
        l.add(20.0, 1, UniverseId(7), ORE, 30, 100.0, true, f, &all);
        assert_eq!(l.entries()[0].amount, 30);
        // Other flags or another ware: a new entry.
        l.add(20.0, 1, UniverseId(7), ORE, 5, 100.0, false, Flags { bit3: true, ..f }, &all);
        l.add(20.0, 1, UniverseId(7), ICE, 5, 100.0, false, f, &all);
        assert_eq!(l.entries().len(), 3);
        assert_eq!(l.amount(21.0, ORE, 1, None, f, &all).0, 30);
    }

    #[test]
    fn expiry_and_removal() {
        let mut l = ReservationList::new();
        let f = Flags::default();
        l.add(0.0, 1, UniverseId(1), ORE, 10, 50.0, false, f, &all);
        l.add(0.0, 1, UniverseId(2), ORE, 20, 500.0, false, f, &all);
        // At t=60 the first entry expired: the query drops it.
        let (n, ch) = l.amount(60.0, ORE, 1, None, f, &all);
        assert_eq!(n, 20);
        assert_eq!(ch.deltas, vec![(ORE, -10)]);
        // Partial removal of a specific reserver and ware.
        let ch = l.remove(61.0, 1, Some(UniverseId(2)), Some(ORE), 5, f, &all);
        assert!(ch.refresh_offers);
        assert_eq!(l.entries()[0].amount, 15);
        // Removing more than held removes the entry.
        l.remove(61.0, 1, Some(UniverseId(2)), Some(ORE), 99, f, &all);
        assert!(l.entries().is_empty());
        // A reserver that no longer exists drops out on the next scan.
        l.add(70.0, 1, UniverseId(3), ICE, 4, 500.0, false, f, &all);
        let gone = |u: UniverseId| u != UniverseId(3);
        assert_eq!(l.amount(71.0, ICE, 1, None, f, &gone).0, 0);
    }
}
