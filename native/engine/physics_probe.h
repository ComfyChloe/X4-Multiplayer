/* Physics and flight-job probe for the engine rewrite (included by fixedtick.c after walkprof.h).
 *
 * Measures, without changing behaviour:
 *  - Jolt: time the MoveWorker spends in XPhys::XJobSystem::WaitForJobs (X4's job system never hands
 *    a ready job to a worker, so this is where every Jolt job of PhysicsSystem::Update runs), the
 *    number of Jolt jobs created and the contact callbacks per frame, and the bodies X4 synchronises
 *    per scene (velocity copy before, pose write-back after the step).
 *  - Pathing (flight models run in its batch jobs): agents, batch counts, the wall-clock budget, and
 *    from a sampler thread the duration of each pass's batch phase.
 *  - JobThreadManager: workers busy per sample (per job category) and queue depth, from a sampler
 *    thread that only reads memory (ReadProcessMemory, so a stale pointer fails instead of faulting).
 *
 * Hooks are data-only: five .rdata vtable entries (XJobSystem slots 2 CreateJob and 5 WaitForJobs,
 * XContactListener slots 2-4) are compare-exchanged from the verified original function to a counting
 * wrapper and restored on disarm. The wrappers call the original first-class and count only on the
 * probe's MoveWorker thread. Every code site whose bytes establish an offset read here is hash-pinned
 * (signatures.py PHYSICS_SITES); any mismatch refuses the probe (the rest of fixedtick keeps running).
 * Engine RVAs (build 23660954) are documented in reports/REWRITE-PHYSICS.md.
 *
 * Optional mode "joltwait" (Lua owner:physics_mode('joltwait')): XJobSystem slots 3/4 (Create/
 * DestroyBarrier) are swapped for physics_jobs.h's serial barrier and the slot-5 wrapper waits with it:
 * X4's job schedule exactly (same jobs, same order, same thread, X4's own Job::Execute 0xb4100) minus the
 * critical section and the Sleep(0) X4 issues after every job. Switched only between frames on the
 * MoveWorker; any anomaly (foreign thread, stall, allocation failure) switches back and is reported. */
#include "physics_jobs.h"

#define PP_SCENE0_RVA 0x38f0748u      /* XPhys::Scene* stepped first by UniverseFrameUpdate */
#define PP_SCENE1_RVA 0x38f0740u      /* ... and second */
#define PP_PATHING_RVA 0x6cf21c8u     /* U::Path::PathingManager* */
#define PP_JTM_RVA 0x6cf1540u         /* XLib::JobThreadManager* */
#define PP_SCENE_SYS 0x20u            /* Scene -> X4 physics system (JPH::PhysicsSystem at offset 0) */
#define PP_SYS_JOBS 0x648u            /* embedded XPhys::XJobSystem passed to PhysicsSystem::Update */
#define PP_SYS_SYNC_COUNT 0x680u      /* count of body ids synchronised each step */
#define PP_PM_BATCH_A 0x180u          /* std::vector of 0x20-byte batches (begin, end) */
#define PP_PM_BATCH_B 0x198u
#define PP_PM_JOINED 0x88u            /* byte: 1 once WaitAndSwapBuffers joined this frame's pass */
#define PP_PM_OUTSTANDING 0x1b0u      /* batch jobs of the running pass not yet finished */
#define PP_PM_DT 0x1f8u               /* float pass dt */
#define PP_PM_PASS_QPC 0x210u         /* int64 QPC at pass start (budget reference) */
#define PP_PM_BUDGET 0x218u           /* double budget in seconds; agents beyond get the cheap update */
#define PP_JTM_THREADS 0x28u          /* 32 x 0x20-byte JobThread runnables, +0x18 = running category */
#define PP_JTM_COUNT 0x528u
#define PP_JTM_QUEUE_SIZE 0x588u      /* std::deque size of queued job slots */
#define PP_MAX_WORKERS 32
#define PP_SAMPLE_US 500
#define PP_SCENES 2

enum { PP_CREATE, PP_WAIT, PP_ADDED, PP_PERSISTED, PP_REMOVED, PP_ENTRIES };
enum { PP_MODE_CREATE_BARRIER, PP_MODE_DESTROY_BARRIER, PP_MODE_ENTRIES };
#define PP_EXECUTE_RVA 0xb4100u   /* JPH::JobSystem::Job::Execute (out-of-line copy) */

