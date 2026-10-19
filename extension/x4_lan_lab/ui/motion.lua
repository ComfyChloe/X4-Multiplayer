-- Original source-clock motion buffer. Units: metres, seconds and radians.
-- Rendering never advances the authoritative simulation or creates an object.
X4LAN_MOTION = {}
local M = X4LAN_MOTION
M.delay = .15
M.horizon = .35
M.stale = .75
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
function M.push(track,time,p,velocity,arrival)
 if type(time)~='number' or time~=time or time<0 or time>1e12 or not M.valid(p,velocity) then return false end
 local samples=track.samples or {}
 local last=samples[#samples]
 if last and time<=last.time then return false end
 -- A pause or discontinuity must not interpolate a ship through intervening space.
 local reset=last and time-last.time>M.stale
 if last and not reset then
  local d2=0;local v2=0
  for i=1,3 do d2=d2+(p[i]-last.pose[i])^2;v2=v2+last.velocity[i]^2 end
  reset=math.sqrt(d2)>10000+math.sqrt(v2)*(time-last.time)
 end
 if reset then samples={};track.offset=nil end
 track.offset=math.min(track.offset or math.huge,arrival-time)
 local pp,vv={},{}
 for i=1,6 do pp[i]=p[i];vv[i]=velocity[i] end
 samples[#samples+1]={time=time,pose=pp,velocity=vv}
 while #samples>16 do table.remove(samples,1) end
 track.samples=samples;track.arrival=arrival
 return true
end
function M.render(track,now,delay)
 local samples=track.samples
 if not samples or #samples==0 then return nil end
 local target=now-track.offset-(delay or M.delay)
 local first,last=samples[1],samples[#samples]
 local p={}
 if target<=first.time then for i=1,6 do p[i]=first.pose[i] end;return p end
 for n=2,#samples do
  local a,b=samples[n-1],samples[n]
  if target<=b.time then
   local t=(target-a.time)/(b.time-a.time)
   for i=1,6 do
    local delta=b.pose[i]-a.pose[i]
    p[i]=a.pose[i]+(i>3 and M.angle(delta) or delta)*t
    if i>3 then p[i]=M.angle(p[i]) end
   end
   return p
  end
 end
 -- After packet loss, freeze at the bounded prediction; never snap back to last pose.
 local dt=math.min(M.horizon,math.max(0,target-last.time))
 for i=1,6 do
  p[i]=last.pose[i]+last.velocity[i]*dt
  if i>3 then p[i]=M.angle(p[i]) end
 end
 return p
end
