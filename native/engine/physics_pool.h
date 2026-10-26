/* Parallel Jolt job execution for the physics rewrite (plain C, Windows; include after physics_jobs.h).
 *
 * Pool ("parallel"): Jolt's jobs run on worker threads and on the waiting thread. Jolt 5.5.0 with
 * mDeterministicSimulation makes the Jolt state independent of execution order and worker count
 * (physics_determinism: bit-identical SaveState); what depends on the order is an application's contact
 * listener, so pj_contacts buffers listener calls during the step and replays them afterwards, on the
 * waiting thread, in a canonical order (pj_contacts_replay).
 *
 * JobSystem entries provided: [1] GetMaxConcurrency, [2] CreateJob, [3] CreateBarrier, [4] DestroyBarrier,
 * [5] WaitForJobs, [6] QueueJob, [7] QueueJobs, [8] FreeJob (MSVC x64 ABI, see physics_jobs.h). Jobs are
 * allocated by CreateJob here - the handle's reference is taken before the job can be queued, unlike
 * X4's CreateJob, which queues first and references after - and freed by FreeJob here. */
#include <stdlib.h>

#if defined(_MSC_VER)
#define PJ_ALIGN16 __declspec(align(16))
#else
#define PJ_ALIGN16 __attribute__((aligned(16)))
#endif

#define PJ_POOL_MAX_THREADS 16
#define PJ_QUEUE_CAP 16384u                   /* power of two */

typedef struct pj_pool_barrier {
    void **vtable;                            /* must stay first */
    volatile LONG in_use;
    SRWLOCK lock;
    void **jobs;                              /* added jobs, one reference each, released after Wait */
    uint32_t count, cap;
    volatile LONG pending;                    /* added and not yet finished */
    HANDLE done;                              /* auto-reset, set when pending reaches zero */
} pj_pool_barrier;

typedef struct {
    HANDLE threads[PJ_POOL_MAX_THREADS];
    int nthreads, max_concurrency;
    volatile LONG stop;
    SRWLOCK qlock;
    void *queue[PJ_QUEUE_CAP];
    uint32_t qhead, qtail;
    HANDLE qsem;
    pj_execute_fn execute;
    void (*thread_init)(void);                /* optional, once on each worker */
    void (*after_wait)(void);                 /* optional, on the waiting thread after all jobs finished */
    volatile LONG64 run_workers, run_waiter, inline_runs, alloc_failures, waits;
} pj_pool_t;

static pj_pool_t pj_pool;
static pj_pool_barrier pj_pb_pool[PJ_BARRIERS];

static int pj_pool_pop(void **job) {
    int ok = 0;
    AcquireSRWLockExclusive(&pj_pool.qlock);
    if (pj_pool.qhead != pj_pool.qtail) {
        *job = pj_pool.queue[pj_pool.qhead & (PJ_QUEUE_CAP - 1)];
        ++pj_pool.qhead;
        ok = 1;
    }
    ReleaseSRWLockExclusive(&pj_pool.qlock);
    return ok;
}

static void pj_pool_run(void *job, volatile LONG64 *counter) {
    pj_pool.execute(job);
    InterlockedIncrement64(counter);
    pj_release(job);                          /* the queue's reference */
}

/* [6] QueueJob: a reference for the queue, then a worker takes it (or it runs here when full). */
static void pj_pool_queue_job(void *system, void *job) {
    (void)system;
    pj_addref(job);
    int queued = 0;
    AcquireSRWLockExclusive(&pj_pool.qlock);
    if (pj_pool.qtail - pj_pool.qhead < PJ_QUEUE_CAP) {
        pj_pool.queue[pj_pool.qtail & (PJ_QUEUE_CAP - 1)] = job;
        ++pj_pool.qtail;
        queued = 1;
    }
    ReleaseSRWLockExclusive(&pj_pool.qlock);
    if (queued) ReleaseSemaphore(pj_pool.qsem, 1, NULL);
    else pj_pool_run(job, &pj_pool.inline_runs);
}

/* [7] QueueJobs */
static void pj_pool_queue_jobs(void *system, void **jobs, uint32_t n) {
    for (uint32_t i = 0; i < n; ++i) pj_pool_queue_job(system, jobs[i]);
}

/* [1] GetMaxConcurrency */
static int pj_pool_max_concurrency(void *system) { (void)system; return pj_pool.max_concurrency; }

