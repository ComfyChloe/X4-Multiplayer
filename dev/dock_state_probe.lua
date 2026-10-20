-- Original opt-in native docking/storage transition feasibility experiment.
local A=X4LAN_BUBBLE_API;local cfg=X4LAN_CONFIG
if not A or not A.universe or cfg.test_profile~='45841' or not cfg.dock_state_probe then return end
local ffi=require('ffi');local C=ffi.C;local U=A.universe
ffi.cdef[[uint32_t GetNumDockedShips(UniverseID,const char*);
uint32_t GetDockedShips(UniverseID*,uint32_t,UniverseID,const char*);]]
local function log(s) DebugError('[X4LAN] DOCK_STATE '..s) end
local state={phase=0};local selected=false
RegisterEvent('X4LAN_DockState.Ready',function(_,value)
 local ship,carrier=tostring(value):match('^(0x[%da-fA-F]+)|(0x[%da-fA-F]+)$')
 assert(ship and selected and not state.ready,'unexpected native docking callback')
 state.ship=A.cargo.object(ship);state.carrier=A.cargo.object(carrier)
 assert(state.ship~=state.sourceShip and state.carrier~=state.sourceCarrier and state.ship~=state.carrier,'diagnostic objects alias originals')
 for _,object in ipairs({state.ship,state.carrier}) do
  assert(C.IsComponentOperational(object) and A.coop.entity(object)==0 and object~=C.GetPlayerContainerID(),'unsafe diagnostic object')
  C.ActivateObject(object,false)
 end
 state.sector=C.GetContextByClass(state.carrier,'sector',false)
 local p=C.GetObjectPositionInSector(state.carrier)
 state.pose=ffi.new('UIPosRot',{x=p.x,y=p.y,z=p.z,yaw=p.yaw*180/math.pi,pitch=p.pitch*180/math.pi,roll=p.roll*180/math.pi})
 state.last=GetCurRealTime()
 state.ready=true
end)
RegisterEvent('X4LAN_DockState.ReadDone',function(_,phase)
 assert(state.awaitingRead and tonumber(phase)==state.phase,'stale native phase acknowledgment')
 state.readDone=true
end)
local expected={
 [0]={stored=true,docked=true},[1]={stored=false,docked=true},[2]={stored=true,docked=true},
 [3]={stored=false,docked=true},[4]={stored=false,docked=false},[5]={stored=true,docked=true}}
local frame=U.frame
function U.frame(now)
 frame(now)
 if not U.ready or A.paused() or state.complete or state.failed then return end
 if not selected then
  for _,entry in ipairs(U.publish) do
   if GetComponentData(A.id(entry.object),'macro'):find('_carrier_',1,true) then
    local n=tonumber(C.GetNumDockedShips(entry.object,nil));assert(n<=128,'dock probe fleet bound')
    local ships=ffi.new('UniverseID[?]',math.max(1,n));n=tonumber(C.GetDockedShips(ships,n,entry.object,nil))
    for i=0,n-1 do
     if GetComponentData(A.id(ships[i]),'isinternallystored') and A.coop.entity(ships[i])==0 and ships[i]~=C.GetPlayerContainerID() then
      assert(A.coop.entity(entry.object)==0 and entry.object~=C.GetPlayerContainerID(),'protected carrier')
      state.sourceShip=ships[i];state.sourceCarrier=entry.object;state.started=now;state.last=now
      selected=true;AddUITriggeredEvent('X4LAN_DockState','Init',{ship=A.id(state.sourceShip),carrier=A.id(state.sourceCarrier)})
      return
     end
    end
   end
  end
  return
 end
 if now-state.last>30 then state.failed=true;log('failed timeout phase='..state.phase);return end
 if not state.ready then return end
 assert(C.IsComponentOperational(state.ship) and C.IsComponentOperational(state.carrier),'diagnostic ship disappeared')
 assert(A.coop.entity(state.carrier)==0 and state.carrier~=C.GetPlayerContainerID(),'player entered diagnostic carrier')
 C.SetObjectSectorPos(state.carrier,state.sector,state.pose)
 if state.awaitingRead then
  if not state.readDone then return end
  state.awaitingRead=nil;state.readDone=nil
  if state.phase==5 then state.complete=true;log('complete phases=6');return end
  state.phase=state.phase+1;state.last=now
  AddUITriggeredEvent('X4LAN_DockState','Apply',state.phase);return
 end
 if not state.ready or now-state.last<3 then return end
 local stored,docked=GetComponentData(A.id(state.ship),'isinternallystored','isdocked')
 local want=expected[state.phase]
 if stored~=want.stored or docked~=want.docked then return end
 log(string.format('read phase=%d stored=%s docked=%s ship=%s carrier=%s',state.phase,tostring(stored),tostring(docked),tostring(state.ship):gsub('ULL$',''),tostring(state.carrier):gsub('ULL$','')))
 state.awaitingRead=true;AddUITriggeredEvent('X4LAN_DockState','Read',state.phase)
end
