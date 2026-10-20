-- Unshipped native original-object and equipment/lifetime observations.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not cfg.seed_binding or cfg.test_profile~='45841' then return end
local U=A.universe;local I=A.identity;local C=require('ffi').C
local frame=U.frame;local nextRead=0;local seen={};local expectedCodes={};local lastRevision
local function log(v) DebugError('[X4LAN] SEED_OBJECT_PROBE '..v) end
function U.frame(now)
 frame(now)
 if not U.ready or A.paused() or now<nextRead then return end
 nextRead=now+2
 if cfg.slot=='p2' and (not U.latest or now-U.acceptedAt>=15) then return end
 local revision=cfg.slot=='p1' and U.revision or U.latest and U.latest.revision
 if not revision then return end
 local list={}
 if cfg.slot=='p1' then
  for _,entry in ipairs(U.publish) do list[#list+1]={id=entry.id,object=entry.object} end
 else
  for id,r in pairs(U.replicas) do if r.entity.kind=='ship' then list[#list+1]={id=id,object=r.object,r=r} end end
 end
 local count,live,matching,maxError=0,0,0,0
 for _,entry in ipairs(list) do
  count=count+1
  local object=entry.object
  if object and C.IsComponentOperational(object) then
   live=live+1
   local code=I.nativeCode(object);local macro,owner=GetComponentData(A.id(object),'macro','owner')
   local p=C.GetObjectPositionInSector(object);local wanted=entry.r and entry.r.entity.pose
   if wanted then maxError=math.max(maxError,math.abs(p.x-wanted[1]),math.abs(p.y-wanted[2]),math.abs(p.z-wanted[3])) end
   local identity=I.rows['N'..entry.id]
   if identity then expectedCodes[entry.id]=identity.code end
   local match=cfg.slot=='p1' or code==expectedCodes[entry.id] and entry.r.seedOriginal==true
   if match then matching=matching+1 end
   local old=seen[entry.id]
   log(string.format('object slot=%s revision=%d id=%d handle=%s code=%s macro=%s owner=%s original=%s sameHandle=%s pose=%.9g,%.9g,%.9g',
    cfg.slot,revision,entry.id,tostring(object):gsub('ULL$',''),code,macro,owner,tostring(match),
    tostring(not old or old==object),p.x,p.y,p.z))
   seen[entry.id]=object
   local stored,docked=GetComponentData(A.id(object),'isinternallystored','isdocked')
   log(string.format('attachment slot=%s revision=%d id=%d stored=%s docked=%s',cfg.slot,revision,entry.id,tostring(stored),tostring(docked)))
   if revision~=lastRevision then AddUITriggeredEvent('X4LAN_SeedObjectsProbe','Read',
    {object=A.id(object),id=entry.id,revision=revision,slot=cfg.slot}) end
  end
 end
 local retained=0
 for id,object in pairs(seen) do
  if cfg.slot=='p2' and not U.replicas[id] and C.IsComponentOperational(object) then retained=retained+1 end
 end
 log(string.format('summary slot=%s revision=%d count=%d live=%d matching=%d retained=%d maxError=%.6f failed=%s',
  cfg.slot,revision,count,live,matching,retained,maxError,tostring(U.failed)))
 lastRevision=revision
end
