/* MSVC translation unit exposing physics_jobs.h to the offline harness (physics_determinism.cpp built
   with --msvc): the harness's job system forwards CreateBarrier/DestroyBarrier/WaitForJobs here, so the
   C serial barrier drives an MSVC build of Jolt 5.5.0 through the same ABI it uses inside X4. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "physics_jobs.h"

void pjm_set_execute(pj_execute_fn execute) { pj_sb.execute = execute; }
void *pjm_create_barrier(void *system) { return pj_sb_create(system); }
void pjm_destroy_barrier(void *system, void *barrier) { pj_sb_destroy(system, (pj_serial_barrier *)barrier); }
void pjm_wait(void *system, void *barrier) { pj_sb_wait(system, (pj_serial_barrier *)barrier); }
unsigned long long pjm_jobs_run(void) { return pj_sb.jobs_run; }
unsigned long long pjm_anomalies(void) { return pj_sb.stalls + pj_sb.foreign_calls + pj_sb.alloc_failures; }

/* Parallel pool and contact buffer (physics_pool.h) for the harness's X4PoolJobSystem and buffered listener. */
#include "physics_pool.h"

int pjp_start(int threads, pj_execute_fn execute) { return pj_pool_start(threads, execute); }
void pjp_stop(void) { pj_pool_stop(); }
int pjp_max_concurrency(void *system) { return pj_pool_max_concurrency(system); }
void *pjp_create_job(void *system, void **ret, const char *name, uint32_t color, const void *function, uint32_t deps) {
    return pj_pool_create_job(system, ret, name, color, function, deps);
}
void pjp_queue_job(void *system, void *job) { pj_pool_queue_job(system, job); }
void pjp_queue_jobs(void *system, void **jobs, uint32_t n) { pj_pool_queue_jobs(system, jobs, n); }
void pjp_free_job(void *system, void *job) { pj_pool_free_job(system, job); }
void *pjp_create_barrier(void *system) { return pj_pool_create_barrier(system); }
void pjp_destroy_barrier(void *system, void *barrier) { pj_pool_destroy_barrier(system, (pj_pool_barrier *)barrier); }
void pjp_wait(void *system, void *barrier) { pj_pool_wait(system, (pj_pool_barrier *)barrier); }
unsigned long long pjp_stats(int which) {
    switch (which) {
    case 0: return (unsigned long long)pj_pool.run_workers;
    case 1: return (unsigned long long)pj_pool.run_waiter;
    case 2: return (unsigned long long)pj_pool.inline_runs;
    default: return (unsigned long long)pj_pool.alloc_failures;
    }
}
int pjc_init(void) { return pj_contacts_init(1u << 17, 64u << 20); }
void *pjc_add(uint32_t kind, void *listener, void *body1, void *body2, const void *manifold, const void *settings) {
    if (!pj_contacts.records && !pjc_init()) return NULL;
    pj_contact *c = pj_contacts_add(kind, listener, body1, body2, manifold, settings);
    if (c) c->sensor_after = ((const unsigned char *)settings)[PJ_SETTINGS_SENSOR];
    return c;
}
void pjc_removed(void *listener, const void *pair) {
    if (!pj_contacts.records && !pjc_init()) return;
    pj_contacts_removed(listener, pair);
}
void pjc_replay(pj_contact_fn added, pj_contact_fn persisted, pj_removed_fn removed) {
    if (pj_contacts.records) pj_contacts_replay(added, persisted, removed);
}
unsigned long long pjc_stats(int which) {
    return which == 0 ? pj_contacts.replayed : which == 1 ? (unsigned long long)pj_contacts.lost : pj_contacts.sensor_mismatches;
}
