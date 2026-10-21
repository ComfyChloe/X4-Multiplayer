"""UATTACH v1: canonical codec and receiver model, with Python/LuaJIT parity. No game simulation."""
from pathlib import Path
import random
import unittest

from x4lan import attachment as A
from test_game_adapter import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
LUA = ROOT / 'extension/x4_lan_lab/ui/attachment_protocol.lua'
ME, OTHER = 'a' * 32, 'b' * 32
EPOCH, EPOCH2 = 'e' * 32, 'f' * 32


def text(seq=1, ship=1, dock=10, state='EXTERNAL', revision=5, recipient=ME, epoch=EPOCH):
    return f'UATTACH|1|{recipient}|{epoch}|{revision}|{seq}|{ship}|{dock}|{state}'


VALID = [text(), text(state='STORED'), text(dock=0, state='FREE'), text(ship=249999, dock=250000),
         text(seq=2147483647, revision=2147483647), text(ship=1, dock=1)]
INVALID = [
    '', 'UATTACH', text().replace('UATTACH', 'UATTACK'), text().replace('|1|', '|2|', 1), text() + '|',
    text() + '|x', '|'.join(text().split('|')[:8]), text().replace(f'|{EPOCH}|', f'|{EPOCH.upper()}|'),
    text(recipient='a' * 31), text(seq='01'), text(seq='+1'), text(seq=' 1'), text(seq='1e3'), text(seq='1.0'),
    text(seq=0), text(seq=-1), text(seq='2147483648'), text(seq='12345678901'), text(revision=0),
    text(ship=0), text(ship=250000), text(dock=250001), text(dock=5, state='FREE'), text(dock=0, state='STORED'),
    text(dock=0, state='EXTERNAL'), text(state='free'), text(state='DOCKED'), text(state=''), text(seq=''),
    text(seq='nan'), text(seq='inf'), text().replace('EXTERNAL', 'EXTERNAL\t'), text().replace('|10|', '| 10|'),
    text(state='STOREDé'), 'UATTACH|1|' + 'a' * 760, text().replace('|', '||', 1)]


class CodecTests(unittest.TestCase):
    def test_valid_records_round_trip_canonically(self):
        for record in VALID:
            with self.subTest(record):
                self.assertEqual(A.encode(A.decode(record)), record)

    def test_invalid_records_are_rejected_atomically(self):
        for record in INVALID:
            with self.subTest(record):
                with self.assertRaises(A.AttachmentError):
                    A.decode(record)

    def test_records_cannot_be_built_out_of_contract(self):
        for kwargs in (dict(dock=3, state='FREE'), dict(dock=0, state='STORED'), dict(ship=0), dict(sequence=True),
                       dict(revision=2 ** 31), dict(recipient=ME.upper()), dict(state='DOCKED')):
            base = dict(recipient=ME, epoch=EPOCH, revision=5, sequence=1, ship=1, dock=10, state='EXTERNAL')
            base.update(kwargs)
            with self.subTest(kwargs), self.assertRaises(A.AttachmentError):
                A.Attachment(**base)

    def test_codec_is_not_registered_or_activated(self):
        self.assertNotIn('attachment_protocol', (ROOT / 'extension/x4_lan_lab/ui.xml').read_text())
        users = [p.name for p in list((ROOT / 'x4lan').glob('*.py')) + list((ROOT / 'extension').rglob('*.lua'))
                 if p.name not in ('attachment.py', 'attachment_protocol.lua') and any(
                     needle in p.read_text(errors='replace')
                     for needle in ('X4LAN_ATTACHMENT_PROTOCOL', 'from .attachment', 'x4lan.attachment', 'UATTACH'))]
        self.assertEqual(users, [], 'codec only: nothing uses it yet')


