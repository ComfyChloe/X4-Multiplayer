"""N-player client adapters under LuaJIT engine doubles (synthetic; no X4, no native evidence).

Covers the pure client (one transform track per remote lease, slot admission) and the
co-op chain of a dedicated four-player session seen from client p4: ship mapping, one
proxy per remote client, AUTH with one group per ship, XSHIP and per-remote spacesuits.
"""
from pathlib import Path
import unittest
from test_game_adapter import MOCK, LuaRuntime
from test_bubble_adapter import EXTRA

UI = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/ui'
A, B, C, OWN = 'a' * 32, 'b' * 32, 'c' * 32, 'e' * 32


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class ClientRemotesTests(unittest.TestCase):
    def runtime(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute((UI / 'client.lua').read_text())
        return lua

    def test_one_track_per_remote_lease_in_the_same_tick(self):
        lua = self.runtime()
        lua.execute('''
          local cfg={token=string.rep('a',32),build='test',bubble='bubble',sector='s',slot='p2',dedicated_session=true,max_players=4}
          client=X4LAN_CLIENT.new(cfg,{},string.rep('1',32))
          local own=string.rep('e',32)
          client:receive('WELCOME|1|'..string.rep('1',32)..'|'..own..'|20',0)
          for tick=1,3 do
            for i,lease in ipairs({string.rep('a',32),string.rep('b',32),string.rep('c',32)}) do
              client:receive('PEER|1|'..own..'|'..tick..'|'..lease..'|'..tick..'|'..(i*100+tick)..'|0|0|0|0|0',tick*.05)
            end
          end
          client:receive('PEER|1|'..own..'|3|'..string.rep('a',32)..'|9|999|0|0|0|0|0',.2) -- same tick, same remote
          client:receive('PEER|1|'..own..'|3|'..own..'|9|999|0|0|0|0|0',.2)                -- own lease never a remote
          local all=client:renderAll(.2);local n=0
          for lease,pose in pairs(all) do n=n+1;assert(pose and pose[1]<400) end
          assert(n==3 and client.remotes[string.rep('a',32)].seq==3)
          assert(client:render(.2)) -- mirrors the latest remote
          client:receive('EMPTY|1|'..own..'|4',.25)
          assert(next(client.remotes)==nil and client:render(.25)==nil)
          client:receive('PEER|1|'..own..'|4|'..string.rep('a',32)..'|10|1|0|0|0|0|0',.26) -- after EMPTY of tick 4
          assert(next(client.remotes)==nil)
        ''')

    def test_slot_admission_follows_max_players(self):
        lua = self.runtime()
        lua.execute('''
          local function make(slot,dedicated,players)
            local cfg={token=string.rep('a',32),build='test',bubble='bubble',sector='s',slot=slot,dedicated_session=dedicated,max_players=players}
            return pcall(X4LAN_CLIENT.new,cfg,{},string.rep('1',32))
          end
          assert(make('p1',false,nil) and make('p2',false,nil))
          assert(not make('p3',false,nil) and not make('p3',false,3))
          assert(make('p3',true,nil) and not make('p4',true,nil))      -- legacy dedicated: three slots
          assert(make('p4',true,4) and make('p8',true,8))
          assert(not make('p5',true,4) and not make('p9',true,8) and not make('p2',true,9))
        ''')


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class FourPlayerCoopTests(unittest.TestCase):
    def make(self, slot='p4'):
        l = LuaRuntime(unpack_returned_tuples=True)
        l.execute(MOCK + EXTRA)
        g = l.globals().X4LAN_CONFIG
        g.slot, g.coop, g.dedicated_session, g.max_players = slot, True, True, 4
        g.ship_macro, g.ship_loadout, g.ship_weapons = 'ship_test_macro', 'loadout', 6
        l.execute('''
          ConvertIDTo64Bit=tonumber
          api.GetContextByClass=function() return 22 end
          GetComponentData=function() return 'cluster_14_sector001_macro' end
          api.SetObjectSectorPos=function(object,sector,p)
            writes[#writes+1]={object=object,x=p.x}
            positions[object]={x=p.x,y=p.y,z=p.z,yaw=p.yaw*math.pi/180,pitch=p.pitch*math.pi/180,roll=p.roll*math.pi/180}
          end
          nextObject=80
          api.SpawnObjectAtPos2=function(macro,sector,p,owner) local n=nextObject;nextObject=n+1;positions[n]=p;return n end
        ''')
        for name in ('bubble_game.lua', 'motion.lua', 'coop_game.lua', 'life_game.lua'):
            l.execute((UI / name).read_text())
        l.execute('''
          events['X4LAN.Loaded']();now=10;frames.onUpdate()
          for _,n in ipairs({61,62,63}) do positions[n]={x=0,y=0,z=0,yaw=0,pitch=0,roll=0} end
          events['X4LAN_Bubble.PreparedOwn'](nil,61);events['X4LAN_Bubble.PreparedProxy'](nil,62)
          events['X4LAN_Bubble.PreparedExtra.1'](nil,63);events['X4LAN_Bubble.ShipsReady']()
          frames.onUpdate();events['X4LAN_Bubble.Ready']();frames.onUpdate();events['X4LAN_Coop.Ready']()
          function auth(rev,...)
            local f={'AUTH','1',client.lease,tostring(rev)}
            for _,v in ipairs({...}) do f[#f+1]=v end
            client:receiveExtension(f,now)
          end
          function nav2(source,entity,epoch,seq,x)
            local f={'NAV2','1',client.lease,source,tostring(entity),tostring(epoch),tostring(seq),'cluster_14_sector001_macro',
              tostring(x),'10000','0','0','0','0','0','0',string.format('%.6f',seq*.05),'0','0','0','0','0','0'}
            client:receiveExtension(f,now)
          end
        ''')
        return l

    def test_client_p4_owns_ship_three_and_renders_both_remote_clients(self):
        l = self.make()
        l.execute('''
          local s,c=X4LAN_BUBBLE_API.state,X4LAN_BUBBLE_API.coop
          local R=X4LAN_BUBBLE_API.roster
          assert(R.players==4 and R.ships==3 and R.own==3 and c.count==3)
          local p=nil;for _,r in ipairs(requests) do if r[2]=='PrepareShips' then p=r[3] end end
          assert(p.count==3 and p.ownIndex==3)
          -- own at index 3; the proxy and the extra fill 1 and 2 in order
          assert(c.ships[3]==61 and c.ships[1]==62 and c.ships[2]==63, 'ship mapping')
          auth(1,'p3','2','p3','p2','1','p2','p4','2','p4')
          assert(c.owners[3]=='p4' and c.owners[1]=='p3' and c.revision==1)
          auth(2,'p3','2','p3','p2','1','p2')            -- a two-ship AUTH is malformed here
          assert(c.revision==1)
          auth(3,'p3','2','p3','p2','1','p2','p5','2','p5') -- p5 is beyond max_players
          assert(c.revision==1)
          c.sectors['cluster_14_sector001_macro']=22
          for seq=1,3 do now=10+seq*.05;nav2(string.rep('c',32),1,2,seq,100+seq);nav2(string.rep('b',32),2,1,seq,200+seq) end
          now=10.6;frames.onUpdate()
          local moved={}
          for _,w in ipairs(writes) do moved[w.object]=true end
          assert(moved[62] and moved[63], 'both remote clients are rendered')
          assert(not moved[61], 'own ship is never moved by the network')
          -- own ship publishes as entity 3
          local found=false
          for _,pkt in ipairs(sent) do if pkt:find('^NAV2|1|'..client.lease..'|3|2|') then found=true end end
          assert(found, 'NAV2 for own ship 3')
        ''')

    def test_xship_reaches_md_and_server_publishes_extra_ships(self):
        l = self.make()
        l.execute('''
          local f={'XSHIP','1',client.lease,string.rep('a',32),'1','3','40.5','20','0','0'}
          client:receiveExtension(f,now)
          assert(countRequest('ApplyExtra')==1 and requests[#requests][3].ship==3 and requests[#requests][3].hull==40.5)
          client:receiveExtension(f,now);assert(countRequest('ApplyExtra')==1)          -- unchanged, throttled
          f[6]='4';client:receiveExtension(f,now);assert(countRequest('ApplyExtra')==1) -- beyond ship count
          f[6]='2';client:receiveExtension(f,now);assert(countRequest('ApplyExtra')==1) -- fixture rows use WORLD
        ''')
        server = self.make('p1')
        server.execute('''
          events['X4LAN_Bubble.XShip'](nil,'3|55|10|0|0')
          now=11;frames.onUpdate()
          local found=false
          for _,pkt in ipairs(sent) do if pkt:find('^XSHIP|1|'..client.lease..'|%d+|3|55|10|0|0$') then found=true end end
          assert(found)
        ''')

    def test_each_remote_client_gets_its_own_spacesuit(self):
        l = self.make()
        l.execute('''
          X4LAN_BUBBLE_API.coop.sectors['cluster_14_sector001_macro']=22
          for i,lease in ipairs({string.rep('b',32),string.rep('c',32)}) do
            client:receiveExtension({'BODY2','1',client.lease,lease,'1','suit','0',tostring(i),'cluster_14_sector001_macro',tostring(i*10),'0','0','0','0','0'},now)
          end
          now=10.1;frames.onUpdate()
          local life=X4LAN_BUBBLE_API.life;local suits={}
          for _,e in pairs(life.remotes) do assert(e.suit);suits[e.suit]=true end
          local n=0;for _ in pairs(suits) do n=n+1 end
          assert(n==2 and countRequest('RemoteSuit')==2)
        ''')


class NPlayerMdContractTests(unittest.TestCase):
    """Static MD contract only; the scripts still need a native X4 run."""
    MD = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab/md'

    def test_bubble_prepares_ordered_ships_and_counts_shots_for_every_ship(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(self.MD / 'x4lan_bubble.xml')
        names = {c.get('name') for c in root.iter('cue')}
        for name in ['Shots%d' % k for k in range(1, 8)] + ['ApplyExtra', 'PrepareShips']:
            self.assertIn(name, names)
        text = (self.MD / 'x4lan_bubble.xml').read_text()
        self.assertIn('md.X4LAN_Bubble.PrepareShips.$ordered.clone', text)
        self.assertIn("'X4LAN_Bubble.PreparedExtra.%s'.[$k]", text)
        self.assertIn("'X4LAN_Bubble.XShip'", text)
        for k in range(3, 8):
            self.assertIn('groupname="$weapons%d"' % k, text)
        self.assertNotIn('[false,false]', text)          # per-ship lists follow the ship count

    def test_coop_lists_follow_the_ship_count_and_dock_window_spares_extra_ships(self):
        text = (self.MD / 'x4lan_coop.xml').read_text()
        self.assertNotIn('[[0,0],[0,0]]', text)
        self.assertIn('<do_all exact="$count" counter="$i">', text)
        self.assertIn('<do_if value="$v.$ship gt 2"><set_value name="$dockWindow" exact="true"/></do_if>', text)
        life = (self.MD / 'x4lan_life.xml').read_text()
        self.assertIn('<do_all exact="$arena.$ships.count" counter="$i">', life)
        cargo = (self.MD / 'x4lan_cargo.xml').read_text()
        self.assertIn('object="$ships.{2}"', cargo)          # native cargo stays on ships 1/2


if __name__ == '__main__':
    unittest.main()
