"""Stable cooperative ships, independent of player slots and sector boundaries.

The relay arbitrates pilot ownership, not game physics. Host X4 remains the
sole authority for damage, cargo, treasury and mission outcomes.
"""
import math
from .economy import EconomySession, integer, parse_lines
from .host import ATOM, HEX, sequence
from .quests import QuestState

POSITION_LIMIT = 50_000_000


def pose(values):
    if len(values) != 6: raise ValueError('pose length')
    numbers = [float(v) for v in values]
    if not all(math.isfinite(v) and abs(v) <= (POSITION_LIMIT if i < 3 else math.tau)
               for i, v in enumerate(numbers)): raise ValueError('pose bounds')
    return numbers


class CoopSession(EconomySession):
    def __init__(self, *args):
        super().__init__(*args)
        self.owners = {1: 'p1', 2: 'p2'}
        self.epochs = {1: 1, 2: 1}
        self.crew = {}
        self.navigation = {}
        self.body_sectors = {}
        self.game_metrics = {}
        self.authority_revision = 1
        self.order_ships = {}
        self.quest = QuestState()

    def expire(self, now):
        super().expire(now)
        live = {p.lease for p in self.peers.values()}
        expired = [key for key in self.crew if key not in live]
        if expired: self.authority_revision += 1
        self.crew = {k: v for k, v in self.crew.items() if k in live}
        retained = live | {p.lease for p, _ in self.resumable.values()}
        self.body_sectors = {k: v for k, v in self.body_sectors.items() if k in retained}
        self.game_metrics = {k: v for k, v in self.game_metrics.items() if k in live}

    def pilot(self, entity):
        for slot, peer in self.peers.items():
            if self.crew.get(peer.lease, (-1, 0, 0))[1] == entity: return slot
        return '0'

    def busy(self, entity):
        return (any(v['result'] == 'PENDING' and self.order_ships.get(k, (0,))[0] == entity
                    for k, v in self.orders.items()) or
                any(v['result'] == 'PENDING' and v.get('ship') == entity for v in self.commands.values()))

    def dead(self, entity):
        return bool(self.world and self.world[(entity - 1) * 3] <= 1.01)

    def receive(self, raw, address, now):
        self.expire(now)
        if len(raw) > 768: return []
        try: f = raw.decode('ascii').split('|')
        except UnicodeError: return []
        if len(f) < 3 or f[1] != '1': return []
        kind = f[0]
        if kind not in ('CREW', 'NAV', 'BODY2', 'PERF', 'COOPORDER', 'QUEST'):
            # Legacy movement/fire/dock frames must never override cooperative authority.
            if kind in ('STATE', 'FIRE', 'DOCKSTATE', 'BODY', 'ORDER'): return []
            old_leases = {s: p.lease for s, p in self.peers.items()}
            result = super().receive(raw, address, now)
            if kind == 'HELLO':
                for slot, peer in self.peers.items():
                    if old_leases.get(slot) != peer.lease:
                        for entity in (1, 2):
                            nav = self.navigation.get(entity)
                            if self.owners[entity] == slot and nav and nav['lease'] != peer.lease:
                                self.epochs[entity] += 1
                                self.navigation.pop(entity, None)
                                self.authority_revision += 1
            if kind == 'ACTION' and len(f) == 7:
                cmd = self.commands.get(f[4])
                if cmd and 'ship' not in cmd:
                    peer = self.peers.get(cmd['slot'])
                    entity = self.crew.get(peer.lease, (-1, 0, 0))[1] if peer else 0
                    cmd['ship'] = entity
                    if entity not in (1, 2) or self.owners[entity] != cmd['slot'] or self.dead(entity):
                        cmd['result'] = 'REJECTED'
                        return [f'RESULT|1|{f[4]}|REJECTED'.encode()]
            return result
        slot, peer = self._peer(f[2], address)
        if peer is None: return []
        try:
            if kind == 'QUEST' and slot == 'p1' and len(f) == 8:
                self.quest.receive(peer.lease, f[3:])
            elif kind == 'CREW' and len(f) == 6:
                seq, controlled, aboard = sequence(f[3]), integer(f[4], 2), integer(f[5], 2)
                old = self.crew.get(peer.lease, (-1, 0, 0))
                if seq <= old[0] or (controlled and controlled != aboard): return []
                granted = controlled
                if controlled:
                    occupied = self.pilot(controlled)
                    if self.dead(controlled) or occupied not in ('0', slot) or (
                            self.owners[controlled] != slot and self.busy(controlled)):
                        granted = 0
                    elif self.owners[controlled] != slot:
                        self.owners[controlled] = slot
                        self.epochs[controlled] += 1
                        self.navigation.pop(controlled, None)
                        self.authority_revision += 1
                self.crew[peer.lease] = (seq, granted, aboard)
                if old[1:] != (granted, aboard): self.authority_revision += 1
                if controlled and not granted:
                    return [f'PILOT_DENIED|1|{peer.lease}|{seq}|{controlled}'.encode()]
            elif kind == 'NAV' and len(f) == 15:
                entity, epoch, seq = integer(f[3], 2), sequence(f[4]), sequence(f[5])
                if entity not in (1, 2) or self.owners[entity] != slot or epoch != self.epochs[entity]: return []
                if not ATOM.fullmatch(f[6]) or not f[6].endswith('_macro'): return []
                pose(f[7:13]); integer(f[13], 2); sequence(f[14])
                old = self.navigation.get(entity)
                if old and old['lease'] == peer.lease and seq <= old['seq']: return []
                if self.dead(entity): return []
                self.navigation[entity] = dict(lease=peer.lease, seq=seq, at=now, fields=f[3:])
            elif kind == 'BODY2' and len(f) == 14:
                seq, mode, dead, gen = sequence(f[3]), f[4], integer(f[5], 1), sequence(f[6])
                if mode not in ('ship', 'foot', 'suit') or (mode == 'suit' and not gen): return []
                if not ATOM.fullmatch(f[7]) or not f[7].endswith('_macro'): return []
                pose(f[8:]); old = self.bodies.get(peer.lease)
                if old and (seq <= old[0] or dead < int(old[2]) or gen < int(old[3])): return []
                self.bodies[peer.lease] = (seq, mode, str(dead), str(gen), *f[8:])
                self.body_sectors[peer.lease] = f[7]
            elif kind == 'PERF' and len(f) == 9:
                seq = sequence(f[3]); values = [float(v) for v in f[4:7]]
                if not all(math.isfinite(v) and 0 <= v <= 60000 for v in values): return []
                if f[7] not in ('ship', 'foot', 'suit', 'paused', 'waiting'): return []
                if not ATOM.fullmatch(f[8]): return []
                old = self.game_metrics.get(peer.lease)
                if old and seq <= old['seq']: return []
                self.game_metrics[peer.lease] = dict(seq=seq, at=now, callbacks_hz=values[0],
                    interval_avg_ms=values[1], interval_max_ms=values[2], state=f[7], sector=f[8])
            elif kind == 'COOPORDER' and len(f) == 9:
                request, host, entity, epoch, market, lines = f[3:]
                entity, epoch = integer(entity, 2), sequence(epoch)
                if entity not in (1, 2) or not HEX.fullmatch(request): return []
                parse_lines(lines)
                old = self.orders.get(request)
                if old:
                    if self.order_ships.get(request) != (entity, epoch): return []
                elif (self.owners[entity] != slot or epoch != self.epochs[entity] or
                      self.pilot(entity) != slot or self.dead(entity)):
                    return [self.reply(peer, request, 'REJECTED')]
                reply = super().receive(f'ORDER|1|{peer.lease}|{request}|{host}|{market}|{lines}'.encode(), address, now)
                if request in self.orders: self.order_ships[request] = (entity, epoch)
                return reply
        except (ValueError, OverflowError, IndexError): return []
        return []

    def snapshots(self, now):
        result = []
        for address, packet in super().snapshots(now):
            f = packet.decode('ascii').split('|')
            if f[0] in ('PEER', 'BODY', 'FIRE', 'DOCKSTATE'): continue
            if f[0] == 'ORDER_EXEC':
                ship = self.order_ships.get(f[3], (0,))[0]
                if not ship: continue
                f[0], f[4] = 'COOP_EXEC', str(ship)
                packet = '|'.join(f).encode()
            if f[0] == 'COMMAND':
                ship = self.commands.get(f[3], {}).get('ship', 0)
                if not ship: continue
                f[4] = 'p' + str(ship)
                packet = '|'.join(f).encode()
            result.append((address, packet))
        for slot, peer in self.peers.items():
            authority = [str(self.authority_revision)]
            for entity in (1, 2): authority += [self.owners[entity], str(self.epochs[entity]), self.pilot(entity)]
            result.append((peer.address, ('AUTH|1|' + peer.lease + '|' + '|'.join(authority)).encode()))
            for entity, nav in self.navigation.items():
                if self.owners[entity] == slot or now - nav['at'] > .75 or self.dead(entity): continue
                result.append((peer.address, ('NAV|1|' + peer.lease + '|' + nav['lease'] + '|' + '|'.join(nav['fields'])).encode()))
            other = self.peers.get('p2' if slot == 'p1' else 'p1')
            if other and other.lease in self.bodies and other.lease in self.body_sectors:
                b = self.bodies[other.lease]
                fields = [other.lease, *map(str, b[:4]), self.body_sectors[other.lease], *b[4:]]
                result.append((peer.address, ('BODY2|1|' + peer.lease + '|' + '|'.join(fields)).encode()))
        result.extend(self.quest.snapshots(self.peers, now))
        return result
