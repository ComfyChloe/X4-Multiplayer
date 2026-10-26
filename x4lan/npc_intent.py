"""NPC combat intent relay (npc_combat_intent): host NPC target/fire state to the guest.

The host publishes, per relevant NPC, the current target, a firing bitmask per weapon kind and
a manoeuvre hint. The relay validates identity against committed baselines, forwards each new
record once (fresh first) and repeats the latest engaged/firing record every REFRESH seconds,
so a lost datagram delays a state change at the guest instead of ending fire. Damage is never
carried here: hits on a player ship stay with the victim's HEALTH authority.

NINTENT|1|lease|hostEpoch|id|gen|seq|sector|kind|ref|mask|hint
"""
import math

from .host import ATOM, MAX_PACKET, sequence

BUDGET = 32
REFRESH = .5
FRESH = 2.0
LIMIT = 4096


def intent_fields(f):
    """Validate the payload fields after the lease (f[3:]); raise ValueError when invalid."""
    entity, gen, seq = sequence(f[4]), sequence(f[5]), sequence(f[6])
    if not ATOM.fullmatch(f[7]) or not f[7].endswith('_macro'):
        raise ValueError('intent sector')
    kind, ref, mask, hint = f[8], sequence(f[9]), sequence(f[10]), sequence(f[11])
    if not entity or not gen or not seq:
        raise ValueError('intent identity')
    if kind not in ('0', 'P', 'N', 'X') or mask > 7 or hint > 2:
        raise ValueError('intent value')
    if (kind in ('0', 'X') and ref != 0) or (kind == 'P' and ref not in (1, 2)) or (kind == 'N' and not ref):
        raise ValueError('intent target')
    return entity, gen, seq


class NpcIntents:
    def __init__(self, session):
        self.session = session
        self.latest = {}
        self.lease = None

    def receive(self, raw, address, now):
        s = self.session
        s.expire(now)
        if len(raw) > MAX_PACKET:
            return []
        try:
            f = raw.decode('ascii').split('|')
            if len(f) != 12 or f[0] != 'NINTENT' or f[1] != '1':
                return []
            slot, peer = s._peer(f[2], address)
            if slot != 'p1' or not peer or f[3] != peer.lease or s.universe_epoch != peer.lease:
                return []
            entity, gen, seq = intent_fields(f)
            sector = f[7]
            ids = s.entity_ids.get(sector, set())
            if entity not in ids or (f[8] == 'N' and int(f[9]) not in ids):
                return []
            old = self.latest.get(entity)
            if old and (gen < old['gen'] or gen == old['gen'] and seq <= old['seq']):
                return []
            if not old and len(self.latest) >= LIMIT:
                return []
            self.latest[entity] = dict(gen=gen, seq=seq, sector=sector, fields=f[3:], at=now,
                                       sent=-math.inf, forwarded=False,
                                       active=f[8] != '0' or f[10] != '0')
        except (ValueError, OverflowError, UnicodeError):
            pass
        return []

    def snapshots(self, now):
        s = self.session
        host, guest = s.peers.get('p1'), s.peers.get('p2')
        if not host or not guest or s.universe_epoch != host.lease or not s.interest or s.interest[0] != guest.lease:
            return []
        if self.lease != guest.lease:
            self.lease = guest.lease
            for entry in self.latest.values():
                entry['forwarded'] = False
        live = set().union(*s.entity_ids.values()) if s.entity_ids else set()
        for entity in [k for k, v in self.latest.items() if k not in live or now - v['at'] >= FRESH * 2]:
            del self.latest[entity]
        sector = s.interest[2]
        here = [(k, v) for k, v in self.latest.items() if v['sector'] == sector and now - v['at'] < FRESH]
        fresh = sorted((kv for kv in here if not kv[1]['forwarded']), key=lambda kv: kv[1]['at'])
        repeat = sorted((kv for kv in here if kv[1]['forwarded'] and kv[1]['active'] and now - kv[1]['sent'] >= REFRESH),
                        key=lambda kv: kv[1]['sent'])
        result = []
        for _, entry in (fresh + repeat)[:BUDGET]:
            entry['forwarded'] = True
            entry['sent'] = now
            result.append((guest.address, ('NINTENT|1|' + guest.lease + '|' + '|'.join(entry['fields'])).encode('ascii')))
        return result
