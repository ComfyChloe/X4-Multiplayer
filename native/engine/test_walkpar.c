/* Offline synthetic tests for the component-walk census (stage 0) and the deferred-event replay
   (stage 1), in the style of test_fixedtick.c. No X4 process is involved: fake events/components
   with vtables drive the engine in native/engine/walkpar.h directly. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include "fixedtick.c"

struct lua_State { int unused; };
static LARGE_INTEGER fake_now;
static BOOL WINAPI fake_qpc(LARGE_INTEGER *v) { *v = fake_now; return TRUE; }

/* ---- fake engine objects ---------------------------------------------------------------------- */

typedef struct FakeComp { void **vt; uint64_t id; uint64_t sector; } FakeComp;

#define MAX_LISTENERS 4
typedef struct FakeEvent {
    void **vt;
    int type;
    int freed;
    int n;
    FakeComp *listener[MAX_LISTENERS];
    uint64_t payload[MAX_LISTENERS];
} FakeEvent;

static int fake_event_type(void *e) { return ((FakeEvent *)e)->type; }
static void fake_event_free(void *e, int drop) { (void)drop; ((FakeEvent *)e)->freed = 1; }
static void *event_vt[2] = { (void *)fake_event_free, (void *)fake_event_type };
static void *comp_vt[1] = { 0 };

/* component registry for replay re-resolution; a missing id models a source that died */
#define MAX_COMP 64
static FakeComp *g_comp[MAX_COMP];
static int g_comp_n;
static int g_resolve_null;   /* when set, fake_find returns NULL (source gone) */

static FakeComp mk_comp(uint64_t id, uint64_t sector) {
    FakeComp c; c.vt = comp_vt; c.id = id; c.sector = sector;
    return c;
}
static void reg(FakeComp *c) { if (g_comp_n < MAX_COMP) g_comp[g_comp_n++] = c; }

static uint64_t fake_sector(void *comp) { return ((FakeComp *)comp)->sector; }
static void *fake_find(uint64_t id) {
    if (g_resolve_null) return NULL;
    for (int i = 0; i < g_comp_n; ++i) if (g_comp[i]->id == id) return g_comp[i];
    return NULL;
}

static int g_cancel_calls;
static void fake_cancel_original(void *src, int type, double minTime) {
    (void)src; (void)type; (void)minTime; ++g_cancel_calls;
}

/* deliver one event to its listeners, timing the delivery for the census */
static void deliver(void *src, FakeEvent *e) {
    wp_deliver_enter(src, e);
    fake_now.QuadPart += 1000;      /* 0.1 ms inside DeliverEvent */
    for (int i = 0; i < e->n; ++i)
        wp_deliver_listener(src, e, e->listener[i], e->payload[i]);
    wp_deliver_exit(src, e);
}

/* the original PostEvent: immediate events deliver now, scheduled ones only schedule */
static int g_scheduled;
static void fake_post_original(void *src, void *event, double time, int imm) {
    (void)time;
    if (imm) deliver(src, (FakeEvent *)event);
    else ++g_scheduled;
}

/* the detour seam: buffer via wp_post, else run the original */
static void engine_post(void *src, FakeEvent *e, double time, int imm) {
    if (!wp_post(src, e, time, imm)) fake_post_original(src, e, time, imm);
}
static void engine_cancel(void *src, int type, double minTime) {
    if (!wp_cancel(src, type, minTime)) fake_cancel_original(src, type, minTime);
}

static void sector_run(FakeComp *sector, int64_t ns, void (*body)(void)) {
    wp_sector_enter(sector);
    fake_now.QuadPart += ns;        /* 10 MHz QPC: ns here are 100ns ticks */
    if (body) body();
    wp_sector_exit(sector);
}

/* ---- shared scenario: two sectors, same-sector + cross-sector immediate posts, one scheduled --- */

static FakeComp S0, S1;             /* sectors */
static FakeComp o0, o1;             /* objects (posters), one per sector */
static FakeComp l0, l1;             /* listeners, one per sector */
static FakeEvent e_same, e_cross, e_sched, e_tz;

