-- x4mp-core MD action queue bridge (thin Lua glue, no per-object work).
--
-- Once per UI update (MoveWorker thread) it takes at most one batch of queued engine
-- actions from the Rust core, hands it to the MD executor cue as ONE event, and passes
-- the executor's results back. Wire format: rust/x4mp-core/src/mdq.rs.
--
--   Rust  --x4mp_core_md_take-->  Lua  --AddUITriggeredEvent('X4MP_Core','Batch',t)-->  MD
--   Rust <--x4mp_core_md_result-- Lua <--RegisterEvent('X4MP_Core.Results', "id st;")-- MD
--
-- Inert unless the core's C entry points exist:
--   Linux:   the LD_PRELOAD agent exports them into the global namespace (ffi.C).
--   Windows: set X4MP_CORE_WIN_DLL to the path of x4mp_core_win.dll before this file
--            runs; the bridge then also starts the core and drives one frame per update
--            (integration point 2 of x4mp-core-win; skip that when the hooks crate does).
--
-- Not installed: to use, list this file in extension/x4_lan_lab/ui.xml and copy
-- x4mp_core_executor.xml to extension/x4_lan_lab/md/. Not yet run natively.

local ffi = require("ffi")
local function log(message) DebugError("[X4MP_CORE] " .. tostring(message)) end

local function declare(text) pcall(ffi.cdef, text) end
declare("intptr_t x4mp_core_md_take(uint8_t* buf, size_t cap);")
declare("intptr_t x4mp_core_md_result(uint64_t id, int status);")

local lib, drive
if pcall(function() return ffi.C.x4mp_core_md_take end) then
    lib = ffi.C
elseif type(X4MP_CORE_WIN_DLL) == "string" then
    declare("int x4mp_core_win_start(void);")
    declare("int x4mp_core_win_frame(uint64_t frame, double game_time, double dt, int paused);")
    declare("intptr_t x4mp_core_log_take(uint8_t* buf, size_t cap);")
    declare("double GetCurrentGameTime(void);")
    declare("bool IsGamePaused(void);")
    local ok, loaded = pcall(ffi.load, X4MP_CORE_WIN_DLL)
    if ok then
        lib = loaded
        local started = tonumber(lib.x4mp_core_win_start())
        log("x4mp_core_win_start=" .. started)
        if started == 0 then drive = true end
    else
        log("FEATURE md_bridge DISABLED reason=dll_load_failed:" .. tostring(loaded))
    end
end
if not lib then return end

local CAP = 4096
local buf = ffi.new("uint8_t[?]", CAP)
local CODES = { ok = 0, failed = 1, unknown_kind = 2 }
local frame, lastTime = 0, nil

local function unescape(s)
    return (s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
end

local function driveFrame()
    local C = ffi.C
    local now = C.GetCurrentGameTime()
    frame = frame + 1
    local rc = lib.x4mp_core_win_frame(frame, now, lastTime and (now - lastTime) or 0, C.IsGamePaused() and 1 or 0)
    lastTime = now
    if rc < 0 then drive = false; log("x4mp_core_win_frame=" .. rc .. ", core withdrew itself") end
    while true do
        local n = tonumber(lib.x4mp_core_log_take(buf, CAP))
        if n <= 0 then break end
        log(ffi.string(buf, n))
    end
end

local function takeBatch()
    local n = tonumber(lib.x4mp_core_md_take(buf, CAP))
    if n <= 0 then return end
    local text = ffi.string(buf, n)
    local batch, count, header = {}, 0, true
    for line in text:gmatch("([^\n]*)\n") do
        if header then
            if line ~= "X4MPQ1" then log("bad batch header"); return end
            header = false
        else
            local id, kind, arg = line:match("^(%d+)\t([%w_]+)\t([^\t]*)$")
            if id then
                count = count + 1
                batch["id" .. count], batch["kind" .. count], batch["arg" .. count] = id, kind, unescape(arg)
            end
        end
    end
    batch.n = count
    if count > 0 then AddUITriggeredEvent("X4MP_Core", "Batch", batch) end
end

RegisterEvent("X4MP_Core.Results", function(_, results)
    for id, status in tostring(results):gmatch("(%d+) ([%a_]+);") do
        local code = CODES[status]
        if code then lib.x4mp_core_md_result(tonumber(id), code) end
    end
end)

SetScript("onUpdate", function()
    local ok, err = pcall(function()
        if drive then driveFrame() end
        takeBatch()
    end)
    if not ok then log(err) end
end)
