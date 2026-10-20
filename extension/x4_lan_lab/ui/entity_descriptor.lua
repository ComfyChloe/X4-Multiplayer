-- Original bounded equipment descriptor codec. No native calls or authority.
local M={MAX_BYTES=131072,MAX_ROWS=1024}
local function fail() error('Invalid ship equipment descriptor',0) end
local function fields(t,wanted)
 if type(t)~='table' then fail() end
 local n=0
 for k in pairs(t) do if not wanted[k] then fail() end;n=n+1 end
 local expected=0;for _ in pairs(wanted) do expected=expected+1 end
 if n~=expected then fail() end
end
local function array(t,limit)
 if type(t)~='table' then fail() end
 local n=0
 for k in pairs(t) do
  if type(k)~='number' or k%1~=0 or k<1 or k>limit then fail() end
  n=n+1
 end
 for i=1,n do if t[i]==nil then fail() end end
 return n
end
local function atom(v,macro)
 if type(v)~='string' or #v<1 or #v>96 or v:find('[^A-Za-z0-9_.%-]')
  or (macro and v:sub(-6)~='_macro') then fail() end
 return v
end
local function integer(v,lo,hi)
 if type(v)~='number' or v~=v or v<lo or v>hi or v%1~=0 then fail() end
 return v
end
local function hex(v)
 if type(v)~='string' or #v>192 then fail() end
 local out={}
 for i=1,#v do local b=v:byte(i);if b<32 or b>126 then fail() end;out[i]=string.format('%02x',b) end
 return table.concat(out)
end
local function less(a,b)
 for i=1,#a.key do if a.key[i]~=b.key[i] then return a.key[i]<b.key[i] end end
 return false
end
local function append_sorted(rows,items)
 table.sort(items,less)
 for i,item in ipairs(items) do
  if i>1 and not less(items[i-1],item) then fail() end
  rows[#rows+1]=item.row
 end
end
local function encode(d)
 -- false explicitly represents no thruster; nil would omit a required field.
 fields(d,{macro=true,owner=true,slots=true,groups=true,software=true,thruster=true})
 array(d.slots,M.MAX_ROWS);array(d.groups,M.MAX_ROWS);array(d.software,64)
 local rows={'X4SHIP1|'..atom(d.macro,true)..'|'..atom(d.owner)}
 local slots,groups={},{}
 for _,s in ipairs(d.slots) do
  fields(s,{kind=true,index=true,macro=true})
  if s.kind~='engine' and s.kind~='shield' and s.kind~='turret' and s.kind~='weapon' then fail() end
  integer(s.index,0,511)
  slots[#slots+1]={key={s.kind,s.index},row='S|'..s.kind..'|'..s.index..'|'..atom(s.macro,true)}
 end
 append_sorted(rows,slots)
 for _,g in ipairs(d.groups) do
  fields(g,{kind=true,path=true,group=true,macro=true,count=true})
  if g.kind~='shield' and g.kind~='turret' then fail() end
  local p,n=hex(g.path),hex(g.group)
  if g.path=='' or (g.path=='..' and g.group=='') then fail() end
  integer(g.count,1,512)
  groups[#groups+1]={key={g.kind,g.path,g.group},row=table.concat({'G',g.kind,p,n,atom(g.macro,true),g.count},'|')}
 end
 append_sorted(rows,groups)
 local software={};for _,v in ipairs(d.software) do software[#software+1]=atom(v) end
 table.sort(software)
 for i,v in ipairs(software) do if i>1 and v==software[i-1] then fail() end;rows[#rows+1]='W|'..v end
 rows[#rows+1]='V|'..(d.thruster==false and 'none' or atom(d.thruster,true))
 local result=table.concat(rows,'\n')
 if #rows>M.MAX_ROWS or #result>M.MAX_BYTES then fail() end
 return result
end
local function split(v,sep)
 local result,at={},1
 while true do
  local p=v:find(sep,at,true)
  if not p then result[#result+1]=v:sub(at);return result end
  result[#result+1]=v:sub(at,p-1);at=p+#sep
 end
end
local function number(v)
 if v~='0' and not v:match('^[1-9][0-9]*$') then fail() end
 if #v>9 then fail() end
 return tonumber(v)
end
local function unhex(v)
 if #v>384 or #v%2~=0 or v:find('[^0-9a-f]') then fail() end
 return (v:gsub('..',function(pair) return string.char(tonumber(pair,16)) end))
end
local function decode(v)
 if type(v)~='string' or #v<1 or #v>M.MAX_BYTES or v:find('[\128-\255]') then fail() end
 local lines=split(v,'\n')
 if #lines<2 or #lines>M.MAX_ROWS then fail() end
 local h=split(lines[1],'|')
 if #h~=3 or h[1]~='X4SHIP1' then fail() end
 local d={macro=h[2],owner=h[3],slots={},groups={},software={}}
 for i=2,#lines do
  local f=split(lines[i],'|')
  if f[1]=='S' and #f==4 then d.slots[#d.slots+1]={kind=f[2],index=number(f[3]),macro=f[4]}
  elseif f[1]=='G' and #f==6 then d.groups[#d.groups+1]={kind=f[2],path=unhex(f[3]),group=unhex(f[4]),macro=f[5],count=number(f[6])}
  elseif f[1]=='W' and #f==2 then d.software[#d.software+1]=f[2]
  elseif f[1]=='V' and #f==2 and d.thruster==nil then
   if f[2]=='none' then d.thruster=false else d.thruster=f[2] end
  else fail() end
 end
 if encode(d)~=v then fail() end
 return d
end
-- Untrusted data is rejected atomically; callers never receive partial rows.
function M.encode(d) local ok,v=pcall(encode,d);if ok then return v end;return nil end
function M.decode(v) local ok,d=pcall(decode,v);if ok then return d end;return nil end
X4LAN_ENTITY_DESCRIPTOR=M
return M
