//! NPC motion on the dedicated relay: UFOCUS routing to the server and catch-up of motion
//! samples a client has not received yet (send-on-divergence makes samples sparse).
//! Synthetic peers are not evidence of native multiplayer.

use std::net::SocketAddr;

use x4lan_relay::session::universe::Baseline;
use x4lan_relay::session::{LeaseSource, Level, Packet, Session};
use x4lan_relay::wire::{Atom, Hex32};

const TOKEN: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const BUILD: &str = "world1-test";
const SECTOR: &str = "cluster_14_sector001_macro";
const OTHER: &str = "cluster_01_sector001_macro";
const MACRO: &str = "ship_arg_s_fighter_01_a_macro";

fn addr(n: u16) -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], 11000 + n))
}

fn join(s: &mut Session, slot: u16, now: f64) -> String {
    let nonce = format!("{slot:x}").repeat(32)[..32].to_string();
    let hello = format!("HELLO|1|{TOKEN}|{BUILD}|sector_macro|bubble|p{slot}|{nonce}");
    let reply = s.receive(hello.as_bytes(), addr(slot), now);
    String::from_utf8(reply[0].clone()).unwrap().split('|').nth(3).unwrap().to_owned()
}

fn send(s: &mut Session, slot: u16, packet: String, now: f64) {
    s.receive(packet.as_bytes(), addr(slot), now);
}

fn publish(s: &mut Session, host: &str, revision: u32, sector: &str, ids: &[u32]) {
    let rows: Vec<String> = ids.iter().map(|i| format!("{i},ship,{MACRO},argon,{i},2,3,0,0,0,none,0")).collect();
    let digest = Baseline::create(Hex32::parse(host).unwrap(), revision, Atom::parse(sector).unwrap(), rows.clone())
        .unwrap()
        .digest;
    send(s, 1, format!("UBEGIN|1|{host}|{host}|{revision}|{sector}|{}|{digest}", rows.len()), 0.02);
    for (i, row) in rows.iter().enumerate() {
        send(s, 1, format!("UOBJECT|1|{host}|{host}|{revision}|{}|{row}", i + 1), 0.02);
    }
    send(s, 1, format!("UCOMMIT|1|{host}|{host}|{revision}"), 0.02);
}

fn session() -> (Session, Vec<String>) {
    let mut s =
        Session::new_dedicated_players(Level::Universe, TOKEN, BUILD, "bubble", LeaseSource::Counter(0), 4).unwrap();
    let leases: Vec<String> = (1..=3).map(|slot| join(&mut s, slot, 0.0)).collect();
    publish(&mut s, &leases[0].clone(), 1, SECTOR, &[1, 2, 3]);
    publish(&mut s, &leases[0].clone(), 2, OTHER, &[7]);
    for slot in [2, 3] {
        send(&mut s, slot, format!("UINTEREST|1|{}|1|{SECTOR}", leases[slot as usize - 1]), 0.03);
    }
    (s, leases)
}

fn umove(host: &str, id: u32, seq: u32, time: f64) -> String {
    format!("UMOVE|1|{host}|{host}|{id}|{seq}|{SECTOR}|{time}|1|2|3|0|0|0|100|0|0|0|0|0")
}

fn kinds(packets: &[Packet], to: SocketAddr, kind: &str) -> Vec<String> {
    let prefix = format!("{kind}|");
    packets
        .iter()
        .filter(|(a, _)| *a == to)
        .map(|(_, p)| String::from_utf8(p.clone()).unwrap())
        .filter(|p| p.starts_with(&prefix))
        .collect()
}

