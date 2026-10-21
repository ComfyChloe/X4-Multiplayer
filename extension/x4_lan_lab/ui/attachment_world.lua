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
 local P=assert(X4LAN_ATTACHMENT_PROTOCOL,'attachment codec missing')
 local D=assert(X4LAN_SEED_DOCK_GATE,'saved dock admission missing')
 assert(D.ready and D.importer and D.importer:ready(),'saved docks not admitted')
 local importer=D.importer;local dockFingerprints=importer:fingerprints()
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
    end
   end
  end
  receiver:admit(epoch,b.revision,ships,dockFingerprints)
  self.viewActive=true
 end
 function self:receive(fields,now)
  if cfg.slot=='p2' and receiver and self.viewActive then return receiver:receive(table.concat(fields,'|'),now) end
  return 'unadmitted'
 end
 function self:clear_view()
  self.viewActive=false
  if receiver then receiver.records={} end -- Retain identity maps and replay marks; require fresh packets.
 end
 function self:allow_pose(ship,object,now)
  if not receiver or not self.viewActive or not D.ready or not importer:ready() then return false end
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
