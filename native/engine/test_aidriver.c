/* Synthetic tests for the AI phase census (native/engine/aidriver.h), in the style of
 * test_fixedtick.c. No game is involved: a VirtualAlloc'd fake image stands in for X4.exe, fake
 * AIScriptContexts hang under a fake U::Sector vtable, and a fake AIDirector::Move drives the census
 * the way the installed stubs would (saving r13 = the context and rbp = Move's frame before each
 * ExecState_Run). The tests cover per-sector/per-fleet bucketing, the wake and event own/foreign
 * split, the pass count and sum/max speedup, the emitted stub machine code, and that install is
 * all-or-nothing and fully restored on disarm. Originals are never called (the fake image has no
 * real code), so the fake Move inlines ad_run_begin/ad_run_end and events use ad_event_note. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include "fixedtick.c"

struct lua_State { int unused; };
static owner fake_owner;
static LARGE_INTEGER fake_now;
static BOOL WINAPI fake_qpc(LARGE_INTEGER *value) { *value = fake_now; return TRUE; }

#define SECTORS 4
#define VEC_CAP 256
static void *sector_vt;
static unsigned char fake_dir[0x300];
static void *wake0[VEC_CAP], *wake1[VEC_CAP];
static unsigned char fake_move_frame[0x200];
static unsigned char sectors[SECTORS][0x80];
static unsigned char fake_script[SECTORS][0x40];

typedef struct { void *vtable; unsigned char pad[0x300]; } fake_ctx;
static fake_ctx ctxs[32];
static unsigned char entities[32][0x200];
static unsigned nctx;
static int run_cost_us[32];

static void set_u64(void *p, size_t off, unsigned long long v) { *(unsigned long long *)((unsigned char *)p + off) = v; }
static void set_ptr(void *p, size_t off, void *v) { *(void **)((unsigned char *)p + off) = v; }

static void make_sector(int i, unsigned long long id) {
    static const char *const names[SECTORS] = {"order.fight", "order.trade", "order.mine", "order.wait"};
    memset(sectors[i], 0, sizeof(sectors[i]));
    set_ptr(sectors[i], 0, sector_vt);
    set_u64(sectors[i], AD_COMP_ID, id);
    unsigned char *s = fake_script[i] + AD_SCRIPT_NAME;    /* std::string SSO: text inline, len +0x10 */
    strcpy((char *)s, names[i]);
    *(size_t *)(s + 0x10) = strlen(names[i]);
    *(size_t *)(s + 0x18) = 15;
}

/* Context whose owner entity lives in sector `sec` (sec < 0: orphan with no sector ancestor). */
static void *make_ctx(int sec) {
    fake_ctx *c = &ctxs[nctx];
    unsigned char *e = entities[nctx];
    ++nctx;
    memset(c, 0, sizeof(*c));
    memset(e, 0, 0x200);
    set_ptr(e, 0, (void *)0x1234);                       /* not the sector vtable */
    set_ptr(e, AD_COMP_PARENT, sec < 0 ? NULL : sectors[sec]);
    set_ptr(c, AD_C_ENTITY, e);
    set_ptr(c, AD_C_SCRIPT, fake_script[sec < 0 ? 0 : sec]);
    *(uint32_t *)((unsigned char *)c + AD_C_MASK) = 1;   /* start/return wake */
    return c;
}

/* Fleet resolver fakes: vtables whose slot 572 (IsRealClass) answers "controllable" for ships only,
   GetCommander from a small table, Find over the fake entities and ships. */
static uintptr_t fake_is_real_ship(void *self, uintptr_t cls) { (void)self; return cls == AD_CLASS_CONTROLLABLE; }
static uintptr_t fake_is_real_none(void *self, uintptr_t cls) { (void)self; (void)cls; return 0; }
static void *fake_vt_ship[600], *fake_vt_plain[600];
static unsigned char *fake_ships[2], *fake_entities[2];
static void *fake_get_commander(ad_commander_info *out, uint64_t id, uint64_t fleet) {
    (void)fleet;
    memset(out, 0, sizeof(*out));
    if (id == 0xB0) out->controllable = 0xA0;   /* B's commander is A; A has none */
    return out;
}
static void *fake_find(void *map, uint64_t id, uint32_t flags) {
    (void)map; (void)flags;
    for (int i = 0; i < 2; ++i) {
        if (fake_ships[i] && *(uint64_t *)(fake_ships[i] + AD_COMP_ID) == id) return fake_ships[i];
        if (fake_entities[i] && *(uint64_t *)(fake_entities[i] + AD_COMP_ID) == id) return fake_entities[i];
    }
    return NULL;
}

