-- Bounded local MD event importer, deliberately unregistered until integration.
-- A query returns a correlated MD readback, not permission to execute later.
-- An asynchronous native action MUST repeat its guards at execution time.
X4LAN_SEED_DOCK_IMPORT = {}
local I = X4LAN_SEED_DOCK_IMPORT
local D = assert(X4LAN_SEED_DOCKS_LEDGER, 'saved dock ledger required')
local ffi = require('ffi')

local function split(text)
    assert(type(text) == 'string' and #text <= 1024, 'invalid native dock event')
    local result = {}; for part in (text .. '|'):gmatch('(.-)|') do result[#result+1] = part end
    return result
end
local function number(text, low, high)
    assert(type(text) == 'string' and text:match('^%d+$') and (#text == 1 or text:sub(1,1) ~= '0'),
        'noncanonical native dock integer')
    local n = tonumber(text)
    assert(n >= low and n <= high, 'native dock integer out of range')
    return n
end
local function reference(text)
    assert(type(text) == 'string' and text:match('^0x[%da-fA-F]+$') and #text <= 18,
        'invalid native dock reference')
    local result = ffi.new('uint64_t', 0)
    for c in text:sub(3):gmatch('.') do result = result * 16 + tonumber(c, 16) end
    assert(result ~= 0, 'null native dock reference')
    return result
end

function I.new(seed, mode, send)
    assert(type(seed) == 'string' and #seed == 32 and seed:match('^[a-f0-9]+$'), 'invalid dock seed')
    assert((mode == 'capture' or mode == 'load') and type(send) == 'function', 'invalid importer options')
    local next_index, serial, last_time = 1, 0, 0
    local types = {}
    local pending, count, ledger, active, started, failed, sealed
    local self = {}
    local function clock(now)
        assert(type(now) == 'number' and now == now and now >= last_time and now <= 1e12,
            'dock importer requires a monotonic clock')
        last_time = now
    end
    local function request(start, query, now)
        pending = {start=start, serial=query and serial or 0, query=query, started=now}
        send({seed=seed, mode=mode, start=start, serial=pending.serial})
    end
    local function protect(fn)
        if failed then return false, failed end
        local ok, err = pcall(fn)
        if not ok then failed=tostring(err); active=nil; pending=nil; return false, failed end
        return true
    end
    function self:frame(now)
        return protect(function()
            clock(now); started=started or now
            if not sealed then assert(now-started <= 120, 'saved dock import timed out') end
            if pending then assert(now-pending.started <= 5, 'native dock response timed out')
            elseif not sealed then request(next_index, nil, now) end
        end)
    end
    function self:ready() return sealed == true and not failed end
    function self:error() return failed end
    function self:index(object) return self:ready() and ledger:index(object) or nil end
    function self:fingerprints()
        assert(self:ready(), 'saved docks not ready'); return ledger:fingerprints()
    end
    function self:query(index, state, now, callback)
        assert(self:ready() and not pending, 'saved dock query unavailable')
        assert(type(index) == 'number' and index == math.floor(index) and index >= 1 and index <= count,
            'unknown saved dock query index')
        assert((state == 'EXTERNAL' or state == 'STORED') and type(callback) == 'function', 'invalid dock query')
        assert(types[index][state], 'requested attachment type differs from saved dock')
        return protect(function()
            clock(now); assert(serial < 2147483647, 'native dock query sequence exhausted')
            serial=serial+1; request(index, {state=state, callback=callback}, now)
        end)
    end
    function self:receive(kind, text, now)
        return protect(function()
            clock(now)
            if kind == 'Error' then error('native saved dock rejection: ' .. tostring(text)) end
            local f = split(text)
            assert(f[1] == seed, 'native dock event has another seed')
            local seq = number(f[2], 0, 2147483647)
            if seq > 0 and seq < serial then return end -- Late response to a completed query.
            assert(pending and seq == pending.serial, 'unsolicited native dock event')
            if kind == 'Begin' then
                assert(#f == 5 and not pending.began, 'duplicate native dock begin')
                local first, last, total = number(f[3],1,250000), number(f[4],1,250000), number(f[5],1,250000)
                assert(first == pending.start and last == (seq == 0 and math.min(first+63,total) or first)
                    and (not count or count == total), 'native dock batch bounds changed')
                count = total; pending.last=last; pending.next=first; pending.began=true
                if not ledger then
                    ledger=D.registry(seed,count,function(object)
                        -- The MD predicate validated saved/live witnesses for THIS event.
                        -- No cached row is available to resolve a dock after this handler.
                        return active and D.handle(active.object) == D.handle(object) and active or nil
                    end)
                end
            elseif kind == 'Row' then
                assert(#f == 12 and pending.began, 'native dock row before begin')
                local index=number(f[3],1,count)
                assert(index == pending.next and index <= pending.last, 'missing or reordered native dock row')
                local row={object=reference(f[4]),parent=reference(f[5]),container=reference(f[6]),
                    macro=f[7],container_macro=f[8],external=number(f[9],0,1)==1,
                    storage=number(f[10],0,1)==1,capacity=number(f[11],0,2147483647),
                    pose_error=tonumber(f[12]),alive=true}
                active=row
                if seq == 0 then
                    ledger:observe(index,row); types[index]={EXTERNAL=row.external, STORED=row.storage}
                else pending.result=ledger:resolve(index,pending.query.state) end
                active=nil; pending.next=index+1
            elseif kind == 'End' then
                assert(#f == 6 and pending.began and f[6] == '1'
                    and number(f[3],1,count) == pending.start and number(f[4],1,count) == pending.last
                    and number(f[5],1,250000) == count and pending.next == pending.last+1,
                    'incomplete native dock batch')
                local done=pending; pending=nil
                if seq == 0 then
                    next_index=done.last+1
                    if next_index == count+1 then ledger:seal(); sealed=true end
                else
                    assert(done.result, 'native dock query has no readback')
                    done.query.callback(done.result, now)
                end
            else error('unknown native dock event') end
        end)
    end
    return self
end
