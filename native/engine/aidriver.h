/* AI phase census and per-sector AI driver (engine rewrite program, agent ai-driver; included by
 * fixedtick.c after walkprof.h, and by test_aidriver.c). Report: reports/REWRITE-AI-DRIVER.md.
 *
 * AIDirector::Move 0x106d250 runs the woken AI script contexts once per simulation frame. It is called
 * from UniverseFrameUpdate at 0x9fe093, after the walk and the MD scheduler. Contexts come from two
 * intrusive wake vectors (double-buffered, write index dir+0x230) and a timed-wake tree (dir+0x1c0);
 * each pass flips the buffers and runs every context of the read vector through ExecState_Run 0xb58bb0.
 *
 * Stage 0 (mode "census") measures that phase without changing behaviour:
 *  - the E8 rel32 of the Move call at 0x9fe093 is redirected through a near stub to ad_phase(), which
 *    times the original Move and snapshots the queues before it runs (external wakes, due timed wakes);
 *  - inside Move, the E8 rel32 of the ExecState_Run call (0x106e3f6) and of the three interrupt-runner
 *    calls (0xb58ce0 at 0x106d8d6/0x106e2ef/0x106e34a) go through stubs that record Move's r13 (the
 *    context) and rbp (Move's frame: [rbp+0x170] is the wake mask Move computed for this run) before
 *    jumping to the census wrappers, which time each run and bucket it by the sector of the context's
 *    owner entity (ctx+0x120, parent chain +0x70 up to the U::Sector vtable) and by its fleet root;
 *  - wakes are classified by diffing the write vector around each run (own vs foreign sector);
 *  - data-only: vtable slot 565 (HandleScriptEvent) of AIScriptContext, AICallStackContext and MD::Cue
 *    counts events delivered during a run to contexts of the same or another sector and to MD cues.
 * Code patches change 4 bytes each, are compare-checked against the verified original rel32 (the whole
 * Move body and the call window are SHA-256 pinned in signatures.py), are written only from the owner
 * (MoveWorker) thread, which is the only thread that executes these sites, and are restored on every
 * disarm. Vtable entries are compare-exchanged from the verified original. Any mismatch installs nothing.
 * Wrappers pass straight through on other threads or when the probe is not armed. Stubs live in pages
 * allocated within +-2 GB of the image and are never freed (the module is pinned while armed). */

#define AD_DIRECTOR_RVA 0x6cf15f0u       /* AI::g_AIDirector (AIDirector*) */
#define AD_SECTOR_VTABLE_RVA 0x2b1ea48u  /* U::Sector primary vtable; no class derives from U::Sector */
#define AD_TLS_INDEX_RVA 0x32598e0u      /* X4.exe _tls_index; the engine reads TLS block 0 directly */
#define AD_TLS_INIT 0x10                 /* byte: engine TLS block initialised */
#define AD_TLS_GAME_TIME 0x300           /* double: this thread's game time (the AI phase reads it) */
#define AD_TLS_RNG 0x318                 /* u64: interpreter chance= stream */
/* AIDirector */
#define AD_D_TIMED 0x1c0                 /* timed-wake tree sentinel (header node) */
#define AD_D_TIMED_LEFT 0x1e0            /* leftmost node (earliest wake) */
#define AD_D_TIMED_SIZE 0x1e8
#define AD_D_WAKE 0x200                  /* vector<AIScriptContext*>[2], {begin,end,cap} 0x18 bytes each */
#define AD_D_WIDX 0x230                  /* u32 write index; the read vector is 1 - widx */
#define AD_D_ENTITY 0x238                /* owner entity of the context being run */
/* timed-wake tree node */
#define AD_N_PARENT 0x00
#define AD_N_LEFT 0x08
#define AD_N_RIGHT 0x10
#define AD_N_TIME 0x20                   /* double */
#define AD_N_CTX 0x28
/* AIScriptContext (primary base = ScriptEventHandler at offset 0) */
#define AD_C_SCRIPT 0xd8                 /* AI::AIScript* definition */
#define AD_C_ENTITY 0x120                /* owner entity (refcounted) */
#define AD_C_EXEC 0x130                  /* root ExecState */
#define AD_C_MASK 0x188                  /* u32 wake reason mask */
#define AD_C_TMASK 0x18c                 /* u32 timed wake reason mask */
#define AD_C_WAKETIME 0x190              /* double, -1 when none */
#define AD_C_TITER 0x1a8                 /* timed-tree node, sentinel when none */
#define AD_C_SLOT 0x1b0                  /* i64[2]: index in wake vector 0/1, -1 when not queued */
#define AD_S_CURRENT 0xc8                /* AICallStackContext: current AIScriptContext */
#define AD_SCRIPT_NAME 0x10              /* AIScript: std::string name */
#define AD_COMP_ID 0x08                  /* Component: UniverseID */
#define AD_COMP_PARENT 0x70              /* Component: parent */
#define AD_MOVE_MASK_SLOT 0x170          /* AIDirector::Move frame: [rbp+0x170] = mask of the current run */
#define AD_PASS_LIMIT 0x40               /* Move's absolute pass cap */
#define AD_LOOP_PASS 0x20                /* Move logs "Possibly infinite wake request loop" in this pass */

#define AD_MAX_DEPTH 24                  /* parent-chain walk limit */
#define AD_KEY_SLOTS 1024                /* per-frame / per-window bucket tables (power of two) */
#define AD_SCRIPT_SLOTS 1024
#define AD_TOP 8
#define AD_STUB_BYTES 32
#define AD_STUB_PAGE 4096

enum { AD_CALL_PHASE, AD_CALL_EXEC, AD_CALL_IRQ_GLOBAL, AD_CALL_IRQ_INT, AD_CALL_IRQ_ATT, AD_CALLS };
enum { AD_V_CTX, AD_V_STACK, AD_V_CUE, AD_VSITES };
enum { AD_OFF, AD_CENSUS, AD_NORMAL, AD_ROUNDS, AD_COMPARE };
static const char *const ad_mode_names[] = {"off", "census", "normal", "rounds", "compare"};

typedef void (*ad_move_fn)(void *dir);
typedef void (*ad_exec_fn)(void *es);
typedef uintptr_t (*ad_irq_fn)(void *es, void *program, uintptr_t a3, uintptr_t a4, uintptr_t a5);
typedef uintptr_t (*ad_event_fn)(void *self, uintptr_t a2, uintptr_t a3, uintptr_t a4);

/* Optional observer for decision capture (ai-orders, X4AITRACE): called on the MoveWorker around every
   context run driven by the AI phase and around every HandleScriptEvent delivery (kind: 0 AI context,
   1 call-stack context, 2 MD cue). `mask` is the wake mask the director used for the run. */
typedef struct {
    void (*run_begin)(void *ctx, void *es, unsigned mask, int irq, void *user);
    void (*run_end)(void *ctx, void *es, int irq, void *user);
    void (*event_begin)(void *handler, int kind, void *user);
    void (*event_end)(void *handler, int kind, void *user);
    void *user;
} ad_observer;

typedef struct {
    unsigned char *site, *target, *stub;
    int32_t orig_rel, hook_rel;
    int patched;
} ad_call_patch;

typedef struct { const void *key; double ms; uint64_t runs; } ad_bucket;
typedef struct { ad_bucket slot[AD_KEY_SLOTS]; uint16_t used_idx[AD_KEY_SLOTS]; unsigned used; unsigned dropped; } ad_btable;

typedef struct { const void *script; double ms; uint64_t runs; char name[56]; } ad_script_acc;

