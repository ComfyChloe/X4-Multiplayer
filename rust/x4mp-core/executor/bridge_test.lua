-- Offline test of x4mp_core_bridge.lua with LuaJIT (no game).
--
--   luajit bridge_test.lua x4mp_core_bridge.lua
--       fake ffi plays the Rust side: batch decoding, the MD event table, results path.
--   LD_PRELOAD=.../libx4mp_agent.so luajit bridge_test.lua x4mp_core_bridge.lua real
--       real LuaJIT ffi against the real agent exports (no core installed outside X4,
--       so the bridge must see "disabled" and send nothing).

local bridgePath, mode = arg[1], arg[2]
local events, results, scripts, registered = {}, {}, {}, {}
DebugError = function(m) print("DEBUG " .. tostring(m)) end
AddUITriggeredEvent = function(screen, control, t) events[#events + 1] = { screen, control, t } end
RegisterEvent = function(name, f) registered[name] = f end
SetScript = function(kind, f) scripts[kind] = f end

if mode ~= "real" then
    local pending = "X4MPQ1\n7\tdebug_text\thello%09world %25\n8\tspawn\tx\n"
    local fake = {
        cdef = function() end,
        new = function() return { data = "" } end,
        string = function(b, n) return b.data:sub(1, n) end,
        load = function() error("no dll") end,
    }
    fake.C = setmetatable({
        x4mp_core_md_take = function(buf, cap)
            assert(cap == 4096)
            local s = pending
            pending = nil
            if not s then return 0 end
            buf.data = s
            return #s
        end,
        x4mp_core_md_result = function(id, code)
            results[#results + 1] = { id, code }
            return 0
        end,
    }, { __index = function(_, k) error("missing " .. k) end })
    package.loaded.ffi = fake
end

dofile(bridgePath)
assert(scripts.onUpdate, "bridge did not register onUpdate")
scripts.onUpdate()

if mode == "real" then
    assert(#events == 0, "no batch expected without a core")
    assert(registered["X4MP_Core.Results"], "results handler missing")
    registered["X4MP_Core.Results"](nil, "1 ok;")
    print("PASS real ffi: agent exports resolved, core absent, nothing sent")
    return
end

assert(#events == 1, "one batch event")
local e = events[1]
assert(e[1] == "X4MP_Core" and e[2] == "Batch")
local t = e[3]
assert(t.n == 2 and t.id1 == "7" and t.kind1 == "debug_text" and t.arg1 == "hello\tworld %", "decoded action 1")
assert(t.id2 == "8" and t.kind2 == "spawn" and t.arg2 == "x", "decoded action 2")
registered["X4MP_Core.Results"](nil, "7 ok;8 unknown_kind;junk;9 weird;")
assert(#results == 2, "two results")
assert(results[1][1] == 7 and results[1][2] == 0 and results[2][1] == 8 and results[2][2] == 2)
scripts.onUpdate()
assert(#events == 1, "empty queue sends nothing")
print("PASS fake ffi: batch decoded into one MD event, results passed back")
