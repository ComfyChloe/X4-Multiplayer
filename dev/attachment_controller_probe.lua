-- Unshipped real-engine controller qualification on one saved non-player pair.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.attachment_controller_probe or cfg.seed_probe~='load' then return end
assert(cfg.enabled==false and cfg.test_profile=='45841','isolated disabled gameplay required')
local ffi=require('ffi');local C=ffi.C;local D=assert(X4LAN_SEED_DOCKS)
ffi.cdef[[
UniverseID GetPlayerControlledShipID(void);UniverseID GetPlayerOccupiedShipID(void);
UniverseID GetPlayerContainerID(void);UniverseID GetParentComponent(UniverseID);
bool IsShipAtExternalDock(UniverseID);bool CanDockAtDockingBay(UniverseID,UniverseID);
uint32_t GetNumDockedShips(UniverseID,const char*);
uint32_t GetDockedShips(UniverseID*,uint32_t,UniverseID,const char*);
void ActivateObject(UniverseID,bool);void SetObjectSectorPos(UniverseID,UniverseID,UIPosRot);
]]
local S={phase=0,sequence=0,operation=0};local scoped_target
local function log(s) DebugError('[X4LAN] ATTACH_CONTROLLER '..s) end
local function id(object) return ConvertStringToLuaID((tostring(object):gsub('ULL$',''))) end
local function reference(text)
 assert(text and text:match('^0x[%da-fA-F]+$') and #text<=18,'invalid selected native reference')
 local result=ffi.new('uint64_t',0);for c in text:sub(3):gmatch('.') do result=result*16+tonumber(c,16) end
 assert(result~=0,'null selected native reference');return result
end
local function protected(object)
 for _,player in ipairs({C.GetPlayerControlledShipID(),C.GetPlayerOccupiedShipID(),C.GetPlayerContainerID()}) do
  local parent=player;local ended=false
  for _=1,64 do
   if parent==0 then ended=true;break end
   if parent==object then return true end
   local next_parent=C.GetParentComponent(parent)
   if next_parent==parent then ended=true;break end
   parent=next_parent
  end
  if not ended then return true end
 end
 return false
end
local function inspect()
 local stored,docked,macro,owner=GetComponentData(id(S.ship),'isinternallystored','isdocked','macro','owner')
 local dock=C.GetContextByClass(S.ship,'dockingbay',false)
 local state=stored and 'STORED' or (docked and C.IsShipAtExternalDock(S.ship) and 'EXTERNAL' or (not docked and 'FREE' or 'TRANSIT'))
 local index=dock~=0 and D.importer:index(dock) or 0
 return {alive=C.IsComponentOperational(S.ship),protected=protected(S.ship),object=S.ship,
  identity=table.concat({cfg.seed_id,S.shipIndex,macro,owner,ffi.string(C.GetObjectIDCode(S.ship))},':'),
  state=state,dock=index,container=dock~=0 and C.GetContextByClass(dock,'container',false) or nil}
end
local function guard(fn)
 if S.failed or S.complete then return end
 local ok,why=pcall(fn)
 if not ok then S.failed=true;if S.controller then S.controller:stop() end;log('STOPPED '..tostring(why)) end
end
local function want()
 local state=({[0]='STORED','FREE','EXTERNAL','STORED','EXTERNAL','FREE','STORED'})[S.phase]
 return state,state=='STORED' and S.inside or (state=='EXTERNAL' and S.outside or 0)
end
RegisterEvent('X4LAN_AttachmentProbe.Selected',function(_,value) guard(function()
 assert(S.selecting and not S.ship,'unexpected native pair selection')
 local a,b,c,d,ship,carrier=tostring(value):match('^(%d+)|(%d+)|(%d+)|(%d+)|(0x[%da-fA-F]+)|(0x[%da-fA-F]+)$')
 assert(a,'malformed native pair selection')
 S.shipIndex=tonumber(a);S.carrierIndex=tonumber(b);S.inside=tonumber(c);S.outside=tonumber(d)
 S.ship=reference(ship);S.carrier=reference(carrier)
 assert(S.ship~=S.carrier and not protected(S.ship) and not protected(S.carrier),'protected or aliased native pair')
 assert(C.IsComponentOperational(S.ship) and C.IsComponentOperational(S.carrier),'dead native pair')
 assert(not GetComponentData(id(S.carrier),'isdocked'),'carrier itself is attached')
 local count=tonumber(C.GetNumDockedShips(S.carrier,nil));assert(count>0 and count<=256,'carrier child bound')
 local children=ffi.new('UniverseID[?]',count+1);local actual=tonumber(C.GetDockedShips(children,count+1,S.carrier,nil))
 assert(actual==count,'carrier children changed during selection')
 for i=0,actual-1 do assert(not protected(children[i]),'player among carrier children');C.ActivateObject(children[i],false) end
 C.ActivateObject(S.carrier,false);C.ActivateObject(S.ship,false)
 S.sector=C.GetContextByClass(S.carrier,'sector',false);local p=C.GetObjectPositionInSector(S.carrier)
 S.pose=ffi.new('UIPosRot',{x=p.x,y=p.y,z=p.z,yaw=p.yaw*180/math.pi,pitch=p.pitch*180/math.pi,roll=p.roll*180/math.pi})
 S.identity=inspect().identity;S.phaseStarted=GetCurRealTime();S.started=S.phaseStarted
 S.controller=X4LAN_ATTACHMENT_CONTROLLER.new({epoch=cfg.seed_id,ttl=2,timeout=30,inspect=inspect,
  target=function(_,index,state)
   local row=scoped_target
   if not row then return nil end
   local count=tonumber(C.GetNumDockedShips(row.object,nil))
   return {dock=index,object=row.object,container=row.container,identity=row.fingerprint,
    alive=C.IsComponentOperational(row.container),protected=protected(row.container),
    compatible=C.CanDockAtDockingBay(S.ship,row.object),available=count<(row.storage and row.capacity or 1),
    cycle_free=row.container==S.carrier and not GetComponentData(id(S.carrier),'isdocked'),
    external=row.external,storage=row.storage}
  end,
  dispatch=function(command)
   local from=inspect();S.operation=S.operation+1;S.awaitingOperation=S.operation;S.issued=true
   log(string.format('dispatch phase=%d operation=%d sequence=%d action=%s state=%s dock=%d',S.phase,S.operation,command.sequence,command.action,command.state,command.dock))
   AddUITriggeredEvent('X4LAN_AttachmentProbe','Apply',{seed=cfg.seed_id,phase=S.phase,operation=S.operation,
    ship=S.shipIndex,action=command.action,state=command.state,dock=command.dock,fromState=from.state,fromDock=from.dock})
   return true
  end})
 log(string.format('selected shipIndex=%d carrierIndex=%d inside=%d outside=%d identity=%s',S.shipIndex,S.carrierIndex,S.inside,S.outside,S.identity))
end) end)
RegisterEvent('X4LAN_AttachmentProbe.Applied',function(_,value) guard(function()
 local operation,ok=tostring(value):match('^(%d+)|([01])$')
 assert(tonumber(operation)==S.awaitingOperation and ok=='1','native action rejected or acknowledgment mismatched')
 S.awaitingOperation=nil
end) end)
RegisterEvent('X4LAN_AttachmentProbe.Read',function(_,phase) guard(function()
 assert(S.reading and tonumber(phase)==S.phase,'native phase readback mismatched');S.reading=nil
 if S.phase==6 then
  S.complete=true;log('complete phases=7 operations='..S.operation)
  if not C.IsGamePaused() then Pause() end
 else S.phase=S.phase+1;S.phaseStarted=GetCurRealTime();S.nextObservation=nil;S.issued=nil end
end) end)
RegisterEvent('X4LAN_AttachmentProbe.Error',function(_,why) guard(function() error(tostring(why)) end) end)
D.afterCompleteReceive=function(kind,value) guard(function()
 DebugError('[X4LAN] ATTACH_DOCK_EVENT kind='..kind..' value='..tostring(value))
 local ok,why=D.importer:receive(kind,tostring(value),GetCurRealTime());assert(ok,why)
end) end
local function read_phase(now)
 local row=inspect();local state,dock=want()
 assert(row.state==state and row.dock==dock and row.identity==S.identity,'Lua phase state differs')
 if S.phase>0 then assert(S.controller:allow_pose(S.shipIndex,now,false)==(state=='FREE'),'pose gate differs from settled native state') end
 log(string.format('read phase=%d state=%s dock=%d shipIndex=%d',S.phase,state,dock,S.shipIndex))
 S.reading=true;AddUITriggeredEvent('X4LAN_AttachmentProbe','Read',S.phase)
end
local function drive(now)
 local status,ticket=S.controller:prepare(S.shipIndex,now,false)
 if status=='ready' then
  local issued,why=S.controller:execute(ticket,now,false);assert(issued=='issued',why or issued)
 elseif status=='settled' then
  assert(not S.awaitingOperation,'settled before native dispatch acknowledgment');S.issued=nil;read_phase(now)
 elseif status=='blocked' then S.issued=nil
 elseif status~='waiting' then error('controller preparation '..tostring(status)..' '..tostring(ticket)) end
end
local original=D.frame
function D.frame(now)
 original(now)
 if not D.complete or S.failed or S.complete then return end
 guard(function()
  if not S.selecting then S.selecting=true;S.selectAt=now;AddUITriggeredEvent('X4LAN_AttachmentProbe','Select',cfg.seed_id);return end
  assert(now-(S.started or S.selectAt)<240,'native controller total timeout')
  if not S.ship then return end
  assert(now-S.phaseStarted<45,'native controller phase timeout '..S.phase)
  assert(not protected(S.ship) and not protected(S.carrier),'player entered diagnostic pair')
  assert(C.IsComponentOperational(S.carrier) and not GetComponentData(id(S.carrier),'isdocked'),'carrier disappeared or docked')
  if C.IsGamePaused() then Pause();return end
  C.SetObjectSectorPos(S.carrier,S.sector,S.pose)
  local ok,why=D.importer:frame(now);assert(ok,why)
  if S.reading or S.querying then return end
  if S.phase==0 then read_phase(now);return end
  local state,dock=want()
  if not S.nextObservation or now>=S.nextObservation then
   S.sequence=S.sequence+1;S.nextObservation=now+.25
   assert(S.controller:desire(S.shipIndex,{epoch=cfg.seed_id,sequence=S.sequence,state=state,dock=dock,
    identity=S.identity,received_at=now},now)=='accepted','synthetic host observation rejected')
  end
  local actual=inspect()
  if S.issued or state=='FREE' or actual.state~='FREE' then drive(now)
  else
   S.querying=true
   assert(D.importer:query(dock,state,now,function(row)
    S.querying=nil;scoped_target=row;drive(GetCurRealTime());scoped_target=nil
   end))
  end
 end)
end
