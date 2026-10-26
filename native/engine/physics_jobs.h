/* Jolt job execution for the physics rewrite (plain C, Windows). Used by physics_probe.h inside
 * fixedtick.c (driving X4's own Jolt 5.5.0 code), by test_physics_jobs.c (synthetic jobs) and by
 * the MSVC offline harness physics_jobs_msvc.cpp (driving an MSVC build of Jolt 5.5.0).
 *
 * Objects are laid out as the MSVC x64 ABI lays out JPH::JobSystem::Job and JPH::JobSystem::Barrier
 * (verified on X4 9.00: XJobSystem::CreateJob 0xc5960, Job::Execute 0xb4100, XBarrier 0xc5c30..):
 *   Job (0x58): +0x00 JobSystem*, +0x08 barrier (0, Barrier*, -1 = done), +0x10 std::function<void()>
 *   (0x40 bytes, impl pointer at +0x38), +0x50 reference count, +0x54 dependency count
 *   (0xe0e0e0e0 executing, 0xd0d0d0d0 done).
 *   Barrier vtable: [0] AddJob(const JobHandle &), [1] AddJobs(const JobHandle *, uint), [2] dtor,
 *   [3] OnJobFinished(Job *). JobSystem vtable: [5] WaitForJobs, [8] FreeJob.
 *
 * Serial barrier ("joltwait"): the exact schedule of X4's XBarrier::Wait (0xc5dc0) - repeatedly run the
 * FIRST job, in insertion order, whose dependency count is zero, on the waiting thread - but without the
 * critical section around each job and without the Sleep(0) X4 calls after every job. Same jobs, same
 * order, same thread: results are bit-identical, including the order of contact callbacks. */
#include <stdint.h>
#include <string.h>

#define PJ_JOB_SYSTEM 0x00
#define PJ_JOB_BARRIER 0x08
#define PJ_JOB_FUNCTION 0x10
#define PJ_JOB_FUNCTION_IMPL 0x48
#define PJ_JOB_REFS 0x50
#define PJ_JOB_DEPS 0x54
#define PJ_JOB_SIZE 0x58
#define PJ_EXECUTING 0xe0e0e0e0u
#define PJ_DONE 0xd0d0d0d0u
#define PJ_SLOT_FREE_JOB 8
#define PJ_BARRIER_SLOT_FINISHED 3
#define PJ_BARRIERS 8

typedef uint32_t (*pj_execute_fn)(void *job);

static volatile uint32_t *pj_deps_of(void *job) { return (volatile uint32_t *)((unsigned char *)job + PJ_JOB_DEPS); }
static volatile LONG *pj_refs_of(void *job) { return (volatile LONG *)((unsigned char *)job + PJ_JOB_REFS); }
static volatile LONG64 *pj_barrier_of(void *job) { return (volatile LONG64 *)((unsigned char *)job + PJ_JOB_BARRIER); }

static void pj_addref(void *job) { InterlockedIncrement(pj_refs_of(job)); }

static void pj_release(void *job) {
    if (InterlockedDecrement(pj_refs_of(job)) == 0) {
        void *system = *(void **)job;
        ((void (*)(void *, void *))(*(void ***)system)[PJ_SLOT_FREE_JOB])(system, job);
    }
}

#ifdef PJ_C_EXECUTE
/* C replica of JPH::JobSystem::Job::Execute for builds without X4 (synthetic tests). */
static uint32_t pj_execute_c(void *job) {
    LONG state = InterlockedCompareExchange((volatile LONG *)pj_deps_of(job), (LONG)PJ_EXECUTING, 0);
    if (state != 0) return (uint32_t)state;
    void *impl = *(void **)((unsigned char *)job + PJ_JOB_FUNCTION_IMPL);
    ((void (*)(void *))(*(void ***)impl)[2])(impl);   /* std::function _Do_call */
    LONG64 barrier = InterlockedExchange64(pj_barrier_of(job), -1);
    InterlockedCompareExchange((volatile LONG *)pj_deps_of(job), (LONG)PJ_DONE, (LONG)PJ_EXECUTING);
    if (barrier) ((void (*)(void *, void *))(*(void ***)(uintptr_t)barrier)[PJ_BARRIER_SLOT_FINISHED])((void *)(uintptr_t)barrier, job);
    return PJ_DONE;
}
#endif

typedef struct pj_serial_barrier {
    void **vtable;                 /* must stay first: Jolt calls through it */
    volatile LONG in_use;
    SRWLOCK lock;
    uint32_t count, cap;
    void **jobs;                   /* added jobs (one reference each), insertion order */
    DWORD waiter;                  /* thread that called WaitForJobs (0 when none) */
} pj_serial_barrier;

typedef struct {
    pj_execute_fn execute;
    uint64_t waits, jobs_run, foreign_calls, stalls, max_jobs, alloc_failures;
} pj_serial_stats;

