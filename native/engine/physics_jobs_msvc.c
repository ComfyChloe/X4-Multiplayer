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
