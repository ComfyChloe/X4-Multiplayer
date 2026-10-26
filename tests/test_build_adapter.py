"""LuaJIT doubles for the station planner hook and the authority validator. No engine claims:
the native path (plan import, add_build_to_expand_station) still needs a native test."""
import math
import unittest
import zlib
from pathlib import Path

from x4lan import build
from test_game_adapter import LuaRuntime
from test_build import entries

UI = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/ui'
HOST = 'a' * 32
GUEST = 'b' * 32

SETUP = r'''
events={};calls={};sent={};logs={};now=10;vanilla=0;mapVanilla=0;money=1000000;transfers={}
local realRequire=require;local realffi=require('ffi')
local function key(v) return (tostring(v):gsub('ULL$','')) end
local function cstr(s) local b=realffi.new('char[?]',#s+1,s);keep[#keep+1]=b;return b end
keep={}
world={
 ['1001']={idcode='ABC-123',macro='station_gen_factory_base_01_macro',owner='player',sectorid='S1',buildstorage='BS1'},
 ['1002']={idcode='XYZ-999',macro='station_gen_factory_base_01_macro',owner='player',sectorid='S1',buildstorage='BS2'},
 S1={macro='cluster_14_sector001_macro',owner='argon',isplayerowned=false},
 BS1={money=1000},BS2={money=0}}
plot={size={8000,8000,8000},center={0,0,0},paid={8000,8000,8000}}
stations={1001};modules=0;fileReady=true;library={};planInfo=nil;missingBlueprints='';valid=true;removed={}
plannerEntries={}
api={
 GetNumBuildMapConstructionPlan=function(h) return #plannerEntries end,
 GetBuildMapConstructionPlan2=function(h,st,stored,buf,n)
  for i,e in ipairs(plannerEntries) do
   local b=buf[i-1];b.idx=e.idx;b.macroid=cstr(e.macro);b.componentid=0;b.connectionid=cstr(e.connection)
   b.predecessoridx=e.pred;b.predecessorconnectionid=cstr(e.predconnection)
   b.offset.x,b.offset.y,b.offset.z=e.pos[1],e.pos[2],e.pos[3]
   b.offset.yaw,b.offset.pitch,b.offset.roll=e.rot[1],e.rot[2],e.rot[3]
  end
  return #plannerEntries
 end,
 GetConstructionMapItemLoadoutCounts2=function(c,h,idx) local e=plannerEntries[tonumber(idx)];c.numturretgroups=e and e.turrets and 1 or 0;c.numturrets=e and e.slotTurret and 1 or 0 end,
 GetConstructionMapItemLoadout2=function(l,h,idx)
  local e=plannerEntries[tonumber(idx)]
  if e and e.turrets then local g=l.turretgroups[0];g.macro=cstr('turret_arg_m_laser_02_mk1_macro');g.path=cstr('..');g.group=cstr('group_01');g.count=3 end
  if e and e.slotTurret then l.turrets[0].macro=cstr('turret_arg_m_laser_02_mk1_macro') end
 end,
 GetBuildPlotSize=function() return realffi.new('Coord3D',plot.size[1],plot.size[2],plot.size[3]) end,
 GetBuildPlotCenterOffset=function() return realffi.new('Coord3D',plot.center[1],plot.center[2],plot.center[3]) end,
 GetPaidBuildPlotSize=function() return realffi.new('Coord3D',plot.paid[1],plot.paid[2],plot.paid[3]) end,
 ExtendBuildPlot=function(st,pos,neg)
  for i,k in ipairs({'x','y','z'}) do
   local p=plot.size[i]/2+plot.center[i]+pos[k];local q=plot.size[i]/2-plot.center[i]+neg[k]
   plot.size[i]=p+q;plot.center[i]=(p-q)/2
  end
  calls[#calls+1]={control='ExtendBuildPlot'};return true
 end,
 GetBuildPlotPayment=function() return 0 end,
 GetBuildPlotPrice=function() return plotPrice or 0 end,
 PayBuildPlotSize=function(st,size) plot.paid={size.x,size.y,size.z};calls[#calls+1]={control='PayBuildPlotSize'} end,
 GetSectorControlStation=function() return 77 end,
 GetObjectPositionInSector=function() return realffi.new('UIPosRot') end,
 GetNumAllFactionStations=function(f) return f=='player' and #stations or 0 end,
 GetAllFactionStations=function(buf,n,f) for i,v in ipairs(stations) do buf[i-1]=v end;return #stations end,
 GetNumStationModules=function() return modules end,
 RemoveBuildPlot2=function(st,storage) removed[#removed+1]={key(st),storage};return true end,
 GetNumImportableConstructionPlans=function() return fileReady and 1 or 0 end,
 GetImportableConstructionPlans=function(buf) if fileReady then buf[0].filename=cstr(fileName);buf[0].id=cstr(fileName) end;return fileReady and 1 or 0 end,
 ImportMapConstructionPlan=function(file,id) library[realffi.string(id)]=realffi.string(file) end,
 GetNumConstructionPlans=function() local n=0;for _ in pairs(library) do n=n+1 end;return n end,
 GetConstructionPlans=function(buf) local i=0;for id in pairs(library) do buf[i].id=cstr(id);buf[i].source=cstr('local');i=i+1 end;return i end,
 RemoveConstructionPlan=function(src,id) library[realffi.string(id)]=nil;return true end,
 IsConstructionPlanValid=function(id,n) return valid end,
 GetNumConstructionPlanInfo=function() return #planInfo end,
 GetConstructionPlanInfo=function(buf)
  for i,e in ipairs(planInfo) do
   local b=buf[i-1];b.idx=i;b.macroid=cstr(e.macro);b.connectionid=cstr(e.connection);b.predecessoridx=e.pred;b.predecessorconnectionid=cstr(e.predconnection)
   b.offset.x,b.offset.y,b.offset.z=e.pos[1],e.pos[2],e.pos[3];b.offset.yaw,b.offset.pitch,b.offset.roll=e.rot[1],e.rot[2],e.rot[3]
  end
  return #planInfo
 end,
 GetMissingConstructionPlanBlueprints3=function() return cstr(missingBlueprints) end,
 IsComponentOperational=function() return true end}
require=function(name)
 if name=='ffi' then return setmetatable({C=api},{__index=realffi}) end
 return realRequire(name)
end
RegisterEvent=function(k,f) events[k]=f end
AddUITriggeredEvent=function(s,c,a) calls[#calls+1]={screen=s,control=c,args=a} end
DebugError=function(v) logs[#logs+1]=v end;GetCurRealTime=function() return now end
GetComponentData=function(id,...)
 local o=world[key(id)] or {};local out={}
 for i,k in ipairs({...}) do out[i]=o[k] end
 return unpack(out,1,select('#',...))
end
ConvertStringTo64Bit=function(v) return v end
GetPlayerMoney=function() return money end
TransferPlayerMoneyTo=function(a,t) transfers[#transfers+1]={a,key(t)};money=money-a end
TransferMoneyToPlayer=function(a,t) transfers[#transfers+1]={-a,key(t)};money=money+a end
planner={name='StationConfigurationMenu',container=realffi.new('uint64_t',1001),holomap=5,buttonConfirm=function() vanilla=vanilla+1 end}
mapMenu={name='MapMenu',buttonRemovePlot=function(st,confirmed,rm) mapVanilla=mapVanilla+1 end}
Helper={getMenu=function(n) if n=='StationConfigurationMenu' then return planner elseif n=='MapMenu' then return mapMenu end end}
X4LAN_CONFIG={world_sync=true,station_build_intents=true,slot='p2'}
X4LAN_BUBBLE_API={state={client={lease=LEASE},transport={send=function(self,p) sent[#sent+1]=p end}},
 paused=function() return false end,id=function(v) return key(v) end,
 universe={latest={epoch=HOSTEPOCH,revision=4},receive=function() return false end,frame=function() end}}
'''


