//! Relevance scheduler: which object state each subscriber gets, and how often.
//!
//! Pure and deterministic: no clock, no randomness, no hash-order iteration. The caller
//! passes `now_us` and the current object/subscriber views; the scheduler keeps only
//! "what did I last send to whom". The same inputs always give the same output.
//!
//! Tiers (proposal section 4):
//!
//! | Tier | Objects | Default rate |
//! |---|---|---|
//! | 1 Player | other players' client-published ships in your current sector | 20-30 Hz by speed |
//! | 2 Combat | your target, anything targeting your ship, objects within weapons range | 20-30 Hz by speed |
//! | 3 Sector | other objects in your sector; everything in adjacent sectors | 1-2 Hz by speed |
//! | 4 Map | objects in map-only sectors | on change only |
//! | 5 Ambient | ambient mass traffic | never |
//!
//! Unchanged objects (no new state since the last send) are re-sent only at
//! `refresh_hz`, so a lost datagram is repaired without resending static data at 30 Hz.

use std::collections::BTreeMap;

use crate::ids::{ObjectId, PlayerId, SectorId};
use crate::msg::{Interest, SectorInterest};

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash, PartialOrd, Ord)]
pub enum Tier {
    Player = 1,
    Combat = 2,
    Sector = 3,
    Map = 4,
    Ambient = 5,
}

/// A rate band: `min_hz` at rest, rising linearly with speed to `max_hz`.
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct TierRate {
    pub min_hz: f64,
    pub max_hz: f64,
}

#[derive(Clone, PartialEq, Debug)]
pub struct SchedulerConfig {
    pub tier1: TierRate,
    pub tier2: TierRate,
    pub tier3: TierRate,
    /// Feature flag (on by default): scale rates with speed inside each band. Off:
    /// every periodic tier uses its `max_hz`.
    pub speed_scaling: bool,
    /// Speed (m/s) at and above which a tier uses its `max_hz`.
    pub speed_for_max_rate: f64,
    /// Objects closer than this to the subscriber's own ship are tier 2.
    pub weapons_range_m: f64,
    /// Resend rate of tier 1-3 objects whose state has not changed.
    pub refresh_hz: f64,
    /// Optional periodic resend of unchanged tier-4 objects; `None` = on change only.
    pub map_refresh_s: Option<f64>,
    /// An object is due up to this much early, so rates are not lost to tick jitter.
    pub tolerance_us: u64,
    /// At most this many records per subscriber per plan; the rest wait for the next.
    pub max_records_per_tick: usize,
}

impl Default for SchedulerConfig {
    fn default() -> Self {
        SchedulerConfig {
            tier1: TierRate { min_hz: 20.0, max_hz: 30.0 },
            tier2: TierRate { min_hz: 20.0, max_hz: 30.0 },
            tier3: TierRate { min_hz: 1.0, max_hz: 2.0 },
            speed_scaling: true,
            speed_for_max_rate: 300.0,
            weapons_range_m: 6000.0,
            refresh_hz: 1.0,
            map_refresh_s: None,
            tolerance_us: 2000,
            max_records_per_tick: 256,
        }
    }
}

impl SchedulerConfig {
    pub fn validate(&self) -> Result<(), &'static str> {
        for rate in [self.tier1, self.tier2, self.tier3] {
            if !(rate.min_hz.is_finite() && rate.max_hz.is_finite())
                || rate.min_hz <= 0.0
                || rate.min_hz > rate.max_hz
                || rate.max_hz > 1000.0
            {
                return Err("tier rates must satisfy 0 < min_hz <= max_hz <= 1000");
            }
        }
        if !(self.speed_for_max_rate.is_finite() && self.speed_for_max_rate > 0.0) {
            return Err("speed_for_max_rate must be positive");
        }
        if !(self.weapons_range_m.is_finite() && self.weapons_range_m >= 0.0) {
            return Err("weapons_range_m must be non-negative");
        }
        if !(self.refresh_hz.is_finite() && self.refresh_hz > 0.0 && self.refresh_hz <= 1000.0) {
            return Err("refresh_hz must be in (0, 1000]");
        }
        if self.map_refresh_s.is_some_and(|s| !(s.is_finite() && s > 0.0)) {
            return Err("map_refresh_s must be positive");
        }
        if self.max_records_per_tick == 0 {
            return Err("max_records_per_tick must be at least 1");
        }
        Ok(())
    }

    pub fn band(&self, tier: Tier) -> Option<TierRate> {
        match tier {
            Tier::Player => Some(self.tier1),
            Tier::Combat => Some(self.tier2),
            Tier::Sector => Some(self.tier3),
            Tier::Map | Tier::Ambient => None,
        }
    }
}

/// What the scheduler needs to know about one subscriber.
#[derive(Clone, Copy, Debug)]
pub struct Subscriber<'a> {
    pub player: PlayerId,
    pub current_sector: SectorId,
    pub own_ship: Option<ObjectId>,
    /// Own ship position in `current_sector` coordinates, if known.
    pub own_position_m: Option<[f64; 3]>,
    pub target: Option<ObjectId>,
    pub sectors: &'a [SectorInterest],
    pub all_map: bool,
}

impl Subscriber<'_> {
    /// The strongest interest this subscriber has in `sector`.
    pub fn interest(&self, sector: SectorId) -> Option<Interest> {
        if sector == self.current_sector {
            return Some(Interest::Full);
        }
        let listed = self.sectors.iter().filter(|s| s.sector == sector).map(|s| s.interest).min();
        listed.or(self.all_map.then_some(Interest::Map))
    }
}

