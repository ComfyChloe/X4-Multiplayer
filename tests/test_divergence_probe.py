"""Divergence probe: recorder under engine doubles, DIV1 parser and offline comparator.

Mocks and synthetic logs only. Nothing here shows X4 actually emits these events or
how fast two native games diverge; that needs the native run in DIVERGENCE-PROBE.txt.
"""
import contextlib
import csv
import io
import math
import tempfile
import unittest
from pathlib import Path

from dev import compare_divergence as cd

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'extension/x4_lan_lab/ui'
MD = ROOT / 'extension/x4_lan_lab/md/x4lan_divergence.xml'

MOCK = '''
logs, events, triggers = {}, {}, {}
savePath = "C:/Users/x/Documents/Egosoft/X4/45841/save"
ships = {}
sectors = {[22]='cluster_14_sector001_macro'}
local function copy(t) local r={}; for k,v in pairs(t) do r[k]=v end; return r end
api = {
  GetSaveFolderPath=function() return savePath end,
  IsComponentOperational=function(id) return ships[id] ~= nil and not ships[id].dead end,
  GetContextByClass=function(id, class) local s=ships[id]
    if class=='sector' then return s and s.sector or 0 end
    if class=='container' then return s and s.container or 0 end
    return 0 end,
  GetObjectPositionInSector=function(id) return copy(ships[id].pos) end,
  IsComponentClass=function(id, class) return ships[id] and ('ship_'..ships[id].cls)==class end,
  GetNumOrders=function(id) return ships[id].order and 1 or 0 end,
  GetOrders=function(buf, n, id) buf[0]={orderdef=ships[id].order, state='critical'}; return 1 end,
  GetDefaultOrder=function(d, id) if ships[id].dorder then d.orderdef=ships[id].dorder; return true end; return false end,
  GetCurrentGameTime=function() return gameTime or 100 end,
}
local realRequire = require
require = function(name)
  if name=="ffi" then return {os="Windows", C=api, typeof=function() error('unknown') end,
    cdef=function() end, string=function(s) return s end,
    new=function(name) return {} end} end
  return realRequire(name)
end
DebugError=function(msg) logs[#logs+1]=msg end
GetCurRealTime=function() return 12.5 end
ConvertIDTo64Bit=function(v) return tonumber(v) end
ConvertStringToLuaID=function(s) return tonumber(s) end
GetComponentData=function(id, ...)
  local out={}
  local s=ships[id]
  for i,field in ipairs({...}) do
    if sectors[id] then out[i]=(field=='macro') and sectors[id] or nil
    elseif s then out[i]=s[field] end
  end
  return unpack(out, 1, select('#', ...))
end
RegisterEvent=function(name, fn) events[name]=fn end
AddUITriggeredEvent=function(screen, control, value) triggers[#triggers+1]={screen=screen, control=control, value=value} end
function ship(id, t) t.sector=t.sector or 22; t.pos=t.pos or {x=1,y=2,z=3,yaw=0,pitch=0,roll=0}; t.cls=t.cls or 's'; ships[id]=t end
X4LAN_CONFIG={enabled=false, test_profile="45841", slot="p1", divergence_probe=true}
'''


def runtime(setup=''):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    lua.execute(setup)
    lua.execute((UI / 'divergence_probe.lua').read_text())
    return lua


