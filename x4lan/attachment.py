"""Original UATTACH v1 codec and bounded receiver model (dev scope, not yet wired in).

A host observation says whether a saved seed ship is FREE, EXTERNAL (docked outside) or
STORED (inside) at a saved seed dock reference. Indices name references saved in the same
pristine seed, never runtime handles or dock.seed values. This module only encodes, decodes
and admits records; it changes no game or session state. See
reports/ATTACHMENT-STATE-PROTOCOL.txt for the agreed contract.
"""
from dataclasses import dataclass
import math
import re

TAG = 'UATTACH'
VERSION = '1'
STATES = ('FREE', 'EXTERNAL', 'STORED')
MAX_PACKET = 768
MAX_REVISION = MAX_SEQUENCE = 2147483647
MAX_SHIP = 249999
MAX_DOCK = 250000
HEX = re.compile(r'[a-f0-9]{32}\Z')
CANONICAL = re.compile(r'(?:0|[1-9][0-9]{0,9})\Z')


class AttachmentError(ValueError):
    """A record that is not canonical UATTACH v1; nothing was decoded."""


def integer(text, low, high):
    if not isinstance(text, str) or not text.isascii() or not CANONICAL.fullmatch(text):
        raise AttachmentError('noncanonical integer')
    value = int(text)
    if not low <= value <= high:
        raise AttachmentError('integer out of range')
    return value


@dataclass(frozen=True)
class Attachment:
    recipient: str
    epoch: str
    revision: int
    sequence: int
    ship: int
    dock: int
    state: str

    def __post_init__(self):
        if not (isinstance(self.recipient, str) and HEX.fullmatch(self.recipient)
                and isinstance(self.epoch, str) and HEX.fullmatch(self.epoch)):
            raise AttachmentError('lease/epoch identity')
        for value, low, high in ((self.revision, 1, MAX_REVISION), (self.sequence, 1, MAX_SEQUENCE),
                                 (self.ship, 1, MAX_SHIP), (self.dock, 0, MAX_DOCK)):
            if type(value) is not int or not low <= value <= high:
                raise AttachmentError('integer out of range')
        if self.state not in STATES:
            raise AttachmentError('unknown state')
        if (self.state == 'FREE') != (self.dock == 0):
            raise AttachmentError('FREE requires dock 0; attached states require a dock')


def encode(record):
    text = '|'.join((TAG, VERSION, record.recipient, record.epoch, str(record.revision), str(record.sequence),
                     str(record.ship), str(record.dock), record.state))
    if len(text) > MAX_PACKET:
        raise AttachmentError('record exceeds transport MTU')
    return text


def decode(text):
    """The record, or AttachmentError; never a partial record."""
    if not isinstance(text, str) or len(text) > MAX_PACKET or not text.isascii() or not text.isprintable() \
            or any(c.isspace() for c in text):
        raise AttachmentError('record size/encoding')
    f = text.split('|')
    if len(f) != 9 or f[0] != TAG or f[1] != VERSION:
        raise AttachmentError('record tag/version/fields')
    if not HEX.fullmatch(f[2]) or not HEX.fullmatch(f[3]):
        raise AttachmentError('lease/epoch identity')
    return Attachment(f[2], f[3], integer(f[4], 1, MAX_REVISION), integer(f[5], 1, MAX_SEQUENCE),
                      integer(f[6], 1, MAX_SHIP), integer(f[7], 0, MAX_DOCK), f[8])


def identity_map(values, high):
    """{seed index: descriptor} with validated integer keys and non-empty ASCII descriptors."""
    out = {}
    for index, descriptor in dict(values).items():
        if type(index) is not int or not 1 <= index <= high:
            raise AttachmentError('identity map index')
        if not isinstance(descriptor, str) or not descriptor or len(descriptor) > 512 or not descriptor.isascii():
            raise AttachmentError('identity map descriptor')
        out[index] = descriptor
    return out


