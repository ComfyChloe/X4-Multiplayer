-- Opt-in, dev-only WINDOWS readiness probe for the exported projectile switch that
-- ui/shot_visual.lua uses (dev/VISUAL-SWITCH-PROBE.txt). Never packaged: a native harness copies it
-- into ui/ (after the production modules) and dev/visual_switch_probe.xml into md/, in the owned
-- isolated test profile only. On ONE original gun of the player's controlled ship it switches the
-- launched bullet stock -> harmless -> stock. Every write is read back at once and again one MD
-- tick later; the gun's slot, ID, weapon macro, absence of an equipment mod and weapon-group
-- memberships, and every other gun's launched bullet, must stay unchanged. Only a gun this probe
-- switched is ever restored. Observation, not acceptance; no firing, no co-op connection.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.visual_switch_probe or cfg.test_profile~='45841' then return end
local ffi=require('ffi');local C=ffi.C
for _,decl in ipairs({'const char* GetWeaponLaunchedMacro(UniverseID);','void SetWeaponLaunchedMacro(UniverseID,const char*);',
 'size_t GetNumUpgradeSlots(UniverseID,const char*,const char*);','UniverseID GetUpgradeSlotCurrentComponent(UniverseID,const char*,size_t);',
 'UniverseID GetPlayerControlledShipID(void);','bool IsComponentOperational(UniverseID);',
 'typedef struct { bool primary; uint32_t idx; } UIWeaponGroup;','uint32_t GetNumWeaponGroupsByWeapon(UniverseID,UniverseID);',
 'uint32_t GetWeaponGroupsByWeapon(UIWeaponGroup*,uint32_t,UniverseID,UniverseID);'}) do pcall(ffi.cdef,decl) end
-- Same stock/harmless pairs as ui/shot_visual.lua.
local BULLETS={
 weapon_ter_s_plasma_01_mk1_macro={'bullet_ter_s_plasma_01_mk1_macro','x4lan_visual_plasma_bullet_macro'},
 weapon_gen_s_laser_01_mk1_macro={'bullet_gen_s_laser_01_mk1_macro','x4lan_visual_pulse_mk1_bullet_macro'},
 weapon_gen_s_laser_01_mk2_macro={'bullet_gen_s_laser_01_mk2_macro','x4lan_visual_pulse_mk2_bullet_macro'},
}
local p={stage=0}
local function log(v) DebugError('[X4LAN] VISUAL_SWITCH_PROBE '..v) end
local function launched(id) local s=C.GetWeaponLaunchedMacro(id);assert(s~=nil,'no launched macro');return ffi.string(s) end
local function groups(ship,id)
 local n=tonumber(C.GetNumWeaponGroupsByWeapon(ship,id));assert(n and n>=0 and n<=8,'group bounds')
 local raw=ffi.new('UIWeaponGroup[?]',math.max(n,1));n=tonumber(C.GetWeaponGroupsByWeapon(raw,n,ship,id))
 local out={};for i=0,n-1 do out[#out+1]=(raw[i].primary and 'P' or 'S')..tonumber(raw[i].idx) end
 table.sort(out);return table.concat(out,',')
end
-- Every installed gun of the ship, by physical slot (empty slots are skipped, never renumbered).
local function guns(ship)
 local n=tonumber(C.GetNumUpgradeSlots(ship,'','weapon'));assert(n and n>=1 and n<=32,'slot bounds')
 local out={}
 for slot=1,n do
  local id=C.GetUpgradeSlotCurrentComponent(ship,'weapon',slot)
  if id~=0 then
   assert(C.IsComponentOperational(id),'weapon unavailable')
   out[#out+1]={slot=slot,id=id,macro=GetComponentData(ConvertStringTo64Bit(tostring(id)),'macro'),
    mod=C.GetInstalledWeaponMod(id,ffi.new('UIWeaponMod',{})),groups=groups(ship,id),launched=launched(id)}
  end
 end
 return out
end
local function check(want)
 assert(C.GetPlayerControlledShipID()==p.ship,'controlled ship changed')
 local now=guns(p.ship);assert(#now==#p.base,'gun count changed')
 for i,w in ipairs(now) do
  local b=p.base[i]
  assert(w.slot==b.slot and w.id==b.id and w.macro==b.macro,'gun identity changed at slot '..b.slot)
  assert(w.mod==b.mod and w.groups==b.groups,'mod or weapon groups changed at slot '..b.slot..': '..b.groups..' -> '..w.groups)
  local expected=i==p.index and want or b.launched
  assert(w.launched==expected,'slot '..w.slot..' launched '..w.launched..' expected '..expected)
 end
end
local function switch(macro)
 C.SetWeaponLaunchedMacro(p.base[p.index].id,macro);p.switched=true;check(macro)
end
local function step()
 p.stage=p.stage+1
 if p.stage==1 then
  p.ship=C.GetPlayerControlledShipID();assert(p.ship~=0,'no controlled ship')
  p.base=guns(p.ship)
  for i,w in ipairs(p.base) do
   local pair=BULLETS[w.macro]
   if not p.index and pair and not w.mod and w.launched==pair[1] then p.index,p.pair=i,pair end
  end
  assert(p.index,'no unmodified original stock gun')
  local w=p.base[p.index]
  log('base slot='..w.slot..' id='..tostring(w.id)..' weapon='..w.macro..' groups='..w.groups..' guns='..#p.base)
  switch(p.pair[2]);log('harmless written and read back')
 elseif p.stage==2 then
  check(p.pair[2]);switch(p.pair[1]);log('harmless persisted one tick; stock written and read back')
 else
  check(p.pair[1]);p.done=true;log('PASS stock->harmless->stock, same slot/ID/weapon/no mod/groups, other guns unchanged')
 end
end
local function fail(err)
 p.done=true;log('FAIL stage='..p.stage..' '..tostring(err))
 if p.switched then
  local ok,res=pcall(function()
   local id=p.base[p.index].id
   if launched(id)==p.pair[2] then C.SetWeaponLaunchedMacro(id,p.pair[1]) end
   return launched(id)==p.pair[1]
  end)
  log('CLEANUP '..((ok and res) and 'stock restored' or 'FAILED '..tostring(res)))
 end
end
RegisterEvent('X4LAN_VisualSwitchProbe.Tick',function()
 if p.done then return end
 local ok,err=pcall(step);if not ok then fail(err) end
end)
log('armed')
