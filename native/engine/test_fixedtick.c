#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include "fixedtick.c"

struct lua_State { int unused; };
static struct lua_State fake_lua;
static owner fake_owner;
static void *fake_table[40];
static LARGE_INTEGER fake_now;

static void *checkudata(lua_State *L, int index, const char *name) {
    (void)L; assert(index == 1 && !strcmp(name, META)); return &fake_owner;
}
static BOOL WINAPI fake_qpc(LARGE_INTEGER *value) { *value = fake_now; return TRUE; }

static void setup(void) {
    memset(fake_table, 0, sizeof(fake_table));
    memset(&fake_owner, 0, sizeof(fake_owner));
    fake_table[3] = &fake_owner;
    fake_table[SLOT] = &hook;
    *(void ***)(image + VBI_RVA) = fake_table;
    registered_table = fake_table;
    fake_owner.armed = 1;
    current = &fake_owner;
    owner_thread = GetCurrentThreadId();
    qpc_frequency = 10000000;
    qpc_read = fake_qpc;
    fake_now.QuadPart = 100000000;
    *(double *)(image + GAME_TIME_RVA) = 10.0;
    *(double *)(image + REAL_TIME_RVA) = 20.0;
    *(double *)(image + SPEED_RVA) = 1.0;
    patched = 0;
    *(unsigned short *)(image + DT_BRANCH_RVA) = branch_original;
    *(unsigned char *)(image + CAPTURE_ACTIVE_RVA) = 0;
    *(double *)(image + CAPTURE_DT_RVA) = 1.0 / 30.0;
    *(unsigned char *)(image + LIMIT_ENABLED_RVA) = 1;
    *(unsigned *)(image + LIMIT_FPS_RVA) = 60;
}

/* Mirrors the fixed branch of start() after its executable checks. */
static void setup_fixed(void) {
    setup();
    saved_capture_dt = *capture_dt();
    *capture_dt() = 1.0 / FIXED_HZ;
    assert(write_branch(branch_original, branch_patched));
    patched = 1;
    fake_owner.fixed = 1;
}

static int fake_collision_steps;

/* Mirrors the catch-up branch of start() after its executable checks. */
static void setup_catchup(void) {
    setup_fixed();
    fake_collision_steps = 2;
    *(int **)(image + COLLISION_STEPS_PTR_RVA) = &fake_collision_steps;
    collision_steps = &fake_collision_steps;
    saved_collision_steps = fake_collision_steps;
    fake_collision_steps = 1;
    fake_owner.catchup = 1;
    fake_owner.last_steps = 1;
    *(unsigned *)(image + LIMIT_FPS_RVA) = 144;
    *(int64_t *)(image + FRAME_QPC_RVA) = 1000000;
}

/* One engine frame of `seconds` wall time: FrameTick stored a new QPC (10 MHz). */
static void catchup_frame(double seconds) {
    *(int64_t *)(image + FRAME_QPC_RVA) += (int64_t)(seconds * 10000000.0 + 0.5);
    fake_now.QuadPart += (int64_t)(seconds * 10000000.0 + 0.5);
    move(&hook);
}

static void run_catchup(double fps, int frames) {
    setup_catchup();
    move(&hook);
    for (int i = 0; i < frames; ++i) catchup_frame(1.0 / fps);
}

static void fixed_frame(void) {
    fake_now.QuadPart += 166667;
    *(double *)(image + GAME_TIME_RVA) += 1.0 / 60.0;
    *(double *)(image + REAL_TIME_RVA) += 1.0 / 60.0;
    move(&hook);
}

static void assert_restored(const char *reason) {
    assert(!fake_owner.armed && !fake_table[SLOT] && !patched);
    assert(*(unsigned short *)(image + DT_BRANCH_RVA) == branch_original);
    assert(reason ? fake_owner.reason && !strcmp(fake_owner.reason, reason) : !fake_owner.reason);
}

/* ---- Stage 3 harness. The call-site bytes are synthetic filler around a generic
   `ff 15 disp32`; no engine bytes are used. ---- */
