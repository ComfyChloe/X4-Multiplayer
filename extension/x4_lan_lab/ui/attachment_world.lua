-- Explicit internal native observation / pose-admission adapter. Unregistered.
-- The connected transport and client lease gate must authenticate every receive.
-- Dock transitions and cross-sector moves remain fenced until their session
-- execution adapter supplies correlated native completion.
X4LAN_ATTACHMENT_WORLD={}
local W=X4LAN_ATTACHMENT_WORLD
local ffi=require('ffi');local C=ffi.C
ffi.cdef[[
UniverseID GetParentComponent(UniverseID);
bool IsShipAtExternalDock(UniverseID);
bool CanDockAtDockingBay(UniverseID,UniverseID);
uint32_t GetNumDockedShips(UniverseID,const char*);
]]
function W.protected(object)
 for _,player in ipairs({C.GetPlayerControlledShipID(),C.GetPlayerOccupiedShipID(),C.GetPlayerContainerID()}) do
  local parent=player;local ended=false
  for _=1,64 do
   if parent==0 then ended=true;break end
   if parent==object then return true end
   local following=C.GetParentComponent(parent)
   if following==parent then ended=true;break end
   parent=following
  end
  if not ended then return true end
 end
 return false
end
function W.new(cfg,A,u)
 assert(cfg.attachment_sync==true and cfg.seed_binding==true and cfg.seed_verified==true
  and cfg.test_profile=='45841' and cfg.world_mode=='replica','explicit saved-world attachment session required')
 assert(cfg.attachment_execute==nil or type(cfg.attachment_execute)=='boolean','explicit native execution flag required')
 local execute=cfg.attachment_execute==true
 local P=assert(X4LAN_ATTACHMENT_PROTOCOL,'attachment codec missing')
 local D=assert(X4LAN_SEED_DOCK_GATE,'saved dock admission missing')
 assert(D.ready and D.importer and D.importer:ready(),'saved docks not admitted')
 local importer=D.importer;local dockFingerprints=importer:fingerprints()
 local bindings,driver,nativeReady,startedAt={},nil,false,nil
 local self={};local receiver,epoch,lease,ids,cursor,sequence,nextPublish=nil,nil,nil,{},1,0,0
 local function descriptor(authority,index,macro,owner,code)
  assert(type(code)=='string' and code~='' and not code:find('[^%g]'),'unresolved native ship code')
  return table.concat({authority,index,macro,owner,code},':')
 end
 function self:protected(object)
  return W.protected(object)
 end
 function self:inspect(object)
  if not object or object==0 or not C.IsComponentOperational(object) or self:protected(object) then return nil end
  local stored,docked=GetComponentData(A.id(object),'isinternallystored','isdocked')
  local dock=C.GetContextByClass(object,'dockingbay',false)
  if not stored and not docked and dock==0 then return 'FREE',0 end
  if dock==0 then return nil end
  local index=importer:index(dock)
  if not index then return nil end
  if stored then return 'STORED',index end
  if docked and C.IsShipAtExternalDock(object) then return 'EXTERNAL',index end
  return nil -- Transit/unresolved is never translated to FREE.
 end
 local function fail(why)
  if self.failure then return end
  self.failure=tostring(why);u.failed=true
  if driver then driver:stop(self.failure);AddUITriggeredEvent('X4LAN_Attachment','Stop') end
  DebugError('[X4LAN] ATTACH_WORLD STOPPED '..self.failure)
 end
 local function nativeShip(ship)
  local binding=assert(bindings[ship],'attachment binding absent')
  local object=binding.object
  local live=C.IsComponentOperational(object);local protected=self:protected(object)
  if not live or protected then return {object=object,identity=binding.identity,state='FREE',dock=0,
   alive=live,protected=protected} end
  local macro,owner=GetComponentData(A.id(object),'macro','owner')
  assert(macro==binding.macro and owner==binding.owner and A.identity.nativeCode(object)==binding.code,
   'native execution ship descriptor changed')
  if self.viewActive and receiver.ships[ship] then
   local metadata=A.identity.rows['N'..ship]
   if not metadata or metadata.epoch~=epoch or GetCurRealTime()-metadata.at>2.5 then return nil end
   assert(metadata.code==binding.code,'native execution host identity changed')
  end
  local state,index=self:inspect(object)
  local dock=C.GetContextByClass(object,'dockingbay',false)
  return {object=object,identity=binding.identity,alive=true,protected=false,state=state,dock=index,
   container=dock~=0 and C.GetContextByClass(dock,'container',false) or nil}
 end
 function self:stop(why) fail(why or 'stopped') end
 local function startDriver()
  local function healthy()
   assert(not self.failure and not u.failed and D.ready and importer:ready()
    and X4LAN_SEED_GATE.ready and not X4LAN_SEED_GATE.failed,'native session admission lost')
  end
  driver=assert(X4LAN_ATTACHMENT_DRIVER,'native attachment driver absent').new({epoch=epoch,receiver=receiver,
   inspect=nativeShip,paused=function() return C.IsGamePaused() end,
   query=function(index,state,now,callback) return importer:query(index,state,now,callback) end,
   target=function(ship,row,state,index)
    local actual=nativeShip(ship);if not actual then return nil end
    local count=tonumber(C.GetNumDockedShips(row.object,nil))
    local stored,docked=GetComponentData(A.id(row.container),'isinternallystored','isdocked')
    return {dock=index,object=row.object,container=row.container,identity=row.fingerprint,
     alive=C.IsComponentOperational(row.container),protected=self:protected(row.container),
     compatible=C.CanDockAtDockingBay(actual.object,row.object),available=count<(row.storage and row.capacity or 1),
     cycle_free=row.container~=actual.object and not stored and not docked,external=row.external,storage=row.storage}
   end,
   prepare=function(operation,command)
    healthy();assert(nativeReady and self.viewActive,'native execution not admitted')
    local from=assert(nativeShip(command.ship),'fresh ship identity missing');local binding=bindings[command.ship]
    AddUITriggeredEvent('X4LAN_Attachment','Prepare',{seed=cfg.seed_id,epoch=epoch,operation=operation,
     ship=command.ship,code=binding.code,macro=binding.macro,owner=binding.owner,
     action=command.action,state=command.state,dock=command.dock,fromState=from.state,fromDock=from.dock})
   end,
   apply=function(operation)
    healthy();assert(nativeReady,'native execution not admitted')
    AddUITriggeredEvent('X4LAN_Attachment','Apply',{seed=cfg.seed_id,epoch=epoch,operation=operation})
   end})
  local function event(fn)
   return function(_,value)
    if self.failure then return end
    local ok,why=pcall(function() healthy();fn(value) end)
    if not ok then fail(why) end
   end
  end
  local function response(value)
   assert(nativeReady and type(value)=='string' and #value<=128,'unexpected native execution response')
   local fields=P.split(value,'|');local operation=P.integer(fields[3],1,P.maxSequence)
   assert(#fields==4 and fields[1]==cfg.seed_id and fields[2]==epoch and operation,'native execution response identity differs')
   return operation,fields[4]
  end
  RegisterEvent('X4LAN_Attachment.Ready',event(function(value)
   assert(not nativeReady and value==cfg.seed_id..'|'..epoch,'native execution readiness differs')
   nativeReady=true;DebugError('[X4LAN] ATTACH_WORLD READY epoch='..epoch)
  end))
  RegisterEvent('X4LAN_Attachment.Window',event(function(value)
   local operation,accepted=response(value);assert(accepted=='0' or accepted=='1','invalid native execution window')
   local status,why=driver:window(operation,accepted=='1',GetCurRealTime(),C.IsGamePaused())
   assert(status~='failed',why)
  end))
  RegisterEvent('X4LAN_Attachment.Applied',event(function(value)
   local operation,outcome=response(value)
   local status,why=driver:acknowledge(operation,outcome,GetCurRealTime());assert(status~='failed',why)
  end))
  RegisterEvent('X4LAN_Attachment.Error',event(function(value) error(tostring(value)) end))
  startedAt=GetCurRealTime();AddUITriggeredEvent('X4LAN_Attachment','Start',{seed=cfg.seed_id,epoch=epoch,role='p2'})
 end
 function self:commit(b)
  assert(cfg.slot=='p2','only the guest admits a remote attachment baseline')
  local current=A.state.client.lease
  if lease then assert(lease==current and epoch==b.epoch,'attachment session restart required')
  else lease=current;epoch=b.epoch;receiver=P.receiver(lease,2) end
  local ships={}
  for _,wire in ipairs(b.rows) do
   local row=assert(X4LAN_UNIVERSE_PROTOCOL.record(wire))
   if row.kind=='ship' and row.id<=P.maxShip then
    local saved=u.seedObjects.entries[row.id]
    if saved then
     assert(saved.macro==row.macro and saved.owner==row.owner,'saved attachment descriptor differs')
     ships[row.id]=descriptor(epoch,row.id,row.macro,row.owner,saved.code)
     bindings[row.id]={object=saved.object,macro=saved.macro,owner=saved.owner,code=saved.code,identity=ships[row.id]}
    end
   end
  end
  if driver then
   for ship,identity in pairs(receiver.ships) do if ships[ship]~=identity then driver:cancel(ship) end end
  end
  receiver:admit(epoch,b.revision,ships,dockFingerprints)
  self.viewActive=true
  if execute and not driver then startDriver() end
 end
 function self:receive(fields,now)
  if cfg.slot=='p2' and receiver and self.viewActive then
   local status=receiver:receive(table.concat(fields,'|'),now)
   if status=='applied' and driver then
    local observed,why=driver:observe(tonumber(fields[7]),now)
    if observed~='accepted' and observed~='coalesced' then fail(why or ('controller observation '..observed)) end
   end
   return status
  end
  return 'unadmitted'
 end
 function self:clear_view()
  self.viewActive=false
  if driver then for ship in pairs(receiver.ships) do driver:cancel(ship) end end
  if receiver then receiver.records={} end -- Retain identity maps and replay marks; require fresh packets.
 end
 function self:allow_pose(ship,object,now)
  if not receiver or not self.viewActive or not D.ready or not importer:ready() then return false end
  if execute and (not nativeReady or not driver or not driver:allow_pose(ship,now,C.IsGamePaused())) then return false end
  local state=receiver:current(ship,now)
  if state~='FREE' then return false end
  local saved=u.seedObjects.entries[ship]
  local macro,owner=GetComponentData(A.id(object),'macro','owner')
  assert(saved and saved.object==object and saved.macro==macro and saved.owner==owner
   and saved.code==A.identity.nativeCode(object),'live attachment ship descriptor changed')
  return self:inspect(object)=='FREE'
 end
 function self:frame(now)
  assert(D.ready and importer:ready(),'saved dock identity lost')
  if lease then assert(lease==A.state.client.lease,'attachment session lease changed') end
  if driver then
   if not nativeReady then assert(now-startedAt<=10,'native attachment startup timed out')
   else
    local status,why=driver:frame(now,C.IsGamePaused())
    if status=='failed' then fail(why);error(why) end
   end
  end
  if cfg.slot~='p1' or now<nextPublish then return end
  nextPublish=now+.05
  local client=A.state.client
  if lease then assert(lease==client.lease,'attachment host lease changed') else lease=client.lease end
  local sender=u.sender;if not sender then return end
  local b=sender.baseline
  if self.revision~=b.revision then
   self.revision=b.revision;ids={}
   local rows={}
   for _,wire in ipairs(b.rows) do local row=assert(X4LAN_UNIVERSE_PROTOCOL.record(wire));rows[row.id]=row end
   for _,entry in ipairs(u.publish) do
    if entry.id<=P.maxShip then ids[#ids+1]={id=entry.id,object=entry.object,descriptor=rows[entry.id]} end
   end
   table.sort(ids,function(a,b) return a.id<b.id end)
   -- Cursor follows stable IDs across rescans instead of restarting at the first ship.
   local old=self.lastPublished or 0;cursor=1
   while cursor<=#ids and ids[cursor].id<=old do cursor=cursor+1 end
   if cursor>#ids then cursor=1 end
  end
  for _=1,math.min(128,#ids) do
   local entry=ids[cursor];cursor=cursor%#ids+1;self.lastPublished=entry.id
   if C.GetContextByClass(entry.object,'sector',false)==A.coop.sectors[b.sector] then
    local state,dock=self:inspect(entry.object)
    local macro,owner=GetComponentData(A.id(entry.object),'macro','owner')
    local saved=u.seedObjects.entries[entry.id];local expected=entry.descriptor
    if state and saved and expected and expected.kind=='ship' and saved.object==entry.object
     and macro==expected.macro and owner==expected.owner and saved.code==A.identity.nativeCode(entry.object) then
     assert(sequence<P.maxSequence,'attachment sequence exhausted');sequence=sequence+1
     local wire=assert(P.encode({recipient=lease,epoch=lease,revision=b.revision,
      sequence=sequence,ship=entry.id,dock=dock,state=state}))
     A.state.transport:send(wire)
    end
   end
  end
 end
 return self
end
