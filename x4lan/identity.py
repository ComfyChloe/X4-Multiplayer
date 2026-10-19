"""Host display identity and shared sensor observations, scoped away from native IDs."""
import re
from .host import sequence

CODE = re.compile(r'[A-Z0-9-]{1,32}\Z')
NAME = re.compile(r'(?:[0-9a-f]{2}){1,160}\Z')


class Identities:
    def __init__(self, session):
        self.session = session
        self.rows = {}
        self.cursor = 0
        self.next_send = 0

    def receive(self, raw, address, now):
        s = self.session
        s.expire(now)
        if len(raw) > 768: return []
        try:
            f = raw.decode('ascii').split('|')
            if len(f) != 11 or f[:2] != ['IDENT','1']: return []
            slot, peer = s._peer(f[2], address)
            if slot != 'p1' or not peer or f[3] != peer.lease: return []
            if s.universe_epoch and s.universe_epoch != peer.lease: return []
            seq, scope, entity, code, name, known, contact = sequence(f[4]), f[5], sequence(f[6]), f[7], f[8], f[9], f[10]
            if scope == 'P':
                if entity not in (1,2): return []
            elif scope == 'N':
                if not any(entity in ids for ids in s.entity_ids.values()): return []
            else: return []
            if not CODE.fullmatch(code) or not NAME.fullmatch(name) or known not in ('0','1') or contact not in ('0','1'): return []
            decoded = bytes.fromhex(name).decode('utf-8')
            if any(ord(c) < 32 or ord(c) == 127 for c in decoded): return []
            key = scope, entity
            old = self.rows.get(key)
            if old and seq <= old[0]: return []
            self.rows[key] = seq, now, f[3:]
        except (ValueError, UnicodeError, OverflowError): pass
        return []

    def snapshots(self, now):
        if now < self.next_send: return []
        self.next_send = now + .1
        s = self.session
        host, guest = s.peers.get('p1'), s.peers.get('p2')
        if not host or not guest: return []
        live = set().union(*s.entity_ids.values()) if s.entity_ids else set()
        self.rows = {k:v for k,v in self.rows.items() if k[0]=='P' or k[1] in live}
        # Only forward fresh observations; the guest expires forced visibility.
        rows = [v for v in self.rows.values() if v[2][0]==host.lease and now-v[1]<2]
        result = []
        for i in range(min(len(rows), min(256, max(32, (len(rows)+9)//10)))):
            fields = rows[(self.cursor+i)%len(rows)][2]
            result.append((guest.address, ('IDENT|1|'+guest.lease+'|'+'|'.join(fields)).encode()))
        self.cursor += len(result)
        return result
