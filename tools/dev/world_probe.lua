-- Unshipped diagnostic wrapper, runs after the real production adapter.
local A=X4LAN_BUBBLE_API
if not A or not A.universe then return end
local ffi=require('ffi');local C=ffi.C;local U=A.universe
local frame=U.frame
local nextLog=0
local seen={}
function U.frame(now)
 frame(now)
 if now<nextLog or not U.ready then return end
 nextLog=now+2
 local count=0;local live=0;local maxError=0;local positions={};local current={}
 for id,r in pairs(U.replicas) do
  count=count+1
  if r.object and C.IsComponentOperational(r.object) then
   local key=tostring(r.object);seen[key]=r.object;current[key]=true
   live=live+1;local p=C.GetObjectPositionInSector(r.object)
   local wanted=r.target or r.entity.pose
   maxError=math.max(maxError,math.abs(p.x-wanted[1]),math.abs(p.y-wanted[2]),math.abs(p.z-wanted[3]))
   positions[#positions+1]=string.format('%d:%.1f,%.1f,%.1f',id,p.x,p.y,p.z)
  end
 end
 local retiredLive=0
 for key,object in pairs(seen) do
  if not current[key] and C.IsComponentOperational(object) then retiredLive=retiredLive+1 end
 end
 DebugError(string.format('[X4LAN] W01_PROBE failed=%s revision=%s replicas=%d live=%d retiredLive=%d maxError=%.3f localPlayer=%s positions=%s',
  tostring(U.failed),U.latest and U.latest.revision or 'none',count,live,retiredLive,maxError,
  tostring(C.IsComponentOperational(C.GetPlayerContainerID())),table.concat(positions,';')))
end
