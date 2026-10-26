-- Basic standing avatar; only the four explicitly shared containers are mapped.
local cfg=X4LAN_CONFIG;local A=X4LAN_BUBBLE_API
-- Experimental room mapping is not yet visually verified; release packages leave this off.
if X4LAN_FEATURES then X4LAN_FEATURES.gate('walking_avatars',cfg and cfg.walking_avatars==true,cfg and cfg.enabled and cfg.trading,'trading',
 A and A.state.active,'active bubble') end
if not cfg or not cfg.enabled or not cfg.trading or cfg.walking_avatars~=true or not A or not A.state.active then return end
local s=A.state;local w={sequence=0,remoteSequence=-1};A.walk=w
local function pose(fields,start)
 local values={}
 for i=1,7 do
  local n=tonumber(fields[start+i-1]);local bound=i==1 and 4 or (i<=4 and 10000 or 360)
  if not n or n~=n or math.abs(n)>bound or (i==1 and (n<0 or n%1~=0)) then return nil end
  values[i]=n
 end
 return values
end
function w.receive(f,now)
 if f[1]~='WALK' then return false end
 if #f~=12 or #f[4]~=32 or not f[4]:match('^[a-f0-9]+$') then return true end
 local seq=tonumber(f[5]);local p=pose(f,6)
 if not seq or seq<0 or seq>2147483647 or seq%1~=0 or not p then return true end
 if w.owner~=f[4] then w.owner=f[4];w.remoteSequence=-1 end
 if seq<=w.remoteSequence then return true end
 w.remoteSequence=seq;w.at=now;w.pending=p
 return true
end
function w.frame(now)
 if not w.setup and A.trade and A.trade.state.ready then
  w.setup=true;AddUITriggeredEvent('X4LAN_Walk','Setup')
 end
 if w.visible and now-(w.at or 0)>1 then
  w.visible=false;w.applied=nil;w.pending=nil;AddUITriggeredEvent('X4LAN_Walk','Hide')
 end
 if w.setup and w.pending and now>=(w.nextApply or 0) then
  local p=w.pending;local key=table.concat(p,'|');w.pending=nil;w.nextApply=now+.2
  if key~=w.applied then
   w.applied=key;w.visible=p[1]~=0
   AddUITriggeredEvent('X4LAN_Walk','Move',{context=p[1],x=p[2],y=p[3],z=p[4],yaw=p[5],pitch=p[6],roll=p[7]})
  end
 end
 if s.client and s.client.lease and w.localPose and not A.paused() and now>=(w.nextSend or 0) then
  w.sequence=w.sequence+1;w.nextSend=now+.2
  s.transport:send('WALK|1|'..s.client.lease..'|'..w.sequence..'|'..w.localPose)
 end
end
RegisterEvent('X4LAN_Walk.Pose',function(_,payload)
 local f={};for value in payload:gmatch('[^|]+') do f[#f+1]=value end
 if #f==7 and pose(f,1) then w.localPose=payload end
end)
