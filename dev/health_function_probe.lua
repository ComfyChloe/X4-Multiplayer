-- Opt-in, dev-only observer for victim-pilot health (PR #36; dev/HEALTH-FUNCTION-PROBE.txt).
-- Never packaged: a native harness copies it into ui/ after health_game.lua, together with
-- dev/health_function_probe.xml in md/. Host p1 only, with pilot_health and health_probe set.
-- Logs every native write request of health_game.lua in order (op sequence) and every HGEN
-- change and HEALTH frame with its outcome. The synthetic peer encodes its phase in the HEALTH
-- sequence (seq = phase * 1000 + n). In phase 3 every tenth accepted frame asks MD to drop the
-- proxy hull to 1.5% (a labelled host-side setter, not combat damage) to show the 2% floor and
-- the next pilot value. This observes; it is no gameplay acceptance and covers no guest-native code.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.health or not cfg or cfg.test_profile~='45841' or cfg.slot~='p1' or not cfg.health_probe then return end
local h=A.health
local op,gens=0,nil
local function log(v) DebugError('[X4LAN] HEALTH_PROBE '..v) end
local function phase(seq) return seq and math.floor(seq/1000) or -1 end

local add=AddUITriggeredEvent
AddUITriggeredEvent=function(screen,control,args)
 if screen=='X4LAN_Health' then
  op=op+1
  args=args or {}
  log(string.format('op=%d %s entity=%s gen=%s hull=%s shield=%s closed=%s phase=%d',op,control,
   tostring(args.entity),tostring(args.gen),tostring(args.hull),tostring(args.shield),tostring(args.closed),phase(h.last[2])))
 end
 return add(screen,control,args)
end

local receive=h.receive
function h.receive(f,now)
 if f[1]=='HGEN' then
  local key=table.concat({tostring(f[4]),tostring(f[5]),tostring(f[6])},'|')
  local r=receive(f,now)
  if key~=gens then gens=key;log('frame HGEN rev|g1|g2='..key..' valid2='..tostring(h.valid[2])..' closed2='..h.closed[2]) end
  return r
 end
 if f[1]~='HEALTH' then return receive(f,now) end
 local e,seq=tonumber(f[5]),tonumber(f[7])
 local before=e and h.last[e]
 local r=receive(f,now)
 local accepted=e~=nil and h.last[e]==seq and before~=seq
 log(string.format('frame HEALTH entity=%s gen=%s seq=%s hull=%s shield=%s accepted=%s phase=%d',
  tostring(f[5]),tostring(f[6]),tostring(f[7]),tostring(f[8]),tostring(f[9]),tostring(accepted),phase(seq)))
 if accepted and phase(seq)==3 and seq%10==0 then add('X4LAN_HealthProbe','Drop',{entity=e,hull=1.5}) end
 return r
end
log('READY slot='..cfg.slot)
