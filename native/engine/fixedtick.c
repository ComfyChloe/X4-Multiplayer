#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <math.h>
#include <stdint.h>
#include <string.h>
#include "fixedtick_signatures.h"

typedef struct lua_State lua_State;
typedef int (*lua_CFunction)(lua_State *);
#define API(ret, name, args) static ret (*p_##name) args
API(int, lua_gettop, (lua_State *));
API(int, lua_type, (lua_State *, int));
API(const char *, lua_tolstring, (lua_State *, int, size_t *));
API(void, lua_settop, (lua_State *, int));
API(int, lua_pushthread, (lua_State *));
API(void, lua_pushvalue, (lua_State *, int));
API(void, lua_pushnil, (lua_State *));
API(void, lua_pushboolean, (lua_State *, int));
API(void, lua_pushnumber, (lua_State *, double));
API(void, lua_pushlstring, (lua_State *, const char *, size_t));
API(void, lua_pushcclosure, (lua_State *, lua_CFunction, int));
API(void, lua_getfield, (lua_State *, int, const char *));
API(void, lua_setfield, (lua_State *, int, const char *));
API(void, lua_createtable, (lua_State *, int, int));
API(void *, lua_newuserdata, (lua_State *, size_t));
API(int, lua_setmetatable, (lua_State *, int));
API(int, luaL_newmetatable, (lua_State *, const char *));
API(void *, luaL_checkudata, (lua_State *, int, const char *));

#define REGISTRY (-10000)
#define META "x4lan.fixedtick.owner.v1"
#define VBI_RVA 0x6cf1570u
#define GAME_TIME_RVA 0x6b60e38u
#define REAL_TIME_RVA 0x6b60e40u
#define SPEED_RVA 0x6b60e60u
#define SLOT 5
/* Stage 1: FrameTick's movie-capture override (je at 0xfa3302) becomes a two-byte NOP,
   so dt is always loaded from g_movieCaptureFrameTime, which holds exactly 1/60. */
#define DT_BRANCH_RVA 0xfa3302u
#define CAPTURE_ACTIVE_RVA 0x6adfc58u
#define CAPTURE_DT_RVA 0x6adfc60u
#define LIMIT_ENABLED_RVA 0x6adfbb0u
#define LIMIT_FPS_RVA 0x6adfbb4u
#define FIXED_HZ 60u
/* Stage 2: per frame the next dt is k/60 (k = whole ticks of accumulated wall time,
   0..MAX_CATCHUP) and Jolt's collision-step count is k, so physics advances in
   exact 1/60 s sub-steps. The frame QPC is the value FrameTick stored this frame. */
#define FRAME_QPC_RVA 0x6b60e80u
#define COLLISION_STEPS_PTR_RVA 0x6cf1510u
#define MAX_CATCHUP 4u
/* Stage 2 adjustment: after a slow frame (wall time above 1.5 ticks), and while the ticks
   deferred by that cap are caught up, at most k_cap ticks run per frame. Capped ticks stay
   in the accumulator (deferred, still bounded by MAX_CATCHUP), so catch-up after a hitch
   is spread over frames instead of making the next frame slow too. */
#define DEFAULT_K_CAP 2u
#define SLOW_FRAME_TICKS 1.5
#define LUA_TNIL 0
#define LUA_TSTRING 4
static const unsigned short branch_original = 0x0874; /* je +8 */
static const unsigned short branch_patched = 0x9066;  /* 66 90 */

/* Stage 3: the main loop's WaitForSingleObject(g_semSimDone, 16) at 0x12ae895 is an
   `ff 15 disp32` call through the IAT slot 0x1bc14b0. Only its displacement is redirected,
   to an 8-byte slot in a module-owned page within +-2 GB holding wait_or_represent; the
   shared IAT entry is never written. The displacement (0x12ae897..0x12ae89a) straddles an
   8-byte boundary, so it is replaced by one 16-byte compare-exchange of the aligned window
   0x12ae890..0x12ae89f, which lies inside the verified main_loop_wait_call signature. */
#define WAIT_WINDOW_RVA 0x12ae890u
#define WAIT_DISP_RVA 0x12ae897u
#define WAIT_NEXT_RVA 0x12ae89bu
#define WAIT_IAT_RVA 0x1bc14b0u
#define SEM_SIM_DONE_RVA 0x3d79108u
#define SIM_IN_FLIGHT_RVA 0x31c20ccu
#define WINDOW_SUSPENDED_RVA 0x38f52eeu
#define APP_STATE_RVA 0x6adf9fcu
#define MOVE_WORKER_RVA 0x38f4ee8u
#define FRAME_STATE_PTR_RVA 0x6cf1578u
#define FS_THREADED 0x1e4u
#define FS_QUEUE_COUNT 0x1e8u
#define FIRST_FRAME_RVA 0x31c2070u
#define GFX_PENDING_RVA 0x6adfbc0u
#define STREAMLINE_PTR_RVA 0x6cf1908u
#define FFX_PTR_RVA 0x6cf2bc8u
#define FFX_FRAME_GEN 0x25e71au
#define TEMP_PAUSE_RVA 0x6b60e70u
#define READ_INDEX_RVA 0x6b66280u
/* FrameTick's threaded present tail (0xfa344f..0xfa3467, signature frame_tick_present_tail):
   BeginThreadedRenderFrame(); ok = RenderPreviousFrame(fs, 1); PresentAndFrameLimit(fs, ok, 0)
   with fs = FrameTick's first argument = *(0x6cf1578) at the main-loop call 0x12ae9cb. */
#define BEGIN_RENDER_RVA 0xfa23a0u
#define RENDER_PREVIOUS_RVA 0xf9ff40u
#define PRESENT_RVA 0xfa2ba0u