/* [2] CreateJob(this, JobHandle *ret, name, color, const std::function &, numDependencies) -> ret. */
static void *pj_pool_create_job(void *system, void **ret, const char *name, uint32_t color, const void *function, uint32_t deps) {
    (void)name; (void)color;
    unsigned char *job = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PJ_JOB_SIZE + 8);
    if (!job) { InterlockedIncrement64(&pj_pool.alloc_failures); *ret = NULL; return ret; }
    *(void **)job = system;
    void *impl = *(void *const *)((const unsigned char *)function + 0x38);
    if (impl)   /* MSVC _Func_base::_Copy(where) returns the copy (in place or on the heap) */
        *(void **)(job + PJ_JOB_FUNCTION_IMPL) = ((void *(*)(void *, void *))(*(void ***)impl)[0])(impl, job + PJ_JOB_FUNCTION);
    *(volatile LONG *)(job + PJ_JOB_REFS) = 1;     /* the returned handle */
    *(volatile uint32_t *)(job + PJ_JOB_DEPS) = deps;
    *ret = job;
    if (deps == 0) pj_pool_queue_job(system, job);
    return ret;
}

/* [8] FreeJob: MSVC _Func_base::_Delete_this(deallocate = not stored in place), then the memory. */
static void pj_pool_free_job(void *system, void *job) {
    (void)system;
    unsigned char *j = job;
    void *impl = *(void **)(j + PJ_JOB_FUNCTION_IMPL);
    if (impl) ((void (*)(void *, int))(*(void ***)impl)[4])(impl, impl != (void *)(j + PJ_JOB_FUNCTION));
    HeapFree(GetProcessHeap(), 0, job);
}

static void pj_pb_add_job(pj_pool_barrier *b, void *const *handle) {
    void *job = *handle;
    InterlockedIncrement(&b->pending);       /* before the job can finish into this barrier */
    if (InterlockedCompareExchange64(pj_barrier_of(job), (LONG64)(uintptr_t)b, 0) != 0) {
        if (InterlockedDecrement(&b->pending) == 0) SetEvent(b->done);
        return;                               /* already finished: not tracked */
    }
    pj_addref(job);
    AcquireSRWLockExclusive(&b->lock);
    if (b->count == b->cap) {
        uint32_t cap = b->cap ? b->cap * 2 : 1024;
        void **mem = b->jobs ? HeapReAlloc(GetProcessHeap(), 0, b->jobs, cap * sizeof(void *))
                             : HeapAlloc(GetProcessHeap(), 0, cap * sizeof(void *));
        if (mem) { b->jobs = mem; b->cap = cap; }
    }
    int stored = b->count < b->cap;
    if (stored) b->jobs[b->count++] = job;
    ReleaseSRWLockExclusive(&b->lock);
    if (!stored) { InterlockedIncrement64(&pj_pool.alloc_failures); pj_release(job); }   /* still counted */
}

static void pj_pb_add_jobs(pj_pool_barrier *b, void *const *handles, uint32_t n) {
    for (uint32_t i = 0; i < n; ++i) pj_pb_add_job(b, &handles[i]);
}

static void *pj_pb_dtor(pj_pool_barrier *b, uint32_t flags) { (void)flags; return b; }

static void pj_pb_on_job_finished(pj_pool_barrier *b, void *job) {
    (void)job;
    if (InterlockedDecrement(&b->pending) == 0) SetEvent(b->done);
}

static void *pj_pb_vtable[4] = {(void *)pj_pb_add_job, (void *)pj_pb_add_jobs, (void *)pj_pb_dtor, (void *)pj_pb_on_job_finished};

/* [3] CreateBarrier */
static void *pj_pool_create_barrier(void *system) {
    (void)system;
    for (int i = 0; i < PJ_BARRIERS; ++i)
        if (InterlockedCompareExchange(&pj_pb_pool[i].in_use, 1, 0) == 0) {
            pj_pool_barrier *b = &pj_pb_pool[i];
            b->vtable = pj_pb_vtable;
            b->count = 0;
            b->pending = 0;
            if (!b->done) b->done = CreateEventW(NULL, FALSE, FALSE, NULL);
            if (!b->done) { InterlockedExchange(&b->in_use, 0); return NULL; }
            ResetEvent(b->done);
            return b;
        }
    return NULL;
}

