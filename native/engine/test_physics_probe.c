/* Synthetic test of the physics/flight-job probe (physics_probe.h) inside fixedtick.c: verified
   vtable swap or nothing, counting/timing only on the owner thread, scene attribution, frame and
   window folding, the read-only sampler (JobThreadManager occupancy, pathing pass), restore. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include "fixedtick.c"

static owner fake_owner;
static void *fake_table[40];
static LARGE_INTEGER fake_now;
static BOOL WINAPI fake_qpc(LARGE_INTEGER *value) { *value = fake_now; return TRUE; }

/* Stand-ins for the engine functions behind the wrapped entries. */
static int calls[PP_ENTRIES];
static void *fake_create(void *self, void *ret, const char *name, uint32_t color, const void *fn, uint32_t deps) {
    (void)self; (void)name; (void)color; (void)fn; (void)deps; ++calls[PP_CREATE]; return ret;
}
static void fake_wait(void *self, void *barrier) { (void)self; (void)barrier; ++calls[PP_WAIT]; fake_now.QuadPart += 2500; /* 0.25 ms */ }
static void fake_added(void *s, const void *a, const void *b, const void *m, void *x) { (void)s; (void)a; (void)b; (void)m; (void)x; ++calls[PP_ADDED]; }
static void fake_persisted(void *s, const void *a, const void *b, const void *m, void *x) { (void)s; (void)a; (void)b; (void)m; (void)x; ++calls[PP_PERSISTED]; }
static void fake_removed(void *s, const void *p) { (void)s; (void)p; ++calls[PP_REMOVED]; }

static unsigned char fake_scene[2][0x40], fake_sys[2][0x700], fake_pm[0x300], fake_jtm[0x600];
static unsigned char fake_batches[0x20 * 7];

typedef struct { const char *name; double value; } captured;
static captured cap[64];
static int ncap;
static void capture(lua_State *L, const char *name, double value) {
    (void)L; assert(ncap < 64); cap[ncap].name = name; cap[ncap].value = value; ++ncap;
}
static double got(const char *name) {
    for (int i = 0; i < ncap; ++i) if (!strcmp(cap[i].name, name)) return cap[i].value;
    fprintf(stderr, "missing field %s\n", name); assert(0); return 0.0;
}

static void **entry_of(int k) { return (void **)(image + x4_physics_vtables[k].entry); }
static void *fn_of(int k) { return image + x4_physics_vtables[k].fn; }

static void setup(void) {
    memset(fake_table, 0, sizeof(fake_table));
    memset(&fake_owner, 0, sizeof(fake_owner));
    fake_table[3] = &fake_owner;
    *(void ***)(image + VBI_RVA) = fake_table;
    registered_table = fake_table;
    fake_owner.armed = 1;
    current = &fake_owner;
    owner_thread = GetCurrentThreadId();
    qpc_frequency = 10000000;
    qpc_read = fake_qpc;
    fake_now.QuadPart = 100000000;
    for (int k = 0; k < PP_ENTRIES; ++k) *entry_of(k) = fn_of(k);
    memset(calls, 0, sizeof(calls));
}

static void use_fakes(void) {
    pp_orig[PP_CREATE] = (void *)fake_create;
    pp_orig[PP_WAIT] = (void *)fake_wait;
    pp_orig[PP_ADDED] = (void *)fake_added;
    pp_orig[PP_PERSISTED] = (void *)fake_persisted;
    pp_orig[PP_REMOVED] = (void *)fake_removed;
}

static void restore_origs(void) {
    for (int k = 0; k < PP_ENTRIES; ++k) pp_orig[k] = fn_of(k);
}

/* One engine frame: both scenes step; scene 1 creates jobs and reports contacts. */
static void physics_frame(int jobs, int contacts) {
    void *ret = NULL;
    for (int s = 0; s < 2; ++s) {
        void *jobsys = fake_sys[s] + PP_SYS_JOBS;
        for (int j = 0; j < jobs; ++j) ((pp_create_fn)*entry_of(PP_CREATE))(jobsys, &ret, "job", 0, NULL, 0);
        ((pp_wait_fn)*entry_of(PP_WAIT))(jobsys, NULL);
    }
    static int bodies[8];
    for (int c = 0; c < contacts; ++c) {   /* pair (0,1) gets two manifolds, the others one */
        ((pp_contact_fn)*entry_of(PP_ADDED))(NULL, &bodies[0], &bodies[1], NULL, NULL);
        ((pp_contact_fn)*entry_of(PP_PERSISTED))(NULL, &bodies[2 + c], &bodies[3 + c], NULL, NULL);
    }
    ((pp_removed_fn)*entry_of(PP_REMOVED))(NULL, NULL);
    pp_frame_end();
}

