"""BOLT/BEND codec (reports/PROJECTILE-VISUAL-PROTOCOL.txt): Python and Lua, explicit direction."""
from dataclasses import replace
import math
from pathlib import Path
import re
import unittest

from x4lan import projectile_protocol as pp
from test_game_adapter import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
LUA = ROOT / 'extension/x4_lan_lab/ui/projectile_protocol.lua'
A, B = 'a' * 32, '0123456789abcdef' * 2
CLIENT_BOLT = ('BOLT|1|' + A + '|2|7|4|19|1234.5|cluster_14_sector001_macro|1|1030|-10000|160.25'
               '|2300|0|-0.5|0.25|-6.2831853071795862|0|0.69999999999999996')


def bolt(direction='client', **kw):
    base = dict(direction=direction, lease=A, source_lease=B if direction == 'forwarded' else None, entity=2,
                epoch=7, gen=4, id=19, time=1234.5, sector='cluster_14_sector001_macro', kind=1, x=1030.0,
                y=-10000.0, z=160.25, vx=2300.0, vy=0.0, vz=-0.5, yaw=0.25, pitch=-2 * math.pi, roll=0.0, ttl=.7)
    base.update(kw); return pp.Bolt(**base)


def end(direction='client', **kw):
    base = dict(direction=direction, lease=A, source_lease=B if direction == 'forwarded' else None, entity=1,
                epoch=2147483647, gen=1, id=1, time=0.0)
    base.update(kw); return pp.End(**base)


def forwarded(raw):
    """The forwarded form of a client packet: sourceLease B inserted after the recipient lease A."""
    f = raw.split('|'); return '|'.join(f[:3] + [B] + f[3:])


def bad_packets():
    """(direction, packet) pairs that must both be refused, by Python and by Lua."""
    f = CLIENT_BOLT.split('|')
    def field(i, value): g = list(f); g[i] = value; return '|'.join(g)
    out = [('forwarded', CLIENT_BOLT), ('client', forwarded(CLIENT_BOLT)), ('client', CLIENT_BOLT + '|'),
           ('client', CLIENT_BOLT.rsplit('|', 1)[0]), ('client', 'BOLX' + CLIENT_BOLT[4:]), ('client', field(1, '2')),
           ('client', CLIENT_BOLT.replace('|', '||', 1)), ('client', CLIENT_BOLT + '\n'), ('client', ' ' + CLIENT_BOLT),
           ('client', 'BEND|1|' + A + '|1|1|1|1'), ('client', 'BEND|1|' + A + '|1|1|1|1|0|0'), ('sideways', CLIENT_BOLT)]
    out += [('client', field(2, v)) for v in ('A' * 32, 'a' * 31, 'a' * 33, 'g' * 32)]
    out += [('client', field(3, v)) for v in ('0', '3', '01', '+1', '1.0', '')]
    for i in (4, 5, 6):
        out += [('client', field(i, v)) for v in ('0', '2147483648', '01', '-1', '1e3', '1.0', '9999999999', '12345678901')]
    out += [('client', field(9, v)) for v in ('0', '4', '1.0')]
    out += [('client', field(8, v)) for v in ('a' * 97, 'bad/sector', 'bad sector', '')]
    numbers = ('nan', 'inf', '-inf', '1e999', '0x10', '1_0', '.5', '5.', '+1', '--1', '1e', '', '1,5')
    for i in [7] + list(range(10, 20)):
        out += [('client', field(i, v)) for v in numbers]
    out += [('client', field(7, v)) for v in ('-1', '1000000000001', '1e13')]
    out += [('client', field(i, v)) for i in (10, 11, 12) for v in ('50000000.000001', '-5.1e7')]
    out += [('client', field(i, v)) for i in (13, 14, 15) for v in ('100000.0001', '-1e6')]
    out += [('client', field(i, v)) for i in (16, 17, 18) for v in ('6.2831853071795871', '-7')]
    out += [('client', field(19, v)) for v in ('0', '-0', '-1', '3.0000000000000004', '4')]
    out += [('client', CLIENT_BOLT.replace('cluster_14', 'clüster_14')), ('client', CLIENT_BOLT + '0' * 800)]
    return out