def timeline():
    """Scripted receiver steps shared by both languages: (method, args, expected)."""
    ships = {1: 'ship:argon:1', 2: 'ship:argon:2'}
    docks = {10: 'dock:pad:10', 11: 'dock:bay:11'}
    return [
        ('receive', (text(), 0.0), 'unadmitted'),
        ('admit', (EPOCH, 5, ships, docks), None),
        ('receive', ('garbage', 1.0), 'invalid'),
        ('receive', (text(recipient=OTHER), 1.0), 'wrong-recipient'),
        ('receive', (text(epoch=EPOCH2), 1.0), 'unadmitted'),
        ('receive', (text(revision=6), 1.0), 'future-baseline'),
        ('receive', (text(revision=4), 1.0), 'stale-baseline'),
        ('receive', (text(ship=3), 1.0), 'unknown-ship'),
        ('receive', (text(dock=12), 1.0), 'unknown-dock'),
        ('receive', (text(seq=1), 1.0), 'applied'),
        ('current', (1, 1.5), ('EXTERNAL', 10, 1, 1.0)),
        ('receive', (text(seq=1), 2.0), 'duplicate'),
        ('receive', (text(seq=1, state='STORED'), 2.0), 'conflict'),
        ('receive', (text(seq=3, ship=2, dock=11, state='STORED'), 2.0), 'applied'),
        ('receive', (text(seq=2, ship=1, dock=0, state='FREE'), 2.5), 'applied'),     # reordered across ships
        ('receive', (text(seq=1, ship=1), 3.0), 'stale'),
        ('current', (1, 3.0), ('FREE', 0, 2, 2.5)),
        ('current', (2, 3.0), ('STORED', 11, 3, 2.0)),
        ('current', (2, 12.1), None),                                                 # ttl 10 s
        ('update_descriptor', ('ship', 1, 'ship:argon:1:refit'), None),
        ('current', (1, 3.0), None),
        ('receive', (text(seq=2, ship=1, dock=0, state='FREE'), 3.0), 'duplicate'),   # mark survives
        ('current', (1, 3.0), None),
        ('receive', (text(seq=4, ship=1, dock=10), 3.0), 'applied'),
        ('remove_dock', (11,), None),
        ('current', (2, 3.0), None),
        ('receive', (text(seq=5, ship=2, dock=11, state='STORED'), 3.0), 'unknown-dock'),
        ('admit', (EPOCH, 6, {1: 'ship:argon:1:refit'}, {10: 'dock:pad:10:moved'}), None),
        ('current', (1, 3.0), None),                                                  # dock descriptor changed
        ('receive', (text(seq=4, ship=1, dock=10, revision=6), 3.0), 'conflict'),     # same seq, other revision
        ('receive', (text(seq=3, ship=1, dock=10, revision=6), 3.0), 'stale'),
        ('receive', (text(seq=6, ship=2, dock=0, state='FREE', revision=6), 3.0), 'unknown-ship'),
        ('receive', (text(seq=7, ship=1, dock=0, state='FREE', revision=6), 4.0), 'applied'),
        ('remove_ship', (1,), None),
        ('current', (1, 4.0), None),
        ('admit', (EPOCH2, 1, ships, docks), None),                                   # new epoch resets marks
        ('receive', (text(seq=1, epoch=EPOCH2, revision=1), 5.0), 'applied'),
        ('expire', (16.0,), None),
        ('current', (1, 5.0), None),
    ]


SHIPS = {1: 'ship:argon:1', 2: 'ship:argon:2'}
DOCKS = {10: 'dock:pad:10'}


