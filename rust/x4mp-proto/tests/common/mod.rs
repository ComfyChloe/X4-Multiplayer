//! Deterministic generators shared by the property and fuzz tests (no external crates).
#![allow(dead_code)]

use x4mp_proto::*;

/// xorshift64*: small, deterministic, good enough for test-case generation.
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }

    pub fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    pub fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }

    pub fn chance(&mut self, percent: u64) -> bool {
        self.below(100) < percent
    }

    pub fn u8(&mut self) -> u8 {
        self.next() as u8
    }

    pub fn u16(&mut self) -> u16 {
        self.next() as u16
    }

    pub fn u32(&mut self) -> u32 {
        self.next() as u32
    }

    /// Mostly small values, sometimes extremes: edge cases matter more than spread.
    pub fn interesting_u64(&mut self) -> u64 {
        match self.below(6) {
            0 => 0,
            1 => u64::MAX,
            2 => self.below(16),
            3 => 1 << self.below(64),
            _ => self.next(),
        }
    }

    pub fn f64_in(&mut self, lo: f64, hi: f64) -> f64 {
        lo + (hi - lo) * (self.next() >> 11) as f64 / (1u64 << 53) as f64
    }

    pub fn bytes(&mut self, n: usize) -> Vec<u8> {
        (0..n).map(|_| self.u8()).collect()
    }
}

pub fn player(rng: &mut Rng) -> PlayerId {
    PlayerId::human(1 + rng.below(u64::from(PlayerId::MAX_HUMAN)) as u16).unwrap()
}

pub fn any_player(rng: &mut Rng) -> PlayerId {
    if rng.chance(20) {
        PlayerId::AUTHORITY
    } else {
        player(rng)
    }
}

pub fn object(rng: &mut Rng) -> ObjectId {
    ObjectId::new(rng.interesting_u64().max(1)).unwrap()
}

pub fn opt_object(rng: &mut Rng) -> Option<ObjectId> {
    ObjectId::new(rng.interesting_u64())
}

pub fn sector(rng: &mut Rng) -> SectorId {
    SectorId::new(rng.u32().max(1)).unwrap()
}

pub fn owner(rng: &mut Rng) -> Owner {
    loop {
        if let Some(o) = Owner::from_wire(rng.u16()) {
            return o;
        }
    }
}

pub fn lease(rng: &mut Rng) -> Option<Lease> {
    Lease::new(rng.interesting_u64())
}

pub fn state(rng: &mut Rng, frame: u64) -> ObjectState {
    let age = if rng.chance(50) { 0 } else { rng.below(frame.min(u64::from(u32::MAX)) + 1) };
    ObjectState {
        id: object(rng),
        generation: rng.u32(),
        frame: frame - age,
        owner: owner(rng),
        sector: sector(rng),
        position: [rng.u32() as i32, rng.u32() as i32, rng.u32() as i32],
        rotation: [rng.u16() as i16, rng.u16() as i16, rng.u16() as i16],
        velocity: [rng.u32() as i32, rng.u32() as i32, rng.u32() as i32],
        angular_velocity: [rng.u16() as i16, rng.u16() as i16, rng.u16() as i16],
        mode: [MotionMode::Normal, MotionMode::Boost, MotionMode::Travel, MotionMode::Highway][rng.below(4) as usize],
        ambient: rng.chance(10),
        combat: rng.chance(40).then(|| CombatIntent {
            target: opt_object(rng),
            firing: rng.u8(),
            manoeuvre: Manoeuvre::from_u8(rng.u8()),
        }),
    }
}

pub const LABEL_CHARS: &[u8] = b"abcXYZ019_.-";

pub fn label_text(rng: &mut Rng) -> String {
    let n = 1 + rng.below(64) as usize;
    (0..n).map(|_| LABEL_CHARS[rng.below(LABEL_CHARS.len() as u64) as usize] as char).collect()
}

/// Owned inputs a random message borrows from.
pub struct Storage {
    pub build: String,
    pub world: String,
    pub params: Vec<u8>,
}

impl Storage {
    pub fn new(rng: &mut Rng) -> Self {
        let n = rng.below(513) as usize;
        Storage { build: label_text(rng), world: label_text(rng), params: rng.bytes(n) }
    }
}

