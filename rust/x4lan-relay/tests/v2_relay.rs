//! Protocol-v2 relay: one authority and up to six synthetic clients over loopback UDP.
//!
//! The relay runs on real sockets but is stepped with a simulated 60 Hz clock, so rates
//! and RTTs are deterministic. These are synthetic peers: they prove the relay's
//! routing, admission and scheduling, not native X4 multiplayer behaviour.

use std::collections::BTreeMap;
use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};

use x4lan_relay::v2::net::V2Relay;
use x4lan_relay::v2::{LeaseSource, V2Config, V2Session};
use x4mp_proto::*;

const TOKEN: &str = "0123456789abcdef0123456789abcdef";
const STEP_US: u64 = 16_667;

fn token() -> Token {
    Token::parse_hex(TOKEN).unwrap()
}

fn pid(n: u16) -> PlayerId {
    PlayerId::human(n).unwrap()
}

fn oid(n: u64) -> ObjectId {
    ObjectId::new(n).unwrap()
}

fn sec(n: u32) -> SectorId {
    SectorId::new(n).unwrap()
}

fn ship(id: u64, owner: Owner, sector: u32, x_m: f64, speed: f64, frame: u64) -> ObjectState {
    ObjectState {
        id: oid(id),
        generation: 1,
        frame,
        owner,
        sector: sec(sector),
        position: quant::position([x_m, 0.0, 0.0]).unwrap(),
        rotation: [0; 3],
        velocity: quant::velocity([speed, 0.0, 0.0]).unwrap(),
        angular_velocity: [0; 3],
        mode: MotionMode::Normal,
        ambient: false,
        combat: None,
    }
}

/// A decoded, owned view of what a client received.
#[derive(Debug, Clone)]
enum Got {
    Welcome(Admission),
    Records(PlayerId, Vec<ObjectState>),
    Intent(PlayerId, u32),
    Result(IntentResult),
    Event(Event),
    Roster(Roster),
    Other(#[allow(dead_code)] Kind),
}

struct Client {
    socket: UdpSocket,
    relay: SocketAddr,
    nonce: Nonce,
    id: Option<PlayerId>,
    lease: Option<Lease>,
    inbox: Vec<Got>,
    /// Answer relay pings (a silent client stops doing so).
    responsive: bool,
    frame: u64,
}

impl Client {
    fn new(relay: SocketAddr, nonce: u8) -> Client {
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        socket.set_nonblocking(true).unwrap();
        Client {
            socket,
            relay,
            nonce: [nonce; 16],
            id: None,
            lease: None,
            inbox: Vec::new(),
            responsive: true,
            frame: 0,
        }
    }

    fn send(&self, message: &Message<'_>) {
        self.socket.send_to(&encode(self.id, self.lease, message).unwrap(), self.relay).unwrap();
    }

    fn hello(&self, role: Role, token: Token, want: Option<PlayerId>, resume: Option<Lease>) {
        let hello = Hello {
            token,
            nonce: self.nonce,
            role,
            want_player: want,
            resume_lease: resume,
            capabilities: Capabilities::SUPPORTED,
            build: Label::new("world1-test").unwrap(),
            world: Label::new("seed-01").unwrap(),
        };
        self.socket.send_to(&encode(None, None, &Message::Hello(hello)).unwrap(), self.relay).unwrap();
    }