typedef struct {
    uint64_t frames;
    uint64_t loading_skips;
    uint64_t paused_frames;
    uint64_t game_time_regressions;
    int64_t previous_qpc;
    double previous_game_time;
    double previous_real_time;
    double last_wall_dt;
    double max_wall_dt;
    double last_game_dt;
    double last_real_dt;
    double game_time;
    double real_time;
    double speed;
    double wall_elapsed;
    double game_elapsed;
    uint64_t fixed_frames;
    const char *reason;
    int armed;
    int fixed;
    int catchup;
    int64_t last_frame_qpc;
    double accumulator;
    uint64_t steps_total;
    uint64_t zero_step_frames;
    uint64_t multi_step_frames;
    uint64_t dropped_ticks;
    unsigned last_steps;
    unsigned k_cap;
    int capped_last;
    uint64_t capped_ticks;
    uint64_t slow_frames;
    int stage3;
} owner;
typedef struct { void **vtable; } handler;
static unsigned char *image;
static owner *current;
static void **registered_table;
static DWORD owner_thread;
static int64_t qpc_frequency;
static int patched;
static double saved_capture_dt;
static int *collision_steps;
static int saved_collision_steps;

typedef DWORD (WINAPI *wait_fn)(HANDLE, DWORD);
typedef void (*begin_render_fn)(void);
typedef unsigned char (*render_previous_fn)(void *frame_state, unsigned char threaded, uint64_t unused);
typedef void (*present_fn)(void *frame_state, unsigned ok, unsigned zero);
static begin_render_fn begin_render;
static render_previous_fn render_previous;
static present_fn present_frame;

enum {
    G_THREAD, G_HANDLE, G_PATCH, G_NOT_IN_FLIGHT, G_SUSPENDED, G_LOADING, G_NO_MOVE_WORKER,
    G_FIRST_FRAME, G_NOT_THREADED, G_GRAPHICS_PENDING, G_MOVIE_CAPTURE, G_STREAMLINE_REFLEX,
    G_DLSS_FG, G_FSR_FG, G_SAVING, G_COUNT
};
static const char *const gate_names[G_COUNT] = {
    "thread", "handle", "patch_changed", "not_in_flight", "suspended", "loading", "no_move_worker",
    "first_frame", "not_threaded", "graphics_pending", "movie_capture", "streamline_reflex",
    "dlss_frame_generation", "fsr_frame_generation", "saving"
};

/* Module-static, not in the Lua owner: the main thread may still be inside the wrapper
   while the MoveWorker stops and collects the owner. */
typedef struct {
    int enabled;                   /* fixed60stage3 requested */
    int represent;                 /* option represent: 1 re-presents, 0 counts would_represent */
    int short_wait;                /* option shortwait: shorten the wait to the present deadline */
    unsigned char *page;           /* module-owned slot page near the image */
    int installed;
    unsigned char original[16], patched[16];
    int32_t displacement;
    volatile LONG active;          /* wrapper may act; cleared first on disarm */
    volatile LONG64 frames_since_install;
    volatile LONG main_thread;
    volatile LONG present_pending; /* a sim-done wait succeeded; FrameTick presents next */
    volatile LONG gates_ok_last;
    int64_t last_present_qpc;
    int64_t last_would_qpc;
    const char *volatile violation;
    uint64_t re_presents, would_represent, waits_ok, waits_timeout, waits_other, budget_skips;
    uint64_t gate_failures[G_COUNT];
    uint64_t index_changed, queue_count_changed, in_flight_changed;
    double max_present_gap;
} stage3_state;
static stage3_state s3;

/* Test-only stall injector: Sleep(ms) every `every` slot calls, from slot 5 (inside this
   callback) or from its own data-only handler in an empty slot (7 before the producer
   slot 10, 12 or 13 after it). Off unless stall_ms is given. */
typedef struct { unsigned ms, every, slot; uint64_t calls, injected; } stall_state;
static stall_state stall;

typedef struct {
    unsigned k_cap;
    int represent, short_wait;
    unsigned stall_ms, stall_every, stall_slot;
} options;

static BOOL WINAPI read_qpc(LARGE_INTEGER *value) { return QueryPerformanceCounter(value); }
static BOOL (WINAPI *qpc_read)(LARGE_INTEGER *) = read_qpc;

static int readable(const void *ptr, size_t length, int write) {
    MEMORY_BASIC_INFORMATION m;
    uintptr_t start = (uintptr_t)ptr, end = start + length;
    if (!ptr || end < start || !VirtualQuery(ptr, &m, sizeof(m))) return 0;
    if (m.State != MEM_COMMIT || (m.Protect & (PAGE_GUARD | PAGE_NOACCESS))) return 0;
    if (end > (uintptr_t)m.BaseAddress + m.RegionSize) return 0;
    DWORD p = m.Protect & 0xff;
    if (write) return p == PAGE_READWRITE || p == PAGE_WRITECOPY ||
                      p == PAGE_EXECUTE_READWRITE || p == PAGE_EXECUTE_WRITECOPY;
    return p == PAGE_READONLY || p == PAGE_READWRITE || p == PAGE_WRITECOPY ||
           p == PAGE_EXECUTE_READ || p == PAGE_EXECUTE_READWRITE ||
           p == PAGE_EXECUTE_WRITECOPY;
}

static int digest_matches(const unsigned char digest[32], const char *hex) {
    static const char alphabet[] = "0123456789abcdef";
    for (unsigned i = 0; i < 32; ++i)
        if (hex[2*i] != alphabet[digest[i] >> 4] ||
            hex[2*i+1] != alphabet[digest[i] & 15]) return 0;
    return 1;
}

static int hash_memory(const void *data, ULONG length, const char *expected) {
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    unsigned char digest[32];
    int ok = 0;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0) goto done;
    if (BCryptCreateHash(alg, &hash, NULL, 0, NULL, 0, 0) < 0) goto done;
    if (BCryptHashData(hash, (PUCHAR)data, length, 0) < 0) goto done;
    if (BCryptFinishHash(hash, digest, sizeof(digest), 0) < 0) goto done;
    ok = digest_matches(digest, expected);
done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    return ok;
}

static int verify_file(void) {
    WCHAR path[32768];
    DWORD n = GetModuleFileNameW(NULL, path, 32768);
    if (!n || n >= 32768) return 0;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    LARGE_INTEGER size;
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    unsigned char buffer[65536], digest[32];
    DWORD got;
    int ok = 0;
    if (!GetFileSizeEx(file, &size) || size.QuadPart != X4_FILE_SIZE) goto done;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0) goto done;
    if (BCryptCreateHash(alg, &hash, NULL, 0, NULL, 0, 0) < 0) goto done;
    for (;;) {
        if (!ReadFile(file, buffer, sizeof(buffer), &got, NULL)) goto done;
        if (!got) break;
        if (BCryptHashData(hash, buffer, got, 0) < 0) goto done;
    }
    if (BCryptFinishHash(hash, digest, sizeof(digest), 0) < 0) goto done;
    ok = digest_matches(digest, X4_FILE_SHA256);
