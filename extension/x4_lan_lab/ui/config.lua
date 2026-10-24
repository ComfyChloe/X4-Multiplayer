-- Generated client copies override this file. The source extension is inert.
X4LAN_CONFIG = {
    enabled = false,
    bubble_actions = false,
    trading = false,
    fire2 = false, -- Matched internal packages select FIRE2 before any shot is played.
    pilot_health = false, -- Dev only: pilot-reported co-op ship health (health_game.lua).
    shot_visual = false, -- Dev only: verified harmless FIRE2 projectiles; requires fire2/SHOT/HEALTH.
    shot_claims = false, -- Dev only, needs pilot_health: shooter-confirmed hit counts (shot_game.lua).
    interaction_test = false, -- Destructive fixtures ONLY in the disposable test world.
    test_profile = "45841",
    host = "127.0.0.1",
    port = 45841,
    token = "",
    build = "SET_EXACT_BUILD",
    bubble = "bubble-01",
    slot = "p1",
    mode = "telemetry", -- "proxy" attempts experimental spawn/move calls.
    proxy_macro = "ship_arg_s_scout_01_a_macro",
    -- Shared co-op ship for both players and both proxies (requires X4: Timelines).
    ship_macro = "ship_ter_s_xperimental_01_a_macro",
    ship_loadout = "x4lan_coop_xperimental",
    ship_weapons = 6, -- the 7th slot is a missile launcher; the hull stores no missiles
}