def safeguards(r):
    """Astra's 21:17 safeguards as one scenario; r wraps either language and returns results
    (an error is recorded as 'error' so both languages are compared step by step)."""
    out = []
    caller = dict(SHIPS)
    out.append(r.call('admit', EPOCH, 5, caller, DOCKS))
    caller[3] = 'ship:late:3'                                          # caller mutation after admit
    out.append(r.call('receive', text(ship=3, dock=0, state='FREE'), 0.0))   # -> unknown-ship
    out.append(r.call('receive', text(seq=1), 0.0))                   # applied at 0
    out.append(r.call('receive', text(seq=1), 9.0))                   # duplicate, no refresh
    out.append(r.call('current', 1, 10.5))                            # None: ttl measured from 0
    out.append(r.call('admit', EPOCH, 5, SHIPS, DOCKS))               # identical map, same revision: no-op
    out.append(r.call('admit', EPOCH, 5, {1: 'ship:argon:1:other'}, DOCKS))   # rebinding: error
    out.append(r.call('admit', EPOCH, 6, SHIPS, {0: 'bad'}))          # invalid dock map: error, atomic
    out.append(r.call('receive', text(seq=2, revision=5, ship=2), 11.0))    # still revision 5: applied
    out.append(r.call('admit', EPOCH, 4, SHIPS, DOCKS))               # backwards: error
    out.append(r.call('expire', 30.0))
    out.append(r.call('receive', text(seq=2, revision=5, ship=2, state='STORED'), 30.0))  # mark kept: conflict
    out.append(r.call('remove_ship', 2))
    out.append(r.call('receive', text(seq=2, revision=5, ship=2), 30.0))    # ship gone: unknown-ship
    out.append(r.call('update_descriptor', 'ship', 2, 'ship:argon:2'))
    out.append(r.call('receive', text(seq=2, revision=5, ship=2), 30.0))    # re-added, mark kept: duplicate
    out.append(r.call('receive', text(seq=3, revision=5, ship=2), 100.0))   # applied at receiver time 100
    out.append(r.call('current', 2, 50.0))                            # clock rollback: still age 0
    out.append(r.call('current', 2, 55.0))                            # receiver time 105
    out.append(r.call('current', 2, 61.0))                            # receiver time 111: expired
    for bad in (float('nan'), float('inf'), -1.0):
        out.append(r.call('current', 2, bad))                         # invalid clock: error
    out.append(r.call('admit', EPOCH2, 1, {i: 's%d' % i for i in range(1, 4098)}, {}))   # >4096 ships: error
    out.append(r.call('admit', EPOCH2, 1, {i: 's%d' % i for i in range(1, 4097)}, {}))   # exactly 4096: ok
    out.append(r.call('update_descriptor', 'ship', 5000, 's5000'))    # 4097th active ship: error
    return out


SAFEGUARDS = [None, 'unknown-ship', 'applied', 'duplicate', None, None, 'error', 'error', 'applied', 'error', None,
              'conflict', None, 'unknown-ship', None, 'duplicate', 'applied', ('EXTERNAL', 10, 3, 100.0),
              ('EXTERNAL', 10, 3, 100.0), None, 'error', 'error', 'error', 'error', None, 'error']


class PyReceiver:
    def __init__(self):
        self.r = A.Receiver(ME, 10)

    def call(self, method, *args):
        try:
            return getattr(self.r, method)(*args)
        except A.AttachmentError:
            return 'error'


class LuaReceiver:
    def __init__(self, lua, protocol):
        self.lua, self.r = lua, protocol.receiver(ME, 10)

    def call(self, method, *args):
        args = [self.lua.table_from(a) if isinstance(a, dict) else a for a in args]
        result = self.lua.globals().pcall(self.r[method], self.r, *args)
        result = result if isinstance(result, tuple) else (result,)
        if not result[0]:
            return 'error'
        result = tuple(result[1:])
        return None if not result or result == (None,) else result[0] if len(result) == 1 else result


