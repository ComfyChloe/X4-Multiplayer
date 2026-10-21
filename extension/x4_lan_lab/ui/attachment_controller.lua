-- Pure transition controller. Dispatch/readback are injected; nothing runs in X4
-- until a separately reviewed adapter registers it. Native execution must repeat
-- these ownership/reference/occupancy checks when consuming an asynchronous cue.
X4LAN_ATTACHMENT_CONTROLLER = {}
local D = X4LAN_ATTACHMENT_CONTROLLER
local handle = assert(X4LAN_SEED_DOCKS_LEDGER, 'saved dock ledger required').handle

local function integer(n, low, high)
    return type(n) == 'number' and n == math.floor(n) and n >= low and n <= high
end
local function time(n) return type(n) == 'number' and n == n and n >= 0 and n <= 1e12 end
local function identity(s)
    return type(s) == 'string' and #s > 0 and #s <= 1024 and not s:find('[^%g]')
end
local function state(mode, dock)
    return (mode == 'FREE' and dock == 0) or
        ((mode == 'EXTERNAL' or mode == 'STORED') and integer(dock, 1, 250000))
end
local function same(actual, desired)
    return actual.state == desired.state and actual.dock == desired.dock
end
local function copy(row)
    local out = {}; for k, v in pairs(row) do out[k] = v end; return out
end

