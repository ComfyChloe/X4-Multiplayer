-- Opt-in native checks of actual target-monitor display and engine targeting.
local A=X4LAN_BUBBLE_API
if not A or not A.identity or X4LAN_CONFIG.test_profile~='45841' or X4LAN_CONFIG.slot~='p2' then return end
local ffi=require('ffi');local C=ffi.C
if not pcall(ffi.typeof,'SofttargetDetails2') then
 ffi.cdef[[typedef struct {UniverseID softtargetID;const char* softtargetConnectionName;uint32_t messageID;} SofttargetDetails2;]]
end
ffi.cdef[[SofttargetDetails2 GetSofttarget2();bool SetSofttarget(UniverseID,const char*const);bool IsTargetable(uint32_t);]]
local frame=A.universe.frame;local nextLog=0
function A.universe.frame(now)
 frame(now)
 if now<nextLog or not A.universe.latest then return end;nextLog=now+1
 for id,r in pairs(A.universe.replicas) do
  if id<=2 and r.object and C.IsComponentOperational(r.object) then
   local code=ffi.string(C.GetObjectIDCode(r.object))
   local known,visible,name=GetComponentData(A.id(r.object),'isknown','isradarvisible','name')
   local selected=C.SetSofttarget(r.object,'')
   local target=C.GetSofttarget2()
   local targetable=target.messageID~=0 and C.IsTargetable(target.messageID)
   local ok,details=pcall(GetTargetMonitorDetails,A.id(r.object),'',false)
   local header=ok and details and details.header and details.header.text or 'unavailable'
   local placeholder=header:match('^%$(%w+)%$$')
   if placeholder then
    local live,value=pcall(GetLiveData,placeholder,A.id(r.object),'')
    if live then header=value end
   end
   DebugError(string.format('[X4LAN] IDENTITY_PROBE id=%s code=%s name=%s known=%s radar=%s selected=%s target=%s targetable=%s header=%s',
    id,code,tostring(name),tostring(known),tostring(visible),tostring(selected),tostring(target.softtargetID),tostring(targetable),tostring(header)))
  end
 end
end
