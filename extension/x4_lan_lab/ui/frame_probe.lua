-- Diagnostic only (config frame_probe=true): measures how often the UI onUpdate runs
-- against real time (GetCurRealTime) and game time (GetCurTime), and counts datagrams
-- through X4LAN_TRANSPORT. It never calls game-changing functions; it only logs.
-- Purpose: quantify UI/onUpdate starvation (unfocused, minimized, loading, saving)
-- before and after a per-simulation-frame network driver (design pending).
X4LAN_FRAME_PROBE = {}
local P = X4LAN_FRAME_PROBE

-- options: window (s of real time per summary), stall (s of real time between two
-- calls that count as a stall), slow (s between calls that count as a slow frame).
function P.new(options)
    options = options or {}
    local self = {window=options.window or 5, stall=options.stall or 0.5,
        slow=options.slow or 0.1, sent=0, received=0}
    local w
    local function reset(real, game)
        w = {real=real, game=game, calls=0, maxGap=0, slow=0, paused=0, zeroDt=0,
            sent=self.sent, received=self.received}
    end
    -- Returns zero, one or two log lines (stall first, then the window summary).
    function self.sample(real, game, paused)
        local lines = {}
        if type(real) ~= "number" or type(game) ~= "number" or real ~= real or game ~= game then
            return lines
        end
        if not self.lastReal or real < self.lastReal then
            -- First sample or a clock reset (new session/UI reload): start a fresh window.
            self.lastReal, self.lastGame = real, game
            reset(real, game)
            return lines
        end
        local gap, gameGap = real - self.lastReal, game - self.lastGame
        if gap >= self.stall then
            lines[#lines+1] = string.format(
                "[X4LAN] FRAME_PROBE stall realGap=%.3f gameGap=%.3f paused=%s",
                gap, gameGap, tostring(paused == true))
        end
        w.calls = w.calls + 1
        if gap > w.maxGap then w.maxGap = gap end
        if gap >= self.slow then w.slow = w.slow + 1 end
        if paused == true then w.paused = w.paused + 1 end
        if gameGap <= 0 then w.zeroDt = w.zeroDt + 1 end
        self.lastReal, self.lastGame = real, game
        local span = real - w.real
        if span >= self.window then
            local gameSpan = game - w.game
            lines[#lines+1] = string.format(
                "[X4LAN] FRAME_PROBE window real=%.3f game=%.3f calls=%d hz=%.2f ratio=%.3f " ..
                "maxGapMs=%.1f slow=%d paused=%d zeroDt=%d sent=%d received=%d",
                span, gameSpan, w.calls, w.calls / span, gameSpan / span, w.maxGap * 1000,
                w.slow, w.paused, w.zeroDt, self.sent - w.sent, self.received - w.received)
            reset(real, game)
        end
        return lines
    end
    -- Wraps a transport object; counts only, never changes payloads or order.
    function self.wrap(transport)
        local probe = self
        return {
            send=function(_, payload)
                local ok = transport:send(payload)
                if ok then probe.sent = probe.sent + 1 end
                return ok
            end,
            receive=function()
                local packet = transport:receive()
                if packet then probe.received = probe.received + 1 end
                return packet
            end,
            close=function() return transport:close() end,
        }
    end
    return self
end

local cfg = X4LAN_CONFIG
if not cfg or cfg.frame_probe ~= true or not SetScript or not GetCurRealTime or not GetCurTime then return end
local probe = P.new({window=tonumber(cfg.frame_probe_window) or 5})
P.active = probe
if X4LAN_TRANSPORT and X4LAN_TRANSPORT.open then
    local open = X4LAN_TRANSPORT.open
    X4LAN_TRANSPORT.open = function(host, port)
        return probe.wrap(open(host, port))
    end
end
local paused = function() return false end
pcall(function()
    local ffi = require("ffi")
    pcall(ffi.cdef, "bool IsGamePaused(void);")
    local C = ffi.C
    paused = function() return C.IsGamePaused() end
end)
local function onUpdate()
    local ok, lines = pcall(probe.sample, GetCurRealTime(), GetCurTime(), paused())
    if not ok then return end
    for _, line in ipairs(lines) do DebugError(line) end
end
SetScript("onUpdate", onUpdate)
DebugError("[X4LAN] FRAME_PROBE armed window=" .. probe.window .. " stall=" .. probe.stall)
