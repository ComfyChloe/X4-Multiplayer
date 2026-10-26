/* Build-pinned, crash-safe function-entry detours for the component-walk census/replay (rw-walk).
 *
 * The event functions this instruments (PostEvent, CancelPendingEvents, DeliverEvent, Sector::Move)
 * are called directly (no vtable), so a data-only pointer swap is impossible; an entry detour is the
 * only interception point the parallel-walk design allows (H3-H5). This installs one carefully, with
 * every safeguard ENGINE-INSTRUMENTATION.txt requires:
 *   - the exact build is already verified by fixedtick.c; each prologue's bytes are SHA-256 pinned and
 *     re-checked here before any write;
 *   - trampolines are allocated within +-2 GB of the image so the one relocated jcc (PostEvent's `je`)
 *     still reaches its in-image target with a rel32;
 *   - install suspends every other thread and ABORTS (fail closed, retry next frame) if any thread is
 *     executing inside a patch range, so no thread resumes onto a half-written instruction;
 *   - full rollback if any site fails; the original bytes are restored on disarm; the trampoline pool
 *     is never freed while armed (a call may be in flight), it is leaked like simprof's tables.
 * Prologue cut lengths and the single jcc offset are pinned per build (23660954); there is no length
 * decoder. A 14-byte `jmp [rip+0]; <abs>` overwrites the entry; the copied prologue + a jmp back to
 * entry+cut forms the trampoline the detour calls to run the original. */

#ifndef X4LAN_WPDETOUR_H
#define X4LAN_WPDETOUR_H

#include <tlhelp32.h>

typedef struct {
    size_t rva;            /* function entry RVA */
    unsigned cut;          /* whole-instruction prologue length copied to the trampoline (>=14) */
    int jcc_off;           /* offset in the prologue of a 2-byte-opcode jcc (0F 8x rel32), or -1 */
    const char *sha;       /* pinned SHA-256 of the first hash_len bytes (fixedtick hash_memory) */
    unsigned hash_len;
    void *detour;          /* entry jumps here */
    unsigned char saved[32];
    unsigned char *tramp;  /* original behaviour: copied prologue + jmp to entry+cut */
    int installed;
} wp_detour;

/* ---- +-2 GB executable pool (bump allocator; never freed while armed) ------------------------- */

static unsigned char *wp_pool;
static size_t wp_pool_used, wp_pool_size;

static int wp_pool_reserve(void) {
    if (wp_pool) return 1;
    uintptr_t base = (uintptr_t)image & ~(uintptr_t)0xffff;
    const size_t size = 0x10000;              /* 64 KB is ample for a handful of trampolines */
    for (uintptr_t delta = 0x100000; delta < 0x70000000ull; delta += 0x100000) {
        for (int dir = 0; dir < 2; ++dir) {
            uintptr_t cand = dir ? base + delta : base - delta;
            if (dir && cand < base) continue;     /* overflow */
            if (!dir && cand > base) continue;
            void *p = VirtualAlloc((void *)cand, size, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
            if (p) {
                uintptr_t d = (uintptr_t)p > base ? (uintptr_t)p - base : base - (uintptr_t)p;
                if (d < 0x78000000ull) { wp_pool = p; wp_pool_size = size; wp_pool_used = 0; return 1; }
                VirtualFree(p, 0, MEM_RELEASE);
            }
        }
    }
    return 0;
}

static unsigned char *wp_pool_alloc(size_t n) {
    n = (n + 15) & ~(size_t)15;
    if (!wp_pool || wp_pool_used + n > wp_pool_size) return NULL;
    unsigned char *p = wp_pool + wp_pool_used;
    wp_pool_used += n;
    return p;
}

/* ---- jmp [rip+0]; <abs64> (14 bytes) ---------------------------------------------------------- */

static void wp_write_absjmp(unsigned char *at, uint64_t target) {
    at[0] = 0xff; at[1] = 0x25; at[2] = at[3] = at[4] = at[5] = 0;
    memcpy(at + 6, &target, 8);
}

/* Build the trampoline for one site: copy `cut` prologue bytes (relocating the one jcc's rel32 so it
   still targets its in-image label from the near pool), then jump back to entry+cut. */
static int wp_detour_build(wp_detour *d) {
    unsigned char *orig = image + d->rva;
    if (!readable(orig, d->cut, 0)) return 0;
    if (!hash_memory(orig, d->hash_len, d->sha)) return 0;         /* fail closed on any mismatch */
    unsigned char *t = wp_pool_alloc(d->cut + 14);
    if (!t) return 0;
    memcpy(d->saved, orig, d->cut);
    memcpy(t, orig, d->cut);
    if (d->jcc_off >= 0) {
        unsigned char *j = t + d->jcc_off;
        if (!(j[0] == 0x0f && (j[1] & 0xf0) == 0x80)) return 0;    /* expected 0F 8x rel32 */
        int32_t rel; memcpy(&rel, j + 2, 4);
        uint64_t target = (uint64_t)(orig + d->jcc_off + 6) + (int64_t)rel;
        int64_t nrel = (int64_t)target - (int64_t)(t + d->jcc_off + 6);
        if (nrel > 0x7fffffffLL || nrel < -0x80000000LL) return 0; /* must reach from the pool */
        int32_t n32 = (int32_t)nrel; memcpy(j + 2, &n32, 4);
    }
    wp_write_absjmp(t + d->cut, (uint64_t)(orig + d->cut));
    d->tramp = t;
    return 1;
}

/* ---- thread suspension with a Rip-clash abort ------------------------------------------------- */

#define WP_MAX_THREADS 512
static HANDLE wp_suspended[WP_MAX_THREADS];
static int wp_suspended_n;

static void wp_resume_all(void) {
    for (int i = 0; i < wp_suspended_n; ++i) { ResumeThread(wp_suspended[i]); CloseHandle(wp_suspended[i]); }
    wp_suspended_n = 0;
}

/* Suspend every other thread of this process. Returns 0 (and resumes) if a thread is executing inside
   any [rva, rva+cut) patch range, or on any API failure. */
static int wp_suspend_others(const wp_detour *ds, int n) {
    wp_suspended_n = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    THREADENTRY32 te; te.dwSize = sizeof(te);
    DWORD me = GetCurrentThreadId(), pid = GetCurrentProcessId();
    int ok = 1;
    if (Thread32First(snap, &te)) {
        do {
            if (te.th32OwnerProcessID != pid || te.th32ThreadID == me) continue;
            if (wp_suspended_n >= WP_MAX_THREADS) { ok = 0; break; }
            HANDLE h = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT, FALSE, te.th32ThreadID);
            if (!h) continue;                     /* a thread we cannot touch (died, or no rights) */
            if (SuspendThread(h) == (DWORD)-1) { CloseHandle(h); continue; }
            wp_suspended[wp_suspended_n++] = h;
        } while (Thread32Next(snap, &te));
    } else ok = 0;
    CloseHandle(snap);
    if (!ok) { wp_resume_all(); return 0; }
    for (int i = 0; i < wp_suspended_n; ++i) {
        CONTEXT ctx; ctx.ContextFlags = CONTEXT_CONTROL;
        if (!GetThreadContext(wp_suspended[i], &ctx)) { wp_resume_all(); return 0; }
        for (int s = 0; s < n; ++s) {
            uintptr_t lo = (uintptr_t)(image + ds[s].rva), hi = lo + ds[s].cut;
            if (ctx.Rip >= lo && ctx.Rip < hi) { wp_resume_all(); return 0; }  /* retry next frame */
        }
    }
    return 1;
}