static DWORD WINAPI foreign_caller(LPVOID unused) {
    (void)unused;
    void *ret = NULL;
    ((pp_create_fn)*entry_of(PP_CREATE))(NULL, &ret, "job", 0, NULL, 0);
    ((pp_wait_fn)*entry_of(PP_WAIT))(NULL, NULL);
    ((pp_contact_fn)*entry_of(PP_ADDED))(NULL, NULL, NULL, NULL, NULL);
    return 0;
}

int main(void) {
    image = VirtualAlloc(NULL, X4_IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image);
    assert(sizeof(x4_physics_vtables) / sizeof(x4_physics_vtables[0]) == PP_ENTRIES);

    /* A foreign pointer in any entry: nothing is installed or changed. */
    setup();
    *entry_of(PP_REMOVED) = image + 1;
    assert(!pp_install(0) && !pp_installed && pp_reason && !strcmp(pp_reason, "vtable entry not original"));
    for (int k = 0; k < PP_REMOVED; ++k) assert(*entry_of(k) == fn_of(k));
    assert(*entry_of(PP_REMOVED) == image + 1);

    /* Verified install: every entry points at its wrapper; the sampler runs. */
    setup();
    *(void **)(image + PP_SCENE0_RVA) = fake_scene[0];
    *(void **)(image + PP_SCENE1_RVA) = fake_scene[1];
    for (int s = 0; s < 2; ++s) {
        *(void **)(fake_scene[s] + PP_SCENE_SYS) = fake_sys[s];
        *(uint64_t *)(fake_sys[s] + PP_SYS_SYNC_COUNT) = s ? 40u : 3u;
    }
    *(void **)(image + PP_PATHING_RVA) = fake_pm;
    *(void **)(image + PP_JTM_RVA) = fake_jtm;
    *(uintptr_t *)(fake_pm + PP_PM_BATCH_A) = (uintptr_t)fake_batches;
    *(uintptr_t *)(fake_pm + PP_PM_BATCH_A + 8) = (uintptr_t)(fake_batches + 0x20 * 4);
    *(uintptr_t *)(fake_pm + PP_PM_BATCH_B) = (uintptr_t)(fake_batches + 0x20 * 4);
    *(uintptr_t *)(fake_pm + PP_PM_BATCH_B + 8) = (uintptr_t)(fake_batches + 0x20 * 7);
    *(float *)(fake_pm + PP_PM_DT) = 1.0f / 60.0f;
    *(double *)(fake_pm + PP_PM_BUDGET) = 0.004;
    *(uint32_t *)(fake_jtm + PP_JTM_COUNT) = 4;
    *(uint32_t *)(fake_jtm + PP_JTM_THREADS + 0 * 0x20 + 0x18) = 1;   /* worker 0: pathing/general */
    *(uint32_t *)(fake_jtm + PP_JTM_THREADS + 2 * 0x20 + 0x18) = 2;   /* worker 2: category 2 */
    *(uint64_t *)(fake_jtm + PP_JTM_QUEUE_SIZE) = 7;
    assert(pp_install(0) && pp_installed && !pp_reason);
    assert(*entry_of(PP_CREATE) == (void *)pp_create && *entry_of(PP_WAIT) == (void *)pp_wait);
    assert(*entry_of(PP_ADDED) == (void *)pp_added && *entry_of(PP_REMOVED) == (void *)pp_removed);
    assert(pp_thread);
    use_fakes();

    /* First frame publishes pointers and learns the scenes' job systems; then 10 measured frames. */
    pp_frame_end();
    memset(&pp_win, 0, sizeof(pp_win));
    for (int f = 0; f < 10; ++f) physics_frame(5, 3);
    assert(calls[PP_CREATE] == 100 && calls[PP_WAIT] == 20 && calls[PP_ADDED] == 30 && calls[PP_REMOVED] == 10);
    assert(pp_win.frames == 10 && pp_win.sum.updates == 20 && pp_win.sum.jobs == 100);
    assert(pp_win.sum.added == 30 && pp_win.sum.persisted == 30 && pp_win.sum.removed == 10);
    /* per frame: pair (0,1) three times -> one multi pair; pairs (2,3),(3,4),(4,5) once each */
    assert(pp_win.sum.pairs == 40 && pp_win.sum.multi_pairs == 10 && pp_win.sum.pair_overflow == 0);
    assert(fabs(pp_win.sum.jolt_ms[0] - 2.5) < 1e-9 && fabs(pp_win.sum.jolt_ms[1] - 2.5) < 1e-9);
    assert(fabs(pp_win.jolt_frame_max_ms - 0.5) < 1e-9);
    assert(pp_win.synced_bodies[0] == 30 && pp_win.synced_bodies[1] == 400 && pp_win.synced_max[1] == 40);

    /* Calls from another thread pass straight through and are not counted. */
    {
        HANDLE t = CreateThread(NULL, 0, foreign_caller, NULL, 0, NULL);
        assert(t && WaitForSingleObject(t, 5000) == WAIT_OBJECT_0);
        CloseHandle(t);
        assert(calls[PP_CREATE] == 101 && calls[PP_WAIT] == 21 && calls[PP_ADDED] == 31);
        assert(pp_frame.jobs == 0 && pp_frame.updates == 0 && pp_frame.added == 0);
    }

    /* Sampler: occupancy of the fake pool and one pathing pass (outstanding 3 -> 0). */
    {
        LARGE_INTEGER start;
        QueryPerformanceCounter(&start);
        *(unsigned char *)(fake_pm + PP_PM_JOINED) = 0;
        *(int32_t *)(fake_pm + PP_PM_OUTSTANDING) = 3;
        *(int64_t *)(fake_pm + PP_PM_PASS_QPC) = start.QuadPart;
        Sleep(30);
        *(int32_t *)(fake_pm + PP_PM_OUTSTANDING) = 0;   /* batch phase ends */
        Sleep(30);
        *(unsigned char *)(fake_pm + PP_PM_JOINED) = 1;  /* the MoveWorker joins ~30 ms later */
        Sleep(20);
        ncap = 0;
        pp_stats(NULL, capture);
        assert(got("phys_installed") == 1 && got("phys_frames") == 10);
        assert(fabs(got("phys_jolt_ms") - 0.5) < 1e-9 && fabs(got("phys_jolt1_ms") - 0.25) < 1e-9);
        assert(got("phys_jobs") == 10 && got("phys_added") == 3 && got("phys_bodies1") == 40);
        assert(got("path_batches_a") == 4 && got("path_batches_b") == 3);
        assert(fabs(got("path_budget_ms") - 4.0) < 1e-9 && fabs(got("path_dt") - 1.0 / 60.0) < 1e-6);
        assert(got("jtm_samples") > 20 && got("jtm_workers") == 4);
        assert(fabs(got("jtm_busy") - 2.0) < 1e-9 && fabs(got("jtm_cat1") - 1.0) < 1e-9 && fabs(got("jtm_cat2") - 1.0) < 1e-9);
        assert(got("jtm_queue") == 7 && got("jtm_queue_max") == 7);
        assert(got("path_inflight") > 0.1 && got("path_inflight") < 0.9);
        assert(got("path_passes") == 1 && got("path_pass_ms") >= 20.0 && got("path_pass_ms") < 500.0);
        assert(got("path_joins") == 1 && got("path_tight_joins") == 0 && got("path_slack_ms") >= 20.0);
        ncap = 0;
        pp_stats(NULL, capture);   /* the window was reset */
        assert(got("phys_frames") == 0 && got("path_passes") == 0);
    }

    /* Loading frames are skipped by the sampler. */
    *(int *)((unsigned char *)fake_table + 0x130) = 1;
    pp_frame_end();
    Sleep(5);
    ncap = 0;
    pp_stats(NULL, capture);   /* discard samples taken before the flag was published */
    Sleep(20);
    ncap = 0;
    pp_stats(NULL, capture);
    assert(got("jtm_samples") > 10 && got("jtm_busy") == 0.0 && got("jtm_workers") == 0);
    *(int *)((unsigned char *)fake_table + 0x130) = 0;

    /* Disarm restores the original entries and joins the sampler. */
    restore_origs();
    disarm_because("test");
    assert(!pp_installed && !pp_thread);
    for (int k = 0; k < PP_ENTRIES; ++k) assert(*entry_of(k) == fn_of(k));

    /* An entry changed by someone else after install is left alone on uninstall. */
    setup();
    assert(pp_install(0));
    *entry_of(PP_WAIT) = image + 5;
    pp_uninstall();
    assert(*entry_of(PP_WAIT) == image + 5 && *entry_of(PP_CREATE) == fn_of(PP_CREATE));

    VirtualFree(image, 0, MEM_RELEASE);
    puts("PASS: physics probe: verified vtable swap or nothing, owner-thread counting, scene attribution");
    puts("PASS: physics probe: frame/window folding, read-only sampler (pool occupancy, pathing pass), loading skip");
    puts("PASS: physics probe: restore on disarm, foreign entries left alone");
    return 0;
}