#define FAKE_SEM ((HANDLE)(uintptr_t)0x5151)
static const int64_t MS = 10000;  /* the fake QPC runs at 10 MHz */
static unsigned char fake_fs[0x400];
static unsigned char fake_sl_helper[0x40];
static unsigned char fake_sl_state[8];
static unsigned char *fake_ffx;
static DWORD wait_result = WAIT_TIMEOUT, last_timeout;
static int64_t wait_advance;      /* QPC ticks the fake wait consumes */
static char calls[64];
static unsigned call_count;
static int render_changes_index, render_bumps_queues;
static unsigned char window_original[16];

static DWORD WINAPI fake_wait(HANDLE h, DWORD ms) {
    assert(h == FAKE_SEM);
    last_timeout = ms;
    fake_now.QuadPart += wait_advance;
    return wait_result;
}
static void fake_begin(void) { calls[call_count++] = 'b'; }
static unsigned char fake_render(void *fs, unsigned char threaded, uint64_t unused) {
    assert(fs == fake_fs && threaded == 1 && unused == 0);
    calls[call_count++] = 'r';
    if (render_changes_index) *(int *)(image + READ_INDEX_RVA) ^= 1;
    if (render_bumps_queues) ++*(int *)(fake_fs + FS_QUEUE_COUNT);  /* a concurrent producer */
    return 1;
}
static void fake_present(void *fs, unsigned ok, unsigned zero) {
    assert(fs == fake_fs && ok == 1 && zero == 0);
    calls[call_count++] = 'p';
}

typedef struct {
    HANDLE sem; int index, queues, temp_pause; unsigned pending;
    double game, real; int64_t frame_qpc; void *iat; unsigned char in_flight, first;
} protected_state;
static protected_state snapshot(void) {
    protected_state p;
    memset(&p, 0, sizeof(p));
    p.sem = *(HANDLE *)(image + SEM_SIM_DONE_RVA);
    p.in_flight = image[SIM_IN_FLIGHT_RVA];
    p.index = *(int *)(image + READ_INDEX_RVA);
    p.queues = *(int *)(fake_fs + FS_QUEUE_COUNT);
    p.game = *(double *)(image + GAME_TIME_RVA);
    p.real = *(double *)(image + REAL_TIME_RVA);
    p.frame_qpc = *(int64_t *)(image + FRAME_QPC_RVA);
    p.iat = *(void **)(image + WAIT_IAT_RVA);
    p.pending = *(unsigned *)(image + GFX_PENDING_RVA);
    p.temp_pause = *(int *)(image + TEMP_PAUSE_RVA);
    p.first = image[FIRST_FRAME_RVA];
    return p;
}

/* Executes the main loop's `call [rip+disp32]` as the CPU does: [next_ip + disp]. */
static wait_fn call_target(void) {
    int32_t d;
    memcpy(&d, image + WAIT_DISP_RVA, sizeof(d));
    return *(wait_fn *)(image + WAIT_NEXT_RVA + d);
}
static DWORD call_site(DWORD ms) {
    DWORD saved = owner_thread;
    owner_thread = GetCurrentThreadId() + 4;  /* the wrapper runs on the main thread, not the MoveWorker */
    DWORD r = call_target()(FAKE_SEM, ms);
    owner_thread = saved;
    return r;
}