typedef struct {
    uint64_t frames, runs, irq_runs, passes, max_passes, loop_frames;
    double ms, max_ms, run_ms, sim_ms;
    uint64_t ext_wakes, ext_bits[9], run_bits[9], timed_due;
    uint64_t wake_own, wake_foreign;
    uint64_t ev_own, ev_foreign, ev_cue, ev_director, ev_outside;
    uint64_t hazard_runs, keyless_runs;
    double sec_sum_ms, sec_max_ms, serial_ms, fleet_sum_ms, fleet_max_ms;
    uint64_t sec_buckets, fleet_buckets;
    uint64_t dropped_keys, dropped_scripts;
    uint64_t ev_pending_foreign;      /* census: event to a still-queued context of another sector */
    uint64_t wake_fleet_same, ev_fleet_same, fleet_hops, fleet_dropped;   /* fleet-root resolver */
    /* stage 1 driver */
    uint64_t driver_frames, preflight_refusals, loop_logs, global_applied, attention_restarts, removed;
    uint64_t bad_caller, rounds_fallback, buckets_run, deferred;
    uint64_t cmp_event_pending, cmp_event_ran, cmp_null_lost, cmp_unchecked, cmp_passes_clean, cmp_passes_dirty;
    uint64_t cmp_post_cancel, cmp_wake_absorb;
    uint64_t last_hash;
} ad_window;

typedef struct {
    int mode, installed, refused;
    const char *reason;
    const char *driver_reason;           /* why a requested stage-1 mode fell back to the census */
    int engine_ok;                       /* stage-1 helper signatures verified and resolved */
    ad_call_patch calls[AD_CALLS];
    void **ventry[AD_VSITES];
    void *vorig[AD_VSITES];
    int vpatched[AD_VSITES];
    unsigned char *stub_page;            /* page 0 stubs (RX), page 1 saved registers (RW); never freed */
    volatile uintptr_t *saved;           /* [2k] r13, [2k+1] rbp of call site k at the last stub entry */
    void *director;                      /* g_AIDirector seen at the current phase */
    int in_phase, in_run;
    void *run_ctx;                       /* context being run */
    const void *run_key;                 /* its sector bucket */
    int run_hazard;
    uint32_t last_widx;
    size_t last_wsize;                   /* write-vector size after the last observed step */
    /* current frame */
    int64_t t0;
    uint32_t f_runs, f_irq, f_passes;
    ad_btable f_sec, f_fleet;
    ad_btable f_scratch;                 /* rounds: sector -> bucket index for the current pass */
    ad_window win;
    ad_btable w_sec;                     /* window: per-sector totals (top list) */
    ad_script_acc scripts[AD_SCRIPT_SLOTS];
    unsigned scripts_used;
    const ad_observer *observer;
} ad_state_t;
static ad_state_t ad;

/* ---- engine reads ---------------------------------------------------------------------------- */

static void *ad_director_now(void) { return image ? *(void **)(image + AD_DIRECTOR_RVA) : NULL; }

/* X4's TLS block of the calling thread (TEB ThreadLocalStoragePointer[_tls_index]); NULL if absent. */
static unsigned char *ad_engine_tls(void) {
    unsigned char *teb = (unsigned char *)NtCurrentTeb();
    void **array = *(void ***)(teb + 0x58);
    unsigned index = *(volatile unsigned *)(image + AD_TLS_INDEX_RVA);
    if (!array || index > 0xffffu) return NULL;   /* a real _tls_index is a small slot number */
    unsigned char *block = array[index];
    return block && block[AD_TLS_INIT] ? block : NULL;
}

static int ad_game_time(double *now) {
    unsigned char *tls = ad_engine_tls();
    if (!tls) return 0;
    *now = *(volatile double *)(tls + AD_TLS_GAME_TIME);
    return 1;
}

static void **ad_vec(void *dir, unsigned which) { return (void **)((unsigned char *)dir + AD_D_WAKE + 0x18 * which); }
static size_t ad_vec_size(void *dir, unsigned which) {
    void **v = ad_vec(dir, which);
    return ((uintptr_t)v[1] - (uintptr_t)v[0]) / sizeof(void *);
}
static uint32_t ad_widx(void *dir) { return *(volatile uint32_t *)((unsigned char *)dir + AD_D_WIDX) & 1u; }

/* First ancestor (including the component itself) whose vtable is U::Sector's; NULL when none
   (superhighway ships are cluster children, the player entity, galaxy/cluster owners). */
static void *ad_sector_of(const void *component) {
    const unsigned char *c = component;
    void *const vt = image + AD_SECTOR_VTABLE_RVA;
    for (int depth = 0; c && depth < AD_MAX_DEPTH; ++depth) {
        if (*(void *const *)c == vt) return (void *)c;
        c = *(const unsigned char *const *)(c + AD_COMP_PARENT);
    }
    return NULL;
}
static void *ad_ctx_entity(const void *ctx) { return ctx ? *(void *const *)((const unsigned char *)ctx + AD_C_ENTITY) : NULL; }
static void *ad_ctx_sector(const void *ctx) { return ad_sector_of(ad_ctx_entity(ctx)); }

/* ---- fleet-root resolver (census) ----------------------------------------------------------------
   The partition report proposes keying buckets on the sector of a context's fleet root (top commander)
   so a commander and its subordinates share a bucket. During the phase the hooks only record UniverseIDs
   (pure reads); after AIDirector::Move returns, each distinct entity is resolved once: Find(id) (stale ids
   return null), climb parents to the controllable (IsRealClass 0x70, vtable slot 572, a class-bitmap test),
   follow the engine's exported GetCommander chain (the UI calls it on this thread), take the root's sector.
   GetCommander reports a commander that is a fleet unit (not a ship) as no controllable; such a chain stops
   there (counted as its own root). Disabled unless both functions match their pinned signatures. */
#define AD_UIDMAP_RVA 0x6cf1500u
#define AD_GET_COMMANDER_RVA 0x1f4350u
#define AD_FIND_RVA 0xced70u
#define AD_IS_REAL_CLASS_SLOT 572
#define AD_CLASS_CONTROLLABLE 0x70
#define AD_FLEET_RECORDS 16384
#define AD_FLEET_PAIRS 8192
#define AD_FLEET_CACHE 4096

typedef struct { uint64_t controllable, fleetunit; int32_t group, pad; } ad_commander_info;
typedef struct { uint64_t eid; double ms; const void *sector; } ad_run_rec;
typedef struct { uint64_t a, b; const void *sa, *sb; int kind; } ad_pair;   /* kind 0 wake, 1 event */
static struct {
    int ok;
    void *(*get_commander)(ad_commander_info *out, uint64_t controllable, uint64_t fleetunit);
    void *(*find)(void *map, uint64_t id, uint32_t flags);
    ad_run_rec runs[AD_FLEET_RECORDS];
    unsigned nruns;
    ad_pair pairs[AD_FLEET_PAIRS];
    unsigned npairs;
    struct { uint64_t eid; const void *key; } cache[AD_FLEET_CACHE];
    uint16_t used[AD_FLEET_CACHE];
    unsigned nused;
} ad_fl;

static uint64_t ad_ctx_eid(const void *ctx) {
    const unsigned char *e = ad_ctx_entity(ctx);
    return e ? *(const uint64_t *)(e + AD_COMP_ID) : 0;
}

static void *ad_controllable_of(void *component) {
    unsigned char *c = component;
    for (int depth = 0; c && depth < AD_MAX_DEPTH; ++depth) {
        uintptr_t (*is_real)(void *, uintptr_t) = (*(uintptr_t (***)(void *, uintptr_t))c)[AD_IS_REAL_CLASS_SLOT];
        if (is_real(c, AD_CLASS_CONTROLLABLE) & 0xff) return c;
        c = *(unsigned char **)(c + AD_COMP_PARENT);
    }
    return NULL;
}

