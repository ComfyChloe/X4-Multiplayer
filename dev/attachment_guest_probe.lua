-- Unshipped full native guest observation of a retained real host capture.
-- Does not replace enumeration, binding, rendering, controller or native actions.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.attachment_guest_probe then return end
assert(cfg.slot=='p2' and cfg.test_profile=='45841' and cfg.attachment_execute)
local A=X4LAN_BUBBLE_API;local U=A.universe;local G=X4LAN_SEED_GATE
local C=require('ffi').C;local expected=assert(X4LAN_NATIVE_REPLAY)
local S={reads={},operations={},pending={},handles={},cycle=0,sequence=0,acks=0}
local function log(v) DebugError('[X4LAN] ATTACH_GUEST '..v) end
local function fail(why)
 if S.failed then return end
 S.failed=tostring(why);U.failed=true;log('failed '..S.failed)
 if U.attachments then U.attachments:stop(S.failed) end
 if not C.IsGamePaused() then Pause() end
end
-- Read-only tracing around the actual renderer call. No substitute pose write,
-- activation, carrier holding or changed authority is introduced by this hook.
do
 local mt=getmetatable(C);local lookup=type(mt)=='table' and mt.__index
 assert(type(lookup)=='function','native call diagnostic hook unavailable')
 local apply=C.SetObjectSectorPos;local traced={}
 mt.__index=function(lib,name)
  if lib==C and name=='SetObjectSectorPos' then return function(object,sector,value)
   local ship=U.seedObjects and U.seedObjects:index(object)
   local selected=ship and expected[ship] and U.replicas[ship] and U.replicas[ship].object==object
   local now=GetCurRealTime();local watch=selected and (not traced[ship] or now-traced[ship]>=1)
   local before=watch and C.GetObjectPositionInSector(object)
   apply(object,sector,value)
   if watch then
    traced[ship]=now;local after=C.GetObjectPositionInSector(object)
    local actualSector=C.GetContextByClass(object,'sector',false)
    local sectorName=actualSector~=0 and GetComponentData(A.id(actualSector),'macro') or 'UNKNOWN'
    local docked,stored=GetComponentData(A.id(object),'isdocked','isinternallystored')
    DebugError(string.format('[X4LAN] ATTACH_DIAGNOSTIC write id=%d at=%.6f sector=%s requested=%s parent=%s docked=%s stored=%s before=%.9g,%.9g,%.9g target=%.9g,%.9g,%.9g after=%.9g,%.9g,%.9g',
     ship,now,sectorName,GetComponentData(A.id(sector),'macro'),tostring(C.GetParentComponent(object)):gsub('ULL$',''),
     tostring(docked),tostring(stored),before.x,before.y,before.z,value.x,value.y,value.z,after.x,after.y,after.z))
   end
  end end
  return lookup(lib,name)
 end
end
local gate=G.frame
G.frame=function(now)
 local ready=gate(now)
 if ready and not S.admitted then
  local D=assert(X4LAN_SEED_DOCK_GATE);assert(D.ready and D.importer:ready())
  local count=0;for _ in pairs(D.importer:fingerprints()) do count=count+1 end
  log('admitted seed='..cfg.seed_id..' docks='..count..' paused='..tostring(C.IsGamePaused()))
  S.admitted=true
  if C.IsGamePaused() then Unpause();log('diagnostic-unpause seed='..cfg.seed_id) end
 end
 return ready
end
local factory=X4LAN_ATTACHMENT_DRIVER.new
X4LAN_ATTACHMENT_DRIVER.new=function(options)
 local prepare,apply=options.prepare,options.apply
 options.prepare=function(operation,command)
  assert(not S.operations[operation],'duplicate native operation')
  S.operations[operation]={pending=true,ship=command.ship}
  log(string.format('prepare operation=%d ship=%d action=%s state=%s dock=%d sequence=%d at=%.6f',
   operation,command.ship,command.action,command.state,command.dock,command.sequence,GetCurRealTime()))
  return prepare(operation,command)
 end
 options.apply=function(operation)
  local op=assert(S.operations[operation]);assert(op.pending and not op.issued)
  op.issued=true;log(string.format('dispatch operation=%d at=%.6f',operation,GetCurRealTime()));return apply(operation)
 end
 local driver=factory(options);local window,ack=driver.window,driver.acknowledge
 driver.window=function(self,operation,accepted,now,paused)
  local status,why=window(self,operation,accepted,now,paused)
  local op=assert(S.operations[operation]);if status~='issued' then op.pending=false end
  log('window operation='..operation..' result='..status);return status,why
 end
 driver.acknowledge=function(self,operation,outcome,now)
  local op=assert(S.operations[operation]);assert(op.pending and op.issued,'unmatched native acknowledgment')
  local status,why=ack(self,operation,outcome,now);op.pending=false;S.acks=S.acks+1
  log('execution-ack operation='..operation..' outcome='..outcome..' result='..status);return status,why
 end
 return driver
