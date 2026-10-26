/* Build-pinned redirection of one rel32 CALL site (plain C, Windows x64; physics agent).
 *
 * pk_callsite_install(c, site, expected_target, detour): `site` must hold E8 <rel32> whose target is
 * `expected_target` (the caller verifies the surrounding code hash first). A near thunk
 * (`jmp qword [rip+0]` + absolute detour address) is allocated within +-2 GB of the site, and the
 * call's rel32 is redirected to it. When the rel32 field is 4-byte aligned it is swapped with one
 * locked compare-exchange, which is safe while other threads execute the code; otherwise the caller must
 * guarantee that nobody executes the site during install/remove (pk_callsite_install refuses unless
 * `quiescent` is set). Removal compare-exchanges the original rel32 back only if the site still holds
 * ours. Thunk pages are never freed (a thread may still be inside), the module must be pinned.
 *
 * The detour has the original's signature and calls `c->target` (the original function). */
#ifndef PHYSICS_PATCH_H
#define PHYSICS_PATCH_H
#include <stdint.h>
#include <string.h>

typedef struct {
    unsigned char *site;       /* the E8 byte */
    unsigned char *target;     /* original callee */
    unsigned char *thunk;
    int32_t orig_rel, new_rel;
    int active;
} pk_callsite;

#define PK_THUNK_SIZE 16
#define PK_GRANULE 0x10000ull
#define PK_REACH 0x7ff00000ll

static unsigned char *pk_thunk_page;   /* one page of thunks near the image, shared by all sites */
static unsigned pk_thunk_used;

static int pk_in_reach(const unsigned char *from_end, const unsigned char *to) {
    long long d = (long long)(to - from_end);
    return d >= -PK_REACH && d <= PK_REACH;
}

/* A committed execute page within reach of `near_addr` (below first, then above). */
static unsigned char *pk_alloc_near(const unsigned char *near_addr) {
    uintptr_t base = (uintptr_t)near_addr & ~(uintptr_t)(PK_GRANULE - 1);
    for (int dir = -1; dir <= 1; dir += 2)
        for (uintptr_t step = 1; step < (uintptr_t)(PK_REACH / (long long)PK_GRANULE) - 64; step += 1) {
            uintptr_t at = dir < 0 ? base - step * PK_GRANULE : base + step * PK_GRANULE;
            if (dir < 0 && at > base) break;   /* wrapped below zero */
            MEMORY_BASIC_INFORMATION mbi;
            if (!VirtualQuery((void *)at, &mbi, sizeof(mbi))) continue;
            if (mbi.State != MEM_FREE) {      /* skip the whole occupied region */
                uintptr_t end = (uintptr_t)mbi.BaseAddress + mbi.RegionSize;
                if (dir > 0 && end > at) step += (end - at) / PK_GRANULE;
                if (dir < 0 && (uintptr_t)mbi.AllocationBase < at)
                    step += (at - (uintptr_t)mbi.AllocationBase) / PK_GRANULE;
                continue;
            }
            void *p = VirtualAlloc((void *)at, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
            if (p) {
                if (pk_in_reach(near_addr, p) && pk_in_reach((const unsigned char *)p + 4096, near_addr)) return p;
                VirtualFree(p, 0, MEM_RELEASE);
            }
        }
    return NULL;
}

static unsigned char *pk_new_thunk(const unsigned char *site, void *detour) {
    if (!pk_thunk_page || pk_thunk_used + PK_THUNK_SIZE > 4096 || !pk_in_reach(site + 5, pk_thunk_page) ||
        !pk_in_reach(site + 5, pk_thunk_page + 4096)) {
        pk_thunk_page = pk_alloc_near(site);
        pk_thunk_used = 0;
        if (!pk_thunk_page) return NULL;
    }
    unsigned char *t = pk_thunk_page + pk_thunk_used;
    static const unsigned char jmp[6] = {0xff, 0x25, 0, 0, 0, 0};   /* jmp qword ptr [rip+0] */
    memcpy(t, jmp, sizeof(jmp));
    uint64_t target = (uint64_t)(uintptr_t)detour;
    memcpy(t + 6, &target, 8);
    t[14] = t[15] = 0xcc;
    pk_thunk_used += PK_THUNK_SIZE;
    FlushInstructionCache(GetCurrentProcess(), t, PK_THUNK_SIZE);
    return t;
}

static int pk_write_rel(unsigned char *field, int32_t expected, int32_t value, int quiescent) {
    DWORD old, ignored;
    if (!VirtualProtect(field, 4, PAGE_EXECUTE_READWRITE, &old)) return 0;
    int ok;
    if (((uintptr_t)field & 3) == 0)
        ok = InterlockedCompareExchange((volatile LONG *)field, (LONG)value, (LONG)expected) == (LONG)expected;
    else if (quiescent) {
        int32_t now;
        memcpy(&now, field, 4);
        ok = now == expected;
        if (ok) memcpy(field, &value, 4);
    } else ok = 0;
    VirtualProtect(field, 4, old, &ignored);
    FlushInstructionCache(GetCurrentProcess(), field - 1, 5);
    return ok;
}

/* NULL on success, else the reason. */
static const char *pk_callsite_install(pk_callsite *c, unsigned char *site, unsigned char *expected_target,
                                       void *detour, int quiescent) {
    if (c->active) return "call site already redirected";
    if (site[0] != 0xe8) return "call site is not a rel32 call";
    int32_t rel;
    memcpy(&rel, site + 1, 4);
    if (site + 5 + rel != expected_target) return "call site target differs";
    if (((uintptr_t)(site + 1) & 3) && !quiescent) return "call site not quiescent";
    unsigned char *thunk = pk_new_thunk(site, detour);
    if (!thunk) return "no thunk memory within reach";
    long long d = (long long)(thunk - (site + 5));
    if (d < INT32_MIN || d > INT32_MAX) return "thunk out of reach";
    c->site = site;
    c->target = expected_target;
    c->thunk = thunk;
    c->orig_rel = rel;
    c->new_rel = (int32_t)d;
    if (!pk_write_rel(site + 1, rel, c->new_rel, quiescent)) return "call site write failed";
    c->active = 1;
    return NULL;
}

/* 1 when the original call is back (or was never redirected). */
static int pk_callsite_remove(pk_callsite *c, int quiescent) {
    if (!c->active) return 1;
    if (!pk_write_rel(c->site + 1, c->new_rel, c->orig_rel, quiescent)) return 0;
    c->active = 0;
    return 1;
}
#endif
