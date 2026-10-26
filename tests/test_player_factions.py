"""Player factions: factions.xml pool diff, MD schema, Lua core (LuaJIT) and relay routing.

Synthetic peers only: proves mapping, protocol and authority rules, not native X4 behaviour.
Schema checks use the read-only x4-re extraction when present (X4_RE or ../x4-re).
"""
import os
import re
import unittest
import xml.etree.ElementTree as E
from pathlib import Path

from x4lan.host import Session
from x4lan.players import PlayerRoster

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None
try:
    from lxml import etree
except ImportError:
    etree = None

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / 'extension/x4_lan_lab'
FACTIONS = EXT / 'libraries/factions.xml'
MD = EXT / 'md/x4lan_player_factions.xml'
RE = Path(os.environ.get('X4_RE', ROOT.parent / 'x4-re'))
LIB = RE / 'gamescripts/resolved/base/libraries'
VANILLA = RE / 'work/pfaction/lib'
TOKEN = 'a' * 32
P1, P2 = ('192.0.2.1', 1000), ('192.0.2.2', 2000)


class FactionPoolDiff(unittest.TestCase):
    def setUp(self):
        self.tree = E.parse(FACTIONS)
        self.pool = self.tree.getroot().findall("add[@sel='/factions']/faction")

    def test_generator_matches_committed_file(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('gen', ROOT / 'dev/build_player_factions.py')
        gen = importlib.util.module_from_spec(spec); spec.loader.exec_module(gen)
        self.assertEqual(FACTIONS.read_text(encoding='utf-8'), gen.build())

    def test_pool_ids_unique_inactive_unknown_without_economy(self):
        ids = [f.get('id') for f in self.pool]
        self.assertEqual(ids, [f'x4lan_player{i}' for i in range(1, 17)])
        self.assertEqual(len(set(ids)), 16)
        colours = set()
        for f in self.pool:
            tags = f.get('tags', '').split()
            self.assertNotIn('economic', tags)
            self.assertEqual(sorted(tags), ['claimspace', 'police'])  # as faction "player"
            self.assertEqual(f.get('policefaction'), f.get('id'))
            self.assertEqual((f.get('active'), f.get('known')), ('0', '0'))
            self.assertEqual(f.get('behaviourset'), 'default')
            self.assertFalse(re.search(r'\{\d+,\s*\d+\}', f.get('name')))
            c = f.find('color')
            colours.add((c.get('r'), c.get('g'), c.get('b')))
            self.assertIsNone(c.get('ref'))
            rel = {r.get('faction'): float(r.get('relation')) for r in f.find('relations')}
            self.assertEqual(rel.pop('player'), .75)
            self.assertEqual(set(rel), set(ids) - {f.get('id')})  # allied to every other human only
            self.assertTrue(all(v == .75 for v in rel.values()))
        self.assertEqual(len(colours), 16)
        player = self.tree.getroot().find("add[@sel=\"/factions/faction[@id='player']/relations\"]")
        self.assertEqual([r.get('faction') for r in player], ids)

    def test_no_faction_logic_or_economy_references(self):
        for path in (EXT / 'md').glob('*.xml'):
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('FactionLogic', text if path != MD else '')
        self.assertNotIn('economic', MD.read_text(encoding='utf-8'))

    @unittest.skipUnless(etree and (VANILLA / '08__libraries_factions.xml').is_file() and (LIB / 'factions.xsd').is_file(),
                         'x4-re vanilla factions extraction not available')
    def test_diff_applies_to_vanilla_and_validates(self):
        base = etree.parse(str(VANILLA / '08__libraries_factions.xml'))
        vanilla_ids = {f.get('id') for f in base.getroot().iter('faction')}
        for add in etree.parse(str(FACTIONS)).getroot():
            self.assertEqual(add.tag, 'add')
            targets = base.xpath(add.get('sel'))
            self.assertEqual(len(targets), 1, add.get('sel'))
            for child in add:
                targets[0].append(child)
        ids = [f.get('id') for f in base.getroot().findall('faction')]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(vanilla_ids & {f'x4lan_player{i}' for i in range(1, 17)})
        schema = etree.XMLSchema(etree.parse(str(LIB / 'factions.xsd')))
        self.assertTrue(schema.validate(base), schema.error_log)
        if (LIB / 'diff.xsd').is_file():
            diff = etree.XMLSchema(etree.parse(str(LIB / 'diff.xsd')))
            self.assertTrue(diff.validate(etree.parse(str(FACTIONS))), diff.error_log)


@unittest.skipUnless(etree and (LIB / 'md.xsd').is_file(), 'x4-re md.xsd not available')
class PlayerFactionsMdSchema(unittest.TestCase):
    def test_md_validates_against_game_schema(self):
        schema = etree.XMLSchema(etree.parse(str(LIB / 'md.xsd')))
        self.assertTrue(schema.validate(etree.parse(str(MD))), schema.error_log)


def joined():
    session = Session(TOKEN, 'build', 'bubble')
    lease1 = session.receive(f'HELLO|1|{TOKEN}|build|s_macro|bubble|p1|{"1"*32}'.encode(), P1, 0)[0].decode().split('|')[3]
    lease2 = session.receive(f'HELLO|1|{TOKEN}|build|s_macro|bubble|p2|{"2"*32}'.encode(), P2, 0)[0].decode().split('|')[3]
    return session, lease1, lease2


class RelayRoster(unittest.TestCase):
    def test_roster_relations_requests_and_sector_routing(self):
        session, l1, l2 = joined()
        r = PlayerRoster()
        name = 'Alice'.encode().hex()
        self.assertEqual(r.handle(f'PFID|1|{l1}|1|{name}|faction_player'.encode(), P1, session.peers, 0), [])
        r.handle(f'PFID|1|{l2}|1|{"426f62"}|faction_player'.encode(), P2, session.peers, 0)
        # Wrong address / lease / malformed: ignored.
        self.assertEqual(r.handle(f'PFID|1|{l1}|2|{name}|x'.encode(), P2, session.peers, 0), [])
        self.assertEqual(r.handle(f'PFID|1|{l1}|3|zz|x'.encode(), P1, session.peers, 0), [])
        rows = [p.decode() for _, p in r.snapshots(session.peers, 1)]
        self.assertIn(f'PFROSTER|1|{l2}|1|{name}|faction_player|1', rows)
        self.assertIn(f'PFROSTER|1|{l1}|2|426f62|faction_player|1', rows)
        # Only the host may author relations; forwarded to the guest only.
        self.assertEqual(r.handle(f'PFREL|1|{l2}|1|1|2|-0.5'.encode(), P2, session.peers, 1), [])
        out = r.handle(f'PFREL|1|{l1}|1|1|2|0.75'.encode(), P1, session.peers, 1)
        self.assertEqual(out, [(P2, f'PFREL|1|{l2}|1|1|2|0.7500'.encode())])
        self.assertEqual(r.handle(f'PFREL|1|{l1}|1|1|2|-1'.encode(), P1, session.peers, 1), [])  # stale rev
        self.assertEqual(r.handle(f'PFREL|1|{l1}|2|1|2|2'.encode(), P1, session.peers, 1), [])   # out of range
        # Guest relation intent goes to the host, tagged with the sender's own index.
        out = r.handle(f'PFREQ|1|{l2}|5|1|-0.5'.encode(), P2, session.peers, 1)
        self.assertEqual(out, [(P1, f'PFREQ|1|{l1}|2|5|1|-0.5000'.encode())])
        self.assertEqual(r.handle(f'PFREQ|1|{l2}|6|2|-0.5'.encode(), P2, session.peers, 1), [])  # self
        # Sectors: host rows cached and forwarded; a guest may only claim for itself.
        out = r.handle(f'PFSEC|1|{l1}|1|cluster_14_sector001_macro|S1'.encode(), P1, session.peers, 1)
        self.assertEqual(out, [(P2, f'PFSEC|1|{l2}|1|cluster_14_sector001_macro|S1'.encode())])
        self.assertEqual(r.handle(f'PFSEC|1|{l2}|1|cluster_01_sector001_macro|S1'.encode(), P2, session.peers, 1), [])
        out = r.handle(f'PFSEC|1|{l2}|2|cluster_01_sector001_macro|S2'.encode(), P2, session.peers, 1)
        self.assertEqual(out, [(P1, f'PFSEC|1|{l1}|2|cluster_01_sector001_macro|S2'.encode())])
        rows = [p.decode() for _, p in r.snapshots(session.peers, 10)]
        self.assertIn(f'PFSEC|1|{l2}|1|cluster_14_sector001_macro|S1', rows)
        self.assertIn(f'PFREL|1|{l2}|1|1|2|0.7500', rows)

    def test_new_host_lease_forgets_authority_state(self):
        session, l1, _ = joined()
        r = PlayerRoster()
        r.handle(f'PFREL|1|{l1}|1|1|2|0.75'.encode(), P1, session.peers, 0)
        session.peers['p1'].lease = 'f' * 32
        r.snapshots(session.peers, 1)
        self.assertEqual(r.relations, {})


@unittest.skipUnless(LuaRuntime, 'lupa.luajit21 not installed')
class LuaCore(unittest.TestCase):
    def runtime(self):
        import ctypes, lupa.luajit21
        ctypes.CDLL(lupa.luajit21.__file__, mode=ctypes.RTLD_GLOBAL)
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute((EXT / 'ui/player_factions_core.lua').read_text(encoding='utf-8'))
        lua.execute('''
            function make(slot)
              local io={sent={},calls={},logs={}}
              io.send=function(p) io.sent[#io.sent+1]=p end
              io.md=function(a,t) io.calls[#io.calls+1]={action=a,args=t} end
              io.log=function(l) io.logs[#io.logs+1]=l end
              return X4LAN_PLAYERS_CORE.new({slot=slot},io),io
            end
            function split(p) local f={};for v in (p..'|'):gmatch('(.-)|') do f[#f+1]=v end;return f end
        ''')
        return lua

    def test_mapping_tokens_names_and_relations(self):
        lua = self.runtime()
        lua.execute('''
            local K=X4LAN_PLAYERS_CORE
            assert(K.index('p1')==1 and K.index('p16')==16 and not K.index('p17') and not K.index('x'))
            assert(K.factionFor(1,1)=='player' and K.factionFor(2,1)=='x4lan_player2' and K.factionFor(1,2)=='x4lan_player1')
            assert(K.tokenFor('player',2)=='S2' and K.tokenFor('x4lan_player1',2)=='S1' and K.tokenFor('argon',2)=='argon')
            assert(K.factionForToken('S2',2)=='player' and K.factionForToken('S1',2)=='x4lan_player1')
            assert(K.factionForToken('S17',1)==nil and K.factionForToken('bad id',1)==nil)
            assert(K.poolIndex('x4lan_player07')==nil and K.poolIndex('x4lan_player7')==7)
            assert(K.relation('allied')==0.75 and K.relation('hostile')==-0.5 and K.relation(2)==nil and K.relation(0/0)==nil)
            assert(K.cleanName('  Bob\\n{1001,2} ')=='Bob1001,2' and K.cleanName('\\1')==nil)
            local long=string.rep('\\195\\164',40)
            local c=K.cleanName(long);assert(#c<=48 and K.decode(K.encode(c))==c)
            assert(K.decode('7b7d')==nil and K.decode('41')=='A' and K.decode('4')==nil)
        ''')

    def test_host_assigns_guest_identity_and_publishes_default_alliance(self):
        lua = self.runtime()
        lua.execute('''
            local p,io=make('p1')
            assert(p.receive('L1',split('PFROSTER|1|L1|1|416c696365|faction_player|1'),1))
            assert(#io.calls==0)  -- own human stays `player`
            p.receive('L1',split('PFROSTER|1|L1|2|426f62|faction_argon|3'),1)
            assert(io.calls[1].action=='Assign' and io.calls[1].args.faction=='x4lan_player2' and io.calls[1].args.name=='Bob')
            assert(io.calls[1].args.icon=='faction_argon' and io.calls[1].args.index==2)
            assert(io.calls[2].action=='Relation' and io.calls[2].args.fa=='player' and io.calls[2].args.fb=='x4lan_player2')
            assert(io.calls[2].args.value==0.75)
            assert(io.sent[1]=='PFREL|1|L1|1|1|2|0.7500')
            p.receive('L1',split('PFROSTER|1|L1|2|426f62|faction_argon|4'),2)
            assert(#io.calls==2)  -- unchanged identity is not reapplied
            -- Guest intent through the relay; then a local host intent.
            p.receive('L1',split('PFREQ|1|L1|2|5|1|-0.5000'),3)
            assert(io.calls[3].action=='Relation' and io.calls[3].args.value==-0.5 and io.sent[2]=='PFREL|1|L1|2|1|2|-0.5000')
            local ok,how=p.setRelation('L1','p2','neutral')
            assert(ok and how=='applied' and io.calls[4].args.value==0 and io.sent[3]=='PFREL|1|L1|3|1|2|0.0000')
            assert(not p.setRelation('L1','p1','neutral'))
            p.receive('L1',split('PFREQ|1|L1|3|5|1|-0.5000'),3)
            assert(#io.calls==4)  -- a human not in the roster cannot change relations
        ''')

    def test_guest_mirrors_relations_in_order_and_requests_changes(self):
        lua = self.runtime()
        lua.execute('''
            local p,io=make('p2')
            p.receive('L2',split('PFROSTER|1|L2|1|416c696365|faction_player|1'),1)
            assert(io.calls[1].action=='Assign' and io.calls[1].args.faction=='x4lan_player1' and io.calls[1].args.short=='Alice')
            p.receive('L2',split('PFREL|1|L2|2|1|2|-0.5000'),1)
            assert(io.calls[2].action=='Relation' and io.calls[2].args.fa=='x4lan_player1' and io.calls[2].args.fb=='player')
            p.receive('L2',split('PFREL|1|L2|1|1|2|0.7500'),1)   -- older revision
            p.receive('L2',split('PFREL|1|L2|2|1|2|-0.5000'),1)  -- duplicate
            assert(#io.calls==2)
            local ok,how=p.setRelation('L2','p1','allied')
            assert(ok and how=='requested' and io.sent[#io.sent]:match('^PFREQ|1|L2|%d+|1|0.7500$'))
            assert(#io.calls==2)  -- a guest never applies its own intent
            p.receive('L2',split('PFREQ|1|L2|1|5|2|0.5'),1)
            assert(#io.calls==2)
        ''')

    def test_sector_ownership_mapping_both_directions(self):
        lua = self.runtime()
        lua.execute('''
            local h,hio=make('p1')
            local g,gio=make('p2')
            for _,c in ipairs({h,g}) do
              c.receive('L',split('PFROSTER|1|L|1|41|faction_player|1'),0)
              c.receive('L',split('PFROSTER|1|L|2|42|faction_player|1'),0)
            end
            hio.calls,gio.calls,hio.sent,gio.sent={},{},{},{}
            -- Host claims a sector: authority row with the host's token.
            h.localSector('L','cluster_14_sector001_macro','player')
            assert(hio.sent[1]=='PFSEC|1|L|1|cluster_14_sector001_macro|S1')
            g.receive('L',split(hio.sent[1]),1)
            assert(gio.calls[1].action=='Sector' and gio.calls[1].args.faction=='x4lan_player1')
            -- MD then reports the applied owner; a guest never re-sends authority rows.
            g.localSector('L','cluster_14_sector001_macro','x4lan_player1')
            assert(#gio.sent==0)
            -- Guest's own claim becomes an intent; the host applies it to the guest's pool faction.
            g.localSector('L','cluster_01_sector001_macro','player')
            assert(gio.sent[1]=='PFSEC|1|L|1|cluster_01_sector001_macro|S2')
            h.receive('L',split(gio.sent[1]),2)
            assert(hio.calls[1].action=='Sector' and hio.calls[1].args.faction=='x4lan_player2')
            h.localSector('L','cluster_01_sector001_macro','x4lan_player2')
            assert(hio.sent[2]=='PFSEC|1|L|2|cluster_01_sector001_macro|S2')
            g.receive('L',split(hio.sent[2]),3)
            assert(#gio.calls==1)  -- already player-owned locally: nothing to apply
            g.localSector('L','cluster_01_sector001_macro','player')
            assert(#gio.sent==1)  -- confirmed by the authority, no second claim
            -- A guest cannot claim for another human; a release reaches the guest as the NPC owner.
            h.receive('L',split('PFSEC|1|L|9|cluster_02_sector001_macro|S1'),4)
            assert(#hio.calls==1)
            h.localSector('L','cluster_14_sector001_macro','argon')
            g.receive('L',split(hio.sent[3]),5)
            assert(gio.calls[2].args.faction=='argon')
        ''')

    def test_host_ship_ownership_only_after_baseline(self):
        lua = self.runtime()
        lua.execute('''
            local h=make('p1');local g=make('p2')
            assert(h.personalOwner('5')==nil)
            h.baseline={['5']=true}
            assert(h.personalOwner('5')==nil and h.personalOwner('6')=='x4lan_player1')
            g.baseline={}
            assert(g.personalOwner('6')==nil)
        ''')

    def test_lua_cores_through_relay(self):
        """Two Lua cores exchange identities and a relation change through PlayerRoster."""
        lua = self.runtime()
        session, l1, l2 = joined()
        relay = PlayerRoster()
        lua.execute('h,hio=make("p1");g,gio=make("p2")')
        g = lua.globals()
        addr = {'p1': P1, 'p2': P2}
        leases = {'p1': l1, 'p2': l2}
        cores = {'p1': (g.h, g.hio), 'p2': (g.g, g.gio)}

        def pump(now):
            for slot, (core, io) in cores.items():
                sent = list(io.sent.values()); io.sent = lua.table()
                for packet in sent:
                    for target, out in relay.handle(packet.encode(), addr[slot], session.peers, now):
                        deliver(target, out, now)
            for target, out in relay.snapshots(session.peers, now):
                deliver(target, out, now)

        def deliver(target, packet, now):
            slot = 'p1' if target == P1 else 'p2'
            core = cores[slot][0]
            core.receive(leases[slot], g.split(packet.decode()), now)

        g.h.identity(l1, 'Alice', 'faction_player')
        g.g.identity(l2, 'Bob', None)
        for t in range(4):
            pump(t * 1.0)
        host_md = [(m.action, dict(m.args)) for m in g.hio.calls.values()]
        guest_md = [(m.action, dict(m.args)) for m in g.gio.calls.values()]
        self.assertIn(('Assign', {'index': 2, 'faction': 'x4lan_player2', 'name': 'Bob', 'short': 'Bob', 'icon': 'faction_player'}), host_md)
        self.assertIn(('Assign', {'index': 1, 'faction': 'x4lan_player1', 'name': 'Alice', 'short': 'Alice', 'icon': 'faction_player'}), guest_md)
        rel = [a for k, a in guest_md if k == 'Relation']
        self.assertEqual((rel[-1]['fa'], rel[-1]['fb'], rel[-1]['value']), ('x4lan_player1', 'player', .75))
        ok, how = g.g.setRelation(l2, 'p1', 'hostile')
        self.assertEqual((ok, how), (True, 'requested'))
        for t in range(4, 7):
            pump(t * 1.0)
        rel = [dict(m.args) for m in g.gio.calls.values() if m.action == 'Relation']
        self.assertEqual(rel[-1]['value'], -.5)
        rel = [dict(m.args) for m in g.hio.calls.values() if m.action == 'Relation']
        self.assertEqual((rel[-1]['fa'], rel[-1]['fb'], rel[-1]['value']), ('player', 'x4lan_player2', -.5))


@unittest.skipUnless(LuaRuntime, 'lupa.luajit21 not installed')
class LuaGlue(unittest.TestCase):
    """player_factions.lua against stubbed engine events (no X4)."""
    def load(self, cfg, bubble=True):
        import ctypes, lupa.luajit21
        ctypes.CDLL(lupa.luajit21.__file__, mode=ctypes.RTLD_GLOBAL)
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''
            logs,events,ui,sent={},{},{},{}
            function DebugError(t) logs[#logs+1]=t end
            function RegisterEvent(n,f) events[n]=f end
            function AddUITriggeredEvent(screen,action,args) ui[#ui+1]={screen=screen,action=action,args=args} end
            pcall(require('ffi').cdef,'typedef uint64_t UniverseID;')
        ''')
        lua.execute((EXT / 'ui/player_factions_core.lua').read_text(encoding='utf-8'))
        g = lua.globals()
        g.X4LAN_CONFIG = lua.table_from(cfg)
        if bubble:
            lua.execute('''X4LAN_BUBBLE_API={paused=function() return false end,
              state={active=true,ready=true,client={lease='L2'},transport={send=function(_,p) sent[#sent+1]=p end}}}''')
        lua.execute((EXT / 'ui/player_factions.lua').read_text(encoding='utf-8'))
        return lua, g

    def logs(self, g):
        return list(g.logs.values())

    def test_flag_off_is_inert(self):
        _, g = self.load(dict(enabled=True, coop=True, slot='p2', player_factions=False))
        self.assertEqual(self.logs(g), [])
        self.assertIsNone(g.X4LAN_PLAYERS)

    def test_missing_prerequisite_logs_feature_disabled(self):
        _, g = self.load(dict(enabled=True, coop=True, slot='p2', player_factions=True), bubble=False)
        self.assertIn('[X4LAN] FEATURE player_factions DISABLED reason=bubble_adapter_inactive', self.logs(g))

    def test_guest_setup_identity_and_relay_timeout(self):
        lua, g = self.load(dict(enabled=True, coop=True, slot='p2', player_factions=True))
        pf = g.X4LAN_BUBBLE_API.players
        pf.frame(0)
        self.assertEqual((g.ui[1].screen, g.ui[1].action), ('X4LAN_PlayerFactions', 'Setup'))
        g.events['X4LAN_PlayerFactions.Ready'](None, '16')
        pf.frame(1)
        pf.frame(2)
        self.assertTrue(any('PLAYER_FACTIONS READY slot=p2' in l for l in self.logs(g)))
        self.assertRegex(g.sent[1], r'^PFID\|1\|L2\|1\|' + 'LAN Player 2'.encode().hex() + r'\|faction_player$')
        ok, how = g.X4LAN_PLAYERS.setRelation('p1', 'hostile')
        self.assertEqual((ok, how), (True, 'requested'))
        pf.frame(40)
        self.assertIn('[X4LAN] FEATURE player_factions DISABLED reason=relay_unsupported', self.logs(g))

    def test_small_pool_disables(self):
        _, g = self.load(dict(enabled=True, coop=True, slot='p2', player_factions=True))
        pf = g.X4LAN_BUBBLE_API.players
        pf.frame(0); g.events['X4LAN_PlayerFactions.Ready'](None, '3'); pf.frame(1)
        self.assertIn('[X4LAN] FEATURE player_factions DISABLED reason=pool_missing count=3', self.logs(g))


class FlagDefaults(unittest.TestCase):
    def test_inert_source_and_internal_packages(self):
        cfg = (EXT / 'ui/config.lua').read_text(encoding='utf-8')
        self.assertRegex(cfg, r'\n\s*player_factions = false,')
        launcher = (ROOT / 'distribution/session_launcher.py').read_text(encoding='utf-8')
        self.assertIn('player_factions=True', launcher)
        glue = (EXT / 'ui/player_factions.lua').read_text(encoding='utf-8')
        self.assertIn("cfg.player_factions~=true", glue)
        self.assertIn('FEATURE player_factions DISABLED reason=', glue)
        ui = (EXT / 'ui.xml').read_text(encoding='utf-8')
        self.assertLess(ui.index('ui/bubble_game.lua'), ui.index('ui/player_factions_core.lua'))
        self.assertLess(ui.index('ui/player_factions_core.lua'), ui.index('ui/player_factions.lua'))


if __name__ == '__main__':
    unittest.main()