class PythonCodecTests(unittest.TestCase):
    def test_spec_client_bolt_decodes_to_named_fields_and_reserializes_exactly(self):
        m = pp.parse(CLIENT_BOLT, 'client')
        self.assertEqual((m.direction, m.lease, m.source_lease, m.entity, m.epoch, m.gen, m.id, m.time, m.sector, m.kind),
                         ('client', A, None, 2, 7, 4, 19, 1234.5, 'cluster_14_sector001_macro', 1))
        self.assertEqual((m.x, m.y, m.z, m.vx, m.vy, m.vz, m.yaw, m.pitch, m.roll, m.ttl),
                         (1030, -10000, 160.25, 2300, 0, -.5, .25, -2 * math.pi, 0, .7))
        self.assertEqual(pp.serialize(m), CLIENT_BOLT.encode())
        f = pp.parse(forwarded(CLIENT_BOLT).encode(), 'forwarded')
        self.assertEqual((f.lease, f.source_lease), (A, B))
        self.assertEqual(replace(f, direction='client', source_lease=None), m)

    def test_exact_roundtrip_in_both_directions(self):
        for direction in pp.DIRECTIONS:
            for message in (bolt(direction), bolt(direction, kind=3, x=-50000000.0, y=5e7, vx=-1e5, vz=1e5, yaw=2 * math.pi,
                                                  roll=-0.0, ttl=3.0, time=1e12, sector='x' * 96),
                            bolt(direction, time=0.1, x=1e-300, ttl=5e-324), end(direction), end(direction, time=1e12, entity=2)):
                with self.subTest(message=message):
                    raw = pp.serialize(message)
                    self.assertEqual(pp.parse(raw, direction), message)
                    self.assertEqual(pp.serialize(pp.parse(raw, direction)), raw)
                    self.assertLessEqual(len(raw), 768)

    def test_non_canonical_text_decodes_but_serializes_canonically(self):
        m = pp.parse(CLIENT_BOLT.replace('|1234.5|', '|1.2345E3|').replace('|160.25|', '|0160.250|'), 'client')
        self.assertEqual(pp.serialize(m), CLIENT_BOLT.encode())

    def test_adversarial_packets_are_refused(self):
        for direction, raw in bad_packets():
            with self.subTest(direction=direction, raw=raw[:90]):
                with self.assertRaises(pp.ProtocolError):
                    pp.parse(raw, direction)

    def test_serialize_validates_every_field(self):
        cases = [bolt(entity=3), bolt(entity=True), bolt(epoch=0), bolt(gen=2147483648), bolt(id=1.0), bolt(kind=4),
                 bolt(x=float('nan')), bolt(vy=float('inf')), bolt(pitch=7.0), bolt(ttl=0.0), bolt(ttl=3.01), bolt(time=-1.0),
                 bolt(sector='a|b'), bolt(sector='a' * 97), bolt(lease='A' * 32), bolt(source_lease=B), bolt('forwarded', source_lease=None),
                 bolt(direction='relay'), bolt(yaw='0.5'), end(time=1e13), end(id=0), end(source_lease=B),
                 bolt(x=10**400), bolt(vy=-10**400), bolt(roll=10**400), bolt(ttl=10**400), bolt(epoch=10**400),
                 end(time=10**400), bolt(x=float('-inf'))]
        for message in cases:
            with self.subTest(message=message):
                with self.assertRaises(pp.ProtocolError):
                    pp.serialize(message)
        with self.assertRaises(pp.ProtocolError):
            pp.serialize('BOLT')

    def test_codec_is_used_only_by_the_relay_helper_and_the_guarded_adapter(self):
        ui = (ROOT / 'extension/x4_lan_lab/ui.xml').read_text()
        self.assertNotIn('projectile_protocol', ui)
        self.assertNotIn('projectile_game', ui)
        self.assertIn('not cfg.projectile_visuals', (ROOT/'extension/x4_lan_lab/ui/projectile_game.lua').read_text())
        for path in list((ROOT / 'x4lan').glob('*.py')) + list((ROOT / 'extension').rglob('*.lua')) + list((ROOT / 'extension').rglob('*.xml')):
            if path.name not in ('projectile_protocol.py', 'projectile_protocol.lua', 'projectile_relay.py', 'projectile_game.lua'):
                self.assertNotIn('projectile_protocol', path.read_text(errors='replace'), path)
                self.assertNotIn('PROJECTILE_PROTOCOL', path.read_text(errors='replace'), path)


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class LuaParityTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(LUA.read_text())
        P = 'X4LAN_PROJECTILE_PROTOCOL.'
        self.serialize = self.lua.eval('function(m) local a,b=' + P + 'serialize(m);return a,b end')
        self.parse = self.lua.eval('function(r,d) local a,b=' + P + 'parse(r,d);return a,b end')

    def table(self, message):
        d = dict(message.__dict__); d['type'] = 'BOLT' if isinstance(message, pp.Bolt) else 'BEND'
        return self.lua.table_from({k: v for k, v in d.items() if v is not None})

    def test_lua_serializes_the_same_canonical_text_and_parses_python_packets(self):
        for direction in pp.DIRECTIONS:
            for message in (bolt(direction), bolt(direction, kind=2, x=-12345678.901234567, y=0.1, z=1e-300, vx=-99999.999999999985,
                                                  yaw=2 * math.pi, pitch=-math.pi / 3, roll=-0.0, ttl=3.0, time=987654321.12345678, sector='x' * 96),
                            end(direction), end(direction, time=1e12)):
                with self.subTest(message=message):
                    raw = pp.serialize(message).decode()
                    out, why = self.serialize(self.table(message))
                    self.assertEqual((out, why), (raw, None))
                    parsed, why = self.parse(raw, direction)
                    self.assertIsNone(why)
                    self.assertEqual(self.serialize(parsed)[0], raw)
                    for name, value in message.__dict__.items():
                        if value is not None: self.assertEqual(parsed[name], value, name)
                    if direction == 'client': self.assertIsNone(parsed['source_lease'])

    def test_lua_refuses_every_packet_python_refuses(self):
        for direction, raw in bad_packets():
            with self.subTest(direction=direction, raw=raw[:90]):
                parsed, why = self.parse(raw, direction)
                self.assertIsNone(parsed)
                self.assertTrue(why)

    def test_lua_serialize_validates_fields(self):
        for change in ("m.entity=3", "m.epoch=0", "m.gen=2147483648", "m.id=1.5", "m.kind=4", "m.x=0/0", "m.vy=1/0",
                       "m.pitch=7", "m.ttl=0", "m.time=-1", "m.sector='a|b'", "m.lease=string.rep('A',32)",
                       "m.source_lease=string.rep('b',32)", "m.direction='relay'", "m.yaw='0.5'", "m.type='BOLX'"):
            with self.subTest(change=change):
                self.lua.globals().m = self.table(bolt())
                self.lua.execute(change)
                out, why = self.serialize(self.lua.globals().m)
                self.assertIsNone(out); self.assertTrue(why)


if __name__ == '__main__':
    unittest.main()
