"""NPC replica motion: shared prediction, send on divergence, dead reckoning and snap backstop.

Synthetic engine doubles and synthetic peers only. Nothing here is evidence of native X4
behaviour; the live failure modes they reproduce are described in the commit message.
"""
from pathlib import Path
import math
import random
import unittest

from test_game_adapter import LuaRuntime
import test_universe_adapter as adapter  # module import: its test classes are not collected twice

UI = Path(__file__).resolve().parents[1] / 'src/extension/x4_lan_lab/ui'
SECTOR = 'cluster_14_sector001_macro'


def motion():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute((UI / 'motion.lua').read_text())
    lua.execute('M=X4LAN_MOTION')
    return lua


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class SharedPredictionTests(unittest.TestCase):
    def test_straight_constant_turn_cap_and_heading_mismatch(self):
        l = motion()
        l.execute('''
          local p,v=M.drPredict({0,0,0,0,0,0},{0,0,300,0,0,0},2)
          assert(p[3]==600 and p[1]==0 and v[3]==300)
          -- 300 m/s turning at 0.5 rad/s: an arc of radius 600 m, heading +1 rad after 2 s.
          p,v=M.drPredict({0,0,0,0,0,0},{0,0,300,.5,0,0},2)
          assert(math.abs(p[1]-600*(1-math.cos(1)))<1e-6 and math.abs(p[3]-600*math.sin(1))<1e-6,p[1]..' '..p[3])
          assert(math.abs(p[4]-1)<1e-9 and math.abs(math.atan2(v[1],v[3])-1)<1e-9)
          -- Opposite yaw convention (yaw = -heading, yaw rate -0.5): the course still turns
          -- toward +heading, from heading 1 rad to 2 rad.
          local q,qv=M.drPredict({0,0,0,-1,0,0},{300*math.sin(1),0,300*math.cos(1),-.5,0,0},2)
          assert(math.abs(math.atan2(qv[1],qv[3])-2)<1e-9)
          assert(math.abs(q[1]-600*(math.cos(1)-math.cos(2)))<1e-6 and math.abs(q[3]-600*(math.sin(2)-math.sin(1)))<1e-6)
          -- Turns end after turnCap (pi/2): then the course is straight.
          p,v=M.drPredict({0,0,0,0,0,0},{0,0,300,.5,0,0},10)
          assert(math.abs(p[4]-math.pi/2)<1e-9 and math.abs(v[1]-300)<1e-6 and math.abs(v[3])<1e-6)
          assert(math.abs(p[3]-600)<1e-6 and math.abs(p[1]-(600+300*(10-math.pi)))<1e-6)
          -- Sideways drift (velocity heading far from yaw) is predicted straight.
          p=M.drPredict({0,0,0,0,0,0},{300,0,0,.5,0,0},2)
          assert(p[1]==600 and p[3]==0)
        ''')

    def test_prediction_is_deterministic_and_matches_the_server_check(self):
        # The server decides with M.drPredict(sent pose, sent velocity, elapsed); clients render
        # the same function of the same sample, so both agree without new packets.
        l = motion()
        l.execute('''
          local a,b=M.drPredict({1,2,3,.3,.1,0},{100,5,200,.2,.01,0},3.7),M.drPredict({1,2,3,.3,.1,0},{100,5,200,.2,.01,0},3.7)
          for i=1,6 do assert(a[i]==b[i]) end
        ''')


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class DeadReckoningTests(unittest.TestCase):
    def test_server_clock_rate_is_estimated_where_the_old_offset_ran_ahead(self):
        # Live dedicated server: the engine steps a fixed 0.05 s per frame at 12-20 fps, so its
        # clock runs at 0.6-1.1 x wall time (agent CADENCE dreal median 0.9, mean 0.79). The old
        # buffer's minimum offset can only fall; with a slower source clock its render target
        # ran past every sample and NPC replicas froze, hopping once per sample.
        l = motion()
        l.execute('''
          math.randomseed(3)
          local clock=M.drClock();local track={}
          local old={stale=2.5,cap=50000,retainClock=true,leak=M.leak}
          local frozen,oldFrozen,maxT=0,0,0
          local nextS=0;local queue={}
          for f=1,60*30 do
           local now=f/60
           while nextS<=now do
            queue[#queue+1]={time=100+.8*nextS,at=nextS+.02+math.random()*.08};nextS=nextS+.25
           end
           local keep={}
           for _,q in ipairs(queue) do
            if q.at<=now then
             local x=3000*(q.time-100)
             M.drPush(track,clock,q.time,{x,0,0,0,0,0},{3000,0,0,0,0,0},now)
             M.push(old,q.time,{x,0,0,0,0,0},{3000,0,0,0,0,0},now)
            else keep[#keep+1]=q end
           end
           queue=keep
           if f>600 then
            local T=M.drTime(clock,now);local p,v=M.drRender(track,T,now,clock.rate)
            -- The source time rendered stays within 0.15 s of the server's present.
            maxT=math.max(maxT,math.abs(T-(100+.8*now)))
            if M.speed(v)<100 then frozen=frozen+1 end
            local q,w=M.renderAt(old,M.clock(old,now,.3),.5)
            if M.speed(w)<1 then oldFrozen=oldFrozen+1 end
           end
          end
          assert(math.abs(clock.rate-.8)<.03,clock.rate)
          assert(maxT<.15,maxT)
          assert(frozen==0,frozen)
          assert(oldFrozen>1000,oldFrozen)  -- reproduction: the old buffer froze most frames
        ''')

    def test_arrival_jitter_does_not_move_a_replica(self):
        l = motion()
        l.execute('''
          math.randomseed(5)
          local clock=M.drClock();local track={};local nextS=0;local prev;local worst=0;local queue={}
          for f=1,60*10 do
           local now=f/60
           while nextS<=now do queue[#queue+1]={t=nextS,at=nextS+.02+math.random()*.15};nextS=nextS+.1 end
           local keep={}
           for _,q in ipairs(queue) do
            if q.at<=now then M.drPush(track,clock,q.t,{300*q.t,0,0,0,0,0},{300,0,0,0,0,0},now) else keep[#keep+1]=q end
           end
           queue=keep
           if f>180 then
            local p=M.drRender(track,M.drTime(clock,now),now,clock.rate)
            if prev then worst=math.max(worst,math.abs((p[1]-prev)-300/60)) end
            prev=p[1]
           end
          end
          assert(worst<1,worst)
        ''')

    def test_correction_blends_without_a_step_and_converges(self):
        l = motion()
        l.execute('''
          local clock=M.drClock();local track={}
          for k=0,10 do M.drPush(track,clock,k*.1,{300*k*.1,0,0,0,0,0},{300,0,0,0,0,0},k*.1+.03) end
          local now=1.03;local T=M.drTime(clock,now);local before=M.drRender(track,T,now)
          -- The NPC really is 40 m to the side: the next sample says so.
          local ok,err,snapped=M.drPush(track,clock,1.1,{330,40,0,0,0,0},{300,0,0,0,0,0},1.13)
          assert(ok and not snapped and math.abs(err-40)<1,err)
          local prevY=before[2];local maxStep=0
          for f=1,120 do
           now=1.13+f/60;local p=M.drRender(track,M.drTime(clock,now),now)
           maxStep=math.max(maxStep,math.abs(p[2]-prevY));prevY=p[2]
          end
          -- 40 m over tau 0.3 s: at most about 40/0.3/60 = 2.2 m per frame, no 40 m step.
          assert(maxStep<2.5,maxStep);assert(math.abs(prevY-40)<.1,prevY)
        ''')

    def test_large_error_snaps_once_per_second(self):
        l = motion()
        l.execute('''
          local clock=M.drClock();local track={}
          for k=0,5 do M.drPush(track,clock,k*.1,{300*k*.1,0,0,0,0,0},{300,0,0,0,0,0},k*.1) end
          local ok,err,snapped=M.drPush(track,clock,.6,{20000,0,0,0,0,0},{300,0,0,0,0,0},.6)
          assert(snapped and err>19000)
          local p=M.drRender(track,M.drTime(clock,.6),.6);assert(math.abs(p[1]-20000)<1)
          -- Another discontinuity within 1 s blends instead of snapping again.
          ok,err,snapped=M.drPush(track,clock,.9,{40000,0,0,0,0,0},{300,0,0,0,0,0},.9)
          assert(not snapped and err>15000)
          ok,err,snapped=M.drPush(track,clock,1.7,{90000,0,0,0,0,0},{300,0,0,0,0,0},1.7)
          assert(snapped)
        ''')

    def test_prediction_coasts_then_holds(self):
        l = motion()
        l.execute('''
          local clock=M.drClock();local track={}
          M.drPush(track,clock,0,{0,0,0,0,0,0},{100,0,0,0,0,0},0)
          local p,v=M.drRender(track,M.drTime(clock,5),5);assert(math.abs(p[1]-500)<1e-6 and v[1]==100)
          p,v=M.drRender(track,M.drTime(clock,20),20);assert(math.abs(p[1]-100*M.dr.coast)<1e-6 and v[1]==0)
        ''')