function D.new(options)
    assert(type(options) == 'table' and type(options.epoch) == 'string' and #options.epoch == 32
        and options.epoch:match('^[a-f0-9]+$'), 'authority epoch required')
    assert(time(options.ttl) and options.ttl > 0 and options.ttl <= 60, 'invalid freshness limit')
    assert(time(options.timeout) and options.timeout > 0 and options.timeout <= 120, 'invalid transition timeout')
    assert(type(options.inspect) == 'function' and type(options.target) == 'function'
        and type(options.dispatch) == 'function', 'transition callbacks required')
    -- Copy configuration so changing the caller's options cannot widen a live gate.
    local epoch, ttl, timeout = options.epoch, options.ttl, options.timeout
    local inspect, target, dispatch = options.inspect, options.target, options.dispatch
    local jobs, marks, tickets = {}, {}, {}
    local count, last_time, stopped = 0, 0, false
    local self = {}

    local function clock(now)
        if not time(now) or now < last_time then stopped = true; return false end
        last_time = now; return not stopped
    end
    local function invalidate(job)
        if job.pending then tickets[job.pending] = nil; job.pending = nil end
    end
    local function failed(job, why)
        invalidate(job); job.failure = why; return 'failed', why
    end
    local function current(job, now)
        return job.desired and now - job.desired.received_at <= ttl
    end
    -- Idle expired intentions are disposable; replay marks and any uncertain or
    -- issued native operation are not. Reap only when admission needs capacity.
    local function reap(now)
        for ship, job in pairs(jobs) do
            if not job.failure and not job.awaiting and not current(job, now) then
                invalidate(job); jobs[ship] = nil; count = count - 1
            end
        end
    end
    local function read(ship, job)
        local ok, value = pcall(inspect, ship)
        if not ok or type(value) ~= 'table' or value.alive ~= true or value.protected ~= false
            or not handle(value.object) or not identity(value.identity) or not state(value.state, value.dock) then
            return nil, 'unresolved, dead or protected ship'
        end
        if job.desired and value.identity ~= job.desired.identity then return nil, 'ship identity changed' end
        if job.object_key and handle(value.object) ~= job.object_key then return nil, 'saved ship reference changed' end
        if value.state ~= 'FREE' and not handle(value.container) then return nil, 'unresolved native container' end
        job.object_key = handle(value.object)
        return copy(value)
    end
    local function destination(ship, wanted, actual)
        local ok, value = pcall(target, ship, wanted.dock, wanted.state)
        if not ok or type(value) ~= 'table' or value.dock ~= wanted.dock
            or not handle(value.object) or not handle(value.container) or not identity(value.identity)
            or value.alive ~= true or value.protected ~= false or value.compatible ~= true
            or value.available ~= true or value.cycle_free ~= true
            or handle(value.container) == handle(actual.object) then
            return nil, 'destination unavailable, incompatible, protected or cyclic'
        end
        if (wanted.state == 'EXTERNAL' and value.external ~= true)
            or (wanted.state == 'STORED' and value.storage ~= true) then
            return nil, 'destination type differs'
        end
        return copy(value)
    end

    function self:desire(ship, observation, now)
        if not clock(now) then return 'stopped' end
        if not integer(ship, 1, 249999) or type(observation) ~= 'table'
            or observation.epoch ~= epoch or not integer(observation.sequence, 1, 2147483647)
            or not state(observation.state, observation.dock) or not identity(observation.identity)
            or not time(observation.received_at) or observation.received_at > now then return 'invalid' end
        local wanted = {epoch=epoch, sequence=observation.sequence, state=observation.state,
            dock=observation.dock, identity=observation.identity, received_at=observation.received_at}
        local mark = marks[ship]
        if mark and wanted.sequence <= mark.sequence then
            if wanted.sequence < mark.sequence then return 'stale' end
            if wanted.state ~= mark.state or wanted.dock ~= mark.dock or wanted.identity ~= mark.identity then
                return 'conflict'
            end
            return 'duplicate' -- Never refresh the original observation's lifetime.
        end
        if now - wanted.received_at > ttl then return 'expired' end
        if mark and wanted.received_at < mark.received_at then return 'invalid' end
        local job = jobs[ship]
        if not job then
            if count >= 4096 then reap(now) end
            if count >= 4096 then return 'full' end
            job = {}; jobs[ship] = job; count = count + 1
        elseif job.failure then return 'failed', job.failure end
        invalidate(job)
        job.desired, marks[ship] = wanted, copy(wanted)
        return 'accepted'
    end

    function self:prepare(ship, now, paused)
        if not clock(now) then return 'stopped' end
        assert(type(paused) == 'boolean', 'pause state must be explicit')
        local job = jobs[ship]
        if not job then return 'unknown' end
        if job.failure then return 'failed', job.failure end
        if paused then invalidate(job); return 'paused' end
        local actual, why = read(ship, job)
        if not actual then return failed(job, why) end
        if job.awaiting then
            if same(actual, job.awaiting.goal) then job.awaiting = nil
            elseif now >= job.awaiting.deadline then return failed(job, 'native transition readback timed out')
            else return 'waiting' end
        end
        if not current(job, now) then
            invalidate(job)
            jobs[ship] = nil; count = count - 1
            return job.desired and 'expired' or 'inactive'
        end
        local wanted = job.desired
        if same(actual, wanted) then invalidate(job); return 'settled' end
        -- Release an old attachment before entering a different saved bay. Never
        -- directly warp a child between carrier frames or evict another occupant.
        local action = actual.state == 'FREE' and 'ATTACH' or 'UNDOCK'
        local goal = action == 'UNDOCK' and {state='FREE', dock=0} or {state=wanted.state, dock=wanted.dock}
        local dock
        if action == 'ATTACH' then
            dock, why = destination(ship, wanted, actual)
            if not dock then invalidate(job); return 'blocked', why end
        end
        local pending = job.pending and tickets[job.pending]
        if pending and pending.sequence == wanted.sequence and pending.action == action
            and same(actual, pending.actual) and handle(actual.container) == handle(pending.actual.container) then
            if dock and (handle(dock.object) ~= handle(pending.target.object)
                or handle(dock.container) ~= handle(pending.target.container)
                or dock.identity ~= pending.target.identity) then
                return failed(job, 'queued destination changed before execution')
            end
            return 'ready', job.pending -- Render polling must not starve queued consumption.
        end
        invalidate(job)
        local ticket = {} -- Opaque, controller-owned capability; not a wire record.
        tickets[ticket] = {ship=ship, sequence=wanted.sequence, action=action,
            actual=actual, target=dock, goal=goal, issued_at=now}
        job.pending = ticket
        return 'ready', ticket
    end

    function self:describe(ticket)
        local record = tickets[ticket]
        if not record then return nil end
        return {ship=record.ship, sequence=record.sequence, action=record.action,
            object=record.actual.object, state=record.goal.state, dock=record.goal.dock,
            target=record.target and record.target.object or nil,
            container=record.target and record.target.container or nil}
    end

    function self:execute(ticket, now, paused)
        if not clock(now) then return 'stopped' end
        assert(type(paused) == 'boolean', 'pause state must be explicit')
        local record = tickets[ticket]
        if not record then return 'stale' end
        local job = jobs[record.ship]
        if not job or job.pending ~= ticket or not current(job, now)
            or job.desired.sequence ~= record.sequence then
            tickets[ticket] = nil; return 'stale'
        end
        if paused then invalidate(job); return 'paused' end
        local actual, why = read(record.ship, job)
        if not actual then return failed(job, why) end
        if not same(actual, record.actual) or handle(actual.container) ~= handle(record.actual.container) then
            invalidate(job); return 'changed' -- Queued-then-dock/undock is re-planned.
        end
        if record.action == 'ATTACH' then
            local dock; dock, why = destination(record.ship, job.desired, actual)
            if not dock then invalidate(job); return 'blocked', why end
            if handle(dock.object) ~= handle(record.target.object)
                or handle(dock.container) ~= handle(record.target.container)
                or dock.identity ~= record.target.identity then return failed(job, 'destination changed before execution') end
        end
        local command = self:describe(ticket)
        invalidate(job) -- Consume before dispatch, including exceptions/unknown outcomes.
        job.awaiting = {goal=copy(record.goal), deadline=now + timeout}
        local ok, applied = pcall(dispatch, command)
        if not ok or applied ~= true then return failed(job, 'native dispatch outcome uncertain') end
        return 'issued'
    end

    function self:allow_pose(ship, now, paused)
        if not clock(now) then return false end
        assert(type(paused) == 'boolean', 'pause state must be explicit')
        local job = jobs[ship]
        if paused or not job or job.failure or job.pending or job.awaiting
            or not current(job, now) or job.desired.state ~= 'FREE' then return false end
        local actual, why = read(ship, job)
        if not actual then failed(job, why); return false end
        return actual.state == 'FREE'
    end

    function self:cancel(ship)
        local job = jobs[ship]
        if not job then return end
        invalidate(job); job.desired = nil
        -- An already issued asynchronous transition cannot be wished away. Keep
        -- its readback fence so a new intention cannot race that old operation.
        if not job.awaiting then jobs[ship] = nil; count = count - 1 end
    end

    function self:stop()
        stopped = true
        for _, job in pairs(jobs) do invalidate(job) end
    end

    return self
end
