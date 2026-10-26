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

/* Capture (stage 5): one model class's Update inputs and outputs for offline replay
   (rust/x4mp-physics capture::flight). A record: pf_cap_head, the model object's first `model_bytes`,
   the owning agent's bytes [PF_CAP_AGENT_FROM, +PF_CAP_AGENT_BYTES) (agent pointer at model+0x10), the
   0x1a0-byte slot before and after the call. Only classes with a known object size are capturable. */
#define PF_CAP_SLOT 0x1a0u
#define PF_CAP_AGENT_FROM 0x340u
#define PF_CAP_AGENT_BYTES 0x60u
#define PF_CAP_MAGIC "X4PFCAP1"
/* Object sizes from the factory's SMem::Allocate (0xe87a08 forced 0x70, 0xe87e3f highway 0xa0, 0xe87f65
   mass traffic 0xc0); linear/spline/steering are built in their own constructors (size not pinned yet). */
static const uint32_t pf_cap_model_bytes[PF_MODELS] = {0x70, 0xa0, 0, 0xc0, 0, 0};
typedef struct {
    uint32_t model, flags;        /* flags: 1 cheap, 2 P(owner), 4 called on the owner thread */
    float dt, max_forward;        /* max_forward: agent+0x364 (also inside the agent bytes) */
    double t;
    uint64_t frame;               /* frame end count since the capture was armed */
} pf_cap_head;                    /* 32 bytes */
static struct {
    unsigned char *buf;
    size_t record_size, model_bytes;
    LONG capacity;
    volatile LONG next, dropped, armed;
    int model;
    uint64_t frame, max_frames;   /* written when full or after max_frames frame ends (0: no limit) */
    wchar_t path[520];
    int written;                  /* 1 written, -1 write failed */
    uint64_t bytes;
} pf_cap;
static volatile LONG pf_cap_request;   /* 1: arm with the staged parameters */
static int pf_cap_req_model;
static LONG pf_cap_req_records;
static uint64_t pf_cap_req_frames;
static wchar_t pf_cap_req_path[520];

static size_t pf_cap_record_size(size_t model_bytes) {
    return (sizeof(pf_cap_head) + model_bytes + PF_CAP_AGENT_BYTES + 2 * PF_CAP_SLOT + 15) & ~(size_t)15;
}

/* Worker or owner thread: run the original, recording it when this class is being captured. */
static void pf_call(int i, void *self, void *slot, float dt, double t, uint8_t cheap, uint8_t p) {
    unsigned char *rec = NULL;
    if (pf_cap.armed && pf_cap.model == i) {
        LONG index = InterlockedIncrement(&pf_cap.next) - 1;
        if (index < pf_cap.capacity) rec = pf_cap.buf + (size_t)index * pf_cap.record_size;
        else InterlockedIncrement(&pf_cap.dropped);
    }
    if (rec) {
        pf_cap_head *h = (pf_cap_head *)rec;
        const unsigned char *agent = *(const unsigned char *const *)((const unsigned char *)self + 0x10);
        h->model = (uint32_t)i;
        h->flags = (cheap ? 1u : 0u) | (p ? 2u : 0u) | (GetCurrentThreadId() == owner_thread ? 4u : 0u);
        h->dt = dt;
        h->t = t;
        h->frame = pf_cap.frame;
        unsigned char *at = rec + sizeof(pf_cap_head);
        memcpy(at, self, pf_cap.model_bytes);
        at += pf_cap.model_bytes;
        if (agent) memcpy(at, agent + PF_CAP_AGENT_FROM, PF_CAP_AGENT_BYTES);
        else memset(at, 0, PF_CAP_AGENT_BYTES);
        memcpy(&h->max_forward, at + (0x364 - PF_CAP_AGENT_FROM), sizeof(float));
        at += PF_CAP_AGENT_BYTES;
        memcpy(at, slot, PF_CAP_SLOT);
        ((pf_update_fn)pf_orig[i])(self, slot, dt, t, cheap, p);
        memcpy(at + PF_CAP_SLOT, slot, PF_CAP_SLOT);
        return;
    }
    ((pf_update_fn)pf_orig[i])(self, slot, dt, t, cheap, p);
}

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
        pf_call(i, self, slot, dt, t, cheap, p);                                                        \
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

static void pf_cap_write(void);

/* Any thread (pass-through wrappers): restore the entries that are still ours. */
static void pf_uninstall(void) {
    InterlockedExchange(&pf_request, 0);
    if (pf_cap.armed) {
        pf_cap.armed = 0;
        if (GetCurrentThreadId() == owner_thread) pf_cap_write();   /* else no file: workers may still record */
    }
    if (!pf_installed) return;
    for (int i = 0; i < PF_MODELS; ++i)
        if (pf_entry[i] && pf_orig[i]) pp_write_entry(pf_entry[i], pf_wrappers[i], pf_orig[i]);
    pf_installed = 0;
}

