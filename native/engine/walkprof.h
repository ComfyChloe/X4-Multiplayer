/* Component-walk profiling for the engine overhaul (included by fixedtick.c).
 *
 * The universe update walks galaxy -> clusters -> sectors -> zones -> objects through the
 * virtual Move (primary vtable slot 674) of each class. Replacing that one vtable entry per
 * class with a timing wrapper measures, per simulation frame, how long the whole walk takes
 * and how its cost splits across sectors. sum(sector) / max(sector) is the ideal speedup a
 * per-sector parallel walk could reach. Data-only: six pointer writes into the vtables,
 * each compare-exchanged from the verified original and restored on disarm. The wrappers
 * measure only on the probe's MoveWorker thread and pass straight through otherwise.
 * All six Move functions take only `this` (verified by disassembly). Nested calls of the
 * same class (e.g. docked ships inside a ship) are counted inclusively.
 *
 * Sector::Move's entry is shared with the attention hooks (x4lan_hooks_64.dll): they verify
 * that the entry holds the original at registration and then replace it with a wrapper that
 * calls the original directly. So the sector wrapper is added lazily at frame end, either on
 * top of the attention wrapper once that is installed, or on the original after a grace
 * period in which nothing else claimed the entry. Any other value leaves sectors unprofiled.
 * While chained, the attention hooks cannot restore their own entry until this one is gone;
 * their restore fails closed and they stay disabled for the session. */

typedef uintptr_t (*walk_move_fn)(void *self);
enum { WK_GALAXY, WK_CLUSTER, WK_SECTOR, WK_ZONE, WK_SHIP, WK_STATION, WK_COUNT };

typedef struct { uint64_t calls; double ms; double max_ms; } walk_acc;
typedef struct {
    uint64_t frames;                 /* simulation frames folded into the window */
    double walk_ms;                  /* sum of per-frame galaxy (whole walk) time */
    double walk_max_ms;
    double sector_sum_ms;            /* sum over frames of per-frame sector totals */
    double sector_max_ms;            /* sum over frames of per-frame slowest sector */
    walk_acc cls[WK_COUNT];          /* per-class totals over the window */
} walk_window;

static walk_move_fn walk_orig[WK_COUNT];
static void **walk_entry[WK_COUNT];
#define WALK_EXTRAS (sizeof(x4_walk_extras) / sizeof(x4_walk_extras[0]))
static void **walk_extra_entry[WALK_EXTRAS];
static void *walk_wrapper[WK_COUNT];
static int walk_installed;
static walk_acc walk_frame[WK_COUNT];
static walk_window walk_win;

enum { WS_PENDING, WS_ORIGINAL, WS_CHAINED, WS_REFUSED };
static int walk_sector_state;
static unsigned walk_sector_wait;
static unsigned walk_sector_grace = 900;   /* simulation frames, ~15 s at 60 fps */
static void walk_sector_poll(void);

static int walk_in_hooks_module(void *target) {
    HMODULE module;
    wchar_t path[MAX_PATH];
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            (LPCWSTR)target, &module))
        return 0;
    DWORD n = GetModuleFileNameW(module, path, MAX_PATH);
    if (!n || n >= MAX_PATH) return 0;
    const wchar_t *base = path + n;
    while (base > path && base[-1] != L'\\' && base[-1] != L'/') --base;
    return _wcsicmp(base, L"x4lan_hooks_64.dll") == 0;
}
static int (*walk_chain_ok)(void *target) = walk_in_hooks_module;

static int walk_sector_live(void) { return walk_sector_state == WS_ORIGINAL || walk_sector_state == WS_CHAINED; }

static uintptr_t walk_call(int k, void *self) {
    walk_move_fn original = walk_orig[k];
    owner *o = current;
    if (!o || !o->armed || GetCurrentThreadId() != owner_thread) return original(self);
    LARGE_INTEGER a, b;
    if (!qpc_read(&a)) return original(self);
    uintptr_t result = original(self);
    if (!qpc_read(&b) || qpc_frequency <= 0) return result;
    double ms = (double)(b.QuadPart - a.QuadPart) * 1000.0 / (double)qpc_frequency;
    walk_acc *acc = &walk_frame[k];
    ++acc->calls;
    acc->ms += ms;
    if (ms > acc->max_ms) acc->max_ms = ms;
    return result;
}

static uintptr_t walk_galaxy(void *s) { return walk_call(WK_GALAXY, s); }
static uintptr_t walk_cluster(void *s) { return walk_call(WK_CLUSTER, s); }
static uintptr_t walk_sector(void *s) { return walk_call(WK_SECTOR, s); }
static uintptr_t walk_zone(void *s) { return walk_call(WK_ZONE, s); }
static uintptr_t walk_ship(void *s) { return walk_call(WK_SHIP, s); }
static uintptr_t walk_station(void *s) { return walk_call(WK_STATION, s); }

/* Start of a simulation frame (slot 0). */
static void walk_frame_begin(void) {
    memset(walk_frame, 0, sizeof(walk_frame));
}