/// What the scheduler needs to know about one object.
#[derive(Clone, Copy, Debug)]
pub struct ObjectInfo {
    pub id: ObjectId,
    pub sector: SectorId,
    /// Who published the state: the authority, or the human whose own ship this is.
    pub publisher: PlayerId,
    pub position_m: [f64; 3],
    pub speed_mps: f64,
    pub combat_target: Option<ObjectId>,
    pub ambient: bool,
    /// Increases whenever new state for this object arrives.
    pub update_seq: u64,
    /// Increases when map-relevant facts change (owner, sector, generation, combat).
    pub map_revision: u64,
}

/// Tier of `object` for `sub`; `None` when it is not sent at all (the subscriber's own
/// ship or an unsubscribed sector).
pub fn classify(config: &SchedulerConfig, sub: &Subscriber<'_>, object: &ObjectInfo) -> Option<Tier> {
    if object.ambient {
        return Some(Tier::Ambient);
    }
    if object.publisher == sub.player || Some(object.id) == sub.own_ship {
        return None;
    }
    let interest = sub.interest(object.sector)?;
    if interest == Interest::Map {
        return Some(Tier::Map);
    }
    if interest == Interest::Full && !object.publisher.is_authority() {
        return Some(Tier::Player);
    }
    let attacking = sub.own_ship.is_some() && object.combat_target == sub.own_ship;
    if Some(object.id) == sub.target || attacking {
        return Some(Tier::Combat);
    }
    if interest == Interest::Adjacent {
        return Some(Tier::Sector);
    }
    if let Some(own) = sub.own_position_m {
        let d2: f64 = own.iter().zip(object.position_m).map(|(a, b)| (a - b) * (a - b)).sum();
        if d2 <= config.weapons_range_m * config.weapons_range_m {
            return Some(Tier::Combat);
        }
    }
    Some(Tier::Sector)
}

/// Send rate for a tier at a given speed; `None` for tiers that are not periodic.
pub fn rate_hz(config: &SchedulerConfig, tier: Tier, speed_mps: f64) -> Option<f64> {
    let band = config.band(tier)?;
    if !config.speed_scaling {
        return Some(band.max_hz);
    }
    let speed = if speed_mps.is_finite() { speed_mps.max(0.0) } else { config.speed_for_max_rate };
    let t = (speed / config.speed_for_max_rate).min(1.0);
    Some(band.min_hz + (band.max_hz - band.min_hz) * t)
}

pub fn interval_us(hz: f64) -> u64 {
    (1.0e6 / hz).round() as u64
}

/// One record to send now.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Due {
    pub object: ObjectId,
    pub tier: Tier,
}

#[derive(Clone, Copy, Debug)]
struct Sent {
    at_us: u64,
    update_seq: u64,
    map_revision: u64,
    tier: Tier,
}

#[derive(Debug)]
pub struct Scheduler {
    config: SchedulerConfig,
    state: BTreeMap<PlayerId, BTreeMap<ObjectId, Sent>>,
}

impl Scheduler {
    pub fn new(config: SchedulerConfig) -> Result<Self, &'static str> {
        config.validate()?;
        Ok(Scheduler { config, state: BTreeMap::new() })
    }

    pub fn config(&self) -> &SchedulerConfig {
        &self.config
    }

    /// Forget everything sent to a player (disconnect, or to force a full resend).
    pub fn forget_player(&mut self, player: PlayerId) {
        self.state.remove(&player);
    }

    /// Decide which of `objects` are due for `sub` at `now_us`, in send priority order
    /// (tier, then least recently sent, then object ID), and record them as sent.
    /// Objects not passed in are forgotten for this subscriber, so an object that
    /// becomes relevant again is sent immediately.
    pub fn plan<'o, I>(&mut self, now_us: u64, sub: &Subscriber<'_>, objects: I) -> Vec<Due>
    where
        I: IntoIterator<Item = &'o ObjectInfo>,
    {
        let previous = self.state.remove(&sub.player).unwrap_or_default();
        let mut next: BTreeMap<ObjectId, Sent> = BTreeMap::new();
        // (tier, last sent, id, update_seq, map_revision)
        let mut candidates: Vec<(Tier, u64, ObjectId, u64, u64)> = Vec::new();
        let tolerance = self.config.tolerance_us;
        let refresh = interval_us(self.config.refresh_hz);
        for object in objects {
            let Some(tier) = classify(&self.config, sub, object) else { continue };
            if tier == Tier::Ambient {
                continue;
            }
            let prev = previous.get(&object.id).copied();
            let due = match prev {
                None => true,
                Some(p) if tier < p.tier => true,
                Some(p) => {
                    let elapsed = now_us.saturating_sub(p.at_us).saturating_add(tolerance);
                    match rate_hz(&self.config, tier, object.speed_mps) {
                        None => {
                            p.map_revision != object.map_revision
                                || self.config.map_refresh_s.is_some_and(|s| elapsed >= (s * 1.0e6).round() as u64)
                        }
                        Some(hz) => {
                            let interval = interval_us(hz);
                            let interval =
                                if object.update_seq != p.update_seq { interval } else { interval.max(refresh) };
                            elapsed >= interval
                        }
                    }
                }
            };
            if due {
                candidates.push((tier, prev.map_or(0, |p| p.at_us), object.id, object.update_seq, object.map_revision));
            }
            if let Some(mut p) = prev {
                p.tier = p.tier.max(tier);
                next.insert(object.id, p);
            }
        }
        candidates.sort_by_key(|c| (c.0, c.1, c.2));
        candidates.truncate(self.config.max_records_per_tick);
        let mut out = Vec::with_capacity(candidates.len());
        for (tier, _, id, update_seq, map_revision) in candidates {
            next.insert(id, Sent { at_us: now_us, update_seq, map_revision, tier });
            out.push(Due { object: id, tier });
        }
        self.state.insert(sub.player, next);
        out
    }
}