done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    CloseHandle(file);
    return ok;
}

static int verify_checks(const x4_signature_check *checks, size_t count) {
    for (unsigned i = 0; i < count; ++i)
        if (!readable(image + checks[i].rva, checks[i].length, 0) ||
            !hash_memory(image + checks[i].rva, checks[i].length, checks[i].sha)) return 0;
    return 1;
}

static int verify_image(int stage3) {
    image = (unsigned char *)GetModuleHandleW(NULL);
    if (!readable(image, sizeof(IMAGE_DOS_HEADER), 0)) return 0;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)image;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 0 || dos->e_lfanew > 4096) return 0;
    IMAGE_NT_HEADERS64 *nt = (IMAGE_NT_HEADERS64 *)(image + dos->e_lfanew);
    if (!readable(nt, sizeof(*nt), 0) || nt->Signature != IMAGE_NT_SIGNATURE ||
        nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
        nt->FileHeader.TimeDateStamp != X4_PE_TIMESTAMP ||
        nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC ||
        nt->OptionalHeader.SizeOfImage != X4_IMAGE_SIZE || !verify_file()) return 0;
    if (!verify_checks(x4_common_checks, sizeof(x4_common_checks)/sizeof(x4_common_checks[0])) ||
        !verify_checks(x4_fixedtick_checks, sizeof(x4_fixedtick_checks)/sizeof(x4_fixedtick_checks[0]))) return 0;
    if (stage3 && !verify_checks(x4_stage3_checks, sizeof(x4_stage3_checks)/sizeof(x4_stage3_checks[0]))) return 0;
    return readable(image + VBI_RVA, sizeof(void *), 0) &&
           readable(image + GAME_TIME_RVA, sizeof(double), 0) &&
           readable(image + REAL_TIME_RVA, sizeof(double), 0) &&
           readable(image + SPEED_RVA, sizeof(double), 0);
}

static void move(handler *self);
static void stall_move(handler *self);
static void no_op(handler *self) { (void)self; }
static void *vtable[] = {(void *)no_op, (void *)move, (void *)no_op};
static handler hook = {vtable};
static void *stall_vtable[] = {(void *)no_op, (void *)stall_move, (void *)no_op};
static handler stall_hook = {stall_vtable};

static void **table_now(void) {
    return image ? *(void ***)(image + VBI_RVA) : NULL;
}

static int table_owned(void) {
    void **table = table_now();
    return table && table == registered_table && readable(table, 0x138, 1) && table[SLOT] == &hook;
}

static int write_branch(unsigned short expected, unsigned short value) {
    volatile SHORT *site = (volatile SHORT *)(image + DT_BRANCH_RVA);
    DWORD protection, ignored;
    if (!VirtualProtect((void *)site, 2, PAGE_EXECUTE_READWRITE, &protection)) return 0;
    int ok = (unsigned short)InterlockedCompareExchange16(site, (SHORT)value, (SHORT)expected) == expected;
    VirtualProtect((void *)site, 2, protection, &ignored);
    FlushInstructionCache(GetCurrentProcess(), (void *)site, 2);
    return ok;
}

static double *capture_dt(void) { return (double *)(image + CAPTURE_DT_RVA); }

static int limiter_at_fixed_rate(void) {
    unsigned fps = *(volatile unsigned *)(image + LIMIT_FPS_RVA);
    return *(volatile unsigned char *)(image + LIMIT_ENABLED_RVA) && (fps == FIXED_HZ || fps == 0);
}

static const char *fixed_guard(void) {
    if (*(volatile unsigned char *)(image + CAPTURE_ACTIVE_RVA)) return "movie capture active";
    if (!(current && current->catchup) && *(volatile double *)capture_dt() != 1.0 / FIXED_HZ)
        return "capture frame time changed";
    if (current && current->catchup) {
        if (*(volatile double *)capture_dt() != (double)current->last_steps / FIXED_HZ)
            return "capture frame time changed";
        if (!collision_steps || *collision_steps != (current->last_steps ? (int)current->last_steps : 1))
            return "collision steps changed";
    }
    if (!(current && current->catchup) && !limiter_at_fixed_rate()) return "frame-rate limit is not 60";
    if (*(volatile unsigned short *)(image + DT_BRANCH_RVA) != branch_patched) return "dt branch changed";
    return NULL;
}

static void unpatch(void) {
    if (!patched) return;
    write_branch(branch_patched, branch_original);
    double dt = *capture_dt();
    for (unsigned k = 0; k <= MAX_CATCHUP; ++k)
        if (dt == (double)k / FIXED_HZ) { *capture_dt() = saved_capture_dt; break; }
    if (collision_steps) { *collision_steps = saved_collision_steps; collision_steps = NULL; }
    patched = 0;
}

/* Called once per simulation frame after FrameTick: account this frame's wall time and
   choose the next frame's step count. One frame of lag; long-run exact. */
static void schedule_next(owner *o) {
    int64_t qpc = *(volatile int64_t *)(image + FRAME_QPC_RVA);
    double frame_wall = 0.0;
    if (o->last_frame_qpc && qpc > o->last_frame_qpc && qpc_frequency > 0) {
        frame_wall = (double)(qpc - o->last_frame_qpc) / (double)qpc_frequency;
        o->accumulator += frame_wall;
    }
    o->last_frame_qpc = qpc;
    /* A 1e-3 tick tolerance keeps steady rates (e.g. 30 fps with QPC rounding) at a
       constant step count instead of alternating k-1/k+1. */
    unsigned k = (unsigned)(o->accumulator * FIXED_HZ + 1e-3);
    if (k > MAX_CATCHUP) {
        o->dropped_ticks += k - MAX_CATCHUP;
        o->accumulator -= (double)(k - MAX_CATCHUP) / FIXED_HZ;
        k = MAX_CATCHUP;
    }
    int slow = frame_wall > SLOW_FRAME_TICKS / FIXED_HZ;
    if (slow) ++o->slow_frames;
    unsigned cap = o->k_cap ? o->k_cap : MAX_CATCHUP;
    if ((slow || o->capped_last) && k > cap) {
        o->capped_ticks += k - cap;  /* deferred: the ticks stay in the accumulator */
        k = cap;
        o->capped_last = 1;
    } else o->capped_last = 0;
    o->accumulator -= (double)k / FIXED_HZ;
    if (o->accumulator < 0.0) o->accumulator = 0.0;
    *capture_dt() = (double)k / FIXED_HZ;
    *collision_steps = k ? (int)k : 1;
    o->last_steps = k;
    o->steps_total += k;
    if (!k) ++o->zero_step_frames; else if (k > 1) ++o->multi_step_frames;
}