/* [4] DestroyBarrier */
static void pj_pool_destroy_barrier(void *system, pj_pool_barrier *b) {
    (void)system;
    if (b) InterlockedExchange(&b->in_use, 0);
}

/* [5] WaitForJobs: help with queued jobs until every added job finished, run after_wait (contact
   replay), then drop the barrier's references. */
static void pj_pool_wait(void *system, pj_pool_barrier *b) {
    (void)system;
    InterlockedIncrement64(&pj_pool.waits);
    while (b->pending > 0) {
        void *job;
        if (pj_pool_pop(&job)) pj_pool_run(job, &pj_pool.run_waiter);
        else WaitForSingleObject(b->done, 1);
    }
    if (pj_pool.after_wait) pj_pool.after_wait();
    AcquireSRWLockExclusive(&b->lock);
    uint32_t n = b->count;
    b->count = 0;
    void **jobs = b->jobs;
    ReleaseSRWLockExclusive(&b->lock);
    for (uint32_t i = 0; i < n; ++i) pj_release(jobs[i]);
}

static DWORD WINAPI pj_pool_worker(LPVOID unused) {
    (void)unused;
    if (pj_pool.thread_init) pj_pool.thread_init();
    for (;;) {
        WaitForSingleObject(pj_pool.qsem, INFINITE);
        if (pj_pool.stop) break;
        void *job;
        if (pj_pool_pop(&job)) pj_pool_run(job, &pj_pool.run_workers);
    }
    return 0;
}

/* Start n workers (1..16); Jolt is told n + 1 (the waiting thread helps). 1 when all started. */
static int pj_pool_start(int n, pj_execute_fn execute) {
    if (pj_pool.nthreads || n <= 0 || n > PJ_POOL_MAX_THREADS || !execute) return 0;
    pj_pool.execute = execute;
    pj_pool.stop = 0;
    pj_pool.qhead = pj_pool.qtail = 0;
    InitializeSRWLock(&pj_pool.qlock);
    pj_pool.qsem = CreateSemaphoreW(NULL, 0, 0x7fffffff, NULL);
    if (!pj_pool.qsem) return 0;
    for (int i = 0; i < n; ++i) {
        pj_pool.threads[i] = CreateThread(NULL, 0, pj_pool_worker, NULL, 0, NULL);
        if (!pj_pool.threads[i]) break;
        ++pj_pool.nthreads;
    }
    pj_pool.max_concurrency = pj_pool.nthreads + 1;
    return pj_pool.nthreads == n;
}

/* Stop and join the workers; only between steps (nothing queued or running). */
static void pj_pool_stop(void) {
    if (!pj_pool.qsem) return;
    pj_pool.stop = 1;
    if (pj_pool.nthreads) ReleaseSemaphore(pj_pool.qsem, pj_pool.nthreads, NULL);
    for (int i = 0; i < pj_pool.nthreads; ++i) {
        if (WaitForSingleObject(pj_pool.threads[i], 2000) == WAIT_OBJECT_0) CloseHandle(pj_pool.threads[i]);
        pj_pool.threads[i] = NULL;
    }
    pj_pool.nthreads = 0;
    CloseHandle(pj_pool.qsem);
    pj_pool.qsem = NULL;
}

/* ------------------------------------------------------------------------------------------------
 * Contact callback buffer. A listener call made on any thread during the step is recorded with its
 * manifold and the ContactSettings as passed in; after the step the waiting thread replays them grouped
 * by body pair (lower id, higher id) and, within a pair, in the order the records were reserved.
 * All calls for one body pair are ordered by happens-before in every execution (one ProcessBodyPair per
 * pair and collision step, then the CCD job for that pair, then the single removed-callbacks job, steps
 * in sequence; CCD-vs-CCD pairs are only cast from the lower id), so the reservation order within a pair
 * is the serial order: per pair - and so per key of an order-dependent listener whose keys never span
 * two body pairs (X4 sums manifold points per component/part pair) - the replay reproduces the serial
 * callback sequence exactly, and the order across pairs is canonical. Neither depends on threads or the
 * job decomposition. Layouts (Jolt 5.5.0, MSVC): ContactManifold +0x00 base offset, +0x10 normal, +0x20 depth,
 * +0x24/+0x28 sub-shape ids, +0x30 points-on-1 (count, points from +0x40, 16 bytes each, 64 max),
 * +0x440 points-on-2 (count, points from +0x450); Body +0x70 BodyID; ContactSettings 0x40 bytes,
 * mIsSensor at +0x18. */