static void setup_stage3_with(int represent_on) {
    setup_catchup();
    fake_owner.stage3 = 1;
    fake_owner.k_cap = DEFAULT_K_CAP;
    memset(image + 0x12ae889, 0x90, 36);
    image[0x12ae895] = 0xff; image[0x12ae896] = 0x15;
    int32_t iat = (int32_t)(WAIT_IAT_RVA - WAIT_NEXT_RVA);
    memcpy(image + WAIT_DISP_RVA, &iat, sizeof(iat));
    memcpy(window_original, image + WAIT_WINDOW_RVA, 16);
    *(void **)(image + WAIT_IAT_RVA) = (void *)fake_wait;
    *(HANDLE *)(image + SEM_SIM_DONE_RVA) = FAKE_SEM;
    image[SIM_IN_FLIGHT_RVA] = 1;
    image[WINDOW_SUSPENDED_RVA] = 0;
    *(unsigned *)(image + APP_STATE_RVA) = 0;
    *(int *)((unsigned char *)fake_table + 0x130) = 0;
    *(void **)(image + MOVE_WORKER_RVA) = &fake_owner;
    *(void **)(image + FRAME_STATE_PTR_RVA) = fake_fs;
    memset(fake_fs, 0, sizeof(fake_fs));
    fake_fs[FS_THREADED] = 1;
    *(int *)(fake_fs + FS_QUEUE_COUNT) = 7;
    image[FIRST_FRAME_RVA] = 0;
    *(unsigned *)(image + GFX_PENDING_RVA) = 0;
    memset(fake_sl_state, 0, sizeof(fake_sl_state));
    *(void **)(fake_sl_helper + 0x30) = fake_sl_state;
    *(void **)(image + STREAMLINE_PTR_RVA) = fake_sl_helper;
    fake_ffx[FFX_FRAME_GEN] = 0;
    *(void **)(image + FFX_PTR_RVA) = fake_ffx;
    *(int *)(image + TEMP_PAUSE_RVA) = 0;
    *(int *)(image + READ_INDEX_RVA) = 1;
    if (s3.page) VirtualFree(s3.page, 0, MEM_RELEASE);
    memset(&s3, 0, sizeof(s3));
    assert(!stage3_prepare());
    s3.enabled = 1; s3.represent = represent_on; s3.short_wait = 0;
    begin_render = fake_begin; render_previous = fake_render; present_frame = fake_present;
    memset(&stall, 0, sizeof(stall)); stall.slot = SLOT;
    wait_result = WAIT_TIMEOUT; wait_advance = 0; call_count = 0;
    render_changes_index = render_bumps_queues = 0;
    /* The first callback installs the patch; the second follows one more FrameTick. */
    move(&hook);
    assert(s3.installed && s3.active && s3.frames_since_install == 0);
    catchup_frame(1.0 / 60.0);
    assert(s3.frames_since_install == 1 && fake_owner.armed);
}
static void setup_stage3(void) { setup_stage3_with(1); }

/* A completed simulation frame: the wait succeeds, then FrameTick presents. */
static void real_frame(void) {
    wait_result = WAIT_OBJECT_0; wait_advance = 0;
    assert(call_site(16) == WAIT_OBJECT_0);
    wait_result = WAIT_TIMEOUT;
}

#define GATE_CASE(gate_id, statement) do { \
        setup_stage3(); real_frame(); wait_advance = 20 * MS; statement; \
        assert(call_site(16) == WAIT_TIMEOUT); \
        assert(!s3.re_presents && !call_count && s3.gate_failures[gate_id] == 1); \
    } while (0)

