-- Unshipped full HOST adapter observation. It neither chooses attachments nor
-- replaces native world enumeration. The ordinary fixture startup also runs.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.attachment_world_probe then return end
assert(cfg.slot=='p1' and cfg.test_profile=='45841' and cfg.attachment_sync and cfg.seed_binding)
local A=X4LAN_BUBBLE_API;local U=A.universe;local G=X4LAN_SEED_GATE
local P=X4LAN_ATTACHMENT_PROTOCOL;local C=require('ffi').C
local S={reads={},sampled={},pending={},revisions={},sent=0,completedReads=0}
local function log(v) DebugError('[X4LAN] ATTACH_FULL '..v) end
local function fail(why)
 S.failed=tostring(why);U.failed=true;log('failed '..S.failed)
 if U.attachments then U.attachments:stop(S.failed) end
 if not C.IsGamePaused() then Pause() end
end
local gate=G.frame
G.frame=function(now)
 local ready=gate(now)
 if ready and not S.admitted then
  local D=assert(X4LAN_SEED_DOCK_GATE);assert(D.ready and D.importer:ready())
  local count=0;for _ in pairs(D.importer:fingerprints()) do count=count+1 end
  log('admitted seed='..cfg.seed_id..' docks='..count..' paused='..tostring(C.IsGamePaused()))
  S.admitted=true
  -- Explicit diagnostic release of this preserved paused snapshot only.
  if C.IsGamePaused() then Unpause();log('diagnostic-unpause seed='..cfg.seed_id) end
 end
 return ready
end
local factory=X4LAN_ATTACHMENT_WORLD.new
X4LAN_ATTACHMENT_WORLD.new=function(config,api,universe)
 local world=factory(config,api,universe);local inspect=world.inspect
 world.inspect=function(self,object)
  local state,index=inspect(self,object);local ship=U.seedObjects:index(object)
  if ship then
   local macro,owner=GetComponentData(A.id(object),'macro','owner')
   S.reads[ship]={state=state,dock=index,object=object,macro=macro,owner=owner,
    code=A.identity.nativeCode(object),at=GetCurRealTime()}
  end
  return state,index
 end
 return world
end
local open=X4LAN_TRANSPORT.open
X4LAN_TRANSPORT.open=function(...)
 local net=open(...)
 return {receive=function() return net:receive() end,close=function() return net:close() end,
  send=function(_,packet)
   if packet:sub(1,8)=='UATTACH|' then
    local row=assert(P.decode(packet));local witness=assert(S.reads[row.ship])
    assert(witness.state==row.state and witness.dock==row.dock and GetCurRealTime()-witness.at<=.05,
     'publication differs from current native inspection')
    S.started=S.started or GetCurRealTime();S.sent=S.sent+1;S.revisions[row.revision]=true
    log(string.format('publish at=%.6f handle=%s code=%s macro=%s owner=%s value=%s',GetCurRealTime(),
     tostring(witness.object):gsub('ULL$',''),witness.code,witness.macro,witness.owner,packet))
    local key=row.revision..':'..row.ship
    if not S.sampled[key] then
     S.sampled[key]=true;S.pending[row.sequence]=GetCurRealTime()
     log('sample-request sequence='..row.sequence..' revision='..row.revision..' id='..row.ship)
     AddUITriggeredEvent('X4LAN_AttachmentWorldProbe','Read',{object=A.id(witness.object),
      sequence=row.sequence,revision=row.revision,id=row.ship})
    end
   end
   return net:send(packet)
  end}
end
RegisterEvent('X4LAN_AttachmentWorldProbe.Read',function(_,sequence)
 local id=tonumber(sequence)
 if not S.pending[id] then fail('unexpected native publication readback');return end
 S.pending[id]=nil;S.completedReads=S.completedReads+1;log('sample-complete sequence='..id)
end)
RegisterEvent('X4LAN_AttachmentWorldProbe.Error',function(_,why) fail(why) end)
local frame=U.frame
U.frame=function(now)
 if S.failed or S.complete then return end
 local ok,why=pcall(function()
  frame(now);assert(not U.failed and not A.state.failed,'full adapter stopped')
  for _,at in pairs(S.pending) do assert(now-at<=5,'native publication readback timed out') end
  if not S.started then return end
  assert(now-S.started<=60,'full adapter observation timed out')
  local revisions=0;for _ in pairs(S.revisions) do revisions=revisions+1 end
  if revisions>=3 and now-S.started>=12 and not next(S.pending) then
   assert(A.coop.ready and A.cargo.ready and A.trade.state.ready and A.quest.ready,'ordinary fixtures not ready')
   log('complete slot=p1 revisions='..revisions..' published='..S.sent..' samples='..S.completedReads..' native_guest=false')
   S.complete=true;Pause()
  end
 end)
 if not ok then fail(why) end
end
