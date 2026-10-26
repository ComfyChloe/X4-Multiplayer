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
#define LUA_TSTRING 4
static const unsigned short branch_original = 0x0874; /* je +8 */
static const unsigned short branch_patched = 0x9066;  /* 66 90 */

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

static int verify_image(void) {
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
    return readable(image + VBI_RVA, sizeof(void *), 0) &&
           readable(image + GAME_TIME_RVA, sizeof(double), 0) &&
           readable(image + REAL_TIME_RVA, sizeof(double), 0) &&
           readable(image + SPEED_RVA, sizeof(double), 0);
}

static void move(handler *self);
static void no_op(handler *self) { (void)self; }
static void *vtable[] = {(void *)no_op, (void *)move, (void *)no_op};
static handler hook = {vtable};

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
    if (o->last_frame_qpc && qpc > o->last_frame_qpc && qpc_frequency > 0)
        o->accumulator += (double)(qpc - o->last_frame_qpc) / (double)qpc_frequency;
    o->last_frame_qpc = qpc;
    /* A 1e-3 tick tolerance keeps steady rates (e.g. 30 fps with QPC rounding) at a
       constant step count instead of alternating k-1/k+1. */
    unsigned k = (unsigned)(o->accumulator * FIXED_HZ + 1e-3);
    if (k > MAX_CATCHUP) {
        o->dropped_ticks += k - MAX_CATCHUP;
        o->accumulator -= (double)(k - MAX_CATCHUP) / FIXED_HZ;
        k = MAX_CATCHUP;
    }
    o->accumulator -= (double)k / FIXED_HZ;
    if (o->accumulator < 0.0) o->accumulator = 0.0;
    *capture_dt() = (double)k / FIXED_HZ;
    *collision_steps = k ? (int)k : 1;
    o->last_steps = k;
    o->steps_total += k;
    if (!k) ++o->zero_step_frames; else if (k > 1) ++o->multi_step_frames;
}

static void disarm_because(const char *reason) {
    if (current) { current->armed = 0; if (reason && !current->reason) current->reason = reason; }
    unpatch();
    if (table_owned())
        InterlockedCompareExchangePointer((void *volatile *)&registered_table[SLOT], NULL, &hook);
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
    p_lua_createtable(L, 0, 26);
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
    if (o->reason) { p_lua_pushlstring(L, o->reason, strlen(o->reason)); p_lua_setfield(L, -2, "reason"); }
    return 1;
}

static int failure(lua_State *L, const char *reason) {
    p_lua_pushnil(L);
    p_lua_pushlstring(L, reason, strlen(reason));
    return 2;
}

static int start(lua_State *L) {
    int fixed = 0;
    if (p_lua_gettop(L) == 1 && p_lua_type(L, 1) == LUA_TSTRING) {
        const char *mode = p_lua_tolstring(L, 1, NULL);
        if (!mode) return failure(L, "unsupported mode");
        if (!strcmp(mode, "fixed60")) fixed = 1;
        else if (!strcmp(mode, "fixed60catchup")) fixed = 2;
        else return failure(L, "unsupported mode");
    } else if (p_lua_gettop(L) != 0) return failure(L, "unsupported mode");
    if (current) return failure(L, "an owner already exists; stop it first");
    int main_thread = p_lua_pushthread(L);
    p_lua_settop(L, -2);
    if (!main_thread) return failure(L, "main Lua thread required");
    if (!verify_image()) return failure(L, "unsupported executable or modified engine signatures");
    void **table = table_now();
    if (!readable(table, 0x138, 1) || table[SLOT] || table[17+SLOT] || !table[3])
        return failure(L, "VBI slot occupied or unavailable");
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
    LARGE_INTEGER frequency;
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0)
        return failure(L, "QPC frequency unavailable");
    HMODULE pinned;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                           GET_MODULE_HANDLE_EX_FLAG_PIN, (LPCWSTR)&hook, &pinned))
        return failure(L, "cannot pin callback module");
    owner *o = p_lua_newuserdata(L, sizeof(*o));
    memset(o, 0, sizeof(*o));
    p_lua_getfield(L, REGISTRY, META);
    p_lua_setmetatable(L, -2);
    registered_table = table;
    owner_thread = GetCurrentThreadId();
    qpc_frequency = frequency.QuadPart;
    current = o;
    if (InterlockedCompareExchangePointer((void *volatile *)&table[SLOT], &hook, NULL)) {
        current = NULL;
        return failure(L, "VBI slot changed during registration");
    }
    if (fixed) {
        saved_capture_dt = *capture_dt();
        *capture_dt() = 1.0 / FIXED_HZ;
        if (!write_branch(branch_original, branch_patched)) {
            *capture_dt() = saved_capture_dt;
            InterlockedCompareExchangePointer((void *volatile *)&table[SLOT], NULL, &hook);
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
