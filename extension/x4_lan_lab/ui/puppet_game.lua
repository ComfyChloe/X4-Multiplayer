-- Original INTERNAL puppet client and load-time measurement. No third-party framework.
-- Puppet client (config puppet_client, guest role only): X4's own NPC-creating systems are
-- switched off at runtime by md/x4lan_puppet.xml, so the client shows what the server sends.
-- The host/authority is never a puppet. On both roles this file also logs X4's recorded load-stage
-- durations (<profile>/progressbar.xml, LOADTIME rows) and a periodic NPC ship census (PUPPET rows).
-- See reports/PUPPET-CLIENT.md.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.enabled then return end
local P={census_every=30,activate_every=1,reread_after=120}
X4LAN_PUPPET=P
local function log(v) DebugError('[X4LAN] '..v) end

-- Role gating. Pure: (config) -> active, reason. The host (slot p1) is never a puppet.
function P.decide(c)
 local flag=c.puppet_client
 if c.slot=='p1' then return false,'host_role' end
 if flag==nil or flag==false then return false,'config' end
 if flag~=true then return false,'invalid_value' end
 if c.slot~='p2' then return false,'unknown_role' end
 return true,nil
end

-- Tolerant reader for X4's progressbar.xml. The exact schema is not documented, so every
-- element with a stage name (id/name/step/stage/type, or the tag itself) and one numeric
-- attribute or numeric text body is a stage. Values are logged as recorded (unit not converted).
local NAME_KEYS={'id','name','step','stage','type'}
local VALUE_KEYS={'time','duration','value','seconds','avg','average','ms','t'}
function P.parseProgress(text)
 if type(text)~='string' or text=='' then return nil,'empty' end
 local stages={}
 local function add(name,value,key)
  if name and value and value==value and value>=0 then stages[#stages+1]={name=name,value=value,key=key} end
 end
 local function attrs(s)
  local a={}
  for k,v in s:gmatch('([%w_:%-]+)%s*=%s*"([^"]*)"') do a[k:lower()]=v end
  for k,v in s:gmatch("([%w_:%-]+)%s*=%s*'([^']*)'") do a[k:lower()]=v end
  return a
 end
 local body=text:gsub('<!%-%-.-%-%->',''):gsub('<%?.-%?>','')
 for tag,rest,close in body:gmatch('<([%a_][%w_:%-]*)([^>]*)(/?)>') do
  local a=attrs(rest)
  local name;for _,k in ipairs(NAME_KEYS) do if a[k] and a[k]~='' then name=a[k];break end end
  local value,key
  for _,k in ipairs(VALUE_KEYS) do if a[k] and tonumber(a[k]) then value,key=tonumber(a[k]),k;break end end
  if value then add(name or tag,value,key) end
 end
 -- <step name="init">8.3</step> or <init>8.3</init>
 for tag,rest,number in body:gmatch('<([%a_][%w_:%-]*)([^>/]*)>%s*([%d%.eE%+%-]+)%s*</[%a_][%w_:%-]*>') do
  local a=attrs(rest);local name
  for _,k in ipairs(NAME_KEYS) do if a[k] and a[k]~='' then name=a[k];break end end
  add(name or tag,tonumber(number),'text')
 end
 if #stages==0 then return nil,'no_stages' end
 return stages
end

function P.loadtimeRow(role,active,stages,bytes)
 local out,sum={},0
 for _,s in ipairs(stages) do
  out[#out+1]=tostring(s.name):gsub('[%s,:|=]','_')..':'..string.format('%g',s.value);sum=sum+s.value
 end
 return string.format('LOADTIME v=1 role=%s puppet=%d source=progressbar.xml bytes=%d key=%s stages=%s sum=%g',
  role,active and 1 or 0,bytes or 0,stages[1].key,table.concat(out,','),sum)
end

function P.censusRow(role,active,ships,factions,replicas,status)
 local localShips=ships-(replicas or 0)
 local row=string.format('PUPPET v=1 role=%s puppet=%d npc_ships=%d replicas=%d local_npc_ships=%d factions=%d',
  role,active and 1 or 0,ships,replicas or 0,localShips,factions)
 if P.baseline then row=row..string.format(' baseline=%d delta=%+d',P.baseline,localShips-P.baseline) end
 if status then
  row=row..string.format(' md_active=%d job_spawns=%s removed=%s jobs_off=%s cues_cancelled=%s',
   status.active and 1 or 0,status.jobSpawns,status.removed,status.jobsOff,status.cancelled)
 end
 return row
end

local role=cfg.slot=='p1' and 'host' or (cfg.slot=='p2' and 'guest' or tostring(cfg.slot))
local active,reason=P.decide(cfg)
P.active,P.role=active,role
local F=rawget(_G,'X4LAN_FEATURES')
if F and F.declare then F.declare('puppet_client',active,reason) end
if active then log('PUPPET ARMED role='..role..' (NPC jobs, faction logic, god, crises and generic offers switch off after load)')
else log('FEATURE puppet_client DISABLED reason='..reason..' role='..role) end
if cfg.slot=='p1' and cfg.puppet_client==true then log('ERROR puppet_client=true refused on the host/authority') end

local ffi=require('ffi');local C=ffi.C
for _,decl in ipairs({'bool IsPlayerValid(void);','const char* GetSaveFolderPath(void);',
 'uint32_t GetAllFactions(const char**,uint32_t,bool);','uint32_t GetNumAllFactionShips(const char*);'}) do
 pcall(ffi.cdef,decl)
end

-- File access: Lua io in tests, libc through FFI in X4 (its UI Lua has no io library).
local stdio
local function readFile(path,limit)
 local io=rawget(_G,'io')
 if io and io.open then
  local f=io.open(path,'rb');if not f then return nil,'file_not_found' end
  local data=f:read(limit);f:close();return data or ''
 end
 if stdio==nil then
  stdio=true
  pcall(ffi.cdef,[[
typedef struct x4lan_puppet_FILE x4lan_puppet_FILE;
x4lan_puppet_FILE* fopen(const char*,const char*);
size_t fread(void*,size_t,size_t,x4lan_puppet_FILE*);
int fclose(x4lan_puppet_FILE*);
]])
 end
 local ok,f=pcall(function() return C.fopen(path,'rb') end)
 if not ok then return nil,'no_file_api' end
 if f==nil then return nil,'file_not_found' end
 local buffer=ffi.new('char[?]',limit)
 local n=tonumber(C.fread(buffer,1,limit,f));C.fclose(f)
 return ffi.string(buffer,n)
end
function P.progressPath()
 local save=ffi.string(C.GetSaveFolderPath()):gsub('\\','/'):gsub('/+$','')
 local profile=save:match('^(.*)/save$')
 if not profile then return nil end
 return profile..'/progressbar.xml'
end
local function loadtime(tag)
 local ok,err=pcall(function()
  local path=P.progressPath();if not path then log('LOADTIME unavailable reason=no_profile_path');return end
  local text,why=readFile(path,65536)
  if not text then log('LOADTIME unavailable reason='..why);return end
  if tag and text==P.lastProgress then return end
  P.lastProgress=text
  local stages,bad=P.parseProgress(text)
  if not stages then
   log('LOADTIME unavailable reason='..bad..' bytes='..#text..' head='..text:sub(1,160):gsub('%c',' '))
   return
  end
  log(P.loadtimeRow(role,active,stages,#text)..(tag and ' '..tag or ''))
 end)
 if not ok then log('LOADTIME unavailable reason=error '..tostring(err)) end
end

local function countShips()
 local names=ffi.new('const char*[256]')
 local n=tonumber(C.GetAllFactions(names,256,true))
 if n<=0 or n>=256 then return nil end
 local ships,factions=0,0
 for i=0,n-1 do
  local faction=ffi.string(names[i])
  if faction~='player' then factions=factions+1;ships=ships+tonumber(C.GetNumAllFactionShips(faction)) end
 end
 return ships,factions
end
local function replicaCount()
 local A=rawget(_G,'X4LAN_BUBBLE_API');local u=A and A.universe
 local n=0;if u and u.replicas then for _ in pairs(u.replicas) do n=n+1 end end
 return n
end
local function census()
 local ships,factions=countShips()
 if not ships then log('PUPPET census unavailable reason=faction_enumeration');return end
 local replicas=replicaCount()
 if active and P.acked and not P.baseline then P.baseline=ships-replicas end
 log(P.censusRow(role,active,ships,factions,replicas,P.status))
end

RegisterEvent('X4LAN_Puppet.Active',function(_,param)
 local n,jobs,cues=tostring(param):match('^(%d+)|(%d+)|(%d+)$')
 if not P.acked then log('PUPPET ACTIVE activation='..tostring(n)..' jobs_off_total='..tostring(jobs)..' cues_cancelled_total='..tostring(cues)) end
 P.acked=true
end)
RegisterEvent('X4LAN_Puppet.Refused',function(_,why)
 P.failed=true;log('FEATURE puppet_client DISABLED reason='..tostring(why))
end)
RegisterEvent('X4LAN_Puppet.Status',function(_,param)
 local a,spawns,removed,jobs,cues=tostring(param):match('^(%d)|(%d+)|(%d+)|(%d+)|(%d+)$')
 if not a then return end
 P.status={active=a=='1',jobSpawns=spawns,removed=removed,jobsOff=jobs,cancelled=cues}
 if P.status.active and not active and not P.warnedSave then
  -- A save written by a puppet client keeps cancelled vanilla managers; never serve it.
  P.warnedSave=true
  log('ERROR FEATURE puppet_client DISABLED reason=puppet_state_in_loaded_save role='..role..' (this save came from a puppet client; do not use it as an authority world)')
 end
end)

local function frame()
 if P.failed then return end
 local now=GetCurRealTime()
 if not C.IsPlayerValid() then return end
 if not P.startedAt then
  P.startedAt=now;P.nextCensus=now+5;P.rereadAt=now+P.reread_after
  loadtime(nil)
  AddUITriggeredEvent('X4LAN_Puppet','Query')
 end
 if active and not P.acked and now>=(P.nextActivate or 0) then
  P.nextActivate=now+P.activate_every;AddUITriggeredEvent('X4LAN_Puppet','Activate')
 end
 if P.rereadAt and now>=P.rereadAt then P.rereadAt=nil;loadtime('reread=1') end
 if now>=P.nextCensus then
  P.nextCensus=now+P.census_every;census();AddUITriggeredEvent('X4LAN_Puppet','Query')
 end
end
SetScript('onUpdate',function()
 local ok,err=pcall(frame)
 if not ok then
  P.failed=true;log('PUPPET STOPPED '..tostring(err))
  if active then log('FEATURE puppet_client DISABLED reason=adapter_stopped') end
 end
end)
