/* Flight-model census (physics agent; data-only, opt-in; included by physics_probe.h).
 *
 * The pathing pass runs e8f250 per agent on the JobThreadManager workers; the flight math is the
 * agent's flight control model's Update (vtable slot +0x70): model->Update(slot(W), dt, t, cheap, P).
 * This census wraps that slot in the six model vtables (x4_physics_flight_vtables, model types 1..6)
 * after verifying each holds the expected function (the code heads are pinned in x4_physics_checks,
 * verified by pp_install). The wrappers count calls, cheap-mode calls and QPC ticks per class with
 * atomics (any thread) and always call the original. Pass-through wrappers can be swapped at any time;
 * install/remove still happen on the owner thread at frame end (after the pathing join) and removal
 * compare-exchanges the original back only where the entry is still ours. */
#define PF_MODELS 6
typedef void (*pf_update_fn)(void *self, void *slot, float dt, double t, uint8_t cheap, uint8_t p);

typedef struct {
    volatile LONG64 calls, cheap, ticks, owner_calls;
    char pad[64 - 4 * sizeof(LONG64)];
} pf_counter;

static const char *const pf_names[PF_MODELS] = {"forced", "highway", "linear", "masstraffic", "spline", "steering"};
static pf_counter pf_count[PF_MODELS];
static void **pf_entry[PF_MODELS];
static void *pf_orig[PF_MODELS];
static int pf_installed;
static const char *pf_reason;              /* first refusal; latched */
static volatile LONG pf_request;           /* 0 none, 1 install, 2 remove */
static struct {
    uint64_t frames, calls[PF_MODELS], cheap[PF_MODELS], ticks[PF_MODELS], owner_calls[PF_MODELS];
    uint64_t frame_calls_max;
} pf_win;

static void pf_note(int i, uint8_t cheap, LONG64 ticks) {
    InterlockedIncrement64(&pf_count[i].calls);
    if (cheap) InterlockedIncrement64(&pf_count[i].cheap);
    InterlockedAdd64(&pf_count[i].ticks, ticks);
    if (GetCurrentThreadId() == owner_thread) InterlockedIncrement64(&pf_count[i].owner_calls);
}

#define PF_WRAPPER(i)                                                                                   \
    static void pf_update_##i(void *self, void *slot, float dt, double t, uint8_t cheap, uint8_t p) {  \
        LARGE_INTEGER a, b;                                                                             \
        QueryPerformanceCounter(&a);                                                                    \
        ((pf_update_fn)pf_orig[i])(self, slot, dt, t, cheap, p);                                        \
        QueryPerformanceCounter(&b);                                                                    \
        pf_note(i, cheap, b.QuadPart - a.QuadPart);                                                     \
    }
PF_WRAPPER(0)
PF_WRAPPER(1)
PF_WRAPPER(2)
PF_WRAPPER(3)
PF_WRAPPER(4)
PF_WRAPPER(5)
static void *const pf_wrappers[PF_MODELS] = {(void *)pf_update_0, (void *)pf_update_1, (void *)pf_update_2,
                                             (void *)pf_update_3, (void *)pf_update_4, (void *)pf_update_5};

/* Owner thread. NULL when installed, else the reason (all entries or none). */
static const char *pf_install(void) {
    if (pf_installed) return NULL;
    if (!pp_installed) return pp_reason ? pp_reason : "physics probe not installed";
    if (sizeof(x4_physics_flight_vtables) / sizeof(x4_physics_flight_vtables[0]) != PF_MODELS) return "flight table size";
    for (int i = 0; i < PF_MODELS; ++i) {
        void **entry = (void **)(image + x4_physics_flight_vtables[i].entry);
        void *function = image + x4_physics_flight_vtables[i].fn;
        if (!readable(entry, sizeof(void *), 0) || *entry != function) return "flight vtable entry not original";
        pf_entry[i] = entry;
        pf_orig[i] = function;
    }
    memset(pf_count, 0, sizeof(pf_count));
    memset(&pf_win, 0, sizeof(pf_win));
    for (int i = 0; i < PF_MODELS; ++i)
        if (!pp_write_entry(pf_entry[i], pf_orig[i], pf_wrappers[i])) {
            for (int j = 0; j < i; ++j) pp_write_entry(pf_entry[j], pf_wrappers[j], pf_orig[j]);
            return "flight vtable write failed";
        }
    pf_installed = 1;
    return NULL;
}

