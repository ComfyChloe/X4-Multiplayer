-- Session orchestration of the reviewed receiver, controller and native bridge.
-- No engine registration. Callers supply authenticated baseline observations and
-- correlated native events; target queries never authorize execution by themselves.
X4LAN_ATTACHMENT_DRIVER={}
function X4LAN_ATTACHMENT_DRIVER.new(options)
 local receiver=assert(options.receiver);local epoch=assert(options.epoch)
 local inspect=assert(options.inspect);local query=assert(options.query);local target=assert(options.target)
 local pausedNow=assert(options.paused)
 local intents={}
 local tracked,ordered,dirty,cursor,lastShip={}, {},false,1,0
 local scoped,witness,querying,pendingShip,failed,bridge,controller
 local self={}
 local function same_goal(a,b)
  return a and b and a.epoch==b.epoch and a.identity==b.identity and a.state==b.state and a.dock==b.dock
 end
 local function stop(why)
  failed=failed or why;bridge:stop();return 'failed',failed
 end
 local function result(status,why)
  if status=='failed' or status=='stopped' then return stop(why or status) end
  return status,why
 end
 controller=X4LAN_ATTACHMENT_CONTROLLER.new({epoch=epoch,ttl=2,timeout=30,requires_ack=true,
  inspect=inspect,
  target=function(ship,dock,state)
   local row=scoped
   if not row and witness then
    local command=bridge:window_command()
    if command and command.ship==ship and command.dock==dock
     and command.target==witness.object and command.container==witness.container then row=witness end
   end
   if not row then return nil end
   return target(ship,row,state,dock)
  end,
  dispatch=function(command) return bridge:dispatch(command) end})
 bridge=X4LAN_ATTACHMENT_EXECUTION.new({controller=controller,epoch=epoch,ttl=2,
  observation=function(ship,now)
   local current=receiver:observation(ship,now);local intent=intents[ship]
   -- A newer identical state does not invalidate an in-flight local capability,
   -- but must never refresh that capability's original receive timestamp.
   if same_goal(current,intent) and current.sequence>=intent.sequence then return intent end
  end,
  prepare=assert(options.prepare),apply=assert(options.apply)})
 function self:failed() return failed end
 function self:observe(ship,now)
  if failed then return 'failed',failed end
  local observation=receiver:observation(ship,now)
  if not observation then return 'expired' end
  if ((querying and querying.ship==ship) or (bridge:busy() and pendingShip==ship))
   and same_goal(observation,intents[ship]) then return 'coalesced' end
  local status,why=controller:desire(ship,observation,now)
  if status=='accepted' then
   intents[ship]=observation
   if not tracked[ship] then tracked[ship]={};dirty=true end
  end
  return result(status,why)
 end
 function self:cancel(ship)
  bridge:cancel(ship);intents[ship]=nil
  -- Retain the job until native ACK/readback or explicit timeout releases it.
 end
 local function queue(ship,ticket,now,paused)
  local status,why=bridge:queue(ticket,now,paused)
  if status=='queued' then pendingShip=ship;witness=scoped end
  return result(status,why)
 end
 local function step(ship,now,paused)
  local observation=receiver:observation(ship,now)
  if not observation then self:cancel(ship)
  else self:observe(ship,now);observation=intents[ship] end
  local actual=inspect(ship)
  if not actual or (actual.state~='FREE' and actual.state~='EXTERNAL' and actual.state~='STORED') then
   local job=tracked[ship];job.unavailable=job.unavailable or now
   if now-job.unavailable>=30 then return stop('native ship observation unavailable for 30s') end
   return 'unavailable'
  end
  tracked[ship].unavailable=nil
  -- The target witness is intentionally available only during its query and
  -- execution window. Replanning between those callbacks would revoke the
  -- queued ticket merely because that witness is out of scope. Observation
  -- changes/cancellation above still invalidate obsolete goals; the bridge
  -- owns the pending operation's deadline and acknowledgement.
  if bridge:busy() and pendingShip==ship then return 'pending' end
  local status,ticket=controller:prepare(ship,now,paused)
  if status=='inactive' or status=='expired' or status=='unknown' then
   tracked[ship]=nil;intents[ship]=nil;dirty=true;return status
  end
  if status=='ready' then return queue(ship,ticket,now,paused) end
  if status=='blocked' and not paused and not querying and not bridge:busy() and observation
   and actual.state=='FREE' and observation.state~='FREE' then
   local capture={ship=ship,sequence=observation.sequence,revision=receiver.revision,at=now,
    identity=observation.identity,state=observation.state,dock=observation.dock}
   querying=capture
   local requested,why=query(capture.dock,capture.state,now,function(row,at)
    if querying~=capture then return end
    querying=nil
    if failed then return end
    local current=receiver:observation(ship,at)
    local intent=intents[ship]
    if not same_goal(current,intent) or intent.sequence~=capture.sequence or current.identity~=capture.identity
     or receiver.revision~=capture.revision or current.state~=capture.state or current.dock~=capture.dock then return end
    scoped=row
    local ok,why=pcall(function()
     local state,ticket=controller:prepare(ship,at,pausedNow())
     if state=='ready' then queue(ship,ticket,at,pausedNow()) else result(state,ticket) end
    end)
    scoped=nil
    if not ok then stop('native query callback failed: '..tostring(why)) end
   end)
   if requested~=true then return stop('native query refused: '..tostring(why)) end
  end
  return result(status,ticket)
 end
 function self:frame(now,paused)
  if failed then return 'failed',failed end
  if querying and now-querying.at>5 then return stop('native target query timed out') end
  local status,why=bridge:frame(now);if status=='failed' then return stop(why) end
  if not bridge:busy() then pendingShip=nil;witness=nil end
  if dirty then
   ordered={};for ship in pairs(tracked) do ordered[#ordered+1]=ship end
   table.sort(ordered);cursor=1
   while cursor<=#ordered and ordered[cursor]<=lastShip do cursor=cursor+1 end
   if cursor>#ordered then cursor=1 end
   dirty=false
  end
  for _=1,math.min(128,#ordered) do
   local ship=ordered[cursor];cursor=cursor%#ordered+1;lastShip=ship
   if tracked[ship] then step(ship,now,paused) end
   if failed then return 'failed',failed end
  end
  return 'ready'
 end
 function self:window(operation,accepted,now,paused)
  local status,why=bridge:window(operation,accepted,now,paused)
  if status=='issued' or not bridge:busy() then witness=nil end
  return result(status,why)
 end
 function self:acknowledge(operation,outcome,now)
  return result(bridge:acknowledge(operation,outcome,now))
 end
 function self:allow_pose(ship,now,paused)
  if failed or (querying and querying.ship==ship) or (bridge:busy() and pendingShip==ship) then return false end
  local observation=receiver:observation(ship,now)
  if not observation or observation.state~='FREE' then return false end
  return controller:allow_pose(ship,now,paused)
 end
 function self:stop(why) return stop(why or 'stopped') end
 return self
end