def mutations(seed, count):
    """Deterministic near-miss records: character edits, field swaps and boundary numbers."""
    rng = random.Random(seed)
    alphabet = 'UATCHERFOSXD|0123456789abcdef-+ .eE\t'
    numbers = ['0', '1', '01', '9', '249999', '250000', '250001', '2147483647', '2147483648', '12345678901', '-1']
    for _ in range(count):
        record = rng.choice(VALID)
        kind = rng.randrange(5)
        if kind == 0:                                               # replace one character
            i = rng.randrange(len(record))
            record = record[:i] + rng.choice(alphabet) + record[i + 1:]
        elif kind == 1:                                             # insert one character
            i = rng.randrange(len(record) + 1)
            record = record[:i] + rng.choice(alphabet) + record[i:]
        elif kind == 2:                                             # delete one character
            i = rng.randrange(len(record))
            record = record[:i] + record[i + 1:]
        elif kind == 3:                                             # boundary number in a numeric field
            f = record.split('|')
            f[rng.randrange(4, 8)] = rng.choice(numbers)
            record = '|'.join(f)
        else:                                                       # swap two fields
            f = record.split('|')
            a, b = rng.sample(range(len(f)), 2)
            f[a], f[b] = f[b], f[a]
            record = '|'.join(f)
        yield record


def random_operations(seed, count):
    """Deterministic receiver operations over a small domain, including clock rollbacks."""
    rng = random.Random(seed)
    ops = [('admit', EPOCH, 5, {1: 's1', 2: 's2', 3: 's3'}, {10: 'd10', 11: 'd11'}),
           ('receive', text(seq=1, ship=1), 0.0), ('receive', text(seq=1, ship=1), 0.0),        # fixed opening:
           ('receive', text(seq=1, ship=1, state='STORED'), 0.0), ('current', 1, 0.0)]          # each outcome once
    now, last = 0.0, {1: (1, 5)}
    for _ in range(count):
        kind = rng.random()
        now = max(0.0, now + rng.choice((0.0, 0.5, 1.0, 3.0, 7.0, -4.0)))
        if kind < 0.6:
            state = rng.choice(('FREE', 'EXTERNAL', 'STORED'))
            dock = 0 if state == 'FREE' else rng.choice((10, 11, 12))
            ship, seq, revision = rng.randint(1, 4), rng.randint(1, 12), rng.choice((5, 5, 5, 6, 4))
            if ship in last and rng.random() < .3:
                seq, revision = last[ship]                          # replay a sequence, maybe with other content
            last[ship] = (seq, revision)
            ops.append(('receive', text(seq=seq, ship=ship, dock=dock, state=state, revision=revision), now))
            if rng.random() < .3:
                ops.append(('current', ship, now))                  # read back while possibly live
        elif kind < 0.75:
            ops.append(('current', rng.randint(1, 4), now))
        elif kind < 0.82:
            ops.append(('expire', now))
        elif kind < 0.87:
            ops.append(('update_descriptor', rng.choice(('ship', 'dock')), rng.choice((1, 2, 10, 11)),
                        rng.choice(('s1', 's2', 'd10', 'changed'))))
        elif kind < 0.92:
            ops.append(('remove_ship', rng.randint(1, 3)) if rng.random() < .5 else ('remove_dock', rng.choice((10, 11))))
        else:
            ops.append(('admit', EPOCH, rng.choice((5, 6)), {1: 's1', 2: rng.choice(('s2', 'changed'))}, {10: 'd10'}))
    return ops