/* Any thread (pass-through wrappers): restore the entries that are still ours. */
static void pf_uninstall(void) {
    InterlockedExchange(&pf_request, 0);
    if (!pf_installed) return;
    for (int i = 0; i < PF_MODELS; ++i)
        if (pf_entry[i] && pf_orig[i]) pp_write_entry(pf_entry[i], pf_wrappers[i], pf_orig[i]);
    pf_installed = 0;
}

/* Owner thread, frame end: apply a queued request, fold this frame's counts. */
static void pf_frame_end(void) {
    LONG request = InterlockedExchange(&pf_request, 0);
    if (request == 1) {
        const char *why = pf_install();
        if (why && !pf_reason) pf_reason = why;
    } else if (request == 2) pf_uninstall();
    if (!pf_installed) return;
    uint64_t frame_calls = 0;
    for (int i = 0; i < PF_MODELS; ++i) {
        uint64_t c = (uint64_t)InterlockedExchange64(&pf_count[i].calls, 0);
        pf_win.calls[i] += c;
        frame_calls += c;
        pf_win.cheap[i] += (uint64_t)InterlockedExchange64(&pf_count[i].cheap, 0);
        pf_win.ticks[i] += (uint64_t)InterlockedExchange64(&pf_count[i].ticks, 0);
        pf_win.owner_calls[i] += (uint64_t)InterlockedExchange64(&pf_count[i].owner_calls, 0);
    }
    if (frame_calls > pf_win.frame_calls_max) pf_win.frame_calls_max = frame_calls;
    ++pf_win.frames;
}

/* Any thread: queue install (on != 0) or removal for the owner thread's next frame end. */
static const char *pf_request_census(int on) {
    if (!pp_installed) return pp_reason ? pp_reason : "physics probe not installed";
    if (on && pf_reason) return pf_reason;
    InterlockedExchange(&pf_request, on ? 1 : 2);
    return NULL;
}

static void pf_stats(lua_State *L, void (*field)(lua_State *, const char *, double)) {
    field(L, "flight_installed", pf_installed);   /* the reason string is pushed by fixedtick.c stats() */
    if (!pf_installed || !pf_win.frames) return;
    double frames = (double)pf_win.frames, total_ticks = 0.0, total_calls = 0.0, cheap = 0.0, owner = 0.0;
    char name[40];
    field(L, "flight_frames", frames);
    for (int i = 0; i < PF_MODELS; ++i) {
        snprintf(name, sizeof(name), "flight_%s_calls", pf_names[i]);
        field(L, name, (double)pf_win.calls[i] / frames);
        snprintf(name, sizeof(name), "flight_%s_us", pf_names[i]);
        field(L, name, pf_win.calls[i] && qpc_frequency > 0
                           ? (double)pf_win.ticks[i] * 1e6 / (double)qpc_frequency / (double)pf_win.calls[i] : 0.0);
        total_ticks += (double)pf_win.ticks[i];
        total_calls += (double)pf_win.calls[i];
        cheap += (double)pf_win.cheap[i];
        owner += (double)pf_win.owner_calls[i];
    }
    field(L, "flight_calls", total_calls / frames);
    field(L, "flight_cheap", cheap / frames);
    field(L, "flight_owner_calls", owner);
    field(L, "flight_calls_max", (double)pf_win.frame_calls_max);
    field(L, "flight_ms", qpc_frequency > 0 ? total_ticks * 1e3 / (double)qpc_frequency / frames : 0.0);
    memset(&pf_win, 0, sizeof(pf_win));
}

/* Lua owner:flight_census('on'|'off') -> true (queued) or nil, reason. */
static int pf_lua_census(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    const char *arg = p_lua_type(L, 2) == LUA_TSTRING ? p_lua_tolstring(L, 2, NULL) : NULL;
    const char *why = (o != current || !o->armed) ? "not the active owner"
                      : !arg || (strcmp(arg, "on") && strcmp(arg, "off")) ? "census mode must be 'on' or 'off'"
                      : pf_request_census(!strcmp(arg, "on"));
    if (why) {
        p_lua_pushnil(L);
        p_lua_pushlstring(L, why, strlen(why));
        return 2;
    }
    p_lua_pushboolean(L, 1);
    return 1;
}
