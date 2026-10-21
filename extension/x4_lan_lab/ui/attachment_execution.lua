-- Local prepare/consume bridge for the asynchronous MD attachment consumer.
-- Unregistered. Inputs are trusted, correlated local callbacks, not wire commands.
X4LAN_ATTACHMENT_EXECUTION = {}
local E=X4LAN_ATTACHMENT_EXECUTION
local function finite(n) return type(n)=='number' and n==n and n>=0 and n<=1e12 end
local fields={'ship','sequence','action','object','state','dock','target','container'}
local function copy(command)
 local out={};for _,name in ipairs(fields) do out[name]=command[name] end;return out
end
function E.new(options)
 assert(type(options)=='table' and type(options.controller)=='table','controller required')
 assert(type(options.epoch)=='string' and #options.epoch==32 and options.epoch:match('^[a-f0-9]+$'),'authority epoch required')
 assert(finite(options.ttl) and options.ttl>1.25 and options.ttl<=60,'native expiry margin requires ttl>1.25s')
 for _,name in ipairs({'observation','prepare','apply'}) do assert(type(options[name])=='function','native callback required: '..name) end
 local controller,epoch,ttl=options.controller,options.epoch,options.ttl
 local observation,prepare,apply=options.observation,options.prepare,options.apply
 local pending,consuming,failed,last_time,operation=nil,nil,nil,0,0
 local self={}
 local function fail(why)
  failed=why;consuming=nil;controller:stop();return 'failed',why
 end
 local function clock(now)
  if not finite(now) or now<last_time then fail('execution bridge clock moved backwards');return false end
  last_time=now;return not failed
 end
 local function fresh(info,now)
  local row=observation(info.ship,now)
  -- The native window lasts at most the current local calendar second. Keep
  -- additional margin before queuing Apply; simulation age alone is insufficient.
  return type(row)=='table' and row.epoch==epoch and row.sequence==info.sequence
   and finite(row.received_at) and row.received_at<=now and now-row.received_at<=ttl-1.25
 end
 function self:failed() return failed end
 function self:busy() return pending~=nil end
 function self:frame(now)
  if not clock(now) then return 'failed',failed end
  if pending then
   if pending.stage=='prepared' and now-pending.at>.5 then pending=nil;return 'expired' end
   if pending.stage=='applied' and now-pending.at>5 then return fail('native acknowledgment missing; outcome unknown') end
  end
  return 'ready'
 end
 function self:queue(ticket,now,paused)
  if not clock(now) then return 'failed',failed end
  assert(type(paused)=='boolean','explicit pause state required')
  if paused then return 'paused' end
  if pending then return 'busy' end
  local info=controller:describe(ticket)
  if not info then return 'stale' end
  if not fresh(info,now) then return 'expired' end
  if operation==2147483647 then return fail('native operation space exhausted') end
  operation=operation+1
  pending={ticket=ticket,info=copy(info),operation=operation,at=now,stage='prepared'}
  local ok,why=pcall(prepare,operation,copy(info))
  if not ok then return fail('native prepare failed: '..tostring(why)) end
  return 'queued',operation
 end
 -- Only available during Window consumption, allowing the target callback to
 -- require the correlated native Prepare witness rather than a cached query.
 function self:window_command() return consuming and copy(consuming.info) or nil end
 function self:window(index,accepted,now,paused)
  if not clock(now) then return 'failed',failed end
  assert(type(accepted)=='boolean' and type(paused)=='boolean','explicit native window/pause state required')
  local current=pending
  if not current or current.stage~='prepared' or index~=current.operation then return 'stale' end
  if not accepted then pending=nil;return 'rejected' end
  if paused or now-current.at>.5 or not fresh(current.info,now) then pending=nil;return paused and 'paused' or 'expired' end
  consuming=current
  local ok,status,why=pcall(controller.execute,controller,current.ticket,now,false)
  consuming=nil
  if not ok then return fail('controller execution exception: '..tostring(status)) end
  if status~='issued' then
   pending=nil
   if status=='failed' or status=='stopped' then return fail('controller refused native execution: '..tostring(why or status)) end
   return status
  end
  if pending~=current or current.stage~='applied' then return fail('controller dispatch did not consume its native window') end
  return 'issued'
 end
 -- Passed as the controller's dispatch callback. It cannot issue outside the
 -- live correlated Window callback, even if another caller has a valid ticket.
 function self:dispatch(command)
  local current=consuming
  assert(not failed and current and pending==current and current.stage=='prepared','native dispatch outside its prepared window')
  for _,name in ipairs(fields) do assert(command[name]==current.info[name],'prepared native command changed: '..name) end
  assert(type(command.receipt)=='table','opaque native receipt required')
  current.stage='applied';current.receipt=command.receipt;current.at=last_time
  apply(current.operation)
  return true
 end
 function self:acknowledge(index,outcome,now)
  if not clock(now) then return 'failed',failed end
  local current=pending
  if not current or current.stage~='applied' or index~=current.operation then return 'stale' end
  if now-current.at>5 then return fail('native acknowledgment arrived after its deadline') end
  if outcome~='applied' and outcome~='rejected' then return fail('native consumer outcome unknown') end
  local status=controller:acknowledge(current.receipt,outcome=='applied',now)
  if status~='accepted' and status~='rejected' then return fail('native receipt no longer matches its controller fence') end
  pending=nil;return status
 end
 function self:cancel(ship)
  if pending and pending.info.ship==ship and pending.stage=='prepared' then pending=nil end
  controller:cancel(ship) -- Issued native work retains its acknowledgment/readback fence.
 end
 function self:stop()
  failed=failed or 'stopped';consuming=nil;controller:stop()
 end
 return self
end