static pj_serial_barrier pj_sb_pool[PJ_BARRIERS];
static pj_serial_stats pj_sb;

static int pj_sb_reserve(pj_serial_barrier *b, uint32_t need) {
    if (need <= b->cap) return 1;
    uint32_t cap = b->cap ? b->cap : 256;
    while (cap < need) cap *= 2;
    void **mem = b->jobs ? HeapReAlloc(GetProcessHeap(), 0, b->jobs, cap * sizeof(void *))
                         : HeapAlloc(GetProcessHeap(), 0, cap * sizeof(void *));
    if (!mem) { ++pj_sb.alloc_failures; return 0; }
    b->jobs = mem;
    b->cap = cap;
    return 1;
}

static void pj_sb_note_thread(pj_serial_barrier *b) {
    if (b->waiter && GetCurrentThreadId() != b->waiter) ++pj_sb.foreign_calls;
}

static void pj_sb_add_job(pj_serial_barrier *b, void *const *handle) {
    void *job = *handle;
    pj_sb_note_thread(b);
    if (InterlockedCompareExchange64(pj_barrier_of(job), (LONG64)(uintptr_t)b, 0) != 0) return;  /* already done */
    pj_addref(job);
    AcquireSRWLockExclusive(&b->lock);
    if (pj_sb_reserve(b, b->count + 1)) {
        b->jobs[b->count++] = job;
        if (b->count > pj_sb.max_jobs) pj_sb.max_jobs = b->count;
    }
    ReleaseSRWLockExclusive(&b->lock);
}

static void pj_sb_add_jobs(pj_serial_barrier *b, void *const *handles, uint32_t n) {
    for (uint32_t i = 0; i < n; ++i) pj_sb_add_job(b, &handles[i]);
}

static void *pj_sb_dtor(pj_serial_barrier *b, uint32_t flags) { (void)flags; return b; }

static void pj_sb_on_job_finished(pj_serial_barrier *b, void *job) {
    pj_sb_note_thread(b);
    AcquireSRWLockExclusive(&b->lock);
    for (uint32_t i = 0; i < b->count; ++i)
        if (b->jobs[i] == job) {
            memmove(&b->jobs[i], &b->jobs[i + 1], (b->count - i - 1) * sizeof(void *));
            --b->count;
            break;
        }
    ReleaseSRWLockExclusive(&b->lock);
    pj_release(job);
}

static void *pj_sb_vtable[4] = {(void *)pj_sb_add_job, (void *)pj_sb_add_jobs, (void *)pj_sb_dtor, (void *)pj_sb_on_job_finished};

/* JobSystem slot 3 CreateBarrier. */
static void *pj_sb_create(void *system) {
    (void)system;
    for (int i = 0; i < PJ_BARRIERS; ++i)
        if (InterlockedCompareExchange(&pj_sb_pool[i].in_use, 1, 0) == 0) {
            pj_serial_barrier *b = &pj_sb_pool[i];
            b->vtable = pj_sb_vtable;
            b->count = 0;
            b->waiter = 0;
            return b;
        }
    return NULL;
}

/* JobSystem slot 4 DestroyBarrier. */
static void pj_sb_destroy(void *system, pj_serial_barrier *b) {
    (void)system;
    if (b) InterlockedExchange(&b->in_use, 0);
}

/* JobSystem slot 5 WaitForJobs: X4's schedule without the per-job critical section and Sleep(0). */
static void pj_sb_wait(void *system, pj_serial_barrier *b) {
    (void)system;
    ++pj_sb.waits;
    b->waiter = GetCurrentThreadId();
    for (;;) {
        void *run = NULL;
        AcquireSRWLockExclusive(&b->lock);
        while (b->count && *pj_deps_of(b->jobs[0]) == PJ_DONE) {   /* finished at the front */
            void *job = b->jobs[0];
            memmove(&b->jobs[0], &b->jobs[1], (b->count - 1) * sizeof(void *));
            --b->count;
            ReleaseSRWLockExclusive(&b->lock);
            pj_release(job);
            AcquireSRWLockExclusive(&b->lock);
        }
        if (!b->count) { ReleaseSRWLockExclusive(&b->lock); break; }
        for (uint32_t i = 0; i < b->count; ++i)
            if (*pj_deps_of(b->jobs[i]) == 0) { run = b->jobs[i]; break; }
        ReleaseSRWLockExclusive(&b->lock);
        if (run) {
            if (pj_sb.execute(run) == PJ_DONE) ++pj_sb.jobs_run;   /* OnJobFinished removes it */
        } else {
            ++pj_sb.stalls;     /* nothing runnable: cannot happen single-threaded; never spin hard */
            SwitchToThread();
        }
    }
    b->waiter = 0;
}
