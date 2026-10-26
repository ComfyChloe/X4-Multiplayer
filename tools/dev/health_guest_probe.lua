-- Opt-in, dev-only GUEST observer for victim-pilot health (PR #36; tools/dev/HEALTH-GUEST-PROBE.txt).
-- Never packaged: a native harness copies it into ui/ after health_game.lua, together with
-- tools/dev/health_guest_probe.xml in md/. Guest p2 only, with pilot_health and health_probe set.
-- The synthetic host selects the phase through its WORLD revision (rev = phase * 1000 + n).
-- Logs the order of Authority/Base/Based/HACK/HEALTH, incoming HGEN/HBASE/WORLD, and acts only in
-- these phases: 3 writes a local hull of 50% on the own ship once (scripted damage); 5 suspends
-- health_game.lua's frame dispatch for 3 s while MD keeps running, then injects the old
-- Authority(own) with a fresh window; 6 injects the old HBASE once. Observation, not acceptance.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.health or not cfg or cfg.test_profile~='45841' or cfg.slot~='p2' or not cfg.health_probe then return end
local h,s=A.health,A.state
local op,done,suspendUntil,oldGen,oldBase=0,{},nil,nil,nil
local function log(v) DebugError('[X4LAN] HEALTH_GUEST_PROBE '..v) end
local function phase() local r=s.remoteRevision;return r and r>=0 and math.floor(r/1000) or -1 end

local add=AddUITriggeredEvent
AddUITriggeredEvent=function(screen,control,args)
 if screen=='X4LAN_Health' then
  op=op+1;args=args or {}
  log(string.format('op=%d %s entity=%s gen=%s own=%s ended=%s phase=%d',op,control,tostring(args.entity),
   tostring(args.gen),tostring(args.own),tostring(args.ended),phase()))
 end
 return add(screen,control,args)
end

-- Sends are derived from health_game.lua's own state, never by touching the transport (natively
-- a userdata without assignable fields): a new h.seq after a frame is a sent HEALTH, a moved
-- h.nextAck after Based or an HBASE repeat is a sent HACK.
local function sentHealth(before)
 local r,l=h.reading,h.live
 if h.seq~=before and l and r then
  op=op+1;log(string.format('op=%d sent HEALTH gen=%d seq=%d hull=%.3f shield=%.3f phase=%d',op,l.gen,h.seq,r.hull,r.shield,phase()))
 end
end
local function sentAck(before,gen)
 if h.nextAck~=before then op=op+1;log(string.format('op=%d sent HACK gen=%s phase=%d',op,tostring(gen),phase())) end
end
local base=h.base
function h.base(f,now) local before=h.nextAck;base(f,now);sentAck(before,f[5]) end

local based=h.based
function h.based(payload)
 op=op+1;log(string.format('op=%d Based %s phase=%d',op,tostring(payload),phase()))
 local before=h.nextAck;based(payload);sentAck(before,tostring(payload):match('|(%d+)$'))
end

local receive=h.receive
function h.receive(f,now)
 -- Phase 5 controlled loss: while health frames are suspended, HGEN/HBASE are dropped here, so
 -- the relay's own expiry cannot revoke before MD's 2 s keepalive watchdog fires.
 if suspendUntil and (f[1]=='HGEN' or f[1]=='HBASE') then
  if not done.dropLogged then done.dropLogged=true;log('dropping HGEN/HBASE during suspension') end
  return true
 end
 if f[1]=='HBASE' then
  if not oldBase then oldBase={};for i,v in ipairs(f) do oldBase[i]=v end end
  log(string.format('frame HBASE gen=%s hull=%s shield=%s valid=%s closed=%s',tostring(f[5]),tostring(f[6]),tostring(f[7]),tostring(h.valid[2]),h.closed[2]))
 elseif f[1]=='WORLD' and #f==22 then
  log(string.format('frame WORLD rev=%s ship2 hull=%s shield=%s own=%s',tostring(f[5]),tostring(f[9]),tostring(f[10]),tostring(h.live and h.live.entity==2)))
 end
 return receive(f,now)
end

local frame=h.frame
function h.frame(now)
 local p=phase()
 if p==3 and not done[3] and h.live then
  done[3]=true;add('X4LAN_HealthGuestProbe','LocalDamage',{entity=h.live.entity,hull=50})
 end
 if p==5 and not done[5] and h.live then
  done[5]=true;oldGen=h.live.gen;suspendUntil=now+3;log('suspend health frames gen='..oldGen)
 end
 if suspendUntil then
  if now<suspendUntil then return end
  suspendUntil=nil
  if h.mdStamp then
   add('X4LAN_Health','Authority',{entity=2,gen=oldGen,own=true,stamp=h.mdStamp,from=h.mdAge,deadline=h.mdAge+1})
   log('injected old Authority gen='..tostring(oldGen)..' own=true fresh window')
  end
 end
 if p==6 and not done[6] and oldBase then
  done[6]=true;log('inject old HBASE gen='..tostring(oldBase[5]));receive(oldBase,now)
 end
 local before=h.seq
 frame(now)
 sentHealth(before)
end
log('READY slot='..cfg.slot)