typedef void *(*pp_create_fn)(void *self, void *ret, const char *name, uint32_t color, const void *fn, uint32_t deps);
typedef void (*pp_wait_fn)(void *self, void *barrier);
typedef void (*pp_contact_fn)(void *self, const void *a, const void *b, const void *manifold, void *settings);
typedef void (*pp_removed_fn)(void *self, const void *pair);

typedef struct {
    uint64_t updates, jobs, added, persisted, removed;
    uint64_t pairs, multi_pairs, pair_overflow;  /* distinct body pairs; pairs with >= 2 manifolds */
    double jolt_ms[PP_SCENES];
} pp_counts;

/* Body pairs reported more than once in one step are the only place where the order of contact
   callbacks reaches X4 state (its contact map sums manifold points per key in callback order). */
#define PP_PAIR_SLOTS 4096
typedef struct { uintptr_t a, b; uint32_t gen, count; } pp_pair;
static pp_pair pp_pairs[PP_PAIR_SLOTS];
static uint32_t pp_pair_gen = 1;

typedef struct {
    uint64_t frames;
    pp_counts sum;
    double jolt_frame_max_ms;
    uint64_t frames_with_jolt;
    uint64_t synced_bodies[PP_SCENES];   /* sum over frames */
    uint64_t synced_max[PP_SCENES];
} pp_window_t;

typedef struct {
    uint64_t samples, loading;
    uint64_t busy_sum, busy_max, cat1_sum, cat2_sum, other_sum;
    uint64_t queue_sum, queue_max;
    uint64_t inflight;                   /* samples with pathing batch jobs outstanding */
    uint64_t jolt_samples, jolt_busy;    /* samples taken while the MoveWorker runs a Jolt step */
    uint64_t passes;
    double pass_ms_sum, pass_ms_max;
    uint64_t joins, tight_joins;         /* joins seen; joins within 1 ms of the batch phase end */
    double slack_ms_sum;                 /* join time - batch phase end (MoveWorker arrived later) */
    uint32_t workers;
} pp_sampled;

static void **pp_entry[PP_ENTRIES];
static void *pp_orig[PP_ENTRIES];
static void **pp_mode_entry[PP_MODE_ENTRIES];
static void *pp_mode_orig[PP_MODE_ENTRIES];
static int pp_joltwait;                  /* 1 while slots 3/4 point at the serial barrier */
static const char *pp_joltwait_reason;   /* first refusal or anomaly; latched: no re-enable this session */
/* Mode requests (Lua, any thread) are applied by the owner thread at the next frame end. */
#define PP_REQ_NONE 0
#define PP_REQ_ORIGINAL 1
#define PP_REQ_JOLTWAIT 2
static volatile LONG pp_mode_request;
static uint64_t pp_lua_foreign;          /* requests made from a thread other than the owner thread */
static uint64_t pp_win_joltwait_frames;  /* frames of the current stats window run in joltwait mode */
static uint64_t pp_joltwait_seen;        /* anomaly count already accounted */
static uint64_t pp_joltwait_frames, pp_joltwait_jobs_mark;
static int pp_installed;
static const char *pp_reason;
static pp_counts pp_frame;
static pp_window_t pp_win;
static void *pp_jobsys[PP_SCENES];
/* Published by the owner thread for the sampler (plain pointers; the sampler reads through them only
   with ReadProcessMemory). */
static void *volatile pp_pub_jtm, *volatile pp_pub_pm;
static volatile LONG pp_pub_loading = 1;
static volatile LONG pp_in_jolt;         /* set by the WaitForJobs wrapper around the original */
static int pp_last_batches[2];
static double pp_last_budget_ms, pp_last_dt;
static uint64_t pp_last_agents_hint;

static SRWLOCK pp_lock = SRWLOCK_INIT;
static pp_sampled pp_samp;
static HANDLE pp_thread, pp_timer;
static volatile LONG pp_stop;
static unsigned pp_interval_us = PP_SAMPLE_US;

static int pp_on_owner(void) {
    owner *o = current;
    return pp_installed && o && o->armed && GetCurrentThreadId() == owner_thread;
}

