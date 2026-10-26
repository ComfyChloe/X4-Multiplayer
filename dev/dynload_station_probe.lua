-- Isolated native diagnostic; generated extension supplies DLCOR_EXPECTED.
local ffi = require('ffi')
for _, decl in ipairs({
  'const char* GetSaveFolderPath(void);', 'bool IsPlayerValid(void);',
  'uint32_t GetNumStationModules(uint64_t, bool, bool);',
  'uint32_t GetStationModules(uint64_t*, uint32_t, uint64_t, bool, bool);',
  'bool IsComponentOperational(uint64_t);',
}) do pcall(ffi.cdef, decl) end
local C = ffi.C
local expected = assert(DLCOR_EXPECTED)
local started, failed, finished, began = false, false, false, nil
local pending, seen, stationIds = {}, {}, {}
local function log(s) DebugError('DLCOR ' .. s) end
local function fail(s) if not failed then log('FAIL ' .. s) end; failed = true end
local function atom(x) return type(x) == 'string' and x:match('^[%w_]+$') end
local function uid(x)
  local s = tostring(x):gsub('ULL$', '')
  if not s:match('^%d+$') then error('nondecimal UID') end
  return s, ConvertStringTo64Bit(s)
end
local function sample(p, phase)
  local count = tonumber(C.GetNumStationModules(p.id, false, false))
  if count < 1 or count > 4096 then error('module count out of bounds') end
  local buf = ffi.new('uint64_t[?]', count)
  local n = tonumber(C.GetStationModules(buf, count, p.id, false, false))
  if n ~= count then error('module count changed during read') end
  local macros, ids, rows = {}, {}, {}
  for i = 0, n - 1 do
    local id, nativeId = uid(buf[i])
    if ids[id] then error('duplicate module ID') end
    ids[id] = true
    local macro = GetComponentData(nativeId, 'macro')
    if not atom(macro) then error('missing module macro') end
    local operational = C.IsComponentOperational(nativeId)
    if not operational then error('non-operational module') end
    macros[macro] = (macros[macro] or 0) + 1
    rows[#rows + 1] = id .. ':' .. macro
  end
  local total = 0
  for macro, wanted in pairs(p.expect.macros) do
    total = total + wanted
    if macros[macro] ~= wanted then error('module macro mismatch ' .. macro) end
  end
  if total ~= n then error('unexpected module macros/count') end
  table.sort(rows)
  local fingerprint = table.concat(rows, ',')
  if p.first and p.first ~= fingerprint then error('module identities changed') end
  p.first = fingerprint
  log(string.format('SAMPLE index=%d plan=%s station=%s phase=%s age=%.3f modules=%d identities=%s',
      p.index, p.expect.plan, p.key, phase, GetCurRealTime() - p.t, n, fingerprint))
end
RegisterEvent('DLCOR.Station', function(_, value)
  if failed or finished then return end
  local ok, err = pcall(function()
    if type(value) ~= 'string' then error('station event is not scalar text') end
    local rawIndex, plan, object = value:match('^(%d+)|([%w_]+)|(0x[%da-fA-F]+)$')
    local index = tonumber(rawIndex)
    if not object or #object > 18 then error('malformed station payload') end
    -- MD component text is hexadecimal. Keep the entire ID in uint64 arithmetic.
    local nativeObject = ffi.new('uint64_t', 0)
    for digit in object:sub(3):gmatch('.') do nativeObject = nativeObject * 16 + tonumber(digit, 16) end
    local row = index and expected[index]
    if not started or not row or seen[index] or plan ~= row.plan then error('station correlation mismatch') end
    local key, id = uid(nativeObject)
    if key == '0' or stationIds[key] then error('duplicate/zero station ID') end
    stationIds[key], seen[index] = true, true
    pending[#pending + 1] = {index=index, expect=row, key=key, id=id, t=GetCurRealTime()}
    log(string.format('CREATED index=%d plan=%s station=%s', index, plan, key))
  end)
  if not ok then fail(tostring(err)) end
end)
SetScript('onUpdate', function()
  if failed or finished then return end
  local ok, err = pcall(function()
    if not C.IsPlayerValid() or GetCurTime() < 2 then return end
    if not started then
      local path = ffi.string(C.GetSaveFolderPath()):gsub('\\', '/')
      local profile = path:match('/(458%d%d)/save/?$')
      local allowed = {['45850']=true,['45851']=true,['45853']=true,['45854']=true,
                       ['45855']=true,['45856']=true,['45857']=true,['45858']=true}
      if not allowed[profile] then error('not an approved native lane profile') end
      started, began = true, GetCurRealTime()
      log('START profile=' .. profile .. ' stations=' .. #expected)
      AddUITriggeredEvent('DLCOR', 'Start')
      return
    end
    if GetCurRealTime() - began > 60 then error('station evidence timeout') end
    for _, p in ipairs(pending) do
      local age = GetCurRealTime() - p.t
      if not p.first and age >= 0.1 then sample(p, 'early'); p.firstAt = GetCurRealTime() end
      if not p.done and p.firstAt and GetCurRealTime() - p.firstAt >= 5 then sample(p, 'late'); p.done = true end
    end
    if #pending == #expected then
      for _, p in ipairs(pending) do if not p.done then return end end
      finished = true
      log('DONE stations=' .. #pending .. ' continuity=stable modules=exact')
    end
  end)
  if not ok then fail(tostring(err)) end
end)
