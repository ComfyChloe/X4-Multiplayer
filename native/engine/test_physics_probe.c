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
static volatile LONG flight_seen, flight_bad_args;
static void fake_update(void *self, void *slot, float dt, double t, uint8_t cheap, uint8_t p) {
    if (self != (void *)0x1234 || slot != (void *)0x5678 || dt != 0.25f || t != 12.5 || cheap > 1 || p != 1)
        InterlockedIncrement(&flight_bad_args);
    InterlockedIncrement(&flight_seen);
}
static void fake_forced(void *self, void *slot, float dt, double t, uint8_t cheap, uint8_t p) {
    (void)self; (void)dt; (void)t; (void)cheap; (void)p;
    ((unsigned char *)slot)[0x40] = 0x99;   /* the original's write */
}
static DWORD WINAPI capture_worker(LPVOID arg) {
    int worker = (int)(intptr_t)arg;
    static unsigned char agents[2][0x400], models[2][0x60], slots[2][0x1a0];
    unsigned char *agent = agents[worker], *model = models[worker], *slot = slots[worker];
    agent[0x340] = (unsigned char)(0x40 + worker);
    *(float *)(agent + 0x364) = 321.0f + (float)worker;
    *(unsigned char **)(model + 0x10) = agent;
    model[0x20] = (unsigned char)(0x20 + worker);
    void **entry = (void **)(image + x4_physics_flight_vtables[0].entry);
    for (int i = 0; i < 40; ++i) {
        slot[0x40] = 0x11;
        ((pf_update_fn)*entry)(model, slot, 0.5f, (double)worker, 0, 1);
    }
    return 0;
}
static DWORD WINAPI flight_worker(LPVOID arg) {
    int model = (int)(intptr_t)arg;
    void **entry = (void **)(image + x4_physics_flight_vtables[model].entry);
    for (int i = 0; i < 100; ++i) ((pf_update_fn)*entry)((void *)0x1234, (void *)0x5678, 0.25f, 12.5, (uint8_t)(i < 10), 1);
    return 0;
}
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

