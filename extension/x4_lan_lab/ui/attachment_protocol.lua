-- Original UATTACH v1 codec and bounded receiver model; mirrors x4lan/attachment.py.
-- Codec/model only: not registered in ui.xml and never called from game adapters yet.
-- Indices name references saved in the same pristine seed, never runtime handles or dock.seed.
-- Recipient/epoch checks are NOT source authentication: the caller must authenticate the host
-- origin before receive()/admit(). A new epoch is adopted only by an explicit trusted admit().
X4LAN_ATTACHMENT_PROTOCOL = {}
local P=X4LAN_ATTACHMENT_PROTOCOL
P.tag='UATTACH';P.version='1';P.maxPacket=768;P.maxSequence=2147483647;P.maxShip=249999;P.maxDock=250000
P.maxActiveShips=4096
P.states={FREE=true,EXTERNAL=true,STORED=true}
function P.split(s,separator)
 local out={};for v in (s..separator):gmatch('(.-)'..separator) do out[#out+1]=v end;return out
end
function P.hex(s) return type(s)=='string' and #s==32 and not s:find('[^a-f0-9]') end
function P.integer(s,low,high)
 if type(s)~='string' or #s>10 or not (s=='0' or s:match('^[1-9]%d*$')) then return nil end
 local n=tonumber(s);if not n or n<low or n>high then return nil end;return n
end
local function whole(n,low,high) return type(n)=='number' and n%1==0 and n>=low and n<=high end
function P.check(r)
 if type(r)~='table' or not P.hex(r.recipient) or not P.hex(r.epoch) then return nil,'lease/epoch identity' end
 if not whole(r.revision,1,P.maxSequence) or not whole(r.sequence,1,P.maxSequence) or
    not whole(r.ship,1,P.maxShip) or not whole(r.dock,0,P.maxDock) then return nil,'integer out of range' end
 if not P.states[r.state] then return nil,'unknown state' end
 if (r.state=='FREE')~=(r.dock==0) then return nil,'FREE requires dock 0; attached states require a dock' end
 return r
end
function P.encode(r)
 local ok,reason=P.check(r);if not ok then return nil,reason end
 local text=table.concat({P.tag,P.version,r.recipient,r.epoch,string.format('%d',r.revision),
  string.format('%d',r.sequence),string.format('%d',r.ship),string.format('%d',r.dock),r.state},'|')
 if #text>P.maxPacket then return nil,'record exceeds transport MTU' end
 return text
end
-- Returns the record or nil plus a reason; never a partial record.
function P.decode(text)
 if type(text)~='string' or #text>P.maxPacket or text:find('[^%g]') then return nil,'record size/encoding' end
 local f=P.split(text,'|')
 if #f~=9 or f[1]~=P.tag or f[2]~=P.version then return nil,'record tag/version/fields' end
 if not P.hex(f[3]) or not P.hex(f[4]) then return nil,'lease/epoch identity' end
 local revision=P.integer(f[5],1,P.maxSequence);local sequence=P.integer(f[6],1,P.maxSequence)
 local ship=P.integer(f[7],1,P.maxShip);local dock=P.integer(f[8],0,P.maxDock)
 if not revision or not sequence or not ship or not dock then return nil,'noncanonical or out-of-range integer' end
 return P.check({recipient=f[3],epoch=f[4],revision=revision,sequence=sequence,ship=ship,dock=dock,state=f[9]})
end
local function same(a,b)
 return a.recipient==b.recipient and a.epoch==b.epoch and a.revision==b.revision and a.sequence==b.sequence and
        a.ship==b.ship and a.dock==b.dock and a.state==b.state
end
-- A validated copy; descriptors are stable identity fingerprints, never changing pose.
local function identities(values,high,limit)
 local out={};local count=0
 for index,descriptor in pairs(values or {}) do
  count=count+1;if count>limit then error('identity map exceeds its bound') end
  if not whole(index,1,high) then error('identity map index') end
  -- Printable ASCII without spaces, as the attachment controller requires of identities.
  if type(descriptor)~='string' or #descriptor==0 or #descriptor>512 or descriptor:find('[^%g]') then
   error('identity map descriptor')
  end
  out[index]=descriptor
 end
 return out,count
end
local function equal(a,b)
 for k,v in pairs(a) do if b[k]~=v then return false end end
 for k in pairs(b) do if a[k]==nil then return false end end
 return true
end
-- One recipient, one committed baseline identity map. Only 'applied' retains a record; per-ship
-- sequence marks survive invalidation within an epoch. Time is always supplied by the caller.
function P.receiver(lease,ttl)
 assert(P.hex(lease),'recipient lease')
 assert(type(ttl)=='number' and ttl==ttl and ttl>0 and ttl<math.huge,'ttl must be an explicit positive number of seconds')
 local self={lease=lease,ttl=ttl,ships={},docks={},records={},marks={},active=0,cache={ships={},docks={}}}
 -- Validation of an identity map is reused while the caller passes the SAME table with the SAME
 -- contents (interned strings make that comparison cheap); any change is validated again in full.
 -- A saved-dock map of ~50000 fingerprints is re-admitted with every W01 baseline commit.
 local function validated(values,high,limit,cache)
  if values~=nil and cache.input==values and cache.snapshot and equal(values,cache.snapshot) then
   return cache.snapshot,cache.count
  end
  local snapshot,count=identities(values,high,limit)
  cache.input,cache.snapshot,cache.count=values,snapshot,count
  return snapshot,count
 end
 local function copy(map) local out={};for k,v in pairs(map) do out[k]=v end;return out end
 -- Receiver time advances only by forward steps of the caller's clock: a rollback never
 -- extends a record's lifetime.
 function self:tick(now)
  if type(now)~='number' or now~=now or now<0 or now==math.huge then
   error('now must be a finite, nonnegative number of seconds')
  end
  if self.clock==nil then self.clock=now else self.clock=self.clock+math.max(0,now-self.last) end
  self.last=now;return self.clock
 end
 function self:admit(epoch,revision,ships,docks)
  if not P.hex(epoch) or not whole(revision,1,P.maxSequence) then error('baseline identity') end
  local count;ships,count=validated(ships,P.maxShip,P.maxActiveShips,self.cache.ships)
  docks=validated(docks,P.maxDock,P.maxDock,self.cache.docks)
  if epoch==self.epoch then
   if revision<self.revision then error('baseline revision moved backwards') end
   if revision==self.revision then
    if not (ships==self.admittedShips or equal(ships,self.admittedShips)) or
       not (docks==self.admittedDocks or equal(docks,self.admittedDocks)) then
     error('a different identity map at the same revision')
    end
    return
   end
  end
  if epoch~=self.epoch then self.records={};self.marks={}
  else
   for ship,kept in pairs(self.records) do
    if ships[ship]~=self.ships[ship] or (kept.dock~=0 and docks[kept.dock]~=self.docks[kept.dock]) then
     self.records[ship]=nil
    end
   end
  end
  -- The validated snapshots stay immutable; removals and descriptor updates change live copies.
  self.epoch,self.revision,self.ships,self.docks,self.active=epoch,revision,copy(ships),copy(docks),count
  self.admittedShips,self.admittedDocks=ships,docks
 end
 function self:receive(record,now)
  now=self:tick(now)
  if type(record)=='string' then record=P.decode(record) elseif not P.check(record) then record=nil end
  if not record then return 'invalid' end
  -- Retain only a private copy of the validated scalars: a caller-owned table changed later
  -- must never alter the replay fingerprint or the retained state.
  record={recipient=record.recipient,epoch=record.epoch,revision=record.revision,sequence=record.sequence,
          ship=record.ship,dock=record.dock,state=record.state}
  if record.recipient~=self.lease then return 'wrong-recipient' end
  if self.epoch==nil or record.epoch~=self.epoch then return 'unadmitted' end
  if record.revision>self.revision then return 'future-baseline' end
  if record.revision<self.revision then return 'stale-baseline' end
  if not self.ships[record.ship] then return 'unknown-ship' end
  if record.dock~=0 and not self.docks[record.dock] then return 'unknown-dock' end
  local mark=self.marks[record.ship]
  -- Replays never refresh received_at; the last accepted record stays with the mark.
  if mark then
   if record.sequence==mark.sequence then return same(record,mark.record) and 'duplicate' or 'conflict' end
   if record.sequence<mark.sequence then return 'stale' end
  end
  self.marks[record.ship]={sequence=record.sequence,record=record}
  self.records[record.ship]={state=record.state,dock=record.dock,sequence=record.sequence,at=now}
  return 'applied'
 end
 function self:current(ship,now)
  now=self:tick(now)
  local kept=self.records[ship]
  if not kept or now-kept.at>self.ttl then return nil end
  return kept.state,kept.dock,kept.sequence,kept.at
 end
 -- The live record in the shape the attachment controller's desire() consumes, or nil.
 function self:observation(ship,now)
  local state,dock,sequence,at=self:current(ship,now)
  if not state then return nil end
  return {epoch=self.epoch,sequence=sequence,state=state,dock=dock,identity=self.ships[ship],received_at=at}
 end
 function self:remove_ship(ship)
  self.records[ship]=nil
  if self.ships[ship] then self.ships[ship]=nil;self.active=self.active-1 end
 end
 function self:remove_dock(dock)
  self.docks[dock]=nil
  for ship,kept in pairs(self.records) do if kept.dock==dock then self.records[ship]=nil end end
 end
 function self:update_descriptor(kind,index,descriptor)
  local table_,high
  if kind=='ship' then table_,high=self.ships,P.maxShip elseif kind=='dock' then table_,high=self.docks,P.maxDock
  else error('descriptor kind') end
  local new=identities({[index]=descriptor},high,1)[index]
  if table_[index]==new then return end
  if kind=='ship' and not table_[index] then
   if self.active>=P.maxActiveShips then error('identity map exceeds its bound') end
   self.active=self.active+1
  end
  table_[index]=new
  for ship,kept in pairs(self.records) do
   if (kind=='ship' and ship==index) or (kind=='dock' and kept.dock==index) then self.records[ship]=nil end
  end
 end
 function self:expire(now)
  now=self:tick(now)
  for ship,kept in pairs(self.records) do if now-kept.at>self.ttl then self.records[ship]=nil end end
 end
 return self
end