def lua_entries(n=3):
    return '\n'.join(
        f"plannerEntries[{i}]={{idx={i},macro='prod_gen_energycells_{i:02d}_macro',connection='{'' if i == 1 else 'connectionsnap001'}',"
        f"pred={0 if i == 1 else i - 1},predconnection='{'' if i == 1 else 'connectionsnap002'}',pos={{0,{-800 * i},12.5}},"
        f"rot={{{math.radians(90 * (i % 4))},0,0}},turrets={'true' if i == 2 else 'false'}}}" for i in range(1, n + 1))


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class BuildAdapterTests(unittest.TestCase):
    def make(self, slot='p2', enabled=True, world=True):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lease = GUEST if slot == 'p2' else HOST
        lua.execute(f"LEASE='{lease}';HOSTEPOCH='{HOST}';fileName=''")
        lua.execute(SETUP)
        lua.execute(f"X4LAN_CONFIG.slot='{slot}';X4LAN_CONFIG.station_build_intents={str(enabled).lower()};X4LAN_CONFIG.world_sync={str(world).lower()}")
        lua.execute(lua_entries())
        lua.execute((UI / 'build_protocol.lua').read_text())
        lua.execute((UI / 'build_game.lua').read_text())
        lua.execute('A=X4LAN_BUBBLE_API;U=A.universe;G=A.build')
        return lua

    def logs(self, lua):
        return list(lua.globals().logs.values())

    def notices(self, lua):
        return [c.args for c in lua.globals().calls.values() if c.control == 'Notice']

    def sent(self, lua, prefix):
        return [p for p in lua.globals().sent.values() if p.startswith(prefix)]

    # ---------------------------------------------------------------- guest
    def test_confirm_sends_intent_and_never_builds_locally(self):
        lua = self.make()
        lua.execute('planner.buttonConfirm()')
        g = lua.globals()
        self.assertEqual(g.vanilla, 0)
        self.assertTrue(lua.eval('G.active~=nil'))
        lua.execute('U.frame(now)')
        header = self.sent(lua, 'BUILDREQ|')[0].split('|')
        self.assertEqual(header[:6], ['BUILDREQ', '1', GUEST, header[3], HOST, 'EXPAND'])
        self.assertEqual(header[6:9], ['ABC-123', 'station_gen_factory_base_01_macro', 'cluster_14_sector001_macro'])
        self.assertEqual((header[9], header[10], header[11]), ('8000,8000,8000,0,0,0', '1000', '3'))
        parts = [p.split('|', 5)[5] for p in self.sent(lua, 'BUILDPART|')]
        xml = ''.join(parts)
        self.assertEqual(int(header[12]), len(xml));self.assertEqual(int(header[14]), zlib.adler32(xml.encode()))
        plan_id, parsed = build.parse_plan(xml)
        self.assertEqual(plan_id, 'x4lan_' + header[3])
        self.assertEqual(parsed[1]['groups'][0]['macro'], 'turret_arg_m_laser_02_mk1_macro')
        self.assertEqual(parsed[1]['pred'], 1);self.assertAlmostEqual(parsed[1]['rot'][0], 180.0, 3)
        self.assertTrue(all(len(p) <= 768 for p in g.sent.values()))
        self.assertTrue(any('BUILDREQ sent request=' in v for v in self.logs(lua)))
        self.assertIn('sent to the host', self.notices(lua)[0])
        self.assertTrue(any('planner hook installed' in v for v in self.logs(lua)))

    def test_pending_then_refusal_shows_reason_and_clears(self):
        lua = self.make()
        lua.execute('planner.buttonConfirm();U.frame(now);R=G.active.request')
        lua.execute("U.receive({'BUILDREPLY','1',LEASE,R,'PENDING','ok','-'},now)")
        self.assertEqual(lua.eval('G.active.state'), 'PENDING')
        lua.execute("now=now+1;U.frame(now)")
        self.assertEqual(self.sent(lua, 'BUILDPART|')[-1:], self.sent(lua, 'BUILDPART|')[-1:])
        before = len(self.sent(lua, 'BUILDPART|'))
        lua.execute("now=now+1;U.frame(now)")
        self.assertEqual(len(self.sent(lua, 'BUILDPART|')), before)  # parts stop once pending
        lua.execute("U.receive({'BUILDREPLY','1',LEASE,R,'REFUSED','funds','-'},now)")
        self.assertTrue(lua.eval('G.active==nil'))
        self.assertIn('not enough money', self.notices(lua)[-1])
        self.assertTrue(any('BUILDREQ refused request=' in v and 'reason=funds' in v for v in self.logs(lua)))
        self.assertEqual(lua.globals().vanilla, 0)

    def test_accept_and_uncertain_messages(self):
        lua = self.make()
        lua.execute("planner.buttonConfirm();U.frame(now);R=G.active.request;U.receive({'BUILDREPLY','1',LEASE,R,'ACCEPTED','build_created','-'},now)")
        self.assertIn('accepted', self.notices(lua)[-1])
        self.assertIn('not shown in your game yet', self.notices(lua)[-1])
        lua.execute("now=now+2;planner.buttonConfirm();U.frame(now);R=G.active.request;U.receive({'BUILDREPLY','1',LEASE,R,'UNCERTAIN','authority_lost','-'},now)")
        self.assertIn('uncertain', self.notices(lua)[-1])
        self.assertTrue(any('BUILDREQ uncertain' in v for v in self.logs(lua)))

    def test_missing_parts_are_resent_and_foreign_replies_ignored(self):
        lua = self.make()
        lua.execute("planner.buttonConfirm();U.frame(now);R=G.active.request")
        lua.execute("U.receive({'BUILDREPLY','1',LEASE,string.rep('9',32),'REFUSED','funds','-'},now)")
        self.assertTrue(lua.eval('G.active~=nil'))
        lua.execute("U.receive({'BUILDREPLY','1',LEASE,R,'RECEIVING','ok','2'},now);sent={};now=now+1;U.frame(now)")
        parts = self.sent(lua, 'BUILDPART|')
        self.assertEqual([p.split('|')[4] for p in parts], ['2'])

    def test_busy_and_local_refusals_send_nothing(self):
        lua = self.make()
        lua.execute('planner.buttonConfirm();planner.buttonConfirm()')
        self.assertTrue(any('reason=busy' in v for v in self.logs(lua)))
        lua = self.make()
        lua.execute("world['1001'].owner='argon';planner.buttonConfirm()")
        self.assertTrue(any('reason=not_owner' in v for v in self.logs(lua)))
        lua.execute("world['1001'].owner='player';plannerEntries[3].slotTurret=true;planner.buttonConfirm()")
        self.assertTrue(any('reason=loadout_slot_macros_unsupported' in v for v in self.logs(lua)))
        lua.execute("plannerEntries={};planner.buttonConfirm()")
        self.assertTrue(any('reason=empty_plan_unsupported' in v for v in self.logs(lua)))
        lua.execute("plannerEntries=nil;planner.buttonConfirm()")  # capture error never falls through
        self.assertTrue(any('capture_error' in v for v in self.logs(lua)))
        lua.execute('U.frame(now)')
        self.assertEqual(self.sent(lua, 'BUILD'), [])
        self.assertEqual(lua.globals().vanilla, 0)

    def test_silent_relay_disables_feature_without_local_build(self):
        lua = self.make()
        lua.execute('planner.buttonConfirm();U.frame(now);now=now+16;U.frame(now)')
        self.assertTrue(any('FEATURE station_build_intents DISABLED reason=relay_unsupported' in v for v in self.logs(lua)))
        self.assertTrue(lua.eval('G.active==nil'))
        self.assertEqual(lua.globals().vanilla, 0)

    def test_remove_plot_is_an_intent(self):
        lua = self.make()
        lua.execute('mapMenu.buttonRemovePlot(planner.container,false)')
        self.assertEqual(lua.globals().mapVanilla, 1)  # confirmation dialog stays vanilla
        lua.execute('mapMenu.buttonRemovePlot(planner.container,true,true);U.frame(now)')
        self.assertEqual(lua.globals().mapVanilla, 1)
        header = self.sent(lua, 'BUILDREQ|')[0].split('|')
        self.assertEqual(header[5:], ['REMOVEPLOT', 'ABC-123', 'station_gen_factory_base_01_macro', 'cluster_14_sector001_macro', '-', '1', '0', '0', '0', '1'])

    def test_host_and_disabled_keep_vanilla(self):
        lua = self.make('p1')
        lua.execute('planner.buttonConfirm()')
        self.assertEqual(lua.globals().vanilla, 1)
        lua = self.make(enabled=False)
        lua.execute('planner.buttonConfirm()')
        self.assertEqual(lua.globals().vanilla, 1)
        self.assertTrue(lua.eval('X4LAN_BUBBLE_API.build==nil'))
        lua = self.make(world=False)
        self.assertTrue(any('FEATURE station_build_intents DISABLED reason=requires_world_sync' in v for v in self.logs(lua)))

    # ---------------------------------------------------------------- authority
    def host_request(self, lua, request='c' * 32, plan=None, budget='5000', plot='8000,8000,9000,0,0,-500', inbox='1', kind='EXPAND'):
        plan = plan or entries()
        xml = build.encode_plan('x4lan_' + request, plan) if kind == 'EXPAND' else ''
        parts = [xml[i:i + build.PART_SIZE] for i in range(0, len(xml), build.PART_SIZE)]
        if kind == 'EXPAND':
            fields = ['BUILDEXEC', '1', HOST, request, 'p2', 'EXPAND', 'ABC-123', 'station_gen_factory_base_01_macro',
                      'cluster_14_sector001_macro', plot, budget, str(len(plan)), str(len(xml)), str(len(parts)), str(zlib.adler32(xml.encode())), inbox]
        else:
            fields = ['BUILDEXEC', '1', HOST, request, 'p2', kind, 'ABC-123', 'station_gen_factory_base_01_macro',
                      'cluster_14_sector001_macro', '-', '1' if kind == 'REMOVEPLOT' else '-', '0', '0', '0', '1', '0']
        lua.globals().fileName = 'x4lan_' + request
        lua.execute('function receive(t) U.receive(t,now) end')
        lua.globals().receive(lua.table_from(dict(enumerate(fields, 1))))
        for i, chunk in enumerate(parts, 1):
            lua.globals().receive(lua.table_from(dict(enumerate(['BUILDXPART', '1', HOST, request, str(i), chunk], 1))))
        info = ['{' + ','.join(f'{k}={v}' for k, v in [
            ('macro', repr(e['macro'])), ('connection', repr(e['connection'])), ('pred', e['pred']), ('predconnection', repr(e['predconnection'])),
            ('pos', '{%s}' % ','.join(map(str, e['pos']))), ('rot', '{%s}' % ','.join(str(math.radians(v)) for v in e['rot']))]) + '}' for e in plan]
        lua.execute('planInfo={' + ','.join(info) + '}')
        return request

    def done(self, lua):
        return [p.split('|')[4:6] for p in self.sent(lua, 'BUILDDONE|')]

    def run_host(self, lua, frames=3):
        for _ in range(frames):
            lua.execute('now=now+.1;U.frame(now)')

    def test_authority_validates_imports_and_creates_native_build(self):
        lua = self.make('p1')
        request = self.host_request(lua)
        self.assertEqual(self.done(lua), [['RECEIVED', 'ok']])
        self.run_host(lua, 1)
        expand = [c for c in lua.globals().calls.values() if c.control == 'Expand']
        self.assertEqual(len(expand), 1)
        self.assertEqual((expand[0].args.station, expand[0].args.plan, expand[0].args.owner), ('1001', 'x4lan_' + request, 'player'))
        self.assertEqual(lua.eval('library["x4lan_"..string.rep("c",32)]'), 'x4lan_' + request)
        g = lua.globals()
        self.assertEqual((g.plot.size[3], g.plot.center[3]), (9000, -500))  # plot = station, extended on the host
        lua.execute(f"events['X4LAN_Build.Result'](nil,'{request}|1|42')")
        self.assertEqual(self.done(lua)[-1], ['ACCEPTED', 'build_created'])
        self.assertEqual(list(g.transfers[1].values()), [4000, 'BS1'])  # storage 1000 -> budget 5000
        # Duplicate execution after completion only repeats the stored result.
        lua.execute('sent={}')
        lua.globals().receive(lua.table_from(dict(enumerate(['BUILDEXEC', '1', HOST, request, 'p2', 'EXPAND', 'ABC-123',
            'station_gen_factory_base_01_macro', 'cluster_14_sector001_macro', '-', '-', '3', '1', '1', '1', '1'], 1))))
        self.assertEqual(self.done(lua), [['ACCEPTED', 'build_created']])
        self.assertEqual(len([c for c in g.calls.values() if c.control == 'Expand']), 1)
        self.assertTrue(any('BUILDREQ accepted request=' + request in v for v in self.logs(lua)))

    def refusal(self, setup='', **kw):
        lua = self.make('p1')
        lua.execute(setup)
        self.host_request(lua, **kw)
        self.run_host(lua, 70 if 'fileReady=false' in setup else 2)
        self.assertFalse([c for c in lua.globals().calls.values() if c.control == 'Expand'])
        return self.done(lua)[-1]

    def test_authority_refusals(self):
        self.assertEqual(self.refusal('stations={}'), ['REFUSED', 'station_not_found_for_owner'])
        self.assertEqual(self.refusal("stations={1001,1002};world['1002'].idcode='ABC-123'"), ['REFUSED', 'station_ambiguous'])
        self.assertEqual(self.refusal("world['1001'].owner='argon'"), ['REFUSED', 'not_owner'])
        self.assertEqual(self.refusal(inbox='0'), ['REFUSED', 'no_plan_inbox'])
        self.assertEqual(self.refusal('fileReady=false'), ['REFUSED', 'plan_file_missing'])
        self.assertEqual(self.refusal('valid=false'), ['REFUSED', 'plan_invalid_patches:0'])
        self.assertEqual(self.refusal("missingBlueprints='prod_gen_energycells_01_macro'"), ['REFUSED', 'missing_blueprints'])
        self.assertEqual(self.refusal('money=100'), ['REFUSED', 'funds'])
        self.assertEqual(self.refusal("plotPrice=2000000;world.S1.owner='argon'"), ['REFUSED', 'funds'])

    def test_funds_refusal_reverts_plot_extension(self):
        lua = self.make('p1')
        lua.execute('money=100')
        self.host_request(lua);self.run_host(lua, 1)
        g = lua.globals()
        self.assertEqual((g.plot.size[3], g.plot.center[3]), (8000, 0))

    def test_imported_plan_must_match_request_exactly(self):
        lua = self.make('p1')
        self.host_request(lua)
        lua.execute('planInfo[2].pos[2]=planInfo[2].pos[2]+5')
        self.run_host(lua, 1)
        self.assertEqual(self.done(lua)[-1], ['REFUSED', 'plan_mismatch:position2'])
        lua = self.make('p1')
        self.host_request(lua)
        lua.execute("planInfo[3].macro='prod_gen_wheat_macro'")
        self.run_host(lua, 1)
        self.assertEqual(self.done(lua)[-1], ['REFUSED', 'plan_mismatch:entry3'])

    def test_native_failure_and_timeout(self):
        lua = self.make('p1')
        request = self.host_request(lua);self.run_host(lua, 1)
        lua.execute(f"events['X4LAN_Build.Result'](nil,'{request}|0|')")
        self.assertEqual(self.done(lua)[-1], ['REFUSED', 'native_build_failed'])
        self.assertFalse(list(lua.globals().transfers.values()))
        lua = self.make('p1')
        self.host_request(lua);self.run_host(lua, 1)
        lua.execute('now=now+31;U.frame(now)')
        self.assertEqual(self.done(lua)[-1], ['UNCERTAIN', 'native_timeout'])

    def test_corrupt_plan_refused_before_any_engine_call(self):
        lua = self.make('p1')
        request = 'd' * 32
        xml = build.encode_plan('x4lan_' + request, entries())
        bad = xml.replace('0.000', '0.001', 1)
        fields = ['BUILDEXEC', '1', HOST, request, 'p2', 'EXPAND', 'ABC-123', 'station_gen_factory_base_01_macro',
                  'cluster_14_sector001_macro', '-', '-', '3', str(len(xml)), str(math.ceil(len(xml) / build.PART_SIZE)), str(zlib.adler32(xml.encode())), '1']
        lua.execute('function receive(t) U.receive(t,now) end')
        lua.globals().receive(lua.table_from(dict(enumerate(fields, 1))))
        for i in range(0, len(bad), build.PART_SIZE):
            lua.globals().receive(lua.table_from(dict(enumerate(['BUILDXPART', '1', HOST, request, str(i // build.PART_SIZE + 1), bad[i:i + build.PART_SIZE]], 1))))
        self.assertEqual(self.done(lua)[-1], ['REFUSED', 'digest'])
        self.assertFalse(dict(lua.globals().library))

    def test_remove_plot_requires_empty_station(self):
        lua = self.make('p1')
        lua.execute('modules=2')
        self.host_request(lua, kind='REMOVEPLOT');self.run_host(lua, 1)
        self.assertEqual(self.done(lua)[-1], ['REFUSED', 'has_modules'])
        lua = self.make('p1')
        self.host_request(lua, kind='REMOVEPLOT');self.run_host(lua, 1)
        self.assertEqual(self.done(lua)[-1], ['ACCEPTED', 'plot_removed'])
        self.assertEqual(list(lua.globals().removed[1].values()), ['1001', True])

    def test_recycle_goes_through_md(self):
        lua = self.make('p1')
        request = self.host_request(lua, kind='RECYCLE');self.run_host(lua, 1)
        self.assertEqual([c.control for c in lua.globals().calls.values() if c.screen == 'X4LAN_Build'], ['Recycle'])
        lua.execute(f"events['X4LAN_Build.Result'](nil,'{request}|1|7')")
        self.assertEqual(self.done(lua)[-1], ['ACCEPTED', 'recycle_created'])

    def test_empty_plot_station_is_a_valid_expand_target(self):
        # A station with only its plot (0 modules) is still a player-owned station object.
        lua = self.make('p1')
        lua.execute('modules=0')
        self.host_request(lua);self.run_host(lua, 1)
        self.assertEqual(len([c for c in lua.globals().calls.values() if c.control == 'Expand']), 1)


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class BuildProtocolTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute((UI / 'build_protocol.lua').read_text())
        self.lua.execute('B=X4LAN_BUILD_PROTOCOL')

    def test_plot_delta_and_funds(self):
        d = self.lua.eval('B.plotDelta({8000,8000,9000,0,0,-500},{8000,8000,8000,0,0,0})')
        self.assertEqual((d.pos[3], d.neg[3]), (0, 1000))
        self.assertIsNone(self.lua.eval('B.plotDelta({8000,8000,8000,0,0,0},{8000,8000,8000,0,0,0.4})'))
        self.assertEqual(self.lua.eval('B.funds(100,50,120,0)').transfer, 70)
        self.assertEqual(self.lua.eval('B.funds(100,50,120,40)'), (None, 'funds'))
        self.assertEqual(self.lua.eval('B.funds(0,500,100,0)').transfer, -400)

    def test_owner_slot_mapping_is_the_extension_point(self):
        self.assertTrue(self.lua.eval("B.ownerAllowed('p2','player')"))
        self.assertFalse(self.lua.eval("B.ownerAllowed('p2','argon')"))
        self.assertFalse(self.lua.eval("B.ownerAllowed('p9','player')"))


if __name__ == '__main__':
    unittest.main()