    fn drain(&mut self) {
        let mut buf = [0u8; 2048];
        loop {
            let n = match self.socket.recv_from(&mut buf) {
                Ok((n, _)) => n,
                Err(e) if e.kind() == ErrorKind::WouldBlock => break,
                Err(e) => panic!("{e}"),
            };
            let packet = decode(&buf[..n]).expect("relay sent a valid datagram");
            if packet.header.kind != Kind::Welcome {
                assert_eq!(packet.header.player, self.id, "addressed to the wrong player");
                assert_eq!(packet.header.lease, self.lease, "wrong lease");
            }
            let got = match packet.message {
                Message::Welcome(w) => {
                    if let Admission::Accepted { player, lease } = w.admission {
                        self.id = Some(player);
                        self.lease = Some(lease);
                    }
                    Got::Welcome(w.admission)
                }
                Message::Ping(ping) => {
                    if self.responsive {
                        self.send(&Message::Pong(Pong { seq: ping.seq, echo_time_us: ping.time_us, time_us: 0 }));
                    }
                    Got::Other(Kind::Ping)
                }
                Message::Snapshot(view) => Got::Records(view.source, view.records().map(|r| *r.state()).collect()),
                Message::Intent(i) => Got::Intent(i.player, i.client_seq),
                Message::IntentResult(r) => Got::Result(r),
                Message::Event(e) => Got::Event(e),
                Message::Roster(r) => Got::Roster(r),
                m => Got::Other(m.kind()),
            };
            self.inbox.push(got);
        }
    }

    fn welcome(&self) -> Option<Admission> {
        self.inbox.iter().rev().find_map(|g| match g {
            Got::Welcome(a) => Some(*a),
            _ => None,
        })
    }

    fn take(&mut self) -> Vec<Got> {
        std::mem::take(&mut self.inbox)
    }

    fn publish(&mut self, state: ObjectState) {
        self.frame += 1;
        let state = ObjectState { frame: self.frame, ..state };
        let id = self.id.unwrap();
        for d in encode_snapshots(self.id, self.lease, id, self.frame, &[state]).unwrap() {
            self.socket.send_to(&d, self.relay).unwrap();
        }
    }

    fn subscribe(
        &self,
        revision: u32,
        current: u32,
        own: u64,
        target: Option<u64>,
        sectors: &[(u32, Interest)],
        all_map: bool,
    ) {
        self.send(&Message::Subscribe(Subscribe {
            revision,
            current: sec(current),
            own_ship: Some(oid(own)),
            target: target.map(oid),
            all_map,
            sectors: sectors.iter().map(|(s, i)| SectorInterest { sector: sec(*s), interest: *i }).collect(),
        }));
    }

    fn intent(&self, seq: u32, player: PlayerId, object: u64) {
        self.send(&Message::Intent(Intent {
            client_seq: seq,
            player,
            kind: IntentKind::CARGO_DROP,
            object: Some(oid(object)),
            generation: 1,
            frame: self.frame,
            params: b"ware=energycells;amount=10",
        }));
    }
}

fn record_counts(got: &[Got]) -> BTreeMap<u64, usize> {
    let mut counts = BTreeMap::new();
    for g in got {
        if let Got::Records(_, states) = g {
            for s in states {
                *counts.entry(s.id.get()).or_insert(0) += 1;
            }
        }
    }
    counts
}

fn events(got: &[Got]) -> Vec<Event> {
    got.iter().filter_map(|g| if let Got::Event(e) = g { Some(*e) } else { None }).collect()
}

fn results(got: &[Got]) -> Vec<IntentResult> {
    got.iter().filter_map(|g| if let Got::Result(r) = g { Some(*r) } else { None }).collect()
}

fn last_roster(got: &[Got]) -> Option<Roster> {
    got.iter().rev().find_map(|g| if let Got::Roster(r) = g { Some(r.clone()) } else { None })
}

struct Harness {
    relay: V2Relay,
    now: u64,
}

impl Harness {
    fn new(max_players: u8) -> Harness {
        let mut config = V2Config::new(token(), "world1-test", "seed-01");
        config.max_players = max_players;
        config.lease_timeout_us = 2_000_000;
        config.resume_window_us = 4_000_000;
        config.ping_interval_us = 200_000;
        let session = V2Session::new(config, LeaseSource::Counter(1000)).unwrap();
        Harness { relay: V2Relay::bind("127.0.0.1", 0, session).unwrap(), now: 0 }
    }

    fn addr(&self) -> SocketAddr {
        self.relay.local_addr().unwrap()
    }