static double pp_elapsed_ms(int64_t a, int64_t b) {
    return qpc_frequency > 0 ? (double)(b - a) * 1000.0 / (double)qpc_frequency : 0.0;
}

static void *pp_create(void *self, void *ret, const char *name, uint32_t color, const void *fn, uint32_t deps) {
    if (pp_on_owner()) ++pp_frame.jobs;
    return ((pp_create_fn)pp_orig[PP_CREATE])(self, ret, name, color, fn, deps);
}

/* A barrier made by the serial barrier is always waited by it (routing by object, not by mode, so a
   switch between frames can never hand one kind of barrier to the other kind of wait). */
static pp_wait_fn pp_wait_target(void *barrier) {
    return (barrier && *(void ***)barrier == pj_sb_vtable) ? (pp_wait_fn)pj_sb_wait : (pp_wait_fn)pp_orig[PP_WAIT];
}

static void pp_wait(void *self, void *barrier) {
    pp_wait_fn target = pp_wait_target(barrier);
    if (!pp_on_owner()) { target(self, barrier); return; }
    LARGE_INTEGER a, b;
    int timed = qpc_read(&a);
    pp_in_jolt = 1;
    target(self, barrier);
    pp_in_jolt = 0;
    if (!timed || !qpc_read(&b)) return;
    ++pp_frame.updates;
    pp_frame.jolt_ms[self == pp_jobsys[1] ? 1 : 0] += pp_elapsed_ms(a.QuadPart, b.QuadPart);
}

static void pp_note_pair(const void *a, const void *b) {
    uintptr_t x = (uintptr_t)a, y = (uintptr_t)b;
    uint64_t h = (uint64_t)x * 0x9e3779b97f4a7c15ull ^ (uint64_t)y * 0xc2b2ae3d27d4eb4full;
    for (unsigned probe = 0; probe < 16; ++probe) {
        pp_pair *p = &pp_pairs[(h + probe) & (PP_PAIR_SLOTS - 1)];
        if (p->gen != pp_pair_gen) { p->gen = pp_pair_gen; p->a = x; p->b = y; p->count = 1; ++pp_frame.pairs; return; }
        if (p->a == x && p->b == y) { if (++p->count == 2) ++pp_frame.multi_pairs; return; }
    }
    ++pp_frame.pair_overflow;
}

static void pp_added(void *self, const void *a, const void *b, const void *m, void *s) {
    if (pp_on_owner()) { ++pp_frame.added; pp_note_pair(a, b); }
    ((pp_contact_fn)pp_orig[PP_ADDED])(self, a, b, m, s);
}
static void pp_persisted(void *self, const void *a, const void *b, const void *m, void *s) {
    if (pp_on_owner()) { ++pp_frame.persisted; pp_note_pair(a, b); }
    ((pp_contact_fn)pp_orig[PP_PERSISTED])(self, a, b, m, s);
}
static void pp_removed(void *self, const void *pair) {
    if (pp_on_owner()) ++pp_frame.removed;
    ((pp_removed_fn)pp_orig[PP_REMOVED])(self, pair);
}

static int pp_write_entry(void **entry, void *expected, void *value) {
    DWORD protection, ignored;
    if (!VirtualProtect(entry, sizeof(void *), PAGE_READWRITE, &protection)) return 0;
    int ok = InterlockedCompareExchangePointer((void *volatile *)entry, value, expected) == expected;
    VirtualProtect(entry, sizeof(void *), protection, &ignored);
    return ok;
}

/* Reads a value through a pointer that may be stale: never faults. */
static int pp_peek(const void *address, void *out, size_t size) {
    SIZE_T got = 0;
    return address && ReadProcessMemory(GetCurrentProcess(), address, out, size, &got) && got == size;
}

static void *pp_ptr_at(uintptr_t rva) {
    void *value = NULL;
    return pp_peek(image + rva, &value, sizeof(value)) ? value : NULL;
}

typedef struct { int64_t pass_start, batch_end; int pass_open; unsigned char joined; } pp_pass_track;