#define PJ_MANIFOLD_SIZE 0x850
#define PJ_MANIFOLD_ON1 0x30
#define PJ_MANIFOLD_ON2 0x440
#define PJ_BODY_ID 0x70
#define PJ_SETTINGS_SIZE 0x40
#define PJ_SETTINGS_SENSOR 0x18

typedef struct {
    uint32_t kind;                            /* 0 added, 1 persisted, 2 removed, 0xffffffff skipped */
    uint32_t key[4];                          /* body 1 id, body 2 id, sub-shape 1, sub-shape 2 */
    uint32_t pair_lo, pair_hi;                /* replay group: body ids, lower first */
    void *listener, *body1, *body2;
    uint32_t arena_offset, arena_size;        /* compact manifold in the arena */
    unsigned char settings[PJ_SETTINGS_SIZE]; /* as passed in, before the live decision */
    unsigned char pair[16];                   /* removed: the SubShapeIDPair */
    uint8_t sensor_after;                     /* mIsSensor after the live decision */
} pj_contact;

typedef struct {
    pj_contact *records;
    uint32_t capacity;
    volatile LONG count;
    unsigned char *arena;
    uint32_t arena_capacity;
    volatile LONG arena_used;
    volatile LONG lost;                       /* records that did not fit */
    uint64_t replayed, sensor_mismatches;
} pj_contacts_t;

static pj_contacts_t pj_contacts;