static uint32_t dir_widx(void) { return *(uint32_t *)(fake_dir + AD_D_WIDX); }
static void vec_set(unsigned which, void **store, size_t n) {
    void **v = (void **)(fake_dir + AD_D_WAKE + 0x18 * which);
    v[0] = store; v[1] = store + n; v[2] = store + VEC_CAP;
}
static void vec_push(unsigned which, void *ctx) {
    void **v = (void **)(fake_dir + AD_D_WAKE + 0x18 * which);
    void **end = (void **)v[1];
    *end = ctx;
    v[1] = end + 1;
}

/* Fake AIDirector::Move: flip the buffers once (one pass), run each queued context, optionally wake
   a planned context into the write vector mid-run (like AIDirector_RequestWake). */
static const int *g_wake_plan;
static void fake_move(void *dir) {
    assert(dir == &fake_dir);
    *(uint32_t *)(fake_dir + AD_D_WIDX) ^= 1u;            /* pass 1 flip */
    unsigned read = dir_widx() ^ 1u;
    void **v = (void **)(fake_dir + AD_D_WAKE + 0x18 * read);
    void **begin = (void **)v[0];
    size_t n = ((uintptr_t)v[1] - (uintptr_t)v[0]) / sizeof(void *);
    for (size_t i = 0; i < n; ++i) {
        void *ctx = begin[i];
        unsigned idx = (unsigned)((fake_ctx *)ctx - ctxs);
        *(uint32_t *)(fake_move_frame + AD_MOVE_MASK_SLOT) = *(uint32_t *)((unsigned char *)ctx + AD_C_MASK);
        ad.saved[2 * AD_CALL_EXEC] = (uintptr_t)ctx;
        ad.saved[2 * AD_CALL_EXEC + 1] = (uintptr_t)fake_move_frame;
        ad_run_frame f;
        ad_run_begin(&f, ctx, ctx, *(uint32_t *)(fake_move_frame + AD_MOVE_MASK_SLOT), 0);
        int plan = g_wake_plan ? g_wake_plan[idx] : -1;
        if (plan >= 0) vec_push(dir_widx(), ctxs + plan);
        fake_now.QuadPart += (int64_t)run_cost_us[idx] * 10;   /* 10 MHz QPC */
        ad_run_end(&f, ctx);
    }
}

static void census_frame(void) {
    ad.calls[AD_CALL_PHASE].target = (unsigned char *)(void *)fake_move;
    ad_phase(&fake_dir);
}

static void setup_image(void) {
    image = VirtualAlloc(NULL, X4_IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image);
    sector_vt = image + AD_SECTOR_VTABLE_RVA;
    fake_vt_ship[AD_IS_REAL_CLASS_SLOT] = (void *)fake_is_real_ship;
    fake_vt_plain[AD_IS_REAL_CLASS_SLOT] = (void *)fake_is_real_none;
    *(void **)(image + AD_SECTOR_VTABLE_RVA + 8 * AD_IS_REAL_CLASS_SLOT) = (void *)fake_is_real_none;
    *(void **)(image + AD_DIRECTOR_RVA) = &fake_dir;
    *(uint32_t *)(image + AD_TLS_INDEX_RVA) = 0x7fffffff;    /* bounded out by ad_engine_tls -> no TLS */
    for (size_t k = 0; k < sizeof(x4_ai_calls) / sizeof(x4_ai_calls[0]); ++k) {
        unsigned char *site = image + x4_ai_calls[k].site;
        site[0] = 0xe8;
        int32_t rel = (int32_t)((intptr_t)(image + x4_ai_calls[k].target) - (intptr_t)(site + 5));
        memcpy(site + 1, &rel, 4);
    }
    for (size_t k = 0; k < sizeof(x4_ai_vsites) / sizeof(x4_ai_vsites[0]); ++k)
        *(void **)(image + x4_ai_vsites[k].entry) = image + x4_ai_vsites[k].fn;
}