class ReceiverTests(unittest.TestCase):
    def test_safeguards(self):
        self.assertEqual(safeguards(PyReceiver()), SAFEGUARDS)

    def test_python_timeline(self):
        r = A.Receiver(ME, 10)
        for step, (method, args, expected) in enumerate(timeline()):
            with self.subTest(step=step, method=method):
                self.assertEqual(getattr(r, method)(*args), expected)

    def test_explicit_ttl_and_validated_admission(self):
        for ttl in (None, 0, -1, float('inf'), float('nan'), True, '5'):
            with self.subTest(ttl=ttl), self.assertRaises(A.AttachmentError):
                A.Receiver(ME, ttl)
        r = A.Receiver(ME, 5)
        for ships in ({0: 'x'}, {250000: 'x'}, {1: ''}, {1: 'x' * 513}, {True: 'x'}, {1: 'caf\u00e9'},
                      {1: 'two words'}, {1: 'tab\there'}, {1: 'line\n'}):
            with self.subTest(ships=ships), self.assertRaises(A.AttachmentError):
                r.admit(EPOCH, 5, ships, {})
        r.admit(EPOCH, 5, {1: 'x'}, {})
        with self.assertRaises(A.AttachmentError):
            r.admit(EPOCH, 4, {1: 'x'}, {})                 # baselines never move backwards in an epoch


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class ParityTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(LUA.read_text())
        self.P = self.lua.globals().X4LAN_ATTACHMENT_PROTOCOL

    def table(self, value):
        return self.lua.table_from(value) if isinstance(value, dict) else value

    def test_codec_parity_over_the_shared_corpus(self):
        for record in VALID + INVALID:
            with self.subTest(record):
                try:
                    py = A.decode(record)
                except A.AttachmentError:
                    py = None
                lua = self.P.decode(record)
                lua = lua[0] if isinstance(lua, tuple) else lua
                self.assertEqual(py is None, lua is None)
                if py is not None:
                    self.assertEqual({k: lua[k] for k in ('recipient', 'epoch', 'revision', 'sequence', 'ship', 'dock', 'state')},
                                     py.__dict__)
                    self.assertEqual(self.P.encode(lua), record)

    def test_receiver_timeline_parity(self):
        r = self.P.receiver(ME, 10)
        for step, (method, args, expected) in enumerate(timeline()):
            with self.subTest(step=step, method=method):
                result = r[method](r, *[self.table(a) for a in args])
                if isinstance(result, tuple):
                    result = tuple(result)
                self.assertEqual(result, expected)

    def test_observation_matches_the_controller_shape_in_both_languages(self):
        py, lua = A.Receiver(ME, 10), self.P.receiver(ME, 10)
        ships = {1: 'seed:ship:1:argon', 2: 'seed:ship:2:argon'}
        py.admit(EPOCH, 5, ships, {10: 'seed:dock:10'})
        lua.admit(lua, EPOCH, 5, self.lua.table_from(ships), self.lua.table_from({10: 'seed:dock:10'}))
        self.assertEqual(py.receive(text(seq=3), 2.0), 'applied')
        self.assertEqual(lua.receive(lua, text(seq=3), 2.0), 'applied')
        expected = dict(epoch=EPOCH, sequence=3, state='EXTERNAL', dock=10, identity='seed:ship:1:argon', received_at=2.0)
        self.assertEqual(py.observation(1, 4.0), expected)
        got = lua.observation(lua, 1, 4.0)
        self.assertEqual({k: got[k] for k in expected}, expected)
        self.assertIsNone(py.observation(2, 4.0))
        self.assertIsNone(lua.observation(lua, 2, 4.0))
        self.assertIsNone(py.observation(1, 13.0))                  # expired
        self.assertIsNone(lua.observation(lua, 1, 13.0))
        for descriptor in ('two words', 'tab\there'):
            with self.subTest(descriptor=descriptor):
                ok = self.lua.globals().pcall(lua.admit, lua, EPOCH, 6, self.lua.table_from({1: descriptor}),
                                              self.lua.table_from({}))
                self.assertFalse(ok[0] if isinstance(ok, tuple) else ok)

    def test_lua_retains_a_private_copy_of_a_received_table(self):
        # A:primary review of 0875417: receive(table) must not keep the caller-owned table.
        out = self.lua.execute(f'''
         local P=X4LAN_ATTACHMENT_PROTOCOL
         local r=P.receiver("{ME}",10)
         r:admit("{EPOCH}",5,{{[1]="s1"}},{{[1]="d1"}})
         local row={{recipient="{ME}",epoch="{EPOCH}",revision=5,sequence=1,ship=1,dock=1,state="EXTERNAL"}}
         local applied=r:receive(row,0)
         row.state="STORED"                                    -- the caller changes its own table
         local replay=r:receive("UATTACH|1|{ME}|{EPOCH}|5|1|1|1|STORED",1)
         local state=r:current(1,1)
         return applied,replay,state''')
        self.assertEqual(tuple(out), ('applied', 'conflict', 'EXTERNAL'))

    def test_lua_reuses_validation_only_for_an_unchanged_identity_map(self):
        out = self.lua.execute(f'''
         local P=X4LAN_ATTACHMENT_PROTOCOL
         local r=P.receiver("{ME}",10)
         local docks={{[1]="d1",[2]="d2"}}
         r:admit("{EPOCH}",5,{{[1]="s1"}},docks)
         local applied=r:receive("UATTACH|1|{ME}|{EPOCH}|5|1|1|2|EXTERNAL",0)
         r:admit("{EPOCH}",6,{{[1]="s1"}},docks)            -- same table, same contents: record kept
         local kept=r:current(1,0)
         docks[2]="d2:moved"                                -- the caller changes its own table
         r:admit("{EPOCH}",7,{{[1]="s1"}},docks)            -- revalidated: dependent record dropped
         local dropped=r:current(1,0)
         docks[1]="bad descriptor"
         local ok=pcall(r.admit,r,"{EPOCH}",8,{{[1]="s1"}},docks)
         r:remove_dock(1)                                   -- live copy only; the snapshot is untouched
         local same=pcall(r.admit,r,"{EPOCH}",7,{{[1]="s1"}},{{[1]="d1",[2]="d2:moved"}})
         return applied,kept,dropped,ok,same,r.revision''')
        self.assertEqual(tuple(out), ('applied', 'EXTERNAL', None, False, True, 7))

    def test_safeguard_parity(self):
        self.assertEqual(safeguards(LuaReceiver(self.lua, self.P)), SAFEGUARDS)

    def test_differential_fuzz_of_the_codec(self):
        accepted = 0
        for record in mutations(20261021, 3000):
            try:
                py = A.decode(record).__dict__
            except A.AttachmentError:
                py = None
            lua = self.P.decode(record)
            lua = lua[0] if isinstance(lua, tuple) else lua
            lua = None if lua is None else {k: lua[k] for k in py or {}} if py else {}
            self.assertEqual(py is None, lua is None, record)
            if py is not None:
                accepted += 1
                self.assertEqual(lua, py, record)
        self.assertGreater(accepted, 50, 'the corpus also exercises accepted near-misses')

    def test_differential_fuzz_of_the_receiver(self):
        py, lua = PyReceiver(), LuaReceiver(self.lua, self.P)
        outcomes = set()
        for step, (method, *args) in enumerate(random_operations(1021, 2500)):
            a, b = py.call(method, *args), lua.call(method, *args)
            self.assertEqual(a, b, (step, method, args))
            outcomes.add(a if isinstance(a, str) else type(a).__name__)
        for expected in ('applied', 'duplicate', 'conflict', 'stale', 'future-baseline', 'stale-baseline',
                         'unknown-ship', 'unknown-dock', 'error', 'tuple'):
            self.assertIn(expected, outcomes, 'fuzz coverage')

    def test_lua_rejects_implicit_ttl_and_bad_admission(self):
        for ttl in ('nil', '0', '-1', 'math.huge', '0/0', 'true'):
            with self.subTest(ttl=ttl):
                ok = self.lua.eval(f'pcall(X4LAN_ATTACHMENT_PROTOCOL.receiver, "{ME}", {ttl})')
                self.assertFalse(ok[0] if isinstance(ok, tuple) else ok)
        ok = self.lua.execute(f'''
         local r=X4LAN_ATTACHMENT_PROTOCOL.receiver("{ME}",5)
         local bad={{pcall(r.admit,r,"{EPOCH}",5,{{[0]="x"}},{{}})}}
         r:admit("{EPOCH}",5,{{[1]="x"}},{{}})
         local back={{pcall(r.admit,r,"{EPOCH}",4,{{[1]="x"}},{{}})}}
         return bad[1],back[1]''')
        self.assertEqual(tuple(ok), (False, False))


if __name__ == '__main__':
    unittest.main()
