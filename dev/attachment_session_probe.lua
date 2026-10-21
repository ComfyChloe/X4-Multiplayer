-- One real-engine component driven by a synthetic host over real authenticated
-- UDP. Full universe_game/fixtures are intentionally absent from this experiment.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.attachment_session_probe then return end
assert(cfg.test_profile=='45841' and cfg.attachment_execute==true,'explicit isolated session diagnostic required')
local ffi=require('ffi');local C=ffi.C;local P=X4LAN_UNIVERSE_PROTOCOL
ffi.cdef[[
void ActivateObject(UniverseID,bool);UniverseID GetPlayerControlledShipID(void);
UniverseID GetPlayerOccupiedShipID(void);UniverseID GetPlayerContainerID(void);
uint32_t GetDockedShips(UniverseID*,uint32_t,UniverseID,const char*);
void SetObjectSectorPos(UniverseID,UniverseID,UIPosRot);
]]
local S={phase=0,pending={},operations={},lastWire=0};local A,u,W
local function id(object) return ConvertStringToLuaID((tostring(object):gsub('ULL$',''))) end
local function log(value) DebugError('[X4LAN] ATTACH_SESSION '..value) end
local function reference(value)
 assert(value:match('^0x[%da-fA-F]+$') and #value<=18,'bad native selected handle')
 local n=ffi.new('uint64_t',0);for digit in value:sub(3):gmatch('.') do n=n*16+tonumber(digit,16) end
 assert(n~=0);return n
end
local function guard(fn)
 if S.failed or S.complete then return end
 local ok,why=pcall(fn)
 if not ok then
  S.failed=true;log('STOPPED '..tostring(why))
  if W then W:stop(why) end
  if S.net then S.net:close() end
  if not C.IsGamePaused() then Pause() end
 end
end
local factory=X4LAN_ATTACHMENT_DRIVER.new
X4LAN_ATTACHMENT_DRIVER.new=function(options)
 local prepare,apply=options.prepare,options.apply
 options.prepare=function(operation,command)
  S.operations[operation]={phase=S.phase,action=command.action,state=command.state,dock=command.dock,sequence=command.sequence}
  log(string.format('prepare phase=%d operation=%d sequence=%d action=%s state=%s dock=%d ship=%d',
   S.phase,operation,command.sequence,command.action,command.state,command.dock,command.ship))
  return prepare(operation,command)
 end
 options.apply=function(operation)
  assert(S.operations[operation],'apply without recorded preparation');S.pending[operation]=true
  local command=S.operations[operation]
  log(string.format('dispatch phase=%d operation=%d sequence=%d action=%s state=%s dock=%d',S.phase,operation,command.sequence,command.action,command.state,command.dock))
  return apply(operation)
 end
 local driver=factory(options);local window,acknowledge=driver.window,driver.acknowledge
 driver.window=function(self,operation,accepted,now,paused)
  local status,why=window(self,operation,accepted,now,paused)
  log('window operation='..operation..' result='..status);return status,why
 end
 driver.acknowledge=function(self,operation,outcome,now)
  local status,why=acknowledge(self,operation,outcome,now)
  assert(status=='accepted' or status=='rejected','native acknowledgment not accepted')
  log('execution-ack operation='..operation..' outcome='..outcome);return status,why
 end
 return driver
end
local register=RegisterEvent
RegisterEvent=function(name,fn)
 if name=='X4LAN_Attachment.Ready' or name=='X4LAN_Attachment.Window' or name=='X4LAN_Attachment.Applied' then
  return register(name,function(...)
   local args={...}
   guard(function()
    local value=tostring(args[2]);log('event kind='..name:match('%.(.+)$')..' value='..value)
    if name=='X4LAN_Attachment.Applied' then
     local operation=tonumber(value:match('|(%d+)|[a-z]+$'))
     assert(operation and S.pending[operation],'unmatched native acknowledgment')
     S.pending[operation]=nil
    end
    fn(unpack(args));assert(not u.failed,'native world adapter failed')
   end)
  end)
 end
 return register(name,fn)
end
RegisterEvent('X4LAN_AttachmentProbe.Error',function(_,why) guard(function() error(tostring(why)) end) end)
RegisterEvent('X4LAN_AttachmentProbe.Selected',function(_,value) guard(function()
 assert(S.selecting and not S.ship,'unexpected native pair selection')
 local ship,carrier,inside,outside,a,b=tostring(value):match('^(%d+)|(%d+)|(%d+)|(%d+)|(0x[%da-fA-F]+)|(0x[%da-fA-F]+)$')
 assert(ship,'malformed native selected pair')
 S.shipIndex,S.carrierIndex,S.inside,S.outside=tonumber(ship),tonumber(carrier),tonumber(inside),tonumber(outside)
 S.ship,S.carrier=reference(a),reference(b)
 assert(not X4LAN_ATTACHMENT_WORLD.protected(S.ship) and not X4LAN_ATTACHMENT_WORLD.protected(S.carrier),'protected selected pair')
 local count=tonumber(C.GetNumDockedShips(S.carrier,nil));assert(count<=256,'carrier child bound')
 local children=ffi.new('UniverseID[?]',count+1);assert(tonumber(C.GetDockedShips(children,count+1,S.carrier,nil))==count,'carrier children changed')
 for i=0,count-1 do assert(not X4LAN_ATTACHMENT_WORLD.protected(children[i]),'protected child');C.ActivateObject(children[i],false) end
 C.ActivateObject(S.carrier,false);C.ActivateObject(S.ship,false)
 S.sector=C.GetContextByClass(S.carrier,'sector',false);local pose=C.GetObjectPositionInSector(S.carrier)
 S.anchor=ffi.new('UIPosRot',{x=pose.x,y=pose.y,z=pose.z,yaw=pose.yaw*180/math.pi,pitch=pose.pitch*180/math.pi,roll=pose.roll*180/math.pi})
 local macro,owner=GetComponentData(id(S.ship),'macro','owner');S.code=ffi.string(C.GetObjectIDCode(S.ship))
 S.macro,S.owner=macro,owner
 u={seedObjects={entries={[S.shipIndex]={object=S.ship,macro=macro,owner=owner,code=S.code}}}}
 A={id=id,state={},identity={rows={},nativeCode=function(object) return ffi.string(C.GetObjectIDCode(object)) end},coop={sectors={}}}
 S.net=X4LAN_TRANSPORT.open(cfg.host,cfg.port)
 S.client=X4LAN_CLIENT.new(cfg,S.net,string.rep('7',32));A.state.client=S.client;A.state.transport=S.net
 S.receiver=P.receiver()
 S.client.receiveExtension=function(_,fields,now)
  if fields[1]=='UBEGIN' or fields[1]=='UOBJECT' or fields[1]=='UCOMMIT' then
   local ack,baseline=S.receiver:receive(fields,now)
   if baseline then
    assert(#baseline.rows==1,'component baseline must contain exactly selected ship')
    local row=assert(P.record(baseline.rows[1]));assert(row.id==S.shipIndex and row.macro==macro and row.owner==owner,'component baseline differs')
    u.latest=baseline;W:commit(baseline);log('baseline epoch='..baseline.epoch..' revision='..baseline.revision..' ship='..row.id)
   end
   if ack then S.net:send('UACK|1|'..S.client.lease..'|'..fields[4]..'|'..fields[5]..'|'..ack) end
   return true
  elseif fields[1]=='IDENT' then
   if #fields==11 and u.latest and fields[4]==u.latest.epoch and fields[6]=='N' and tonumber(fields[7])==S.shipIndex then
    assert(fields[8]==S.code,'synthetic host changed selected identity')
    local sequence=assert(P.integer(fields[5]))
    if sequence>(S.identitySequence or 0) then
     S.identitySequence=sequence;A.identity.rows['N'..S.shipIndex]={epoch=fields[4],code=fields[8],at=now}
    end
   end
   return true
  elseif fields[1]=='UATTACH' then
   local result=W:receive(fields,now)
   log('wire result='..result..' value='..table.concat(fields,'|'))
   if result=='applied' then S.lastWire=now;S.wantedState=fields[9];S.wantedDock=tonumber(fields[8]) end
   return true
  end
  return false
 end
 log(string.format('selected ship=%d carrier=%d inside=%d outside=%d code=%s macro=%s owner=%s',S.shipIndex,S.carrierIndex,S.inside,S.outside,S.code,macro,owner))
end) end)
RegisterEvent('X4LAN_AttachmentProbe.Read',function(_,phase) guard(function()
 assert(S.reading and tonumber(phase)==S.phase,'native phase read order differs')
 log('phase-complete phase='..S.phase)
 S.reading=false;S.stableAt=nil;S.phase=S.phase+1;S.phaseAt=GetCurRealTime()
 if S.phase==8 then
  S.complete=true;AddUITriggeredEvent('X4LAN_Attachment','Stop');S.net:close();Pause()
  log('complete phases=8 native_host=false whole_universe=false')
 end
end) end)
X4LAN_ADMISSION_AFTER={frame=function(now) guard(function()
 S.started=S.started or now;S.phaseAt=S.phaseAt or now
 assert(now-S.started<240 and now-S.phaseAt<45,'component phase timed out')
 assert(X4LAN_SEED_GATE.frame(now) and not X4LAN_SEED_GATE.failed,'production admission lost')
 if not S.selecting then
  S.selecting=true;log('begin seed='..cfg.seed_id);AddUITriggeredEvent('X4LAN_AttachmentProbe','Select',cfg.seed_id);return
 end
 if not S.ship then return end
 -- The diagnostic explicitly releases its own paused-save experiment only after
 -- production admission and native selection. Production pause ownership stays unchanged.
 if C.IsGamePaused() then Unpause();return end
 assert(S.client:update(now,nil),'component transport failed')
 if not S.client.lease then return end
 if not W then log('connected lease='..S.client.lease);W=X4LAN_ATTACHMENT_WORLD.new(cfg,A,u) end
 if now>=(S.nextInterest or 0) then
  S.interest=(S.interest or 0)+1;S.nextInterest=now+1
  S.net:send('UINTEREST|1|'..S.client.lease..'|'..S.interest..'|'..cfg.sector)
 end
 W:frame(now);assert(not u.failed,'component adapter failed')
 assert(not X4LAN_ATTACHMENT_WORLD.protected(S.carrier) and C.IsComponentOperational(S.carrier)
  and not GetComponentData(id(S.carrier),'isdocked'),'carrier changed')
 C.SetObjectSectorPos(S.carrier,S.sector,S.anchor)
 if not u.latest or S.reading or next(S.pending) then return end
 local wanted=({'FREE','STORED','FREE','EXTERNAL','STORED','EXTERNAL','FREE','STORED'})[S.phase+1]
 local dock=wanted=='FREE' and 0 or (wanted=='STORED' and S.inside or S.outside)
 local actual,index=W:inspect(S.ship)
 local allowed=W:allow_pose(S.shipIndex,S.ship,now)
 if actual~=wanted or index~=dock or S.wantedState~=wanted or S.wantedDock~=dock
  or now-S.lastWire>2 or (wanted=='FREE' and not allowed) then S.stableAt=nil;return end
 assert(not allowed or wanted=='FREE','attached ship admitted a pose write')
 S.stableAt=S.stableAt or now;if now-S.stableAt<.5 then return end
 log(string.format('read phase=%d ship=%d code=%s state=%s dock=%d allowPose=%s',S.phase,S.shipIndex,S.code,actual,index,tostring(allowed)))
 S.reading=true;AddUITriggeredEvent('X4LAN_AttachmentProbe','Read',S.phase)
end) end}