/* One sample of the JobThreadManager and the pathing pass (sampler thread, read-only). */
static void pp_take_sample(pp_sampled *s, pp_pass_track *t) {
    unsigned char *jtm = (unsigned char *)pp_pub_jtm, *pm = (unsigned char *)pp_pub_pm;
    uint32_t workers = 0;
    if (jtm && pp_peek(jtm + PP_JTM_COUNT, &workers, sizeof(workers)) && workers && workers <= PP_MAX_WORKERS) {
        unsigned char threads[PP_MAX_WORKERS * 0x20];
        uint64_t queue = 0;
        if (pp_peek(jtm + PP_JTM_THREADS, threads, workers * 0x20u)) {
            s->workers = workers;
            for (uint32_t i = 0; i < workers; ++i) {
                uint32_t category;
                memcpy(&category, threads + i * 0x20u + 0x18, sizeof(category));
                if (!category) continue;
                ++s->busy_sum;
                if (category == 1) ++s->cat1_sum; else if (category == 2) ++s->cat2_sum; else ++s->other_sum;
            }
            s->busy_max = s->busy_sum;
            if (pp_in_jolt) { s->jolt_samples = 1; s->jolt_busy = s->busy_sum; }
        }
        if (pp_peek(jtm + PP_JTM_QUEUE_SIZE, &queue, sizeof(queue)) && queue < 0x100000) {
            s->queue_sum = queue;
            s->queue_max = queue;
        }
    }
    int32_t outstanding = 0;
    int64_t start = 0;
    unsigned char joined = 0;
    LARGE_INTEGER now;
    if (pm && pp_peek(pm + PP_PM_OUTSTANDING, &outstanding, sizeof(outstanding)) &&
        pp_peek(pm + PP_PM_PASS_QPC, &start, sizeof(start)) && pp_peek(pm + PP_PM_JOINED, &joined, 1) &&
        QueryPerformanceCounter(&now)) {
        if (outstanding > 0) s->inflight = 1;
        if (start && start != t->pass_start) { t->pass_start = start; t->pass_open = 1; t->batch_end = 0; }
        if (t->pass_open && outstanding == 0 && now.QuadPart > t->pass_start) {
            double ms = pp_elapsed_ms(t->pass_start, now.QuadPart);
            if (ms < 1000.0) { s->passes = 1; s->pass_ms_sum = ms; s->pass_ms_max = ms; t->batch_end = now.QuadPart; }
            t->pass_open = 0;
        }
        if (joined && !t->joined && t->batch_end) {
            double slack = pp_elapsed_ms(t->batch_end, now.QuadPart);
            if (slack >= 0.0 && slack < 1000.0) {
                s->joins = 1;
                s->slack_ms_sum = slack;
                if (slack < 1.0) s->tight_joins = 1;
            }
            t->batch_end = 0;
        }
        t->joined = joined;
    }
}

/* Fold one sample into the shared window (sampler thread; tests call it directly). */
static void pp_fold_sample(const pp_sampled *s) {
    AcquireSRWLockExclusive(&pp_lock);
    pp_samp.samples += s->samples;
    pp_samp.loading += s->loading;
    pp_samp.busy_sum += s->busy_sum;
    if (s->busy_max > pp_samp.busy_max) pp_samp.busy_max = s->busy_max;
    pp_samp.cat1_sum += s->cat1_sum;
    pp_samp.cat2_sum += s->cat2_sum;
    pp_samp.other_sum += s->other_sum;
    pp_samp.queue_sum += s->queue_sum;
    if (s->queue_max > pp_samp.queue_max) pp_samp.queue_max = s->queue_max;
    pp_samp.inflight += s->inflight;
    pp_samp.jolt_samples += s->jolt_samples;
    pp_samp.jolt_busy += s->jolt_busy;
    pp_samp.passes += s->passes;
    pp_samp.pass_ms_sum += s->pass_ms_sum;
    if (s->pass_ms_max > pp_samp.pass_ms_max) pp_samp.pass_ms_max = s->pass_ms_max;
    pp_samp.joins += s->joins;
    pp_samp.tight_joins += s->tight_joins;
    pp_samp.slack_ms_sum += s->slack_ms_sum;
    if (s->workers) pp_samp.workers = s->workers;
    ReleaseSRWLockExclusive(&pp_lock);
}

static void pp_sample_once(pp_pass_track *track) {
    pp_sampled s;
    memset(&s, 0, sizeof(s));
    s.samples = 1;
    if (pp_pub_loading) s.loading = 1;
    else pp_take_sample(&s, track);
    pp_fold_sample(&s);
}

