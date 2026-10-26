-- Mirror capture driver and native cross-check (ecs-core; native/engine/mirror.h).
-- Isolated development installations only (same gate as fixed_tick_probe.lua, which must load
-- first: it owns the probe and publishes it as X4LAN_FIXEDTICK_OWNER).
-- Settings (ui/config.lua): X4LAN_MIRROR_PATH (absolute capture file; required, must not exist),
-- X4LAN_MIRROR_INTERVAL (frames between captures, default 1), X4LAN_MIRROR_FRAMES (default 10),
-- X4LAN_MIRROR_FLAGS (1 = record every component, 2 = skip the nav cache; default 0),
-- X4LAN_MIRROR_ATTENTION (skip sector subtrees below this attention, default 0),
-- X4LAN_MIRROR_DELAY (seconds after the probe armed, default 20), X4LAN_MIRROR_CHECKS (samples per
-- captured frame, default 16).
-- In the UI slot of every frame the probe captured, logs for sampled objects the engine's own
-- GetPositionalOffset(id, 0) and GetObjectPositionInSector(id) as
--   [X4LAN] MIRRORCHECK frame=<n> uid=<id> off=x,y,z sec=x,y,z
-- (%.9g round-trips every float); `x4mp-capdump check <capture> <debug log>` compares them with the
-- capture bit for bit. Nothing is written to the game.
if not X4LAN_CONFIG or X4LAN_CONFIG.test_profile~='45841' then return end
if not X4LAN_MIRROR_PATH or X4LAN_MIRROR_PROBE then return end
X4LAN_MIRROR_PROBE=true
local ffi=require('ffi')
local C=ffi.C
local function typedef(name,def) if not pcall(ffi.typeof,name) then ffi.cdef(def) end end
typedef('UniverseID','typedef uint64_t UniverseID;')
typedef('UIPosRot','typedef struct { float x,y,z,yaw,pitch,roll; } UIPosRot;')
pcall(ffi.cdef,[[UIPosRot GetPositionalOffset(UniverseID positionalid, UniverseID spaceid);]])
pcall(ffi.cdef,[[UIPosRot GetObjectPositionInSector(UniverseID objectid);]])
pcall(ffi.cdef,[[bool IsValidComponent(UniverseID componentid);]])
local interval=X4LAN_MIRROR_INTERVAL or 1
local frames=X4LAN_MIRROR_FRAMES or 10
local flags=X4LAN_MIRROR_FLAGS or 0
local attention=X4LAN_MIRROR_ATTENTION or 0
local delay=X4LAN_MIRROR_DELAY or 20
local checks=X4LAN_MIRROR_CHECKS or 16
local startAt,armed,done
local lastChecked=-1
local checked=0
local function log(s) DebugError('[X4LAN] '..s) end
local function f9(v) return string.format('%.9g',v) end
SetScript('onUpdate',function()
 if done then return end
 local owner=X4LAN_FIXEDTICK_OWNER
 if not owner then return end
 local now=GetCurRealTime()
 if not armed then
  if not startAt then startAt=now+delay;return end
  if now<startAt then return end
  local ok,why=owner:mirror(X4LAN_MIRROR_PATH,interval,frames,flags,attention)
  if not ok then
   log('FEATURE mirror DISABLED reason='..tostring(why));done=true;return
  end
  armed=true
  local s=owner:mirror_stats()
  log(string.format('MIRROR armed path=%s interval=%d frames=%d flags=%d attention=%d layout=%s',
   X4LAN_MIRROR_PATH,interval,frames,flags,attention,tostring(s.layout)))
  return
 end
 local s=owner:mirror_stats()
 -- The probe captured in slot 5 of this very frame when its frame counter equals last_frame.
 if s.frames_written>0 and s.last_frame~=lastChecked and s.last_frame==s.probe_frame then
  lastChecked=s.last_frame
  local uids=owner:mirror_sample()
  for i=1,math.min(#uids,checks) do
   local id=uids[i]
   if C.IsValidComponent(id) then
    local off=C.GetPositionalOffset(id,0)
    local sec=C.GetObjectPositionInSector(id)
    log(string.format('MIRRORCHECK frame=%.0f uid=%.0f off=%s,%s,%s sec=%s,%s,%s',s.last_frame,id,
     f9(off.x),f9(off.y),f9(off.z),f9(sec.x),f9(sec.y),f9(sec.z)))
    checked=checked+1
   end
  end
 end
 if s.armed==0 then
  done=true
  log(string.format('MIRROR done frames=%d dropped=%d partial=%d rows=%d roots=%d factions=%d bytes=%d walkErrors=%d meanMs=%.3f maxMs=%.3f checks=%d regionQueries=%d cacheFull=%d visited=%d walkMs=%.3f serializeMs=%.3f reason=%s',
   s.frames_written,s.frames_dropped,s.partial_frames,s.rows,s.roots,s.factions,s.bytes,s.walk_errors,
   s.mean_ms,s.max_ms,checked,s.region_queries or 0,s.region_cache_full or 0,s.visited or 0,
   s.walk_ms or 0,s.serialize_ms or 0,tostring(s.reason or 'complete')))
 end
end)