end
local worldFactory=X4LAN_ATTACHMENT_WORLD.new
X4LAN_ATTACHMENT_WORLD.new=function(...)
 local world=worldFactory(...);local receive,commit=world.receive,world.commit
 world.receive=function(self,fields,now)
  local status=receive(self,fields,now)
  log(string.format('wire at=%.6f result=%s value=%s',now,status,table.concat(fields,'|')))
  return status
 end
 world.commit=function(self,b)
  commit(self,b);S.started=S.started or GetCurRealTime()
  log('baseline epoch='..b.epoch..' revision='..b.revision..' count='..#b.rows)
 end
 return world
end
RegisterEvent('X4LAN_AttachmentGuestProbe.Read',function(_,sequence)
 local id=tonumber(sequence)
 if not S.reads[id] then fail('unexpected native read completion');return end
 S.reads[id]=nil;log('sample-complete sequence='..id)
end)
RegisterEvent('X4LAN_AttachmentGuestProbe.Error',function(_,why) fail(why) end)
local frame=U.frame
U.frame=function(now)
 if S.failed or S.complete then return end
 local ok,why=pcall(function()
  frame(now);assert(not U.failed and not A.state.failed,'full guest adapter stopped')
  for _,at in pairs(S.reads) do assert(now-at<=5,'native guest read timed out') end
  if not S.started or not U.latest then return end
  local elapsed=now-S.started
  assert(elapsed<=45,'guest observation timed out')
  if not next(S.reads) and now>=(S.nextSample or S.started+3) and elapsed<30 then
   S.cycle=S.cycle+1;S.nextSample=now+2
   log(string.format('cycle index=%d revision=%d elapsed=%.6f at=%.6f',S.cycle,U.latest.revision,elapsed,now))
   local count=0
   for ship,want in pairs(expected) do
    count=count+1
    local r=U.replicas[ship];local object=r and r.object
    if not object or not C.IsComponentOperational(object) then
     log('missing cycle='..S.cycle..' id='..ship)
    else
     local macro,owner=GetComponentData(A.id(object),'macro','owner')
     local state,dock=U.attachments:inspect(object)
     local allowed=U.attachments:allow_pose(ship,object,now)
     local p=C.GetObjectPositionInSector(object);local wanted=r.entity.pose
     local sector=C.GetContextByClass(object,'sector',false)
     local sectorName=sector~=0 and GetComponentData(A.id(sector),'macro') or 'UNKNOWN'
     local parent=tostring(C.GetParentComponent(object)):gsub('ULL$','')
     local err=math.max(math.abs(p.x-wanted[1]),math.abs(p.y-wanted[2]),math.abs(p.z-wanted[3]))
     local same=not S.handles[ship] or S.handles[ship]==object;S.handles[ship]=object
     S.sequence=S.sequence+1;S.reads[S.sequence]=now
     log(string.format('sample sequence=%d cycle=%d revision=%d id=%d handle=%s code=%s macro=%s owner=%s original=%s sameHandle=%s state=%s dock=%d allowPose=%s poseError=%.9g at=%.6f sector=%s parent=%s pose=%.9g,%.9g,%.9g target=%.9g,%.9g,%.9g',
      S.sequence,S.cycle,U.latest.revision,ship,tostring(object):gsub('ULL$',''),A.identity.nativeCode(object),macro,owner,
      tostring(r.seedOriginal==true),tostring(same),state or 'UNKNOWN',dock or 0,tostring(allowed),err,now,
      sectorName,parent,p.x,p.y,p.z,wanted[1],wanted[2],wanted[3]))
     AddUITriggeredEvent('X4LAN_AttachmentGuestProbe','Read',{object=A.id(object),id=ship,
      sequence=S.sequence,revision=U.latest.revision,cycle=S.cycle})
    end
   end
   log('cycle-end index='..S.cycle..' count='..count)
  end
  if elapsed>=30 and not next(S.reads) then
   for _,op in pairs(S.operations) do if op.pending then return end end
   assert(A.coop.ready and A.cargo.ready and A.trade.state.ready and A.quest.ready,'ordinary guest fixtures not ready')
   -- Pause alone does not stop client receive callbacks. Close this owned
   -- diagnostic transport before sealing the evidence boundary.
   A.state.transport:close();AddUITriggeredEvent('X4LAN_Attachment','Stop')
   S.complete=true;Pause()
   log(string.format('complete slot=p2 cycles=%d samples=%d executionAcks=%d elapsed=%.6f live_native_host=false',S.cycle,S.sequence,S.acks,elapsed))
  end
 end)
 if not ok then fail(why) end
end