static unsigned ad_fl_slot(uint64_t eid) { return (unsigned)((eid * 0x9E3779B97F4A7C15ull) >> 52) & (AD_FLEET_CACHE - 1); }

/* Fleet-root sector of an entity id; `fallback` (the sector seen at run time) when it is gone. */
static const void *ad_fleet_key(uint64_t eid, const void *fallback) {
    unsigned i = ad_fl_slot(eid);
    for (unsigned n = 0; n < AD_FLEET_CACHE; ++n, i = (i + 1) & (AD_FLEET_CACHE - 1)) {
        if (!ad_fl.cache[i].eid) break;
        if (ad_fl.cache[i].eid == eid) return ad_fl.cache[i].key;
    }
    void *map = *(void **)(image + AD_UIDMAP_RVA);
    void *ent = map && eid ? ad_fl.find(map, eid, 4) : NULL;
    const void *key = fallback;
    if (ent) {
        void *ctl = ad_controllable_of(ent);
        if (!ctl) key = ad_sector_of(ent);
        else {
            void *root = ctl;
            uint64_t id = *(uint64_t *)((unsigned char *)ctl + AD_COMP_ID);
            for (int depth = 0; depth < 16; ++depth) {
                ad_commander_info info;
                memset(&info, 0, sizeof(info));
                ad_fl.get_commander(&info, id, 0);
                if (!info.controllable || info.controllable == id) break;
                void *c = ad_fl.find(map, info.controllable, 4);
                if (!c || c == root) break;
                root = c; id = info.controllable;
                ++ad.win.fleet_hops;
            }
            key = ad_sector_of(root);
        }
    }
    if (eid && ad_fl.nused < AD_FLEET_CACHE / 2 && !ad_fl.cache[i].eid) {
        ad_fl.cache[i].eid = eid; ad_fl.cache[i].key = key; ad_fl.used[ad_fl.nused++] = (uint16_t)i;
    }
    return key;
}

static void ad_fl_pair(int kind, const void *a_ctx, const void *b_ctx, const void *sa, const void *sb) {
    if (!ad_fl.ok) return;
    if (ad_fl.npairs >= AD_FLEET_PAIRS) { ++ad.win.fleet_dropped; return; }
    ad_fl.pairs[ad_fl.npairs++] = (ad_pair){ad_ctx_eid(a_ctx), ad_ctx_eid(b_ctx), sa, sb, kind};
}

static void ad_bucket_add(ad_btable *t, const void *key, double ms);

/* After the phase: bucket the frame's runs by fleet-root sector, and count cross-sector wakes/events
   whose two ends share a fleet-root sector (own-bucket under fleet keying). */
static void ad_fl_resolve(void) {
    if (!ad_fl.ok) return;
    for (unsigned k = 0; k < ad_fl.nruns; ++k)
        ad_bucket_add(&ad.f_fleet, ad_fleet_key(ad_fl.runs[k].eid, ad_fl.runs[k].sector), ad_fl.runs[k].ms);
    for (unsigned k = 0; k < ad_fl.npairs; ++k) {
        const ad_pair *p = &ad_fl.pairs[k];
        if (ad_fleet_key(p->a, p->sa) == ad_fleet_key(p->b, p->sb)) {
            if (p->kind) ++ad.win.ev_fleet_same; else ++ad.win.wake_fleet_same;
        }
    }
    for (unsigned k = 0; k < ad_fl.nused; ++k) ad_fl.cache[ad_fl.used[k]].eid = 0;
    ad_fl.nused = 0; ad_fl.nruns = 0; ad_fl.npairs = 0;
}

/* ---- bucket tables ----------------------------------------------------------------------------- */

static ad_bucket *ad_bucket_get(ad_btable *t, const void *key) {
    uintptr_t h = ((uintptr_t)key >> 4) * 0x9E3779B97F4A7C15ull;
    unsigned i = (unsigned)(h >> 54) & (AD_KEY_SLOTS - 1);
    for (unsigned n = 0; n < AD_KEY_SLOTS; ++n, i = (i + 1) & (AD_KEY_SLOTS - 1)) {
        ad_bucket *b = &t->slot[i];
        if (b->runs && b->key == key) return b;
        if (!b->runs) {
            if (t->used >= AD_KEY_SLOTS * 3 / 4) { ++t->dropped; return NULL; }
            b->key = key; b->ms = 0.0;
            t->used_idx[t->used++] = (uint16_t)i;
            return b;
        }
    }
    ++t->dropped;
    return NULL;
}
static void ad_bucket_add(ad_btable *t, const void *key, double ms) {
    ad_bucket *b = ad_bucket_get(t, key);
    if (!b) return;
    b->ms += ms; ++b->runs;
}
static void ad_btable_clear(ad_btable *t) {
    for (unsigned k = 0; k < t->used; ++k) memset(&t->slot[t->used_idx[k]], 0, sizeof(ad_bucket));
    t->used = 0; t->dropped = 0;
}
/* Fold one frame's table: sum, max and bucket count; the NULL key (serial bucket) also on its own. */
static void ad_btable_fold(const ad_btable *t, double *sum, double *max, uint64_t *count, double *serial) {
    double s = 0.0, m = 0.0;
    for (unsigned k = 0; k < t->used; ++k) {
        const ad_bucket *b = &t->slot[t->used_idx[k]];
        s += b->ms;
        if (b->ms > m) m = b->ms;
        if (!b->key && serial) *serial += b->ms;
    }
    *sum += s; *max += m; *count += t->used;
}

static double ad_ms(int64_t a, int64_t b) {
    return qpc_frequency > 0 ? (double)(b - a) * 1000.0 / (double)qpc_frequency : 0.0;
}
static int64_t ad_now(void) {
    LARGE_INTEGER t;
    return qpc_read(&t) ? t.QuadPart : 0;
}

static void ad_script_name(const void *script, char *out, size_t cap) {
    out[0] = 0;
    if (!script || cap < 2) return;
    const unsigned char *s = (const unsigned char *)script + AD_SCRIPT_NAME;
    size_t size = *(const size_t *)(s + 0x10), capacity = *(const size_t *)(s + 0x18);
    const char *text = capacity >= 16 ? *(const char *const *)s : (const char *)s;
    if (!text || size > 4096) return;
    size_t n = size < cap - 1 ? size : cap - 1;
    memcpy(out, text, n);
    out[n] = 0;
    for (size_t i = 0; i < n; ++i) if (out[i] == ' ' || out[i] == '=' || (unsigned char)out[i] < 0x20) out[i] = '_';
}

static void ad_script_add(const void *script, double ms) {
    uintptr_t h = ((uintptr_t)script >> 4) * 0x9E3779B97F4A7C15ull;
    unsigned i = (unsigned)(h >> 54) & (AD_SCRIPT_SLOTS - 1);
    for (unsigned n = 0; n < AD_SCRIPT_SLOTS; ++n, i = (i + 1) & (AD_SCRIPT_SLOTS - 1)) {
        ad_script_acc *a = &ad.scripts[i];
        if (a->runs && a->script == script) { a->ms += ms; ++a->runs; return; }
        if (!a->runs) {
            if (ad.scripts_used >= AD_SCRIPT_SLOTS * 3 / 4) break;
            a->script = script; a->ms = ms; a->runs = 1;
            ad_script_name(script, a->name, sizeof(a->name));
            ++ad.scripts_used;
            return;
        }
    }
    ++ad.win.dropped_scripts;
}