int main(void) {
    assert(!verify_image(0) && !verify_image(1));
    image = VirtualAlloc(NULL, X4_IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image);
    p_luaL_checkudata = checkudata;

    setup(); move(&hook);
    assert(fake_owner.frames == 1 && fake_owner.last_game_dt == 0.0);
    fake_now.QuadPart += 200000;
    *(double *)(image + GAME_TIME_RVA) += 1.0 / 60.0;
    *(double *)(image + REAL_TIME_RVA) += 0.02;
    move(&hook);
    assert(fake_owner.frames == 2);
    assert(fabs(fake_owner.last_wall_dt - 0.02) < 1e-9);
    assert(fabs(fake_owner.last_game_dt - 1.0 / 60.0) < 1e-9);
    assert(fabs(fake_owner.last_real_dt - 0.02) < 1e-9);
    assert(fake_owner.game_time_regressions == 0);

    setup(); *(int *)((unsigned char *)fake_table + 0x130) = 1; move(&hook);
    assert(fake_owner.frames == 0 && fake_owner.loading_skips == 1 && fake_owner.armed);
    setup(); *(double *)(image + SPEED_RVA) = 0.0; move(&hook);
    assert(fake_owner.paused_frames == 1);
    setup(); move(&hook); *(double *)(image + GAME_TIME_RVA) = 9.0; move(&hook);
    assert(fake_owner.game_time_regressions == 1);
    setup(); ++owner_thread; move(&hook);
    assert(!fake_owner.frames && !fake_owner.armed && !fake_table[SLOT]);
    setup(); *(void ***)(image + VBI_RVA) = NULL; move(&hook);
    assert(!fake_owner.frames && !fake_owner.armed && fake_table[SLOT] == &hook);
    setup(); fake_table[SLOT] = &fake_owner; move(&hook);
    assert(!fake_owner.frames && !fake_owner.armed && fake_table[SLOT] == &fake_owner);
    setup(); stop(&fake_lua); stop(&fake_lua);
    assert(!current && !fake_owner.armed && !fake_table[SLOT]);

    setup_fixed();
    assert(image[DT_BRANCH_RVA] == 0x66 && image[DT_BRANCH_RVA + 1] == 0x90);
    move(&hook); fixed_frame(); fixed_frame();
    assert(fake_owner.armed && fake_owner.frames == 3 && fake_owner.fixed_frames == 2);
    assert(fabs(fake_owner.game_elapsed - 2.0 / 60.0) < 1e-9);
    assert(fabs(fake_owner.wall_elapsed - 0.0333334) < 1e-6);
    assert(!write_branch(branch_original, branch_patched));

    setup_fixed(); *(unsigned char *)(image + CAPTURE_ACTIVE_RVA) = 1; move(&hook);
    assert_restored("movie capture active");
    assert(*capture_dt() == 1.0 / 30.0);
    setup_fixed(); *(unsigned *)(image + LIMIT_FPS_RVA) = 144; move(&hook);
    assert_restored("frame-rate limit is not 60");
    setup_fixed(); *(unsigned char *)(image + LIMIT_ENABLED_RVA) = 0; move(&hook);
    assert_restored("frame-rate limit is not 60");
    setup_fixed(); *(unsigned *)(image + LIMIT_FPS_RVA) = 0; move(&hook);
    assert(fake_owner.armed && patched);
    setup_fixed(); *capture_dt() = 1.0 / 24.0; move(&hook);
    assert_restored("capture frame time changed");
    assert(*capture_dt() == 1.0 / 24.0);
    setup_fixed(); *(unsigned short *)(image + DT_BRANCH_RVA) = 0xcccc; move(&hook);
    assert(!fake_owner.armed && !patched && *(unsigned short *)(image + DT_BRANCH_RVA) == 0xcccc);
    setup_fixed(); ++owner_thread; move(&hook);
    assert_restored("callback ownership lost");
    setup_fixed(); stop(&fake_lua); stop(&fake_lua);
    assert_restored(NULL); assert(!current && *capture_dt() == 1.0 / 30.0);

    /* Stage 2: steps track wall time at any frame rate, physics sub-steps equal k. */
    run_catchup(60.0, 600);
    assert(fake_owner.armed && fake_owner.steps_total >= 598 && fake_owner.steps_total <= 600);
    assert(fake_owner.dropped_ticks == 0);
    run_catchup(30.0, 300);
    assert(fake_owner.armed && fake_owner.last_steps == 2 && fake_collision_steps == 2);
    assert(*capture_dt() == 2.0 / 60.0 && fake_owner.steps_total >= 598);
    run_catchup(144.0, 1440);
    assert(fake_owner.armed && fake_owner.zero_step_frames > 700);
    assert(fake_owner.steps_total >= 598 && fake_owner.steps_total <= 600);
    run_catchup(1000.0, 10000);
    assert(fake_owner.armed && fake_owner.steps_total >= 598 && fake_owner.steps_total <= 600);
    setup_catchup(); move(&hook); catchup_frame(1.0);
    assert(fake_owner.last_steps == MAX_CATCHUP && fake_owner.dropped_ticks == 60 - MAX_CATCHUP);
    assert(fake_collision_steps == (int)MAX_CATCHUP);
    setup_catchup(); move(&hook); catchup_frame(1.0 / 30.0);
    fake_collision_steps = 7; catchup_frame(1.0 / 60.0);
    assert(!fake_owner.armed && !patched && fake_owner.reason && !strcmp(fake_owner.reason, "collision steps changed"));
    assert(fake_collision_steps == 2 && *capture_dt() == 1.0 / 30.0);
    setup_catchup(); move(&hook); catchup_frame(1.0 / 30.0);
    *capture_dt() = 0.5; catchup_frame(1.0 / 60.0);
    assert(!fake_owner.armed && !strcmp(fake_owner.reason, "capture frame time changed"));
    setup_catchup(); move(&hook); catchup_frame(1.0 / 30.0); stop(&fake_lua);
    assert(!patched && fake_collision_steps == 2 && *capture_dt() == 1.0 / 30.0);
    assert(*(unsigned short *)(image + DT_BRANCH_RVA) == branch_original);

    /* Stage 2 adjustment: after a slow frame k is capped (default 2); the excess is deferred. */
    setup_catchup(); fake_owner.k_cap = 2; move(&hook); catchup_frame(1.0);
    assert(fake_owner.dropped_ticks == 56 && fake_owner.last_steps == 2 && fake_owner.capped_ticks == 2);
    assert(fake_collision_steps == 2 && *capture_dt() == 2.0 / 60.0 && fake_owner.slow_frames == 1);
    catchup_frame(1.0 / 60.0);
    assert(fake_owner.last_steps == 2 && fake_owner.capped_ticks == 3);
    catchup_frame(1.0 / 60.0);
    assert(fake_owner.last_steps == 2 && fake_owner.capped_ticks == 3 && !fake_owner.capped_last);
    catchup_frame(1.0 / 60.0);
    assert(fake_owner.last_steps == 1 && fake_owner.capped_ticks == 3);
    assert(fake_owner.steps_total == 0 + 2 + 2 + 2 + 1 && fake_owner.dropped_ticks == 56);
    setup_catchup(); fake_owner.k_cap = 4; move(&hook); catchup_frame(1.0);  /* kcap=4: Stage 2 behaviour */
    assert(fake_owner.last_steps == 4 && fake_owner.capped_ticks == 0 && fake_owner.dropped_ticks == 56);
    setup_catchup(); fake_owner.k_cap = 2; move(&hook);
    for (int i = 0; i < 300; ++i) catchup_frame(1.0 / 30.0);
    assert(fake_owner.capped_ticks == 0 && fake_owner.steps_total >= 598);  /* steady 30 fps fits the cap */
    setup_catchup(); fake_owner.k_cap = 2; move(&hook);
    for (int i = 0; i < 200; ++i) catchup_frame(1.0 / 20.0);
    assert(fake_owner.capped_ticks > 0 && fake_owner.last_steps <= 2 && fake_owner.armed);

    /* Options. */
    {
        options opt;
        assert(!parse_options(NULL, &opt) && opt.k_cap == 2 && opt.represent == 1 && opt.short_wait == 1);
        assert(opt.stall_ms == 0 && opt.stall_slot == SLOT);
        assert(!parse_options("kcap=3,represent=0,shortwait=0,stall_ms=100,stall_every=120,stall_slot=12", &opt));
        assert(opt.k_cap == 3 && !opt.represent && !opt.short_wait && opt.stall_ms == 100 &&
               opt.stall_every == 120 && opt.stall_slot == 12);
        const char *bad[] = {"kcap=0", "kcap=5", "foo=1", "kcap=", "kcap", "stall_slot=11", "stall_slot=10",
                             "represent=2", "stall_ms=1001", "stall_every=0", "kcap=2;", "kcap=-1"};
        for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
            const char *why = parse_options(bad[i], &opt);
            assert(why && !strcmp(why, "unsupported option"));
        }
    }

    /* Stage 3: slot page within rel32 reach; only the displacement changes; IAT untouched. */
    fake_ffx = VirtualAlloc(NULL, 0x260000, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(fake_ffx);
    setup_stage3();
    {
        int64_t distance = (int64_t)((intptr_t)s3.page - (intptr_t)(image + WAIT_NEXT_RVA));
        assert(distance >= INT32_MIN && distance <= INT32_MAX && current_displacement() == s3.displacement);
        assert(!memcmp(image + WAIT_WINDOW_RVA, window_original, 7));
        assert(!memcmp(image + WAIT_WINDOW_RVA + 11, window_original + 11, 5));
        assert(*(void **)(image + WAIT_IAT_RVA) == (void *)fake_wait);
        assert(*(void **)s3.page == (void *)wait_or_represent && call_target() == wait_or_represent);
        MEMORY_BASIC_INFORMATION m;
        assert(VirtualQuery(s3.page, &m, sizeof(m)) && (m.Protect & 0xff) == PAGE_READONLY);
    }
    /* WAIT_OBJECT_0 passes through unchanged and never re-presents. */
    real_frame();
    assert(s3.waits_ok == 1 && !s3.re_presents && !call_count && last_timeout == 16 && s3.present_pending);
    {
        protected_state before = snapshot();
        /* WAIT_TIMEOUT inside the frame budget: no re-present (rate limit). */
        wait_advance = 10 * MS;
        assert(call_site(16) == WAIT_TIMEOUT);
        assert(s3.waits_timeout == 1 && s3.budget_skips == 1 && !s3.re_presents && !call_count);
        /* Budget elapsed: exactly begin, render(fs, 1), present(fs, ok, 0); result unchanged. */
        wait_advance = 7 * MS;
        assert(call_site(16) == WAIT_TIMEOUT);
        assert(s3.re_presents == 1 && call_count == 3 && !memcmp(calls, "brp", 3));
        protected_state after = snapshot();
        assert(!memcmp(&before, &after, sizeof(before)));  /* protected globals untouched */
        assert(!s3.index_changed && !s3.queue_count_changed && !s3.in_flight_changed);
        assert(fabs(s3.max_present_gap - 0.017) < 1e-6);
        /* 5 ms later: skipped; 12 ms after that: the second re-present. */
        wait_advance = 5 * MS; call_site(16);
        assert(s3.re_presents == 1 && s3.budget_skips == 2);
        wait_advance = 12 * MS; call_site(16);
        assert(s3.re_presents == 2 && call_count == 6);
        /* A real frame restarts the present clock. */
        real_frame(); wait_advance = 16 * MS; call_site(16);
        assert(s3.re_presents == 2 && s3.budget_skips == 3);
        after = snapshot();
        assert(!memcmp(&before, &after, sizeof(before)));
    }
    /* Other wait results pass through. */
    wait_result = WAIT_FAILED; assert(call_site(16) == WAIT_FAILED && s3.waits_other == 1);
    wait_result = WAIT_TIMEOUT;
    /* A concurrent producer may advance FrameState+0x1e8: counted, not fatal. */
    setup_stage3(); real_frame(); render_bumps_queues = 1; wait_advance = 20 * MS; call_site(16);
    assert(s3.re_presents == 1 && s3.queue_count_changed == 1 && !s3.violation);
    catchup_frame(1.0 / 60.0); assert(fake_owner.armed);

    /* Gates: each failure is counted by reason and prevents the re-present. */
    GATE_CASE(G_SUSPENDED, image[WINDOW_SUSPENDED_RVA] = 1);
    GATE_CASE(G_LOADING, *(unsigned *)(image + APP_STATE_RVA) = 2);
    GATE_CASE(G_LOADING, *(int *)((unsigned char *)fake_table + 0x130) = 1);
    GATE_CASE(G_NOT_IN_FLIGHT, image[SIM_IN_FLIGHT_RVA] = 0);
    GATE_CASE(G_NO_MOVE_WORKER, *(void **)(image + MOVE_WORKER_RVA) = NULL);
    GATE_CASE(G_FIRST_FRAME, image[FIRST_FRAME_RVA] = 1);
    GATE_CASE(G_FIRST_FRAME, s3.frames_since_install = 0);
    GATE_CASE(G_NOT_THREADED, fake_fs[FS_THREADED] = 0);
    GATE_CASE(G_NOT_THREADED, *(void **)(image + FRAME_STATE_PTR_RVA) = NULL);
    GATE_CASE(G_GRAPHICS_PENDING, *(unsigned *)(image + GFX_PENDING_RVA) = 0x40000);
    GATE_CASE(G_MOVIE_CAPTURE, image[CAPTURE_ACTIVE_RVA] = 1);
    GATE_CASE(G_STREAMLINE_REFLEX, fake_sl_state[0] = 1);
    GATE_CASE(G_STREAMLINE_REFLEX, *(void **)(image + STREAMLINE_PTR_RVA) = NULL);
    GATE_CASE(G_DLSS_FG, fake_sl_state[3] = 1);
    GATE_CASE(G_DLSS_FG, fake_sl_state[4] = 1);
    GATE_CASE(G_FSR_FG, fake_ffx[FFX_FRAME_GEN] = 1);
    GATE_CASE(G_SAVING, *(int *)(image + TEMP_PAUSE_RVA) = 1);
    GATE_CASE(G_HANDLE, *(HANDLE *)(image + SEM_SIM_DONE_RVA) = (HANDLE)(uintptr_t)0x9999);
    GATE_CASE(G_THREAD, s3.main_thread = (LONG)(GetCurrentThreadId() + 8));
    /* Called from the MoveWorker thread itself. */
    setup_stage3(); real_frame(); wait_advance = 20 * MS;
    assert(call_target()(FAKE_SEM, 16) == WAIT_TIMEOUT && s3.gate_failures[G_THREAD] == 1 && !s3.re_presents);
    /* No real present observed yet since installation: never re-present. */
    setup_stage3(); wait_advance = 20 * MS; call_site(16);
    assert(s3.budget_skips == 1 && !s3.re_presents);

    /* represent=0: counts would_represent at the same cadence and never calls the engine. */
    setup_stage3_with(0); real_frame();
    wait_advance = 17 * MS; call_site(16);
    wait_advance = 5 * MS; call_site(16);
    wait_advance = 12 * MS; call_site(16);
    assert(s3.would_represent == 2 && s3.budget_skips == 1 && !s3.re_presents && !call_count);

    /* shortwait: once gates pass, the wait is shortened to the present deadline. */
    setup_stage3(); s3.short_wait = 1; real_frame();
    wait_advance = 17 * MS; call_site(16);
    assert(last_timeout == 16 && s3.re_presents == 1);
    fake_now.QuadPart += 5 * MS; wait_advance = 12 * MS; call_site(16);
    assert(last_timeout == 12 && s3.re_presents == 2);
    fake_now.QuadPart += 20 * MS; wait_advance = 0; call_site(16);
    assert(last_timeout == 1);
    s3.gates_ok_last = 0; call_site(16);
    assert(last_timeout == 16);

    /* Inactive wrapper (disarm race): plain pass-through. */
    setup_stage3(); real_frame(); s3.active = 0; wait_advance = 20 * MS;
    assert(call_site(16) == WAIT_TIMEOUT && !s3.re_presents && !s3.waits_timeout && last_timeout == 16);
    s3.active = 1;

    /* Read-index change during a re-present: counted; the next slot-5 guard disarms and restores. */
    setup_stage3(); real_frame(); render_changes_index = 1; wait_advance = 20 * MS; call_site(16);
    assert(s3.index_changed == 1 && s3.violation);
    catchup_frame(1.0 / 60.0);
    assert_restored("render read index changed during a re-present");
    assert(!memcmp(image + WAIT_WINDOW_RVA, window_original, 16) && !s3.page && !s3.installed && !s3.active);
    assert(fake_collision_steps == 2 && *capture_dt() == 1.0 / 30.0);

    /* stop() restores the displacement, frees the page and restores Stage 1/2. */
    setup_stage3();
    {
        unsigned char *page = s3.page;
        stop(&fake_lua); stop(&fake_lua);
        assert(!memcmp(image + WAIT_WINDOW_RVA, window_original, 16) && !s3.page && !s3.active);
        MEMORY_BASIC_INFORMATION m;
        assert(VirtualQuery(page, &m, sizeof(m)) && m.State == MEM_FREE);
        assert_restored(NULL);
        assert(fake_collision_steps == 2 && *capture_dt() == 1.0 / 30.0);
        assert(*(void **)(image + WAIT_IAT_RVA) == (void *)fake_wait && call_target() == fake_wait);
    }
    /* A foreign write to the displacement: disarm, keep the foreign bytes, free the page. */
    setup_stage3();
    {
        int32_t foreign = 0x11223344;
        memcpy(image + WAIT_DISP_RVA, &foreign, sizeof(foreign));
        catchup_frame(1.0 / 60.0);
        assert_restored("stage3 call displacement changed");
        assert(current_displacement() == foreign && !s3.page);
    }
    /* A foreign change elsewhere in the window while the displacement is still ours: nothing
       is written and the page stays alive, because the call still reads it. */
    setup_stage3();
    {
        unsigned char *page = s3.page;
        image[WAIT_WINDOW_RVA] ^= 0xff;
        stop(&fake_lua);
        assert(current_displacement() == s3.displacement && s3.page == page && !s3.active);
        assert(*(void **)page == (void *)wait_or_represent);
        VirtualFree(page, 0, MEM_RELEASE); s3.page = NULL;
    }
    /* Install failure (window changed after verification): fail closed in the first callback. */
    setup_stage3(); stop(&fake_lua);
    setup_catchup(); fake_owner.stage3 = 1; fake_owner.k_cap = 2;
    memset(&s3, 0, sizeof(s3));
    assert(!stage3_prepare()); s3.enabled = 1;
    image[WAIT_WINDOW_RVA + 1] ^= 0xff;
    move(&hook);
    assert_restored("stage3 call-site patch failed");
    assert(!s3.page && !s3.installed && current_displacement() == (int32_t)(WAIT_IAT_RVA - WAIT_NEXT_RVA));
    image[WAIT_WINDOW_RVA + 1] ^= 0xff;

    /* Stall injector: every M-th slot-5 callback sleeps; a separate-slot handler likewise. */
    setup(); stall.ms = 1; stall.every = 2; stall.slot = SLOT; stall.calls = stall.injected = 0;
    for (int i = 0; i < 4; ++i) move(&hook);
    assert(stall.injected == 2);
    setup(); stall.slot = 12; stall.calls = stall.injected = 0; fake_table[12] = &stall_hook;
    for (int i = 0; i < 4; ++i) { move(&hook); stall_move(&stall_hook); }
    assert(stall.injected == 2);
    stop(&fake_lua);
    assert(!fake_table[12] && !fake_table[SLOT]);
    memset(&stall, 0, sizeof(stall));

    VirtualFree(fake_ffx, 0, MEM_RELEASE);
    VirtualFree(image, 0, MEM_RELEASE);
    puts("PASS: fixedtick read-only cadence, pause/loading, lifetime and fail-closed guards");
    puts("PASS: fixed60 dt branch patch, per-frame guards and restore on every disarm");
    puts("PASS: fixed60catchup tracks wall time at 30/60/144/1000 fps, bounded catch-up, Jolt sub-steps, restore");
    puts("PASS: slow-frame k cap (default 2, deferred and counted), kcap=4 keeps Stage 2, option parsing");
    puts("PASS: fixed60stage3 displacement patch/restore, wrapper OBJECT_0/TIMEOUT, gates, budget, protected globals, disarm, stall injector");
    return 0;
}
