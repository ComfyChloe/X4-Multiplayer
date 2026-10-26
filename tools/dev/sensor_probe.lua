-- Opt-in isolated engine experiment, never included by the production ui.xml.
-- Compare discovery away from the original starter ship and the remote proxy.
local A=X4LAN_BUBBLE_API
if not A or not A.universe or X4LAN_CONFIG.test_profile~='45841' then return end
local ffi=require('ffi');local C=ffi.C
local objects={};local started;local phase=0
RegisterEvent('X4LAN_SensorProbe.Object',function(_,object)
 local native=ConvertIDTo64Bit(object)
 objects[#objects+1]=native
 -- The controlled fixture must survive the guest's unrelated scene suppression.
 A.universe.excluded[tostring(native):gsub('ULL$','')]=true
end)
local function sample(label)
 for i,object in ipairs(objects) do
  if C.IsComponentOperational(object) then
   local known,visible,name,range=GetComponentData(A.id(object),'isknown','isradarvisible','name','maxradarrange')
   local p=C.GetObjectPositionInSector(object)
   local ok,details=pcall(GetTargetMonitorDetails,A.id(object),'',false)
   local header=ok and details and details.header and details.header.text or 'unavailable'
   local placeholder=header:match('%$(%w+)%$')
   if placeholder then
    local live,value=pcall(GetLiveData,placeholder,A.id(object),'')
    if live then header=value end
   end
   DebugError(string.format('[X4LAN] SENSOR_PROBE phase=%s index=%d object=%s known=%s radar=%s range=%s name=%s header=%s position=%.1f,%.1f,%.1f',
    label,i,tostring(object),tostring(known),tostring(visible),tostring(range),tostring(name),tostring(header),p.x,p.y,p.z))
  end
 end
 AddUITriggeredEvent('X4LAN_SensorProbe','Readback',label)
end
local frame=A.universe.frame
function A.universe.frame(now)
 frame(now)
 if not A.coop.ready then return end
 if not started then
  started=now;AddUITriggeredEvent('X4LAN_SensorProbe','Setup');return
 end
 local elapsed=now-started
 if phase==0 and elapsed>8 then phase=1;sample('before1')
 elseif phase==1 and elapsed>14 then
  phase=2;sample('before2');AddUITriggeredEvent('X4LAN_SensorProbe','Remote')
 elseif phase==2 and elapsed>24 then phase=3;sample('remote1')
 elseif phase==3 and elapsed>30 then
  phase=4;sample('remote2');AddUITriggeredEvent('X4LAN_SensorProbe','Activate')
 elseif phase==4 and elapsed>40 then phase=5;sample('activated1')
 elseif phase==5 and elapsed>48 then
  phase=6;sample('activated2');AddUITriggeredEvent('X4LAN_SensorProbe','NativeShip')
 elseif phase==6 and elapsed>58 then
  phase=7;sample('native_ship');AddUITriggeredEvent('X4LAN_SensorProbe','NativePilot')
 elseif phase==7 and elapsed>68 then phase=8;sample('native_pilot')
 end
end