/* Sampler thread: JobThreadManager occupancy and the pathing pass, read-only. */
static DWORD WINAPI pp_sampler(LPVOID unused) {
    (void)unused;
    pp_pass_track track;
    memset(&track, 0, sizeof(track));
    while (!pp_stop) {
        if (pp_timer) {
            LARGE_INTEGER due;
            due.QuadPart = -(LONGLONG)pp_interval_us * 10;
            if (SetWaitableTimer(pp_timer, &due, 0, NULL, NULL, FALSE)) WaitForSingleObject(pp_timer, 50);
            else Sleep(1);
        } else Sleep(1);
        if (pp_stop) break;
        pp_sample_once(&track);
    }
    return 0;
}

static void pp_sampler_stop(void) {
    if (!pp_thread) return;
    pp_stop = 1;
    if (WaitForSingleObject(pp_thread, 2000) == WAIT_OBJECT_0) CloseHandle(pp_thread);
    pp_thread = NULL;  /* a thread that did not exit keeps its handle; it only reads memory */
    if (pp_timer) { CloseHandle(pp_timer); pp_timer = NULL; }
}

static uint64_t pp_joltwait_anomalies(void) { return pj_sb.stalls + pj_sb.foreign_calls + pj_sb.alloc_failures; }

/* Owner thread, between frames. Restores the original barrier entries (only if they are still ours). */
static void pp_joltwait_disable(const char *reason) {
    if (!pp_joltwait) return;
    void *ours[PP_MODE_ENTRIES] = {(void *)pj_sb_create, (void *)pj_sb_destroy};
    for (int k = 0; k < PP_MODE_ENTRIES; ++k)
        if (pp_mode_entry[k] && pp_mode_orig[k]) pp_write_entry(pp_mode_entry[k], ours[k], pp_mode_orig[k]);
    pp_joltwait = 0;
    if (reason && !pp_joltwait_reason) pp_joltwait_reason = reason;
}

/* Owner thread, between frames: verify both entries, preallocate, swap both or neither. */
static const char *pp_joltwait_enable(void) {
    if (!pp_installed) return "physics probe not installed";
    if (pp_joltwait) return NULL;
    if (pp_joltwait_reason) return pp_joltwait_reason;
    if (sizeof(x4_physics_mode_vtables) / sizeof(x4_physics_mode_vtables[0]) != PP_MODE_ENTRIES) return "mode table size";
    void *ours[PP_MODE_ENTRIES] = {(void *)pj_sb_create, (void *)pj_sb_destroy};
    for (int k = 0; k < PP_MODE_ENTRIES; ++k) {
        void **entry = (void **)(image + x4_physics_mode_vtables[k].entry);
        void *function = image + x4_physics_mode_vtables[k].fn;
        if (!readable(entry, sizeof(void *), 0) || *entry != function) return "barrier entry not original";
        pp_mode_entry[k] = entry;
        pp_mode_orig[k] = function;
    }
    for (int i = 0; i < PJ_BARRIERS; ++i)
        if (pj_sb_pool[i].in_use || !pj_sb_reserve(&pj_sb_pool[i], 16384)) return "barrier pool unavailable";
    pj_sb.execute = (pj_execute_fn)(void *)(image + PP_EXECUTE_RVA);
    if (!pp_write_entry(pp_mode_entry[0], pp_mode_orig[0], ours[0])) return "barrier entry write failed";
    if (!pp_write_entry(pp_mode_entry[1], pp_mode_orig[1], ours[1])) {
        pp_write_entry(pp_mode_entry[0], ours[0], pp_mode_orig[0]);
        return "barrier entry write failed";
    }
    pp_joltwait_seen = pp_joltwait_anomalies();
    pp_joltwait = 1;
    return NULL;
}

static void pp_uninstall(void) {
    InterlockedExchange(&pp_mode_request, PP_REQ_NONE);
    pp_joltwait_disable(NULL);
    pp_sampler_stop();
    if (!pp_installed) return;
    void *wrappers[PP_ENTRIES] = {(void *)pp_create, (void *)pp_wait, (void *)pp_added, (void *)pp_persisted, (void *)pp_removed};
    for (int k = 0; k < PP_ENTRIES; ++k)
        if (pp_entry[k] && pp_orig[k]) pp_write_entry(pp_entry[k], wrappers[k], pp_orig[k]);
    pp_installed = 0;
}