static void ad_mask_bits(uint64_t bits[9], unsigned mask) {
    if (!mask) { ++bits[8]; return; }
    for (unsigned b = 0; b < 8; ++b) if (mask & (1u << b)) ++bits[b];
}

/* ---- census steps ------------------------------------------------------------------------------ */

/* Run start: Move flips the buffers once per pass. Detect the flip (the write vector is now a fresh
   one), baseline its size and count the pass. No classification here. */
static void ad_wake_baseline(void) {
    void *dir = ad.director;
    if (!dir) return;
    uint32_t w = ad_widx(dir);
    if (w != ad.last_widx) {
        ad.last_widx = w; ad.last_wsize = ad_vec_size(dir, w);
        if (ad.mode == AD_CENSUS) ++ad.f_passes;   /* the stage-1 driver counts its passes exactly */
    }
}

/* Run end: wakes appended to the write vector during this run are attributed to the running context's
   bucket. A wake of a foreign-sector context marks the run hazardous (it must be routed to the
   barrier outbox in the rounds driver). Self-rewakes queued by Move after the run return (pass
   boundary) are picked up as the next run's baseline and not counted; they are always own. */
static void ad_wake_scan(const void *key) {
    void *dir = ad.director;
    if (!dir) return;
    uint32_t w = ad_widx(dir);
    if (w != ad.last_widx) { ad.last_widx = w; ad.last_wsize = ad_vec_size(dir, w); return; }
    size_t size = ad_vec_size(dir, w);
    if (size <= ad.last_wsize) { ad.last_wsize = size; return; }
    void **begin = (void **)ad_vec(dir, w)[0];
    for (size_t i = ad.last_wsize; i < size; ++i) {
        void *woken = begin[i];
        if (!woken) continue;
        const void *wkey = ad_ctx_sector(woken);
        if (wkey == key) ++ad.win.wake_own;
        else { ++ad.win.wake_foreign; ad.run_hazard = 1; ad_fl_pair(0, ad.run_ctx, woken, key, wkey); }
    }
    ad.last_wsize = size;
}

static void ad_frame_begin(void *dir) {
    ad.director = dir;
    ad.f_runs = ad.f_irq = ad.f_passes = 0;
    ad_btable_clear(&ad.f_sec);
    ad_btable_clear(&ad.f_fleet);
    /* External wakes: the current write vector becomes Move's first read vector. */
    uint32_t w = ad_widx(dir);
    void **begin = (void **)ad_vec(dir, w)[0];
    size_t n = ad_vec_size(dir, w);
    for (size_t i = 0; i < n; ++i) {
        void *ctx = begin[i];
        if (!ctx) continue;
        ++ad.win.ext_wakes;
        ad_mask_bits(ad.win.ext_bits, *(uint32_t *)((unsigned char *)ctx + AD_C_MASK));
    }
    /* Due timed wakes (in-order walk from the leftmost node, bounded). */
    double now;
    if (ad_game_time(&now)) {
        unsigned char *head = (unsigned char *)dir + AD_D_TIMED;
        unsigned char *node = *(unsigned char **)((unsigned char *)dir + AD_D_TIMED_LEFT);
        for (unsigned k = 0; node && node != head && k < 1000000; ++k) {
            if (*(double *)(node + AD_N_TIME) > now) break;
            ++ad.win.timed_due;
            unsigned char *next = *(unsigned char **)(node + AD_N_RIGHT);
            if (next) {
                while (*(unsigned char **)(next + AD_N_LEFT)) next = *(unsigned char **)(next + AD_N_LEFT);
            } else {
                next = *(unsigned char **)(node + AD_N_PARENT);
                while (next != head && *(unsigned char **)(next + AD_N_RIGHT) == node) {
                    node = next; next = *(unsigned char **)(node + AD_N_PARENT);
                }
            }
            node = next;
        }
    }
    /* Move flips the buffers at the top of its first pass; baseline the pre-flip write vector so the
       first run's ad_wake_baseline sees the flip and counts pass 1. */
    ad.last_widx = w;
    ad.last_wsize = n;
    ad.t0 = ad_now();
}

static void ad_frame_end(void *dir) {
    int64_t t1 = ad_now();
    (void)dir;
    double ms = ad.t0 && t1 ? ad_ms(ad.t0, t1) : 0.0;
    ad_window *w = &ad.win;
    ++w->frames;
    w->ms += ms;
    if (ms > w->max_ms) w->max_ms = ms;
    w->runs += ad.f_runs;
    w->irq_runs += ad.f_irq;
    w->passes += ad.f_passes;
    if (ad.f_passes > w->max_passes) w->max_passes = ad.f_passes;
    if (ad.f_passes > AD_LOOP_PASS) ++w->loop_frames;
    ad_btable_fold(&ad.f_sec, &w->sec_sum_ms, &w->sec_max_ms, &w->sec_buckets, &w->serial_ms);
    ad_fl_resolve();   /* after Move returned: read-only engine lookups, outside the interpreter */
    ad_btable_fold(&ad.f_fleet, &w->fleet_sum_ms, &w->fleet_max_ms, &w->fleet_buckets, NULL);
    w->dropped_keys += ad.f_sec.dropped + ad.f_fleet.dropped;
    ad.director = NULL;
}

/* One context run (ExecState_Run or an interrupt-runner call) of the AI phase. */
typedef struct { void *ctx; const void *saved_key; void *saved_ctx; int saved_in_run, saved_hazard; int64_t t0; int irq; } ad_run_frame;

static void ad_run_begin(ad_run_frame *f, void *ctx, void *es, unsigned mask, int irq) {
    void *dir = ad.director;
    f->ctx = ctx; f->irq = irq;
    f->saved_key = ad.run_key; f->saved_ctx = ad.run_ctx; f->saved_in_run = ad.in_run; f->saved_hazard = ad.run_hazard;
    (void)dir;
    ad_wake_baseline();                                 /* a pass may have flipped since the last run */
    ad.run_ctx = ctx;
    ad.run_key = ad_ctx_sector(ctx);
    ad.in_run = 1;
    ad.run_hazard = 0;
    if (irq) ++ad.f_irq; else { ++ad.f_runs; ad_mask_bits(ad.win.run_bits, mask); }
    if (ad.observer && ad.observer->run_begin) ad.observer->run_begin(ctx, es, mask, irq, ad.observer->user);
    f->t0 = ad_now();
}

static void ad_run_end(ad_run_frame *f, void *es) {
    int64_t t1 = ad_now();
    double ms = f->t0 && t1 ? ad_ms(f->t0, t1) : 0.0;
    void *ctx = f->ctx;
    if (ad.observer && ad.observer->run_end) ad.observer->run_end(ctx, es, f->irq, ad.observer->user);
    ad_wake_scan(ad.run_key);
    ad.win.run_ms += ms;
    ad_bucket_add(&ad.f_sec, ad.run_key, ms);
    if (ad_fl.ok) {   /* resolved after the phase (ad_fl_resolve) */
        if (ad_fl.nruns < AD_FLEET_RECORDS) ad_fl.runs[ad_fl.nruns++] = (ad_run_rec){ad_ctx_eid(ctx), ms, ad.run_key};
        else { ++ad.win.fleet_dropped; ad_bucket_add(&ad.f_fleet, ad.run_key, ms); }
    } else ad_bucket_add(&ad.f_fleet, ad.run_key, ms);
    ad_bucket_add(&ad.w_sec, ad.run_key, ms);
    if (!ad.run_key) ++ad.win.keyless_runs;
    if (!f->irq) {
        if (ad.run_hazard) ++ad.win.hazard_runs;
        ad_script_add(ctx ? *(void **)((unsigned char *)ctx + AD_C_SCRIPT) : NULL, ms);
    }
    ad.run_key = f->saved_key; ad.run_ctx = f->saved_ctx; ad.in_run = f->saved_in_run;
    ad.run_hazard = f->saved_hazard | ad.run_hazard;
}

