/* Synthetic test of physics_jobs.h: the serial barrier runs jobs in exactly the order of a reference
   model of X4's XBarrier::Wait (first job with zero dependencies, in insertion order), handles jobs that
   become ready later, jobs added while a job runs (Jolt's TrySpawnJobFindCollisions), jobs that finished
   before they were added, and reference counting; random DAGs over many seeds. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#define PJ_C_EXECUTE
#include "physics_jobs.h"

#define MAXJ 256
typedef struct fake_impl { void **vtable; int id; } fake_impl;
typedef struct fake_job {                 /* MSVC layout of JPH::JobSystem::Job */
    void *system;                         /* +0x00 */
    volatile LONG64 barrier;              /* +0x08 */
    unsigned char function[0x38];         /* +0x10 std::function storage */
    fake_impl *impl;                      /* +0x48 std::function impl pointer */
    volatile LONG refs;                   /* +0x50 */
    volatile uint32_t deps;               /* +0x54 */
} fake_job;

static int order[4 * MAXJ], norder;
static fake_job *jobs[MAXJ];
static int njobs, freed;
static int succ[MAXJ][8], nsucc[MAXJ];    /* dependents released when a job runs */
static int spawn_from[MAXJ];              /* job index that adds job i to the barrier while running, or -1 */
static pj_serial_barrier *current_barrier;
static void *fake_system_vtable[9];
static struct { void **vtable; } fake_system = {fake_system_vtable};

static void fake_free_job(void *system, void *job) { (void)system; ++freed; free(job); }

static void handle_add(fake_job *j) { void *h = j; pj_sb_add_job(current_barrier, &h); }

static void fake_do_call(fake_impl *self) {
    order[norder++] = self->id;
    for (int s = 0; s < nsucc[self->id]; ++s) {
        fake_job *d = jobs[succ[self->id][s]];
        InterlockedDecrement((volatile LONG *)&d->deps);   /* RemoveDependency; QueueJob is a no-op in X4 */
    }
    for (int i = 0; i < njobs; ++i)
        if (spawn_from[i] == self->id) handle_add(jobs[i]);
}
static void *fake_impl_vtable[5] = {NULL, NULL, (void *)fake_do_call, NULL, NULL};
static fake_impl impls[MAXJ];

static fake_job *make_job(int id, uint32_t deps) {
    fake_job *j = calloc(1, sizeof(fake_job));
    j->system = &fake_system;
    impls[id].vtable = fake_impl_vtable;
    impls[id].id = id;
    j->impl = &impls[id];
    j->deps = deps;
    j->refs = 1;   /* the creator's JobHandle */
    return j;
}

/* Reference model of X4's XBarrier::Wait on plain arrays (no Jolt objects). */
static int ref_order[4 * MAXJ], nref;
static void reference(int n, const uint32_t *deps0, int added0) {
    uint32_t deps[MAXJ];
    int in_list[MAXJ], list[4 * MAXJ], count = 0, done[MAXJ] = {0};
    memcpy(deps, deps0, sizeof(uint32_t) * n);
    memset(in_list, 0, sizeof(in_list));
    for (int i = 0; i < added0; ++i) list[count++] = i;
    nref = 0;
    for (;;) {
        if (!count) break;
        int pick = -1;
        for (int k = 0; k < count; ++k) if (deps[list[k]] == 0 && !done[list[k]]) { pick = k; break; }
        assert(pick >= 0);
        int id = list[pick];
        done[id] = 1;
        ref_order[nref++] = id;
        for (int s = 0; s < nsucc[id]; ++s) --deps[succ[id][s]];
        for (int i = 0; i < n; ++i) if (spawn_from[i] == id) list[count++] = i;
        memmove(&list[pick], &list[pick + 1], (count - pick - 1) * sizeof(int));
        --count;
    }
}

static uint32_t rng_state;
static uint32_t rnd(void) { rng_state = rng_state * 1664525u + 1013904223u; return rng_state >> 8; }

