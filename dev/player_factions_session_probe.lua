-- Unshipped two-client session driver for player factions (install as ui/x4lan_pf_session.lua in
-- lane session runs only; never in packages). After PLAYER_FACTIONS READY it logs, every 10 s, what
-- this client sees: own slot, the other client's pool faction (name, diplomacy list entry, UI relation)
-- and owner/pilot of both shared co-op ships. It drives the relation intents of the acceptance:
--   client 2 at +40 s: setRelation('p1','neutral')   (request through the authority)
--   client 1 at +80 s: setRelation('p2','hostile')   (authority-local)
--   client 1 at +120 s: setRelation('p2','allied')
-- and logs PFSESSION DONE at +160 s. Log prefix: [X4LAN] PFSESSION.
-- With md/x4lan_pf_session_probe.xml (dev/player_factions_session_probe.xml) it also:
--   sets this client's player name from X4LAN_CONFIG.pf_test_name (lane --client-lua) at start;
--   logs RADAR rows: the other client's co-op ship as this game sees it (known, radar visible,
--   owner, owner name, target-monitor header);
--   cargo/magnet: client 1 (authority) seeds 30 energy cells into shared ship 2 at +15 s; client 2
--   jettisons 10 at +25 s (native DropCargo -> CARGOREQ DROP) and runs the loot magnet +35..+55 s
--   (native pickup -> CARGOREQ PICKUP). Outcomes are the cargo adapter's CARGO RESULT lines.
local cfg=X4LAN_CONFIG
if not cfg or cfg.player_factions~=true then return end
local ffi=require('ffi');local C=ffi.C
pcall(ffi.cdef,[[bool DropCargo(UniverseID,const char*,uint32_t);void SetLootMagnetActive(bool);const char* GetPlayerName(void);]])
local function log(v) DebugError('[X4LAN] PFSESSION '..v) end
local other=cfg.slot=='p1' and 'p2' or 'p1'
local steps=cfg.slot=='p1' and {{80,'hostile'},{120,'allied'}} or {{40,'neutral'}}
local nextLog,done,stepIndex=0,false,1
local named,seeded,dropped,magnetOn,magnetOff
local function shipText(i,ship,pilot)
 if not ship or ship==0 then return i..':none' end
 local ok,owner,ownername=pcall(GetComponentData,X4LAN_BUBBLE_API.id(ship),'owner','ownername')
 return string.format('%d:%s/%s/pilot=%s',i,tostring(ok and owner),tostring(ok and ownername),tostring(pilot or '0'))
end
local function report(now,readyAt)
 local P=X4LAN_PLAYERS
 local f=P.faction(other)
 local listed='no'
 local ok,lib=pcall(GetLibrary,'factions')
 if ok and type(lib)=='table' then for _,e in ipairs(lib) do if e.id==f then listed='yes:'..tostring(e.name) end end end
 local okd,name,short=pcall(GetFactionData,f,'name','shortname')
 local okr,rel=pcall(GetUIRelation,f)
 local c=X4LAN_BUBBLE_API and X4LAN_BUBBLE_API.coop
 local ships={}
 for i=1,2 do ships[i]=shipText(i,c and c.ships[i],c and c.pilots[i]) end
 log(string.format('slot=%s t=%.0f other=%s faction=%s name=%s short=%s diplomacy_list=%s uirelation=%s ships=%s;%s',
  cfg.slot,now-readyAt,other,tostring(f),tostring(okd and name),tostring(okd and short),listed,tostring(okr and rel),ships[1],ships[2]))
 local otherShip=c and c.ships[other=='p1' and 1 or 2]
 if otherShip and otherShip~=0 then
  local id=X4LAN_BUBBLE_API.id(otherShip)
  local okc,known,radar,owner,ownername=pcall(GetComponentData,id,'isknown','isradarvisible','owner','ownername')
  local header='unavailable'
  local okt,details=pcall(GetTargetMonitorDetails,id,'',false)
  if okt and details and details.header and details.header.text then header=details.header.text end
  log(string.format('RADAR slot=%s other_ship=%s known=%s radar=%s owner=%s ownername=%s header=%s',cfg.slot,other=='p1' and 1 or 2,
   tostring(okc and known),tostring(okc and radar),tostring(okc and owner),tostring(okc and ownername),tostring(header)))
 end
 local okn,me=pcall(function() return ffi.string(C.GetPlayerName()) end)
 log(string.format('SELF slot=%s player_name=%s',cfg.slot,tostring(okn and me)))
 local client=X4LAN_BUBBLE_API and X4LAN_BUBBLE_API.state and X4LAN_BUBBLE_API.state.client
 if client then log(string.format('DRAIN slot=%s latest=%d max=%d',cfg.slot,client.drained or 0,client.drainedMax or 0)) end
end
local wrapped=false
SetScript('onUpdate',function()
 local ok,err=pcall(function()
  local A=X4LAN_BUBBLE_API
  if not wrapped and A and A.players and A.players.receive then
   -- Trace relation packets as this client receives them (diagnostic only).
   wrapped=true;local receive=A.players.receive
   A.players.receive=function(f,now)
    if f[1]=='PFREL' or f[1]=='PFREQ' then log('RX '..table.concat(f,'|',1,math.min(#f,8))) end
    return receive(f,now)
   end
  end
  if not named and type(cfg.pf_test_name)=='string' and A and A.state and A.state.loaded then
   named=true;AddUITriggeredEvent('X4LAN_PFSession','Name',cfg.pf_test_name)
   log('NAME_REQUEST slot='..cfg.slot..' name='..cfg.pf_test_name)
  end
  local P=X4LAN_PLAYERS
  local st=P and P.state
  if done or not st or not st.ready or st.failed then return end
  local now=GetCurRealTime();local t=now-st.readyAt
  if cfg.slot=='p1' and not seeded and t>=15 then seeded=true;AddUITriggeredEvent('X4LAN_PFSession','Seed') end
  if cfg.slot=='p2' and not dropped and t>=25 then
   dropped=true;local ship=C.GetPlayerControlledShipID()
   log('CARGO_DROP slot=p2 ship='..tostring(ship)..' native='..tostring(C.DropCargo(ship,'energycells',10)))
  end
  if cfg.slot=='p2' and not magnetOn and t>=35 then magnetOn=true;C.SetLootMagnetActive(true);log('MAGNET on slot=p2') end
  if cfg.slot=='p2' and magnetOn and not magnetOff and t>=55 then magnetOff=true;C.SetLootMagnetActive(false);log('MAGNET off slot=p2') end
  local step=steps[stepIndex]
  if step and t>=step[1] then
   local okr,how=P.setRelation(other,step[2])
   log(string.format('slot=%s ACTION setRelation(%s,%s) -> %s %s',cfg.slot,other,step[2],tostring(okr),tostring(how)))
   stepIndex=stepIndex+1
  end
  if now>=nextLog then nextLog=now+10;report(now,st.readyAt) end
  if t>=160 then report(now,st.readyAt);log('DONE slot='..cfg.slot);done=true end
 end)
 if not ok then log('ERROR '..tostring(err));done=true end
end)
log('armed slot='..tostring(cfg.slot))
