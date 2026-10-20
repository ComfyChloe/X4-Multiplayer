-- Shared indices refer to MD object references saved with the pristine world.
-- Display ID codes are NOT globally unique and are never used as lookup keys.
X4LAN_SEED_OBJECTS={}
local B=X4LAN_SEED_OBJECTS
B.newObjectBase=250000
local function key(object) return tostring(object):gsub('ULL$','') end
function B.registry(alive,protected)
 local self={entries={},ids={},handles={}}
 function self:observe(index,object,macro,owner,nativeCode)
  assert(type(index)=='number' and index>=1 and index<B.newObjectBase and index==math.floor(index),'invalid saved object index')
  if not object or not alive(object) then return false end
  local old=self.entries[index];local other=self.handles[key(object)]
  assert(not other or other==index,'native component appears at multiple seed indices')
  assert(not old or old.object==object and old.macro==macro,'saved object reference changed')
  self.entries[index]={object=object,macro=macro,owner=owner,code=nativeCode}
  self.handles[key(object)]=index
  return true
 end
 function self:index(object) return self.handles[key(object)] end
 function self:resolve(epoch,id,nativeCode,macro,owner)
  assert(type(epoch)=='string' and type(id)=='number' and id>0,'invalid shared object identity')
  if self.epoch then assert(self.epoch==epoch,'seed authority epoch changed') else self.epoch=epoch end
  local entry=self.entries[id]
  if not entry then return nil end -- Host-created objects need a lifecycle descriptor.
  assert(alive(entry.object),'bound seed object disappeared id='..id)
  assert(entry.macro==macro and entry.owner==owner and entry.code==nativeCode,
   'saved object descriptor differs id='..id..' native='..tostring(entry.code)..'/'..entry.macro..'/'..entry.owner..
   ' host='..tostring(nativeCode)..'/'..macro..'/'..owner)
  assert(not protected(entry.object),'protected player or service object in seed binding')
  self.ids[id]=entry
  return entry.object
 end
 function self:release(id)
  -- Sector-view absence is not a destruction event. Keep the original reference.
  local entry=self.ids[id]
  return entry and entry.object
 end
 return self
end