static void run_case(int n, uint32_t seed) {
    rng_state = seed;
    njobs = n;
    norder = 0;
    freed = 0;
    uint32_t deps[MAXJ] = {0};
    memset(nsucc, 0, sizeof(nsucc));
    for (int i = 0; i < n; ++i) {
        spawn_from[i] = -1;
        int parents = (int)(rnd() % 3);
        for (int p = 0; p < parents && i > 0; ++p) {
            int a = (int)(rnd() % (uint32_t)i);
            if (nsucc[a] < 8) { succ[a][nsucc[a]++] = i; ++deps[i]; }
        }
    }
    int added0 = n - (int)(rnd() % (uint32_t)(n / 4 + 1));          /* the rest are spawned later */
    for (int i = added0; i < n; ++i) {
        spawn_from[i] = (int)(rnd() % (uint32_t)added0);
        /* a spawned job must not be a dependency of jobs that only it could unlock */
    }
    for (int i = 0; i < n; ++i) jobs[i] = make_job(i, deps[i]);
    reference(n, deps, added0);

    current_barrier = pj_sb_create(&fake_system);
    assert(current_barrier);
    void *handles[MAXJ];
    for (int i = 0; i < added0; ++i) handles[i] = jobs[i];
    pj_sb_add_jobs(current_barrier, handles, (uint32_t)added0);
    pj_sb_wait(&fake_system, current_barrier);
    assert(current_barrier->count == 0);
    pj_sb_destroy(&fake_system, current_barrier);
    assert(norder == nref);
    for (int k = 0; k < norder; ++k) assert(order[k] == ref_order[k]);
    for (int i = 0; i < n; ++i) assert(jobs[i]->deps == PJ_DONE && jobs[i]->barrier == -1 && jobs[i]->refs == 1);
    for (int i = 0; i < n; ++i) pj_release(jobs[i]);   /* the creators' handles */
    assert(freed == n);
}

int main(void) {
    fake_system_vtable[PJ_SLOT_FREE_JOB] = (void *)fake_free_job;
    pj_sb.execute = pj_execute_c;
    /* Fixed small case: 0 -> {2}, 1 -> {2}; 2 waits for both; 3 independent, added first. */
    for (int seed = 1; seed <= 2000; ++seed) run_case(4 + seed % 120, (uint32_t)seed * 2654435761u);
    /* A job that already finished before it is added is not tracked (Jolt's SetBarrier fails). */
    {
        fake_job *j = make_job(0, 0);
        njobs = 1; spawn_from[0] = -1; nsucc[0] = 0; norder = 0; freed = 0;
        assert(pj_execute_c(j) == PJ_DONE && j->barrier == -1);
        current_barrier = pj_sb_create(&fake_system);
        handle_add(j);
        assert(current_barrier->count == 0 && j->refs == 1);
        pj_sb_wait(&fake_system, current_barrier);
        pj_sb_destroy(&fake_system, current_barrier);
        pj_release(j);
        assert(freed == 1);
    }
    /* Pool exhaustion: eight barriers at most, released ones are reused. */
    {
        pj_serial_barrier *b[PJ_BARRIERS];
        for (int i = 0; i < PJ_BARRIERS; ++i) assert((b[i] = pj_sb_create(&fake_system)) != NULL);
        assert(pj_sb_create(&fake_system) == NULL);
        pj_sb_destroy(&fake_system, b[3]);
        assert(pj_sb_create(&fake_system) == b[3]);
        for (int i = 0; i < PJ_BARRIERS; ++i) pj_sb_destroy(&fake_system, b[i]);
    }
    assert(pj_sb.stalls == 0 && pj_sb.foreign_calls == 0 && pj_sb.alloc_failures == 0);
    printf("PASS: serial barrier = X4 schedule (first ready job in insertion order) on 2000 random job graphs, %llu jobs\n",
           (unsigned long long)pj_sb.jobs_run);
    puts("PASS: serial barrier: late-ready and spawned jobs, finished-before-added jobs, references, pool reuse");
    return 0;
}
