-- Generated client copies override this file. The source extension is inert.
X4LAN_CONFIG = {
    enabled = false,
    bubble_actions = false,
    trading = false,
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
