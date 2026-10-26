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
 * same class (e.g. docked ships inside a ship) are counted inclusively. */

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
static void *walk_wrapper[WK_COUNT];
static int walk_installed;
static walk_acc walk_frame[WK_COUNT];
static walk_window walk_win;

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
    if (!walk_installed || !walk_frame[WK_GALAXY].calls) return;
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

static void walk_uninstall(void) {
    if (!walk_installed) return;
    for (int k = 0; k < WK_COUNT; ++k)
        if (walk_entry[k] && walk_orig[k])
            walk_write_entry(walk_entry[k], walk_wrapper[k], (void *)walk_orig[k]);
    walk_installed = 0;
}

/* Verify every site first; install all six or none. Returns 1 when installed. */
static int walk_install(int verify_code) {
    void *wrappers[WK_COUNT] = {(void *)walk_galaxy, (void *)walk_cluster, (void *)walk_sector,
                                (void *)walk_zone, (void *)walk_ship, (void *)walk_station};
    if (walk_installed) return 1;
    for (int k = 0; k < WK_COUNT; ++k) {
        const x4_walk_site *site = &x4_walk_sites[k];
        void **entry = (void **)(image + site->entry);
        void *function = image + site->fn;
        if (!readable(entry, sizeof(void *), 0) || *entry != function) return 0;
        if (verify_code && (!readable(function, site->length, 0) || !hash_memory(function, site->length, site->sha)))
            return 0;
        walk_entry[k] = entry;
        walk_orig[k] = (walk_move_fn)function;
        walk_wrapper[k] = wrappers[k];
    }
    for (int k = 0; k < WK_COUNT; ++k) {
        if (!walk_write_entry(walk_entry[k], (void *)walk_orig[k], walk_wrapper[k])) {
            for (int j = 0; j < k; ++j) walk_write_entry(walk_entry[j], walk_wrapper[j], (void *)walk_orig[j]);
            return 0;
        }
    }
    memset(&walk_win, 0, sizeof(walk_win));
    walk_installed = 1;
    return 1;
}
