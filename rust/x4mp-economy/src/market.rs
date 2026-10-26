//! Trade offers: per-sector books and the barrier-published, read-only snapshot.
//!
//! An offer belongs to the container (station or ship) that owns it, so its book lives in the
//! owner's sector and only that sector's job changes it during a round. Trade searches from
//! any sector (`find_buy_offer` / `find_sell_offer`, `Scripts::FindTradeOfferAction::Execute`
//! 0xb8f000; MD `GenerateShortageData`) read the [`OfferSnapshot`] published at the last
//! barrier. Every offer carries a `version` that its owner bumps on every change, so a
//! decision taken on a snapshot can be checked at the barrier against the live book
//! ([`crate::msg`]).
//!
//! Snapshot order is the engine's search order: sector (`SectorIx`), then the owner's
//! `OrderKey` inside the sector, then the owner's offer list (`list` 0 = Container+0x8f8,
//! 1 = +0x910) and the position in it. The search core 0x7f2880 visits the space's zones,
//! each zone's stations (child bucket 2) and then other containers, and filters each
//! container's two lists in order; a single-result search then picks
//! `Random::U64(matches)` (thread RNG) and a multiple-result search returns the matches in
//! that order. That the ecs-core tree key reproduces the zone order is to be confirmed with
//! a capture of search results (reports/REWRITE-ECONOMY.md §6).

use crate::ids::{OrderKey, SectorIx, TradeId, UniverseId, WareIx};

/// One trade offer as the economy model sees it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Offer {
    pub id: TradeId,
    /// Owning container (station, build storage or ship).
    pub owner: UniverseId,
    pub sector: SectorIx,
    /// Tree-order key of the owner inside `sector`.
    pub key: OrderKey,
    /// Owner's offer list: 0 = Container+0x8f8, 1 = Container+0x910.
    pub list: u8,
    /// Position in that list.
    pub pos: u32,
    pub ware: WareIx,
    /// `true`: the owner buys (Trade+0x18 buyer = owner, +0x10 empty); `false`: it sells
    /// (Trade+0x10 seller = owner). Confirmed on captured offers (stress3: a silicon wafer
    /// factory buys silicon and sells wafers).
    pub buy: bool,
    /// Offered amount (Trade+0x40). The engine computes it net of the owner's reservations
    /// (the refresh after every reservation change), so it is also the amount a new
    /// reservation can take.
    pub amount: u32,
    /// Unit price in cents (Trade+0x30 tag; engine money is an `i64` in cents).
    pub unit_price: i64,
    /// Trade+0x54 flags (bundle 0x10, unbundle 0x20, supply 0x40, shady 0x8000, fixed price
    /// 0x100000, ...).
    pub flags: u32,
    /// Bumped by the owner's sector on every change of amount, reservation or price.
    pub version: u32,
}

/// Offers of one sector, kept sorted by `(key, list, pos, id)`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct OfferBook {
    offers: Vec<Offer>,
}

impl OfferBook {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn len(&self) -> usize {
        self.offers.len()
    }

    pub fn is_empty(&self) -> bool {
        self.offers.is_empty()
    }

    pub fn offers(&self) -> &[Offer] {
        &self.offers
    }

    fn position(&self, key: OrderKey, list: u8, pos: u32, id: TradeId) -> Result<usize, usize> {
        self.offers.binary_search_by(|o| (o.key, o.list, o.pos, o.id).cmp(&(key, list, pos, id)))
    }

    /// Inserts or replaces an offer; a replacement that changes anything bumps the version.
    pub fn upsert(&mut self, mut offer: Offer) {
        match self.position(offer.key, offer.list, offer.pos, offer.id) {
            Ok(i) => {
                let old = &self.offers[i];
                let changed = old.amount != offer.amount
                    || old.unit_price != offer.unit_price
                    || old.ware != offer.ware
                    || old.buy != offer.buy
                    || old.flags != offer.flags;
                offer.version = if changed { old.version.wrapping_add(1) } else { old.version };
                self.offers[i] = offer;
            }
            Err(i) => self.offers.insert(i, offer),
        }
    }

    pub fn remove(&mut self, id: TradeId) -> Option<Offer> {
        let i = self.offers.iter().position(|o| o.id == id)?;
        Some(self.offers.remove(i))
    }

    pub fn find_by_id(&self, id: TradeId) -> Option<&Offer> {
        self.offers.iter().find(|o| o.id == id)
    }

    pub fn find_by_id_mut(&mut self, id: TradeId) -> Option<&mut Offer> {
        self.offers.iter_mut().find(|o| o.id == id)
    }

    /// A new version for every offer of `owner` for `ware` (the barrier changed the owner's
    /// reservations; the refresh before the next snapshot recomputes them).
    pub fn touch(&mut self, owner: UniverseId, ware: WareIx) {
        for o in self.offers.iter_mut().filter(|o| o.owner == owner && o.ware == ware) {
            o.version = o.version.wrapping_add(1);
        }
    }
}

/// Read-only view of every offer, published at the barrier and shared by all sector jobs of
/// the next round.
#[derive(Clone, Debug, Default)]
pub struct OfferSnapshot {
    /// Frame at whose barrier the snapshot was taken.
    pub frame: u64,
    offers: Vec<Offer>,
    /// `(id, index into offers)` sorted by id.
    by_id: Vec<(TradeId, u32)>,
    /// Per ware (dense `WareIx`), indices into `offers` in tree order.
    by_ware: Vec<Vec<u32>>,
}