/* Owner thread, frame end (no pathing job runs): write the capture file. */
static void pf_cap_write(void) {
    LONG records = pf_cap.next < pf_cap.capacity ? pf_cap.next : pf_cap.capacity;
    HANDLE f = CreateFileW(pf_cap.path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    pf_cap.written = -1;
    if (f == INVALID_HANDLE_VALUE) return;
    uint32_t header[12] = {1u, (uint32_t)pf_cap.model, (uint32_t)pf_cap.record_size, (uint32_t)pf_cap.model_bytes,
                           PF_CAP_AGENT_FROM, PF_CAP_AGENT_BYTES, PF_CAP_SLOT, (uint32_t)records,
                           (uint32_t)pf_cap.dropped, X4_PE_TIMESTAMP, X4_IMAGE_SIZE, (uint32_t)sizeof(pf_cap_head)};
    DWORD done = 0;
    int ok = WriteFile(f, PF_CAP_MAGIC, 8, &done, NULL) && done == 8 &&
             WriteFile(f, header, sizeof(header), &done, NULL) && done == sizeof(header);
    size_t total = (size_t)records * pf_cap.record_size, off = 0;
    while (ok && off < total) {
        DWORD chunk = (DWORD)(total - off > (1u << 24) ? (1u << 24) : total - off);
        ok = WriteFile(f, pf_cap.buf + off, chunk, &done, NULL) && done == chunk;
        off += chunk;
    }
    CloseHandle(f);
    if (ok) { pf_cap.written = 1; pf_cap.bytes = 8 + sizeof(header) + total; }
}

static const char *pf_cap_arm(void) {
    int m = pf_cap_req_model;
    if (m < 0 || m >= PF_MODELS || !pf_cap_model_bytes[m]) return "model class not capturable";
    if (pf_cap.buf || pf_cap.armed) return "capture already used";
    size_t size = pf_cap_record_size(pf_cap_model_bytes[m]);
    unsigned char *buf = VirtualAlloc(NULL, size * (size_t)pf_cap_req_records, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!buf) return "capture buffer allocation failed";
    pf_cap.buf = buf;
    pf_cap.record_size = size;
    pf_cap.model_bytes = pf_cap_model_bytes[m];
    pf_cap.capacity = pf_cap_req_records;
    pf_cap.model = m;
    pf_cap.next = pf_cap.dropped = 0;
    pf_cap.frame = 0;
    pf_cap.max_frames = pf_cap_req_frames;
    memcpy(pf_cap.path, pf_cap_req_path, sizeof(pf_cap.path));
    pf_cap.armed = 1;
    return NULL;
}

/* Owner thread, frame end: apply a queued request, fold this frame's counts. */
static void pf_frame_end(void) {
    LONG request = InterlockedExchange(&pf_request, 0);
    if (request == 1) {
        const char *why = pf_install();
        if (why && !pf_reason) pf_reason = why;
    } else if (request == 2) pf_uninstall();
    if (!pf_installed) return;
    if (InterlockedExchange(&pf_cap_request, 0)) {
        const char *why = pf_cap_arm();
        if (why && !pf_reason) pf_reason = why;
    }
    if (pf_cap.armed) {
        ++pf_cap.frame;
        if (pf_cap.next >= pf_cap.capacity || (pf_cap.max_frames && pf_cap.frame >= pf_cap.max_frames)) {
            pf_cap.armed = 0;
            pf_cap_write();
        }
    }
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
    if (pf_cap.buf) {
        field(L, "flight_cap_armed", pf_cap.armed);
        field(L, "flight_cap_records", (double)(pf_cap.next < pf_cap.capacity ? pf_cap.next : pf_cap.capacity));
        field(L, "flight_cap_dropped", (double)pf_cap.dropped);
        field(L, "flight_cap_written", pf_cap.written);
        field(L, "flight_cap_bytes", (double)pf_cap.bytes);
    }
    memset(&pf_win, 0, sizeof(pf_win));
}

/* Any thread: stage a capture of `model` (class name) with `records` records into `path` (UTF-8); the
   census is installed with it. Armed at the owner thread's next frame end; written when full or on
   disarm. One capture per session. */
static const char *pf_request_capture(const char *model, LONG records, const char *path, uint64_t frames) {
    int m = -1;
    for (int i = 0; i < PF_MODELS; ++i) if (model && !strcmp(model, pf_names[i])) m = i;
    if (m < 0) return "unknown model class";
    if (!pf_cap_model_bytes[m]) return "model class not capturable";
    if (records < 1 || records > 1000000) return "records out of range";
    if (!path || !MultiByteToWideChar(CP_UTF8, 0, path, -1, pf_cap_req_path, 520)) return "bad capture path";
    if (pf_cap.buf || pf_cap_request) return "capture already used";
    const char *why = pf_request_census(1);
    if (why) return why;
    pf_cap_req_model = m;
    pf_cap_req_records = records;
    pf_cap_req_frames = frames;
    InterlockedExchange(&pf_cap_request, 1);
    return NULL;
}

/* Lua owner:flight_capture(model, records, path[, frames]) -> true (queued) or nil, reason. */
static int pf_lua_capture(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    const char *model = p_lua_type(L, 2) == LUA_TSTRING ? p_lua_tolstring(L, 2, NULL) : NULL;
    LONG records = p_lua_type(L, 3) == LUA_TNUMBER ? (LONG)p_lua_tonumber(L, 3) : 0;
    const char *path = p_lua_type(L, 4) == LUA_TSTRING ? p_lua_tolstring(L, 4, NULL) : NULL;
    double frames = p_lua_type(L, 5) == LUA_TNUMBER ? p_lua_tonumber(L, 5) : 0.0;
    const char *why = (o != current || !o->armed) ? "not the active owner"
                      : pf_request_capture(model, records, path, frames > 0.0 ? (uint64_t)frames : 0);
    if (why) {
        p_lua_pushnil(L);
        p_lua_pushlstring(L, why, strlen(why));
        return 2;
    }
    p_lua_pushboolean(L, 1);
    return 1;
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