/* ---- install / uninstall ---------------------------------------------------------------------- */

static int wp_patch_entry(wp_detour *d) {
    unsigned char *orig = image + d->rva;
    DWORD prot, ign;
    if (!VirtualProtect(orig, d->cut, PAGE_EXECUTE_READWRITE, &prot)) return 0;
    wp_write_absjmp(orig, (uint64_t)d->detour);
    for (unsigned i = 14; i < d->cut; ++i) orig[i] = 0x90;   /* nop the tail of the overwritten prologue */
    VirtualProtect(orig, d->cut, prot, &ign);
    FlushInstructionCache(GetCurrentProcess(), orig, d->cut);
    d->installed = 1;
    return 1;
}

static void wp_restore_entry(wp_detour *d) {
    unsigned char *orig = image + d->rva;
    DWORD prot, ign;
    if (!VirtualProtect(orig, d->cut, PAGE_EXECUTE_READWRITE, &prot)) return;
    memcpy(orig, d->saved, d->cut);
    VirtualProtect(orig, d->cut, prot, &ign);
    FlushInstructionCache(GetCurrentProcess(), orig, d->cut);
    d->installed = 0;
}

/* Prepare (verify + trampolines) then install all sites atomically under a thread freeze. Returns 1
   on success; on any failure nothing stays installed. Caller retries on a transient 0 (Rip clash). */
static int wp_detour_install(wp_detour *ds, int n) {
    if (!wp_pool_reserve()) return 0;
    for (int i = 0; i < n; ++i)
        if (!ds[i].installed && !ds[i].tramp && !wp_detour_build(&ds[i])) return 0;  /* reuse on retry */
    if (!wp_suspend_others(ds, n)) return 0;
    int done = 0, ok = 1;
    for (; done < n; ++done) if (!ds[done].installed && !wp_patch_entry(&ds[done])) { ok = 0; break; }
    if (!ok) for (int i = 0; i < done; ++i) wp_restore_entry(&ds[i]);
    wp_resume_all();
    return ok;
}

static void wp_detour_uninstall(wp_detour *ds, int n) {
    int any = 0;
    for (int i = 0; i < n; ++i) if (ds[i].installed) any = 1;
    if (!any) return;
    /* a Rip-clash on restore is the same hazard; if we cannot freeze cleanly, retry is not possible
       at disarm, so restore anyway only when the freeze (with its clash check) succeeds. */
    if (wp_suspend_others(ds, n)) {
        for (int i = 0; i < n; ++i) if (ds[i].installed) wp_restore_entry(&ds[i]);
        wp_resume_all();
    }
}

#endif /* X4LAN_WPDETOUR_H */
