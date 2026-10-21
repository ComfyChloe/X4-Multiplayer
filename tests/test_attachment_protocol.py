"""UATTACH v1: canonical codec and receiver model, with Python/LuaJIT parity. No game simulation."""
from pathlib import Path
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
        for ships in ({0: 'x'}, {250000: 'x'}, {1: ''}, {1: 'x' * 513}, {True: 'x'}, {1: 'café'}):
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

    def test_safeguard_parity(self):
        lua, P = self.lua, self.P

        class LuaReceiver:
            def __init__(self):
                self.r = P.receiver(ME, 10)

            def call(self, method, *args):
                args = [lua.table_from(a) if isinstance(a, dict) else a for a in args]
                result = lua.globals().pcall(self.r[method], self.r, *args)
                result = result if isinstance(result, tuple) else (result,)
                if not result[0]:
                    return 'error'
                result = tuple(result[1:])
                return None if not result or result == (None,) else result[0] if len(result) == 1 else result
        self.assertEqual(safeguards(LuaReceiver()), SAFEGUARDS)

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