class Receiver:
    """Admits UATTACH records for ONE recipient against one committed baseline identity map.

    receive() returns an outcome string; only 'applied' retains a record. Sequence high-water
    marks are per ship and survive invalidation within an epoch, so an old in-flight record
    cannot resurrect invalidated state. Time is always supplied by the caller (`now`).
    """
    def __init__(self, lease, ttl):
        if not isinstance(lease, str) or not HEX.fullmatch(lease):
            raise AttachmentError('recipient lease')
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            raise AttachmentError('ttl must be an explicit positive number of seconds')
        self.lease, self.ttl = lease, float(ttl)
        self.epoch = self.revision = None
        self.ships, self.docks = {}, {}
        self.records, self.marks = {}, {}

    def admit(self, epoch, revision, ships, docks):
        """Adopt the committed baseline identity map (ships/docks: {seed index: descriptor})."""
        if not isinstance(epoch, str) or not HEX.fullmatch(epoch) or type(revision) is not int \
                or not 1 <= revision <= MAX_REVISION:
            raise AttachmentError('baseline identity')
        ships, docks = identity_map(ships, MAX_SHIP), identity_map(docks, MAX_DOCK)
        if epoch != self.epoch:
            self.records, self.marks = {}, {}
        elif revision < self.revision:
            raise AttachmentError('baseline revision moved backwards')
        else:
            for ship, kept in list(self.records.items()):
                if ships.get(ship) != self.ships.get(ship) or (
                        kept[1] and docks.get(kept[1]) != self.docks.get(kept[1])):
                    del self.records[ship]
        self.epoch, self.revision, self.ships, self.docks = epoch, revision, ships, docks

    def receive(self, record, now):
        if isinstance(record, str):
            try:
                record = decode(record)
            except AttachmentError:
                return 'invalid'
        if not isinstance(record, Attachment):
            return 'invalid'
        if record.recipient != self.lease:
            return 'wrong-recipient'
        if self.epoch is None or record.epoch != self.epoch:
            return 'unadmitted'
        if record.revision > self.revision:
            return 'future-baseline'
        if record.revision < self.revision:
            return 'stale-baseline'
        if record.ship not in self.ships:
            return 'unknown-ship'
        if record.dock and record.dock not in self.docks:
            return 'unknown-dock'
        mark = self.marks.get(record.ship)
        if mark is not None:
            if record.sequence == mark[0]:
                return 'duplicate' if record == mark[1] else 'conflict'
            if record.sequence < mark[0]:
                return 'stale'
        self.marks[record.ship] = (record.sequence, record)
        self.records[record.ship] = (record.state, record.dock, record.sequence, now)
        return 'applied'

    def current(self, ship, now):
        kept = self.records.get(ship)
        if kept is None or now - kept[3] > self.ttl:
            return None
        return kept

    def remove_ship(self, ship):
        self.records.pop(ship, None)
        self.ships.pop(ship, None)

    def remove_dock(self, dock):
        self.docks.pop(dock, None)
        for ship, kept in list(self.records.items()):
            if kept[1] == dock:
                del self.records[ship]

    def update_descriptor(self, kind, index, descriptor):
        """A changed ship or dock descriptor invalidates the records that depend on it."""
        table, high = (self.ships, MAX_SHIP) if kind == 'ship' else (self.docks, MAX_DOCK) if kind == 'dock' else (None, 0)
        if table is None:
            raise AttachmentError('descriptor kind')
        new = identity_map({index: descriptor}, high)[index]
        if table.get(index) == new:
            return
        table[index] = new
        for ship, kept in list(self.records.items()):
            if (kind == 'ship' and ship == index) or (kind == 'dock' and kept[1] == index):
                del self.records[ship]

    def expire(self, now):
        for ship, kept in list(self.records.items()):
            if now - kept[3] > self.ttl:
                del self.records[ship]