#[test]
fn focus_reaches_the_server_once_per_sequence_with_the_client_lease() {
    let (mut s, l) = session();
    send(&mut s, 2, format!("UFOCUS|1|{}|1|2", l[1]), 0.1);
    send(&mut s, 3, format!("UFOCUS|1|{}|4|0007", l[2]), 0.1);
    let first = s.snapshots(0.15);
    let mut focus = kinds(&first, addr(1), "UFOCUS");
    focus.sort();
    let mut expected = vec![format!("UFOCUS|1|{}|1|2|{}", l[0], l[1]), format!("UFOCUS|1|{}|4|7|{}", l[0], l[2])];
    expected.sort();
    assert_eq!(focus, expected);
    // Not repeated without a new sequence; an older or equal sequence is ignored.
    send(&mut s, 2, format!("UFOCUS|1|{}|1|3", l[1]), 0.2);
    assert!(kinds(&s.snapshots(0.25), addr(1), "UFOCUS").is_empty());
    send(&mut s, 2, format!("UFOCUS|1|{}|2|0", l[1]), 0.3);
    assert_eq!(kinds(&s.snapshots(0.35), addr(1), "UFOCUS"), vec![format!("UFOCUS|1|{}|2|0|{}", l[0], l[1])]);
    // The server cannot focus, malformed values are dropped, clients never receive UFOCUS.
    send(&mut s, 1, format!("UFOCUS|1|{}|9|1", l[0]), 0.4);
    send(&mut s, 2, format!("UFOCUS|1|{}|x|1", l[1]), 0.4);
    let quiet = s.snapshots(0.45);
    assert!(kinds(&quiet, addr(1), "UFOCUS").is_empty());
    assert!(kinds(&quiet, addr(2), "UFOCUS").is_empty() && kinds(&quiet, addr(3), "UFOCUS").is_empty());
}

#[test]
fn a_late_client_catches_up_on_sparse_motion_recent_samples_first() {
    let (mut s, l) = session();
    let host = l[0].clone();
    // Entity 1 last diverged 3 s ago (still valid for dead reckoning); 2 is recent.
    send(&mut s, 1, umove(&host, 1, 1, 10.0), 1.0);
    s.snapshots(1.05);
    send(&mut s, 1, umove(&host, 2, 2, 13.0), 4.0);
    let late = join(&mut s, 4, 4.0);
    send(&mut s, 4, format!("UINTEREST|1|{late}|1|{SECTOR}"), 4.0);
    let out = s.snapshots(4.05);
    let rows = kinds(&out, addr(4), "UMOVE");
    assert_eq!(rows.len(), 2, "{rows:?}");
    assert!(rows[0].contains("|2|2|"), "recent sample first: {rows:?}");
    assert!(rows[1].contains("|1|1|"));
    // Clients that already have entity 1 do not get it again.
    assert_eq!(kinds(&out, addr(2), "UMOVE").len(), 1);
    // Nothing older than 5 s is forwarded: after a sector round trip only entity 2 returns.
    send(&mut s, 3, format!("UINTEREST|1|{}|2|{OTHER}", l[2]), 6.4);
    s.snapshots(6.45);
    send(&mut s, 3, format!("UINTEREST|1|{}|3|{SECTOR}", l[2]), 6.5);
    let rows = kinds(&s.snapshots(6.55), addr(3), "UMOVE");
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert!(rows[0].contains("|2|2|"));
}

#[test]
fn a_sector_change_resends_cached_motion() {
    let (mut s, l) = session();
    let host = l[0].clone();
    send(&mut s, 1, umove(&host, 1, 1, 10.0), 1.0);
    assert_eq!(kinds(&s.snapshots(1.05), addr(2), "UMOVE").len(), 1);
    send(&mut s, 2, format!("UINTEREST|1|{}|2|{OTHER}", l[1]), 1.1);
    s.snapshots(1.15);
    send(&mut s, 2, format!("UINTEREST|1|{}|3|{SECTOR}", l[1]), 1.2);
    let back = s.snapshots(1.25);
    assert_eq!(kinds(&back, addr(2), "UMOVE").len(), 1, "replicas were discarded on the sector change");
    assert!(kinds(&back, addr(3), "UMOVE").len() <= 1, "client 3 only gets the loss repeat");
}