static void *ptr_at(uintptr_t rva) { return *(void *volatile *)(image + rva); }

/* Reserve and commit one page at a 64 KB-aligned free address near the image, searching
   downward and then upward; the caller accepts it only if a rel32 from the call reaches it. */
static unsigned char *alloc_near(unsigned char *base, size_t size) {
    const uintptr_t G = 0x10000, reach = 0x7ff00000;
    uintptr_t start = (uintptr_t)base, low = start > reach + G ? start - reach : G;
    MEMORY_BASIC_INFORMATION m;
    for (uintptr_t a = (start & ~(G - 1)) - G; a >= low && a < start;) {
        if (!VirtualQuery((void *)a, &m, sizeof(m))) break;
        if (m.State == MEM_FREE) {
            void *p = VirtualAlloc((void *)a, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
            if (p == (void *)a) return p;
            if (p) VirtualFree(p, 0, MEM_RELEASE);
            a -= G;
        } else {
            uintptr_t b = m.AllocationBase ? (uintptr_t)m.AllocationBase : (uintptr_t)m.BaseAddress;
            if (b < low + G) break;
            a = (b & ~(G - 1)) - G;
        }
    }
    for (uintptr_t a = (start + size + G - 1) & ~(G - 1); a - start < reach;) {
        if (!VirtualQuery((void *)a, &m, sizeof(m))) break;
        if (m.State == MEM_FREE) {
            void *p = VirtualAlloc((void *)a, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
            if (p == (void *)a) return p;
            if (p) VirtualFree(p, 0, MEM_RELEASE);
            a += G;
        } else a = ((uintptr_t)m.BaseAddress + m.RegionSize + G - 1) & ~(G - 1);
    }
    return NULL;
}

static int displacement_for(const unsigned char *slot, int32_t *out) {
    int64_t d = (int64_t)((intptr_t)slot - (intptr_t)(image + WAIT_NEXT_RVA));
    if (d < INT32_MIN || d > INT32_MAX) return 0;
    *out = (int32_t)d;
    return 1;
}

typedef struct { uint64_t lo, hi; } pair128;

/* lock cmpxchg16b on a 16-byte aligned destination (atomic on every x86-64 CPU that
   runs Windows 8.1+, which requires CMPXCHG16B). */
static int compare_exchange16(void *destination, const uint64_t expected[2], const uint64_t value[2]) {
    uint64_t lo = expected[0], hi = expected[1];
    unsigned char ok;
    __asm__ __volatile__("lock cmpxchg16b %1\n\tsete %0"
                         : "=q"(ok), "+m"(*(volatile pair128 *)destination), "+a"(lo), "+d"(hi)
                         : "b"(value[0]), "c"(value[1])
                         : "memory", "cc");
    return ok;
}

/* One locked 16-byte compare-exchange of the aligned window holding the displacement, so
   another thread observes either the old or the new instruction, never a torn one. The
   final FlushProcessWriteBuffers interrupts every core running this process: interrupts
   land between instructions, so any call that already read the old slot has completed,
   and the return from the interrupt serialises the instruction stream. */
static int write_window(const unsigned char expected[16], const unsigned char value[16]) {
    unsigned char *site = image + WAIT_WINDOW_RVA;
    DWORD protection, ignored;
    if (((uintptr_t)site & 15) || !VirtualProtect(site, 16, PAGE_EXECUTE_READWRITE, &protection)) return 0;
    uint64_t compare[2], next[2];
    memcpy(compare, expected, 16);
    memcpy(next, value, 16);
    int ok = compare_exchange16(site, compare, next);
    VirtualProtect(site, 16, protection, &ignored);
    FlushInstructionCache(GetCurrentProcess(), site, 16);
    FlushProcessWriteBuffers();
    return ok;
}

static int32_t current_displacement(void) {
    int32_t d;
    memcpy(&d, (const void *)(image + WAIT_DISP_RVA), sizeof(d));
    return d;
}

static DWORD WINAPI wait_or_represent(HANDLE handle, DWORD ms);

/* start(): remember the verified original window and prepare the slot page. */
static const char *stage3_prepare(void) {
    memcpy(s3.original, image + WAIT_WINDOW_RVA, 16);
    unsigned char *page = alloc_near(image, X4_IMAGE_SIZE);
    if (!page) return "no slot page within 2 GB of the image";
    int32_t d;
    if (!displacement_for(page, &d)) { VirtualFree(page, 0, MEM_RELEASE); return "slot page out of rel32 range"; }
    *(void **)page = (void *)wait_or_represent;
    DWORD ignored;
    if (!VirtualProtect(page, 4096, PAGE_READONLY, &ignored)) {
        VirtualFree(page, 0, MEM_RELEASE);
        return "slot page protection failed";
    }
    s3.page = page;
    s3.displacement = d;
    memcpy(s3.patched, s3.original, 16);
    memcpy(s3.patched + (WAIT_DISP_RVA - WAIT_WINDOW_RVA), &d, sizeof(d));
    return NULL;
}

/* Slot 5 (MoveWorker, between FrameTicks): the main thread is in FrameTick's render tail,
   the message pump, or blocked inside this very wait (its return address 0x12ae89b is
   outside the changed bytes). The atomic window write lets it execute either the old or
   the new call, both complete and with identical caller-visible semantics. */
static int stage3_install(void) {
    if (!s3.page || !write_window(s3.original, s3.patched)) return 0;
    s3.installed = 1;
    s3.frames_since_install = 0;
    InterlockedExchange(&s3.active, 1);
    return 1;
}

/* Restore the original displacement, then free the page. write_window ends with a
   process-wide serialising flush, so no call through the old slot is still in flight. */
static void stage3_restore(void) {
    InterlockedExchange(&s3.active, 0);
    int release = 1;
    if (s3.installed) {
        if (!write_window(s3.patched, s3.original) && current_displacement() == s3.displacement)
            release = 0;  /* foreign bytes still reference the slot: keep the page alive */
        s3.installed = 0;
    }
    if (s3.page && release) { VirtualFree(s3.page, 0, MEM_RELEASE); s3.page = NULL; }
}

static const char *stage3_guard(void) {
    if (!s3.installed) return stage3_install() ? NULL : "stage3 call-site patch failed";
    if (current_displacement() != s3.displacement) return "stage3 call displacement changed";
    if (!readable(s3.page, sizeof(void *), 0) || *(void *volatile *)s3.page != (void *)wait_or_represent)
        return "stage3 slot changed";
    if (s3.violation) return s3.violation;
    ++s3.frames_since_install;
    return NULL;
}

static int gate_failure(HANDLE handle, DWORD thread) {
    if (thread == owner_thread || thread != (DWORD)s3.main_thread) return G_THREAD;
    if (handle != ptr_at(SEM_SIM_DONE_RVA)) return G_HANDLE;
    if (current_displacement() != s3.displacement) return G_PATCH;
    if (*(volatile unsigned char *)(image + SIM_IN_FLIGHT_RVA) != 1) return G_NOT_IN_FLIGHT;
    if (*(volatile unsigned char *)(image + WINDOW_SUSPENDED_RVA)) return G_SUSPENDED;
    unsigned char *table = ptr_at(VBI_RVA);
    if ((*(volatile unsigned *)(image + APP_STATE_RVA) & 2) || !table ||
        *(volatile int *)(table + 0x130)) return G_LOADING;
    if (!ptr_at(MOVE_WORKER_RVA)) return G_NO_MOVE_WORKER;
    if (*(volatile unsigned char *)(image + FIRST_FRAME_RVA) || s3.frames_since_install < 1) return G_FIRST_FRAME;
    unsigned char *fs = ptr_at(FRAME_STATE_PTR_RVA);
    if (!fs || !readable(fs + FS_THREADED, 8, 0) || !*(volatile unsigned char *)(fs + FS_THREADED))
        return G_NOT_THREADED;
    if (*(volatile unsigned *)(image + GFX_PENDING_RVA)) return G_GRAPHICS_PENDING;
    if (*(volatile unsigned char *)(image + CAPTURE_ACTIVE_RVA)) return G_MOVIE_CAPTURE;
    unsigned char *helper = ptr_at(STREAMLINE_PTR_RVA), *sl = NULL;
    if (helper && readable(helper + 0x30, sizeof(void *), 0)) sl = *(unsigned char *volatile *)(helper + 0x30);
    if (!sl || !readable(sl, 5, 0) || sl[0]) return G_STREAMLINE_REFLEX;
    if (sl[3] || sl[4]) return G_DLSS_FG;
    unsigned char *ffx = ptr_at(FFX_PTR_RVA);
    if (!ffx || !readable(ffx + FFX_FRAME_GEN, 1, 0) || *(volatile unsigned char *)(ffx + FFX_FRAME_GEN))
        return G_FSR_FG;
    if (*(volatile int *)(image + TEMP_PAUSE_RVA)) return G_SAVING;
    return -1;
}

static int64_t budget_ticks(void) { return qpc_frequency / FIXED_HZ; }

/* Re-present the read half exactly as FrameTick's threaded tail does. Reads, never writes,
   the semaphores, g_SimFrameInFlight, g_RenderReadIndex, FrameState+0x1e8, the time
   globals and the frame QPC. */
static void represent(void) {
    unsigned char *fs = ptr_at(FRAME_STATE_PTR_RVA);
    int index = *(volatile int *)(image + READ_INDEX_RVA);
    int queues = *(volatile int *)(fs + FS_QUEUE_COUNT);
    unsigned char in_flight = *(volatile unsigned char *)(image + SIM_IN_FLIGHT_RVA);
    begin_render();
    unsigned char ok = render_previous(fs, 1, 0);
    present_frame(fs, ok, 0);
    ++s3.re_presents;
    if (*(volatile int *)(image + READ_INDEX_RVA) != index) {
        ++s3.index_changed;
        s3.violation = "render read index changed during a re-present";
    }
    /* The MoveWorker's GetNewRenderQueue may legitimately advance this concurrently;
       counted, not fatal. Zero is expected while the stall injector holds the MoveWorker. */
    if (*(volatile int *)(fs + FS_QUEUE_COUNT) != queues) ++s3.queue_count_changed;
    if (*(volatile unsigned char *)(image + SIM_IN_FLIGHT_RVA) != in_flight) {
        ++s3.in_flight_changed;
        s3.violation = "simulation in-flight flag changed during a re-present";
    }
}

static void mark_present(int64_t now) {
    if (s3.last_present_qpc && qpc_frequency > 0) {
        double gap = (double)(now - s3.last_present_qpc) / (double)qpc_frequency;
        if (gap > s3.max_present_gap) s3.max_present_gap = gap;
    }
    s3.last_present_qpc = now;
}

/* With represent=0 a hypothetical re-present and a real present both restart the budget. */
static int64_t last_present_or_would(void) {
    if (s3.represent || s3.last_present_qpc > s3.last_would_qpc) return s3.last_present_qpc;
    return s3.last_would_qpc;
}

/* The redirected call. Always performs the real wait (through the IAT value, so any
   third-party hook on it still runs) and returns its result unchanged. */
static DWORD WINAPI wait_or_represent(HANDLE handle, DWORD ms) {
    wait_fn real = *(wait_fn volatile *)(image + WAIT_IAT_RVA);
    if (!s3.active) return real(handle, ms);
    DWORD thread = GetCurrentThreadId();
    InterlockedCompareExchange(&s3.main_thread, (LONG)thread, 0);
    LARGE_INTEGER now;
    int have_now = qpc_read(&now);
    /* The first wait after a successful one follows FrameTick and its real present. */
    if (have_now && InterlockedExchange(&s3.present_pending, 0)) mark_present(now.QuadPart);
    DWORD timeout = ms;
    int64_t last = last_present_or_would();
    if (s3.short_wait && s3.gates_ok_last && have_now && last && ms != INFINITE) {
        int64_t remaining = last + budget_ticks() - now.QuadPart;
        DWORD wait_ms = remaining > 0 ? (DWORD)((remaining * 1000 + qpc_frequency - 1) / qpc_frequency) : 1;
        if (!wait_ms) wait_ms = 1;
        if (wait_ms < timeout) timeout = wait_ms;
    }
    DWORD result = real(handle, timeout);
    if (result == WAIT_OBJECT_0) {
        ++s3.waits_ok;
        InterlockedExchange(&s3.present_pending, 1);
        return result;
    }
    if (result != WAIT_TIMEOUT) { ++s3.waits_other; return result; }
    ++s3.waits_timeout;
    if (!s3.active) return result;
    int gate = gate_failure(handle, thread);
    if (gate >= 0) { ++s3.gate_failures[gate]; s3.gates_ok_last = 0; return result; }
    s3.gates_ok_last = 1;
    if (!qpc_read(&now)) return result;
    last = last_present_or_would();
    if (!last || now.QuadPart - last < budget_ticks()) { ++s3.budget_skips; return result; }
    if (!s3.represent) {
        ++s3.would_represent;
        s3.last_would_qpc = now.QuadPart;
        return result;
    }
    represent();
    if (qpc_read(&now)) mark_present(now.QuadPart);
    return result;
}

static void inject_stall(void) {
    if (stall.ms && ++stall.calls % stall.every == 0) { ++stall.injected; Sleep(stall.ms); }
}

static void stall_move(handler *self) {
    (void)self;
    owner *o = current;
    if (!o || !o->armed || GetCurrentThreadId() != owner_thread || !registered_table ||
        *(int *)((unsigned char *)registered_table + 0x130)) return;
    inject_stall();
}

static void disarm_because(const char *reason) {
    if (current) { current->armed = 0; if (reason && !current->reason) current->reason = reason; }
    unpatch();
    if (s3.enabled) { stage3_restore(); s3.enabled = 0; }
    if (table_owned())
        InterlockedCompareExchangePointer((void *volatile *)&registered_table[SLOT], NULL, &hook);
    if (registered_table && stall.ms && stall.slot != SLOT)
        InterlockedCompareExchangePointer((void *volatile *)&registered_table[stall.slot], NULL, &stall_hook);
}

static void disarm(void) { disarm_because(NULL); }

static void move(handler *self) {
    (void)self;
    owner *o = current;
    if (!o || !o->armed) return;
    if (GetCurrentThreadId() != owner_thread || !table_owned()) { disarm_because("callback ownership lost"); return; }
    if (*(int *)((unsigned char *)registered_table + 0x130)) { ++o->loading_skips; return; }
    LARGE_INTEGER now;
    if (!qpc_read(&now) || qpc_frequency <= 0) { disarm_because("QPC unavailable"); return; }
    if (o->fixed) {
        const char *why = fixed_guard();
        if (why) { disarm_because(why); return; }
    }
    if (o->stage3) {
        const char *why = stage3_guard();
        if (why) { disarm_because(why); return; }
    }
    double game = *(volatile double *)(image + GAME_TIME_RVA);
    double real = *(volatile double *)(image + REAL_TIME_RVA);
    double speed = *(volatile double *)(image + SPEED_RVA);
    if (o->frames) {
        o->last_wall_dt = (double)(now.QuadPart - o->previous_qpc) / (double)qpc_frequency;
        o->last_game_dt = game - o->previous_game_time;
        o->last_real_dt = real - o->previous_real_time;
        if (o->last_wall_dt > o->max_wall_dt) o->max_wall_dt = o->last_wall_dt;
        if (o->last_game_dt < 0.0) ++o->game_time_regressions;
        o->wall_elapsed += o->last_wall_dt;
        o->game_elapsed += o->last_game_dt;
        if (o->fixed && fabs(o->last_real_dt - 1.0 / FIXED_HZ) < 1e-9) ++o->fixed_frames;
    }
    if (o->catchup) schedule_next(o);
    if (speed == 0.0) ++o->paused_frames;
    o->previous_qpc = now.QuadPart;
    o->previous_game_time = game;
    o->previous_real_time = real;
    o->game_time = game;
    o->real_time = real;
    o->speed = speed;
    ++o->frames;
    if (stall.slot == SLOT) inject_stall();
}

static int stop(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    if (o == current) { disarm(); current = NULL; }
    return 0;
}

static int active(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    if (o == current && o->armed && !table_owned()) disarm();
    p_lua_pushboolean(L, o == current && o->armed);
    return 1;
}

static void number_field(lua_State *L, const char *name, double value) {
    p_lua_pushnumber(L, value);
    p_lua_setfield(L, -2, name);
}

static int stats(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    p_lua_createtable(L, 0, 64);
    number_field(L, "frames", (double)o->frames);
    number_field(L, "loading_skips", (double)o->loading_skips);
    number_field(L, "paused_frames", (double)o->paused_frames);
    number_field(L, "game_time_regressions", (double)o->game_time_regressions);
    number_field(L, "wall_dt", o->last_wall_dt);
    number_field(L, "max_wall_dt", o->max_wall_dt);
    number_field(L, "game_dt", o->last_game_dt);
    number_field(L, "real_dt", o->last_real_dt);
    number_field(L, "game_time", o->game_time);
    number_field(L, "real_time", o->real_time);
    number_field(L, "speed", o->speed);
    number_field(L, "fixed", o->fixed);
    number_field(L, "fixed_frames", (double)o->fixed_frames);
    number_field(L, "wall_elapsed", o->wall_elapsed);
    number_field(L, "game_elapsed", o->game_elapsed);
    number_field(L, "catchup", o->catchup);
    number_field(L, "steps_total", (double)o->steps_total);
    number_field(L, "zero_step_frames", (double)o->zero_step_frames);
    number_field(L, "multi_step_frames", (double)o->multi_step_frames);
    number_field(L, "dropped_ticks", (double)o->dropped_ticks);
    number_field(L, "last_steps", o->last_steps);
    number_field(L, "accumulator", o->accumulator);
    number_field(L, "k_cap", o->k_cap);
    number_field(L, "capped_ticks", (double)o->capped_ticks);
    number_field(L, "slow_frames", (double)o->slow_frames);
    number_field(L, "stalls_injected", (double)stall.injected);
    number_field(L, "stage3", o->stage3);
    if (o->stage3) {
        char summary[512];
        int used = 0;
        number_field(L, "represent", s3.represent);
        number_field(L, "represent_active", o == current && o->armed && s3.active);
        number_field(L, "re_presents", (double)s3.re_presents);
        number_field(L, "would_represent", (double)s3.would_represent);
        number_field(L, "waits_ok", (double)s3.waits_ok);
        number_field(L, "waits_timeout", (double)s3.waits_timeout);
        number_field(L, "waits_other", (double)s3.waits_other);
        number_field(L, "budget_skips", (double)s3.budget_skips);
        number_field(L, "max_present_gap", s3.max_present_gap);
        number_field(L, "index_changed", (double)s3.index_changed);
        number_field(L, "queue_count_changed", (double)s3.queue_count_changed);
        number_field(L, "in_flight_changed", (double)s3.in_flight_changed);
        for (unsigned g = 0; g < G_COUNT; ++g) {
            char name[48];
            wsprintfA(name, "gate_%s", gate_names[g]);
            number_field(L, name, (double)s3.gate_failures[g]);
            if (s3.gate_failures[g] && used < (int)sizeof(summary) - 64)
                used += wsprintfA(summary + used, "%s%s:%lu", used ? "," : "", gate_names[g],
                                  (unsigned long)s3.gate_failures[g]);
        }
        p_lua_pushlstring(L, summary, (size_t)used);
        p_lua_setfield(L, -2, "gate_summary");
    }
    if (o->reason) { p_lua_pushlstring(L, o->reason, strlen(o->reason)); p_lua_setfield(L, -2, "reason"); }
    return 1;
}

static int failure(lua_State *L, const char *reason) {
    p_lua_pushnil(L);
    p_lua_pushlstring(L, reason, strlen(reason));
    return 2;
}

/* "key=value[,key=value...]": kcap (1..4), represent (0/1), shortwait (0/1),
   stall_ms (0..1000, 0 = off), stall_every (1..1000000), stall_slot (5, 7, 12, 13). */
static const char *parse_options(const char *text, options *out) {
    out->k_cap = DEFAULT_K_CAP; out->represent = 1; out->short_wait = 1;
    out->stall_ms = 0; out->stall_every = 120; out->stall_slot = SLOT;
    if (!text) return NULL;
    while (*text) {
        char key[16];
        unsigned n = 0, value = 0, digits = 0;
        while (*text && *text != '=' && *text != ',' && n < sizeof(key) - 1) key[n++] = *text++;
        key[n] = 0;
        if (*text != '=') return "unsupported option";
        ++text;
        while (*text >= '0' && *text <= '9' && digits < 8) { value = value * 10 + (unsigned)(*text++ - '0'); ++digits; }
        if (!digits || (*text && *text != ',')) return "unsupported option";
        if (*text == ',') ++text;
        if (!strcmp(key, "kcap") && value >= 1 && value <= MAX_CATCHUP) out->k_cap = value;
        else if (!strcmp(key, "represent") && value <= 1) out->represent = (int)value;
        else if (!strcmp(key, "shortwait") && value <= 1) out->short_wait = (int)value;
        else if (!strcmp(key, "stall_ms") && value <= 1000) out->stall_ms = value;
        else if (!strcmp(key, "stall_every") && value >= 1 && value <= 1000000) out->stall_every = value;
        else if (!strcmp(key, "stall_slot") && (value == 5 || value == 7 || value == 12 || value == 13))
            out->stall_slot = value;
        else return "unsupported option";
    }
    return NULL;
}

static int start(lua_State *L) {
    int fixed = 0, stage3 = 0, args = p_lua_gettop(L);
    const char *text = NULL;
    static char environment[256];
    if (args > 2) return failure(L, "unsupported mode");
    if (args >= 1) {
        if (p_lua_type(L, 1) != LUA_TSTRING) return failure(L, "unsupported mode");
        const char *mode = p_lua_tolstring(L, 1, NULL);
        if (!mode) return failure(L, "unsupported mode");
        if (!strcmp(mode, "probe")) fixed = 0;
        else if (!strcmp(mode, "fixed60")) fixed = 1;
        else if (!strcmp(mode, "fixed60catchup")) fixed = 2;
        else if (!strcmp(mode, "fixed60stage3")) { fixed = 2; stage3 = 1; }
        else return failure(L, "unsupported mode");
    }
    if (args == 2) {
        if (p_lua_type(L, 2) == LUA_TSTRING) text = p_lua_tolstring(L, 2, NULL);
        else if (p_lua_type(L, 2) != LUA_TNIL) return failure(L, "unsupported option");
    }
    if (!text) {
        DWORD n = GetEnvironmentVariableA("X4LAN_FIXEDTICK_OPTIONS", environment, sizeof(environment));
        if (n && n < sizeof(environment)) text = environment;
    }
    options opt;
    const char *bad = parse_options(text, &opt);
    if (bad) return failure(L, bad);
    if (current) return failure(L, "an owner already exists; stop it first");
    int main_thread = p_lua_pushthread(L);
    p_lua_settop(L, -2);
    if (!main_thread) return failure(L, "main Lua thread required");
    if (!verify_image(stage3)) return failure(L, "unsupported executable or modified engine signatures");
    void **table = table_now();
    if (!readable(table, 0x138, 1) || table[SLOT] || table[17+SLOT] || !table[3])
        return failure(L, "VBI slot occupied or unavailable");
    if (opt.stall_ms && opt.stall_slot != SLOT && table[opt.stall_slot])
        return failure(L, "stall slot occupied");
    if (*(int *)((unsigned char *)table + 0x130)) return failure(L, "loading");
    if (fixed) {
        if (!readable(image + CAPTURE_DT_RVA, sizeof(double), 1) ||
            !readable(image + CAPTURE_ACTIVE_RVA, 1, 0) ||
            !readable(image + LIMIT_ENABLED_RVA, 8, 0)) return failure(L, "fixed-dt globals unavailable");
        if (*(unsigned char *)(image + CAPTURE_ACTIVE_RVA)) return failure(L, "movie capture active");
        if (fixed == 1 && !limiter_at_fixed_rate()) return failure(L, "frame-rate limit is not 60");
        if (fixed == 2) {
            if (!readable(image + COLLISION_STEPS_PTR_RVA, sizeof(void *), 0) ||
                !readable(image + FRAME_QPC_RVA, sizeof(int64_t), 0)) return failure(L, "catch-up globals unavailable");
            int *steps = *(int **)(image + COLLISION_STEPS_PTR_RVA);
            if (!readable(steps, sizeof(int), 1)) return failure(L, "collision steps unavailable");
            if (*steps < 1 || *steps > (int)MAX_CATCHUP) return failure(L, "unexpected collision steps");
        }
        if (*(unsigned short *)(image + DT_BRANCH_RVA) != branch_original) return failure(L, "dt branch changed");
    }
    if (stage3) {
        static const uintptr_t globals[] = {
            WAIT_IAT_RVA, SEM_SIM_DONE_RVA, SIM_IN_FLIGHT_RVA, WINDOW_SUSPENDED_RVA, APP_STATE_RVA,
            MOVE_WORKER_RVA, FRAME_STATE_PTR_RVA, FIRST_FRAME_RVA, GFX_PENDING_RVA, STREAMLINE_PTR_RVA,
            FFX_PTR_RVA, TEMP_PAUSE_RVA, READ_INDEX_RVA
        };
        for (unsigned i = 0; i < sizeof(globals) / sizeof(globals[0]); ++i)
            if (!readable(image + globals[i], sizeof(void *), 0)) return failure(L, "stage3 globals unavailable");
        if (!*(void **)(image + WAIT_IAT_RVA)) return failure(L, "stage3 wait import unavailable");
    }
    LARGE_INTEGER frequency;
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0)
        return failure(L, "QPC frequency unavailable");
    HMODULE pinned;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                           GET_MODULE_HANDLE_EX_FLAG_PIN, (LPCWSTR)&hook, &pinned))
        return failure(L, "cannot pin callback module");
    if (stage3) {
        memset(&s3, 0, sizeof(s3));
        const char *why = stage3_prepare();
        if (why) return failure(L, why);
        s3.enabled = 1;
        s3.represent = opt.represent;
        s3.short_wait = opt.short_wait;
        begin_render = (begin_render_fn)(void *)(image + BEGIN_RENDER_RVA);
        render_previous = (render_previous_fn)(void *)(image + RENDER_PREVIOUS_RVA);
        present_frame = (present_fn)(void *)(image + PRESENT_RVA);
    }
    memset(&stall, 0, sizeof(stall));
    stall.ms = opt.stall_ms; stall.every = opt.stall_every; stall.slot = opt.stall_slot;
    owner *o = p_lua_newuserdata(L, sizeof(*o));
    memset(o, 0, sizeof(*o));
    o->k_cap = opt.k_cap;
    o->stage3 = stage3;
    p_lua_getfield(L, REGISTRY, META);
    p_lua_setmetatable(L, -2);
    registered_table = table;
    owner_thread = GetCurrentThreadId();
    qpc_frequency = frequency.QuadPart;
    current = o;
    if (InterlockedCompareExchangePointer((void *volatile *)&table[SLOT], &hook, NULL)) {
        current = NULL;
        if (stage3) { stage3_restore(); s3.enabled = 0; }
        return failure(L, "VBI slot changed during registration");
    }
    if (stall.ms && stall.slot != SLOT &&
        InterlockedCompareExchangePointer((void *volatile *)&table[stall.slot], &stall_hook, NULL)) {
        stall.ms = 0;
        disarm_because(NULL);
        current = NULL;
        return failure(L, "stall slot changed during registration");
    }
    if (fixed) {
        saved_capture_dt = *capture_dt();
        *capture_dt() = 1.0 / FIXED_HZ;
        if (!write_branch(branch_original, branch_patched)) {
            *capture_dt() = saved_capture_dt;
            disarm_because(NULL);
            current = NULL;
            return failure(L, "dt branch patch failed");
        }
        patched = 1;
        o->fixed = 1;
        if (fixed == 2) {
            collision_steps = *(int **)(image + COLLISION_STEPS_PTR_RVA);
            saved_collision_steps = *collision_steps;
            *collision_steps = 1;
            o->catchup = 1;
            o->last_steps = 1;
        }
    }
    /* Stage 3's call-site patch is installed by the first slot-5 callback (stage3_guard). */
    o->armed = 1;
    return 1;
}

