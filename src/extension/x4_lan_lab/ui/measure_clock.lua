-- Opt-in measurement support: a wall clock for frame_profile.lua and net_rtt.lua.
-- Loads nothing and defines nothing unless a matched package enables a measurement.
local cfg=X4LAN_CONFIG
if not cfg or not cfg.enabled or (cfg.frame_profile~=true and cfg.net_probe~=true) then return end
local M={}
X4LAN_MEASURE=M

-- Wall-clock seconds. Preference: test/diagnostic injection, QueryPerformanceCounter
-- (Windows) or clock_gettime(CLOCK_MONOTONIC) (Linux) through LuaJIT FFI, then os.clock,
-- then GetCurRealTime. The chosen source, its observed resolution and why a preferred
-- source failed (failures=...) are logged. Live Windows clients fell back to GetCurRealTime,
-- which does not advance within a frame, so every FRAMECOST section read 0 ms.
local failures={}
local function failed(source,reason)
 failures[#failures+1]=source..':'..tostring(reason):gsub('[%s|,]+','_'):sub(1,80)
end
-- QueryPerformanceCounter from `library` (ffi.C or an explicitly loaded kernel32).
local function qpcClock(ffi,library)
 local counter=ffi.new('int64_t[1]')
 assert(library.QueryPerformanceFrequency(counter)~=0,'QueryPerformanceFrequency failed')
 local frequency=tonumber(counter[0])
 assert(frequency and frequency>0,'invalid QPC frequency')
 assert(library.QueryPerformanceCounter(counter)~=0,'QueryPerformanceCounter failed')
 local base=counter[0]
 local secondsPerTick=1/frequency
 return function()
  library.QueryPerformanceCounter(counter)
  return tonumber(counter[0]-base)*secondsPerTick
 end
end
local function ffiClock()
 local ok,ffi=pcall(require,'ffi')
 if not ok or type(ffi)~='table' or not ffi.cdef or not ffi.new then
  failed('ffi','unavailable')
  return nil
 end
 if ffi.os=='Windows' then
  -- Another module may already have declared these; a redefinition error is harmless.
  local declared,declareError=pcall(ffi.cdef,'int QueryPerformanceCounter(int64_t *count); int QueryPerformanceFrequency(int64_t *frequency);')
  if not declared then
   failed('qpc_cdef',declareError)
  end
  local okDefault,clock=pcall(qpcClock,ffi,ffi.C)
  if okDefault then
   return clock,'qpc'
  end
  failed('qpc',clock)
  local loaded,kernel32=pcall(ffi.load,'kernel32')
  if loaded then
   local okKernel,kernelClock=pcall(qpcClock,ffi,kernel32)
   if okKernel then
    return kernelClock,'qpc-kernel32'
   end
   failed('qpc_kernel32',kernelClock)
  else
   failed('kernel32',kernel32)
  end
 else
  pcall(ffi.cdef,'typedef struct { long tv_sec; long tv_nsec; } x4lan_measure_timespec; int clock_gettime(int clock, x4lan_measure_timespec *value);')
  local okClock,clock=pcall(function()
   local C=ffi.C;local value=ffi.new('x4lan_measure_timespec[1]')
   assert(C.clock_gettime(1,value)==0,'clock_gettime(CLOCK_MONOTONIC) failed')
   local base=tonumber(value[0].tv_sec)
   return function() C.clock_gettime(1,value);return (tonumber(value[0].tv_sec)-base)+tonumber(value[0].tv_nsec)*1e-9 end
  end)
  if okClock then
   return clock,'clock_gettime'
  end
  failed('clock_gettime',clock)
 end
end

local function usable(clock)
 local ok,a=pcall(clock);if not ok or type(a)~='number' or a~=a then return false end
 local b=clock();return type(b)=='number' and b>=a
end

-- Smallest positive step seen over a bounded number of reads; nil means the clock did not
-- advance at all (for example a per-frame cached time), so durations cannot be measured.
function M.resolution(clock,reads)
 local previous=clock();local best
 for _=1,reads or 2000 do
  local value=clock();local step=value-previous
  if step>0 then
   if not best or step<best then best=step end
   if best<=1e-7 then break end
  end
  previous=value
 end
 return best
end

-- os.clock (C clock(): process time on Linux, wall time since start on Windows, about 1 ms
-- resolution there) still measures a frame's mod time when no FFI clock is available.
local function osClock()
 local osLibrary=rawget(_G,'os')
 if type(osLibrary)~='table' or type(osLibrary.clock)~='function' then
  failed('os_clock','unavailable')
  return nil
 end
 if not usable(osLibrary.clock) or not M.resolution(osLibrary.clock,200000) then
  failed('os_clock','does_not_advance')
  return nil
 end
 return osLibrary.clock
end
local function chooseClock()
 local injected=rawget(_G,'X4LAN_MEASURE_CLOCK')
 if type(injected)=='function' and usable(injected) then
  return injected,'injected'
 end
 local clock,source=ffiClock()
 if clock and usable(clock) then
  return clock,source
 end
 if clock then
  failed(source,'unusable')
 end
 local fallback=osClock()
 if fallback then
  return fallback,'os.clock'
 end
 return GetCurRealTime,'GetCurRealTime'
end
local chosenClock,chosenSource=chooseClock()
M.clock=chosenClock
M.source=chosenSource
M.failures=failures
M.resolutionSeconds=M.resolution(M.clock)
M.timed=M.resolutionSeconds~=nil
-- Mean cost of one clock read; each profiled call performs two, so FRAMECOST can state
-- an estimate of its own overhead.
do
 local reads=1000;local first=M.clock();for _=1,reads do M.clock() end
 M.clockCostSeconds=M.timed and math.max(0,(M.clock()-first)/(reads+1)) or 0
end

-- Nearest-rank percentile of an unsorted numeric list (copied, never reordered in place).
function M.percentile(values,p)
 local n=#values;if n==0 then return nil end
 local sorted={};for i=1,n do sorted[i]=values[i] end
 table.sort(sorted)
 local rank=math.ceil(p*n);if rank<1 then rank=1 elseif rank>n then rank=n end
 return sorted[rank]
end
function M.log(text) DebugError('[X4LAN] '..text) end
M.log(string.format('MEASURE_CLOCK v=1 source=%s resolution_us=%s read_cost_us=%.3f frame_profile=%s net_probe=%s failures=%s',
 M.source,M.timed and string.format('%.3f',M.resolutionSeconds*1e6) or 'none',M.clockCostSeconds*1e6,
 tostring(cfg.frame_profile==true),tostring(cfg.net_probe==true),
 #failures>0 and table.concat(failures,',') or 'none'))