/// A random valid non-snapshot message of the given kind.
pub fn message<'a>(rng: &mut Rng, kind: Kind, s: &'a Storage) -> Message<'a> {
    match kind {
        Kind::Hello => {
            let role = if rng.chance(20) { Role::Authority } else { Role::Player };
            Message::Hello(Hello {
                token: Token(rng.bytes(16).try_into().unwrap()),
                nonce: rng.bytes(16).try_into().unwrap(),
                role,
                want_player: if role == Role::Player && rng.chance(50) { Some(player(rng)) } else { None },
                resume_lease: lease(rng),
                capabilities: Capabilities(rng.u32()),
                build: Label::new(&s.build).unwrap(),
                world: Label::new(&s.world).unwrap(),
            })
        }
        Kind::Welcome => Message::Welcome(Welcome {
            nonce: rng.bytes(16).try_into().unwrap(),
            admission: if rng.chance(70) {
                Admission::Accepted { player: any_player(rng), lease: Lease::new(rng.next().max(1)).unwrap() }
            } else {
                Admission::Refused(RefuseReason::new(rng.u8().max(1)).unwrap())
            },
            capabilities: Capabilities(rng.u32()),
            max_players: rng.u8(),
            relay_epoch: rng.next(),
            relay_time_us: rng.interesting_u64(),
        }),
        Kind::Ping => Message::Ping(Ping { seq: rng.u32(), time_us: rng.interesting_u64() }),
        Kind::Pong => {
            Message::Pong(Pong { seq: rng.u32(), echo_time_us: rng.interesting_u64(), time_us: rng.interesting_u64() })
        }
        Kind::Subscribe => {
            let mut sectors: Vec<SectorInterest> = Vec::new();
            for _ in 0..rng.below(33) {
                let sec = sector(rng);
                if sectors.iter().all(|e| e.sector != sec) {
                    let interest = [Interest::Full, Interest::Adjacent, Interest::Map][rng.below(3) as usize];
                    sectors.push(SectorInterest { sector: sec, interest });
                }
            }
            Message::Subscribe(Subscribe {
                revision: rng.u32(),
                current: sector(rng),
                own_ship: opt_object(rng),
                target: opt_object(rng),
                all_map: rng.chance(50),
                sectors,
            })
        }
        Kind::Snapshot => unreachable!("snapshots are built with encode_snapshots"),
        Kind::Intent => Message::Intent(Intent {
            client_seq: rng.u32(),
            player: player(rng),
            kind: IntentKind::new(rng.u16().max(1)).unwrap(),
            object: opt_object(rng),
            generation: rng.u32(),
            frame: rng.interesting_u64(),
            params: &s.params,
        }),
        Kind::IntentResult => Message::IntentResult(IntentResult {
            client_seq: rng.u32(),
            player: player(rng),
            outcome: match rng.below(3) {
                0 => Outcome::Accepted,
                1 => Outcome::Refused(Reason::new(rng.u16().max(1)).unwrap()),
                _ => Outcome::Uncertain(Reason::new(rng.u16().max(1)).unwrap()),
            },
            frame: rng.interesting_u64(),
        }),
        Kind::Event => Message::Event(Event {
            event_seq: rng.u32(),
            frame: rng.interesting_u64(),
            object: object(rng),
            generation: rng.u32(),
            kind: match rng.below(3) {
                0 => EventKind::OwnerChange { from: owner(rng), to: owner(rng) },
                1 => EventKind::Destroy { sector: sector(rng) },
                _ => EventKind::SectorChange { from: sector(rng), to: sector(rng) },
            },
        }),
        Kind::Roster => {
            let mut ids: Vec<u16> = (0..rng.below(65)).map(|_| 1 + rng.below(0x7FFE) as u16).collect();
            ids.sort_unstable();
            ids.dedup();
            let opt = |rng: &mut Rng| if rng.chance(20) { None } else { Some(rng.below(65535) as u16) };
            Message::Roster(Roster {
                revision: rng.u32(),
                authority_connected: rng.chance(50),
                players: ids
                    .into_iter()
                    .map(|id| RosterEntry {
                        player: PlayerId::human(id).unwrap(),
                        connected: rng.chance(70),
                        rtt_ms: opt(rng),
                        jitter_ms: opt(rng),
                    })
                    .collect(),
            })
        }
        Kind::Bye => Message::Bye(Bye { reason: rng.u8() }),
    }
}

/// A random valid datagram of any kind (snapshots included).
pub fn datagram(rng: &mut Rng) -> Vec<u8> {
    let kind = Kind::ALL[rng.below(Kind::ALL.len() as u64) as usize];
    let header_player = if rng.chance(30) { None } else { Some(any_player(rng)) };
    let header_lease = lease(rng);
    if kind == Kind::Snapshot {
        let frame = rng.interesting_u64();
        let states: Vec<ObjectState> = (0..rng.below(20)).map(|_| state(rng, frame)).collect();
        let source = any_player(rng);
        return encode_snapshots(header_player, header_lease, source, frame, &states).unwrap().swap_remove(0);
    }
    let storage = Storage::new(rng);
    let m = message(rng, kind, &storage);
    encode(header_player, header_lease, &m).unwrap()
}
