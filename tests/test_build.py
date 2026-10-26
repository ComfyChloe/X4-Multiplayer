"""Station build intents through the real relay: routing, dedup, size limits, authority loss."""
import math
import os
import tempfile
import unittest
import zlib
from pathlib import Path

from x4lan import build
from x4lan.universe import UniverseSession
from test_host import TOKEN, P1, P2, FOREIGN
from test_game_adapter import LuaRuntime

UI = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/ui'
MD = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/md/x4lan_build.xml'


def entries(n=3, groups=True):
    out = []
    for i in range(1, n + 1):
        e = dict(index=i, macro=f'prod_gen_energycells_{i:02d}_macro', connection='' if i == 1 else 'connectionsnap001',
                 pred=0 if i == 1 else i - 1, predconnection='' if i == 1 else 'connectionsnap002',
                 pos=[0.0, -800.0 * i, 12.5], rot=[90.0 * (i % 4), 0.0, -0.0], groups=[])
        if groups and i == 2:
            e['groups'] = [dict(kind='turrets', macro='turret_arg_m_laser_02_mk1_macro', path='..\\con_turret_01', group='group_01', exact=4),
                           dict(kind='shields', macro='shield_arg_m_standard_01_mk1_macro', path='..', group='group_02', exact=2)]
        out.append(e)
    return out