/* Owner thread, frame end (slot 15): fold the frame, refresh the published pointers. */
static void pp_frame_end(void) {
    if (!pp_installed) return;
    if (pp_joltwait) {
        ++pp_joltwait_frames;
        ++pp_win_joltwait_frames;
        if (pp_joltwait_anomalies() != pp_joltwait_seen)
            pp_joltwait_disable(pj_sb.alloc_failures ? "job list allocation failed" :
                                pj_sb.foreign_calls ? "barrier used from another thread" : "no runnable job");
    }
    /* Between frames on the owner thread: no Jolt update (and no barrier) is in flight. */
    LONG request = InterlockedExchange(&pp_mode_request, PP_REQ_NONE);
    if (request == PP_REQ_JOLTWAIT) {
        const char *why = pp_joltwait_enable();
        if (why && !pp_joltwait_reason) pp_joltwait_reason = why;
    } else if (request == PP_REQ_ORIGINAL) pp_joltwait_disable(NULL);
    void *jtm = NULL, *pm = NULL;
    if (readable(image + PP_JTM_RVA, sizeof(void *), 0)) jtm = *(void **)(image + PP_JTM_RVA);
    if (readable(image + PP_PATHING_RVA, sizeof(void *), 0)) pm = *(void **)(image + PP_PATHING_RVA);
    pp_pub_jtm = jtm;
    pp_pub_pm = pm;
    pp_pub_loading = registered_table && *(int *)((unsigned char *)registered_table + 0x130);
    ++pp_win.frames;
    double frame_ms = 0.0;
    for (int s = 0; s < PP_SCENES; ++s) {
        pp_win.sum.jolt_ms[s] += pp_frame.jolt_ms[s];
        frame_ms += pp_frame.jolt_ms[s];
        void *scene = pp_ptr_at(s ? PP_SCENE1_RVA : PP_SCENE0_RVA), *sys = NULL;
        uint64_t synced = 0;
        if (scene && pp_peek((unsigned char *)scene + PP_SCENE_SYS, &sys, sizeof(sys)) && sys) {
            pp_jobsys[s] = (unsigned char *)sys + PP_SYS_JOBS;
            if (pp_peek((unsigned char *)sys + PP_SYS_SYNC_COUNT, &synced, sizeof(synced)) && synced < 0x100000) {
                pp_win.synced_bodies[s] += synced;
                if (synced > pp_win.synced_max[s]) pp_win.synced_max[s] = synced;
            }
        } else pp_jobsys[s] = NULL;
    }
    if (pp_frame.updates) ++pp_win.frames_with_jolt;
    if (frame_ms > pp_win.jolt_frame_max_ms) pp_win.jolt_frame_max_ms = frame_ms;
    pp_win.sum.updates += pp_frame.updates;
    pp_win.sum.jobs += pp_frame.jobs;
    pp_win.sum.added += pp_frame.added;
    pp_win.sum.persisted += pp_frame.persisted;
    pp_win.sum.removed += pp_frame.removed;
    pp_win.sum.pairs += pp_frame.pairs;
    pp_win.sum.multi_pairs += pp_frame.multi_pairs;
    pp_win.sum.pair_overflow += pp_frame.pair_overflow;
    memset(&pp_frame, 0, sizeof(pp_frame));
    if (++pp_pair_gen == 0) { memset(pp_pairs, 0, sizeof(pp_pairs)); pp_pair_gen = 1; }
    if (pm) {
        uintptr_t va[4] = {0};
        float dt = 0.0f;
        double budget = 0.0;
        if (pp_peek((unsigned char *)pm + PP_PM_BATCH_A, &va[0], 16) && pp_peek((unsigned char *)pm + PP_PM_BATCH_B, &va[2], 16)) {
            pp_last_batches[0] = va[1] >= va[0] && va[1] - va[0] < 0x10000 ? (int)((va[1] - va[0]) / 0x20) : -1;
            pp_last_batches[1] = va[3] >= va[2] && va[3] - va[2] < 0x10000 ? (int)((va[3] - va[2]) / 0x20) : -1;
        }
        if (pp_peek((unsigned char *)pm + PP_PM_DT, &dt, sizeof(dt))) pp_last_dt = dt;
        if (pp_peek((unsigned char *)pm + PP_PM_BUDGET, &budget, sizeof(budget))) pp_last_budget_ms = budget * 1000.0;
    }
}