#include <stdlib.h>
#include "aidriver_move.h"   /* stage 1: our AIDirector::Move (normal / rounds / compare) */

/* ---- hook wrappers ----------------------------------------------------------------------------- */

static int ad_owner_ok(void) {
    owner *o = current;
    return o && o->armed && ad.installed && GetCurrentThreadId() == owner_thread;
}

/* Stage-1 driver refused (structural check failed): the original Move runs from now on and the
   census continues on it; the reason is reported through aistats() as driver_reason. */
static void ad_driver_refuse(const char *why) {
    ++ad.win.preflight_refusals;
    if (!ad.driver_reason) ad.driver_reason = why;
    ad.mode = AD_CENSUS;
}

static void ad_phase(void *dir) {
    ad_move_fn original = (ad_move_fn)ad.calls[AD_CALL_PHASE].target;
    if (!ad_owner_ok() || ad.in_phase || dir != ad_director_now() || ad.mode == AD_OFF) { original(dir); return; }
    int mode = ad.mode;
    if (mode >= AD_NORMAL) {
        const char *why = ad_preflight(dir);
        if (why) { ad_driver_refuse(why); mode = AD_CENSUS; }
    }
    ad_frame_begin(dir);
    ad.in_phase = 1;
    if (mode >= AD_NORMAL) { ++ad.win.driver_frames; ad_move_impl(dir, mode); }
    else original(dir);
    ad.in_phase = 0;
    ad_frame_end(dir);
    if (mode >= AD_NORMAL) {
        if (mode == AD_COMPARE) ad.win.last_hash = ad_state_hash(dir);
        const char *why = ad_preflight(dir);   /* the next frame's first read vector must be sound */
        if (why) ad_driver_refuse(why);
    }
}

static void ad_saved_regs(int site, void **ctx, unsigned char **frame) {
    *ctx = (void *)ad.saved[2 * site];
    *frame = (unsigned char *)ad.saved[2 * site + 1];
}

static void ad_exec_run(void *es) {
    void *ctx; unsigned char *frame;
    ad_saved_regs(AD_CALL_EXEC, &ctx, &frame);
    ad_exec_fn original = (ad_exec_fn)ad.calls[AD_CALL_EXEC].target;
    if (!ad_owner_ok() || !ad.in_phase) { original(es); return; }
    ad_run_frame f;
    ad_run_begin(&f, ctx, es, frame ? *(uint32_t *)(frame + AD_MOVE_MASK_SLOT) : 0, 0);
    original(es);
    ad_run_end(&f, es);
}

static uintptr_t ad_irq_common(int site, void *es, void *program, uintptr_t a3, uintptr_t a4, uintptr_t a5) {
    void *ctx; unsigned char *frame;
    ad_saved_regs(site, &ctx, &frame);
    ad_irq_fn original = (ad_irq_fn)ad.calls[site].target;
    if (!ad_owner_ok() || !ad.in_phase) return original(es, program, a3, a4, a5);
    ad_run_frame f;
    ad_run_begin(&f, ctx, es, frame ? *(uint32_t *)(frame + AD_MOVE_MASK_SLOT) : 0, 1);
    uintptr_t result = original(es, program, a3, a4, a5);
    ad_run_end(&f, es);
    return result;
}
static uintptr_t ad_irq_global(void *es, void *p, uintptr_t a3, uintptr_t a4, uintptr_t a5) { return ad_irq_common(AD_CALL_IRQ_GLOBAL, es, p, a3, a4, a5); }
static uintptr_t ad_irq_interrupt(void *es, void *p, uintptr_t a3, uintptr_t a4, uintptr_t a5) { return ad_irq_common(AD_CALL_IRQ_INT, es, p, a3, a4, a5); }
static uintptr_t ad_irq_attention(void *es, void *p, uintptr_t a3, uintptr_t a4, uintptr_t a5) { return ad_irq_common(AD_CALL_IRQ_ATT, es, p, a3, a4, a5); }

static void ad_rounds_event(void *receiver);   /* aidriver_move.h */

static void ad_event_note(int kind, void *self) {
    if (!ad.in_phase) return;
    if (!ad.in_run) { ++ad.win.ev_director; return; }
    if (kind == AD_V_CUE) { ++ad.win.ev_cue; ad.run_hazard = 1; return; }
    void *ctx = kind == AD_V_CTX ? self : *(void **)((unsigned char *)self + AD_S_CURRENT);
    const void *rkey = ad_ctx_sector(ctx);
    if (rkey == ad.run_key) { ++ad.win.ev_own; return; }
    ++ad.win.ev_foreign;
    ad.run_hazard = 1;
    if (ctx) ad_fl_pair(1, ad.run_ctx, ctx, ad.run_key, rkey);
    /* still queued in this pass's read vector (slot of the read buffer set): a rounds order could
       run it before or after the poster, so the event is order-visible there */
    if (ctx && ad.director) {
        unsigned r = 1u - ad_widx(ad.director);
        if (*(int64_t *)((unsigned char *)ctx + AD_C_SLOT + 8 * r) != -1) ++ad.win.ev_pending_foreign;
    }
    if (ad.mode >= AD_ROUNDS) ad_rounds_event(ctx);
}

static uintptr_t ad_event_common(int kind, void *self, uintptr_t a2, uintptr_t a3, uintptr_t a4) {
    ad_event_fn original = (ad_event_fn)ad.vorig[kind];
    if (!ad_owner_ok()) return original(self, a2, a3, a4);
    if (ad.mode >= AD_CENSUS) ad_event_note(kind, self);
    if (!ad.in_phase) ++ad.win.ev_outside;
    const ad_observer *obs = ad.observer;
    if (obs && obs->event_begin) obs->event_begin(self, kind, obs->user);
    uintptr_t result = original(self, a2, a3, a4);
    if (obs && obs->event_end) obs->event_end(self, kind, obs->user);
    return result;
}
static uintptr_t ad_ctx_event(void *s, uintptr_t a2, uintptr_t a3, uintptr_t a4) { return ad_event_common(AD_V_CTX, s, a2, a3, a4); }
static uintptr_t ad_stack_event(void *s, uintptr_t a2, uintptr_t a3, uintptr_t a4) { return ad_event_common(AD_V_STACK, s, a2, a3, a4); }
static uintptr_t ad_cue_event(void *s, uintptr_t a2, uintptr_t a3, uintptr_t a4) { return ad_event_common(AD_V_CUE, s, a2, a3, a4); }

/* ---- patching ---------------------------------------------------------------------------------- */

