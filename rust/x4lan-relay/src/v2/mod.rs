//! Opt-in protocol-v2 session mode: N players by player ID, one server authority,
//! sector-channel snapshots routed through the relevance scheduler, intents forwarded
//! to the authority, events and a roster with RTT statistics.
//!
//! Selected only by `"protocol": "v2"` in session.json; the version-1 [`crate::session`]
//! code is not used or changed by this mode. [`V2Session`] is a pure state machine
//! (datagram in, datagrams out, caller-supplied clock) so it can be tested without
//! sockets; [`net::V2Relay`] wraps it in the UDP loop.
//!
//! Synthetic clients exercising this mode are not evidence of native multiplayer.

pub mod config;
pub mod net;

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::net::SocketAddr;

use x4mp_proto::msg::{RECORD_OWNER_OFFSET, RECORD_SECTOR_OFFSET};
use x4mp_proto::quant;
use x4mp_proto::rtt::RttStats;
use x4mp_proto::sched::{ObjectInfo, Scheduler, SchedulerConfig, Subscriber};
use x4mp_proto::{
    decode, encode, retarget, Admission, Capabilities, Event, EventKind, Header, Hello, IntentResult, Interest, Label,
    Lease, Message, Nonce, ObjectId, ObjectState, Outcome, Owner, PlayerId, Reason, RefuseReason, Role, Roster,
    RosterEntry, SectorId, SnapshotBuilder, SnapshotView, Subscribe, Token, Welcome,
};

/// One outgoing datagram.
pub type Packet = (SocketAddr, Vec<u8>);

pub const DEFAULT_MAX_PLAYERS: u8 = 6;
const MAX_PENDING_INTENTS: usize = 32;
const INTENT_HISTORY: usize = 256;
const EVENT_WINDOW: usize = 1024;
const TOMBSTONES: usize = 4096;
const RETIRED_NONCES: usize = 64;
const OUTSTANDING_PINGS: usize = 16;

/// Optional relay behaviour. Every feature is ON by default (development rule); a
/// disabled feature records why and is reported with `FEATURE <name> DISABLED`.
/// `None` = on (the default); `Some(reason)` = off.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Features {
    /// Relay-initiated Ping and RTT/jitter statistics per peer.
    pub rtt_stats: Option<String>,
    /// Periodic and on-change Roster messages.
    pub roster: Option<String>,
    /// Repeat each forwarded Event `event_repeats` times (loss protection).
    pub event_repeat: Option<String>,
    /// Answer duplicate intents from the relay's result cache.
    pub intent_cache: Option<String>,
}

