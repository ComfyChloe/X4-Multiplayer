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
//! Snapshot order is engine tree order: sector (`SectorIx`), then the owner's `OrderKey`
//! inside the sector, then the offer id. Searches return offers in that order, which is
//! the order the original space gatherer visits containers in (to be confirmed against the
//! capture of `FindTradeOfferAction` results, see reports/REWRITE-ECONOMY.md).

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
    pub ware: WareIx,
    /// `true`: the owner buys (buy offer); `false`: the owner sells (sell offer).
    pub buy: bool,
    /// Offered amount before reservations.
    pub amount: u32,
    /// Sum of the reservations held against this offer.
    pub reserved: u32,
    /// Unit price in cents (engine money is an `i64` in cents, Lua shows credits).
    pub unit_price: i64,
    /// Bumped by the owner's sector on every change of amount, reservation or price.
    pub version: u32,
}

impl Offer {
    /// Amount still available to new reservations.
    pub fn available(&self) -> u32 {
        self.amount.saturating_sub(self.reserved)
    }
}

/// Offers of one sector, kept sorted by `(key, id)`.
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

    fn position(&self, key: OrderKey, id: TradeId) -> Result<usize, usize> {
        self.offers.binary_search_by(|o| (o.key, o.id).cmp(&(key, id)))
    }

    /// Inserts or replaces an offer; a replacement that changes anything bumps the version.
    pub fn upsert(&mut self, mut offer: Offer) {
        match self.position(offer.key, offer.id) {
            Ok(i) => {
                let old = &self.offers[i];
                let changed = old.amount != offer.amount
                    || old.reserved != offer.reserved
                    || old.unit_price != offer.unit_price
                    || old.ware != offer.ware
                    || old.buy != offer.buy;
                offer.version = if changed { old.version.wrapping_add(1) } else { old.version };
                self.offers[i] = offer;
            }
            Err(i) => self.offers.insert(i, offer),
        }
    }

    pub fn remove(&mut self, key: OrderKey, id: TradeId) -> Option<Offer> {
        self.position(key, id).ok().map(|i| self.offers.remove(i))
    }

    pub fn find_by_id(&self, id: TradeId) -> Option<&Offer> {
        self.offers.iter().find(|o| o.id == id)
    }

    pub fn find_by_id_mut(&mut self, id: TradeId) -> Option<&mut Offer> {
        self.offers.iter_mut().find(|o| o.id == id)
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

#[cfg(test)]
mod tests {
    use super::*;

    fn offer(id: u64, sector: u32, key: u64, ware: u16, buy: bool, amount: u32) -> Offer {
        Offer {
            id: TradeId(id),
            owner: UniverseId(1000 + key),
            sector: SectorIx(sector),
            key: OrderKey(key),
            ware: WareIx(ware),
            buy,
            amount,
            reserved: 0,
            unit_price: 1600,
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
