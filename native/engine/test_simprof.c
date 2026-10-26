/* Synthetic sampler tests. A worker thread runs a fake MoveWorker loop over a fake VBI table
   with known functions; the sampler (the same code the DLL uses) unwinds against this
   executable's own .pdata. Checks attribution, phases, waits, JIT-leaf recovery, truncation,
   folds, failure back-off/disable, clean stop and overhead. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include "simprof.c"

typedef struct { void **vtable; } handler;
static void *fake_table[SP_SLOTS];
static volatile LONG worker_stop;
static volatile LONG worker_ready;
static DWORD worker_id;
static const NT_TIB *worker_tib;
static HANDLE idle_timer, sound_timer;
static int64_t qpf;
static unsigned char *jit_code;
static uint64_t jit_per_ms;
static volatile uint64_t sink;
/* Ground truth (QPC ticks) per phase, measured by the worker itself. */
enum { T_IDLE, T_SLOT1, T_SLOT3, T_SLOT6, T_SLOT9, T_SLOT10, T_ALL, T_COUNT };
static volatile int64_t truth[T_COUNT];

static int64_t qpc(void) { LARGE_INTEGER v; QueryPerformanceCounter(&v); return v.QuadPart; }

__attribute__((noinline)) static void leaf_work(double us) {
    int64_t end = qpc() + (int64_t)(us * (double)qpf / 1e6);
    do { for (unsigned i = 0; i < 2000; ++i) sink += (uint64_t)i * i; } while (qpc() < end);
}
__attribute__((noinline)) static void component_move(int depth, double us) {
    if (depth) component_move(depth - 1, us); else leaf_work(us);
    sink += (uint64_t)depth;
}
__attribute__((noinline)) static void universe_update(void) { component_move(3, 4000); sink++; }
__attribute__((noinline)) static void recurse(int depth) {
    if (depth) recurse(depth - 1); else leaf_work(1000);
    sink += (uint64_t)depth;
}
static void timed_wait(HANDLE timer, double us) {
    LARGE_INTEGER due; due.QuadPart = -(LONGLONG)(us * 10);
    SetWaitableTimer(timer, &due, 0, NULL, NULL, FALSE);
    WaitForSingleObject(timer, INFINITE);
}

#define TIMED(slot, body) do { int64_t a_ = qpc(); body; truth[slot] += qpc() - a_; } while (0)
__attribute__((noinline)) static void slot1_body(handler *h) { (void)h; TIMED(T_SLOT1, leaf_work(500)); }
/* Tail call: the frame below Run is slot1_body, not the registered Move(). */
__attribute__((noinline)) static void timers_move(handler *h) { __attribute__((musttail)) return slot1_body(h); }
__attribute__((noinline)) static void client_move(handler *h) { (void)h; TIMED(T_SLOT3, universe_update()); }
__attribute__((noinline)) static void ui_move(handler *h) {
    (void)h; TIMED(T_SLOT6, ((void (*)(uint64_t))(void *)jit_code)(jit_per_ms));
}
__attribute__((noinline)) static void sound_move(handler *h) { (void)h; TIMED(T_SLOT9, timed_wait(sound_timer, 1500)); }
__attribute__((noinline)) static void render_move(handler *h) { (void)h; TIMED(T_SLOT10, recurse(80)); }
static void no_op(handler *h) { (void)h; }
static void *vt1[] = {(void *)no_op, (void *)timers_move, (void *)no_op};
static void *vt3[] = {(void *)no_op, (void *)client_move, (void *)no_op};
static void *vt6[] = {(void *)no_op, (void *)ui_move, (void *)no_op};
static void *vt9[] = {(void *)no_op, (void *)sound_move, (void *)no_op};
static void *vt10[] = {(void *)no_op, (void *)render_move, (void *)no_op};
static handler h1 = {vt1}, h3 = {vt3}, h6 = {vt6}, h9 = {vt9}, h10 = {vt10};

__attribute__((noinline)) static void frame_start(void) {
    TIMED(T_IDLE, timed_wait(idle_timer, 2000));
    simprof_loop(1);
}

/* Fake U::MoveWorker::Run dispatch, shaped like the engine's: the slot index lives in RBX
   across `call [rax+8]`. With SEH unwind directives so it has .pdata like the engine. */
