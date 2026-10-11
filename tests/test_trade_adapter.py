import unittest
from pathlib import Path
from lupa.luajit21 import LuaRuntime
ROOT=Path(__file__).resolve().parents[1]/'extension/x4_lan_lab/ui'
CATALOG='M,10000000,10000000,100000000,100000000;'+';'.join('W,%s,%d,0,0,300,400,1200,1400,2000,1700,1900,2000'%(w,v) for w,v in [('energycells',1),('foodrations',1),('medicalsupplies',2),('hullparts',12),('refinedmetals',14),('siliconwafers',18)])
class TradeAdapterTests(unittest.TestCase):
 def setUp(self):
  self.lua=LuaRuntime(unpack_returned_tuples=True)
  self.lua.execute('''
X4LAN_CONFIG={enabled=true,trading=true,slot='p1'}
now=100;events={};sent={};callbacks={};originalCalls=0;closed=0
function GetCurRealTime() return now end
function DebugError(v) end
function AddUITriggeredEvent(a,b,c) events[#events+1]={a,b,c} end
function RegisterEvent(a,b) callbacks[a]=b end
function ConvertIDTo64Bit(v) return v end
function ConvertStringToLuaID(v) return v end
function GetTradeList() return {{ware='energycells',ismissionoffer=false},{ware='energycells',ismissionoffer=true,amount=2000,isbuyoffer=true}} end
X4LAN_BUBBLE_API={state={active=true,ready=true,ship='10',client={lease=string.rep('a',32)},transport={send=function(self,v) sent[#sent+1]=v end}},nonce=function() return string.rep('c',32) end,paused=function() return false end}
Menus={{name='MapMenu',buttonConfirmTrade=function() originalCalls=originalCalls+1 end,closeContextMenu=function() closed=closed+1 end}}
''')
  self.lua.execute((ROOT/'trade_protocol.lua').read_text());self.lua.execute((ROOT/'trade_game.lua').read_text())
  self.lua.globals().catalog=CATALOG
  self.lua.execute('''
A=X4LAN_BUBBLE_API;T=A.trade;S=T.state;P=X4LAN_TRADE_PROTOCOL
callbacks['X4LAN_Trade.Market1'](nil,'101');callbacks['X4LAN_Trade.Ready']()
callbacks['X4LAN_Trade.LocalDock'](nil,'m1')
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'1','1','2',catalog:sub(1,400)},now)
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'1','2','2',catalog:sub(401)},now)
''')
 def test_native_basket_sign_price_confirmation_and_pending_guard(self):
  self.lua.execute('''
local menu=Menus[1]
menu.contextMenuData={component='101',currentShip='10',orders={offer=-13}}
menu.getTradeOfferByID=function() return {ware='energycells',price=14} end
menu.buttonConfirmTrade()
assert(originalCalls==0 and closed==1 and S.pending.lines=='energycells,13,1400')
T.frame(now)
assert(sent[#sent]:match('ORDER|1|') and sent[#sent]:match('energycells,13,1400$'))
local request=S.pending.request
menu.buttonConfirmTrade();assert(S.pending.request==request and closed==1)
''')
 def test_unshared_ship_or_market_never_calls_native_mutation(self):
  self.lua.execute('''
local menu=Menus[1]
menu.contextMenuData={component='999',currentShip='10',orders={}}
menu.buttonConfirmTrade();assert(originalCalls==0 and not S.pending)
menu.contextMenuData.component='101';menu.contextMenuData.currentShip='11'
menu.buttonConfirmTrade();assert(originalCalls==0 and not S.pending)
''')
 def test_host_deduplicates_before_engine_and_replies_after_completion(self):
  self.lua.execute('''
local request=string.rep('d',32)
local packet={'ORDER_EXEC','1',A.state.client.lease,request,'p2','m1','energycells,10,1400'}
T.receive(A.state.client,packet,now);T.receive(A.state.client,packet,now)
local count=0;for _,e in ipairs(events) do if e[2]=='Order' then count=count+1 end end
assert(count==1 and S.executed[request]=='PENDING')
callbacks['X4LAN_Trade.Result'](nil,request..'|OK')
T.receive(A.state.client,packet,now)
assert(sent[#sent]=='ORDER_DONE|1|'..A.state.client.lease..'|'..request..'|OK')
''')
 def test_atomic_fragments_and_guest_money(self):
  self.lua.execute('''
X4LAN_CONFIG.slot='p2'
local before=#events
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'2','2','2',catalog:sub(401)},now)
assert(#events==before)
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'2','1','2',catalog:sub(1,400)},now)
assert(events[#events][2]=='Apply' and events[#events][3].money==10000000)
assert(P.snapshot(catalog..';')==nil)
assert(P.lines('energycells,10,1;energycells,1,2')==nil)
''')
 def test_host_epoch_change_locks_pending_transaction(self):
  self.lua.execute('''
assert(T.submit('m1','energycells,-10,1200'))
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('b',32),'3','1','2',catalog:sub(1,400)},now)
assert(S.locked)
''')
 def test_dock_metadata_locks_only_remote_proxy(self):
  self.lua.execute('''
T.receive(A.state.client,{'DOCKSTATE','1',A.state.client.lease,string.rep('b',32),'3','m2'},now)
assert(A.state.remoteDockLock and events[#events][2]=='RemoteDock' and events[#events][3]==2)
T.receive(A.state.client,{'DOCKSTATE','1',A.state.client.lease,string.rep('b',32),'2','none'},now)
assert(A.state.remoteDockLock)
T.receive(A.state.client,{'DOCKSTATE','1',A.state.client.lease,string.rep('b',32),'4','none'},now)
assert(not A.state.remoteDockLock)
''')

 def test_shared_market_filters_only_its_offers(self):
  self.lua.execute("assert(#GetTradeList('101')==1);assert(#GetTradeList('999')==2)")
 def test_fragment_assembly_memory_is_bounded(self):
  self.lua.execute("local a=P.assembler();for i=1000,1,-1 do a:add(i,1,2,'bad',1) end;local n=0;for _ in pairs(a.pending) do n=n+1 end;assert(n<=4)")

 def test_request_id_is_unique_per_client_lease_not_lua_random_seed(self):
  self.lua.execute("assert(T.submit('m1','energycells,1,1400'));local a=S.pending.request;assert(a==string.rep('a',24)..'00000001');S.pending=nil;A.state.client.lease=string.rep('b',32);assert(T.submit('m1','energycells,1,1400'));assert(S.pending.request~=a)")

 def test_early_snapshot_waits_for_engine_ready(self):
  self.lua.execute('''
X4LAN_CONFIG.slot='p2';S.ready=false;events={}
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'2','1','2',catalog:sub(1,400)},now)
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'2','2','2',catalog:sub(401)},now)
assert(#events==0)
callbacks['X4LAN_Trade.Ready']()
assert(#events==1 and events[1][2]=='Apply' and events[1][3].money==10000000)
''')
 def test_coop_trade_uses_taken_ship_epoch_and_shared_guest_balance(self):
  self.lua.execute('''
X4LAN_CONFIG.coop=true;X4LAN_CONFIG.slot='p2'
A.coop={ready=true,controlled=1,ships={'21','10'},pilots={'p2','0'},owners={'p2','p2'},epochs={4,1},docks={1,0}}
local menu=Menus[1];menu.contextMenuData={component='101',currentShip='21',orders={offer=-10}}
menu.getTradeOfferByID=function() return {ware='energycells',price=14} end
menu.buttonConfirmTrade();assert(S.pending.ship==1 and S.pending.epoch==4)
T.frame(now);assert(sent[#sent]:find('|1|4|m1|energycells,10,1400',1,true))
S.pending=nil
catalog=catalog:gsub('M,10000000,10000000','M,19986000,19986000')
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'2','1','2',catalog:sub(1,400)},now)
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'2','2','2',catalog:sub(401)},now)
assert(events[#events][2]=='Apply' and events[#events][3].money==19986000)
local n=#events
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'3','1','2',catalog:sub(1,400)},now)
T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),'3','2','2',catalog:sub(401)},now)
assert(#events==n) -- no repeated cargo writes when the authoritative contents did not change
A.coop.pilots[1]='0';assert(not T.submit('m1','energycells,10,1400'))
''')
 def test_coop_unchanged_economy_still_periodically_reconciles_local_drift(self):
  self.lua.execute('''
X4LAN_CONFIG.coop=true;X4LAN_CONFIG.slot='p2'
local function snapshot(rev)
 T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),tostring(rev),'1','2',catalog:sub(1,400)},now)
 T.receive(A.state.client,{'ECON','1',A.state.client.lease,string.rep('a',32),tostring(rev),'2','2',catalog:sub(401)},now)
end
snapshot(2);local n=#events
now=100.5;snapshot(3);assert(#events==n)
now=102.1;snapshot(4);assert(#events==n+1 and events[#events][2]=='Apply')
''')
