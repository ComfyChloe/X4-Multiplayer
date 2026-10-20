-- Unshipped fresh-world experiment: native carrier movement and stored children.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.universe or cfg.test_profile~='45841' or not cfg.hangar_pose_probe then return end
local ffi=require('ffi');local C=ffi.C;local U=A.universe
ffi.cdef[[uint32_t GetNumDockedShips(UniverseID,const char*);
uint32_t GetDockedShips(UniverseID*,uint32_t,UniverseID,const char*);]]
local function log(v) DebugError('[X4LAN] HANGAR_POSE '..v) end
local state={};local stage=0;local due=0
local function position(object)
 local p=C.GetObjectPositionInSector(object)
 return {p.x,p.y,p.z,p.yaw,p.pitch,p.roll}
end
local function value(p,dx)
 return ffi.new('UIPosRot',{x=p[1]+dx,y=p[2],z=p[3],yaw=p[4]*180/math.pi,pitch=p[5]*180/math.pi,roll=p[6]*180/math.pi})
end
local function read(phase)
 local parent=position(state.carrier)
 log(string.format('parent phase=%s handle=%s pose=%.9g,%.9g,%.9g',phase,tostring(state.carrier):gsub('ULL$',''),parent[1],parent[2],parent[3]))
 for index,child in ipairs(state.children) do
  assert(C.IsComponentOperational(child),'stored diagnostic child disappeared')
  local p=position(child);local stored,docked=GetComponentData(A.id(child),'isinternallystored','isdocked')
  log(string.format('child phase=%s index=%d handle=%s stored=%s docked=%s pose=%.9g,%.9g,%.9g',
   phase,index,tostring(child):gsub('ULL$',''),tostring(stored),tostring(docked),p[1],p[2],p[3]))
  AddUITriggeredEvent('X4LAN_HangarPose','Read',{phase=phase,index=index,object=A.id(child),carrier=A.id(state.carrier)})
 end
end
local frame=U.frame
function U.frame(now)
 frame(now)
 if not U.ready or A.paused() or stage==5 then return end
 if stage==2 then C.SetObjectSectorPos(state.carrier,state.sector,value(state.origin,10000)) end
 if now<due then return end
 if stage==0 then
  for _,entry in ipairs(U.publish) do
   local macro=GetComponentData(A.id(entry.object),'macro')
   if macro:find('_carrier_',1,true) then
    local n=tonumber(C.GetNumDockedShips(entry.object,nil));assert(n<=128,'diagnostic dock limit')
    local ships=ffi.new('UniverseID[?]',math.max(1,n));n=tonumber(C.GetDockedShips(ships,n,entry.object,nil))
    local children={}
    for i=0,n-1 do
     if GetComponentData(A.id(ships[i]),'isinternallystored') then children[#children+1]=ships[i] end
    end
    if #children>=2 then
     state={carrier=entry.object,children=children,origin=position(entry.object),sector=C.GetContextByClass(entry.object,'sector',false)}
     assert(A.coop.entity(state.carrier)==0 and state.carrier~=C.GetPlayerContainerID(),'player carrier')
     C.ActivateObject(state.carrier,false)
     for _,child in ipairs(children) do assert(A.coop.entity(child)==0 and child~=C.GetPlayerContainerID(),'player child');C.ActivateObject(child,false) end
     log('selected children='..#children..' macro='..macro);stage=1;due=now+1;return
    end
   end
  end
  due=now+1
 elseif stage==1 then
  state.origin=position(state.carrier);read('before');C.SetObjectSectorPos(state.carrier,state.sector,value(state.origin,10000));read('ffi-immediate');stage=2;due=now+2
 elseif stage==2 then
  read('ffi-move')
  local p=value(state.origin,20000)
  AddUITriggeredEvent('X4LAN_HangarPose','Warp',{object=A.id(state.carrier),sector=A.id(state.sector),x=p.x,y=p.y,z=p.z,yaw=p.yaw,pitch=p.pitch,roll=p.roll})
  stage=3;due=now+2
 elseif stage==3 then
  read('md-warp')
  AddUITriggeredEvent('X4LAN_HangarPose','Undock',{object=A.id(state.children[1])})
  stage=4;due=now+2
 elseif stage==4 then
  read('after-undock');log('complete children='..#state.children);stage=5
 end
end
