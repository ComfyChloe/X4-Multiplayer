//! identity.py: host display identity and shared sensor observations, scoped away from
//! native IDs.

use std::net::SocketAddr;

use super::{Fields, Packet, Session};
use crate::ordmap::OrderedMap;
use crate::wire::{hex_utf8, is_code, is_lower_hex_pairs, seq, Parsed, Reject, Slot};

/// P: co-op ships 1/2; F: targets 1/2 and markets 3/4; N: committed universe IDs.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Scope {
    Player,
    Fixture,
    Npc,
}

#[derive(Default)]
pub struct Identities {
    rows: OrderedMap<(Scope, u32), (u32, f64, Vec<String>)>,
    cursor: usize,
    next_send: f64,
}

impl Session {
    pub(super) fn receive_identity(&mut self, raw: &[u8], address: SocketAddr, now: f64) {
        if raw.len() > 768 {
            return;
        }
        let Some(fields) = Fields::decode(raw) else { return };
        let _ = self.identity_update(&fields.f, address, now);
    }

    fn identity_update(&mut self, f: &[&str], address: SocketAddr, now: f64) -> Parsed<()> {
        if f.len() != 11 || f[0] != "IDENT" || f[1] != "1" {
            return Err(Reject);
        }
        let (slot, lease) = self.authenticated(f[2], address).ok_or(Reject)?;
        if slot != Slot::P1 || f[3] != lease.as_str() {
            return Err(Reject);
        }
        if self.universe.epoch.is_some_and(|e| e != lease) {
            return Err(Reject);
        }
        let (sequence, scope, entity, code, name, known, contact) =
            (seq(f[4])?, f[5], seq(f[6])?, f[7], f[8], f[9], f[10]);
        let scope = match scope {
            "P" if matches!(entity, 1 | 2) => Scope::Player,
            "F" if matches!(entity, 1..=4) => Scope::Fixture,
            "N" if self.universe.entity_ids.values().any(|ids| ids.contains(&entity)) => Scope::Npc,
            _ => return Err(Reject),
        };
        let name_ok = name.len() <= 320 && is_lower_hex_pairs(name);
        if !is_code(code) || !name_ok || !matches!(known, "0" | "1") || !matches!(contact, "0" | "1") {
            return Err(Reject);
        }
        let decoded = hex_utf8(name)?;
        if decoded.chars().any(|c| c < ' ' || c == '\x7f') {
            return Err(Reject);
        }
        let rows = &mut self.universe.identities.rows;
        if rows.get(&(scope, entity)).is_some_and(|old| sequence <= old.0) {
            return Err(Reject);
        }
        let fields = f[3..].iter().map(|s| (*s).to_owned()).collect();
        rows.insert((scope, entity), (sequence, now, fields));
        Ok(())
    }

    pub(super) fn snapshots_identity(&mut self, now: f64, out: &mut Vec<Packet>) {
        let ids = &mut self.universe.identities;
        if now < ids.next_send {
            return;
        }
        ids.next_send = now + 0.1;
        let (Some(host), Some(guest)) = (self.core.peers.get(&Slot::P1), self.core.peers.get(&Slot::P2)) else {
            return;
        };
        let entity_ids = &self.universe.entity_ids;
        ids.rows
            .retain(|(scope, entity), _| *scope != Scope::Npc || entity_ids.values().any(|set| set.contains(entity)));
        // Only forward fresh observations; the guest expires forced visibility.
        let rows: Vec<&Vec<String>> =
            ids.rows.values().filter(|v| v.2[0] == host.lease.as_str() && now - v.1 < 2.0).map(|v| &v.2).collect();
        let budget = rows.len().min(rows.len().div_ceil(10).clamp(32, 256));
        for i in 0..budget {
            let fields = rows[(ids.cursor + i) % rows.len()];
            out.push((guest.address, format!("IDENT|1|{}|{}", guest.lease, fields.join("|")).into_bytes()));
        }
        ids.cursor += budget;
    }
}