typedef struct { char name[48]; double value; } captured;   /* names copied: some are built in a buffer */
static captured cap[160];
static int ncap;
static void capture(lua_State *L, const char *name, double value) {
    (void)L; assert(ncap < 160 && strlen(name) < sizeof(cap[0].name));
    strcpy(cap[ncap].name, name); cap[ncap].value = value; ++ncap;
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

static DWORD WINAPI foreign_uninstall(LPVOID unused) {
    (void)unused;
    pp_uninstall();
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
    for (int b = 0; b < 7; ++b) *(uint64_t *)(fake_batches + b * 0x20 + 8) = (uint64_t)(b + 1);   /* agents */
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

    /* Sampler thread: it runs and reads the fake pool. */
    Sleep(40);
    {
        ncap = 0;
        pp_stats(NULL, capture);
        assert(got("phys_installed") == 1 && got("phys_frames") == 10);
        assert(fabs(got("phys_jolt_ms") - 0.5) < 1e-9 && fabs(got("phys_jolt1_ms") - 0.25) < 1e-9);
        assert(got("phys_jobs") == 10 && got("phys_added") == 3 && got("phys_bodies1") == 40);
        assert(got("path_batches_a") == 4 && got("path_batches_b") == 3 && got("path_agents") == 28);
        assert(fabs(got("path_budget_ms") - 4.0) < 1e-9 && fabs(got("path_dt") - 1.0 / 60.0) < 1e-6);
        assert(got("jtm_samples") >= 1 && got("jtm_workers") == 4);   /* timing depends on PC load */
    }
    /* Deterministic samples (thread stopped): occupancy, one pass (outstanding 3 -> 0), the join 20 ms later. */
    {
        pp_sampler_stop();
        ncap = 0;
        pp_stats(NULL, capture);   /* discard the thread's samples */
        pp_pass_track track;
        memset(&track, 0, sizeof(track));
        LARGE_INTEGER start;
        QueryPerformanceCounter(&start);
        *(unsigned char *)(fake_pm + PP_PM_JOINED) = 0;
        *(int32_t *)(fake_pm + PP_PM_OUTSTANDING) = 3;
        *(int64_t *)(fake_pm + PP_PM_PASS_QPC) = start.QuadPart;
        pp_sample_once(&track);
        Sleep(30);
        pp_sample_once(&track);
        *(int32_t *)(fake_pm + PP_PM_OUTSTANDING) = 0;   /* batch phase ends */
        pp_sample_once(&track);
        Sleep(20);
        pp_sample_once(&track);
        *(unsigned char *)(fake_pm + PP_PM_JOINED) = 1;  /* the MoveWorker joins */
        pp_sample_once(&track);
        ncap = 0;
        pp_stats(NULL, capture);
        assert(got("jtm_samples") == 5 && got("jtm_workers") == 4);
        assert(fabs(got("jtm_busy") - 2.0) < 1e-9 && fabs(got("jtm_cat1") - 1.0) < 1e-9 && fabs(got("jtm_cat2") - 1.0) < 1e-9);
        assert(got("jtm_queue") == 7 && got("jtm_queue_max") == 7);
        assert(fabs(got("path_inflight") - 0.4) < 1e-9);
        assert(got("path_passes") == 1 && got("path_pass_ms") >= 25.0 && got("path_pass_ms") < 5000.0);
        assert(got("path_joins") == 1 && got("path_tight_joins") == 0 && got("path_slack_ms") >= 15.0);
        ncap = 0;
        pp_stats(NULL, capture);   /* the window was reset */
        assert(got("phys_frames") == 0 && got("path_passes") == 0);
    }

    /* Loading frames are skipped by the sampler. */
    *(int *)((unsigned char *)fake_table + 0x130) = 1;
    pp_frame_end();
    {
        pp_pass_track track;
        memset(&track, 0, sizeof(track));
        pp_sample_once(&track);
        ncap = 0;
        pp_stats(NULL, capture);
        assert(got("jtm_samples") == 1 && got("jtm_busy") == 0.0 && got("jtm_workers") == 0);
    }
    *(int *)((unsigned char *)fake_table + 0x130) = 0;

    /* Disarm restores the original entries and joins the sampler. */
    restore_origs();
    disarm_because("test");
    assert(!pp_installed && !pp_thread);   /* the sampler was stopped above; disarm is idempotent */
    for (int k = 0; k < PP_ENTRIES; ++k) assert(*entry_of(k) == fn_of(k));

    /* joltwait: both barrier entries swapped or neither; waits route by barrier kind; anomalies switch back. */
    {
        setup();
        for (int k = 0; k < PP_MODE_ENTRIES; ++k)
            *(void **)(image + x4_physics_mode_vtables[k].entry) = image + x4_physics_mode_vtables[k].fn;
        assert(pp_install(0));
        use_fakes();
        void **cb = (void **)(image + x4_physics_mode_vtables[PP_MODE_CREATE_BARRIER].entry);
        void **db = (void **)(image + x4_physics_mode_vtables[PP_MODE_DESTROY_BARRIER].entry);
        *db = image + 3;   /* foreign destroy entry: refused, create entry untouched */
        assert(pp_joltwait_enable() && !pp_joltwait && *cb == image + x4_physics_mode_vtables[0].fn);
        *db = image + x4_physics_mode_vtables[PP_MODE_DESTROY_BARRIER].fn;
        assert(!pp_joltwait_enable() && pp_joltwait);
        assert(*cb == (void *)pj_sb_create && *db == (void *)pj_sb_destroy);
        /* A serial barrier is waited by the serial wait, anything else by the original. */
        void *sb = pj_sb_create(NULL);
        assert(sb && pp_wait_target(sb) == (pp_wait_fn)pj_sb_wait);
        assert(pp_wait_target(fake_scene) == (pp_wait_fn)fake_wait);
        int before = calls[PP_WAIT];
        ((pp_wait_fn)*entry_of(PP_WAIT))(fake_sys[0] + PP_SYS_JOBS, sb);   /* empty barrier: returns at once */
        assert(calls[PP_WAIT] == before);
        pj_sb_destroy(NULL, sb);
        pp_frame_end();
        assert(pp_joltwait);
        ++pj_sb.stalls;    /* an anomaly switches back at the next frame end */
        pp_frame_end();
        assert(!pp_joltwait && pp_joltwait_reason && !strcmp(pp_joltwait_reason, "no runnable job"));
        assert(*cb == image + x4_physics_mode_vtables[0].fn && *db == image + x4_physics_mode_vtables[1].fn);
        pj_sb.stalls = 0;
        /* The reason is latched: no re-enable in this session, neither directly nor by request. */
        assert(pp_joltwait_enable() == pp_joltwait_reason && !pp_joltwait);
        assert(pp_request_mode("joltwait") == pp_joltwait_reason && pp_mode_request == PP_REQ_NONE);
        /* Requests (any thread) are applied at the owner thread's next frame end. */
        pp_joltwait_reason = NULL;
        assert(pp_request_mode("fast") && pp_request_mode(NULL) && pp_mode_request == PP_REQ_NONE);
        assert(!pp_request_mode("joltwait") && !pp_joltwait);
        pp_frame_end();
        assert(pp_joltwait && *cb == (void *)pj_sb_create && *db == (void *)pj_sb_destroy);
        assert(!pp_request_mode("original") && pp_joltwait);
        pp_frame_end();
        assert(!pp_joltwait && *cb == image + x4_physics_mode_vtables[0].fn);
        /* A refusal at the frame end is latched and reported. */
        *db = image + 3;
        assert(!pp_request_mode("joltwait"));
        pp_frame_end();
        assert(!pp_joltwait && pp_joltwait_reason && !strcmp(pp_joltwait_reason, "barrier entry not original"));
        assert(pp_request_mode("joltwait") == pp_joltwait_reason);
        *db = image + x4_physics_mode_vtables[PP_MODE_DESTROY_BARRIER].fn;
        pp_joltwait_reason = NULL;
        assert(!pp_request_mode("joltwait"));
        pp_frame_end();
        assert(pp_joltwait);
        /* A disarm on another thread leaves joltwait installed (a barrier may be in flight on the
           MoveWorker): entries 3/4 and the WaitForJobs router stay ours, counting wrappers go back. */
        restore_origs();   /* uninstall writes pp_orig back */
        {
            HANDLE t = CreateThread(NULL, 0, foreign_uninstall, NULL, 0, NULL);
            assert(t && WaitForSingleObject(t, 5000) == WAIT_OBJECT_0);
            CloseHandle(t);
        }
        assert(!pp_installed && pp_joltwait && pp_orphaned == 1);
        assert(*cb == (void *)pj_sb_create && *db == (void *)pj_sb_destroy && *entry_of(PP_WAIT) == (void *)pp_wait);
        assert(*entry_of(PP_CREATE) == fn_of(PP_CREATE) && *entry_of(PP_ADDED) == fn_of(PP_ADDED));
        {   /* the router still routes: a serial barrier to the serial wait, X4's to the original */
            void *sb = pj_sb_create(NULL);
            assert(pp_wait_target(sb) == (pp_wait_fn)pj_sb_wait && pp_wait_target(fake_scene) == (pp_wait_fn)fn_of(PP_WAIT));
            pj_sb_destroy(NULL, sb);
        }
        /* The next install on the owner thread adopts the router; the owner-thread uninstall restores all. */
        assert(pp_install(0) && pp_installed && pp_joltwait && *entry_of(PP_WAIT) == (void *)pp_wait);
        assert(*entry_of(PP_CREATE) == (void *)pp_create);
        pp_uninstall();
        assert(!pp_joltwait && *cb == image + x4_physics_mode_vtables[0].fn && *entry_of(PP_WAIT) == fn_of(PP_WAIT));
        assert(pp_install(0));
        pp_joltwait_reason = NULL;
        assert(!pp_request_mode("joltwait"));
        pp_frame_end();
        assert(pp_joltwait);
        restore_origs();
        disarm_because("test");   /* uninstall switches back first */
        assert(!pp_joltwait && *cb == image + x4_physics_mode_vtables[0].fn && *db == image + x4_physics_mode_vtables[1].fn);
    }

    /* Flight-model census: queued, verified, all six slots or none, counts on any thread (cheap-mode calls
       too), arguments passed through unchanged, folded at frame end, latched refusal, restored on disarm. */
    {
        setup();
        assert(pp_install(0));
        use_fakes();
        void **fe[PF_MODELS];
        for (int i = 0; i < PF_MODELS; ++i) {
            fe[i] = (void **)(image + x4_physics_flight_vtables[i].entry);
            *fe[i] = image + x4_physics_flight_vtables[i].fn;
        }
        *fe[2] = image + 7;   /* foreign entry: refused, nothing swapped */
        assert(!pf_request_census(1));
        pp_frame_end();
        assert(!pf_installed && pf_reason && !strcmp(pf_reason, "flight vtable entry not original"));
        assert(*fe[0] == image + x4_physics_flight_vtables[0].fn);
        assert(pf_request_census(1) == pf_reason);   /* latched */
        pf_reason = NULL;
        *fe[2] = image + x4_physics_flight_vtables[2].fn;
        assert(!pf_request_census(1));
        pp_frame_end();
        assert(pf_installed);
        for (int i = 0; i < PF_MODELS; ++i) { assert(*fe[i] == pf_wrappers[i]); pf_orig[i] = (void *)fake_update; }
        memset(&pf_win, 0, sizeof(pf_win));
        HANDLE w[2] = {CreateThread(NULL, 0, flight_worker, (LPVOID)(intptr_t)5, 0, NULL),
                       CreateThread(NULL, 0, flight_worker, (LPVOID)(intptr_t)2, 0, NULL)};
        assert(w[0] && w[1] && WaitForMultipleObjects(2, w, TRUE, 5000) == WAIT_OBJECT_0);
        CloseHandle(w[0]); CloseHandle(w[1]);
        ((pf_update_fn)*fe[0])((void *)0x1234, (void *)0x5678, 0.25f, 12.5, 0, 1);   /* owner thread */
        assert(flight_seen == 201 && flight_bad_args == 0);
        pp_frame_end();
        assert(pf_win.frames == 1 && pf_win.calls[5] == 100 && pf_win.calls[2] == 100 && pf_win.calls[0] == 1);
        assert(pf_win.cheap[5] == 10 && pf_win.cheap[2] == 10 && pf_win.owner_calls[0] == 1 && pf_win.owner_calls[5] == 0);
        assert(pf_win.frame_calls_max == 201);
        ncap = 0;
        pp_stats(NULL, capture);
        assert(got("flight_installed") == 1 && got("flight_frames") == 1 && got("flight_calls") == 201);
        assert(got("flight_steering_calls") == 100 && got("flight_linear_calls") == 100 && got("flight_cheap") == 20);
        assert(got("flight_owner_calls") == 1 && got("flight_steering_us") >= 0.0 && got("flight_ms") >= 0.0);
        assert(pf_win.frames == 0);   /* window reset */
        /* Removal by request (the uninstall writes pf_orig back), then a re-install and a disarm. */
        for (int i = 0; i < PF_MODELS; ++i) pf_orig[i] = image + x4_physics_flight_vtables[i].fn;
        assert(!pf_request_census(0));
        pp_frame_end();
        assert(!pf_installed && *fe[5] == image + x4_physics_flight_vtables[5].fn);
        assert(!pf_request_census(1));
        pp_frame_end();
        assert(pf_installed);
        restore_origs();
        disarm_because("test");   /* pp_uninstall removes the census (any thread) */
        for (int i = 0; i < PF_MODELS; ++i) assert(*fe[i] == image + x4_physics_flight_vtables[i].fn);
        assert(!pf_installed);
    }
    puts("PASS: flight census: verified swap of six model slots or none, counts on any thread, arguments unchanged, fold, latched refusal, restore");

    /* Flight capture: staged, armed at frame end, records from worker threads (model bytes, agent block,
       slot before/after the original), written when full; refusals for unknown/uncapturable classes. */
    {
        setup();
        assert(pp_install(0));
        use_fakes();
        for (int i = 0; i < PF_MODELS; ++i)
            *(void **)(image + x4_physics_flight_vtables[i].entry) = image + x4_physics_flight_vtables[i].fn;
        pf_reason = NULL;
        wchar_t wpath[MAX_PATH];
        char path[MAX_PATH];
        GetTempPathW(MAX_PATH, wpath);
        wcscat(wpath, L"x4pf-test-capture.log");
        WideCharToMultiByte(CP_UTF8, 0, wpath, -1, path, MAX_PATH, NULL, NULL);
        DeleteFileW(wpath);
        assert(pf_request_capture("warp", 10, path, 0) && pf_request_capture("steering", 10, path, 0));
        assert(pf_request_capture("forced", 0, path, 0) && pf_request_capture("forced", 10, NULL, 0));
        assert(!pf_request_capture("forced", 64, path, 1000));
        assert(pf_request_capture("forced", 64, path, 1000));   /* one capture per session */
        pp_frame_end();
        assert(pf_installed && pf_cap.armed && pf_cap.capacity == 64 && pf_cap.model == 0);
        for (int i = 0; i < PF_MODELS; ++i) pf_orig[i] = (void *)fake_forced;
        HANDLE w[2] = {CreateThread(NULL, 0, capture_worker, (LPVOID)(intptr_t)0, 0, NULL),
                       CreateThread(NULL, 0, capture_worker, (LPVOID)(intptr_t)1, 0, NULL)};
        assert(w[0] && w[1] && WaitForMultipleObjects(2, w, TRUE, 5000) == WAIT_OBJECT_0);
        CloseHandle(w[0]); CloseHandle(w[1]);
        assert(pf_cap.next == 80 && pf_cap.dropped == 16);
        pp_frame_end();   /* full: disarm and write */
        assert(!pf_cap.armed && pf_cap.written == 1);
        HANDLE f = CreateFileW(wpath, GENERIC_READ, 0, NULL, OPEN_EXISTING, 0, NULL);
        assert(f != INVALID_HANDLE_VALUE);
        static unsigned char file[1 << 20];
        DWORD got_bytes = 0;
        assert(ReadFile(f, file, sizeof(file), &got_bytes, NULL));
        CloseHandle(f);
        DeleteFileW(wpath);
        const uint32_t *hdr = (const uint32_t *)(file + 8);
        assert(!memcmp(file, PF_CAP_MAGIC, 8) && hdr[0] == 1 && hdr[1] == 0 && hdr[3] == 0x60 && hdr[7] == 64);
        assert(hdr[8] == 16 && hdr[9] == X4_PE_TIMESTAMP && hdr[10] == X4_IMAGE_SIZE && hdr[11] == sizeof(pf_cap_head));
        size_t rs = hdr[2];
        assert(rs == pf_cap_record_size(0x60) && got_bytes == 8 + 48 + 64 * rs);
        for (int r = 0; r < 64; ++r) {
            const unsigned char *rec = file + 56 + r * rs;
            const pf_cap_head *h = (const pf_cap_head *)rec;
            int worker = (int)h->t;          /* the worker id is passed as t */
            assert(h->model == 0 && h->dt == 0.5f && h->max_forward == 321.0f + (float)worker && (h->flags & 3) == 2);
            const unsigned char *model = rec + sizeof(pf_cap_head);
            assert(model[0x20] == 0x20 + worker);                       /* model bytes */
            const unsigned char *agent = model + 0x60;
            assert(agent[0] == 0x40 + worker);                          /* agent +0x340 */
            const unsigned char *before = agent + PF_CAP_AGENT_BYTES, *after = before + PF_CAP_SLOT;
            assert(before[0x40] == 0x11 && after[0x40] == 0x99 && after[0x41] == before[0x41]);
        }
        restore_origs();
        disarm_because("test");
        assert(!pf_installed);
    }
    puts("PASS: flight capture: staged, armed at frame end, records from workers, dropped beyond capacity, file written");

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
    puts("PASS: physics probe joltwait: verified swap of both barrier entries, routing by barrier kind, fail-back on anomaly, queued requests, latched refusals, a disarm on another thread keeps joltwait installed and the next install adopts it");
    return 0;
}
