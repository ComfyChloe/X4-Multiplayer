-- Opt-in saved native dock-reference experiment; no gameplay or wire path.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.dock_registry_probe then return end
assert(cfg.enabled==false and cfg.test_profile=='45841','dock registry requires isolated disabled gameplay')
local D={};X4LAN_SEED_DOCKS=D
local function dump()
 D.stage='dump'
 AddUITriggeredEvent('X4LAN_DockRegistry','Dump',{seed=D.seed,mode=D.mode})
end
function D.start(mode,seed,done)
 assert(not D.stage and (mode=='capture' or mode=='load') and type(done)=='function','invalid registry start')
 assert(type(seed)=='string' and #seed==32 and seed:match('^[a-f0-9]+$'),'invalid registry seed')
 D.mode=mode;D.seed=seed;D.done=done;D.started=GetCurRealTime()
 if mode=='capture' then
  D.stage='mark';AddUITriggeredEvent('X4LAN_DockRegistry','Mark',seed)
 else dump() end
end
RegisterEvent('X4LAN_DockRegistry.Ready',function(_,value)
 local seed,count=tostring(value):match('^([a-f0-9]+)|(%d+)$');count=tonumber(count)
 assert(D.stage=='mark' and seed==D.seed and count and count>0 and count<=250000,'invalid dock registry acknowledgment')
 D.count=count;dump()
end)
RegisterEvent('X4LAN_DockRegistry.Done',function(_,value)
 local seed,count,ok=tostring(value):match('^([a-f0-9]+)|(%d+)|([01])$');count=tonumber(count)
 assert(D.stage=='dump' and seed==D.seed and count and count>0 and count<=250000 and ok=='1'
  and (not D.count or D.count==count),'incomplete native dock registry')
 D.stage='complete'
 DebugError('[X4LAN] SEED_DOCKS complete mode='..D.mode..' id='..D.seed..' count='..count)
 D.done()
end)
RegisterEvent('X4LAN_DockRegistry.Error',function(_,value)
 D.stage='failed';error('SEED_DOCKS native error: '..tostring(value))
end)
function D.frame(now)
 if D.started and D.stage~='complete' and D.stage~='failed' and now-D.started>60 then
  D.stage='failed';error('SEED_DOCKS native registry timed out')
 end
end
