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

int main(void) {
    assert(!verify_image());
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

    VirtualFree(image, 0, MEM_RELEASE);
    puts("PASS: fixedtick read-only cadence, pause/loading, lifetime and fail-closed guards");
    puts("PASS: fixed60 dt branch patch, per-frame guards and restore on every disarm");
    return 0;
}