    /// One 60 Hz relay step; then every client drains (and answers pings).
    fn step(&mut self, clients: &mut [&mut Client]) {
        self.now += STEP_US;
        self.relay.step(self.now);
        for c in clients.iter_mut() {
            c.drain();
        }
    }
}

#[test]
fn authority_and_five_clients_full_session() {
    let mut h = Harness::new(5);
    let relay = h.addr();
    let mut auth = Client::new(relay, 0xA0);
    let mut c: Vec<Client> = (1..=7).map(|n| Client::new(relay, n)).collect();
    let (c1, rest) = c.split_at_mut(1);
    let c1 = &mut c1[0];
    let (c2, rest) = rest.split_at_mut(1);
    let c2 = &mut c2[0];
    let (c3, rest) = rest.split_at_mut(1);
    let c3 = &mut c3[0];
    let (c4, rest) = rest.split_at_mut(1);
    let c4 = &mut c4[0];
    let (c5, rest) = rest.split_at_mut(1);
    let c5 = &mut c5[0];
    let (c6, rest) = rest.split_at_mut(1);
    let c6 = &mut c6[0];
    let c7 = &mut rest[0];

    // --- admission ---------------------------------------------------------
    auth.hello(Role::Authority, token(), None, None);
    let mut intruder = Client::new(relay, 0xEE);
    intruder.hello(Role::Player, Token([0; 16]), None, None);
    h.step(&mut [&mut auth, &mut intruder]);
    assert!(matches!(auth.welcome(), Some(Admission::Accepted { player, .. }) if player == PlayerId::AUTHORITY));
    assert!(intruder.inbox.is_empty(), "a wrong token gets no reply");
    for client in [&mut *c1, &mut *c2, &mut *c3, &mut *c4, &mut *c5, &mut *c6] {
        client.hello(Role::Player, token(), None, None);
        h.step(&mut [client]);
    }
    for (n, client) in [&*c1, &*c2, &*c3, &*c4, &*c5].iter().enumerate() {
        assert_eq!(client.id, Some(pid(n as u16 + 1)), "lowest free ID first");
    }
    assert_eq!(c6.welcome(), Some(Admission::Refused(RefuseReason::FULL)), "sixth player refused: slot full");
    assert_eq!(h.relay.session.players().len(), 5);

    // --- world set-up: the authority assigns each player its ship --------
    let p = |n| Owner::Player(pid(n));
    let xenon = Owner::Faction(FactionId::new(7).unwrap());
    let mut frame = 1000;
    let npcs = |frame: u64| {
        let mut attacker = ship(203, xenon, 1, 30_000.0, 0.0, frame);
        attacker.combat =
            Some(CombatIntent { target: Some(oid(102)), firing: 0b0001, manoeuvre: Manoeuvre::AttackRun });
        vec![
            ship(201, xenon, 1, 1_000.0, 0.0, frame),  // within weapons range of ship 101
            ship(202, xenon, 1, 50_000.0, 0.0, frame), // far: tier 3
            attacker,                                  // attacking ship 102
            ObjectState { ambient: true, ..ship(204, xenon, 1, 10.0, 0.0, frame) },
            ship(205, xenon, 3, 0.0, 0.0, frame), // station in a map-only sector
        ]
    };
    let mut world = vec![
        ship(101, p(1), 1, 0.0, 400.0, frame),
        ship(102, p(2), 1, 20_000.0, 400.0, frame),
        ship(103, p(3), 1, 40_000.0, 400.0, frame),
        ship(104, p(4), 2, 0.0, 400.0, frame),
        ship(105, p(5), 2, 5_000.0, 400.0, frame),
    ];
    world.extend(npcs(frame));
    for d in encode_snapshots(auth.id, auth.lease, PlayerId::AUTHORITY, frame, &world).unwrap() {
        auth.socket.send_to(&d, relay).unwrap();
    }
    let adjacent = [(2, Interest::Adjacent)];
    c1.subscribe(1, 1, 101, None, &adjacent, false);
    c2.subscribe(1, 1, 102, None, &adjacent, false);
    c3.subscribe(1, 1, 103, None, &adjacent, false);
    c4.subscribe(1, 2, 104, None, &[], true);
    c5.subscribe(1, 2, 105, None, &[], true);
    // An out-of-date subscription revision is ignored.
    c1.subscribe(0, 2, 101, None, &[], false);
    h.step(&mut [&mut auth, c1, c2, c3, c4, c5, c6]);
    assert_eq!(h.relay.session.object_count(), 10);
    // c4 keeps its set-up inbox: map-tier objects are sent once, at subscription.
    for client in [&mut auth, &mut *c1, &mut *c2, &mut *c3, &mut *c5] {
        client.take();
    }

    // --- two seconds of traffic: players fly, the authority runs NPCs -----
    for _ in 0..120 {
        frame += 1;
        for d in encode_snapshots(auth.id, auth.lease, PlayerId::AUTHORITY, frame, &npcs(frame)).unwrap() {
            auth.socket.send_to(&d, relay).unwrap();
        }
        c1.publish(ship(101, p(1), 1, 0.0, 400.0, 0));
        c2.publish(ship(102, p(2), 1, 20_000.0, 400.0, 0));
        c3.publish(ship(103, p(3), 1, 40_000.0, 400.0, 0));
        c4.publish(ship(104, p(4), 2, 0.0, 400.0, 0));
        c5.publish(ship(105, p(5), 2, 5_000.0, 400.0, 0));
        h.step(&mut [&mut auth, c1, c2, c3, c4, c5]);
    }
    let got1 = c1.take();
    let n1 = record_counts(&got1);
    // Tier 1 (other players' ships, 400 m/s): 30 Hz.
    assert!((58..=61).contains(&n1[&102]), "{n1:?}");
    assert!((58..=61).contains(&n1[&103]), "{n1:?}");
    // Own ship never comes back; ambient traffic never; unsubscribed sector never.
    assert_eq!((n1.get(&101), n1.get(&204), n1.get(&205)), (None, None, None), "{n1:?}");
    // Tier 2 (NPC within weapons range, at rest): 20 Hz.
    assert!((39..=41).contains(&n1[&201]), "{n1:?}");
    // Tier 3 (far NPC at rest: 1 Hz; adjacent-sector ships at 400 m/s: 2 Hz).
    assert!((1..=3).contains(&n1[&202]), "{n1:?}");
    assert!((3..=5).contains(&n1[&104]), "{n1:?}");
    // The attacker of ship 102 is tier 2 for player 2 but tier 3 for player 1.
    assert!((1..=3).contains(&n1[&203]), "{n1:?}");
    let n2 = record_counts(&c2.take());
    assert!((39..=41).contains(&n2[&203]), "{n2:?}");
    // Players 4/5: their sector at full detail, sector 1 and 3 at map tier (on change only).
    let n4 = record_counts(&c4.take());
    assert!((58..=61).contains(&n4[&105]), "{n4:?}");
    assert_eq!((n4[&101], n4[&201], n4[&205]), (1, 1, 1), "map tier sends once: {n4:?}");
    assert_eq!(n4.get(&104), None);
    // Records carry the publisher: player ships come from their own clients.
    assert!(got1
        .iter()
        .any(|g| matches!(g, Got::Records(src, s) if *src == pid(2) && s.iter().any(|o| o.id == oid(102)))));
    // The authority receives every player's own-ship stream at the full client rate.
    let auth_got = auth.take();
    let from_clients: usize =
        auth_got.iter().filter(|g| matches!(g, Got::Records(src, _) if !src.is_authority())).count();
    assert_eq!(from_clients, 5 * 120);
    // RTT: pongs return one 16.667 ms step later; the roster carries it.
    assert_eq!(h.relay.session.rtt(pid(1)).unwrap().srtt_us(), Some(STEP_US));
    let roster = last_roster(&got1).expect("roster");
    assert!(roster.authority_connected);
    assert_eq!(roster.players.len(), 5);
    assert!(roster.players.iter().all(|e| e.connected && e.rtt_ms == Some(17)), "{roster:?}");

    // --- intents ------------------------------------------------------------
    c1.intent(1, pid(1), 101);
    c2.intent(1, pid(3), 103); // claims to be player 3: dropped
    c3.intent(7, pid(3), 103); // the authority will never answer this one
    h.step(&mut [&mut auth, c1, c2, c3]);
    let forwarded: Vec<(PlayerId, u32)> =
        auth.take().iter().filter_map(|g| if let Got::Intent(p, s) = g { Some((*p, *s)) } else { None }).collect();
    assert_eq!(forwarded, vec![(pid(1), 1), (pid(3), 7)]);
    assert_eq!(h.relay.session.stats.intents_spoofed, 1);
    let accepted = IntentResult { client_seq: 1, player: pid(1), outcome: Outcome::Accepted, frame: frame + 1 };
    auth.send(&Message::IntentResult(accepted));
    h.step(&mut [&mut auth, c1]);
    assert_eq!(results(&c1.take()), vec![accepted]);
    // A retransmitted intent is answered from the relay's cache, not re-executed.
    c1.intent(1, pid(1), 101);
    h.step(&mut [&mut auth, c1]);
    assert_eq!(results(&c1.take()), vec![accepted]);
    assert!(!auth.take().iter().any(|g| matches!(g, Got::Intent(..))));
    // The unanswered intent is re-forwarded to the authority after 250 ms.
    for _ in 0..16 {
        h.step(&mut [&mut auth, c1, c2, c3]);
    }
    let retries = auth.take().iter().filter(|g| matches!(g, Got::Intent(p, 7) if *p == pid(3))).count();
    assert_eq!(retries, 1);

    // --- events -------------------------------------------------------------
    for client in [&mut *c1, &mut *c2, &mut *c4] {
        client.take();
    }
    let destroy =
        Event { event_seq: 1, frame, object: oid(201), generation: 1, kind: EventKind::Destroy { sector: sec(1) } };
    auth.send(&Message::Event(destroy));
    auth.send(&Message::Event(destroy)); // the authority's own repeat: deduplicated
    let give = Event {
        event_seq: 2,
        frame,
        object: oid(202),
        generation: 1,
        kind: EventKind::OwnerChange { from: xenon, to: p(2) },
    };
    auth.send(&Message::Event(give));
    for _ in 0..10 {
        frame += 1;
        // A late record of the destroyed object must not resurrect it.
        let late = ship(201, xenon, 1, 1_000.0, 0.0, frame);
        for d in encode_snapshots(auth.id, auth.lease, PlayerId::AUTHORITY, frame, &[late]).unwrap() {
            auth.socket.send_to(&d, relay).unwrap();
        }
        h.step(&mut [&mut auth, c1, c2, c3, c4, c5]);
    }
    assert_eq!(h.relay.session.stats.events_duplicate, 1);
    let got1 = c1.take();
    let e1 = events(&got1);
    assert_eq!(e1.iter().filter(|e| e.event_seq == 1).count(), 3, "event repeated three times");
    assert_eq!(record_counts(&got1).get(&201), None, "destroyed object not replicated");
    assert!(h.relay.session.object(oid(201)).is_none());
    assert!(events(&c4.take()).iter().any(|e| e.event_seq == 1), "map subscriber sees the destruction");
    assert!(events(&c2.take()).iter().any(|e| e.event_seq == 2), "new owner sees the ownership change");
    // Player 2 now owns 202 and may publish it; player 1 may not.
    c2.publish(ship(202, p(2), 1, 50_000.0, 50.0, 0));
    c1.publish(ship(202, p(2), 1, 0.0, 0.0, 0));
    c1.publish(ship(102, p(2), 1, 0.0, 0.0, 0));
    let rejected = h.relay.session.stats.records_rejected;
    h.step(&mut [&mut auth, c1, c2]);
    assert_eq!(h.relay.session.stats.records_rejected, rejected + 2);
    assert_eq!(h.relay.session.object(oid(202)).unwrap().1, pid(2));

    // --- disconnects ----------------------------------------------------------
    c5.send(&Message::Bye(Bye { reason: 0 }));
    h.step(&mut [&mut auth, c5]);
    assert!(!h.relay.session.players().contains(&pid(5)));
    c6.inbox.clear();
    c6.hello(Role::Player, token(), None, None);
    h.step(&mut [&mut auth, c6]);
    assert_eq!(c6.id, Some(pid(5)), "a freed ID is reused");
    // Player 4 goes silent; after the 2 s lease it is listed as disconnected.
    c4.responsive = false;
    for _ in 0..125 {
        h.step(&mut [&mut auth, c1, c2, c3, c6]);
        c4.socket.recv_from(&mut [0u8; 2048]).ok();
    }
    assert!(!h.relay.session.players().contains(&pid(4)));
    let roster = last_roster(&c1.take()).unwrap();
    let four = roster.players.iter().find(|e| e.player == pid(4)).unwrap();
    assert!(!four.connected);
    // ID 4 stays reserved for resume: a newcomer finds the session full.
    c7.hello(Role::Player, token(), None, None);
    h.step(&mut [&mut auth, c7]);
    assert_eq!(c7.welcome(), Some(Admission::Refused(RefuseReason::FULL)));
    // Player 4 resumes with its lease from a new instance (new nonce, new port).
    let mut c4b = Client::new(relay, 0x44);
    let old_lease = c4.lease;
    c4b.hello(Role::Player, token(), Some(pid(4)), old_lease);
    h.step(&mut [&mut auth, &mut c4b]);
    assert_eq!(c4b.welcome(), Some(Admission::Accepted { player: pid(4), lease: old_lease.unwrap() }));
    // A wrong resume lease is refused.
    let mut thief = Client::new(relay, 0x66);
    thief.hello(Role::Player, token(), Some(pid(4)), Lease::new(12345));
    h.step(&mut [&mut auth, &mut thief]);
    assert_eq!(thief.welcome(), Some(Admission::Refused(RefuseReason::RESUME_REJECTED)));

    // --- authority loss -------------------------------------------------------
    auth.send(&Message::Bye(Bye { reason: 0 }));
    h.step(&mut [c1, c3]);
    let uncertain = results(&c3.take());
    assert_eq!(
        uncertain,
        vec![IntentResult {
            client_seq: 7,
            player: pid(3),
            outcome: Outcome::Uncertain(Reason::AUTHORITY_LOST),
            frame: 0
        }]
    );
    c1.intent(2, pid(1), 101);
    h.step(&mut [c1]);
    let refused = results(&c1.take());
    assert_eq!(refused[0].outcome, Outcome::Refused(Reason::AUTHORITY_UNAVAILABLE));
    // A fresh authority would be a fresh world: it needs a fresh relay.
    let mut auth2 = Client::new(relay, 0xA1);
    auth2.hello(Role::Authority, token(), None, None);
    h.step(&mut [&mut auth2]);
    assert_eq!(auth2.welcome(), Some(Admission::Refused(RefuseReason::AUTHORITY_TAKEN)));
}

#[test]
fn six_players_at_the_default_limit() {
    let session = V2Session::new(V2Config::new(token(), "world1-test", "seed-01"), LeaseSource::Os).unwrap();
    assert_eq!(session.config().max_players, 6);
    let mut relay = V2Relay::bind("127.0.0.1", 0, session).unwrap();
    let addr = relay.local_addr().unwrap();
    let mut clients: Vec<Client> = (1..=7).map(|n| Client::new(addr, n)).collect();
    let mut now = 0;
    for client in clients.iter_mut() {
        client.hello(Role::Player, token(), None, None);
        now += STEP_US;
        relay.step(now);
        client.drain();
    }
    let ids: Vec<Option<PlayerId>> = clients.iter().map(|c| c.id).collect();
    assert_eq!(ids, vec![Some(pid(1)), Some(pid(2)), Some(pid(3)), Some(pid(4)), Some(pid(5)), Some(pid(6)), None]);
    assert_eq!(clients[6].welcome(), Some(Admission::Refused(RefuseReason::FULL)));
    // Lost Welcome: the same instance asking again gets the same identity.
    clients[2].inbox.clear();
    clients[2].hello(Role::Player, token(), None, None);
    now += STEP_US;
    relay.step(now);
    clients[2].drain();
    assert!(matches!(clients[2].welcome(), Some(Admission::Accepted { player, .. }) if player == pid(3)));
}

// ---------------------------------------------------------------- pure-session checks

fn addr(port: u16) -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], port))
}