def logs(lua):
    return [lua.eval('logs')[i] for i in range(1, len(lua.eval('logs')) + 1)]


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class RecorderTests(unittest.TestCase):
    def test_inert_without_flag_and_flag_is_off_in_source_config(self):
        lua = runtime('X4LAN_CONFIG.divergence_probe=false')
        self.assertEqual(len(lua.eval('logs')), 0)
        self.assertIsNone(lua.eval('next(events)'))
        self.assertIsNone(lua.eval('X4LAN_DIVERGENCE'))
        self.assertIn('divergence_probe = false', (UI / 'config.lua').read_text())
        ui = (ROOT / 'extension/x4_lan_lab/ui.xml').read_text()
        self.assertEqual(ui.count('name="ui/divergence_probe.lua"'), 1)

    def test_refuses_outside_isolated_profile_and_invalid_settings(self):
        for setup in ('savePath="C:/Users/x/Documents/Egosoft/X4/70000001/save"',
                      'X4LAN_CONFIG.divergence_interval=1', "X4LAN_CONFIG.divergence_sectors={'bad macro'}"):
            lua = runtime(setup)
            self.assertIsNone(lua.eval('next(events)'), setup)
            self.assertIn('DIV1 STOPPED', logs(lua)[-1])

    def test_source_is_log_only(self):
        text = (UI / 'divergence_probe.lua').read_text()
        for forbidden in ('SetScript', 'RemoveComponent', 'SpawnObject', 'SetObjectSectorPos', 'transport', 'send(',
                          'ActivateObject', 'SetOrder', 'Pause('):
            self.assertNotIn(forbidden, text)
        md = MD.read_text()
        for forbidden in ('destroy_object', 'create_ship', 'set_object', 'warp', 'create_order', 'cancel_all_orders',
                          'set_value name="player', 'reward_player', 'add_cargo', 'remove_cargo'):
            self.assertNotIn(forbidden, md)

    def test_md_scheduler_is_well_formed_and_bounded(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(MD).getroot()
        cues = {c.get('name'): c for c in root.iter('cue')}
        self.assertEqual(set(cues), {'Boot', 'Hello', 'Sector', 'Start', 'Sample'})
        self.assertIn('Boot.$hellos lt 120', cues['Hello'].find('conditions/check_value').get('value'))
        start = cues['Start'].find('actions/do_if').get('value')
        for bound in ('$interval ge 5', '$interval le 3600', '$maxships le 2000'):
            self.assertIn(bound, start)
        events = [e.get('name') for e in root.iter('raise_lua_event')]
        self.assertEqual(sorted(set(events)), sorted("'X4LAN_Divergence.%s'" % n for n in ('Hello', 'Missing', 'Tick', 'Sector', 'Ship', 'End')))

    def test_handshake_aligns_first_sample_to_interval_grid(self):
        lua = runtime("X4LAN_CONFIG.divergence_interval=30;X4LAN_CONFIG.divergence_sectors={'a_macro','b_macro'}")
        self.assertIn('DIV1 CFG label=p1', logs(lua)[0])
        lua.execute("events['X4LAN_Divergence.Hello'](nil,'95.5')")
        t = lua.eval('triggers')
        self.assertEqual([t[i].control for i in range(1, 4)], ['Sector', 'Sector', 'Start'])
        self.assertEqual(t[1].value, 'a_macro')
        self.assertEqual((t[3].value.interval, t[3].value.first, t[3].value.maxships), (30, 120, 400))

    def test_sample_lines_round_trip_through_the_parser(self):
        lua = runtime()
        lua.execute('''
          ship(1001,{macro='ship_arg_m_trans_01_a_macro',owner='argon',name='Argon Trader | "Mk1"',idcode='ABC-123',
            hullpercent=87.5,shieldpercent=100,isdocked=true,isinternallystored=false,container=2001,order='TradeRoutine',dorder='Patrol',cls='m'})
          ship(2001,{macro='station_macro',owner='argon',idcode='STA-001'})
          ship(1002,{macro='ship_arg_s_fighter_01_a_macro',owner='argon',hullpercent=10,shieldpercent=0})
          ship(1003,{macro='ship_x',owner='argon',dead=true})
          local E=events
          E['X4LAN_Divergence.Tick'](nil,'7|180|180.05|2026-10-25T10:00:00|cluster_14_sector001_macro')
          E['X4LAN_Divergence.Sector'](nil,'7|cluster_14_sector001_macro|3|3|1')
          E['X4LAN_Divergence.Ship'](nil,1001);E['X4LAN_Divergence.Ship'](nil,1002);E['X4LAN_Divergence.Ship'](nil,1003)
          E['X4LAN_Divergence.End'](nil,'7|1')
        ''')
        log = cd.parse_log(logs(lua))
        self.assertEqual((log.malformed, log.orphans), (0, 0))
        sample = log.runs[0][0]
        self.assertEqual((sample.n, sample.gt, sample.age), (7, 180.0, 180.05))
        self.assertTrue(sample.complete('cluster_14_sector001_macro'))
        s = sample.sectors['cluster_14_sector001_macro']
        self.assertEqual((s['written'], s['gone'], s['player']), (2, 1, '1'))
        a = sample.ships['cluster_14_sector001_macro']['1001']
        self.assertEqual(a['name'], 'Argon Trader | "Mk1"')
        self.assertEqual((a['code'], a['hull'], a['order'], a['ostate'], a['dorder'], a['docked'], a['dock'], a['cls'], a['first']),
                         ('ABC-123', 87.5, 'TradeRoutine', 'critical', 'Patrol', '1', 'STA-001/station_macro', 'm', '7'))
        b = sample.ships['cluster_14_sector001_macro']['1002']
        self.assertEqual((b['order'], b['dorder'], b['dock'], b['code']), ('', '', '', ''))

    def test_line_budget_drops_and_marks_sector_incomplete(self):
        lua = runtime('X4LAN_CONFIG.divergence_max_lines=1')
        lua.execute('''
          ship(1,{macro='m',owner='argon'});ship(2,{macro='m',owner='argon'})
          local E=events
          E['X4LAN_Divergence.Tick'](nil,'1|30|30|w|s')
          E['X4LAN_Divergence.Sector'](nil,'1|cluster_14_sector001_macro|2|2|0')
          E['X4LAN_Divergence.Ship'](nil,1);E['X4LAN_Divergence.Ship'](nil,2)
          E['X4LAN_Divergence.End'](nil,'1|1')
        ''')
        text = logs(lua)
        self.assertEqual(sum('DIV1 SHIP' in t for t in text), 1)
        self.assertIn('dropped=1', [t for t in text if 'DIV1 END' in t][0])
        self.assertFalse(cd.parse_log(text).runs[0][0].complete('cluster_14_sector001_macro'))

    def test_errors_are_bounded_and_stop_the_probe(self):
        lua = runtime()
        lua.execute("for i=1,30 do events['X4LAN_Divergence.Ship'](nil,1) end")
        text = logs(lua)
        self.assertEqual(sum('DIV1 ERROR' in t for t in text), 20)
        self.assertIn('DIV1 STOPPED', text[-1])


def ship_line(n, sector, id, macro='ship_m', owner='argon', code='', x=0.0, hull=100.0, order='TradeRoutine', **extra):
    fields = dict(n=n, s=sector, id=id, macro=macro, owner=owner, code=code, name='', cls='m', sector=sector,
                  x='%.1f' % x, y='0.0', z='0.0', yaw='0', pitch='0', roll='0', hull='%.2f' % hull, shield='100.00',
                  order=order, ostate='', dorder='', docked=0, stored=0, dock='', first=n)
    fields.update(extra)
    return '[=ERROR=] [X4LAN] DIV1 SHIP ' + ' '.join('%s=%s' % kv for kv in fields.items())


def build_log(samples, interval=30, psector='A', dropped=None):
    """samples: list of {sector: [ship kwargs]} per tick."""
    lines = ['[X4LAN] DIV1 CFG label=x slot=p1 enabled=false world_mode= interval=%d' % interval]
    for i, sectors in enumerate(samples, 1):
        gt = i * interval
        lines.append('[X4LAN] DIV1 TICK n=%d gt=%d age=%.3f lgt= rt=1 wall=w psector=%s' % (i, gt, gt + .05, psector))
        for sector, ships in sectors.items():
            lines += [ship_line(i, sector, **s) for s in ships]
            drop = (dropped or {}).get((i, sector), 0)
            lines.append('[X4LAN] DIV1 SECTOR n=%d sector=%s player=%d found=%d requested=%d written=%d gone=0 dropped=%d errors=0 lgt='
                         % (i, sector, 1 if sector == psector else 0, len(ships) + drop, len(ships) + drop, len(ships), drop))
        lines.append('[X4LAN] DIV1 END n=%d ships=0 dropped=0 gone=0 errors=0 lgt= rt=1' % i)
    return cd.parse_log(lines)


class ParserTests(unittest.TestCase):
    def test_ignores_other_lines_and_counts_malformed(self):
        log = cd.parse_log(['[General] 1.0 unrelated', '[X4LAN] relay=connected',
                            '[X4LAN] DIV1 SHIP n=1 s=A id=x macro=m', '[X4LAN] DIV1 TICK n=a gt=1',
                            '[X4LAN] DIV1 SHIP n=1 n=2', '[X4LAN] DIV1 ' + 'x' * 5000,
                            ship_line(1, 'A', 5, x=float('nan'))])
        self.assertEqual(log.malformed, 5)
        self.assertEqual(log.runs, [])

    def test_ship_without_tick_is_orphaned_and_duplicates_dropped(self):
        lines = [ship_line(1, 'A', 5), '[X4LAN] DIV1 TICK n=1 gt=30 age=30 rt=1 psector=A',
                 ship_line(1, 'A', 5), ship_line(1, 'A', 5)]
        log = cd.parse_log(lines)
        self.assertEqual((log.orphans, log.duplicates), (1, 1))

    def test_percent_decoding_and_backwards_clock_starts_new_run(self):
        f = cd.parse_line('x [X4LAN] DIV1 SHIP n=1 s=A id=9 macro=m owner=o name=A%20B%7C%C3%A4 x=1 y=2 z=3 yaw=0 pitch=0 roll=0 hull= shield=')[1]
        self.assertEqual((f['name'], f['hull']), ('A B|\u00e4', None))
        log = cd.parse_log(['[X4LAN] DIV1 TICK n=1 gt=60 age=60', '[X4LAN] DIV1 TICK n=2 gt=90 age=90',
                            '[X4LAN] DIV1 TICK n=1 gt=30 age=30'])
        self.assertEqual([len(r) for r in log.runs], [2, 1])


class ComparatorTests(unittest.TestCase):
    def compare(self, host, guest, **kw):
        return cd.compare(host, guest, **kw)

    def test_identical_worlds_match_by_id_without_error(self):
        world = [{'A': [dict(id=1, code='AA-1', x=10), dict(id=2, x=20)], 'B': [dict(id=3, x=5)]}] * 3
        r = self.compare(build_log(world), build_log(world))
        total = cd.aggregate(r['rows'])
        self.assertEqual((total['matched_id'], total['host_only'], total['guest_only']), (9, 0, 0))
        self.assertEqual((total['existence_match'], total['pos_max'], total['hull_mismatch']), (1.0, 0.0, 0))
        self.assertEqual({row['category'] for row in r['rows']}, {'player', 'none'})

    def test_spawn_and_destroy_divergence(self):
        host = build_log([{'A': [dict(id=1), dict(id=2)]}, {'A': [dict(id=1), dict(id=2)]}])
        # Guest: ship 2 destroyed at tick 2, and a locally spawned ship far away.
        guest = build_log([{'A': [dict(id=1), dict(id=2)]}, {'A': [dict(id=1), dict(id=900, macro='ship_s', x=50000)]}])
        r = self.compare(host, guest)
        second = r['rows'][1]
        self.assertEqual((second['matched_id'], second['host_only'], second['guest_only']), (1, 1, 1))
        self.assertEqual(r['rows'][0]['host_only'], 0)

    def test_component_id_reuse_never_matches_different_ship(self):
        host = build_log([{'A': [dict(id=500, macro='ship_a', code='AAA-1')]}])
        guest = build_log([{'A': [dict(id=500, macro='ship_b', code='BBB-2')]}])
        row = self.compare(host, guest)['rows'][0]
        self.assertEqual((row['matched_id'], row['matched_pose'], row['id_conflicts'], row['host_only'], row['guest_only']),
                         (0, 0, 1, 1, 1))
        # Same macro but different idcode is also a conflict, not an id match.
        host = build_log([{'A': [dict(id=500, code='AAA-1')]}]); guest = build_log([{'A': [dict(id=500, code='CCC-3')]}])
        row = self.compare(host, guest)['rows'][0]
        self.assertEqual((row['matched_id'], row['id_conflicts']), (0, 1))

    def test_generation_changes_when_an_id_is_reused_in_one_log(self):
        log = build_log([{'A': [dict(id=7, macro='ship_a')]}, {'A': [dict(id=7, macro='ship_b')]}, {'A': [dict(id=7, macro='ship_b')]}])
        cd.assign_generations(log.runs[0])
        self.assertEqual([s.ships['A']['7']['gen'] for s in log.runs[0]], [0, 1, 1])

    def test_code_fallback_matches_preloaded_ship_with_different_ids(self):
        host = build_log([{'A': [dict(id=1, code='XYZ-9', x=0)]}])
        guest = build_log([{'A': [dict(id=77, code='XYZ-9', x=250)]}])
        row = self.compare(host, guest)['rows'][0]
        self.assertEqual((row['matched_code'], row['errors']), (1, [250.0]))

    def test_ambiguous_code_and_pose_fallback_is_reported_separately(self):
        host = build_log([{'A': [dict(id=1, x=0), dict(id=2, x=5000)]}])
        guest = build_log([{'A': [dict(id=11, x=100), dict(id=12, x=4000)]}])
        row = self.compare(host, guest, pose_tolerance=500)['rows'][0]
        self.assertEqual((row['matched_pose'], row['host_only'], row['guest_only']), (1, 1, 1))
        self.assertEqual((row['errors'], row['pose_errors']), ([], [100.0]))

    def test_position_hull_order_statistics_and_buckets(self):
        def tick(i):
            return {'B': [dict(id=k, x=k * i, hull=100 - (i if k == 1 else 0), order='Patrol' if k == 2 and i > 2 else 'TradeRoutine')
                          for k in range(1, 11)]}
        host = build_log([tick(0) for _ in range(40)], interval=30)
        guest = build_log([tick(i) for i in range(40)], interval=30)
        r = self.compare(host, guest)
        buckets = {s['bucket']: s for s in cd.summary_rows(r) if s['category'] == 'none' and s['sector'] == '*'}
        self.assertEqual(set(buckets), {'0-1min', '1-5min', '5-15min', '15-60min', 'all'})
        late = buckets['15-60min']
        self.assertGreater(late['pos_median'], buckets['0-1min']['pos_median'])
        self.assertEqual(late['pos_max'], 390.0)
        self.assertGreater(late['hull_rate'], 0); self.assertGreater(late['order_rate'], 0)
        self.assertEqual(buckets['0-1min']['samples'], 2)

    def test_incomplete_or_one_sided_sectors_are_skipped(self):
        host = build_log([{'A': [dict(id=1)], 'B': [dict(id=2)]}], dropped={(1, 'A'): 1})
        guest = build_log([{'A': [dict(id=1)]}])
        r = self.compare(host, guest)
        self.assertEqual(r['rows'], [])
        self.assertEqual(sorted(s for _, s in r['skipped']), ['A', 'B'])

    def test_player_owned_excluded_unless_requested(self):
        host = build_log([{'A': [dict(id=1, owner='player', x=0)]}])
        guest = build_log([{'A': [dict(id=1, owner='player', x=900)]}])
        self.assertEqual(self.compare(host, guest)['rows'][0]['matched_id'], 0)
        self.assertEqual(self.compare(host, guest, include_player_owned=True)['rows'][0]['errors'], [900.0])

    def test_unaligned_logs_are_rejected(self):
        with self.assertRaises(cd.ProbeLogError):
            self.compare(build_log([{'A': []}], interval=30), build_log([{'A': []}], interval=45))
        with self.assertRaises(cd.ProbeLogError):
            self.compare(cd.parse_log([]), build_log([{'A': []}]))

    def test_cli_writes_report_and_csvs(self):
        world = [{'A': [dict(id=1, x=1)], 'B': [dict(id=2)]}] * 3
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / n for n in ('host.log', 'guest.log', 'ticks.csv', 'summary.csv', 'report.txt')]
            for path in paths[:2]:
                lines = ['[X4LAN] DIV1 CFG label=x interval=30']
                for i, sectors in enumerate(world, 1):
                    lines.append('[X4LAN] DIV1 TICK n=%d gt=%d age=%d psector=A' % (i, i * 30, i * 30))
                    for sector, ships in sectors.items():
                        lines += [ship_line(i, sector, **s) for s in ships]
                        lines.append('[X4LAN] DIV1 SECTOR n=%d sector=%s player=%d found=1 requested=1 written=1 gone=0 dropped=0 errors=0'
                                     % (i, sector, sector == 'A'))
                    lines.append('[X4LAN] DIV1 END n=%d' % i)
                path.write_text('\n'.join(lines))
            with contextlib.redirect_stdout(io.StringIO()):
                code = cd.main([str(paths[0]), str(paths[1]), '--csv', str(paths[2]), '--summary-csv', str(paths[3]),
                                '--report', str(paths[4])])
            self.assertEqual(code, 0)
            ticks = list(csv.DictReader(io.StringIO(paths[2].read_text(encoding='utf-8'))))
            self.assertEqual(len(ticks), 6)
            self.assertEqual({t['category'] for t in ticks}, {'player', 'none'})
            summary = list(csv.DictReader(io.StringIO(paths[3].read_text(encoding='utf-8'))))
            self.assertTrue(any(s['bucket'] == 'all' and s['sector'] == '*' and math.isclose(float(s['existence_match']), 1.0)
                                for s in summary))
            self.assertIn('X4 DIVERGENCE PROBE COMPARISON', paths[4].read_text())
            empty = Path(folder) / 'empty.log'; empty.write_text('no probe lines')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cd.main([str(paths[0]), str(empty)]), 2)


if __name__ == '__main__':
    unittest.main()