void asm_run(void **table, void (*start)(void));
extern char asm_slot_return[];
__asm__(
    ".text\n.globl asm_run\n.globl asm_slot_return\n.def asm_run; .scl 2; .type 32; .endef\n"
    ".seh_proc asm_run\nasm_run:\n"
    "  pushq %rbx\n  .seh_pushreg %rbx\n  pushq %rsi\n  .seh_pushreg %rsi\n  pushq %rdi\n  .seh_pushreg %rdi\n"
    "  subq $32, %rsp\n  .seh_stackalloc 32\n  .seh_endprologue\n"
    "  movq %rcx, %rsi\n  movl $99, %ebx\n  callq *%rdx\n  xorl %ebx, %ebx\n"
    "1:\n  movq (%rsi,%rbx,8), %rcx\n  testq %rcx, %rcx\n  jz 2f\n  movq (%rcx), %rax\n  callq *8(%rax)\n"
    "asm_slot_return:\n2:\n  incq %rbx\n  cmpq $17, %rbx\n  jb 1b\n"
    "  addq $32, %rsp\n  popq %rdi\n  popq %rsi\n  popq %rbx\n  retq\n.seh_endproc\n");

__attribute__((noinline)) static DWORD WINAPI fake_run(LPVOID unused) {
    (void)unused;
    worker_id = GetCurrentThreadId();
    worker_tib = (const NT_TIB *)NtCurrentTeb();
    InterlockedExchange(&worker_ready, 1);
    while (!worker_stop) {
        int64_t t0 = qpc();
        simprof_note_slots(fake_table);
        asm_run(fake_table, frame_start);
        simprof_loop(2);
        truth[T_ALL] += qpc() - t0;
    }
    return 0;
}

/* push rbx; push rbp; sub rsp,28h; mov rax,rcx; l: dec rax; jnz l; add rsp,28h; pop rbp; pop rbx; ret */
static const unsigned char jit_bytes[] = {0x53, 0x55, 0x48, 0x83, 0xec, 0x28, 0x48, 0x89, 0xc8, 0x48, 0xff,
                                          0xc8, 0x75, 0xfb, 0x48, 0x83, 0xc4, 0x28, 0x5d, 0x5b, 0xc3};

