-- Station build intents (BUILDREQ): wire contract and pure validation helpers.
-- Mirrors x4lan/build.py. No engine calls here, so the authority checks run under tests.
-- The plan payload is X4's own construction plan XML (<plan><entry ...>), in one strict
-- canonical spelling: the encoder below writes it and the parser accepts nothing else.
X4LAN_BUILD_PROTOCOL={}
local B=X4LAN_BUILD_PROTOCOL
B.maxEntries=512;B.maxBytes=131072;B.partSize=560;B.maxParts=256;B.maxGroups=64
B.kinds={EXPAND=true,REMOVEPLOT=true,RECYCLE=true}
B.final={ACCEPTED=true,REFUSED=true,UNCERTAIN=true,STALE=true,BUSY=true}
B.status={RECEIVING=true,PENDING=true,ACCEPTED=true,REFUSED=true,UNCERTAIN=true,STALE=true,BUSY=true}
local function split(s,sep)
 local out={};for v in (s..sep):gmatch('(.-)'..sep) do out[#out+1]=v end;return out
end
B.split=split
function B.integer(s,limit)
 if type(s)~='string' or not s:match('^%d+$') or #s>13 then return nil end
 local n=tonumber(s);if not n or n>(limit or 2147483647) then return nil end;return n
end
function B.atom(s,macro)
 return type(s)=='string' and #s>0 and #s<=96 and not s:find('[^a-zA-Z0-9_.-]') and
        (not macro or s:sub(-6)=='_macro')
end
function B.hex(s) return type(s)=='string' and #s==32 and not s:find('[^a-f0-9]') end
function B.reason(s) return type(s)=='string' and #s>0 and #s<=64 and not s:find('[^a-z0-9_:.-]') end
-- adler32, identical to zlib.adler32 used by the relay.
function B.digest(s)
 local a,b=1,0
 for i=1,#s do a=(a+s:byte(i))%65521;b=(b+a)%65521 end
 return b*65536+a
end
-- Loadout group paths are engine connection paths such as "..\con_turret_01".
local function path(s) return type(s)=='string' and #s>0 and #s<=128 and not s:find('[^a-zA-Z0-9_.\\/-]') end
local function num(v,limit,fmt)
 if type(v)~='number' or v~=v or math.abs(v)>limit then return nil end
 local text=string.format(fmt,v);if text=='-0.000' or text=='-0.0000' then text=text:sub(2) end
 return text
end
local function numtext(s,limit)
 if type(s)~='string' or not s:match('^%-?%d+%.%d+$') or #s>24 then return nil end
 local v=tonumber(s);if not v or math.abs(v)>limit then return nil end;return v
end
B.positionLimit=1000000;B.angleLimit=360
-- entries: {index,macro,connection,pred,predconnection,pos={x,y,z},rot={yaw,pitch,roll} (degrees),
-- groups={{kind='shields'|'turrets',macro,path,group,exact}}}
function B.encode(id,entries)
 if not B.atom(id) or type(entries)~='table' or #entries>B.maxEntries then return nil,'plan_size' end
 local out={'<plan id="',id,'" name="',id,'">'}
 for i,e in ipairs(entries) do
  if e.index~=i or not B.atom(e.macro,true) then return nil,'plan_entry' end
  local conn=e.connection or ''
  if conn~='' and not B.atom(conn) then return nil,'plan_connection' end
  out[#out+1]='<entry index="'..i..'" macro="'..e.macro..'"'..(conn~='' and (' connection="'..conn..'"') or '')..'>'
  local pred=e.pred or 0
  if pred~=0 then
   if type(pred)~='number' or pred%1~=0 or pred<1 or pred>#entries or pred==i or conn=='' or not B.atom(e.predconnection) then return nil,'plan_predecessor' end
   out[#out+1]='<predecessor index="'..pred..'" connection="'..e.predconnection..'"/>'
  end
  local p,r=e.pos or {},e.rot or {}
  local x,y,z=num(p[1],B.positionLimit,'%.3f'),num(p[2],B.positionLimit,'%.3f'),num(p[3],B.positionLimit,'%.3f')
  local yaw,pitch,roll=num(r[1],B.angleLimit,'%.4f'),num(r[2],B.angleLimit,'%.4f'),num(r[3],B.angleLimit,'%.4f')
  if not (x and y and z and yaw and pitch and roll) then return nil,'plan_offset' end
  out[#out+1]='<offset><position x="'..x..'" y="'..y..'" z="'..z..'"/><rotation yaw="'..yaw..'" pitch="'..pitch..'" roll="'..roll..'"/></offset>'
  local groups=e.groups or {}
  if #groups>B.maxGroups then return nil,'plan_loadout_size' end
  if #groups>0 then
   out[#out+1]='<upgrades><groups>'
   for _,g in ipairs(groups) do
    if (g.kind~='shields' and g.kind~='turrets') or not B.atom(g.macro,true) or not path(g.path) or not B.atom(g.group) or
       type(g.exact)~='number' or g.exact%1~=0 or g.exact<1 or g.exact>1000 then return nil,'plan_loadout' end
    out[#out+1]='<'..g.kind..' macro="'..g.macro..'" path="'..g.path..'" group="'..g.group..'" exact="'..g.exact..'"/>'
   end
   out[#out+1]='</groups></upgrades>'
  end
  out[#out+1]='</entry>'
 end
 out[#out+1]='</plan>'
 local xml=table.concat(out)
 if #xml>B.maxBytes then return nil,'plan_size' end
 return xml
end
-- Strict parse: re-encoding the parsed structure must reproduce the input exactly.
function B.parse(xml)
 if type(xml)~='string' or #xml==0 or #xml>B.maxBytes or xml:find('[^%g ]') or xml:find('|',1,true) then return nil,'plan_encoding' end
 local id,body=xml:match('^<plan id="([^"]*)" name="[^"]*">(.*)</plan>$')
 if not id or not B.atom(id) then return nil,'plan_header' end
 local entries={};local pos=1
 while pos<=#body do
  local s,e,attrs,inner=body:find('^<entry ([^>]*)>(.-)</entry>',pos)
  if not s then return nil,'plan_entry' end
  local index,macro,conn=attrs:match('^index="(%d+)" macro="([^"]*)"(.*)$')
  local connection=''
  if conn and conn~='' then connection=conn:match('^ connection="([^"]*)"$');if not connection then return nil,'plan_entry' end end
  local en={index=tonumber(index),macro=macro,connection=connection,pred=0,groups={}}
  local rest=inner
  local pi,pc,after=rest:match('^<predecessor index="(%d+)" connection="([^"]*)"/>(.*)$')
  if pi then en.pred=tonumber(pi);en.predconnection=pc;rest=after end
  local x,y,z,yaw,pitch,roll,tail=rest:match('^<offset><position x="([^"]*)" y="([^"]*)" z="([^"]*)"/><rotation yaw="([^"]*)" pitch="([^"]*)" roll="([^"]*)"/></offset>(.*)$')
  if not x then return nil,'plan_offset' end
  en.pos={numtext(x,B.positionLimit),numtext(y,B.positionLimit),numtext(z,B.positionLimit)}
  en.rot={numtext(yaw,B.angleLimit),numtext(pitch,B.angleLimit),numtext(roll,B.angleLimit)}
  for i=1,3 do if not en.pos[i] or not en.rot[i] then return nil,'plan_offset' end end
  if tail~='' then
   local groups=tail:match('^<upgrades><groups>(.*)</groups></upgrades>$')
   if not groups then return nil,'plan_loadout' end
   for kind,m,p,g,exact in groups:gmatch('<(%a+) macro="([^"]*)" path="([^"]*)" group="([^"]*)" exact="(%d+)"/>') do
    en.groups[#en.groups+1]={kind=kind,macro=m,path=p,group=g,exact=tonumber(exact)}
   end
  end
  entries[#entries+1]=en;pos=e+1
  if #entries>B.maxEntries then return nil,'plan_size' end
 end
 local again,reason=B.encode(id,entries)
 if not again then return nil,reason end
 if again~=xml then return nil,'plan_not_canonical' end
 return {id=id,entries=entries}
end
function B.parts(xml)
 local out={};for i=1,math.ceil(#xml/B.partSize) do out[i]=xml:sub((i-1)*B.partSize+1,i*B.partSize) end;return out
end
-- plot: {sx,sy,sz,cx,cy,cz} metres (size and centre offset), or nil.
function B.plotText(p)
 if not p then return '-' end
 local out={}
 for i=1,6 do
  local v=p[i];if type(v)~='number' or v~=v or math.abs(v)>1000000 or (i<=3 and v<0) then return nil end
  out[i]=string.format('%d',math.floor(v+.5))
 end
 return table.concat(out,',')
end
function B.plotParse(s)
 if s=='-' then return false end
 local f=split(s or '',',');if #f~=6 then return nil end
 local p={}
 for i=1,6 do
  if not f[i]:match('^%-?%d+$') or #f[i]>8 then return nil end
  p[i]=tonumber(f[i]);if i<=3 and p[i]<0 then return nil end
 end
 return p
end
-- Relative ExtendBuildPlot change that turns the current plot into the wanted one.
function B.plotDelta(want,have)
 local pos,neg,change={},{},false
 for i=1,3 do
  local wp,wn=want[i]/2+want[i+3],want[i]/2-want[i+3]
  local hp,hn=have[i]/2+have[i+3],have[i]/2-have[i+3]
  pos[i],neg[i]=wp-hp,wn-hn
  if math.abs(pos[i])>=1 or math.abs(neg[i])>=1 then change=true end
 end
 return change and {pos=pos,neg=neg} or nil
end
-- Header shared by guest request and authority execution (fields after the routing prefix).
-- kind|code|macro|sector|plot|option|entries|bytes|parts|digest
function B.header(f,base)
 local kind,code,macro,sector,plot,option=f[base],f[base+1],f[base+2],f[base+3],f[base+4],f[base+5]
 local entries,bytes,parts,digest=B.integer(f[base+6],B.maxEntries),B.integer(f[base+7],B.maxBytes),B.integer(f[base+8],B.maxParts),B.integer(f[base+9],4294967295)
 if not B.kinds[kind] or not B.atom(code) or not B.atom(macro,true) or not B.atom(sector,true) then return nil end
 if not entries or not bytes or not parts or not digest then return nil end
 local p=B.plotParse(plot);if p==nil then return nil end
 local h={kind=kind,code=code,macro=macro,sector=sector,plot=p or nil,plotText=plot,entries=entries,bytes=bytes,parts=parts,digest=digest,option=option}
 if kind=='EXPAND' then
  if entries<1 or bytes<1 or parts~=math.ceil(bytes/B.partSize) then return nil end
  if option~='-' then h.budget=B.integer(option);if not h.budget then return nil end end
 else
  if entries~=0 or bytes~=0 or parts~=0 or digest~=1 or plot~='-' then return nil end
  if kind=='REMOVEPLOT' then if option~='0' and option~='1' then return nil end;h.removeStorage=option=='1'
  elseif option~='-' then return nil end
 end
 return h
end
-- Exact comparison of an engine-imported plan with the requested one. Engine offsets
-- are radians (UIPosRot); the plan file carries degrees.
function B.samePlan(want,got)
 if #want~=#got then return false,'plan_mismatch:count' end
 for i,w in ipairs(want) do
  local g=got[i]
  if g.macro~=w.macro or (g.connection or '')~=(w.connection or '') or (g.pred or 0)~=(w.pred or 0) or
     ((w.pred or 0)~=0 and g.predconnection~=w.predconnection) then return false,'plan_mismatch:entry'..i end
  for k=1,3 do
   if math.abs(g.pos[k]-w.pos[k])>1 then return false,'plan_mismatch:position'..i end
   local d=math.abs(g.rot[k]-math.rad(w.rot[k]))%(2*math.pi);if d>math.pi then d=2*math.pi-d end
   if d>.002 then return false,'plan_mismatch:rotation'..i end
  end
 end
 return true
end
-- Owner check. Today every human player shares faction "player"; per-player factions
-- (player_factions) replace factionFor without changing callers.
function B.factionFor(slot)
 if slot=='p1' or slot=='p2' then return 'player' end
 return nil
end
function B.ownerAllowed(slot,owner)
 local faction=B.factionFor(slot);return faction~=nil and owner==faction
end
-- Money plan: build storage is set to the requested budget; plot cost is paid on top.
function B.funds(playerMoney,storageMoney,budget,plotPrice)
 local transfer=budget and (budget-storageMoney) or 0
 local need=math.max(transfer,0)+math.max(plotPrice or 0,0)
 if need>playerMoney then return nil,'funds' end
 return {transfer=transfer,plot=math.max(plotPrice or 0,0)}
end
