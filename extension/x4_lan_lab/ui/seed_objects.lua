-- Stable native object binding for peers which loaded the same pristine seed.
-- Session network IDs and engine handles are deliberately separate. Missing
-- objects stay unresolved; this registry never creates or deletes native objects.
X4LAN_SEED_OBJECTS={}
local B=X4LAN_SEED_OBJECTS
local function key(object) return tostring(object):gsub('ULL$','') end
local function code(value)
 return type(value)=='string' and #value>0 and #value<=32 and value:match('^[A-Z0-9-]+$')
end
function B.registry(alive,protected)
 local self={codes={},ids={},handles={}}
 function self:observe(nativeCode,object,macro,owner)
  if not code(nativeCode) or not object or not alive(object) or protected(object) then return false end
  local old=self.codes[nativeCode]
  assert(not old or old.object==object or not alive(old.object),'ambiguous native seed code')
  if old and old.object==object then
   assert(old.macro==macro,'native seed macro changed')
   old.owner=owner
  else self.codes[nativeCode]={object=object,macro=macro,owner=owner,code=nativeCode} end
  return true
 end
 function self:resolve(epoch,id,nativeCode,macro,owner)
  assert(type(epoch)=='string' and type(id)=='number' and id>0,'invalid shared object identity')
  assert(code(nativeCode),'invalid native seed code')
  if self.epoch then assert(self.epoch==epoch,'seed authority epoch changed') else self.epoch=epoch end
  local old=self.ids[id]
  if old then
   assert(old.code==nativeCode and old.macro==macro,'shared object identity changed')
   assert(alive(old.object),'bound seed object disappeared')
  end
  local entry=self.codes[nativeCode]
  if not entry or not alive(entry.object) then return nil end
  assert(entry.macro==macro and entry.owner==owner,'native seed descriptor differs from host')
  assert(not protected(entry.object),'protected player or service object in seed binding')
  local other=self.handles[key(entry.object)]
  assert(not other or other==id,'native seed object bound to multiple shared IDs')
  self.ids[id]=entry;self.handles[key(entry.object)]=id
  return entry.object
 end
 function self:release(id)
  -- Absence from a sector view is not a destruction event. Retain the original
  -- component and stable mapping so a later view can bind the same object again.
  local entry=self.ids[id]
  return entry and entry.object
 end
 return self
end