static void make_jit(void) {
    jit_code = VirtualAlloc(NULL, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    memcpy(jit_code, jit_bytes, sizeof(jit_bytes));
    DWORD old;
    assert(VirtualProtect(jit_code, 4096, PAGE_EXECUTE_READ, &old));
    FlushInstructionCache(GetCurrentProcess(), jit_code, 4096);
    int64_t t = qpc();
    ((void (*)(uint64_t))(void *)jit_code)(50000000);
    double ms = (double)(qpc() - t) * 1000.0 / (double)qpf;
    jit_per_ms = (uint64_t)(50000000 / ms);
}

static uint32_t id_of_fn(void *fn) { return sp_id_of((uint64_t)(uintptr_t)fn, 1); }
static uint32_t fn_self(uint32_t id) { sp_fn *e = sp_fn_slot(id); return e ? e->self : 0; }
static uint32_t fn_incl(uint32_t id) { sp_fn *e = sp_fn_slot(id); return e ? e->incl : 0; }

static HANDLE start_worker(LPTHREAD_START_ROUTINE fn) {
    worker_stop = 0; worker_ready = 0;
    HANDLE t = CreateThread(NULL, 0, fn, NULL, 0, NULL);
    while (!worker_ready) Sleep(1);
    return t;
}

static void stop_worker(HANDLE t) {
    InterlockedExchange(&worker_stop, 1);
    assert(WaitForSingleObject(t, 5000) == WAIT_OBJECT_0);
    CloseHandle(t);
}

static void assert_not_suspended(HANDLE t) {
    DWORD previous = SuspendThread(t);
    assert(previous == 0);
    ResumeThread(t);
}

static void test_attribution(int use_register) {
    memset((void *)truth, 0, sizeof(truth));
    memset(fake_table, 0, sizeof(fake_table));
    fake_table[1] = &h1; fake_table[3] = &h3; fake_table[6] = &h6; fake_table[9] = &h9; fake_table[10] = &h10;
    HANDLE t = start_worker(fake_run);
    sp_params p = {.thread_id = worker_id, .tib = worker_tib, .run_address = (uintptr_t)(void *)asm_run + 1,
                   .slot_return = use_register ? (uintptr_t)asm_slot_return : 0, .slot_register = use_register ? 3 : -1,
                   .interval_us = 500, .depth = 32};
    assert(!simprof_start(&p));
    assert(simprof_start(&p) && !strcmp(simprof_start(&p), "sampler already running"));
    Sleep(4000);
    AcquireSRWLockExclusive(&sp.lock);
    sp_window w = sp.win;
    uint32_t leaf = fn_self(id_of_fn(leaf_work)), universe = fn_incl(id_of_fn(universe_update));
    uint32_t component_self = fn_self(id_of_fn(component_move)), ext = fn_self(SP_EXT_CODE);
    uint32_t run_incl = fn_incl(id_of_fn(asm_run)), ui_incl = fn_incl(id_of_fn(ui_move));
    ReleaseSRWLockExclusive(&sp.lock);
    double n = (double)w.samples, all = (double)truth[T_ALL];
    assert(w.samples > 2000 && !w.failures && !w.dropped_fn && !w.dropped_fold);
    struct { const char *name; unsigned phase; int truth; } rows[] = {
        {"idle", SP_PH_IDLE, T_IDLE}, {use_register ? "slot1" : "frame(slot1)", use_register ? 1 : SP_PH_FRAME, T_SLOT1},
        {"slot3", 3, T_SLOT3},
        {"slot6", 6, T_SLOT6}, {"slot9", 9, T_SLOT9}, {"lost(slot10)", SP_PH_LOST, T_SLOT10}};
    printf("%s: samples=%llu rate=%.0f/s depth=%.1f scanned=%llu truncated=%llu suspend_mean=%.1fus max=%.1fus\n",
           use_register ? "slot register" : "callee match", (unsigned long long)w.samples, n / 4.0, (double)w.frames / n, (unsigned long long)w.scanned,
           (unsigned long long)w.truncated, (double)w.suspend_ticks * 1e6 / qpf / n, (double)w.suspend_max * 1e6 / qpf);
    for (unsigned i = 0; i < SP_PHASES; ++i)
        if (w.phase_run[i] + w.phase_wait[i]) printf(" %s=%llu/%llu", sp_phase_names[i],
            (unsigned long long)w.phase_run[i], (unsigned long long)w.phase_wait[i]);
    printf("\n");
    for (unsigned i = 0; i < sizeof(rows) / sizeof(rows[0]); ++i) {
        double sampled = 100.0 * (double)(w.phase_run[rows[i].phase] + w.phase_wait[rows[i].phase]) / n;
        double expected = 100.0 * (double)truth[rows[i].truth] / all;
        printf("  %-13s sampled %5.1f%%  measured %5.1f%%  wait %5.1f%%\n", rows[i].name, sampled, expected,
               100.0 * (double)w.phase_wait[rows[i].phase] / n);
        assert(fabs(sampled - expected) < 4.0);
    }
    double slot3 = 100.0 * (double)w.phase_run[3] / n;
    printf("  self leaf_work %u, self component_move %u, incl universe_update %u, self [ext] %u, incl ui_move %u, incl run %u\n",
           leaf, component_self, universe, ext, ui_incl, run_incl);
    assert(fabs(100.0 * universe / n - slot3) < 1.0);          /* inclusive parent == its slot */
    assert(100.0 * leaf / n > slot3 + 4.0);                     /* leaf self: slot3 + slot1 + slot10 work */
    assert(component_self < w.samples / 50);                    /* recursion frames have little self time */
    assert(w.phase_wait[9] > 0.8 * (double)(w.phase_run[9] + w.phase_wait[9]));
    assert(w.phase_wait[SP_PH_IDLE] > 0.9 * (double)(w.phase_run[SP_PH_IDLE] + w.phase_wait[SP_PH_IDLE]));
    assert(w.phase_wait[3] < w.phase_run[3] / 20);
    assert(ext > 0.8 * (double)w.phase_run[6] && w.scanned >= ext);  /* JIT leaf recovered by the scan */
    assert(fabs((double)ui_incl - (double)w.phase_run[6]) < 0.05 * n);
    assert(w.truncated > 0.8 * (double)w.phase_run[SP_PH_LOST]);
    assert(run_incl > 0.97 * (double)(w.samples - w.phase_run[SP_PH_LOST]));
    assert(w.phase_run[SP_PH_LOST_OUT] + w.phase_wait[SP_PH_LOST_OUT] < w.samples / 100); /* fake_run outside asm_run */

    sp_report rep;
    assert(simprof_report(&rep));
    printf("  phases: %s\n  self: %.160s...\n  incl: %.160s...\n", rep.phases, rep.self_top, rep.incl_top);
    char name[40];
    sp_format_id(id_of_fn(universe_update), name, sizeof(name));
    assert(strstr(rep.incl_top, name) && strstr(rep.phases, "slot3=") && strstr(rep.phases, "idle="));
    assert(strstr(rep.self_top, "[ext]") && strstr(rep.self_top, "ntdll!NtWaitForSingleObject"));
    assert(rep.overhead_pct > 0.0 && rep.fold_pending > 0);
    char line[1024];
    uint64_t folded = 0;
    unsigned lines = 0, slot3_lines = 0, trunc_lines = 0;
    while (simprof_next_fold(line, sizeof(line))) {
        ++lines;
        const char *count = strrchr(line, ' ');
        assert(count);
        folded += strtoull(count + 1, NULL, 10);
        if (!strncmp(line, "slot3;", 6) && strstr(line, name)) ++slot3_lines;
        if (!strncmp(line, "lost;[truncated];", 17)) ++trunc_lines;
    }
    assert(lines == rep.fold_pending && slot3_lines && trunc_lines);
    assert(folded == rep.samples - rep.loading);
    assert(!simprof_next_fold(line, sizeof(line)));
    simprof_stop();
    simprof_stop();
    assert(!sp.thread && !sp.tables && !sp.module_count);
    assert_not_suspended(t);
    stop_worker(t);
}

static BOOL WINAPI failing_context(HANDLE t, CONTEXT *c) { (void)t; (void)c; SetLastError(5); return FALSE; }
static DWORD WINAPI failing_suspend(HANDLE t) { (void)t; SetLastError(6); return (DWORD)-1; }

__attribute__((noinline)) static DWORD WINAPI busy_worker(LPVOID unused) {
    (void)unused;
    worker_id = GetCurrentThreadId();
    worker_tib = (const NT_TIB *)NtCurrentTeb();
    InterlockedExchange(&worker_ready, 1);
    while (!worker_stop) leaf_work(100);
    return 0;
}

static void test_failure_disables(BOOL (WINAPI *context)(HANDLE, CONTEXT *),
                                  DWORD (WINAPI *suspend)(HANDLE), const char *reason) {
    HANDLE t = start_worker(busy_worker);
    sp_get_context = context; sp_suspend = suspend;
    sp_params p = {.thread_id = worker_id, .tib = worker_tib, .slot_register = -1, .interval_us = 1000, .depth = 24};
    assert(!simprof_start(&p));
    for (int i = 0; i < 400 && sp.state == 1; ++i) Sleep(10);
    assert(sp.state == 2 && sp.reason && !strncmp(sp.reason, reason, strlen(reason)));
    assert(WaitForSingleObject(sp.thread, 2000) == WAIT_OBJECT_0); /* the thread exits by itself */
    assert_not_suspended(t);
    printf("  disabled: %s\n", sp.reason);
    simprof_stop();
    sp_get_context = GetThreadContext; sp_suspend = SuspendThread;
    stop_worker(t);
}

static void test_parameters(void) {
    NT_TIB *tib = (NT_TIB *)NtCurrentTeb();
    sp_params p = {.thread_id = GetCurrentThreadId(), .tib = tib, .slot_register = -1, .interval_us = 400, .depth = 24};
    assert(!strcmp(simprof_start(&p), "invalid interval"));
    p.interval_us = 1000; p.depth = 4;
    assert(!strcmp(simprof_start(&p), "invalid depth"));
    p.depth = 24; p.run_address = 0x10;
    assert(!strcmp(simprof_start(&p), "run address outside main image"));
    assert(!sp.thread && !sp.tables && !sp.module_count);
    p.run_address = 0; p.thread_id = 0xfffffff0u;
    assert(!strcmp(simprof_start(&p), "OpenThread failed"));
    assert(!sp.tables && !sp.module_count);
}

static double timed_work(unsigned units) {
    int64_t t = qpc();
    for (unsigned u = 0; u < units; ++u) for (unsigned i = 0; i < 200000; ++i) sink += (uint64_t)i * u;
    return (double)(qpc() - t) / (double)qpf;
}

static volatile LONG overhead_go, overhead_done;
static double overhead_result;
static unsigned overhead_units;
static DWORD WINAPI overhead_worker(LPVOID unused) {
    (void)unused;
    worker_id = GetCurrentThreadId();
    worker_tib = (const NT_TIB *)NtCurrentTeb();
    InterlockedExchange(&worker_ready, 1);
    for (;;) {
        while (!overhead_go && !worker_stop) Sleep(0);
        if (worker_stop) return 0;
        overhead_result = timed_work(overhead_units);
        InterlockedExchange(&overhead_go, 0);
        InterlockedExchange(&overhead_done, 1);
    }
}

static double run_units(void) {
    InterlockedExchange(&overhead_done, 0);
    InterlockedExchange(&overhead_go, 1);
    while (!overhead_done) Sleep(1);
    return overhead_result;
}

static int compare(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y;
}

static void test_overhead(unsigned interval_us, unsigned depth) {
    HANDLE t = start_worker(overhead_worker);
    overhead_units = 1;
    double one = run_units();
    overhead_units = (unsigned)(0.5 / one) + 1; /* ~0.5 s per measurement */
    double base[7], sampled[7], fraction = 0, min_fraction = 1e9, mean_us = 0, max_us = 0, walk_us = 0, rate = 0,
           depth_mean = 0;
    for (int r = 0; r < 7; ++r) {
        base[r] = run_units();
        sp_params p = {.thread_id = worker_id, .tib = worker_tib, .slot_register = -1,
                       .interval_us = interval_us, .depth = depth};
        assert(!simprof_start(&p));
        sampled[r] = run_units();
        sp_report rep;
        simprof_report(&rep);
        fraction += rep.overhead_pct / 7; mean_us += rep.suspend_us_mean / 7;
        if (rep.overhead_pct < min_fraction) min_fraction = rep.overhead_pct;
        walk_us += rep.walk_us_mean / 7; rate += rep.rate_hz / 7; depth_mean += rep.depth_mean / 7;
        if (rep.suspend_us_max > max_us) max_us = rep.suspend_us_max;
        simprof_stop();
    }
    qsort(base, 7, sizeof(double), compare);
    qsort(sampled, 7, sizeof(double), compare);
    double slowdown = 100.0 * (sampled[3] - base[3]) / base[3];
    printf("  interval=%uus depth<=%u: %.0f samples/s, target suspended %.2f%% of wall time "
           "(lowest of 7 %.2f%%; mean %.1fus incl. walk %.1fus over %.1f frames, max %.1fus); "
           "throughput slowdown %.2f%% (median of 7)\n",
           interval_us, depth, rate, fraction, min_fraction, mean_us, walk_us, depth_mean, max_us, slowdown);
    /* The profiler's own cost is the lowest overhead of the repetitions: on a busy PC other processes delay the
       sampler thread and inflate the rest. X4LAN_PERF_STRICT=1 also bounds the mean (quiet PC only). */
    if (interval_us >= 1000) {
        assert(min_fraction < 3.0);
        if (getenv("X4LAN_PERF_STRICT")) assert(fraction < 3.0);
    }
    assert(slowdown < 15.0);
    InterlockedExchange(&worker_stop, 1);
    WaitForSingleObject(t, 5000);
    CloseHandle(t);
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    LARGE_INTEGER f; QueryPerformanceFrequency(&f); qpf = f.QuadPart;
    idle_timer = CreateWaitableTimerExW(NULL, NULL, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    sound_timer = CreateWaitableTimerExW(NULL, NULL, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    assert(idle_timer && sound_timer);
    make_jit();
    test_parameters();
    puts("PASS: simprof parameter validation and cleanup on start failure");
    test_attribution(1);
    test_attribution(0);
    puts("PASS: simprof attribution (slot register and callee fallback, idle wait, JIT leaf scan, truncation, "
         "self/inclusive, folds) and clean stop");
    test_failure_disables(failing_context, SuspendThread, "GetThreadContext failed");
    test_failure_disables(GetThreadContext, failing_suspend, "SuspendThread failed");
    puts("PASS: simprof backs off, disables with a reason, never leaves the target suspended");
    test_overhead(1000, 32);
    test_overhead(500, 64);
    test_overhead(2000, 32);
    puts("PASS: simprof overhead below 3% of wall time at 1 ms sampling");
    return 0;
}