HOST = '''
  npcs={};ids={}
  api.GetNumAllFactionShips=function(f) return f=='argon' and #ids or 0 end
  api.GetAllFactionShips=function(out,cap,f)
   if f~='argon' then return 0 end;for i,id in ipairs(ids) do out[i-1]=id end;return #ids
  end
  -- heading h, speed s, turn rate w (rad/s) or a function of time; yaw = sign*heading.
  function addNpc(id,x,z,s,w,sign)
   ids[#ids+1]=id;npcs[id]={h=0,s=s,w=w or 0,sign=sign or 1}
   positions[id]={x=x,y=0,z=z,yaw=0,pitch=0,roll=0}
  end
  function step(dt)
   for id,n in pairs(npcs) do
    local p=positions[id];if p then
     local w=type(n.w)=='function' and n.w(now) or n.w
     n.h=n.h+w*dt;p.x=p.x+math.sin(n.h)*n.s*dt;p.z=p.z+math.cos(n.h)*n.s*dt;p.yaw=M.angle(n.sign*n.h)
    end
   end
  end
  M=X4LAN_MOTION
  function hostTick(k) for i=1,k do now=now+.05;step(.05);frames.onUpdate() end end
  function moves(from)
   local out={};for i=from+1,#sent do local f=P.split(sent[i],'|');if f[1]=='UMOVE' then out[#out+1]=f end end;return out
  end
  function countFor(list,id) local n=0;for _,f in ipairs(list) do if tonumber(f[5])==U.identities[tostring(id)].id then n=n+1 end end;return n end
  function start() U.receive({'UINTEREST','1',client.lease,'1','cluster_14_sector001_macro'},now);hostTick(4);U.sender.done=true;hostTick(2) end
'''


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class ServerSendOnDivergenceTests(unittest.TestCase):
    make = adapter.UniverseAdapterTests.make

    def host(self, setup=''):
        l = self.make('p1', setup=setup)
        l.execute(HOST)
        return l

    def test_still_and_straight_npcs_send_keepalives_maneuvering_ones_by_relevance(self):
        # Player ships: 11 at (100,200,300) and 42 (the second shared ship).
        l = self.host()
        l.execute('''
          addNpc(1001,40000,40000,0)                 -- still, cold
          addNpc(1002,60000,0,3000)                  -- straight, fast, cold
          addNpc(1003,1000,1000,300)                 -- straight, hot
          addNpc(1004,800,-800,300,.5)               -- constant turn, hot: predicted exactly
          addNpc(1005,-900,500,300,function(t) return 1.2*math.sin(1.7*t) end)  -- maneuvering, hot
          addNpc(1006,90000,0,300,function(t) return 1.2*math.sin(1.7*t) end)   -- maneuvering, cold
          start();local from=#sent;hostTick(160)  -- 8 s
          local m=moves(from)
          r={countFor(m,1001),countFor(m,1002),countFor(m,1003),countFor(m,1004),countFor(m,1005),countFor(m,1006)}
        ''')
        still, cold_straight, hot_straight, hot_turn, hot_maneuver, cold_maneuver = [l.eval(f'r[{i}]') for i in range(1, 7)]
        self.assertLessEqual(still, 3)            # 4 s keepalive
        self.assertLessEqual(cold_straight, 3)    # dead reckoning holds a straight line
        self.assertLessEqual(hot_straight, 9)     # 1 s keepalive
        self.assertLessEqual(hot_turn, 12)        # constant turn rate is part of the shared model
        self.assertGreaterEqual(hot_maneuver, 24)  # >= 3 Hz while maneuvering near a player
        self.assertLessEqual(cold_maneuver, 17)   # at most 2 Hz far away

    def test_without_new_packets_clients_stay_within_the_tier_threshold(self):
        # The lockstep-like property: after every tick, the shared prediction of the last sent
        # sample (exactly as clients receive it) is within the tier threshold of the truth.
        l = self.host()
        l.execute('''
          addNpc(1005,-900,500,300,function(t) return 1.2*math.sin(1.7*t) end)
          addNpc(1006,90000,0,2000,function(t) return .4*math.sin(.9*t) end)
          start()
          local seen=#sent;last={};worst={[1005]=0,[1006]=0}
          for k=1,200 do
           hostTick(1)
           for _,f in ipairs(moves(seen)) do
            local id=tonumber(f[5]);local pose,vel={},{}
            for i=1,6 do pose[i]=tonumber(f[8+i]);vel[i]=tonumber(f[14+i]) end
            last[id]={time=tonumber(f[8]),pose=pose,vel=vel}
           end
           seen=#sent
           for object,n in pairs(npcs) do
            local s=last[U.identities[tostring(object)].id]
            if s then
             local q=M.drPredict(s.pose,s.vel,now-s.time);local p=positions[object]
             local e=math.sqrt((q[1]-p.x)^2+(q[2]-p.y)^2+(q[3]-p.z)^2)
             worst[object]=math.max(worst[object],e)
            end
           end
          end
        ''')
        self.assertLess(l.eval('worst[1005]'), 6)     # hot threshold 3 m plus one tick
        self.assertLess(l.eval('worst[1006]'), 300)   # cold threshold 150 m, at most 2 Hz

    def test_client_focus_makes_a_far_npc_hot(self):
        l = self.host()
        l.execute('''
          addNpc(1006,90000,0,300,function(t) return 1.2*math.sin(1.7*t) end)
          start();local from=#sent;hostTick(80);cold=countFor(moves(from),1006)
          local id=U.identities['1006'].id
          from=#sent
          for k=1,8 do U.receive({'UFOCUS','1',client.lease,tostring(k),tostring(id),string.rep('b',32)},now);hostTick(10) end
          hot=countFor(moves(from),1006)
          -- Focus expires after 3 s without a refresh.
          hostTick(80);from=#sent;hostTick(80);later=countFor(moves(from),1006)
        ''')
        self.assertLessEqual(l.eval('cold'), 9)
        self.assertGreaterEqual(l.eval('hot'), 2 * l.eval('cold'))
        self.assertLessEqual(l.eval('later'), 9)

    def test_teleport_is_sent_next_tick_without_velocity(self):
        l = self.host()
        l.execute('''
          addNpc(1003,1000,1000,300);start();hostTick(10)
          local from=#sent;positions[1003].x=positions[1003].x+20000;now=now+.05;frames.onUpdate()
          local m=moves(from);assert(#m==1,#m)
          for i=15,17 do assert(tonumber(m[1][i])==0) end
          assert(tonumber(m[1][9])>20000)
        ''')

    def test_budget_sends_the_most_diverged_first(self):
        l = self.host('X4LAN_CONFIG.npc_motion_diagnostics=1')
        l.execute('''
          for id=2001,2200 do addNpc(id,(id-2100)*20,500,300,function(t) return 1.5*math.sin(2*t+id) end) end
          start();local before=#sent
          local worst=0
          for k=1,40 do local s=#sent;hostTick(1);worst=math.max(worst,#moves(s)) end
          assert(worst==32,worst)
          local found=false
          for _,v in ipairs(logs) do if v:find('role=host',1,true) and v:find('deferred=',1,true) and v:find('sent_hot=',1,true)
           and v:find('pred_err_p95=',1,true) and v:find('sectors=cluster_14_sector001_macro',1,true) then found=true end end
          assert(found)
        ''')

    def test_capture_enumerates_a_large_universe_in_a_few_frames(self):
        # Live: 512 objects and at most one faction per 20 Hz frame; captures every 25-30 s.
        l = self.host()
        l.execute('''
          for id=100001,120000 do ids[#ids+1]=id end
          addNpc(1003,1000,1000,300)
          U.receive({'UINTEREST','1',client.lease,'1','cluster_14_sector001_macro'},now)
          local frames=0
          while not U.sender do hostTick(1);frames=frames+1;assert(frames<20,'capture too slow') end
          assert(frames<=8,frames);assert(#U.sender.baseline.rows==1)
        ''')


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class ClientDeadReckoningTests(unittest.TestCase):
    make = adapter.UniverseAdapterTests.make

    def guest(self, setup=''):
        l = self.make('p2', setup=setup)
        l.execute(adapter.NPC_HELPERS)
        return l

    def test_slow_server_clock_highway_replica_moves_every_frame_without_jumps(self):
        l = self.guest('X4LAN_CONFIG.npc_motion_diagnostics=1')
        l.execute('''
          math.randomseed(9)
          tick(1);baseline(1,{testRow},true);tick(2)
          local object=U.replicas[1].object;local seq=1;local nextS=now;local t0=now
          local frozen,back,missed,maxDev,prev=0,0,0,0,nil
          for f=1,60*30 do
           now=now+1/60
           while nextS<=now do
            -- Server clock at 0.8 x wall; 3000 m/s per server second; arrival jitter 0-80 ms.
            local st=50+.8*(nextS-t0);seq=seq+1
            local arrival=nextS+.02+math.random()*.08
            if arrival<=now then U.receive(umove(1,seq,st,1500+3000*(st-50),3000),now);nextS=nextS+.25 else break end
           end
           local before=#writes+1;frames.onUpdate()
           if f>300 then
            if writesFor(object,before)~=1 then missed=missed+1 end
            local step=positions[object].x-prev
            if step<1 then frozen=frozen+1 end
            maxDev=math.max(maxDev,math.abs(step-3000*.8/60))
           end
           prev=positions[object].x
          end
          assert(frozen==0 and missed==0,frozen..' '..missed)
          assert(maxDev<10,maxDev)
          jumps=0;for _,v in ipairs(logs) do local j=v:match(' jumps=(%d+)');if j then jumps=jumps+tonumber(j) end end
          rate=nil;for _,v in ipairs(logs) do rate=v:match('clock_rate=([%d.]+)') or rate end
        ''')
        self.assertEqual(l.eval('jumps'), 0)
        self.assertAlmostEqual(float(l.eval('rate')), 0.8, delta=0.03)

    def test_stale_baseline_pauses_only_when_motion_stops(self):
        l = self.guest()
        l.execute('''
          tick(1);baseline(1,{testRow},true);tick(1)
          local seq=1;local x=1500
          for k=1,400 do  -- 20 s with live motion, no new baseline
           if k%4==0 then seq=seq+1;x=x+20;U.receive(umove(1,seq,now,x,100),now) end
           tick(1)
          end
          for _,v in ipairs(logs) do assert(not v:find('baseline_stale',1,true)) end
          local n=#writes;tick(60);assert(#writes>n)       -- still rendering
          tick(100)                                         -- motion silent for 8 s
          local found=false;for _,v in ipairs(logs) do if v:find('FEATURE npc_replicas DISABLED reason=baseline_stale',1,true) then found=true end end
          assert(found)
          n=#writes;tick(20);assert(#writes==n)
          seq=seq+1;U.receive(umove(1,seq,now,x,100),now);tick(2)
          local resumed=false;for _,v in ipairs(logs) do resumed=resumed or v:find('FEATURE_RESUMED npc_replicas',1,true) end
          assert(resumed)
        ''')

    def test_discontinuity_snaps_once_and_is_logged(self):
        l = self.guest('X4LAN_CONFIG.npc_motion_diagnostics=1')
        l.execute('''
          tick(1);baseline(1,{testRow},true);tick(2)
          local seq=1
          for i=1,10 do seq=seq+1;U.receive(umove(1,seq,now,1500+i*15,300),now);tick(1) end
          seq=seq+1;U.receive(umove(1,seq,now,60000,300),now);tick(1)
          assert(math.abs(positions[U.replicas[1].object].x-60000)<50)
          tick(40)
          snap=false;for _,v in ipairs(logs) do if v:find('NPC_SNAP id=1 ',1,true) and v:find('reason=diverged',1,true) then snap=true end end
          row=nil;for _,v in ipairs(logs) do if v:find('snaps=1',1,true) then row=v end end
        ''')
        self.assertTrue(l.eval('snap'))
        row = l.eval('row')
        self.assertIsNotNone(row)
        for field in ('err_p50=', 'err_p95=', 'snaps_s=', 'gap_err=', 'clock_rate=', 'late_p95='):
            self.assertIn(field, row)

    def test_zone_frame_slip_of_a_replica_is_rewritten(self):
        l = self.guest('X4LAN_CONFIG.npc_motion_diagnostics=1')
        l.execute('''
          tick(1);baseline(1,{testRow},true);tick(2)
          local object=U.replicas[1].object
          local set=api.SetObjectSectorPos;local slipped=false
          -- Engine double: one write lands 20 km off (wrong zone frame); the next lands exactly.
          api.SetObjectSectorPos=function(id,sector,p)
           set(id,sector,p)
           if id==object and not slipped and p.x>1600 then slipped=true;positions[id].x=positions[id].x+20000 end
          end
          local seq=1
          for i=1,20 do seq=seq+1;U.receive(umove(1,seq,now,1500+i*10,200),now);tick(1) end
          assert(slipped and positions[object].x<5000,positions[object].x)
          local found=false;for _,v in ipairs(logs) do if v:find('NPC_FRAME_SLIP id=1',1,true) then found=true end end
          assert(found)
        ''')

    def test_targeted_replica_is_announced_as_focus(self):
        l = self.guest('target=0;api.GetPlayerTargetID=function() return target end')
        l.execute('''
          tick(1);baseline(1,{testRow},true);tick(2)
          target=U.replicas[1].object;tick(10)
          local focus={};for _,p in ipairs(sent) do if p:find('^UFOCUS|') then focus[#focus+1]=p end end
          assert(#focus==1 and focus[1]:find('|1$'),focus[1])
          tick(30);local n=0;for _,p in ipairs(sent) do if p:find('^UFOCUS|') then n=n+1 end end
          assert(n==2 or n==3,n)  -- refreshed every second
          target=0;tick(10)
          local last;for _,p in ipairs(sent) do if p:find('^UFOCUS|') then last=p end end
          assert(last:find('|0$'),last)
        ''')


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class NpcSceneTests(unittest.TestCase):
    """Server adapter -> Python relay -> client adapter, with a server clock at 0.8 x wall."""
    make = adapter.UniverseAdapterTests.make

    def test_scene_fidelity_and_bandwidth(self):
        from x4lan.universe import UniverseSession, Baseline
        from test_host import TOKEN, P1, P2
        rng = random.Random(11)
        host = self.make('p1')
        host.execute(HOST)
        host.execute('''
          for i=0,19 do addNpc(3000+i,rnd(-4000,4000),rnd(-4000,4000),300,function(t) return 1.1*math.sin(1.3*t+i) end) end
          for i=0,19 do addNpc(3100+i,rnd(-20000,20000),rnd(8000,20000),rnd(100,400),function(t) return .2*math.sin(.4*t+i) end) end
          for i=0,9 do addNpc(3200+i,rnd(-60000,60000),rnd(30000,60000),4000,0) end
          for i=0,19 do addNpc(3300+i,rnd(-30000,30000),rnd(-30000,30000),0) end
        '''.replace('rnd', 'math.random'))
        guest = self.make('p2', setup='X4LAN_CONFIG.npc_motion_diagnostics=1')
        relay = UniverseSession(TOKEN, 'world1-test', 'test')

        def join(slot, addr, nonce):
            packet = f'HELLO|1|{TOKEN}|world1-test|{SECTOR}|test|{slot}|{nonce}'.encode()
            return relay.receive(packet, addr, 0)[0].decode().split('|')[3]
        ha, gb = join('p1', P1, '1' * 32), join('p2', P2, '2' * 32)
        relay.receive(f'UINTEREST|1|{gb}|1|{SECTOR}'.encode(), P2, .1)
        host.execute("U.receive({'UINTEREST','1',client.lease,'1','cluster_14_sector001_macro'},now)")
        H, G = host.globals(), guest.globals()
        host.execute('history={}')
        wall, next_host, next_relay, frame = 0.0, 0.0, 0.0, 0
        seen, delivered, queue, errors, umoves = 0, False, [], [], 0
        hot_ids = set()
        while wall < 14:
            wall = round(wall + .001, 3)
            if wall >= next_host:
                next_host += .0625                       # server clock: 0.05 per frame at 16 fps
                host.execute('hostTick(1);local h={};for id,p in pairs(positions) do if npcs[id] then h[id]={p.x,p.y,p.z} end end;history[#history+1]={t=now,p=h}')
                sent = H.sent
                while seen < len(sent):
                    seen += 1
                    if sent[seen].startswith('UMOVE|'):
                        relay.receive(sent[seen].replace('a' * 32, ha).encode(), P1, wall)
                if H.U.sender and not H.U.sender.done:
                    b = H.U.sender.baseline
                    rows = [b.rows[i] for i in range(1, len(b.rows) + 1)]
                    H.U.sender.done = True
                    rb = Baseline.create(ha, b.revision, SECTOR, rows)
                    for i in range(len(rows) + 2):
                        relay.receive(rb.packet(ha, i), P1, wall)
                    if not delivered:
                        delivered = True
                        G.rows_in = guest.table_from(rows)
                        G.rev_in = b.revision
                        guest.execute('tick(1);local r={};for i=1,#rows_in do r[i]=rows_in[i] end;baseline(rev_in,r,true)')
            if wall >= next_relay:
                next_relay = wall + .05 + rng.random() * .005
                for addr, p in relay.snapshots(wall):
                    if addr == P2 and p.startswith(b'UMOVE'):
                        queue.append((wall + .02 + rng.random() * .03, p.decode()))
            if delivered and wall * 60 >= frame:
                frame += 1
                G.now = 10 + wall
                keep = []
                for at, p in queue:
                    if at <= wall:
                        f = p.split('|')
                        f[2] = f[3] = 'a' * 32
                        G.pkt = '|'.join(f)
                        guest.execute('U.receive(P.split(pkt,"|"),now)')
                        if wall > 4:
                            umoves += 1
                    else:
                        keep.append((at, p))
                queue = keep
                guest.execute('frames.onUpdate()')
                if wall > 4:
                    errors.extend(self.errors(host, guest))
        # The guest's render time is the server's clock: compare with the truth at that time.
        errors.sort()
        p95 = errors[int(len(errors) * .95)]
        rate = umoves / 10
        jumps = sum(int(x) for x in guest.eval('''(function() local out={}
            for _,v in ipairs(logs) do local j=v:match(' jumps=(%d+)');if j then out[#out+1]=j end end
            return table.concat(out,',') end)()''').split(',') if x)
        self.assertEqual(jumps, 0)
        self.assertLess(p95, 30, p95)
        self.assertLess(rate, 200, rate)   # 70 NPCs; the old scheme sent 640/s regardless

    @staticmethod
    def errors(host, guest):
        T = guest.eval('U.renderT')
        if T is None:
            return []
        # Truth at server time T, linearly interpolated from the server's per-frame history.
        data = guest.globals()
        host.globals().T = T
        pairs = host.eval('''(function()
          local h=history;local a,b
          for i=2,#h do if h[i].t>=T then a,b=h[i-1],h[i];break end end
          if not a then return '' end
          local k=(T-a.t)/(b.t-a.t);local out={}
          for id,p in pairs(a.p) do local q=b.p[id]
           if q then out[#out+1]=U.identities[tostring(id)].id..':'..(p[1]+(q[1]-p[1])*k)..':'..(p[2]+(q[2]-p[2])*k)..':'..(p[3]+(q[3]-p[3])*k) end
          end
          return table.concat(out,';') end)()''')
        result = []
        for item in filter(None, pairs.split(';')):
            nid, x, y, z = item.split(':')
            r = data.U.replicas[int(nid)]
            if r is None or r.object is None:
                continue
            p = data.positions[r.object]
            if p is None:
                continue
            result.append(math.sqrt((p.x - float(x)) ** 2 + (p.y - float(y)) ** 2 + (p.z - float(z)) ** 2))
        return result


if __name__ == '__main__':
    unittest.main()