static void scene_init(void) {
    g_comp_n = 0; g_resolve_null = 0; g_scheduled = 0; g_cancel_calls = 0;
    S0 = mk_comp(100, 100); S1 = mk_comp(101, 101);
    o0 = mk_comp(200, 100); o1 = mk_comp(201, 101);
    l0 = mk_comp(300, 100); l1 = mk_comp(301, 101);
    reg(&S0); reg(&S1); reg(&o0); reg(&o1); reg(&l0); reg(&l1);
    e_same = (FakeEvent){ event_vt, WP_TYPE_ATTACKED, 0, 1, { &l0 }, { 0xaa } };
    e_cross = (FakeEvent){ event_vt, WP_TYPE_CHANGED_ATTN, 0, 1, { &l1 }, { 0xbb } };
    e_sched = (FakeEvent){ event_vt, WP_TYPE_WARP, 0, 1, { &l1 }, { 0xcc } };
    /* AddTempZoneEvent's listener is the sector itself (Sector::OnAddTempZoneEvent) */
    e_tz = (FakeEvent){ event_vt, WP_TYPE_ADD_TEMP_ZONE, 0, 1, { &S1 }, { 0xdd } };
}

static void s0_body(void) {
    engine_post(&o0, &e_same, 10.0, 1);    /* same-sector immediate */
    engine_post(&o0, &e_cross, 10.0, 1);   /* cross-sector immediate (-> S1 listener) */
    engine_post(&o0, &e_sched, 20.0, 0);   /* scheduled (no same-frame delivery) */
}
static void s1_body(void) {
    engine_post(&o1, &e_tz, 10.0, 1);      /* AddTempZone: must stay synchronous */
}

static void run_scene(void) {
    wp_walk_begin();
    sector_run(&S0, 20000, s0_body);       /* 2 ms */
    sector_run(&S1, 30000, s1_body);       /* 3 ms */
    wp_walk_end();
}

/* ---- tests ------------------------------------------------------------------------------------ */

static void engine_setup(int mode) {
    qpc_read = fake_qpc;
    qpc_frequency = 10000000;      /* 10 MHz */
    wp_qpc_hz_cache = qpc_frequency;
    fake_now.QuadPart = 1000000;
    wp_sector_resolver = fake_sector;
    wp_registry_find = fake_find;
    wp_deliver_post = fake_post_original;
    wp_deliver_cancel = fake_cancel_original;
    wp_window_clear();
    wp_logA_n = wp_logB_n = 0;
    wp_cue_xor[0] = wp_cue_xor[1] = wp_cue_cnt[0] = wp_cue_cnt[1] = 0;
    wp_log_overflow = 0;
    wp_ready = WP_READY_OK;
    wp_thread = 0;
    wp_mode = mode;     /* white-box: set directly (no native dispatcher in the offline test) */
    wp_frame_reset();   /* scope-only tests don't call wp_walk_begin, so clear the frame here */
}