/* End of a simulation frame (slot 15): fold the frame into the window. */
static void walk_frame_end(void) {
    if (!walk_installed) return;
    if (walk_sector_state == WS_PENDING) { walk_sector_poll(); return; }
    if (!walk_frame[WK_GALAXY].calls) return;
    ++walk_win.frames;
    walk_win.walk_ms += walk_frame[WK_GALAXY].ms;
    if (walk_frame[WK_GALAXY].ms > walk_win.walk_max_ms) walk_win.walk_max_ms = walk_frame[WK_GALAXY].ms;
    walk_win.sector_sum_ms += walk_frame[WK_SECTOR].ms;
    walk_win.sector_max_ms += walk_frame[WK_SECTOR].max_ms;
    for (int k = 0; k < WK_COUNT; ++k) {
        walk_win.cls[k].calls += walk_frame[k].calls;
        walk_win.cls[k].ms += walk_frame[k].ms;
        if (walk_frame[k].max_ms > walk_win.cls[k].max_ms) walk_win.cls[k].max_ms = walk_frame[k].max_ms;
    }
}

static int walk_write_entry(void **entry, void *expected, void *value) {
    DWORD protection, ignored;
    if (!VirtualProtect(entry, sizeof(void *), PAGE_READWRITE, &protection)) return 0;
    int ok = InterlockedCompareExchangePointer((void *volatile *)entry, value, expected) == expected;
    VirtualProtect(entry, sizeof(void *), protection, &ignored);
    return ok;
}

/* Frame end while the sector entry is pending (owner thread only). */
static void walk_sector_poll(void) {
    void **entry = walk_entry[WK_SECTOR];
    void *value = *entry;
    int state;
    if (value == (void *)(image + x4_walk_sites[WK_SECTOR].fn)) {
        if (++walk_sector_wait < walk_sector_grace) return;
        state = WS_ORIGINAL;
    } else if (walk_chain_ok && walk_chain_ok(value)) {
        state = WS_CHAINED;
    } else {
        walk_sector_state = WS_REFUSED;
        return;
    }
    walk_orig[WK_SECTOR] = (walk_move_fn)value;
    walk_sector_state = walk_write_entry(entry, value, walk_wrapper[WK_SECTOR]) ? state : WS_REFUSED;
}

static void walk_uninstall(void) {
    if (!walk_installed) return;
    for (int k = 0; k < WK_COUNT; ++k)
        if ((k != WK_SECTOR || walk_sector_live()) && walk_entry[k] && walk_orig[k])
            walk_write_entry(walk_entry[k], walk_wrapper[k], (void *)walk_orig[k]);
    for (size_t e = 0; e < WALK_EXTRAS; ++e) {
        int k = x4_walk_extras[e].cls;
        walk_write_entry(walk_extra_entry[e], walk_wrapper[k], (void *)walk_orig[k]);
    }
    walk_installed = 0;
    walk_sector_state = WS_PENDING;
}

/* Verify every site first; install the five non-sector entries and the extra vtables or
   none (the sector entry follows from walk_sector_poll). Returns 1 when installed. */
static int walk_install(int verify_code) {
    void *wrappers[WK_COUNT] = {(void *)walk_galaxy, (void *)walk_cluster, (void *)walk_sector,
                                (void *)walk_zone, (void *)walk_ship, (void *)walk_station};
    if (walk_installed) return 1;
    for (int k = 0; k < WK_COUNT; ++k) {
        const x4_walk_site *site = &x4_walk_sites[k];
        void **entry = (void **)(image + site->entry);
        void *function = image + site->fn;
        if (!readable(entry, sizeof(void *), 0)) return 0;
        if (*entry != function && (k != WK_SECTOR || !walk_chain_ok || !walk_chain_ok(*entry))) return 0;
        if (verify_code && (!readable(function, site->length, 0) || !hash_memory(function, site->length, site->sha)))
            return 0;
        walk_entry[k] = entry;
        walk_orig[k] = (walk_move_fn)function;
        walk_wrapper[k] = wrappers[k];
    }
    for (size_t e = 0; e < WALK_EXTRAS; ++e) {
        void **entry = (void **)(image + x4_walk_extras[e].entry);
        if (!readable(entry, sizeof(void *), 0) || *entry != (void *)walk_orig[x4_walk_extras[e].cls]) return 0;
        walk_extra_entry[e] = entry;
    }
    for (int k = 0; k < WK_COUNT; ++k) {
        if (k == WK_SECTOR) continue;
        if (!walk_write_entry(walk_entry[k], (void *)walk_orig[k], walk_wrapper[k])) {
            for (int j = 0; j < k; ++j)
                if (j != WK_SECTOR) walk_write_entry(walk_entry[j], walk_wrapper[j], (void *)walk_orig[j]);
            return 0;
        }
    }
    for (size_t e = 0; e < WALK_EXTRAS; ++e) {
        int k = x4_walk_extras[e].cls;
        if (!walk_write_entry(walk_extra_entry[e], (void *)walk_orig[k], walk_wrapper[k])) {
            for (size_t j = 0; j < e; ++j) {
                int c = x4_walk_extras[j].cls;
                walk_write_entry(walk_extra_entry[j], walk_wrapper[c], (void *)walk_orig[c]);
            }
            for (int j = 0; j < WK_COUNT; ++j)
                if (j != WK_SECTOR) walk_write_entry(walk_entry[j], walk_wrapper[j], (void *)walk_orig[j]);
            return 0;
        }
    }
    memset(&walk_win, 0, sizeof(walk_win));
    walk_sector_state = WS_PENDING;
    walk_sector_wait = 0;
    walk_installed = 1;
    return 1;
}
