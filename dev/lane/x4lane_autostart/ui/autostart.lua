-- TEST ONLY. x4lane_autostart lives in dev/lane/ and is installed by dev/x4lane.py into the
-- native test lane game views (profiles 45850/45851); it is never part of a mod package.
-- It starts a disposable new world with a fixed gamestart and logs [X4LANE] AUTOSTART markers:
--   loaded   this UI state ran the script (once per UI load)
--   MENU     the start menu is showing
--   NEWGAME  NewGame(<gamestart>) was called from the start menu (start='menu', or the
--            -module command line option did not start the world within menu_fallback_s)
--   SKIP     the start menu is still showing after menu_deadline_s; no further attempt
--   INGAME   a player exists outside the start menu (logged once per UI load)
--   READY    game time advanced ready_gametime_s seconds since INGAME (simulation runs)
--   ALIVE    every alive_s seconds in game: real time, game time, pause state
--   QUIT     quit_after_s seconds of process real time passed; QuitGame() (safety net when
--            the runner died; the runner normally closes X4 itself)
-- Settings: X4LANE_AUTOSTART in ui/settings.lua (written per run by dev/x4lane.py).
local cfg = X4LANE_AUTOSTART
if type(cfg) ~= 'table' or cfg.enabled ~= true then return end
local ffi = require('ffi')
local C = ffi.C
for _, declaration in ipairs({ 'bool IsPlayerValid(void);', 'bool IsStartmenu(void);',
  'const char* GetGameStartName(void);', 'bool IsGamePaused(void);' }) do
  pcall(ffi.cdef, declaration)
end
local function call(name)
  local ok, value = pcall(function() return C[name]() end)
  if ok then return value end
  return nil
end
local function seconds(value) return string.format('%.3f', tonumber(value) or -1) end
local function gameTime()
  local ok, value = pcall(GetCurTime)
  return ok and tonumber(value) or -1
end
local tag = string.format('lane=%s run=%s', tostring(cfg.lane), tostring(cfg.run))
local function out(text) DebugError('[X4LANE] AUTOSTART ' .. text) end
local gamestart = tostring(cfg.gamestart or 'custom_creative')
local start = cfg.start == 'menu' and 'menu' or 'module'
local s = {}

SetScript('onUpdate', function()
  local now = GetCurRealTime()
  if not s.loaded then
    s.loaded = true
    out(string.format('loaded gamestart=%s start=%s realtime=%s %s', gamestart, start, seconds(now), tag))
  end
  if cfg.mute == true and not s.muted then
    s.muted = true
    local ok, on = pcall(GetSoundOption)
    if ok and on then
      pcall(SetSoundOption)
      out('sound=off')
    end
  end
  if type(cfg.quit_after_s) == 'number' and now >= cfg.quit_after_s then
    if not s.quitting then
      s.quitting = true
      out(string.format('QUIT reason=quit_after realtime=%s %s', seconds(now), tag))
      QuitGame()
    end
    return
  end
  local startmenu = call('IsStartmenu')
  if call('IsPlayerValid') and not startmenu then
    local gt = gameTime()
    if not s.ingame then
      s.ingame = now
      s.gameTime0 = gt
      s.nextAlive = now
      local name = call('GetGameStartName')
      name = name ~= nil and ffi.string(name) or '?'
      out(string.format('INGAME gamestart=%s expected=%s match=%s realtime=%s gametime=%s %s',
        name, gamestart, tostring(name == gamestart), seconds(now), seconds(gt), tag))
    end
    if not s.ready and gt - s.gameTime0 >= (tonumber(cfg.ready_gametime_s) or 5) then
      s.ready = true
      out(string.format('READY realtime=%s gametime=%s %s', seconds(now), seconds(gt), tag))
    end
    local every = tonumber(cfg.alive_s) or 10
    if every > 0 and now >= s.nextAlive then
      s.nextAlive = now + every
      out(string.format('ALIVE realtime=%s gametime=%s paused=%s', seconds(now), seconds(gt),
        tostring(call('IsGamePaused'))))
    end
    return
  end
  if not startmenu then return end -- loading or between states: never start a game here
  if not s.menu then
    s.menu = now
    out(string.format('MENU realtime=%s %s', seconds(now), tag))
  end
  if s.newgame or s.skipped then return end
  local waited = now - s.menu
  local due = start == 'menu' and (tonumber(cfg.menu_delay_s) or 3) or (tonumber(cfg.menu_fallback_s) or 60)
  if waited < due then return end
  if now > (tonumber(cfg.menu_deadline_s) or 300) then
    s.skipped = true
    out(string.format('SKIP menu deadline passed realtime=%s %s', seconds(now), tag))
    return
  end
  s.newgame = true
  out(string.format('NEWGAME gamestart=%s reason=%s realtime=%s %s', gamestart,
    start == 'menu' and 'menu' or 'module_fallback', seconds(now), tag))
  NewGame(gamestart)
end)
