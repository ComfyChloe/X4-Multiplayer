-- Original source-clock motion buffer. Units: metres, seconds and radians.
-- Rendering never advances the authoritative simulation or creates an object.
X4LAN_MOTION = {}
local M = X4LAN_MOTION
M.delay = .15
M.horizon = .35
M.stale = .75
-- Player motion modes (NMODE, see coop_game.lua). Outside 'normal' a remote ship is
-- driven by pose only. 'fast' is never sent: receivers infer it from a legacy/undeclared
-- stream whose sampled speed exceeds M.fastEnter (hysteresis M.fastExit, hold M.modeHold).
M.modes = {normal=true, boost=true, travel=true, highway=true}
M.profiles = {
 normal={delay=.15, horizon=.35},
 boost={delay=.2, horizon=.35},
 fast={delay=.25, horizon=.5},
 travel={delay=.25, horizon=.5},
 highway={delay=.3, horizon=.6},
}
M.fastEnter, M.fastExit, M.modeHold = 1200, 900, 1
-- Discontinuity speed allowance for declared non-normal modes (m/s). Normal mode keeps
-- the original rule, so in-sector teleports are still never interpolated there.
M.speedCap = {boost=20000, travel=20000, highway=50000}
-- Opt-in per track: the clock offset may rise this many seconds per arrival second, so a
-- faster observer clock (crystal drift) cannot slowly consume the interpolation delay.
M.leak = .002
-- Bounded render-clock slew (seconds per second) and the step that snaps instead.
M.slew, M.snap = .1, 1
function M.angle(v) return (v + math.pi) % (2 * math.pi) - math.pi end
function M.valid(p, velocity)
 if type(p)~='table' or #p~=6 or type(velocity)~='table' or #velocity~=6 then return false end
 for i=1,6 do
  local a,b=p[i],velocity[i]
  if type(a)~='number' or a~=a or math.abs(a)>(i<=3 and 50000000 or 2*math.pi) or
     type(b)~='number' or b~=b or math.abs(b)>(i<=3 and 100000 or 2*math.pi) then return false end
 end
 return true
end
function M.velocity(previous,p,time)
 local v={0,0,0,0,0,0}
 local dt=previous and time-previous.time or 0
 if dt>.001 and dt<=M.stale then
  for i=1,6 do
   local delta=p[i]-previous.pose[i]
   if i>3 then delta=M.angle(delta) end
   v[i]=delta/dt
   if math.abs(v[i])>(i<=3 and 100000 or 2*math.pi) then return {0,0,0,0,0,0} end
  end
 end
 return v
end
function M.speed(velocity)
 if type(velocity)~='table' then return 0 end
 return math.sqrt((velocity[1] or 0)^2+(velocity[2] or 0)^2+(velocity[3] or 0)^2)
end
-- Optional track fields (all absent by default, preserving the original behaviour):
--  retainClock: a discontinuity resets samples but keeps the clock offset (both peers use
--   real time, so the mapping survives pauses and teleports of the same source process).
--  leak: see M.leak.  mode: declared mode of the incoming sample (see M.speedCap).
function M.push(track,time,p,velocity,arrival)
 if type(time)~='number' or time~=time or time<0 or time>1e12 or not M.valid(p,velocity) then return false end
 local samples=track.samples or {}
 local last=samples[#samples]
 if last and time<=last.time then return false end
 -- A pause or discontinuity must not interpolate a ship through intervening space.
 local reset=last and time-last.time>M.stale
 if last and not reset then
  local d2=0;local speed=M.speed(last.velocity)
  for i=1,3 do d2=d2+(p[i]-last.pose[i])^2 end
  local cap=M.speedCap[track.mode or 'normal']
  if cap then speed=math.max(speed,math.min(cap,M.speed(velocity))) end
  reset=math.sqrt(d2)>10000+speed*(time-last.time)
 end
 if reset then
  samples={};track.resets=(track.resets or 0)+1
  if not track.retainClock then track.offset=nil end
 end
 if track.offset and track.leak and track.arrival and arrival>track.arrival then
  track.offset=track.offset+track.leak*(arrival-track.arrival)
 end
 track.offset=math.min(track.offset or math.huge,arrival-time)
 local pp,vv={},{}
 for i=1,6 do pp[i]=p[i];vv[i]=velocity[i] end
 samples[#samples+1]={time=time,pose=pp,velocity=vv}
 while #samples>16 do table.remove(samples,1) end
 track.samples=samples;track.arrival=arrival
 return true
end
-- Source time to render at `now`: the observer-clock lag (offset + delay) moves toward
-- its wanted value at no more than `rate` seconds per second, so a delay or offset change
-- slows or speeds the replica slightly instead of making it hop by speed * change.
function M.clock(track,now,delay,rate)
 if not track.offset then return nil end
 local want=track.offset+(delay or M.delay)
 local lag,at=track.lag,track.lagAt
 if not lag or not at or now<at or math.abs(want-lag)>M.snap then lag=want
 else
  local step=(rate or M.slew)*(now-at)
  lag=want>lag and math.min(want,lag+step) or math.max(want,lag-step)
 end
 track.lag,track.lagAt=lag,now
 return now-lag
end
function M.renderAt(track,target,horizon)
 local samples=track.samples
 if not samples or #samples==0 or not target then return nil end
 horizon=horizon or M.horizon
 local first,last=samples[1],samples[#samples]
 local p={};local velocity={0,0,0}
 -- Optional second result: the derivative of rendered translation. Pose-only
 -- callers retain their API; Euler rates are not a native angular vector.
 if target<=first.time then for i=1,6 do p[i]=first.pose[i] end;return p,velocity end
 for n=2,#samples do
  local a,b=samples[n-1],samples[n]
  if target<=b.time then
   local t=(target-a.time)/(b.time-a.time)
   for i=1,6 do
    local delta=b.pose[i]-a.pose[i]
    p[i]=a.pose[i]+(i>3 and M.angle(delta) or delta)*t
    if i<=3 then velocity[i]=delta/(b.time-a.time) end
    if i>3 then p[i]=M.angle(p[i]) end
   end
   return p,velocity
  end
 end
 -- After packet loss, freeze at the bounded prediction; never snap back to last pose.
 local dt=math.min(horizon,math.max(0,target-last.time))
 for i=1,6 do
  p[i]=last.pose[i]+last.velocity[i]*dt
  if i<=3 and target-last.time<horizon then velocity[i]=last.velocity[i] end
  if i>3 then p[i]=M.angle(p[i]) end
 end
 return p,velocity
end
function M.render(track,now,delay)
 if not track.samples or #track.samples==0 then return nil end
 return M.renderAt(track,now-track.offset-(delay or M.delay),M.horizon)
end
-- Effective render mode from the declared mode (nil for legacy senders) and sampled
-- speed. `state` keeps hysteresis: any non-normal indication holds for M.modeHold s.
function M.classify(state,declared,speed,now)
 local mode=declared and declared~='normal' and M.profiles[declared] and declared or nil
 if not mode then
  if speed>M.fastEnter or (state.mode and state.mode~='normal' and speed>M.fastExit) then mode='fast' end
 end
 if mode then state.mode,state.until_=mode,now+M.modeHold
 elseif state.mode and state.mode~='normal' and now<(state.until_ or 0) then mode=state.mode
 else mode='normal';state.mode=mode end
 return mode,M.profiles[mode]
end