/* Reserve one 2-page allocation within +-2 GB (minus a margin) of every byte of the image. */
static unsigned char *ad_alloc_near(void) {
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    uintptr_t gran = si.dwAllocationGranularity ? si.dwAllocationGranularity : 0x10000;
    uintptr_t lo = (uintptr_t)image, hi = lo + X4_IMAGE_SIZE;
    const uintptr_t reach = 0x70000000u;
    /* above the image */
    for (uintptr_t a = (hi + gran - 1) & ~(gran - 1); a + 2 * AD_STUB_PAGE - lo < reach;) {
        MEMORY_BASIC_INFORMATION m;
        if (!VirtualQuery((void *)a, &m, sizeof(m))) break;
        uintptr_t base = (uintptr_t)m.BaseAddress, end = base + m.RegionSize;
        if (m.State == MEM_FREE) {
            uintptr_t c = (base + gran - 1) & ~(gran - 1);
            if (c + 2 * AD_STUB_PAGE <= end && c + 2 * AD_STUB_PAGE - lo < reach) {
                void *p = VirtualAlloc((void *)c, 2 * AD_STUB_PAGE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
                if (p) return p;
            }
        }
        if (end <= a) break;
        a = end;
    }
    /* below the image */
    for (uintptr_t a = lo; a > gran && hi - (a - 2 * AD_STUB_PAGE) < reach;) {
        MEMORY_BASIC_INFORMATION m;
        if (!VirtualQuery((void *)(a - 1), &m, sizeof(m))) break;
        uintptr_t base = (uintptr_t)m.BaseAddress, end = base + m.RegionSize;
        if (m.State == MEM_FREE && end - base >= 2 * AD_STUB_PAGE) {
            uintptr_t c = (end - 2 * AD_STUB_PAGE) & ~(gran - 1);
            if (c >= base && hi - c < reach) {
                void *p = VirtualAlloc((void *)c, 2 * AD_STUB_PAGE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
                if (p) return p;
            }
        }
        if (base >= a) break;
        a = base;
    }
    return NULL;
}

static int ad_rel32_reaches(const unsigned char *from_next, const void *to) {
    intptr_t d = (intptr_t)to - (intptr_t)from_next;
    return d >= INT32_MIN && d <= INT32_MAX;
}

/* Stub k: save r13 and rbp to saved[2k], saved[2k+1], then jmp [rip+0] to the wrapper. */
static void ad_emit_stub(unsigned char *s, volatile uintptr_t *slot_r13, volatile uintptr_t *slot_rbp, void *wrapper) {
    int32_t d1 = (int32_t)((intptr_t)slot_r13 - (intptr_t)(s + 7));
    int32_t d2 = (int32_t)((intptr_t)slot_rbp - (intptr_t)(s + 14));
    memset(s, 0xcc, AD_STUB_BYTES);
    s[0] = 0x4c; s[1] = 0x89; s[2] = 0x2d; memcpy(s + 3, &d1, 4);       /* mov [rip+d1], r13 */
    s[7] = 0x48; s[8] = 0x89; s[9] = 0x2d; memcpy(s + 10, &d2, 4);      /* mov [rip+d2], rbp */
    s[14] = 0xff; s[15] = 0x25; memset(s + 16, 0, 4);                   /* jmp [rip+0] */
    memcpy(s + 20, &wrapper, sizeof(wrapper));
}

/* Replace the rel32 of an E8 call from `expected` to `value`; only the owner thread executes these
   sites, so an unaligned 4-byte write cannot be observed half-done. */
static int ad_write_rel(unsigned char *site, int32_t expected, int32_t value) {
    DWORD protection, ignored;
    if (site[0] != 0xe8) return 0;
    if (!VirtualProtect(site + 1, 4, PAGE_EXECUTE_READWRITE, &protection)) return 0;
    int32_t now;
    memcpy(&now, site + 1, 4);
    int ok = now == expected;
    if (ok) {
        if (((uintptr_t)(site + 1) & 3) == 0)
            ok = InterlockedCompareExchange((volatile LONG *)(site + 1), value, expected) == expected;
        else
            memcpy(site + 1, &value, 4);
    }
    VirtualProtect(site + 1, 4, protection, &ignored);
    FlushInstructionCache(GetCurrentProcess(), site, 5);
    return ok;
}

static int ad_write_entry(void **entry, void *expected, void *value) {
    DWORD protection, ignored;
    if (!VirtualProtect(entry, sizeof(void *), PAGE_READWRITE, &protection)) return 0;
    int ok = InterlockedCompareExchangePointer((void *volatile *)entry, value, expected) == expected;
    VirtualProtect(entry, sizeof(void *), protection, &ignored);
    return ok;
}

static void ad_uninstall(void) {
    if (!ad.installed) return;
    for (int k = AD_VSITES - 1; k >= 0; --k)
        if (ad.vpatched[k]) {
            void *wrappers[AD_VSITES] = {(void *)ad_ctx_event, (void *)ad_stack_event, (void *)ad_cue_event};
            ad_write_entry(ad.ventry[k], wrappers[k], ad.vorig[k]);
            ad.vpatched[k] = 0;
        }
    for (int k = AD_CALLS - 1; k >= 0; --k) {
        ad_call_patch *p = &ad.calls[k];
        if (p->patched) { ad_write_rel(p->site, p->hook_rel, p->orig_rel); p->patched = 0; }
    }
    ad.installed = 0;
    ad.in_phase = ad.in_run = 0;
    ad.director = NULL;
}

/* Verify every site, then patch all or nothing. verify_code=0 skips the SHA-256 checks (tests). */
static const char *ad_install_hooks(int verify_code) {
    static void *const call_wrappers[AD_CALLS] = {(void *)ad_phase, (void *)ad_exec_run, (void *)ad_irq_global,
                                                  (void *)ad_irq_interrupt, (void *)ad_irq_attention};
    void *vwrappers[AD_VSITES] = {(void *)ad_ctx_event, (void *)ad_stack_event, (void *)ad_cue_event};
    if (sizeof(x4_ai_calls) / sizeof(x4_ai_calls[0]) != AD_CALLS ||
        sizeof(x4_ai_vsites) / sizeof(x4_ai_vsites[0]) != AD_VSITES) return "signature table mismatch";
    if (verify_code && !verify_checks(x4_aidriver_checks, sizeof(x4_aidriver_checks) / sizeof(x4_aidriver_checks[0])))
        return "AI driver code signature mismatch";
    /* optional: the fleet-root resolver (census falls back to sector keys without it) */
    ad_fl.ok = 0;
    if (verify_code && verify_checks(x4_aidriver_fleet_checks, sizeof(x4_aidriver_fleet_checks) / sizeof(x4_aidriver_fleet_checks[0])) &&
        readable(image + AD_UIDMAP_RVA, sizeof(void *), 0)) {
        ad_fl.get_commander = (void *)(image + AD_GET_COMMANDER_RVA);
        ad_fl.find = (void *)(image + AD_FIND_RVA);
        ad_fl.ok = 1;
    }
    if (!readable(image + AD_DIRECTOR_RVA, sizeof(void *), 0) || !readable(image + AD_TLS_INDEX_RVA, 4, 0) ||
        !readable(image + AD_SECTOR_VTABLE_RVA, sizeof(void *), 0)) return "AI director globals unavailable";
    for (int k = 0; k < AD_CALLS; ++k) {
        unsigned char *site = image + x4_ai_calls[k].site;
        int32_t rel;
        if (!readable(site, 5, 0) || site[0] != 0xe8) return "AI call site changed";
        memcpy(&rel, site + 1, 4);
        if (site + 5 + rel != image + x4_ai_calls[k].target) return "AI call target changed";
    }
    for (int k = 0; k < AD_VSITES; ++k) {
        void **entry = (void **)(image + x4_ai_vsites[k].entry);
        if (!readable(entry, sizeof(void *), 0) || *entry != (void *)(image + x4_ai_vsites[k].fn))
            return "AI event vtable entry changed";
    }
    if (!ad.stub_page) {
        ad.stub_page = ad_alloc_near();
        if (!ad.stub_page) return "no stub page within 2 GB of the image";
        ad.saved = (volatile uintptr_t *)(ad.stub_page + AD_STUB_PAGE);
        for (int k = 0; k < AD_CALLS; ++k)
            ad_emit_stub(ad.stub_page + k * AD_STUB_BYTES, &ad.saved[2 * k], &ad.saved[2 * k + 1], call_wrappers[k]);
        DWORD old;
        if (!VirtualProtect(ad.stub_page, AD_STUB_PAGE, PAGE_EXECUTE_READ, &old)) return "stub page protection failed";
        FlushInstructionCache(GetCurrentProcess(), ad.stub_page, AD_STUB_PAGE);
    }
    for (int k = 0; k < AD_CALLS; ++k) {
        ad_call_patch *p = &ad.calls[k];
        p->site = image + x4_ai_calls[k].site;
        p->target = image + x4_ai_calls[k].target;
        p->stub = ad.stub_page + k * AD_STUB_BYTES;
        memcpy(&p->orig_rel, p->site + 1, 4);
        if (!ad_rel32_reaches(p->site + 5, p->stub)) return "stub out of rel32 reach";
        p->hook_rel = (int32_t)((intptr_t)p->stub - (intptr_t)(p->site + 5));
        p->patched = 0;
    }
    for (int k = 0; k < AD_VSITES; ++k) {
        ad.ventry[k] = (void **)(image + x4_ai_vsites[k].entry);
        ad.vorig[k] = image + x4_ai_vsites[k].fn;
        ad.vpatched[k] = 0;
    }
    memset(&ad.win, 0, sizeof(ad.win));
    ad_btable_clear(&ad.w_sec);
    memset(ad.scripts, 0, sizeof(ad.scripts));
    ad.scripts_used = 0;
    ad.mode = AD_CENSUS;
    ad.installed = 1;  /* wrappers pass through until every patch is in place */
    for (int k = 0; k < AD_VSITES; ++k) {
        if (!ad_write_entry(ad.ventry[k], ad.vorig[k], vwrappers[k])) { ad_uninstall(); return "AI event vtable patch failed"; }
        ad.vpatched[k] = 1;
    }
    /* Inner sites first, the phase entry last: no census wrapper runs before the phase is bracketed. */
    for (int k = AD_CALLS - 1; k >= 0; --k) {
        ad_call_patch *p = &ad.calls[k];
        if (!ad_write_rel(p->site, p->orig_rel, p->hook_rel)) { ad_uninstall(); return "AI call site patch failed"; }
        p->patched = 1;
    }
    return NULL;
}

/* Install the census hooks once (all or nothing), then select the mode. A stage-1 mode (normal,
   rounds, compare) additionally needs every engine helper the driver calls verified against its
   pinned signature and resolved; on refusal the census stays installed in census mode. Switching
   modes happens on the owner thread outside the AI phase (Lua runs in slot 6, Move in slot 3). */
static const char *ad_install(int mode, int verify_code) {
    if (mode < AD_CENSUS || mode > AD_COMPARE) return "unsupported aidriver mode";
    if (!ad.installed) {
        const char *why = ad_install_hooks(verify_code);
        if (why) return why;
    }
    if (mode >= AD_NORMAL && !ad.engine_ok) {
        if (verify_code && !verify_checks(x4_aidriver_helper_checks,
                                          sizeof(x4_aidriver_helper_checks) / sizeof(x4_aidriver_helper_checks[0]))) {
            ad.mode = AD_CENSUS;
            return "AI driver helper signature mismatch";
        }
        if (!readable(image + AD_DATA_ABORT_TABLE, 8, 0) || !readable(image + AD_DATA_RESULT_KEY, 8, 0)) {
            ad.mode = AD_CENSUS;
            return "AI driver data unavailable";
        }
        ad_resolve_engine();
        ad.engine_ok = 1;
    }
#ifdef X4LAN_WALKPAR_H
    /* integrated build: rounds defer through the walk agent's one buffer (Q4/Q7) */
    ad_evbuf = mode >= AD_ROUNDS ? ad_walkpar_buffer() : NULL;
#endif
    ad.mode = mode;
    ad.driver_reason = NULL;
    return NULL;
}

/* Per frame (slot 15, owner thread): every patched site must still hold our value. */
static const char *ad_check(void) {
    if (!ad.installed) return NULL;
    for (int k = 0; k < AD_CALLS; ++k) {
        int32_t rel;
        memcpy(&rel, ad.calls[k].site + 1, 4);
        if (ad.calls[k].site[0] != 0xe8 || rel != ad.calls[k].hook_rel) return "AI call site changed while armed";
    }
    void *vwrappers[AD_VSITES] = {(void *)ad_ctx_event, (void *)ad_stack_event, (void *)ad_cue_event};
    for (int k = 0; k < AD_VSITES; ++k)
        if (*ad.ventry[k] != vwrappers[k]) return "AI event vtable entry changed while armed";
    return NULL;
}

/* Called by the probe at the end of each simulation frame with the slot-loop time. */
static void ad_note_sim_frame(double ms) {
    if (ad.installed) ad.win.sim_ms += ms;
}

/* ---- Lua ----------------------------------------------------------------------------------------- */

/* handle:aidriver(mode) -> true | nil, reason. mode: "census" (stage 0) or "off". */
static int ad_lua_start(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    const char *mode = p_lua_type(L, 2) == LUA_TSTRING ? p_lua_tolstring(L, 2, NULL) : NULL;
    if (!mode) return failure(L, "aidriver mode required");
    if (o != current || !o->armed) return failure(L, "probe not armed");
    if (GetCurrentThreadId() != owner_thread) return failure(L, "owner thread required");
    if (!strcmp(mode, "off")) { ad_uninstall(); ad.mode = AD_OFF; p_lua_pushboolean(L, 1); return 1; }
    int m = -1;
    for (int k = AD_CENSUS; k <= AD_COMPARE; ++k) if (!strcmp(mode, ad_mode_names[k])) m = k;
    if (m < 0) return failure(L, "unsupported aidriver mode");
    const char *why = ad_install(m, 1);
    if (why) {
        if (!ad.installed) { ad.refused = 1; ad.reason = why; }
        else ad.driver_reason = why;          /* a stage-1 mode was refused: census continues */
        return failure(L, why);
    }
    ad.refused = 0; ad.reason = NULL;
    p_lua_pushboolean(L, 1);
    return 1;
}

static void ad_top_scripts(char *out, size_t cap) {
    out[0] = 0;
    unsigned picked[AD_TOP]; unsigned count = 0;
    for (unsigned t = 0; t < AD_TOP; ++t) {
        int best = -1;
        for (unsigned i = 0; i < AD_SCRIPT_SLOTS; ++i) {
            if (!ad.scripts[i].runs) continue;
            int taken = 0;
            for (unsigned j = 0; j < count; ++j) if (picked[j] == i) taken = 1;
            if (!taken && (best < 0 || ad.scripts[i].ms > ad.scripts[best].ms)) best = (int)i;
        }
        if (best < 0) break;
        picked[count++] = (unsigned)best;
    }
    size_t len = 0;
    double frames = ad.win.frames ? (double)ad.win.frames : 1.0;
    for (unsigned j = 0; j < count && len + 1 < cap; ++j) {
        const ad_script_acc *a = &ad.scripts[picked[j]];
        int n = snprintf(out + len, cap - len, "%s%s=%.3fms/%.1f", j ? " " : "", a->name[0] ? a->name : "?",
                         a->ms / frames, (double)a->runs / frames);
        if (n < 0) break;
        len += (size_t)n < cap - len ? (size_t)n : cap - len - 1;
    }
}

static void ad_top_sectors(char *out, size_t cap) {
    out[0] = 0;
    const ad_btable *t = &ad.w_sec;
    unsigned picked[AD_TOP]; unsigned count = 0;
    double total = 0.0;
    for (unsigned k = 0; k < t->used; ++k) total += t->slot[t->used_idx[k]].ms;
    for (unsigned r = 0; r < AD_TOP; ++r) {
        int best = -1;
        for (unsigned k = 0; k < t->used; ++k) {
            unsigned i = t->used_idx[k];
            int taken = 0;
            for (unsigned j = 0; j < count; ++j) if (picked[j] == i) taken = 1;
            if (!taken && (best < 0 || t->slot[i].ms > t->slot[best].ms)) best = (int)i;
        }
        if (best < 0) break;
        picked[count++] = (unsigned)best;
    }
    size_t len = 0;
    for (unsigned j = 0; j < count && len + 1 < cap; ++j) {
        const ad_bucket *b = &t->slot[picked[j]];
        unsigned long long id = b->key ? *(const unsigned long long *)((const unsigned char *)b->key + AD_COMP_ID) : 0;
        int n = b->key ? snprintf(out + len, cap - len, "%s%llx=%.1f%%", j ? " " : "", id, total > 0 ? 100.0 * b->ms / total : 0.0)
                       : snprintf(out + len, cap - len, "%sserial=%.1f%%", j ? " " : "", total > 0 ? 100.0 * b->ms / total : 0.0);
        if (n < 0) break;
        len += (size_t)n < cap - len ? (size_t)n : cap - len - 1;
    }
}

/* handle:aistats() -> table of the census window (per-frame means), then resets the window. */
static int ad_lua_stats(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    (void)o;
    const ad_window *w = &ad.win;
    double frames = w->frames ? (double)w->frames : 1.0;
    static const char *const bit_names[9] = {"start", "done", "irq", "irqtime", "attention", "ret", "global", "restore", "none"};
    char key[40], text[1024];
    p_lua_createtable(L, 0, 64);
    string_field(L, "mode", ad_mode_names[ad.installed ? ad.mode : AD_OFF]);
    number_field(L, "installed", ad.installed);
    number_field(L, "refused", ad.refused);
    if (ad.reason) string_field(L, "reason", ad.reason);
    number_field(L, "frames", (double)w->frames);
    number_field(L, "ms", w->ms / frames);
    number_field(L, "max_ms", w->max_ms);
    number_field(L, "run_ms", w->run_ms / frames);
    number_field(L, "share", w->sim_ms > 0.0 ? w->ms / w->sim_ms : 0.0);
    number_field(L, "runs", (double)w->runs / frames);
    number_field(L, "irq_runs", (double)w->irq_runs / frames);
    number_field(L, "passes", (double)w->passes / frames);
    number_field(L, "max_passes", (double)w->max_passes);
    number_field(L, "loop_frames", (double)w->loop_frames);
    number_field(L, "ext_wakes", (double)w->ext_wakes / frames);
    number_field(L, "timed_due", (double)w->timed_due / frames);
    number_field(L, "wake_own", (double)w->wake_own / frames);
    number_field(L, "wake_foreign", (double)w->wake_foreign / frames);
    number_field(L, "ev_own", (double)w->ev_own / frames);
    number_field(L, "ev_foreign", (double)w->ev_foreign / frames);
    number_field(L, "ev_cue", (double)w->ev_cue / frames);
    number_field(L, "ev_director", (double)w->ev_director / frames);
    number_field(L, "hazard", w->runs ? (double)w->hazard_runs / (double)w->runs : 0.0);
    number_field(L, "keyless_runs", (double)w->keyless_runs / frames);
    number_field(L, "sectors", (double)w->sec_buckets / frames);
    number_field(L, "sector_sum_ms", w->sec_sum_ms / frames);
    number_field(L, "sector_max_ms", w->sec_max_ms / frames);
    number_field(L, "serial_ms", w->serial_ms / frames);
    number_field(L, "speedup", w->sec_max_ms > 0.0 ? w->sec_sum_ms / w->sec_max_ms : 0.0);
    number_field(L, "max_share", w->sec_sum_ms > 0.0 ? w->sec_max_ms / w->sec_sum_ms : 0.0);
    number_field(L, "fleets", (double)w->fleet_buckets / frames);
    number_field(L, "fleet_speedup", w->fleet_max_ms > 0.0 ? w->fleet_sum_ms / w->fleet_max_ms : 0.0);
    number_field(L, "fleet_max_share", w->fleet_sum_ms > 0.0 ? w->fleet_max_ms / w->fleet_sum_ms : 0.0);
    number_field(L, "dropped", (double)(w->dropped_keys + w->dropped_scripts));
    number_field(L, "ev_pending_foreign", (double)w->ev_pending_foreign / frames);
    number_field(L, "fleet_resolver", ad_fl.ok);
    number_field(L, "wake_fleet_same", (double)w->wake_fleet_same / frames);
    number_field(L, "ev_fleet_same", (double)w->ev_fleet_same / frames);
    number_field(L, "fleet_hops", (double)w->fleet_hops / frames);
    number_field(L, "fleet_dropped", (double)w->fleet_dropped);
    /* stage-1 driver (zero in census mode) */
    number_field(L, "engine_ok", ad.engine_ok);
    number_field(L, "evbuf", ad_evbuf != NULL);
    if (ad.driver_reason) string_field(L, "driver_reason", ad.driver_reason);
    number_field(L, "driver_frames", (double)w->driver_frames);
    number_field(L, "preflight_refusals", (double)w->preflight_refusals);
    number_field(L, "loop_logs", (double)w->loop_logs);
    number_field(L, "global_applied", (double)w->global_applied / frames);
    number_field(L, "attention_restarts", (double)w->attention_restarts / frames);
    number_field(L, "removed", (double)w->removed / frames);
    number_field(L, "bad_caller", (double)w->bad_caller);
    number_field(L, "rounds_fallback", (double)w->rounds_fallback);
    number_field(L, "buckets_run", (double)w->buckets_run / frames);
    number_field(L, "deferred", (double)w->deferred / frames);
    number_field(L, "cmp_event_pending", (double)w->cmp_event_pending / frames);
    number_field(L, "cmp_event_ran", (double)w->cmp_event_ran / frames);
    number_field(L, "cmp_null_lost", (double)w->cmp_null_lost / frames);
    number_field(L, "cmp_unchecked", (double)w->cmp_unchecked);
    number_field(L, "cmp_passes_clean", (double)w->cmp_passes_clean);
    number_field(L, "cmp_passes_dirty", (double)w->cmp_passes_dirty);
    number_field(L, "cmp_post_cancel", (double)w->cmp_post_cancel / frames);
    number_field(L, "cmp_wake_absorb", (double)w->cmp_wake_absorb / frames);
    {
        char hash[24];
        snprintf(hash, sizeof(hash), "%016llx", (unsigned long long)w->last_hash);
        string_field(L, "state_hash", hash);
    }
    for (int b = 0; b < 9; ++b) {
        snprintf(key, sizeof(key), "ext_%s", bit_names[b]);
        number_field(L, key, (double)w->ext_bits[b] / frames);
        snprintf(key, sizeof(key), "run_%s", bit_names[b]);
        number_field(L, key, (double)w->run_bits[b] / frames);
    }
    ad_top_scripts(text, sizeof(text));
    string_field(L, "scripts", text);
    ad_top_sectors(text, sizeof(text));
    string_field(L, "top_sectors", text);
    memset(&ad.win, 0, sizeof(ad.win));
    ad_btable_clear(&ad.w_sec);
    memset(ad.scripts, 0, sizeof(ad.scripts));
    ad.scripts_used = 0;
    return 1;
}