static void setup_owner(void) {
    memset(&fake_owner, 0, sizeof(fake_owner));
    fake_owner.armed = 1;
    current = &fake_owner;
    owner_thread = GetCurrentThreadId();
    qpc_frequency = 10000000;
    qpc_read = fake_qpc;
    fake_now.QuadPart = 1000000;
}

static void setup_world(void) {
    nctx = 0;
    memset(&fake_dir, 0, sizeof(fake_dir));
    set_ptr(fake_dir, AD_D_TIMED_LEFT, fake_dir + AD_D_TIMED);   /* empty timed tree */
    set_ptr(fake_dir + AD_D_TIMED, AD_N_PARENT, fake_dir + AD_D_TIMED);
    for (int i = 0; i < SECTORS; ++i) make_sector(i, 0x100 + i);
    vec_set(0, wake0, 0);
    vec_set(1, wake1, 0);
    *(uint32_t *)(fake_dir + AD_D_WIDX) = 0;
}

int main(void) {
    assert(!verify_image());
    setup_image();
    setup_owner();
    setup_world();

    /* ---- install / uninstall / stub bytes / fail-closed --------------------------------------- */
    assert(!ad_install(AD_CENSUS, 0) && ad.installed);
    for (size_t k = 0; k < AD_CALLS; ++k) {
        int32_t rel; memcpy(&rel, ad.calls[k].site + 1, 4);
        assert(ad.calls[k].site[0] == 0xe8 && ad.calls[k].site + 5 + rel == ad.calls[k].stub);
    }
    {
        void *vw[AD_VSITES] = {(void *)ad_ctx_event, (void *)ad_stack_event, (void *)ad_cue_event};
        for (int k = 0; k < AD_VSITES; ++k) assert(*ad.ventry[k] == vw[k]);
    }
    assert(!ad_check());
    {
        unsigned char *s = ad.calls[AD_CALL_EXEC].stub;    /* mov [rip+d],r13; mov [rip+d2],rbp; jmp [rip] */
        int32_t d1, d2; void *target;
        assert(s[0] == 0x4c && s[1] == 0x89 && s[2] == 0x2d);
        memcpy(&d1, s + 3, 4);
        assert((volatile uintptr_t *)(s + 7 + d1) == &ad.saved[2 * AD_CALL_EXEC]);
        assert(s[7] == 0x48 && s[8] == 0x89 && s[9] == 0x2d);
        memcpy(&d2, s + 10, 4);
        assert((volatile uintptr_t *)(s + 14 + d2) == &ad.saved[2 * AD_CALL_EXEC + 1]);
        assert(s[14] == 0xff && s[15] == 0x25);
        memcpy(&target, s + 20, sizeof(target));
        assert(target == (void *)ad_exec_run);
    }
    ad_uninstall();
    assert(!ad.installed);
    for (size_t k = 0; k < AD_CALLS; ++k) {
        int32_t rel; memcpy(&rel, ad.calls[k].site + 1, 4);
        assert(ad.calls[k].site + 5 + rel == ad.calls[k].target);
    }
    for (int k = 0; k < AD_VSITES; ++k) assert(*ad.ventry[k] == image + x4_ai_vsites[k].fn);

    {   /* changed call target -> install nothing */
        unsigned char *site = image + x4_ai_calls[1].site;
        int32_t saved, bad;
        memcpy(&saved, site + 1, 4); bad = saved + 0x10; memcpy(site + 1, &bad, 4);
        assert(ad_install(AD_CENSUS, 0) && !ad.installed);
        for (int k = 0; k < AD_VSITES; ++k) assert(*ad.ventry[k] == image + x4_ai_vsites[k].fn);
        memcpy(site + 1, &saved, 4);
    }
    {   /* changed vtable entry -> install nothing, call sites untouched */
        void **entry = (void **)(image + x4_ai_vsites[2].entry);
        void *saved = *entry; *entry = image + 7;
        assert(ad_install(AD_CENSUS, 0) && !ad.installed);
        int32_t rel; memcpy(&rel, (image + x4_ai_calls[0].site) + 1, 4);
        assert((image + x4_ai_calls[0].site) + 5 + rel == image + x4_ai_calls[0].target);
        *entry = saved;
    }

    /* ---- per-sector census: four sectors, 1/2/3/4 ms, no cross-sector wakes ------------------- */
    assert(!ad_install(AD_CENSUS, 0) && ad.installed);
    {
        void *c0 = make_ctx(0), *c1 = make_ctx(1), *c2 = make_ctx(2), *c3 = make_ctx(3);
        static int none[32]; for (int i = 0; i < 32; ++i) none[i] = -1;
        g_wake_plan = none;
        run_cost_us[0] = 1000; run_cost_us[1] = 2000; run_cost_us[2] = 3000; run_cost_us[3] = 4000;
        vec_push(dir_widx(), c0); vec_push(dir_widx(), c1); vec_push(dir_widx(), c2); vec_push(dir_widx(), c3);
        ad_note_sim_frame(20.0);
        census_frame();
        assert(ad.win.frames == 1 && ad.win.runs == 4 && ad.win.irq_runs == 0 && ad.win.passes == 1);
        assert(ad.win.ext_wakes == 4 && ad.win.ext_bits[0] == 4);   /* bit 0 = start wake */
        assert(fabs(ad.win.run_ms - 10.0) < 1e-6);
        assert(fabs(ad.win.sec_sum_ms - 10.0) < 1e-6 && fabs(ad.win.sec_max_ms - 4.0) < 1e-6);
        assert(ad.win.sec_buckets == 4 && ad.win.keyless_runs == 0 && ad.win.hazard_runs == 0);
        assert(ad.win.wake_own == 0 && ad.win.wake_foreign == 0);
        assert(fabs(ad.win.sec_sum_ms / ad.win.sec_max_ms - 2.5) < 1e-6);   /* ideal per-sector speedup */
        assert(fabs(ad.win.sim_ms - 20.0) < 1e-6);
    }

    /* ---- cross-sector wake + keyless (serial) context ----------------------------------------- */
    setup_world();
    memset(&ad.win, 0, sizeof(ad.win)); ad_btable_clear(&ad.w_sec);
    {
        void *a = make_ctx(0), *b = make_ctx(1), *orphan = make_ctx(-1);
        static int plan[32]; for (int i = 0; i < 32; ++i) plan[i] = -1;
        plan[0] = 1;                       /* while running a (sector0) wake b (sector1): foreign */
        g_wake_plan = plan;
        run_cost_us[0] = 1000; run_cost_us[1] = 1000; run_cost_us[2] = 1000;
        vec_push(dir_widx(), a); vec_push(dir_widx(), b); vec_push(dir_widx(), orphan);
        census_frame();
        assert(ad.win.runs == 3 && ad.win.passes == 1);
        assert(ad.win.wake_foreign == 1 && ad.win.wake_own == 0 && ad.win.hazard_runs == 1);
        assert(ad.win.keyless_runs == 1 && ad.win.sec_buckets == 3);
        assert(fabs(ad.win.serial_ms - 1.0) < 1e-6);
    }

    /* ---- event own / foreign / cue / director split (ad_event_note, no originals) ------------- */
    setup_world();
    memset(&ad.win, 0, sizeof(ad.win)); ad_btable_clear(&ad.w_sec);
    {
        void *same = make_ctx(0), *same2 = make_ctx(0), *foreign = make_ctx(2);
        ad.in_phase = 1; ad.in_run = 1; ad.run_key = ad_ctx_sector(same); ad.run_hazard = 0;
        ad_event_note(AD_V_CTX, same2);        /* same sector */
        ad_event_note(AD_V_CTX, foreign);      /* other sector */
        ad_event_note(AD_V_CUE, (void *)0x999);/* MD cue: always a barrier/hazard */
        ad.in_run = 0;
        ad_event_note(AD_V_CTX, same);         /* delivered outside a run -> director */
        ad.in_phase = 0;
        assert(ad.win.ev_own == 1 && ad.win.ev_foreign == 1 && ad.win.ev_cue == 1 && ad.win.ev_director == 1);
        assert(ad.run_hazard == 1);
    }

    /* ---- fleet-root resolver: ship B (sector 1) is commanded by ship A (sector 0) -------------- */
    setup_world();
    memset(&ad.win, 0, sizeof(ad.win)); ad_btable_clear(&ad.w_sec);
    {
        static unsigned char shipA[0x100], shipB[0x100];
        memset(shipA, 0, sizeof(shipA)); memset(shipB, 0, sizeof(shipB));
        set_ptr(shipA, 0, fake_vt_ship); set_u64(shipA, AD_COMP_ID, 0xA0); set_ptr(shipA, AD_COMP_PARENT, sectors[0]);
        set_ptr(shipB, 0, fake_vt_ship); set_u64(shipB, AD_COMP_ID, 0xB0); set_ptr(shipB, AD_COMP_PARENT, sectors[1]);
        fake_ships[0] = shipA; fake_ships[1] = shipB;
        void *onB = make_ctx(1), *onA = make_ctx(0);           /* entities re-parented onto the ships */
        set_ptr(entities[0], 0, fake_vt_plain); set_ptr(entities[0], AD_COMP_PARENT, shipB); set_u64(entities[0], AD_COMP_ID, 0xE0);
        set_ptr(entities[1], 0, fake_vt_plain); set_ptr(entities[1], AD_COMP_PARENT, shipA); set_u64(entities[1], AD_COMP_ID, 0xE1);
        fake_entities[0] = entities[0]; fake_entities[1] = entities[1];
        ad_fl.ok = 1; ad_fl.get_commander = fake_get_commander; ad_fl.find = fake_find;
        *(void **)(image + AD_UIDMAP_RVA) = (void *)0x5000;
        static int plan[32]; for (int i = 0; i < 32; ++i) plan[i] = -1;
        plan[0] = 1;                                          /* the run on B wakes the context on A */
        g_wake_plan = plan;
        run_cost_us[0] = 2000; run_cost_us[1] = 1000;
        vec_push(dir_widx(), onB); vec_push(dir_widx(), onA);
        census_frame();
        assert(ad.win.wake_foreign == 1);                       /* sector 1 -> sector 0 */
        assert(ad.win.wake_fleet_same == 1);                    /* same fleet root (A, sector 0) */
        assert(ad.win.sec_buckets == 2 && ad.win.fleet_buckets == 1);   /* both runs in A's sector */
        assert(fabs(ad.win.fleet_max_ms - 3.0) < 1e-6 && ad.win.fleet_hops >= 1);
        ad_fl.ok = 0;
    }

    ad_uninstall();
    assert(!ad.installed);
    VirtualFree(image, 0, MEM_RELEASE);
    puts("PASS: aidriver install/uninstall swaps call sites and vtable slots, all-or-nothing, restored");
    puts("PASS: aidriver stub emits r13/rbp capture + jmp to the wrapper with correct rip-relative targets");
    puts("PASS: aidriver census buckets runs per sector, counts passes, external wakes and sum/max speedup");
    puts("PASS: aidriver classifies cross-sector wakes, keyless serial contexts and hazards");
    puts("PASS: aidriver event split counts same-sector, foreign, cue and director deliveries");
    /* original-order stamp from Move's frame (Q14): pass at rbp-0xa8, read-vector header pointer at
       rbp-0x80, element iterator at rbp-0x88 (rsp+0x58/+0x78 with rbp = rsp+0x100) */
    {
        static unsigned char fr[0x300];
        static void *elems[8], *hdr[3];
        unsigned char *rbp = fr + 0x180;
        hdr[0] = elems; hdr[1] = elems + 5; hdr[2] = elems + 8;
        *(uint32_t *)(rbp + AD_MOVE_PASS_SLOT) = 3;
        *(void ***)(rbp + AD_MOVE_VEC_SLOT) = (void **)hdr;
        *(void ***)(rbp + AD_MOVE_ITER_SLOT) = elems + 4;
        ad_stamp_from_frame(rbp);
        assert(ad.run_pass == 3 && ad.run_pos == 4);
        *(void ***)(rbp + AD_MOVE_ITER_SLOT) = elems + 5;   /* past the end: unknown */
        ad_stamp_from_frame(rbp);
        assert(ad.run_pos == -1);
        ad_stamp_from_frame(NULL);
        assert(ad.run_pos == -1);
    }
    puts("PASS: aidriver fleet-root resolver: commander chain after the phase, fleet buckets, cross-sector wakes inside a fleet");
    puts("PASS: aidriver census original-order stamp (pass, read-vector index) from the Move frame");
    return 0;
}
