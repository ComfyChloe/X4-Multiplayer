"""Opt-in UATTACH relay for W01 (dev scope; disabled unless UniverseSession(attachments=True)).

The host (p1) publishes state OBSERVATIONS of saved seed ships (FREE / EXTERNAL / STORED at a
saved dock index); this relay forwards them to the guest's current view only. It never
executes anything, never establishes an epoch and never trusts remote handles: the guest's
native ledger validates dock existence, type and occupancy. See
reports/ATTACHMENT-RELAY-CONTRACT.txt and reports/ATTACHMENT-STATE-PROTOCOL.txt.

Uplink recipientLease is the publisher (host) lease, as with other universe input; downlink
re-encodes the same validated record with the current guest lease as recipient.
"""
import math

from .attachment import Attachment, AttachmentError, MAX_ACTIVE_SHIPS, MAX_SHIP, decode, encode

ATTACH_TTL = 2.0         # seconds an accepted observation stays fresh; the host republishes
ATTACH_RETRY = 0.25      # resend interval of an unchanged fresh record to the guest
ATTACH_BUDGET = 32       # UATTACH datagrams per snapshot tick, fair round-robin


def saved_ships(baseline):
    """{saved ship index: (macro, owner)} of a committed baseline: W01 rows of kind 'ship' with
    IDs 1..249999 only (dynamic ships start at 250000; drops are never attachments)."""
    ships = {}
    for row in baseline.rows:
        f = row.split(',')
        index = int(f[0])
        if f[1] == 'ship' and 1 <= index <= MAX_SHIP:
            ships[index] = (f[2], f[3])
    return ships


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be a finite positive number')
    return float(value)


class AttachmentRelay:
    """Bounded relay state for ONE host epoch and ONE active guest view."""
    def __init__(self, session, ttl=ATTACH_TTL, retry=ATTACH_RETRY, budget=ATTACH_BUDGET):
        self.session = session
        self.ttl, self.retry = positive(ttl, 'ttl'), positive(retry, 'retry')
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 256:
            raise ValueError('budget must be an integer 1..256')
        self.budget = budget
        self.epoch = None
        self.marks = {}          # ship -> (sequence, record): replay fence for the whole epoch
        self.live = {}           # ship -> accepted fresh observation of the current view
        self.view = None         # (guest lease, sector) the live records belong to
        self.cursor = 0
        self._baseline, self._ships = None, {}

    def ships(self, baseline):
        """Parse each immutable committed baseline once, retaining only one view's map."""
        # Baseline is frozen and owns a tuple of immutable row strings. Object
        # identity avoids an O(n) equality comparison on every observation.
        if baseline is not self._baseline:
            self._ships = saved_ships(baseline)
            self._baseline = baseline
        return self._ships

    def current_view(self):
        """(guest, sector, committed baseline of that sector) or None."""
        s = self.session
        guest = s.peers.get('p2')
        if not guest or not s.interest or s.interest[0] != guest.lease:
            return None
        sector = s.interest[2]
        baseline = s.sectors.get(sector)
        return (guest, sector, baseline) if baseline else None

    def track(self, view):
        """Live records belong to one guest lease and sector; a change clears them (marks stay)."""
        key = (view[0].lease, view[1]) if view else None
        if key != self.view:
            self.live, self.view, self.cursor = {}, key, 0
            self._baseline, self._ships = None, {}

    def reset(self):
        """Host loss: stop output and forget everything of this epoch."""
        self.epoch, self.marks, self.live, self.view, self.cursor = None, {}, {}, None, 0
        self._baseline, self._ships = None, {}

    def receive(self, raw, address, now):
        """Admit one host observation. Never replies; returns [] in every case."""
        s = self.session
        s.expire(now)
        try:
            record = decode(raw.decode('ascii'))
        except (AttachmentError, UnicodeError):
            return []
        slot, peer = s._peer(record.recipient, address)
        if slot != 'p1' or not peer or record.recipient != peer.lease:
            return []                                   # only the authenticated host publishes
        if not s.universe_epoch or record.epoch != s.universe_epoch or record.epoch != peer.lease:
            return []                                   # never establishes or replaces an epoch
        if self.epoch != record.epoch:
            self.reset()
            self.epoch = record.epoch
        view = self.current_view()
        self.track(view)
        if not view:
            return []
        guest, sector, baseline = view
        # Only the committed baseline of the guest's current view, and only its saved ships.
        if record.revision != baseline.revision:
            return []
        descriptor = self.ships(baseline).get(record.ship)
        if descriptor is None:
            return []
        mark = self.marks.get(record.ship)
        if mark is not None and record.sequence <= mark[0]:
            return []                                   # duplicate, conflict or stale: no refresh
        if record.ship not in self.live and len(self.live) >= MAX_ACTIVE_SHIPS:
            return []
        self.marks[record.ship] = (record.sequence, record)
        self.live[record.ship] = dict(record=record, sector=sector, revision=baseline.revision,
                                      descriptor=descriptor, at=now, sent=-math.inf)
        return []

    def snapshots(self, now):
        """UATTACH datagrams for the guest: fresh records of the acknowledged baseline only."""
        s = self.session
        host = s.peers.get('p1')
        if not host or not s.universe_epoch or host.lease != s.universe_epoch or (self.epoch and self.epoch != host.lease):
            self.reset()
            return []
        view = self.current_view()
        self.track(view)
        for ship, entry in list(self.live.items()):
            if now - entry['at'] > self.ttl:
                del self.live[ship]                      # expired records are never revived
        if not view or not self.live:
            return []
        guest, sector, baseline = view
        current_ships = self.ships(baseline)
        ready = []
        for ship in sorted(self.live):
            entry = self.live[ship]
            current = current_ships.get(ship) if baseline.revision == entry['revision'] else entry['descriptor']
            if (entry['sector'] == sector and entry['revision'] == s.delivered and current == entry['descriptor']
                    and now - entry['sent'] >= self.retry):
                ready.append(ship)
        out = []
        for i in range(min(self.budget, len(ready))):
            ship = ready[(self.cursor + i) % len(ready)]
            entry = self.live[ship]
            r = entry['record']
            out.append((guest.address, encode(Attachment(guest.lease, r.epoch, r.revision, r.sequence, r.ship,
                                                         r.dock, r.state)).encode('ascii')))
            entry['sent'] = now
        if ready:
            self.cursor = (self.cursor + self.budget) % len(ready)
        return out