int main(void) {
    /* fail-closed arm: a blank image fails signature verification -> engine refuses to enable */
    image = VirtualAlloc(NULL, X4_IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image);
    qpc_frequency = 10000000;
    walkpar_arm();
    assert(wp_ready == WP_READY_REFUSED);
    walkpar_set_mode(WP_REPLAY);
    assert(wp_mode == WP_OFF);              /* refused -> stays off */

    /* Exercise the native binding path, not just engine_setup's injected fakes. */
    wp_det[WPD_POST].tramp = (void *)fake_post_original;
    wp_det[WPD_CANCEL].tramp = (void *)fake_cancel_original;
    wp_det[WPD_CANCEL].installed = 1;
    wp_bind_replay_callbacks();
    assert(wp_registry_find == wp_native_find);
    assert(wp_deliver_post == fake_post_original);
    assert(wp_deliver_cancel == fake_cancel_original);
    assert(wp_replay_ready());
    assert(wp_native_find(200) == NULL);     /* no registry while loading */
    wp_det[WPD_CANCEL].installed = 0;
    wp_bind_replay_callbacks();
    assert(!wp_replay_ready());             /* a stale trampoline after census rearm is not enough */
    wp_det[WPD_POST].tramp = wp_det[WPD_CANCEL].tramp = NULL;

    /* Missing wiring must pass through before taking ownership of an event. */
    for (int missing = 0; missing < 3; ++missing) {
        engine_setup(WP_REPLAY);
        scene_init();
        if (missing == 0) wp_registry_find = NULL;
        if (missing == 1) wp_deliver_post = NULL;
        if (missing == 2) wp_deliver_cancel = NULL;
        wp_walk_begin();
        engine_post(&o0, &e_sched, 20.0, 0);
        engine_cancel(&o0, WP_TYPE_WARP, WP_CANCEL_ALL);
        wp_walk_end();
        assert(g_scheduled == 1 && g_cancel_calls == 1);
        assert(wp_count == 0 && !e_sched.freed);
        assert(wp_frame.replayed == 0 && wp_frame.replay_dropped == 0);
        assert(wp_frame.degraded_frames == 2 && wp_replay_abort);
    }
    wp_replay_abort = 0;

    /* native sector resolver: +0x70 parent walk to the Sector vtable, cached per frame */
    {
        void **sec = (void **)(image + 0x9000);
        sec[0] = (void *)(image + WP_SECTOR_VFTABLE_RVA);          /* a sector */
        *(uint64_t *)((char *)sec + 8) = 777;
        void **ship = (void **)(image + 0x9100);
        ship[0] = (void *)(image + 0x1000);                        /* not the sector vtable */
        *(uint64_t *)((char *)ship + 8) = 111;
        *(void **)((char *)ship + 0x70) = sec;                     /* parent = sector */
        void **loose = (void **)(image + 0x9200);
        loose[0] = (void *)(image + 0x1000);
        *(void **)((char *)loose + 0x70) = NULL;                   /* no sector ancestor */
        memset(wp_scache_key, 0, sizeof(wp_scache_key));
        assert(wp_native_sector(ship) == 777);
        assert(wp_native_sector(loose) == 0);
        assert(wp_native_sector(NULL) == 0);
        *(void **)((char *)ship + 0x70) = NULL;                    /* same frame -> cached */
        assert(wp_native_sector(ship) == 777);
        memset(wp_scache_key, 0, sizeof(wp_scache_key));           /* new frame -> recomputed */
        assert(wp_native_sector(ship) == 0);
        wp_sector_resolver = NULL;
    }

    /* Stage 0 census: counters fold into the window, cross-sector flags, sector timing. */
    engine_setup(WP_CENSUS);
    scene_init();
    run_scene();
    assert(wp_win.frames == 1);
    assert(wp_win.posts == 4);              /* same, cross, sched (S0) + tz (S1) */
    assert(wp_win.posts_imm == 3 && wp_win.posts_sched == 1);
    assert(wp_win.xsector_post == 0);       /* both posters are in their own sector */
    /* census never buffers */
    assert(wp_win.buffered == 0 && wp_win.replayed == 0);
    /* deliveries: e_same, e_cross, e_tz are immediate (3 DeliverEvent calls, 3 listeners) */
    assert(wp_win.deliver_calls == 3 && wp_win.deliver_listeners == 3);
    assert(wp_win.xsector_listener == 1);   /* e_cross delivered during S0 to an S1 listener */
    assert(wp_win.posts_temp_zone_inline == 0); /* only counted while buffering (replay) */
    /* sector timing: 2 ms + 3 ms = 5 ms, max 3 ms (plus the small deliver time inside each) */
    assert(wp_win.sector_ms > 5.0 && wp_win.sector_ms < 5.6);
    assert(wp_win.sector_max_ms > 3.0 && wp_win.sector_max_ms < 3.4);
    assert(wp_ideal_speedup() > 1.4 && wp_ideal_speedup() < 1.9);

    /* Native path: the walk is bracketed by the dispatcher (wp_walk_begin sets in_walk) with NO
       sector bracket (no Sector::Move body patch). Posts are still counted and cross-sector is
       derived from source-vs-listener sectors via the resolver. */
    engine_setup(WP_CENSUS);
    scene_init();
    wp_walk_begin();                                 /* in_walk=1, cur_sector stays 0 */
    engine_post(&o0, &e_same, 10.0, 1);              /* o0 in 100 -> l0 in 100: same-sector */
    engine_post(&o0, &e_cross, 10.0, 1);             /* o0 in 100 -> l1 in 101: cross-sector */
    wp_walk_end();
    assert(wp_win.posts == 2 && wp_win.posts_imm == 2 && wp_win.posts_rootlevel == 0);
    assert(wp_win.deliver_listeners == 2 && wp_win.xsector_listener == 1);

    /* Stage 1 functional: a NORMAL run (log A) and a REPLAY run (log B) of the same scene agree. */
    engine_setup(WP_CENSUS);     /* normal run: phase off, deliveries inline -> A */
    scene_init();
    run_scene();
    size_t a_records = wp_logA_n;
    assert(a_records == 3);      /* e_same->l0, e_cross->l1, e_tz->l0 */

    wp_mode = WP_REPLAY;          /* replay run: buffer + replay -> B (white-box; gate bypassed) */
    scene_init();
    run_scene();
    assert(wp_win.buffered >= 3);                    /* same, cross, sched buffered (tz stays inline) */
    assert(wp_frame.posts_temp_zone_inline == 1);    /* AddTempZone delivered inline, not buffered */
    assert(wp_logB_n == 3);
    int detail = 0;
    assert(wp_compare(&detail) == 0);                /* normal vs replay: per-listener sequences equal */

    /* The scheduled post was buffered and replayed as a schedule insert (no listener record). */
    assert(wp_win.replayed >= 3);

    /* no-data guard: empty or overflowed logs are not a false pass */
    wp_logA_n = wp_logB_n = 0; wp_log_overflow = 0;
    assert(wp_compare(&detail) == WP_CMP_NODATA);
    wp_logA_n = wp_logB_n = 1; wp_log_overflow = 1;
    assert(wp_compare(&detail) == WP_CMP_NODATA);
    wp_log_overflow = 0;

    /* Cancel kills only SCHEDULED (imm==0) buffered posts and frees them; an immediate post stays. */
    engine_setup(WP_REPLAY);
    scene_init();
    wp_walk_begin();
    wp_sector_enter(&S0);
    engine_post(&o0, &e_same, 10.0, 1);              /* immediate ATTACKED -> l0 */
    engine_post(&o0, &e_sched, 20.0, 0);             /* scheduled WARP */
    assert(wp_count == 2);
    engine_cancel(&o0, WP_TYPE_WARP, WP_CANCEL_ALL); /* kills the scheduled WARP post, frees it */
    assert(wp_buf[1].dead == 1 && e_sched.freed == 1);
    engine_cancel(&o0, WP_TYPE_ATTACKED, WP_CANCEL_ALL); /* must NOT kill the delivered immediate */
    assert(wp_buf[0].dead == 0);
    wp_sector_exit(&S0);
    wp_walk_end();
    assert(wp_logB_n == 1 && wp_logB[0].listener_id == 300);  /* only the immediate replayed, to l0 */
    assert(wp_frame.cancels == 2 && wp_frame.cancels_applied == 2);

    /* Replay re-resolves by UniverseID; a gone source drops the event (and frees it). */
    engine_setup(WP_REPLAY);
    scene_init();
    wp_walk_begin();
    wp_sector_enter(&S0);
    engine_post(&o0, &e_same, 10.0, 1);
    wp_sector_exit(&S0);
    g_resolve_null = 1;                               /* source cannot be found at replay */
    wp_walk_end();
    assert(wp_frame.replay_dropped == 1 && wp_frame.replayed == 0);
    assert(e_same.freed == 1);

    /* Overflow fails closed: once the buffer is full, further posts pass through (not dropped). */
    engine_setup(WP_REPLAY);
    scene_init();
    wp_walk_begin();
    wp_sector_enter(&S0);
    for (int i = 0; i < WP_OP_MAX; ++i) engine_post(&o0, &e_same, 10.0, 0);
    assert(wp_count == WP_OP_MAX);
    int before = g_scheduled;
    engine_post(&o0, &e_same, 10.0, 0);               /* overflow -> pass through to the original */
    assert(g_scheduled == before + 1 && wp_frame.degraded_frames == 1);
    wp_sector_exit(&S0);
    wp_walk_end();

    /* Difference detection: an injected divergence is reported with the offending event type. */
    wp_logA_n = wp_logB_n = 0; wp_task_count = 0; wp_log_overflow = 0;
    wp_cue_xor[0] = wp_cue_xor[1] = wp_cue_cnt[0] = wp_cue_cnt[1] = 0;
    wp_logA[wp_logA_n++] = (wp_cmp_rec){ 0, WP_TYPE_ATTACKED, 0, 200, 300, 0, 0xaa };
    wp_logB[wp_logB_n++] = (wp_cmp_rec){ 0, WP_TYPE_WARP, 0, 200, 300, 0, 0xaa };
    detail = 0;
    assert(wp_compare(&detail) == -1 && (detail & 0xffff) == WP_TYPE_ATTACKED);

    /* MD cue activations compare as a multiset; a count/xor mismatch is caught (logs non-empty). */
    wp_logA_n = wp_logB_n = 0; wp_log_overflow = 0;
    wp_logA[wp_logA_n++] = (wp_cmp_rec){ 0, WP_TYPE_ATTACKED, 0, 200, 300, 0, 0xaa };
    wp_logB[wp_logB_n++] = (wp_cmp_rec){ 0, WP_TYPE_ATTACKED, 0, 200, 300, 0, 0xaa };
    wp_cue_cnt[0] = 2; wp_cue_xor[0] = 0x11 ^ 0x22;
    wp_cue_cnt[1] = 2; wp_cue_xor[1] = 0x22 ^ 0x11;  /* same multiset, different order */
    assert(wp_compare(&detail) == 0);
    wp_cue_xor[1] = 0x22 ^ 0x33;                      /* different set */
    assert(wp_compare(&detail) == -1 && (detail & 0xffff) == (WP_REC_CUE & 0xffff));

    /* Shared buffer for the ai-driver AI phase: scopes keyed by bucket, replay in caller order. */
    engine_setup(WP_REPLAY);
    scene_init();
    wp_thread = GetCurrentThreadId();
    wp_scope_begin(100);                              /* bucket for sector 100 */
    engine_post(&o0, &e_same, 10.0, 1);              /* buffered under task(100) */
    wp_scope_end();
    wp_scope_begin(101);                              /* bucket for sector 101 */
    engine_post(&o1, &e_cross, 10.0, 1);             /* buffered under task(101) */
    wp_scope_end();
    assert(wp_buffer_count() == 2);
    {
        uint64_t sid; int tk, ty;
        assert(wp_buffer_op(0, &sid, &tk, &ty) == WP_OP_POST && sid == 200 && ty == WP_TYPE_ATTACKED);
        assert(wp_buffer_op(1, &sid, &tk, &ty) == WP_OP_POST && sid == 201);
    }
    /* replay bucket 101 before 100: the 101 listener delivery happens first */
    wp_logB_n = 0;
    uint64_t order[2] = { 101, 100 };
    wp_replay_tasks(order, 2);
    assert(wp_buffer_count() == 0);
    assert(wp_logB_n == 2 && wp_logB[0].listener_id == 301 && wp_logB[1].listener_id == 300);

    /* review #3 (ai-driver path): a cross-task cancel reaches the earlier scheduled post of the same
       (src,type) regardless of task, because wp_cancel's buffer-kill is not task-filtered. So
       replay_tasks ordering A-before-B cannot leave that post live. (wp_replay keeps global post
       order on the walk path; this documents the AI-phase scope path.) */
    engine_setup(WP_REPLAY);
    scene_init();
    wp_thread = GetCurrentThreadId();
    wp_scope_begin(101);                              /* task B: earlier scheduled WARP on o1 */
    engine_post(&o1, &e_sched, 20.0, 0);
    wp_scope_end();
    wp_scope_begin(100);                              /* task A: later cancel of o1 WARP */
    engine_cancel(&o1, WP_TYPE_WARP, WP_CANCEL_ALL);
    wp_scope_end();
    assert(e_sched.freed == 1);                       /* the task-B scheduled post was killed+freed */
    uint64_t order2[2] = { 100, 101 };                /* replay task A (cancel) before task B */
    wp_replay_tasks(order2, 2);
    assert(wp_frame.cancels_applied == 1 && wp_frame.replayed == 0);  /* post dead, only cancel ran */

    /* Q20: sector grouping must not change original A1 B1 A2 emission order.
       Equal keys retain emission order; full-width keys include the pass number. */
    engine_setup(WP_REPLAY);
    scene_init();
    wp_walk_begin();
    FakeEvent a1 = e_same, a2 = e_same, b1 = e_same, b2 = e_same;
    a1.payload[0] = 1; a2.payload[0] = 3; b1.payload[0] = 2; b2.payload[0] = 4;
    wp_scope_begin(100);
    wp_set_order((1ull << 32) | 0); engine_post(&o0, &a1, 10, 1);
    wp_set_order((1ull << 32) | 2); engine_post(&o0, &a2, 10, 1);
    wp_scope_end();
    wp_scope_begin(101);
    wp_set_order((1ull << 32) | 1); engine_post(&o1, &b1, 10, 1);
    wp_set_order((1ull << 32) | 2); engine_post(&o1, &b2, 10, 1);
    wp_scope_end();
    wp_replay_ordered();
    assert(wp_logB_n == 4 && wp_count == 0 && wp_order == 0);
    for (unsigned i = 0; i < 4; ++i) assert(wp_logB[i].payload == i + 1);
    wp_replay_ordered();
    assert(wp_logB_n == 4);  /* never replay consumed events twice */

    /* A logically later post executed first must survive an earlier-key cancel. */
    engine_setup(WP_REPLAY);
    scene_init();
    wp_walk_begin();
    wp_scope_begin(101);
    wp_set_order(20); engine_post(&o1, &e_sched, 20, 0);
    wp_scope_end();
    wp_scope_begin(100);
    wp_set_order(10); engine_cancel(&o1, WP_TYPE_WARP, WP_CANCEL_ALL);
    wp_scope_end();
    assert(!e_sched.freed && !wp_buf[0].dead);
    wp_replay_ordered();
    assert(wp_frame.replayed == 1 && wp_frame.cancels_applied == 1);
    assert(wp_buf[0].kind == WP_OP_CANCEL && wp_buf[1].kind == WP_OP_POST);

    /* Earlier-key scheduled posts still cancel, immediate posts never cancel. */
    engine_setup(WP_REPLAY);
    scene_init();
    wp_walk_begin();
    wp_set_order(10); engine_post(&o0, &e_sched, 20, 0);
    wp_set_order(10); engine_post(&o0, &e_same, 10, 1);
    wp_set_order(20); engine_cancel(&o0, WP_TYPE_WARP, WP_CANCEL_ALL);
    engine_cancel(&o0, WP_TYPE_ATTACKED, WP_CANCEL_ALL);
    assert(e_sched.freed && !e_same.freed);
    int imm = -1; void *borrowed = &e_same;
    assert(wp_buffer_event(0, &imm, &borrowed) == 0 && !borrowed && imm == 0);
    assert(wp_buffer_event(1, &imm, &borrowed) == WP_OP_POST && imm == 1 && borrowed == &e_same);
    assert(wp_buffer_event(2, &imm, &borrowed) == WP_OP_CANCEL && imm == 0 && !borrowed);
    wp_replay_ordered();
    assert(wp_frame.replayed == 1 && wp_frame.cancels_applied == 2);
    assert(wp_buffer_event(0, &imm, &borrowed) == 0 && !borrowed);

    /* AI-only mode keeps the whole galaxy walk inline, buffering explicit scopes. */
    engine_setup(WP_AI_REPLAY);
    scene_init();
    assert(wp_get_mode() == WP_AI_REPLAY);
    wp_walk_begin();
    engine_post(&o0, &e_sched, 20, 0);
    assert(wp_count == 0 && g_scheduled == 1);
    wp_walk_end();
    wp_scope_begin(100);
    engine_post(&o0, &e_same, 10, 1);
    assert(wp_count == 1 && wp_frame.replayed == 0);
    /* replay closes buffering itself, even when the caller left the scope open */
    wp_replay_ordered();
    assert(!wp_phase && wp_count == 0 && wp_frame.replayed == 1);
    wp_scope_end();

    /* Foreign thread: entry points pass through and count nothing. */
    engine_setup(WP_CENSUS);
    scene_init();
    wp_walk_begin();
    ++wp_thread;                                      /* pretend another thread calls in */
    assert(wp_post(&o0, &e_same, 10.0, 1) == 0);
    wp_deliver_listener(&o0, &e_same, &l0, 0xaa);
    --wp_thread;
    wp_walk_end();
    assert(wp_win.posts == 0 && wp_win.deliver_listeners == 0);

    VirtualFree(image, 0, MEM_RELEASE);
    puts("PASS: stage 0 census counts posts/deliveries/cross-sector flags and sector sum/max time");
    puts("PASS: stage 1 replay equivalence per listener (SYNTHETIC only: one process, identical input)");
    puts("PASS: AddTempZoneEvent stays synchronous; source UniverseID stored and re-resolved at replay");
    puts("PASS: cancel kills only scheduled posts and frees them; gone source dropped+freed; overflow passes through");
    puts("PASS: comparison reports no-data, the first differing type, and cue activations as a multiset");
    puts("PASS: ai-driver scope + ordered replay; foreign-thread entry points pass through and count nothing");
    puts("PASS: Q20 original-order keys, cancel ordering, borrowed-event readout and AI-only scope mode");
    return 0;
}