static int resolve_lua(void) {
    HMODULE runtime = GetModuleHandleW(L"lua51_64.dll");
    if (!runtime) return 0;
#define RESOLVE(name) p_##name = (void *)GetProcAddress(runtime, #name); if (!p_##name) return 0
    RESOLVE(lua_gettop); RESOLVE(lua_type); RESOLVE(lua_tolstring); RESOLVE(lua_settop); RESOLVE(lua_pushthread);
    RESOLVE(lua_pushvalue); RESOLVE(lua_pushnil); RESOLVE(lua_pushboolean);
    RESOLVE(lua_pushnumber); RESOLVE(lua_pushlstring); RESOLVE(lua_pushcclosure);
    RESOLVE(lua_getfield); RESOLVE(lua_setfield); RESOLVE(lua_createtable);
    RESOLVE(lua_newuserdata); RESOLVE(lua_setmetatable); RESOLVE(luaL_newmetatable);
    RESOLVE(luaL_checkudata);
#undef RESOLVE
    return 1;
}

static void method(lua_State *L, const char *name, lua_CFunction function) {
    p_lua_pushcclosure(L, function, 0); p_lua_setfield(L, -2, name);
}

__declspec(dllexport) int luaopen_x4lan_fixedtick(lua_State *L) {
    if (!resolve_lua()) return 0;
    p_luaL_newmetatable(L, META);
    p_lua_pushvalue(L, -1); p_lua_setfield(L, -2, "__index");
    method(L, "__gc", stop); method(L, "stop", stop);
    method(L, "active", active); method(L, "stats", stats);
    p_lua_settop(L, -2);
    p_lua_createtable(L, 0, 1); method(L, "start", start);
    return 1;
}