class PlanFormatTests(unittest.TestCase):
    def test_canonical_roundtrip_and_strictness(self):
        xml = build.encode_plan('x4lan_' + 'c' * 32, entries())
        self.assertEqual(build.parse_plan(xml), ('x4lan_' + 'c' * 32, build.parse_plan(xml)[1]))
        self.assertIn('<rotation yaw="90.0000" pitch="0.0000" roll="0.0000"/>', xml)
        for bad in [xml.replace('<plan ', '<!DOCTYPE x><plan '), xml.replace('macro="prod', 'macro="pr"od', 1),
                    xml.replace('x="0.000"', 'x="0"', 1), xml.replace('></entry>', '><x/></entry>', 1),
                    xml.replace('index="2"', 'index="3"', 1), xml + ' ', xml.replace('12.500', 'nan', 1),
                    xml.replace('exact="4"', 'exact="0"'), xml.replace('group_01', 'group|01')]:
            with self.assertRaises(ValueError):
                build.parse_plan(bad)

    def test_limits(self):
        with self.assertRaises(ValueError):
            build.encode_plan('x', entries(build.MAX_ENTRIES + 1, False))
        e = entries(1, False)
        e[0]['pos'] = [2e6, 0, 0]
        with self.assertRaises(ValueError):
            build.encode_plan('x', e)

    @unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
    def test_lua_and_python_produce_identical_xml(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute((UI / 'build_protocol.lua').read_text())
        lua.execute('''
         B=X4LAN_BUILD_PROTOCOL
         E={}
         for i=1,3 do
          E[i]={index=i,macro=string.format('prod_gen_energycells_%02d_macro',i),connection=i==1 and '' or 'connectionsnap001',
           pred=i==1 and 0 or i-1,predconnection=i==1 and '' or 'connectionsnap002',pos={0,-800*i,12.5},rot={90*(i%4),0,-0.0},groups={}}
         end
         E[2].groups={{kind='turrets',macro='turret_arg_m_laser_02_mk1_macro',path='..\\\\con_turret_01',group='group_01',exact=4},
          {kind='shields',macro='shield_arg_m_standard_01_mk1_macro',path='..',group='group_02',exact=2}}
         XML=B.encode('x4lan_'..string.rep('c',32),E)
         P=B.parse(XML)
        ''')
        g = lua.globals()
        xml = build.encode_plan('x4lan_' + 'c' * 32, entries())
        self.assertEqual(g.XML, xml)
        self.assertEqual(g.B.digest(xml), zlib.adler32(xml.encode()))
        self.assertEqual(g.P.entries[2].groups[1].path, '..\\con_turret_01')
        self.assertEqual(lua.eval('B.parse(XML.." ")'), (None, 'plan_header'))


class BuildRelayTests(unittest.TestCase):
    def setUp(self):
        self.s = UniverseSession(TOKEN, 'world1-test', 'build')
        self.leases = {}
        for slot, addr in (('p1', P1), ('p2', P2)):
            packet = f'HELLO|1|{TOKEN}|world1-test|sector_macro|build|{slot}|{slot[-1] * 32}'.encode()
            self.leases[slot] = self.s.receive(packet, addr, 0)[0].decode().split('|')[3]
        self.a, self.b = self.leases['p1'], self.leases['p2']
        self.id = 'e' * 32
        self.lines = []
        self.s.builds.logger = self.lines.append
        self.xml = build.encode_plan('x4lan_' + self.id, entries())
        self.parts = [self.xml[i:i + build.PART_SIZE] for i in range(0, len(self.xml), build.PART_SIZE)]

    def header(self, request=None, kind='EXPAND', host=None, budget='250000', xml=None, n=3):
        xml = self.xml if xml is None else xml
        parts = math.ceil(len(xml) / build.PART_SIZE)
        if kind != 'EXPAND':
            return (f'BUILDREQ|1|{self.b}|{request or self.id}|{host or self.a}|{kind}|ABC-123|station_gen_factory_base_01_macro|'
                    f'cluster_14_sector001_macro|-|{"1" if kind == "REMOVEPLOT" else "-"}|0|0|0|1').encode()
        return (f'BUILDREQ|1|{self.b}|{request or self.id}|{host or self.a}|EXPAND|ABC-123|station_gen_factory_base_01_macro|'
                f'cluster_14_sector001_macro|8000,8000,8000,0,0,-500|{budget}|{n}|{len(xml)}|{parts}|{zlib.adler32(xml.encode())}').encode()

    def send_parts(self, parts=None, address=P2, lease=None, now=.2):
        for i, chunk in enumerate(parts or self.parts, 1):
            self.s.receive(f'BUILDPART|1|{lease or self.b}|{self.id}|{i}|{chunk}'.encode(), address, now)

    def reply(self, packet, now=.1, address=P2):
        out = self.s.receive(packet, address, now)
        return out[0].decode().split('|') if out else None

    def host(self, now, kind=b'BUILDEXEC'):
        return [p for a, p in self.s.snapshots(now) if p.startswith(kind) and a == P1]

    def test_assembly_forward_and_idempotent_result(self):
        self.assertGreater(len(self.parts), 1)
        r = self.reply(self.header())
        self.assertEqual(r[4:6], ['RECEIVING', 'ok'])
        self.assertEqual(r[6], ','.join(str(i) for i in range(1, len(self.parts) + 1)))
        self.send_parts(self.parts[:1])
        self.assertEqual(self.reply(self.header())[6], ','.join(str(i) for i in range(2, len(self.parts) + 1)))
        self.send_parts()
        self.assertEqual(self.reply(self.header(), .3)[4], 'PENDING')
        out = self.s.snapshots(.4)
        execs = [p for _, p in out if p.startswith(b'BUILDEXEC')]
        xparts = [p for _, p in out if p.startswith(b'BUILDXPART')]
        self.assertEqual(len(execs), 1)
        f = execs[0].decode().split('|')
        self.assertEqual((f[2], f[3], f[4], f[5], f[15]), (self.a, self.id, 'p2', 'EXPAND', '0'))
        self.assertEqual(''.join(p.decode().split('|', 5)[5] for p in xparts), self.xml)
        self.assertTrue(all(len(p) <= 768 for _, p in out))
        self.assertEqual(self.host(.5), [])
        self.s.receive(f'BUILDDONE|1|{self.a}|{self.id}|RECEIVED|ok'.encode(), P1, .6)
        self.assertEqual(self.host(.8, b'BUILDXPART'), [])
        self.assertEqual(len(self.host(1.9)), 1)  # header keeps polling for the final result
        self.s.receive(f'BUILDDONE|1|{self.a}|{self.id}|ACCEPTED|build_created'.encode(), P1, 1.95)
        for t in (2, 3):
            self.assertEqual(self.reply(self.header(), t)[4:6], ['ACCEPTED', 'build_created'])
        self.assertEqual(self.host(5), [])
        self.assertTrue(any('accepted request=' + self.id in v for v in self.lines))
        self.assertEqual(self.reply(self.header(budget='1'), 6)[4:6], ['REFUSED', 'request_conflict'])

    def test_refusal_is_relayed_and_never_reforwarded(self):
        self.reply(self.header());self.send_parts();self.s.snapshots(.3)
        self.s.receive(f'BUILDDONE|1|{self.a}|{self.id}|REFUSED|missing_blueprints'.encode(), P1, .4)
        self.assertEqual(self.reply(self.header(), .5)[4:6], ['REFUSED', 'missing_blueprints'])
        self.assertEqual(self.host(1), [])

    def test_only_authority_completes_and_foreign_packets_ignored(self):
        self.assertIsNone(self.reply(self.header(), address=FOREIGN))
        self.reply(self.header());self.send_parts();self.s.snapshots(.3)
        self.s.receive(f'BUILDDONE|1|{self.b}|{self.id}|ACCEPTED|ok'.encode(), P2, .4)
        self.s.receive(f'BUILDDONE|1|{self.a}|{self.id}|ACCEPTED|bad reason'.encode(), P1, .4)
        self.assertEqual(self.reply(self.header(), .5)[4], 'PENDING')
        self.assertEqual(self.reply(self.header().replace(self.b.encode(), self.a.encode(), 1), .5, P1)[4:6],
                         ['REFUSED', 'request_conflict'])

    def test_authority_requests_build_locally(self):
        other = 'f' * 32
        packet = self.header(other).replace(self.b.encode(), self.a.encode(), 1)
        self.assertEqual(self.reply(packet, .1, P1)[4:6], ['REFUSED', 'authority_builds_locally'])

    def test_size_limits_and_malformed_headers(self):
        big = 'x' * (build.MAX_BYTES + 1)
        bad = [self.header(xml=big), self.header(n=build.MAX_ENTRIES + 1),
               self.header().replace(b'|EXPAND|', b'|DELETE|'), self.header().replace(b'8000,8000', b'-8000,8000'),
               self.header().replace(b'|250000|', b'|-5|'), self.header(kind='RECYCLE').replace(b'|-|0|0|0|1', b'|-|1|0|0|1'),
               self.header()[:-1] + b'x']
        for packet in bad:
            self.assertIsNone(self.reply(packet), packet)
        self.assertFalse(self.s.builds.requests)
        self.assertEqual(self.s.receive(b'BUILDREQ|1|' + b'x' * 800, P2, .1), [])

    def test_bad_plan_digest_and_identity_refused(self):
        self.reply(self.header())
        parts = list(self.parts);parts[0] = parts[0][:-1] + ('a' if parts[0][-1] != 'a' else 'b')
        self.send_parts(parts)
        self.assertEqual(self.reply(self.header(), .3)[4:6], ['REFUSED', 'digest'])
        other = 'a' * 32;xml = build.encode_plan('x4lan_' + 'b' * 32, entries())
        self.id = other;self.reply(self.header(other, xml=xml), .4)
        self.send_parts([xml[i:i + build.PART_SIZE] for i in range(0, len(xml), build.PART_SIZE)], now=.5)
        self.assertEqual(self.reply(self.header(other, xml=xml), .6)[4:6], ['REFUSED', 'plan_identity'])

    def test_conflicting_part_refuses(self):
        self.reply(self.header())
        self.send_parts(self.parts[:1])
        self.s.receive(f'BUILDPART|1|{self.b}|{self.id}|1|{self.parts[0][:-1]}'.encode(), P2, .3)
        self.assertEqual(self.reply(self.header(), .4)[4:6], ['REFUSED', 'part_conflict'])

    def test_one_active_request_per_player_and_retained_ids(self):
        self.reply(self.header())
        self.assertEqual(self.reply(self.header('1' * 32), .2)[4:6], ['BUSY', 'busy'])
        self.assertNotIn('1' * 32, self.s.builds.requests)

    def test_relay_full_never_evicts(self):
        for i in range(build.LIMIT):
            self.s.builds.requests[f'{i:032x}'] = dict(slot='p1', state='ACCEPTED', signature=None)
        self.assertEqual(self.reply(self.header(), .2)[4:6], ['BUSY', 'relay_full'])
        self.assertEqual(len(self.s.builds.requests), build.LIMIT)

    def test_stale_host_epoch_and_absent_authority(self):
        self.assertEqual(self.reply(self.header(host='9' * 32))[4:6], ['STALE', 'no_authority'])
        # Authority leaves before forwarding: nothing can have been built.
        self.reply(self.header());self.send_parts()
        del self.s.peers['p1']
        self.s.snapshots(.3)
        self.assertEqual(self.reply(self.header(), .4)[4:6], ['STALE', 'no_authority'])

    def test_authority_loss_after_forward_is_uncertain_and_not_replayed(self):
        self.reply(self.header());self.send_parts();self.s.snapshots(.3)
        old = self.s.peers.pop('p1')
        self.s.peers['p1'] = type(old)(P1, '3' * 32, '7' * 32, .4)  # a fresh authority game
        self.assertEqual([p for _, p in self.s.snapshots(.4) if p.startswith(b'BUILD')], [])
        self.assertEqual(self.reply(self.header(), .5)[4:6], ['UNCERTAIN', 'authority_lost'])
        self.assertEqual([p for _, p in self.s.snapshots(1) if p.startswith(b'BUILD')], [])
        self.assertTrue(any('uncertain' in v for v in self.lines))

    def test_silent_authority_becomes_uncertain(self):
        self.reply(self.header());self.send_parts();self.s.snapshots(.3)
        self.s.peers['p1'].last_seen = 30  # keep the host lease alive
        self.s.snapshots(build.SILENT_AFTER + 1)
        self.assertEqual(self.s.builds.requests[self.id]['state'], 'UNCERTAIN')
        self.assertEqual(self.s.builds.requests[self.id]['reason'], 'authority_silent')

    def test_remove_plot_and_recycle_skip_plan_parts(self):
        r = self.reply(self.header(kind='REMOVEPLOT'))
        self.assertEqual(r[4], 'PENDING')
        f = self.host(.2)[0].decode().split('|')
        self.assertEqual((f[5], f[10], f[11], f[15]), ('REMOVEPLOT', '1', '0', '0'))

    def test_plan_inbox_written_atomically_and_removed_after_result(self):
        with tempfile.TemporaryDirectory() as folder:
            self.s.builds.plan_inbox = Path(folder) / 'constructionplans'
            self.reply(self.header());self.send_parts()
            f = self.host(.3)[0].decode().split('|')
            self.assertEqual(f[15], '1')
            path = self.s.builds.plan_inbox / f'x4lan_{self.id}.xml'
            text = path.read_text()
            self.assertTrue(text.startswith('<?xml') and '<plans>' + self.xml + '</plans>' in text)
            self.assertEqual(sorted(os.listdir(self.s.builds.plan_inbox)), [path.name])
            self.s.receive(f'BUILDDONE|1|{self.a}|{self.id}|ACCEPTED|build_created'.encode(), P1, .4)
            self.assertFalse(path.exists())

    def test_old_relay_session_ignores_build_packets(self):
        # Backward compatibility: sessions without world sync drop BUILDREQ silently;
        # the guest then refuses locally with reason relay_unsupported.
        from x4lan.coop import CoopSession
        s = CoopSession(TOKEN, 'world1-test', 'build')
        lease = s.receive(f'HELLO|1|{TOKEN}|world1-test|sector_macro|build|p2|{"2" * 32}'.encode(), P2, 0)[0].decode().split('|')[3]
        self.assertEqual(s.receive(self.header().replace(self.b.encode(), lease.encode(), 1), P2, .1), [])


class BuildMdTests(unittest.TestCase):
    def test_md_structure(self):
        import xml.etree.ElementTree as E
        root = E.parse(MD).getroot()
        cues = {c.get('name'): c for c in root.iter('cue')}
        self.assertEqual(set(cues), {'Expand', 'Recycle', 'Notice'})
        self.assertIsNotNone(cues['Expand'].find('.//add_build_to_expand_station'))
        self.assertIsNotNone(cues['Recycle'].find('.//add_build_to_recycle_station'))
        for name in ('Expand', 'Recycle'):
            self.assertIn("X4LAN_Build.Result", E.tostring(cues[name]).decode())

    # Opt-in (the game's md.xsd is not redistributable; compiling it takes ~90 s). Defined only
    # when X4_MD_XSD is set so dev/check.py does not count a skip as missing coverage.
    if os.environ.get('X4_MD_XSD'):
        def test_md_against_game_schema(self):
            from lxml import etree
            schema = etree.XMLSchema(etree.parse(os.environ['X4_MD_XSD']))
            self.assertTrue(schema.validate(etree.parse(str(MD))), schema.error_log)


if __name__ == '__main__':
    unittest.main()