impl OfferSnapshot {
    /// Builds the snapshot from the books of all sectors, given in `SectorIx` order.
    pub fn build<'a>(frame: u64, books: impl IntoIterator<Item = (SectorIx, &'a OfferBook)>) -> Self {
        let mut offers = Vec::new();
        let mut last: Option<SectorIx> = None;
        for (sector, book) in books {
            assert!(last.is_none_or(|l| l < sector), "books must be given in SectorIx order");
            last = Some(sector);
            for o in book.offers() {
                debug_assert_eq!(o.sector, sector);
                offers.push(o.clone());
            }
        }
        let mut by_id: Vec<(TradeId, u32)> = offers.iter().enumerate().map(|(i, o)| (o.id, i as u32)).collect();
        by_id.sort_unstable();
        let wares = offers.iter().map(|o| o.ware.0 as usize + 1).max().unwrap_or(0);
        let mut by_ware = vec![Vec::new(); wares];
        for (i, o) in offers.iter().enumerate() {
            by_ware[o.ware.0 as usize].push(i as u32);
        }
        Self { frame, offers, by_id, by_ware }
    }

    pub fn len(&self) -> usize {
        self.offers.len()
    }

    pub fn is_empty(&self) -> bool {
        self.offers.is_empty()
    }

    pub fn all(&self) -> &[Offer] {
        &self.offers
    }

    pub fn get(&self, id: TradeId) -> Option<&Offer> {
        self.by_id.binary_search_by(|(k, _)| k.cmp(&id)).ok().map(|i| &self.offers[self.by_id[i].1 as usize])
    }

    /// Offers of `ware` on the requested side, in tree order.
    pub fn find(&self, ware: WareIx, buy: bool) -> impl Iterator<Item = &Offer> {
        self.by_ware
            .get(ware.0 as usize)
            .map(|v| v.as_slice())
            .unwrap_or(&[])
            .iter()
            .map(move |&i| &self.offers[i as usize])
            .filter(move |o| o.buy == buy)
    }
}

// x4mp-sim has no StableHash for SectorIx (its own column hashes skip it), so Offer's impls
// are written out; the sector enters the hash as its index.
impl x4mp_sim::hash::StableHash for Offer {
    fn stable_hash(&self, h: &mut x4mp_sim::hash::Fnv64) {
        self.id.stable_hash(h);
        self.owner.stable_hash(h);
        self.sector.0.stable_hash(h);
        self.key.0.stable_hash(h);
        self.list.stable_hash(h);
        self.pos.stable_hash(h);
        self.ware.stable_hash(h);
        self.buy.stable_hash(h);
        self.amount.stable_hash(h);
        self.unit_price.stable_hash(h);
        self.flags.stable_hash(h);
        self.version.stable_hash(h);
    }
}
impl x4mp_sim::shadow::Compare for Offer {
    fn compare(&self, a: &Self, c: &mut x4mp_sim::shadow::Comparator) {
        c.field("id", |c| self.id.compare(&a.id, c));
        c.field("owner", |c| self.owner.compare(&a.owner, c));
        c.field("sector", |c| self.sector.compare(&a.sector, c));
        c.field("key", |c| self.key.compare(&a.key, c));
        c.field("list", |c| self.list.compare(&a.list, c));
        c.field("pos", |c| self.pos.compare(&a.pos, c));
        c.field("ware", |c| self.ware.compare(&a.ware, c));
        c.field("buy", |c| self.buy.compare(&a.buy, c));
        c.field("amount", |c| self.amount.compare(&a.amount, c));
        c.field("unit_price", |c| self.unit_price.compare(&a.unit_price, c));
        c.field("flags", |c| self.flags.compare(&a.flags, c));
        c.field("version", |c| self.version.compare(&a.version, c));
    }
}
x4mp_sim::impl_fields!(OfferBook { offers });

#[cfg(test)]
mod tests {
    use super::*;

    fn offer(id: u64, sector: u32, key: u64, ware: u16, buy: bool, amount: u32) -> Offer {
        Offer {
            id: TradeId(id),
            owner: UniverseId(1000 + key),
            sector: SectorIx(sector),
            key: OrderKey(key),
            list: if buy { 0 } else { 1 },
            pos: 0,
            ware: WareIx(ware),
            buy,
            amount,
            unit_price: 1600,
            flags: 0,
            version: 0,
        }
    }

    #[test]
    fn upsert_bumps_version_only_on_change() {
        let mut b = OfferBook::new();
        b.upsert(offer(7, 0, 3, 1, false, 100));
        b.upsert(offer(7, 0, 3, 1, false, 100));
        assert_eq!(b.find_by_id(TradeId(7)).unwrap().version, 0);
        b.upsert(offer(7, 0, 3, 1, false, 90));
        assert_eq!(b.find_by_id(TradeId(7)).unwrap().version, 1);
    }

    #[test]
    fn snapshot_is_in_tree_order() {
        let mut s0 = OfferBook::new();
        let mut s1 = OfferBook::new();
        s1.upsert(offer(1, 1, 5, 2, true, 10));
        s0.upsert(offer(9, 0, 8, 2, true, 10));
        s0.upsert(offer(3, 0, 2, 2, true, 10));
        s0.upsert(offer(4, 0, 2, 2, false, 10));
        let snap = OfferSnapshot::build(1, [(SectorIx(0), &s0), (SectorIx(1), &s1)]);
        let ids: Vec<u64> = snap.find(WareIx(2), true).map(|o| o.id.0).collect();
        assert_eq!(ids, vec![3, 9, 1]);
        assert!(!snap.get(TradeId(4)).unwrap().buy);
        assert!(snap.get(TradeId(5)).is_none());
    }
}
