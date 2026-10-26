-- Pure Lua session and interpolation logic; tested independently of the engine.
X4LAN_CLIENT = {}
local CONNECTION_TIMEOUT = 30 -- Matches the relay's player lease timeout.
-- Datagrams drained per update. The relay sends a client about 1000 packets/s in a busy
-- sector (universe motion, identity, NPC health, co-op frames); at 20-45 FPS a cap of 32
-- per frame queued them in the OS buffer (seconds of added latency, then loss).
local DRAIN = 256
local function split(packet)
    if #packet > 768 or packet:find("[^%g]") then return {} end
    local fields = {}
    for value in (packet .. "|"):gmatch("(.-)|") do fields[#fields+1] = value end
    return fields
end
local function hex(value)
    return value and #value == 32 and not value:find("[^a-f0-9]")
end
local function seq(value)
    if not value or not value:match("^%d+$") then return nil end
    local n = tonumber(value)
    if n > 2147483647 then return nil end
    return n
end
local function finite(value, bound)
    local n = tonumber(value)
    if not n or n ~= n or math.abs(n) > bound then return nil end
    return n
end
local function angle(a, b, t)
    local delta = (b - a + math.pi) % (2 * math.pi) - math.pi
    return a + delta * t
end
function X4LAN_CLIENT.new(config, transport, nonce, log, resume)
    assert(hex(config.token) and hex(nonce), "token/nonce must be 32 hex characters")
    assert(config.slot == "p1" or config.slot == "p2", "slot must be p1 or p2")
    for _, key in ipairs({"build", "sector", "bubble"}) do
        assert(type(config[key]) == "string" and #config[key] <= 96 and config[key]:match("^[%w_.-]+$"), "invalid " .. key)
    end
    local self = {config=config, transport=transport, nonce=nonce, log=log or function() end,
        lease=nil, sequence=resume and resume.sequence or 0, tick=resume and resume.tick or -1,
        resume=resume, lastHello=-math.huge, lastSend=-math.huge,
        lastHost=-math.huge, samples={}, remote=nil, remoteSeq=-1}
    function self:receive(packet, now)
        local f = split(packet)
        if f[2] ~= "1" then return end
        if f[1] == 'RESET' and #f == 3 and f[3] == self.nonce and self.resume and not self.lease then
            self.reset=true;return
        end
        if f[1] == "WELCOME" and #f == 5 and f[3] == self.nonce and hex(f[4]) and f[5] == "20" then
            if self.resume and f[4] ~= self.resume.lease then return end
            if self.lease and self.lease ~= f[4] then return end
            if self.lease ~= f[4] then
                self.lease = f[4]
                self.samples, self.remote, self.remoteSeq = {}, nil, -1
                self.log((self.resume and 'reconnected to relay as ' or 'connected to relay as ') .. config.slot)
            end
            self.lastHost = now
            return
        end
        if not self.lease or f[3] ~= self.lease then return end
        if self.receiveExtension and self:receiveExtension(f, now) then return end
        local tick = seq(f[4])
        if not tick or tick <= self.tick then return end
        if f[1] == "EMPTY" and #f == 4 then
            self.samples, self.tick, self.lastHost = {}, tick, now
            return
        end
        if f[1] ~= "PEER" or #f ~= 12 or not hex(f[5]) then return end
        local remoteSeq = seq(f[6])
        if not remoteSeq then return end
        local pose = {}
        for i = 1, 6 do
            pose[i] = finite(f[i+6], i <= 3 and 50000 or 2*math.pi)
            if not pose[i] then return end
        end
        self.tick, self.lastHost = tick, now
        if self.remote ~= f[5] then
            self.remote, self.remoteSeq, self.samples = f[5], -1, {}
        end
        if remoteSeq <= self.remoteSeq then return end
        self.remoteSeq = remoteSeq
        self.samples[#self.samples+1] = {time=now, pose=pose}
        while #self.samples > 8 do table.remove(self.samples, 1) end
    end
    function self:update(now, pose)
        self.startedAt = self.startedAt or now
        if not self.lease and not self.resume and now-self.startedAt >= CONNECTION_TIMEOUT then
            return false, 'initial-handshake-timeout'
        end
        if self.reset then return false, 'relay-reset' end
        if self.lease and now - self.lastHost >= CONNECTION_TIMEOUT then return false end
        if now - self.lastHello >= 1 then
            local hello = {"HELLO", "1", config.token, config.build,
                config.sector, config.bubble, config.slot, self.nonce}
            if self.resume and not self.lease then
                hello[1],hello[8],hello[9],hello[10]='RESUME',self.resume.nonce,self.resume.lease,self.nonce
            end
            self.transport:send(table.concat(hello, '|'))
            self.lastHello = now
        end
        local drained = 0
        for _ = 1, DRAIN do
            local packet = self.transport:receive()
            if not packet then break end
            drained = drained + 1
            self:receive(packet, now)
        end
        self.drained, self.drainedMax = drained, math.max(self.drainedMax or 0, drained)
        if self.reset then return false, 'relay-reset' end
        if self.lease and now - self.lastSend >= 0.05 then
            self.sequence = self.sequence + 1
            if self.sequence > 2147483647 then return false end
            local packet = {(pose and "STATE" or "IDLE"), "1", self.lease, tostring(self.sequence)}
            if pose then
                for i = 1, 6 do packet[#packet+1] = string.format("%.9g", pose[i]) end
            end
            self.transport:send(table.concat(packet, "|"))
            self.lastSend = now
        end
        return true
    end
    function self:render(now)
        local samples = self.samples
        if #samples == 0 or now - samples[#samples].time > 0.75 then return nil end
        local target = now - 0.10
        local a, b = samples[1], samples[#samples]
        for i = 2, #samples do
            if samples[i].time >= target then b = samples[i]; break end
            a = samples[i]
        end
        local t = b.time > a.time and math.max(0, math.min(1, (target-a.time)/(b.time-a.time))) or 0
        local pose = {}
        for i = 1, 6 do
            pose[i] = i <= 3 and (a.pose[i] + (b.pose[i]-a.pose[i])*t) or angle(a.pose[i], b.pose[i], t)
        end
        return pose
    end
    return self
end