static int pj_contacts_init(uint32_t records, uint32_t arena_bytes) {
    if (pj_contacts.records) return 1;
    pj_contacts.records = VirtualAlloc(NULL, (SIZE_T)records * sizeof(pj_contact), MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    pj_contacts.arena = VirtualAlloc(NULL, arena_bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!pj_contacts.records || !pj_contacts.arena) return 0;
    pj_contacts.capacity = records;
    pj_contacts.arena_capacity = arena_bytes;
    return 1;
}

static uint32_t pj_points_bytes(const unsigned char *manifold, uint32_t count_offset) {
    uint32_t n = *(const uint32_t *)(manifold + count_offset);
    return 16 + (n > 64 ? 64 : n) * 16;      /* count (padded to 16) + points */
}

/* Record an added (kind 0) or persisted (kind 1) call. The caller stores sensor_after afterwards. */
static pj_contact *pj_contacts_add(uint32_t kind, void *listener, void *body1, void *body2, const void *manifold,
                                   const void *settings_before) {
    LONG index = InterlockedIncrement(&pj_contacts.count) - 1;
    if ((uint32_t)index >= pj_contacts.capacity) { InterlockedIncrement(&pj_contacts.lost); return NULL; }
    const unsigned char *m = manifold;
    uint32_t on1 = pj_points_bytes(m, PJ_MANIFOLD_ON1), on2 = pj_points_bytes(m, PJ_MANIFOLD_ON2);
    uint32_t size = PJ_MANIFOLD_ON1 + on1 + on2;
    LONG offset = InterlockedExchangeAdd(&pj_contacts.arena_used, (LONG)size);
    pj_contact *c = &pj_contacts.records[index];
    if ((uint32_t)offset + size > pj_contacts.arena_capacity) {
        c->kind = 0xffffffffu;
        InterlockedIncrement(&pj_contacts.lost);
        return NULL;
    }
    unsigned char *dst = pj_contacts.arena + offset;
    memcpy(dst, m, PJ_MANIFOLD_ON1);
    memcpy(dst + PJ_MANIFOLD_ON1, m + PJ_MANIFOLD_ON1, on1);
    memcpy(dst + PJ_MANIFOLD_ON1 + on1, m + PJ_MANIFOLD_ON2, on2);
    c->listener = listener;
    c->body1 = body1;
    c->body2 = body2;
    c->key[0] = *(const uint32_t *)((const unsigned char *)body1 + PJ_BODY_ID);
    c->key[1] = *(const uint32_t *)((const unsigned char *)body2 + PJ_BODY_ID);
    c->key[2] = *(const uint32_t *)(m + 0x24);
    c->key[3] = *(const uint32_t *)(m + 0x28);
    c->pair_lo = c->key[0] < c->key[1] ? c->key[0] : c->key[1];
    c->pair_hi = c->key[0] < c->key[1] ? c->key[1] : c->key[0];
    c->arena_offset = (uint32_t)offset;
    c->arena_size = size;
    memcpy(c->settings, settings_before, PJ_SETTINGS_SIZE);
    c->sensor_after = 0;
    c->kind = kind;
    return c;
}

static void pj_contacts_removed(void *listener, const void *pair) {
    LONG index = InterlockedIncrement(&pj_contacts.count) - 1;
    if ((uint32_t)index >= pj_contacts.capacity) { InterlockedIncrement(&pj_contacts.lost); return; }
    pj_contact *c = &pj_contacts.records[index];
    const uint32_t *p = pair;                 /* {body1 id, sub-shape 1, body2 id, sub-shape 2} */
    c->listener = listener;
    c->body1 = c->body2 = NULL;
    c->key[0] = p[0]; c->key[1] = p[2]; c->key[2] = p[1]; c->key[3] = p[3];
    c->pair_lo = p[0] < p[2] ? p[0] : p[2];
    c->pair_hi = p[0] < p[2] ? p[2] : p[0];
    memcpy(c->pair, pair, 16);
    c->kind = 2;
}

/* Body pair, then reservation order (the record's index: records live in one array). A total order. */
static int pj_contact_cmp(const void *a, const void *b) {
    const pj_contact *x = *(pj_contact *const *)a, *y = *(pj_contact *const *)b;
    if (x->pair_lo != y->pair_lo) return x->pair_lo < y->pair_lo ? -1 : 1;
    if (x->pair_hi != y->pair_hi) return x->pair_hi < y->pair_hi ? -1 : 1;
    return (x > y) - (x < y);
}

typedef void (*pj_contact_fn)(void *listener, void *body1, void *body2, void *manifold, void *settings);
typedef void (*pj_removed_fn)(void *listener, const void *pair);

static PJ_ALIGN16 unsigned char pj_replay_manifold[PJ_MANIFOLD_SIZE];
static PJ_ALIGN16 unsigned char pj_replay_settings[PJ_SETTINGS_SIZE];

/* Replay on the waiting thread: sort, rebuild each manifold, call the listener's functions, check that
   the listener decides the sensor flag as it was decided live. Empties the buffer. */
static void pj_contacts_replay(pj_contact_fn added, pj_contact_fn persisted, pj_removed_fn removed) {
    uint32_t n = (uint32_t)pj_contacts.count;
    if (n > pj_contacts.capacity) n = pj_contacts.capacity;
    pj_contact **order = n ? HeapAlloc(GetProcessHeap(), 0, n * sizeof(pj_contact *)) : NULL;
    if (n && !order) pj_contacts.lost += (LONG)n;
    if (order) {
        for (uint32_t i = 0; i < n; ++i) order[i] = &pj_contacts.records[i];
        qsort(order, n, sizeof(pj_contact *), pj_contact_cmp);
        for (uint32_t i = 0; i < n; ++i) {
            pj_contact *c = order[i];
            if (c->kind == 2) { removed(c->listener, c->pair); ++pj_contacts.replayed; continue; }
            if (c->kind > 1) continue;
            const unsigned char *src = pj_contacts.arena + c->arena_offset;
            uint32_t on1 = pj_points_bytes(src, PJ_MANIFOLD_ON1);
            memset(pj_replay_manifold, 0, sizeof(pj_replay_manifold));
            memcpy(pj_replay_manifold, src, PJ_MANIFOLD_ON1);
            memcpy(pj_replay_manifold + PJ_MANIFOLD_ON1, src + PJ_MANIFOLD_ON1, on1);
            memcpy(pj_replay_manifold + PJ_MANIFOLD_ON2, src + PJ_MANIFOLD_ON1 + on1, c->arena_size - PJ_MANIFOLD_ON1 - on1);
            memcpy(pj_replay_settings, c->settings, PJ_SETTINGS_SIZE);
            (c->kind ? persisted : added)(c->listener, c->body1, c->body2, pj_replay_manifold, pj_replay_settings);
            if ((pj_replay_settings[PJ_SETTINGS_SENSOR] != 0) != (c->sensor_after != 0)) ++pj_contacts.sensor_mismatches;
            ++pj_contacts.replayed;
        }
        HeapFree(GetProcessHeap(), 0, order);
    }
    pj_contacts.count = 0;
    pj_contacts.arena_used = 0;
}