impl Features {
    pub const NAMES: [&'static str; 4] = ["rtt_stats", "roster", "event_repeat", "intent_cache"];

    fn slot(&mut self, name: &str) -> Option<&mut Option<String>> {
        match name {
            "rtt_stats" => Some(&mut self.rtt_stats),
            "roster" => Some(&mut self.roster),
            "event_repeat" => Some(&mut self.event_repeat),
            "intent_cache" => Some(&mut self.intent_cache),
            _ => None,
        }
    }

    /// Disable `name` with a reason. Unknown names are an error, not ignored.
    pub fn disable(&mut self, name: &str, reason: &str) -> Result<(), String> {
        let slot = self.slot(name).ok_or_else(|| format!("unknown v2 feature {name}"))?;
        *slot = Some(if reason.trim().is_empty() { "unspecified".to_string() } else { reason.to_string() });
        Ok(())
    }

    fn entries(&self) -> [(&'static str, &Option<String>); 4] {
        [
            ("rtt_stats", &self.rtt_stats),
            ("roster", &self.roster),
            ("event_repeat", &self.event_repeat),
            ("intent_cache", &self.intent_cache),
        ]
    }

    /// The `FEATURES` startup line.
    pub fn features_line(&self, scheduler: &SchedulerConfig) -> String {
        let mut parts = vec!["FEATURES x4lan-relay protocol=v2".to_string()];
        for (name, disabled) in self.entries() {
            parts.push(format!("{name}={}", if disabled.is_some() { "off" } else { "on" }));
        }
        parts.push(format!("speed_scaling={}", if scheduler.speed_scaling { "on" } else { "off" }));
        parts.join(" ")
    }

    /// One `FEATURE <name> DISABLED reason=...` line per disabled feature.
    pub fn disabled_lines(&self, scheduler: &SchedulerConfig, speed_scaling_reason: Option<&str>) -> Vec<String> {
        let mut out: Vec<String> = self
            .entries()
            .iter()
            .filter_map(|(name, d)| d.as_ref().map(|r| format!("FEATURE {name} DISABLED reason={r}")))
            .collect();
        if !scheduler.speed_scaling {
            out.push(format!(
                "FEATURE speed_scaling DISABLED reason={}",
                speed_scaling_reason.unwrap_or("unspecified")
            ));
        }
        out
    }
}

#[derive(Clone, Debug)]
pub struct V2Config {
    pub token: Token,
    /// Separate token for the authority role; `None` = the session token.
    pub authority_token: Option<Token>,
    pub build: String,
    pub world: String,
    pub max_players: u8,
    pub lease_timeout_us: u64,
    /// How long an expired player's ID (and intent history) stays reserved for resume.
    pub resume_window_us: u64,
    pub tick_hz: u32,
    pub ping_interval_us: u64,
    pub ping_timeout_us: u64,
    pub roster_interval_us: u64,
    pub event_repeats: u8,
    pub event_repeat_interval_us: u64,
    /// Re-forward a pending intent to the authority after this long.
    pub intent_retry_us: u64,
    /// The authority's motion for a client-owned ship is ignored while the owning client
    /// published it within this window (v1 stale-movement bound: 0.75 s).
    pub client_motion_stale_us: u64,
    pub max_objects: usize,
    pub scheduler: SchedulerConfig,
    pub features: Features,
    pub speed_scaling_reason: Option<String>,
}

impl V2Config {
    pub fn new(token: Token, build: &str, world: &str) -> Self {
        V2Config {
            token,
            authority_token: None,
            build: build.to_string(),
            world: world.to_string(),
            max_players: DEFAULT_MAX_PLAYERS,
            lease_timeout_us: 30_000_000,
            resume_window_us: 300_000_000,
            tick_hz: 60,
            ping_interval_us: 1_000_000,
            ping_timeout_us: 5_000_000,
            roster_interval_us: 1_000_000,
            event_repeats: 3,
            event_repeat_interval_us: 50_000,
            intent_retry_us: 250_000,
            client_motion_stale_us: 750_000,
            max_objects: 8192,
            scheduler: SchedulerConfig::default(),
            features: Features::default(),
            speed_scaling_reason: None,
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        if Label::new(&self.build).is_none() || Label::new(&self.world).is_none() {
            return Err("v2 build/world must match [A-Za-z0-9_.-]{1,64}".into());
        }
        if !(1..=x4mp_proto::msg::MAX_ROSTER as u8).contains(&self.max_players) {
            return Err(format!("v2 max_players must be 1-{}", x4mp_proto::msg::MAX_ROSTER));
        }
        if !(1..=1000).contains(&self.tick_hz) {
            return Err("v2 tick_hz must be 1-1000".into());
        }
        for (name, v) in [
            ("lease_timeout", self.lease_timeout_us),
            ("ping_interval", self.ping_interval_us),
            ("ping_timeout", self.ping_timeout_us),
            ("roster_interval", self.roster_interval_us),
            ("event_repeat_interval", self.event_repeat_interval_us),
            ("intent_retry", self.intent_retry_us),
        ] {
            if v == 0 {
                return Err(format!("v2 {name} must be positive"));
            }
        }
        if self.event_repeats == 0 || self.max_objects == 0 {
            return Err("v2 event_repeats and max_objects must be at least 1".into());
        }
        self.scheduler.validate().map_err(|e| format!("v2 scheduler: {e}"))
    }

    fn token_for(&self, role: Role) -> &Token {
        match role {
            Role::Player => &self.token,
            Role::Authority => self.authority_token.as_ref().unwrap_or(&self.token),
        }
    }
}

/// Source of leases and the relay epoch.
pub enum LeaseSource {
    /// Operating-system CSPRNG; production.
    Os,
    /// 1, 2, 3, ...: deterministic tests only.
    Counter(u64),
}

impl LeaseSource {
    fn next(&mut self) -> u64 {
        match self {
            LeaseSource::Os => loop {
                let bytes = crate::rng::random16();
                let mut eight = [0u8; 8];
                eight.copy_from_slice(&bytes[..8]);
                let v = u64::from_le_bytes(eight);
                if v != 0 {
                    return v;
                }
            },
            LeaseSource::Counter(n) => {
                *n += 1;
                *n
            }
        }
    }
}

/// Counters for status output and tests.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct V2Stats {
    pub received: u64,
    pub malformed: u64,
    pub unauthenticated: u64,
    pub ignored: u64,
    pub admitted: u64,
    pub refused: u64,
    pub records_accepted: u64,
    pub records_rejected: u64,
    pub records_stale: u64,
    pub intents_forwarded: u64,
    pub intents_refused: u64,
    pub intents_duplicate: u64,
    pub intents_spoofed: u64,
    pub results_forwarded: u64,
    pub events_accepted: u64,
    pub events_duplicate: u64,
    pub events_sent: u64,
    pub snapshots_sent: u64,
    pub records_sent: u64,
}

enum IntentSlot {
    Pending {
        forwarded_us: u64,
        datagram: Vec<u8>,
    },
    /// The result datagram as received from the authority (header retargeted on send).
    Done(Vec<u8>),
}

struct QueuedEvent {
    datagram: Vec<u8>,
    remaining: u8,
    next_us: u64,
}

struct Peer {
    id: PlayerId,
    address: SocketAddr,
    nonce: Nonce,
    lease: Lease,
    capabilities: Capabilities,
    last_seen_us: u64,
    rtt: RttStats,
    pings: VecDeque<(u32, u64)>,
    next_ping_seq: u32,
    next_ping_us: u64,
    subscription: Option<Subscribe>,
    intents: BTreeMap<u32, IntentSlot>,
    events: VecDeque<QueuedEvent>,
}

impl Peer {
    fn new(id: PlayerId, address: SocketAddr, nonce: Nonce, lease: Lease, caps: Capabilities, now: u64) -> Self {
        Peer {
            id,
            address,
            nonce,
            lease,
            capabilities: caps,
            last_seen_us: now,
            rtt: RttStats::default(),
            pings: VecDeque::new(),
            next_ping_seq: 1,
            next_ping_us: now,
            subscription: None,
            intents: BTreeMap::new(),
            events: VecDeque::new(),
        }
    }

    fn send(&self, message: &Message<'_>, out: &mut Vec<Packet>) {
        if let Ok(bytes) = encode(Some(self.id), Some(self.lease), message) {
            out.push((self.address, bytes));
        }
    }

    fn forward(&self, datagram: &[u8], out: &mut Vec<Packet>) {
        if let Some(bytes) = retarget(datagram, Some(self.id), Some(self.lease)) {
            out.push((self.address, bytes));
        }
    }

    fn pending_intents(&self) -> usize {
        self.intents.values().filter(|s| matches!(s, IntentSlot::Pending { .. })).count()
    }

    fn trim_intents(&mut self) {
        while self.intents.len() > INTENT_HISTORY {
            let Some(seq) = self.intents.iter().find(|(_, s)| matches!(s, IntentSlot::Done(_))).map(|(k, _)| *k) else {
                break;
            };
            self.intents.remove(&seq);
        }
    }
}

struct Entry {
    raw: Vec<u8>,
    state: ObjectState,
    publisher: PlayerId,
    client_motion_us: u64,
    update_seq: u64,
    map_revision: u64,
}

impl Entry {
    fn info(&self) -> ObjectInfo {
        ObjectInfo {
            id: self.state.id,
            sector: self.state.sector,
            publisher: self.publisher,
            position_m: quant::position_m(self.state.position),
            speed_mps: quant::speed_mps(self.state.velocity),
            combat_target: self.state.combat.and_then(|c| c.target),
            ambient: self.state.ambient,
            update_seq: self.update_seq,
            map_revision: self.map_revision,
        }
    }

    fn set_owner(&mut self, owner: Owner) {
        self.state.owner = owner;
        self.raw[RECORD_OWNER_OFFSET..RECORD_OWNER_OFFSET + 2].copy_from_slice(&owner.to_wire().to_le_bytes());
    }

    fn set_sector(&mut self, sector: SectorId) {
        self.state.sector = sector;
        self.raw[RECORD_SECTOR_OFFSET..RECORD_SECTOR_OFFSET + 4].copy_from_slice(&sector.get().to_le_bytes());
    }
}

pub struct V2Session {
    config: V2Config,
    leases: LeaseSource,
    relay_epoch: u64,
    authority: Option<Peer>,
    authority_resumable: Option<(Peer, u64)>,
    authority_admitted_once: bool,
    players: BTreeMap<PlayerId, Peer>,
    resumable: BTreeMap<PlayerId, (Peer, u64)>,
    retired: VecDeque<Nonce>,
    objects: BTreeMap<ObjectId, Entry>,
    by_sector: BTreeMap<SectorId, BTreeSet<ObjectId>>,
    tombstones: BTreeMap<ObjectId, u32>,
    tombstone_order: VecDeque<ObjectId>,
    seen_events: BTreeSet<u32>,
    event_order: VecDeque<u32>,
    scheduler: Scheduler,
    update_counter: u64,
    roster_revision: u32,
    roster_dirty: bool,
    next_roster_us: u64,
    pub stats: V2Stats,
}

impl V2Session {
    pub fn new(config: V2Config, mut leases: LeaseSource) -> Result<Self, String> {
        config.validate()?;
        let scheduler = Scheduler::new(config.scheduler.clone()).map_err(str::to_string)?;
        let relay_epoch = leases.next();
        Ok(V2Session {
            config,
            leases,
            relay_epoch,
            authority: None,
            authority_resumable: None,
            authority_admitted_once: false,
            players: BTreeMap::new(),
            resumable: BTreeMap::new(),
            retired: VecDeque::new(),
            objects: BTreeMap::new(),
            by_sector: BTreeMap::new(),
            tombstones: BTreeMap::new(),
            tombstone_order: VecDeque::new(),
            seen_events: BTreeSet::new(),
            event_order: VecDeque::new(),
            scheduler,
            update_counter: 0,
            roster_revision: 0,
            roster_dirty: true,
            next_roster_us: 0,
            stats: V2Stats::default(),
        })
    }

    pub fn config(&self) -> &V2Config {
        &self.config
    }

    /// `FEATURES` line followed by one line per disabled feature.
    pub fn feature_report(&self) -> Vec<String> {
        let mut lines = vec![self.config.features.features_line(&self.config.scheduler)];
        lines.extend(
            self.config.features.disabled_lines(&self.config.scheduler, self.config.speed_scaling_reason.as_deref()),
        );
        lines
    }

    pub fn relay_epoch(&self) -> u64 {
        self.relay_epoch
    }

    pub fn players(&self) -> Vec<PlayerId> {
        self.players.keys().copied().collect()
    }

    pub fn authority_connected(&self) -> bool {
        self.authority.is_some()
    }

    pub fn rtt(&self, player: PlayerId) -> Option<&RttStats> {
        if player.is_authority() {
            self.authority.as_ref().map(|p| &p.rtt)
        } else {
            self.players.get(&player).map(|p| &p.rtt)
        }
    }

    pub fn object_count(&self) -> usize {
        self.objects.len()
    }

    pub fn object(&self, id: ObjectId) -> Option<(&ObjectState, PlayerId)> {
        self.objects.get(&id).map(|e| (&e.state, e.publisher))
    }

    fn next_update(&mut self) -> u64 {
        self.update_counter += 1;
        self.update_counter
    }

    // ------------------------------------------------------------ receive

    /// Handle one datagram; returns datagrams to send (to any peer).
    pub fn receive(&mut self, raw: &[u8], from: SocketAddr, now: u64) -> Vec<Packet> {
        let mut out = Vec::new();
        self.stats.received += 1;
        self.expire(now, &mut out);
        let packet = match decode(raw) {
            Ok(packet) => packet,
            Err(_) => {
                self.stats.malformed += 1;
                return out;
            }
        };
        if let Message::Hello(hello) = &packet.message {
            if packet.header.player.is_none() && packet.header.lease.is_none() {
                self.hello(hello, from, now, &mut out);
            } else {
                self.stats.malformed += 1;
            }
            return out;
        }
        let Some(sender) = self.authenticate(&packet.header, from) else {
            self.stats.unauthenticated += 1;
            return out;
        };
        if let Some(peer) = self.peer_mut(sender) {
            peer.last_seen_us = now;
        }
        match packet.message {
            Message::Ping(ping) => {
                let pong = Message::Pong(x4mp_proto::Pong { seq: ping.seq, echo_time_us: ping.time_us, time_us: now });
                if let Some(peer) = self.peer(sender) {
                    peer.send(&pong, &mut out);
                }
            }
            Message::Pong(pong) => {
                if let Some(peer) = self.peer_mut(sender) {
                    if let Some(i) = peer.pings.iter().position(|(seq, _)| *seq == pong.seq) {
                        let (_, sent) = peer.pings.remove(i).expect("indexed");
                        peer.rtt.record(now.saturating_sub(sent));
                    }
                }
            }
            Message::Subscribe(sub) if !sender.is_authority() => {
                if let Some(peer) = self.players.get_mut(&sender) {
                    if peer.subscription.as_ref().is_none_or(|old| sub.revision > old.revision) {
                        peer.subscription = Some(sub);
                    }
                }
            }
            Message::Snapshot(view) if sender.is_authority() => self.authority_snapshot(&view, now),
            Message::Snapshot(view) => self.client_snapshot(sender, &view, now, &mut out),
            Message::Intent(intent) if !sender.is_authority() => {
                self.intent(sender, intent.player, intent.client_seq, raw, now, &mut out)
            }
            Message::IntentResult(result) if sender.is_authority() => self.intent_result(&result, raw, &mut out),
            Message::Event(event) if sender.is_authority() => self.event(&event, raw, now, &mut out),
            Message::Bye(_) => self.bye(sender, now, &mut out),
            _ => self.stats.ignored += 1,
        }
        out
    }

    fn peer(&self, id: PlayerId) -> Option<&Peer> {
        if id.is_authority() {
            self.authority.as_ref()
        } else {
            self.players.get(&id)
        }
    }

    fn peer_mut(&mut self, id: PlayerId) -> Option<&mut Peer> {
        if id.is_authority() {
            self.authority.as_mut()
        } else {
            self.players.get_mut(&id)
        }
    }

    fn authenticate(&self, header: &Header, from: SocketAddr) -> Option<PlayerId> {
        let id = header.player?;
        let lease = header.lease?;
        let peer = self.peer(id)?;
        (peer.address == from && peer.lease.matches(lease)).then_some(id)
    }

    fn address_in_use(&self, from: SocketAddr, except: Option<PlayerId>) -> bool {
        self.players.values().chain(self.authority.iter()).any(|p| p.address == from && Some(p.id) != except)
    }

    fn welcome(&self, hello: &Hello<'_>, admission: Admission, now: u64) -> Message<'static> {
        Message::Welcome(Welcome {
            nonce: hello.nonce,
            admission,
            capabilities: hello.capabilities.intersect(Capabilities::SUPPORTED),
            max_players: self.config.max_players,
            relay_epoch: self.relay_epoch,
            relay_time_us: now,
        })
    }

    fn refuse(&mut self, hello: &Hello<'_>, reason: RefuseReason, from: SocketAddr, now: u64, out: &mut Vec<Packet>) {
        self.stats.refused += 1;
        if let Ok(bytes) = encode(None, None, &self.welcome(hello, Admission::Refused(reason), now)) {
            out.push((from, bytes));
        }
    }

    fn accept(&mut self, hello: &Hello<'_>, id: PlayerId, now: u64, out: &mut Vec<Packet>) {
        let Some(peer) = self.peer(id) else { return };
        let message = self.welcome(hello, Admission::Accepted { player: id, lease: peer.lease }, now);
        peer.send(&message, out);
    }

    fn retire(&mut self, nonce: Nonce) {
        self.retired.push_back(nonce);
        while self.retired.len() > RETIRED_NONCES {
            self.retired.pop_front();
        }
    }

    // ------------------------------------------------------------ admission

    fn hello(&mut self, hello: &Hello<'_>, from: SocketAddr, now: u64, out: &mut Vec<Packet>) {
        // A wrong token gets no reply at all (no oracle, no amplification).
        if !self.config.token_for(hello.role).matches(&hello.token) {
            self.stats.unauthenticated += 1;
            return;
        }
        if hello.build.as_str() != self.config.build {
            return self.refuse(hello, RefuseReason::BUILD_MISMATCH, from, now, out);
        }
        if hello.world.as_str() != self.config.world {
            return self.refuse(hello, RefuseReason::WORLD_MISMATCH, from, now, out);
        }
        match hello.role {
            Role::Authority => self.admit_authority(hello, from, now, out),
            Role::Player => self.admit_player(hello, from, now, out),
        }
    }

    fn admit_authority(&mut self, hello: &Hello<'_>, from: SocketAddr, now: u64, out: &mut Vec<Packet>) {
        let id = PlayerId::AUTHORITY;
        if let Some(a) = self.authority.as_mut() {
            let same = a.nonce == hello.nonce && a.address == from;
            let resume = hello.resume_lease.is_some_and(|l| a.lease.matches(l));
            if !same && !resume {
                return self.refuse(hello, RefuseReason::AUTHORITY_TAKEN, from, now, out);
            }
            if resume && self.players.values().any(|p| p.address == from) {
                return self.refuse(hello, RefuseReason::ADDRESS_IN_USE, from, now, out);
            }
            let a = self.authority.as_mut().expect("present");
            a.address = from;
            a.nonce = hello.nonce;
            a.last_seen_us = now;
            return self.accept(hello, id, now, out);
        }
        if self.retired.contains(&hello.nonce) {
            return self.refuse(hello, RefuseReason::NONCE_RETIRED, from, now, out);
        }
        if self.address_in_use(from, None) {
            return self.refuse(hello, RefuseReason::ADDRESS_IN_USE, from, now, out);
        }
        let resumable = self
            .authority_resumable
            .as_ref()
            .is_some_and(|(a, _)| hello.resume_lease.is_some_and(|l| a.lease.matches(l)));
        if resumable {
            let (mut a, _) = self.authority_resumable.take().expect("checked");
            a.address = from;
            a.nonce = hello.nonce;
            a.last_seen_us = now;
            a.pings.clear();
            a.next_ping_us = now;
            self.authority = Some(a);
        } else if self.authority_admitted_once || hello.resume_lease.is_some() {
            // A fresh authority means a fresh world: that requires a fresh relay (v1 rule).
            return self.refuse(hello, RefuseReason::AUTHORITY_TAKEN, from, now, out);
        } else {
            let lease = Lease::new(self.leases.next()).expect("non-zero");
            let caps = hello.capabilities.intersect(Capabilities::SUPPORTED);
            self.authority = Some(Peer::new(id, from, hello.nonce, lease, caps, now));
            self.authority_admitted_once = true;
        }
        self.stats.admitted += 1;
        self.roster_dirty = true;
        self.accept(hello, id, now, out);
    }

    fn admit_player(&mut self, hello: &Hello<'_>, from: SocketAddr, now: u64, out: &mut Vec<Packet>) {
        // Lost Welcome: the same instance asks again from the same address.
        if let Some(id) = self.players.values().find(|p| p.nonce == hello.nonce && p.address == from).map(|p| p.id) {
            if hello.want_player.is_none_or(|w| w == id) {
                if let Some(p) = self.players.get_mut(&id) {
                    p.last_seen_us = now;
                }
                return self.accept(hello, id, now, out);
            }
        }
        match (hello.want_player, hello.resume_lease) {
            (Some(id), Some(lease)) => {
                if self.retired.contains(&hello.nonce) {
                    return self.refuse(hello, RefuseReason::NONCE_RETIRED, from, now, out);
                }
                if self.address_in_use(from, Some(id)) {
                    return self.refuse(hello, RefuseReason::ADDRESS_IN_USE, from, now, out);
                }
                if let Some(p) = self.players.get_mut(&id).filter(|p| p.lease.matches(lease)) {
                    // Address change while live (NAT rebinding, game restart of the client).
                    p.address = from;
                    p.nonce = hello.nonce;
                    p.last_seen_us = now;
                    p.pings.clear();
                    return self.accept(hello, id, now, out);
                }
                if self.resumable.get(&id).is_some_and(|(p, _)| p.lease.matches(lease)) {
                    let (mut p, _) = self.resumable.remove(&id).expect("checked");
                    p.address = from;
                    p.nonce = hello.nonce;
                    p.last_seen_us = now;
                    p.pings.clear();
                    p.next_ping_us = now;
                    p.events.clear();
                    self.players.insert(id, p);
                    self.stats.admitted += 1;
                    self.roster_dirty = true;
                    return self.accept(hello, id, now, out);
                }
                self.refuse(hello, RefuseReason::RESUME_REJECTED, from, now, out)
            }
            (None, None) => {
                if self.retired.contains(&hello.nonce) || self.players.values().any(|p| p.nonce == hello.nonce) {
                    return self.refuse(hello, RefuseReason::NONCE_RETIRED, from, now, out);
                }
                if self.address_in_use(from, None) {
                    return self.refuse(hello, RefuseReason::ADDRESS_IN_USE, from, now, out);
                }
                let free = (1..=u16::from(self.config.max_players))
                    .filter_map(PlayerId::human)
                    .find(|id| !self.players.contains_key(id) && !self.resumable.contains_key(id));
                let Some(id) = free else {
                    return self.refuse(hello, RefuseReason::FULL, from, now, out);
                };
                let lease = Lease::new(self.leases.next()).expect("non-zero");
                let caps = hello.capabilities.intersect(Capabilities::SUPPORTED);
                self.players.insert(id, Peer::new(id, from, hello.nonce, lease, caps, now));
                self.stats.admitted += 1;
                self.roster_dirty = true;
                self.accept(hello, id, now, out)
            }
            _ => self.refuse(hello, RefuseReason::RESUME_REJECTED, from, now, out),
        }
    }

    // ------------------------------------------------------------ state

    fn index(&mut self, id: ObjectId, old: Option<SectorId>, new: SectorId) {
        if old == Some(new) {
            return;
        }
        if let Some(old) = old {
            if let Some(set) = self.by_sector.get_mut(&old) {
                set.remove(&id);
                if set.is_empty() {
                    self.by_sector.remove(&old);
                }
            }
        }
        self.by_sector.entry(new).or_default().insert(id);
    }

    fn remove_object(&mut self, id: ObjectId) -> Option<Entry> {
        let entry = self.objects.remove(&id)?;
        if let Some(set) = self.by_sector.get_mut(&entry.state.sector) {
            set.remove(&id);
            if set.is_empty() {
                self.by_sector.remove(&entry.state.sector);
            }
        }
        self.tombstones.insert(id, entry.state.generation);
        self.tombstone_order.push_back(id);
        while self.tombstone_order.len() > TOMBSTONES {
            if let Some(old) = self.tombstone_order.pop_front() {
                if !self.tombstone_order.contains(&old) {
                    self.tombstones.remove(&old);
                }
            }
        }
        Some(entry)
    }

    fn authority_snapshot(&mut self, view: &SnapshotView<'_>, now: u64) {
        for record in view.records() {
            let s = *record.state();
            if self.tombstones.get(&s.id).is_some_and(|g| s.generation <= *g) {
                self.stats.records_stale += 1;
                continue;
            }
            let seq = self.next_update();
            match self.objects.get_mut(&s.id) {
                None => {
                    if self.objects.len() >= self.config.max_objects {
                        self.stats.records_rejected += 1;
                        continue;
                    }
                    self.tombstones.remove(&s.id);
                    self.objects.insert(
                        s.id,
                        Entry {
                            raw: record.raw().to_vec(),
                            state: s,
                            publisher: PlayerId::AUTHORITY,
                            client_motion_us: 0,
                            update_seq: seq,
                            map_revision: 1,
                        },
                    );
                    self.index(s.id, None, s.sector);
                }
                Some(e) => {
                    if s.generation < e.state.generation {
                        self.stats.records_stale += 1;
                        continue;
                    }
                    let new_instance = s.generation > e.state.generation;
                    if !new_instance && e.publisher.is_authority() && s.frame < e.state.frame {
                        self.stats.records_stale += 1;
                        continue;
                    }
                    // Own ship stays local: while its owning client publishes it, the
                    // authority's copy of that ship's motion is not distributed.
                    let client_live = !new_instance
                        && !e.publisher.is_authority()
                        && s.owner == Owner::Player(e.publisher)
                        && self.players.contains_key(&e.publisher)
                        && now.saturating_sub(e.client_motion_us) < self.config.client_motion_stale_us;
                    if client_live {
                        self.stats.records_stale += 1;
                        continue;
                    }
                    let map_change = new_instance
                        || s.owner != e.state.owner
                        || s.sector != e.state.sector
                        || s.combat.is_some() != e.state.combat.is_some();
                    let old_sector = e.state.sector;
                    e.raw = record.raw().to_vec();
                    e.state = s;
                    e.publisher = PlayerId::AUTHORITY;
                    e.update_seq = seq;
                    e.map_revision += u64::from(map_change);
                    self.index(s.id, Some(old_sector), s.sector);
                }
            }
            self.stats.records_accepted += 1;
        }
    }

    fn client_snapshot(&mut self, sender: PlayerId, view: &SnapshotView<'_>, now: u64, out: &mut Vec<Packet>) {
        if view.source != sender {
            self.stats.records_rejected += view.len() as u64;
            return;
        }
        let mut forward = SnapshotBuilder::new(None, None, sender, view.frame);
        for record in view.records() {
            let s = *record.state();
            let seq = self.next_update();
            // A client may publish only objects the authority has assigned to it.
            let Some(e) = self.objects.get_mut(&s.id).filter(|e| {
                !s.ambient
                    && e.state.owner == Owner::Player(sender)
                    && s.owner == Owner::Player(sender)
                    && s.generation == e.state.generation
            }) else {
                self.stats.records_rejected += 1;
                continue;
            };
            if e.publisher == sender && s.frame <= e.state.frame {
                self.stats.records_stale += 1;
                continue;
            }
            let map_change = s.sector != e.state.sector || s.combat.is_some() != e.state.combat.is_some();
            let old_sector = e.state.sector;
            e.raw = record.raw().to_vec();
            e.state = s;
            e.publisher = sender;
            e.client_motion_us = now;
            e.update_seq = seq;
            e.map_revision += u64::from(map_change);
            self.index(s.id, Some(old_sector), s.sector);
            self.stats.records_accepted += 1;
            // Accepted records are a subset of a datagram that fit, so they fit again.
            let _ = forward.push_record(&record);
        }
        if let Some(a) = self.authority.as_ref().filter(|_| !forward.is_empty()) {
            a.forward(&forward.finish(), out);
        }
    }

    // ------------------------------------------------------------ intents

    fn result_datagram(&self, player: PlayerId, client_seq: u32, outcome: Outcome) -> Vec<u8> {
        let message = Message::IntentResult(IntentResult { client_seq, player, outcome, frame: 0 });
        encode(None, None, &message).unwrap_or_default()
    }

    fn intent(&mut self, sender: PlayerId, claimed: PlayerId, seq: u32, raw: &[u8], now: u64, out: &mut Vec<Packet>) {
        if claimed != sender {
            self.stats.intents_spoofed += 1;
            return;
        }
        let cache = self.config.features.intent_cache.is_none();
        let retry = self.config.intent_retry_us;
        let authority = self.authority.as_ref().map(|a| (a.id, a.lease, a.address));
        let refuse = |s: &Self, reason| s.result_datagram(sender, seq, Outcome::Refused(reason));
        let Some(peer) = self.players.get(&sender) else { return };
        match peer.intents.get(&seq) {
            Some(IntentSlot::Done(result)) if cache => {
                self.stats.intents_duplicate += 1;
                peer.forward(result, out);
                return;
            }
            Some(IntentSlot::Pending { forwarded_us, .. }) => {
                self.stats.intents_duplicate += 1;
                if let Some((id, lease, address)) = authority.filter(|_| now.saturating_sub(*forwarded_us) >= retry) {
                    if let Some(bytes) = retarget(raw, Some(id), Some(lease)) {
                        out.push((address, bytes));
                    }
                    if let Some(IntentSlot::Pending { forwarded_us, .. }) =
                        self.players.get_mut(&sender).and_then(|p| p.intents.get_mut(&seq))
                    {
                        *forwarded_us = now;
                    }
                }
                return;
            }
            _ => {}
        }
        let refusal = if authority.is_none() {
            Some(Reason::AUTHORITY_UNAVAILABLE)
        } else if peer.pending_intents() >= MAX_PENDING_INTENTS {
            Some(Reason::RELAY_BUSY)
        } else {
            None
        };
        if let Some(reason) = refusal {
            self.stats.intents_refused += 1;
            let result = refuse(self, reason);
            if let Some(peer) = self.players.get(&sender) {
                peer.forward(&result, out);
            }
            return;
        }
        let Some((id, lease, address)) = authority else { return };
        if let Some(bytes) = retarget(raw, Some(id), Some(lease)) {
            out.push((address, bytes));
        }
        self.stats.intents_forwarded += 1;
        if let Some(peer) = self.players.get_mut(&sender) {
            peer.intents.insert(seq, IntentSlot::Pending { forwarded_us: now, datagram: raw.to_vec() });
            peer.trim_intents();
        }
    }

    fn intent_result(&mut self, result: &IntentResult, raw: &[u8], out: &mut Vec<Packet>) {
        let player = result.player;
        let connected = self.players.contains_key(&player);
        let Some(peer) = self.players.get_mut(&player).or_else(|| self.resumable.get_mut(&player).map(|(p, _)| p))
        else {
            self.stats.ignored += 1;
            return;
        };
        let stored = match peer.intents.get(&result.client_seq) {
            // Keep the first answer; an authority retry must not change the outcome.
            Some(IntentSlot::Done(first)) => first.clone(),
            _ => {
                peer.intents.insert(result.client_seq, IntentSlot::Done(raw.to_vec()));
                peer.trim_intents();
                raw.to_vec()
            }
        };
        if connected {
            peer.forward(&stored, out);
            self.stats.results_forwarded += 1;
        }
    }

    /// The authority is gone: every pending intent becomes UNCERTAIN, never replayed.
    fn authority_lost(&mut self, out: &mut Vec<Packet>) {
        self.authority = None;
        self.roster_dirty = true;
        let mut results: Vec<(PlayerId, u32)> = Vec::new();
        for (id, peer) in self.players.iter().chain(self.resumable.iter().map(|(id, (p, _))| (id, p))) {
            for (seq, slot) in &peer.intents {
                if matches!(slot, IntentSlot::Pending { .. }) {
                    results.push((*id, *seq));
                }
            }
        }
        for (id, seq) in results {
            let datagram = self.result_datagram(id, seq, Outcome::Uncertain(Reason::AUTHORITY_LOST));
            let peer = match self.players.get_mut(&id) {
                Some(p) => p,
                None => &mut self.resumable.get_mut(&id).expect("listed").0,
            };
            peer.intents.insert(seq, IntentSlot::Done(datagram.clone()));
            if self.players.contains_key(&id) {
                self.players[&id].forward(&datagram, out);
            }
        }
    }

    // ------------------------------------------------------------ events

    fn event(&mut self, event: &Event, raw: &[u8], now: u64, out: &mut Vec<Packet>) {
        if !self.seen_events.insert(event.event_seq) {
            self.stats.events_duplicate += 1;
            return;
        }
        self.event_order.push_back(event.event_seq);
        while self.event_order.len() > EVENT_WINDOW {
            if let Some(old) = self.event_order.pop_front() {
                self.seen_events.remove(&old);
            }
        }
        self.stats.events_accepted += 1;
        if self.objects.get(&event.object).is_some_and(|e| event.generation < e.state.generation) {
            self.stats.records_stale += 1;
            return;
        }
        let mut owners: Vec<PlayerId> = Vec::new();
        let mut sectors: Vec<SectorId> = Vec::new();
        let seq = self.next_update();
        match event.kind {
            EventKind::OwnerChange { from, to } => {
                owners.extend(from.player());
                owners.extend(to.player());
                if let Some(e) = self.objects.get_mut(&event.object) {
                    e.set_owner(to);
                    if !e.publisher.is_authority() && to != Owner::Player(e.publisher) {
                        e.publisher = PlayerId::AUTHORITY;
                    }
                    e.map_revision += 1;
                    e.update_seq = seq;
                    sectors.push(e.state.sector);
                }
            }
            EventKind::Destroy { sector } => {
                sectors.push(sector);
                if let Some(e) = self.remove_object(event.object) {
                    owners.extend(e.state.owner.player());
                    sectors.push(e.state.sector);
                }
            }
            EventKind::SectorChange { from, to } => {
                sectors.extend([from, to]);
                if let Some(e) = self.objects.get_mut(&event.object) {
                    owners.extend(e.state.owner.player());
                    let old = e.state.sector;
                    e.set_sector(to);
                    e.map_revision += 1;
                    e.update_seq = seq;
                    self.index(event.object, Some(old), to);
                }
            }
        }
        let repeats = if self.config.features.event_repeat.is_none() { self.config.event_repeats } else { 1 };
        let interval = self.config.event_repeat_interval_us;
        for peer in self.players.values_mut() {
            let interested = peer.capabilities.has(Capabilities::EVENTS)
                && (owners.contains(&peer.id)
                    || peer
                        .subscription
                        .as_ref()
                        .is_some_and(|sub| sectors.iter().any(|sector| subscriber_interest(sub, *sector).is_some())));
            if !interested {
                continue;
            }
            peer.forward(raw, out);
            self.stats.events_sent += 1;
            if repeats > 1 {
                peer.events.push_back(QueuedEvent {
                    datagram: raw.to_vec(),
                    remaining: repeats - 1,
                    next_us: now + interval,
                });
            }
        }
    }

    fn bye(&mut self, sender: PlayerId, now: u64, out: &mut Vec<Packet>) {
        let _ = now;
        if sender.is_authority() {
            if let Some(a) = self.authority.take() {
                self.retire(a.nonce);
            }
            self.authority_lost(out);
            return;
        }
        if let Some(peer) = self.players.remove(&sender) {
            self.retire(peer.nonce);
            self.scheduler.forget_player(sender);
            self.roster_dirty = true;
        }
    }

    // ------------------------------------------------------------ time

    fn expire(&mut self, now: u64, out: &mut Vec<Packet>) {
        let timeout = self.config.lease_timeout_us;
        let expired: Vec<PlayerId> =
            self.players.values().filter(|p| now.saturating_sub(p.last_seen_us) >= timeout).map(|p| p.id).collect();
        for id in expired {
            let mut peer = self.players.remove(&id).expect("listed");
            self.retire(peer.nonce);
            peer.events.clear();
            peer.pings.clear();
            self.scheduler.forget_player(id);
            self.resumable.insert(id, (peer, now));
            self.roster_dirty = true;
        }
        if self.authority.as_ref().is_some_and(|a| now.saturating_sub(a.last_seen_us) >= timeout) {
            let a = self.authority.take().expect("checked");
            self.retire(a.nonce);
            self.authority_resumable = Some((a, now));
            self.authority_lost(out);
        }
        let window = self.config.resume_window_us;
        let before = self.resumable.len();
        self.resumable.retain(|_, (_, at)| now.saturating_sub(*at) < window);
        self.roster_dirty |= before != self.resumable.len();
        if self.authority_resumable.as_ref().is_some_and(|(_, at)| now.saturating_sub(*at) >= window) {
            self.authority_resumable = None;
        }
    }

    /// One relay tick: expiry, pings, intent retries, event repeats, roster, snapshots.
    pub fn tick(&mut self, now: u64) -> Vec<Packet> {
        let mut out = Vec::new();
        self.expire(now, &mut out);
        if self.config.features.rtt_stats.is_none() {
            let (interval, timeout) = (self.config.ping_interval_us, self.config.ping_timeout_us);
            for peer in self.players.values_mut().chain(self.authority.iter_mut()) {
                while peer.pings.front().is_some_and(|(_, sent)| now.saturating_sub(*sent) >= timeout) {
                    peer.pings.pop_front();
                    peer.rtt.lost();
                }
                if now >= peer.next_ping_us {
                    let seq = peer.next_ping_seq;
                    peer.next_ping_seq = peer.next_ping_seq.wrapping_add(1);
                    peer.pings.push_back((seq, now));
                    if peer.pings.len() > OUTSTANDING_PINGS {
                        peer.pings.pop_front();
                        peer.rtt.lost();
                    }
                    peer.send(&Message::Ping(x4mp_proto::Ping { seq, time_us: now }), &mut out);
                    peer.next_ping_us = now + interval;
                }
            }
        }
        if let Some(a) = self.authority.as_ref() {
            let retry = self.config.intent_retry_us;
            for peer in self.players.values_mut() {
                for slot in peer.intents.values_mut() {
                    if let IntentSlot::Pending { forwarded_us, datagram } = slot {
                        if now.saturating_sub(*forwarded_us) >= retry {
                            a.forward(datagram, &mut out);
                            *forwarded_us = now;
                        }
                    }
                }
            }
        }
        for peer in self.players.values_mut() {
            let mut keep = VecDeque::new();
            while let Some(mut queued) = peer.events.pop_front() {
                if now >= queued.next_us {
                    peer.forward(&queued.datagram, &mut out);
                    self.stats.events_sent += 1;
                    queued.remaining -= 1;
                    queued.next_us = now + self.config.event_repeat_interval_us;
                }
                if queued.remaining > 0 {
                    keep.push_back(queued);
                }
            }
            peer.events = keep;
        }
        if self.config.features.roster.is_none() && (self.roster_dirty || now >= self.next_roster_us) {
            if self.roster_dirty {
                self.roster_revision = self.roster_revision.wrapping_add(1);
            }
            self.roster_dirty = false;
            self.next_roster_us = now + self.config.roster_interval_us;
            let roster = Message::Roster(self.roster());
            for peer in self.players.values().chain(self.authority.iter()) {
                if peer.capabilities.has(Capabilities::ROSTER) {
                    peer.send(&roster, &mut out);
                }
            }
        }
        self.snapshots(now, &mut out);
        out
    }

    fn roster(&self) -> Roster {
        let ms = |us: Option<u64>| us.map(|v| ((v + 500) / 1000).min(u64::from(u16::MAX - 1)) as u16);
        let mut players: Vec<RosterEntry> = self
            .players
            .values()
            .map(|p| RosterEntry {
                player: p.id,
                connected: true,
                rtt_ms: ms(p.rtt.srtt_us()),
                jitter_ms: ms(p.rtt.rttvar_us()),
            })
            .chain(self.resumable.values().map(|(p, _)| RosterEntry {
                player: p.id,
                connected: false,
                rtt_ms: None,
                jitter_ms: None,
            }))
            .collect();
        players.sort_by_key(|e| e.player);
        Roster { revision: self.roster_revision, authority_connected: self.authority.is_some(), players }
    }

    fn snapshots(&mut self, now: u64, out: &mut Vec<Packet>) {
        let V2Session { players, objects, by_sector, scheduler, stats, .. } = self;
        for peer in players.values() {
            let Some(sub) = peer.subscription.as_ref() else { continue };
            let own_position_m = sub
                .own_ship
                .and_then(|id| objects.get(&id))
                .filter(|e| e.state.sector == sub.current)
                .map(|e| quant::position_m(e.state.position));
            let subscriber = Subscriber {
                player: peer.id,
                current_sector: sub.current,
                own_ship: sub.own_ship,
                own_position_m,
                target: sub.target,
                sectors: &sub.sectors,
                all_map: sub.all_map,
            };
            let infos: Vec<ObjectInfo> = if sub.all_map {
                objects.values().map(Entry::info).collect()
            } else {
                let mut ids: BTreeSet<ObjectId> = BTreeSet::new();
                for sector in std::iter::once(sub.current).chain(sub.sectors.iter().map(|s| s.sector)) {
                    if let Some(set) = by_sector.get(&sector) {
                        ids.extend(set.iter().copied());
                    }
                }
                ids.iter().filter_map(|id| objects.get(id)).map(Entry::info).collect()
            };
            let due = scheduler.plan(now, &subscriber, infos.iter());
            // One Snapshot stream per publisher, so each carries its publisher's clock.
            let mut groups: BTreeMap<PlayerId, Vec<&Entry>> = BTreeMap::new();
            for d in due {
                if let Some(e) = objects.get(&d.object) {
                    groups.entry(e.publisher).or_default().push(e);
                }
            }
            for (publisher, entries) in groups {
                let frame = entries.iter().map(|e| e.state.frame).max().unwrap_or(0);
                let mut builder = SnapshotBuilder::new(Some(peer.id), Some(peer.lease), publisher, frame);
                for e in entries {
                    if !matches!(builder.push_raw(&e.raw, e.state.frame), Ok(true)) {
                        if !builder.is_empty() {
                            let full = std::mem::replace(
                                &mut builder,
                                SnapshotBuilder::new(Some(peer.id), Some(peer.lease), publisher, frame),
                            );
                            out.push((peer.address, full.finish()));
                            stats.snapshots_sent += 1;
                        }
                        if !matches!(builder.push_raw(&e.raw, e.state.frame), Ok(true)) {
                            continue;
                        }
                    }
                    stats.records_sent += 1;
                }
                if !builder.is_empty() {
                    out.push((peer.address, builder.finish()));
                    stats.snapshots_sent += 1;
                }
            }
        }
    }
}

fn subscriber_interest(sub: &Subscribe, sector: SectorId) -> Option<Interest> {
    if sector == sub.current {
        return Some(Interest::Full);
    }
    sub.sectors.iter().filter(|s| s.sector == sector).map(|s| s.interest).min().or(sub.all_map.then_some(Interest::Map))
}