fn hello_bytes(role: Role, nonce: u8, build: &str, world: &str) -> Vec<u8> {
    encode(
        None,
        None,
        &Message::Hello(Hello {
            token: token(),
            nonce: [nonce; 16],
            role,
            want_player: None,
            resume_lease: None,
            capabilities: Capabilities::SUPPORTED,
            build: Label::new(build).unwrap(),
            world: Label::new(world).unwrap(),
        }),
    )
    .unwrap()
}

fn admission(out: &[(SocketAddr, Vec<u8>)]) -> Admission {
    match decode(&out[0].1).unwrap().message {
        Message::Welcome(w) => w.admission,
        m => panic!("{m:?}"),
    }
}

#[test]
fn admission_refusals_and_hostile_datagrams() {
    let mut s = V2Session::new(V2Config::new(token(), "world1-test", "seed-01"), LeaseSource::Counter(0)).unwrap();
    let out = s.receive(&hello_bytes(Role::Player, 1, "world1-other", "seed-01"), addr(1), 0);
    assert_eq!(admission(&out), Admission::Refused(RefuseReason::BUILD_MISMATCH));
    let out = s.receive(&hello_bytes(Role::Player, 1, "world1-test", "seed-02"), addr(1), 0);
    assert_eq!(admission(&out), Admission::Refused(RefuseReason::WORLD_MISMATCH));
    let out = s.receive(&hello_bytes(Role::Player, 1, "world1-test", "seed-01"), addr(1), 0);
    assert!(matches!(admission(&out), Admission::Accepted { .. }));
    // One endpoint cannot hold two identities.
    let out = s.receive(&hello_bytes(Role::Player, 2, "world1-test", "seed-01"), addr(1), 0);
    assert_eq!(admission(&out), Admission::Refused(RefuseReason::ADDRESS_IN_USE));
    // Version-1 text, garbage, oversize and unauthenticated v2 datagrams change nothing.
    let ping = encode(Some(pid(1)), Lease::new(999), &Message::Ping(Ping { seq: 1, time_us: 1 })).unwrap();
    for raw in [&b"HELLO|1|x"[..], &[0xF4, 0x32, 2], &[0u8; 1300], &ping] {
        assert!(s.receive(raw, addr(9), 0).is_empty());
    }
    assert_eq!(s.stats.malformed, 3);
    assert_eq!(s.stats.unauthenticated, 1);
    assert_eq!(s.players(), vec![pid(1)]);
    // Clients cannot publish objects the authority has not assigned to them.
    let lease = match admission(&s.receive(&hello_bytes(Role::Player, 1, "world1-test", "seed-01"), addr(1), 0)) {
        Admission::Accepted { lease, .. } => lease,
        a => panic!("{a:?}"),
    };
    let claim = ship(500, Owner::Player(pid(1)), 1, 0.0, 0.0, 1);
    let d = encode_snapshots(Some(pid(1)), Some(lease), pid(1), 1, &[claim]).unwrap();
    s.receive(&d[0], addr(1), 1);
    assert_eq!(s.object_count(), 0);
    assert_eq!(s.stats.records_rejected, 1);
}
