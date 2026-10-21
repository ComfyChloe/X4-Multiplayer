-- Local native qualification of the production saved-dock importer, no relay.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.seed_dock_import_probe then return end
assert(cfg.enabled==false and cfg.test_profile=='45841','isolated disabled gameplay required')
local C=require('ffi').C
local D={};X4LAN_SEED_DOCKS=D
local function log(s) DebugError('[X4LAN] DOCK_IMPORT '..s) end
function D.start(mode,seed,done)
 assert(not D.importer and type(done)=='function','duplicate native dock import')
 D.mode=mode;D.seed=seed;D.done=done;D.started=GetCurRealTime()
 D.importer=X4LAN_SEED_DOCK_IMPORT.new(seed,mode,function(v) AddUITriggeredEvent('X4LAN_SeedDocks','Read',v) end)
 if mode=='load' then D.marked=true end
end
RegisterEvent('X4LAN_SeedDocks.Marked',function(_,seed)
 assert(D.mode=='capture' and seed==D.seed and not D.marked,'unexpected dock capture marker');D.marked=true
end)
for _,kind in ipairs({'Begin','Row','End','Error'}) do
 RegisterEvent('X4LAN_SeedDocks.'..kind,function(_,value)
  assert(D.importer and not D.complete,'unexpected native dock event')
  log('event mode='..D.mode..' kind='..kind..' value='..tostring(value))
  local ok,why=D.importer:receive(kind,tostring(value),GetCurRealTime())
  assert(ok,why)
 end)
end
function D.frame(now)
 if not D.importer or D.complete then return end
 assert(now-D.started<120,'native dock import probe timed out')
 -- A multi-frame scan must remain in the exact simulation state that is saved.
 -- Otherwise a ship may be destroyed after its row was admitted but before SaveGame.
 if not C.IsGamePaused() then Pause();return end
 if not D.pauseLogged then
  DebugError('[X4LAN] DOCK_IMPORT_PAUSE mode='..D.mode..' paused=true');D.pauseLogged=true
 end
 if not D.marked then
  if not D.markRequested then D.markRequested=true;AddUITriggeredEvent('X4LAN_SeedDocks','Mark',D.seed) end
  return
 end
 local ok,why=D.importer:frame(now);assert(ok,why)
 if not D.importer:ready() then return end
 if not D.fingerprints then
  D.fingerprints=D.importer:fingerprints();D.queries={}
  local external,storage
  for i,value in ipairs(D.fingerprints) do
   log('fingerprint mode='..D.mode..' index='..i..' value='..value)
   local e,s=value:match('^[^:]+:[^:]+:[^:]+:[^:]+:([01]):([01]):')
   if e=='1' and not external then external=i;D.queries[#D.queries+1]={i,'EXTERNAL'} end
   if s=='1' and not storage then storage=i;D.queries[#D.queries+1]={i,'STORED'} end
  end
  assert(external and storage,'native collection lacks external or storage coverage')
 end
 if D.awaiting then return end
 local query=table.remove(D.queries,1)
 if query then
  D.awaiting=true
  assert(D.importer:query(query[1],query[2],now,function(row)
   log('queried mode='..D.mode..' index='..query[1]..' state='..query[2]..' fingerprint='..row.fingerprint)
   D.awaiting=false
  end))
 else
  D.complete=true;log('complete mode='..D.mode..' seed='..D.seed..' count='..#D.fingerprints);D.done()
 end
end