/* Verify every site, then install all five entries or none. 1 when installed. */
static int pp_install(int verify_code) {
    pp_reason = NULL;
    if (pp_installed) return 1;
    void *wrappers[PP_ENTRIES] = {(void *)pp_create, (void *)pp_wait, (void *)pp_added, (void *)pp_persisted, (void *)pp_removed};
    size_t n = sizeof(x4_physics_vtables) / sizeof(x4_physics_vtables[0]);
    if (n != PP_ENTRIES) { pp_reason = "vtable table size"; return 0; }
    if (verify_code && !verify_checks(x4_physics_checks, sizeof(x4_physics_checks) / sizeof(x4_physics_checks[0]))) {
        pp_reason = "physics signature mismatch";
        return 0;
    }
    for (int k = 0; k < PP_ENTRIES; ++k) {
        void **entry = (void **)(image + x4_physics_vtables[k].entry);
        void *function = image + x4_physics_vtables[k].fn;
        if (!readable(entry, sizeof(void *), 0) || *entry != function) { pp_reason = "vtable entry not original"; return 0; }
        pp_entry[k] = entry;
        pp_orig[k] = function;
    }
    for (int k = 0; k < PP_ENTRIES; ++k) {
        if (!pp_write_entry(pp_entry[k], pp_orig[k], wrappers[k])) {
            for (int j = 0; j < k; ++j) pp_write_entry(pp_entry[j], wrappers[j], pp_orig[j]);
            pp_reason = "vtable write failed";
            return 0;
        }
    }
    memset(&pp_frame, 0, sizeof(pp_frame));
    memset(&pp_win, 0, sizeof(pp_win));
    AcquireSRWLockExclusive(&pp_lock);
    memset(&pp_samp, 0, sizeof(pp_samp));
    ReleaseSRWLockExclusive(&pp_lock);
    pp_installed = 1;
    pp_stop = 0;
    pp_timer = CreateWaitableTimerExW(NULL, NULL, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    pp_thread = CreateThread(NULL, 0, pp_sampler, NULL, 0, NULL);
    if (!pp_thread) pp_reason = "sampler thread not started";  /* hooks keep counting */
    return 1;
}

/* Any thread: queue a mode for the owner thread's next frame end. NULL when queued. A refusal found
   there is reported through stats() (joltwait_reason) and latched. */
static const char *pp_request_mode(const char *mode) {
    LONG code;
    if (!pp_installed) return pp_reason ? pp_reason : "physics probe not installed";
    if (!mode) return "mode required";
    if (!strcmp(mode, "joltwait")) code = PP_REQ_JOLTWAIT;
    else if (!strcmp(mode, "original")) code = PP_REQ_ORIGINAL;
    else return "unsupported physics mode";
    if (code == PP_REQ_JOLTWAIT && pp_joltwait_reason) return pp_joltwait_reason;
    if (GetCurrentThreadId() != owner_thread) ++pp_lua_foreign;
    InterlockedExchange(&pp_mode_request, code);
    return NULL;
}

/* Lua owner:physics_mode('joltwait' | 'original') -> true (queued) or nil, reason. X4 may run the UI
   script on a thread other than the frame brackets' thread (lane battle-ab-001), so the switch itself
   happens at the owner thread's next frame end. */
static int pp_lua_mode(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    const char *mode = p_lua_type(L, 2) == LUA_TSTRING ? p_lua_tolstring(L, 2, NULL) : NULL;
    const char *why = (o != current || !o->armed) ? "not the active owner" : pp_request_mode(mode);
    if (why) {
        p_lua_pushnil(L);
        p_lua_pushlstring(L, why, strlen(why));
        return 2;
    }
    p_lua_pushboolean(L, 1);
    return 1;
}

/* stats() fields for one window, then reset the window. */
static void pp_stats(lua_State *L, void (*field)(lua_State *, const char *, double)) {
    field(L, "phys_installed", pp_installed);
    if (!pp_installed) return;
    field(L, "phys_mode", pp_joltwait);
    field(L, "joltwait_frames", (double)pp_joltwait_frames);
    field(L, "joltwait_jobs", (double)(pj_sb.jobs_run - pp_joltwait_jobs_mark));
    field(L, "joltwait_max_jobs", (double)pj_sb.max_jobs);
    field(L, "joltwait_anomalies", (double)pp_joltwait_anomalies());
    field(L, "phys_mode_frames", (double)pp_win_joltwait_frames);
    field(L, "phys_lua_foreign", (double)pp_lua_foreign);
    pp_joltwait_jobs_mark = pj_sb.jobs_run;
    pp_win_joltwait_frames = 0;
    double frames = pp_win.frames ? (double)pp_win.frames : 1.0;
    field(L, "phys_frames", (double)pp_win.frames);
    field(L, "phys_updates", (double)pp_win.sum.updates / frames);
    field(L, "phys_jolt_ms", (pp_win.sum.jolt_ms[0] + pp_win.sum.jolt_ms[1]) / frames);
    field(L, "phys_jolt0_ms", pp_win.sum.jolt_ms[0] / frames);
    field(L, "phys_jolt1_ms", pp_win.sum.jolt_ms[1] / frames);
    field(L, "phys_jolt_max_ms", pp_win.jolt_frame_max_ms);
    field(L, "phys_jobs", (double)pp_win.sum.jobs / frames);
    field(L, "phys_added", (double)pp_win.sum.added / frames);
    field(L, "phys_persisted", (double)pp_win.sum.persisted / frames);
    field(L, "phys_removed", (double)pp_win.sum.removed / frames);
    field(L, "phys_pairs", (double)pp_win.sum.pairs / frames);
    field(L, "phys_multi_pairs", (double)pp_win.sum.multi_pairs / frames);
    field(L, "phys_multi_pairs_total", (double)pp_win.sum.multi_pairs);
    field(L, "phys_pair_overflow", (double)pp_win.sum.pair_overflow);
    field(L, "phys_bodies0", (double)pp_win.synced_bodies[0] / frames);
    field(L, "phys_bodies1", (double)pp_win.synced_bodies[1] / frames);
    field(L, "phys_bodies_max", (double)(pp_win.synced_max[0] > pp_win.synced_max[1] ? pp_win.synced_max[0] : pp_win.synced_max[1]));
    field(L, "path_batches_a", pp_last_batches[0]);
    field(L, "path_batches_b", pp_last_batches[1]);
    field(L, "path_budget_ms", pp_last_budget_ms);
    field(L, "path_dt", pp_last_dt);
    pp_sampled s;
    AcquireSRWLockExclusive(&pp_lock);
    s = pp_samp;
    memset(&pp_samp, 0, sizeof(pp_samp));
    ReleaseSRWLockExclusive(&pp_lock);
    double live = s.samples > s.loading ? (double)(s.samples - s.loading) : 1.0;
    field(L, "jtm_samples", (double)s.samples);
    field(L, "jtm_workers", s.workers);
    field(L, "jtm_busy", (double)s.busy_sum / live);
    field(L, "jtm_busy_max", (double)s.busy_max);
    field(L, "jtm_cat1", (double)s.cat1_sum / live);
    field(L, "jtm_cat2", (double)s.cat2_sum / live);
    field(L, "jtm_other", (double)s.other_sum / live);
    field(L, "jtm_queue", (double)s.queue_sum / live);
    field(L, "jtm_queue_max", (double)s.queue_max);
    field(L, "path_inflight", (double)s.inflight / live);
    field(L, "jtm_jolt_samples", (double)s.jolt_samples);
    field(L, "jtm_busy_in_jolt", s.jolt_samples ? (double)s.jolt_busy / (double)s.jolt_samples : 0.0);
    field(L, "path_passes", (double)s.passes);
    field(L, "path_pass_ms", s.passes ? s.pass_ms_sum / (double)s.passes : 0.0);
    field(L, "path_pass_max_ms", s.pass_ms_max);
    field(L, "path_joins", (double)s.joins);
    field(L, "path_tight_joins", (double)s.tight_joins);
    field(L, "path_slack_ms", s.joins ? s.slack_ms_sum / (double)s.joins : 0.0);
    memset(&pp_win, 0, sizeof(pp_win));
}
