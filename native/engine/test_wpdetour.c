/* Offline validation of the crash-safe entry-detour installer (wpdetour.h), independent of X4: a
   self-authored function with a PostEvent-shaped prologue (test rcx,rcx; je <far>; ...) is detoured,
   called from several threads, then restored. Proves the relocating trampoline (incl. the jcc),
   the near-image pool, install/call/restore, fail-closed on a byte mismatch, and concurrency. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include "fixedtick.c"

struct lua_State { int unused; };

/* int fake(void *a, int b): returns b+1 when a!=0, else 0. 14-byte prologue holds a `je` whose
   target (0x22) is outside the stolen bytes, exactly like PostEvent's entry. */
static const unsigned char FAKE_CODE[] = {
    0x48, 0x85, 0xC9,                   /* 00: test rcx,rcx          */
    0x0F, 0x84, 0x19, 0x00, 0x00, 0x00, /* 03: je 0x22               */
    0x8D, 0x42, 0x01,                   /* 09: lea eax,[rdx+1]       */
    0x90, 0x90,                         /* 0C: nop nop  (cut = 0x0E) */
    0xC3,                               /* 0E: ret  (a!=0 -> b+1)    */
    0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC,
    0xCC, 0xCC, 0xCC,                   /* 0F..21: fill              */
    0x31, 0xC0,                         /* 22: xor eax,eax           */
    0xC3,                               /* 24: ret  (a==0 -> 0)      */
};
#define FAKE_RVA 0x2000u

static void sha_hex(const void *data, ULONG len, char out[65]) {
    BCRYPT_ALG_HANDLE alg = NULL; BCRYPT_HASH_HANDLE h = NULL; unsigned char d[32];
    assert(BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) >= 0);
    assert(BCryptCreateHash(alg, &h, NULL, 0, NULL, 0, 0) >= 0);
    assert(BCryptHashData(h, (PUCHAR)data, len, 0) >= 0);
    assert(BCryptFinishHash(h, d, sizeof(d), 0) >= 0);
    for (int i = 0; i < 32; ++i) sprintf(out + 2 * i, "%02x", d[i]);
    out[64] = 0; BCryptDestroyHash(h); BCryptCloseAlgorithmProvider(alg, 0);
}

typedef int (*fake_fn)(void *, int);
static wp_detour g_d;
static volatile LONG g_detour_calls;

static int my_detour(void *a, int b) {
    InterlockedIncrement(&g_detour_calls);
    return ((fake_fn)g_d.tramp)(a, b);
}

static volatile LONG g_worker_stop, g_worker_bad;
static DWORD WINAPI worker(LPVOID p) {
    fake_fn f = (fake_fn)p;
    while (!g_worker_stop) {
        if (f((void *)1, 41) != 42) InterlockedIncrement(&g_worker_bad);
        if (f((void *)0, 41) != 0) InterlockedIncrement(&g_worker_bad);
    }
    return 0;
}

int main(void) {
    image = VirtualAlloc(NULL, X4_IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    assert(image);
    memcpy(image + FAKE_RVA, FAKE_CODE, sizeof(FAKE_CODE));
    FlushInstructionCache(GetCurrentProcess(), image + FAKE_RVA, sizeof(FAKE_CODE));
    fake_fn f = (fake_fn)(image + FAKE_RVA);
    assert(f((void *)1, 41) == 42 && f((void *)0, 7) == 0);     /* baseline */

    static char sha[65];
    sha_hex(image + FAKE_RVA, 32, sha);

    /* fail closed: a wrong hash refuses to build/install */
    g_d = (wp_detour){ FAKE_RVA, 14, 3, "00" "00000000000000000000000000000000000000000000000000000000000000",
                       32, (void *)my_detour, {0}, NULL, 0 };
    assert(!wp_detour_install(&g_d, 1) && !g_d.installed);

    /* real install */
    g_d = (wp_detour){ FAKE_RVA, 14, 3, sha, 32, (void *)my_detour, {0}, NULL, 0 };
    assert(wp_detour_install(&g_d, 1) && g_d.installed && g_d.tramp);
    assert(*(unsigned char *)(image + FAKE_RVA) == 0xff);       /* entry is now jmp [rip+0] */

    g_detour_calls = 0;
    assert(f((void *)1, 41) == 42);     /* detour -> trampoline -> original, a!=0 */
    assert(f((void *)0, 41) == 0);      /* a==0 path through the relocated je      */
    assert(g_detour_calls == 2);

    /* concurrency: hammer the detoured function from workers while the main thread also calls it */
    g_worker_stop = g_worker_bad = 0;
    HANDLE th[3];
    for (int i = 0; i < 3; ++i) th[i] = CreateThread(NULL, 0, worker, (void *)f, 0, NULL);
    for (int i = 0; i < 200000; ++i) { if (f((void *)1, i) != i + 1) { g_worker_bad = 1; break; } }
    g_worker_stop = 1;
    WaitForSingleObject(th[0], 5000); WaitForSingleObject(th[1], 5000); WaitForSingleObject(th[2], 5000);
    for (int i = 0; i < 3; ++i) CloseHandle(th[i]);
    assert(g_worker_bad == 0);

    /* restore: original bytes back, calls no longer hit the detour */
    wp_detour_uninstall(&g_d, 1);
    assert(!g_d.installed);
    assert(memcmp(image + FAKE_RVA, FAKE_CODE, 14) == 0);
    LONG before = g_detour_calls;
    assert(f((void *)1, 10) == 11 && f((void *)0, 10) == 0);
    assert(g_detour_calls == before);

    VirtualFree(image, 0, MEM_RELEASE);
    puts("PASS: wpdetour relocating trampoline (incl jcc), near-image pool, install/call/restore");
    puts("PASS: wpdetour fail-closed on byte mismatch; detour transparent under 4-thread concurrency");
    return 0;
}
