-- Unshipped two-client session driver for player factions (install as ui/x4lan_pf_session.lua in
-- lane session runs only; never in packages). After PLAYER_FACTIONS READY it logs, every 10 s, what
-- this client sees: own slot, the other client's pool faction (name, diplomacy list entry, UI relation)
-- and owner/pilot of both shared co-op ships. It drives the relation intents of the acceptance:
--   client 2 at +40 s: setRelation('p1','neutral')   (request through the authority)
--   client 1 at +80 s: setRelation('p2','hostile')   (authority-local)
--   client 1 at +120 s: setRelation('p2','allied')
-- and logs PFSESSION DONE at +160 s. Log prefix: [X4LAN] PFSESSION.
local cfg=X4LAN_CONFIG
if not cfg or cfg.player_factions~=true then return end
local ffi=require('ffi')
local function log(v) DebugError('[X4LAN] PFSESSION '..v) end
local other=cfg.slot=='p1' and 'p2' or 'p1'
local steps=cfg.slot=='p1' and {{80,'hostile'},{120,'allied'}} or {{40,'neutral'}}
local nextLog,done,stepIndex=0,false,1
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
end
SetScript('onUpdate',function()
 local ok,err=pcall(function()
  local P=X4LAN_PLAYERS
  local st=P and P.state
  if done or not st or not st.ready or st.failed then return end
  local now=GetCurRealTime();local t=now-st.readyAt
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
