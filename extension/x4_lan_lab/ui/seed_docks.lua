-- Pure saved-reference ledger. No engine calls, UI registration or network input.
-- Definitions come from the rehydrated pristine-save registry, not remote handles.
X4LAN_SEED_DOCKS_LEDGER = {}
local D = X4LAN_SEED_DOCKS_LEDGER
local MAX = 250000

local function integer(value, low, high)
    return type(value) == 'number' and value == math.floor(value)
        and value >= low and value <= high
end

local function handle(value)
    local kind = type(value)
    if kind == 'number' then
        if not integer(value, 1, 9007199254740991) then return nil end
        return string.format('%.0f', value)
    end
    if kind ~= 'string' and kind ~= 'cdata' then return nil end
    local key = tostring(value):gsub('ULL$', '')
    if not key:match('^[1-9]%d*$') or #key > 20 then return nil end
    if #key == 20 and key > '18446744073709551615' then return nil end
    return key
end
D.handle = handle

local function macro(value)
    return type(value) == 'string' and #value <= 96
        and value:match('^[a-z][a-z0-9_]*_macro$') ~= nil
end

local function definition(row)
    assert(type(row) == 'table', 'missing saved dock definition')
    local object, parent, container = handle(row.object), handle(row.parent), handle(row.container)
    assert(object and parent and container and object ~= parent and object ~= container,
        'invalid saved dock references')
    assert(macro(row.macro) and macro(row.container_macro), 'invalid saved dock macro')
    assert(type(row.external) == 'boolean' and type(row.storage) == 'boolean'
        and not (row.external and row.storage), 'inconsistent saved dock type')
    assert(integer(row.capacity, 0, 2147483647), 'invalid saved storage capacity')
    -- Copy only scalars; the caller cannot change an admitted definition in place.
    return {object=row.object, parent=row.parent, container=row.container,
        object_key=object, parent_key=parent, container_key=container,
        macro=row.macro, container_macro=row.container_macro,
        external=row.external, storage=row.storage, capacity=row.capacity}
end

local function matches(saved, observed)
    if type(observed) ~= 'table' or observed.alive ~= true then return false end
    if handle(observed.object) ~= saved.object_key or handle(observed.parent) ~= saved.parent_key
        or handle(observed.container) ~= saved.container_key then return false end
    if observed.macro ~= saved.macro or observed.container_macro ~= saved.container_macro
        or observed.external ~= saved.external or observed.storage ~= saved.storage
        or observed.capacity ~= saved.capacity then return false end
    local error = observed.pose_error
    return type(error) == 'number' and error == error and error >= 0 and error <= .05
end

function D.registry(seed, count, inspect)
    assert(type(seed) == 'string' and #seed == 32 and seed:match('^[a-f0-9]+$'), 'invalid seed')
    assert(integer(count, 1, MAX) and type(inspect) == 'function', 'invalid dock registry')
    local entries, handles, nodes = {}, {}, {}
    local next_index, next_node = 1, 0
    local sealed, failed = false, false
    local self = {}

    local function fail(message)
        failed = true
        error(message)
    end

    local function node(key)
        if not nodes[key] then next_node = next_node + 1; nodes[key] = next_node end
        return nodes[key]
    end

    function self:observe(index, row)
        assert(not failed and not sealed, 'dock registry not collecting')
        if index ~= next_index or index > count then fail('unordered or repeated saved dock index') end
        local ok, saved = pcall(definition, row)
        if not ok then fail(saved) end
        if handles[saved.object_key] then fail('native dock occurs at multiple saved indices') end
        local read_ok, observed = pcall(inspect, saved.object)
        if not read_ok or not matches(saved, observed) then fail('saved dock witnesses do not match live readback') end
        -- First-encounter graph labels are invariant under local handle renumbering.
        -- They do not replace the separate native save/reload identity acceptance.
        local object_node = node(saved.object_key)
        local container_node = node(saved.container_key)
        local parent_node = node(saved.parent_key)
        saved.fingerprint = table.concat({seed, index, saved.macro, saved.container_macro,
            saved.external and '1' or '0', saved.storage and '1' or '0', saved.capacity,
            object_node, container_node, parent_node}, ':')
        entries[index], handles[saved.object_key] = saved, index
        next_index = next_index + 1
    end

    function self:seal()
        assert(not failed and not sealed, 'dock registry cannot seal')
        if next_index ~= count + 1 then fail('incomplete saved dock registry') end
        sealed = true
    end

    function self:ready() return sealed and not failed end

    function self:index(object)
        if not self:ready() then return nil end
        local key = handle(object)
        return key and handles[key] or nil
    end

    function self:fingerprints()
        assert(self:ready(), 'dock registry not ready')
        local copy = {}
        for index, entry in ipairs(entries) do copy[index] = entry.fingerprint end
        return copy
    end

    function self:resolve(index, state)
        assert(self:ready(), 'dock registry not ready')
        assert(integer(index, 1, count), 'unknown saved dock index')
        assert(state == 'EXTERNAL' or state == 'STORED', 'invalid attached state')
        local saved = entries[index]
        if (state == 'EXTERNAL' and not saved.external) or (state == 'STORED' and not saved.storage) then
            fail('requested attachment type differs from saved dock')
        end
        local ok, observed = pcall(inspect, saved.object)
        if not ok or not matches(saved, observed) then fail('saved dock changed before use') end
        return {object=saved.object, container=saved.container, parent=saved.parent,
            fingerprint=saved.fingerprint, external=saved.external, storage=saved.storage,
            capacity=saved.capacity}
    end

    return self
end
