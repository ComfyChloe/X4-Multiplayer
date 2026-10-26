/* View-cache capture probe for the transforms rewrite (x4lan_xform_64.dll, test lanes only).
 *
 * Captures selected simulation frames of U::Component::BuildCache 0x3d4a80 (the view-cache
 * build in U::UniverseFrameUpdate) for offline replay with rust/x4mp-transform
 * (src/capture.rs, examples/replay_capture.rs). Never packaged; started by ui/xform_probe.lua
 * from a lane overlay.
 *
 * Patch: the E8 rel32 of the call at 0x9fe549 (UniverseFrameUpdate -> BuildCache) is redirected
 * to a jump stub within +-2 GB of the image, which jumps to hook_build_cache(). On ordinary
 * frames the hook calls the original BuildCache unchanged. On capture frames it runs the
 * capture core (xform_capture.h): read-only walk, original BuildCache, read-back. Trace frames
 * additionally redirect the two PropagateCacheFlags calls inside BuildCache (0x3d4d0a, 0x3d4ea6)
 * and its recursion (0x3d4d18) for the duration of that one call, then restore them.
 *
 * Safety: refuses unless X4.exe is the pinned build (file SHA-256, PE timestamp, image size)
 * and the code of BuildCache, PropagateCacheFlags, the call-site region and the count helpers
 * hashes to the pinned values, and every patched call is E8 with the expected target. Patches
 * are written only on the MoveWorker outside slot 3 (start/stop run from the slot-6 Lua
 * callback; trace patches from inside the hook, before and after the original call), each
 * compared against the expected displacement and read back. Any failure leaves the original
 * code in place (or, if a restore fails, the stubs, which still call the originals; the module
 * is pinned). The hook captures only on the registering thread and never during loading.
 * Engine memory is only read, plus the engine's own IsClass (vtable slot 573) as BuildCache
 * itself calls it.
 */
#ifndef XFORM_PROBE_EMBED
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "fixedtick_signatures.h"

static unsigned char *image;
#include "xform_capture.h"
#endif /* XFORM_PROBE_EMBED: the includer (offline test) provides image and the capture core */

#ifndef XFORM_PROBE_EMBED
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
API(double, lua_tonumber, (lua_State *, int));
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
#define META "x4lan.xform.owner.v1"
#define LUA_TNUMBER 3
#define LUA_TSTRING 4
#endif

#define VBI_RVA 0x6cf1570u
#define RVA_PROPAGATE 0x3d46b0u
#define SITE_FRAME 0x9fe549u     /* UniverseFrameUpdate: call BuildCache */
#define SITE_CHILD 0x3d4d0au     /* BuildCache: call PropagateCacheFlags (children) */
#define SITE_ATTACH 0x3d4ea6u    /* BuildCache: call PropagateCacheFlags (attachments) */
#define SITE_RECURSE 0x3d4d18u   /* BuildCache: call BuildCache (recursion) */
#define STUB_BYTES 16

static const struct { uint32_t rva, length; const char *sha; } code_checks[] = {
    {0x3d4a80u, 1792, "88b7a3b84ea6f6a4ea1bbc039c59683fa3621d4eb21119b838ad08de6ff0a9de"}, /* BuildCache */
    {0x3d46b0u, 976, "e51e1c65075e806dc17c7dcc5b1cb635b7df7b196083687d706be4c803892dd9"},  /* PropagateCacheFlags */
    {0x9fe500u, 128, "574034288966dd92debb9ac0010c7f3a0f58fb666531fffaaacb630f66c45b2a"},  /* call-site region */
    {0x3d55c0u, 64, "085614be5629e4fc13339376ca69adea5b643d7d14fdda9dc490bde5ba7b1c8c"},   /* count helpers */
};

typedef struct {
    uint32_t rva, target;
    unsigned char *site, *stub;
    int32_t original_rel, hook_rel;
    int patched;
} call_site;

#ifndef XFORM_PROBE_EMBED
typedef void (*propagate_fn)(void *c, unsigned idx, const uint32_t *flags);
#endif

typedef struct {
    int armed;
    const char *reason;
    uint64_t frames, captures, failures, loading_skips, foreign_thread;
    double first_s, every_s, next_s;
    uint32_t max_captures, trace_every;
    uint64_t last_nodes, last_visits, last_opens, last_bytes, last_events;
    double last_walk_ms, last_build_ms, last_write_ms, last_time;
    const char *last_skip;
    char last_file[512];
} owner;

/* The state is static so that the hook never reads Lua memory; the userdata is a handle. */
static owner state;
static owner *current;
#ifndef XFORM_PROBE_EMBED
static uint64_t generation;
typedef struct { uint64_t generation; } handle;
#endif
static DWORD owner_thread;
static WCHAR capture_dir[400];
static unsigned char *stub_page;
static call_site frame_site = {SITE_FRAME, XC_RVA_BUILD_CACHE, 0, 0, 0, 0, 0};
static call_site trace_sites[3] = {
    {SITE_CHILD, RVA_PROPAGATE, 0, 0, 0, 0, 0},
    {SITE_ATTACH, RVA_PROPAGATE, 0, 0, 0, 0, 0},
    {SITE_RECURSE, XC_RVA_BUILD_CACHE, 0, 0, 0, 0, 0},
};
static xc_build_cache_fn original_build_cache;
static propagate_fn original_propagate;
static xc_capture cap;
static xc_buf buf;
static uint32_t (*trace)[4];
static size_t trace_n, trace_cap;
static int tracing;
static int64_t qpc_freq;

static int readable(const void *ptr, size_t length) {
    MEMORY_BASIC_INFORMATION m;
    uintptr_t start = (uintptr_t)ptr, end = start + length;
    while (start < end) {
        if (!VirtualQuery((const void *)start, &m, sizeof m) || m.State != MEM_COMMIT ||
            (m.Protect & (PAGE_NOACCESS | PAGE_GUARD)))
            return 0;
        start = (uintptr_t)m.BaseAddress + m.RegionSize;
    }
    return 1;
}

static int digest_matches(const unsigned char d[32], const char *hex) {
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < 32; ++i)
        if (hex[2 * i] != digits[d[i] >> 4] || hex[2 * i + 1] != digits[d[i] & 15]) return 0;
    return hex[64] == 0;
}

static int sha256(const void *data, size_t n, unsigned char out[32]) {
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    int ok = 0;
    if (n > 0xffffffffu) return 0;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0) goto done;
    if (BCryptCreateHash(alg, &hash, NULL, 0, NULL, 0, 0) < 0) goto done;
    if (BCryptHashData(hash, (PUCHAR)data, (ULONG)n, 0) < 0) goto done;
    ok = BCryptFinishHash(hash, out, 32, 0) >= 0;
done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    return ok;
}

#ifndef XFORM_PROBE_EMBED
static int verify_file(void) {
    WCHAR path[32768];
    DWORD n = GetModuleFileNameW(NULL, path, 32768);
    if (!n || n >= 32768) return 0;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    LARGE_INTEGER size;
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    static unsigned char chunk[1 << 16];
    unsigned char digest[32];
    DWORD got;
    int ok = 0;
    if (!GetFileSizeEx(file, &size) || size.QuadPart != X4_FILE_SIZE) goto done;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0) goto done;
    if (BCryptCreateHash(alg, &hash, NULL, 0, NULL, 0, 0) < 0) goto done;
    for (;;) {
        if (!ReadFile(file, chunk, sizeof chunk, &got, NULL)) goto done;
        if (!got) break;
        if (BCryptHashData(hash, chunk, got, 0) < 0) goto done;
    }
    if (BCryptFinishHash(hash, digest, sizeof digest, 0) < 0) goto done;
    ok = digest_matches(digest, X4_FILE_SHA256);
done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    CloseHandle(file);
    return ok;
}

#endif

/* with_file: also hash the executable on disk (the embedded offline test maps a verified copy). */
static const char *verify_image(int with_file) {
#ifndef XFORM_PROBE_EMBED
    image = (unsigned char *)GetModuleHandleW(NULL);
#endif
    if (!readable(image, 4096)) return "image unreadable";
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)image;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 0 || dos->e_lfanew > 4096) return "not a PE image";
    IMAGE_NT_HEADERS64 *nt = (IMAGE_NT_HEADERS64 *)(image + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
        nt->FileHeader.TimeDateStamp != X4_PE_TIMESTAMP || nt->OptionalHeader.SizeOfImage != X4_IMAGE_SIZE)
        return "unsupported executable (PE header)";
#ifndef XFORM_PROBE_EMBED
    if (with_file && !verify_file()) return "unsupported executable (file hash)";
#else
    (void)with_file;
#endif
    for (size_t i = 0; i < sizeof code_checks / sizeof code_checks[0]; ++i) {
        unsigned char d[32];
        if (!readable(image + code_checks[i].rva, code_checks[i].length) ||
            !sha256(image + code_checks[i].rva, code_checks[i].length, d) || !digest_matches(d, code_checks[i].sha))
            return "modified engine code (view-cache signatures)";
    }
    if (!readable(image + VBI_RVA, 8) || !readable(image + XC_RVA_SPEC_DEPTH, 24) || !readable(image + XC_RVA_DROP_POSE, 1))
        return "engine globals unreadable";
    return NULL;
}

/* ---- call-site patching ---- */
static unsigned char *alloc_near(size_t size) {
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    uintptr_t gran = si.dwAllocationGranularity ? si.dwAllocationGranularity : 0x10000;
    uintptr_t lo = (uintptr_t)image, hi = lo + X4_IMAGE_SIZE;
    const uintptr_t reach = 0x70000000u;
    for (uintptr_t a = (hi + gran - 1) & ~(gran - 1); a + size - lo < reach;) {
        MEMORY_BASIC_INFORMATION m;
        if (!VirtualQuery((void *)a, &m, sizeof m)) break;
        uintptr_t base = (uintptr_t)m.BaseAddress, end = base + m.RegionSize;
        if (m.State == MEM_FREE) {
            uintptr_t c = (base + gran - 1) & ~(gran - 1);
            if (c + size <= end && c + size - lo < reach) {
                void *p = VirtualAlloc((void *)c, size, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
                if (p) return p;
            }
        }
        if (end <= a) break;
        a = end;
    }
    for (uintptr_t a = lo; a > gran + size && hi - (a - size) < reach;) {
        MEMORY_BASIC_INFORMATION m;
        if (!VirtualQuery((void *)(a - 1), &m, sizeof m)) break;
        uintptr_t base = (uintptr_t)m.BaseAddress, end = base + m.RegionSize;
        if (m.State == MEM_FREE && end - base >= size) {
            uintptr_t c = (end - size) & ~(gran - 1);
            if (c >= base && hi - c < reach) {
                void *p = VirtualAlloc((void *)c, size, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
                if (p) return p;
            }
        }
        if (base >= a) break;
        a = base;
    }
    return NULL;
}

static void emit_stub(unsigned char *s, void *target) {
    memset(s, 0xcc, STUB_BYTES);
    s[0] = 0xff;
    s[1] = 0x25; /* jmp [rip+0] */
    memset(s + 2, 0, 4);
    memcpy(s + 6, &target, sizeof target);
}

/* Fills in a site; checks E8 and the expected target. */
static int prepare_site(call_site *c, unsigned char *stub) {
    c->site = image + c->rva;
    if (c->site[0] != 0xe8) return 0;
    memcpy(&c->original_rel, c->site + 1, 4);
    if (c->site + 5 + c->original_rel != image + c->target) return 0;
    intptr_t d = (intptr_t)stub - (intptr_t)(c->site + 5);
    if (d < INT32_MIN || d > INT32_MAX) return 0;
    c->stub = stub;
    c->hook_rel = (int32_t)d;
    return 1;
}

/* Writes the displacement from `expected` to `value` and reads it back. Only the MoveWorker
   executes these sites and it is not inside them now, so the unaligned write is not observed
   half-done. */
static int write_rel(call_site *c, int32_t expected, int32_t value) {
    DWORD protection, ignored;
    int32_t now;
    memcpy(&now, c->site + 1, 4);
    if (c->site[0] != 0xe8 || now != expected) return 0;
    if (!VirtualProtect(c->site + 1, 4, PAGE_EXECUTE_READWRITE, &protection)) return 0;
    memcpy(c->site + 1, &value, 4);
    VirtualProtect(c->site + 1, 4, protection, &ignored);
    FlushInstructionCache(GetCurrentProcess(), c->site, 5);
    memcpy(&now, c->site + 1, 4);
    return now == value;
}

static int patch(call_site *c) {
    if (c->patched) return 1;
    c->patched = write_rel(c, c->original_rel, c->hook_rel);
    return c->patched;
}
static int unpatch(call_site *c) {
    if (!c->patched) return 1;
    if (!write_rel(c, c->hook_rel, c->original_rel)) return 0;
    c->patched = 0;
    return 1;
}

/* ---- hooks ---- */
static void trace_add(uint32_t kind, uint32_t node, uint32_t a, uint32_t b) {
    if (trace_n == trace_cap) {
        size_t n = trace_cap ? trace_cap * 2 : 1u << 18;
        if (n > (1u << 24)) { ++trace_n; return; }
        void *p = realloc(trace, n * sizeof *trace);
        if (!p) { ++trace_n; return; }
        trace = p;
        trace_cap = n;
    }
    if (trace_n < trace_cap) {
        trace[trace_n][0] = kind;
        trace[trace_n][1] = node;
        trace[trace_n][2] = a;
        trace[trace_n][3] = b;
    }
    ++trace_n;
}

static uint32_t node_of(const void *c) {
    int64_t i = xc_find(&cap, c);
    return i < 0 ? 0xffffffffu : (uint32_t)i;
}

static void hook_propagate(void *c, unsigned idx, const uint32_t *flags) {
    if (tracing) {
        uint32_t before = xc_u32_at(xc_vc(c, idx), XC_VC_DIRTY);
        uint32_t n = node_of(c);
        trace_add(6, n, *flags, before);
        original_propagate(c, idx, flags);
        trace_add(8, n, xc_u32_at(xc_vc(c, idx), XC_VC_DIRTY), 0);
        return;
    }
    original_propagate(c, idx, flags);
}

static void hook_recurse(void *c, unsigned idx, double time) {
    if (tracing) trace_add(7, node_of(c), 0, 0);
    original_build_cache(c, idx, time);
}

static void run_plain(void *galaxy, unsigned idx, double time, void *ctx) {
    (void)ctx;
    original_build_cache(galaxy, idx, time);
}

static void run_traced(void *galaxy, unsigned idx, double time, void *ctx) {
    int *ok = ctx;
    *ok = 1;
    trace_n = 0;
    for (int i = 0; i < 3; ++i)
        if (!patch(&trace_sites[i])) *ok = 0;
    tracing = *ok;
    original_build_cache(galaxy, idx, time);
    tracing = 0;
    for (int i = 2; i >= 0; --i)
        if (!unpatch(&trace_sites[i])) *ok = -1;
}

static double ms_since(LARGE_INTEGER a) {
    LARGE_INTEGER b;
    QueryPerformanceCounter(&b);
    return qpc_freq > 0 ? (double)(b.QuadPart - a.QuadPart) * 1000.0 / (double)qpc_freq : 0.0;
}

static int write_capture(owner *o, const xc_buf *w, uint32_t number) {
    WCHAR path[600];
    _snwprintf(path, sizeof path / sizeof path[0], L"%ls\\xform_cap_%03u.cap", capture_dir, number);
    path[sizeof path / sizeof path[0] - 1] = 0;
    HANDLE f = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return 0;
    size_t done = 0;
    int ok = 1;
    while (done < w->n && ok) {
        DWORD part = (DWORD)((w->n - done) > (1u << 30) ? (1u << 30) : (w->n - done)), wrote = 0;
        ok = WriteFile(f, w->b + done, part, &wrote, NULL) && wrote == part;
        done += wrote;
    }
    ok = CloseHandle(f) && ok;
    if (ok) WideCharToMultiByte(CP_UTF8, 0, path, -1, o->last_file, sizeof o->last_file, NULL, NULL);
    return ok;
}

static void capture(owner *o, void *galaxy, unsigned idx, double time) {
    uint32_t number = (uint32_t)o->captures;
    int traced = o->trace_every && (number % o->trace_every) == 0;
    xc_meta meta = {number, o->frames, (uint32_t)traced};
    int ran = 0, trace_ok = 0;
    LARGE_INTEGER t0;
    QueryPerformanceCounter(&t0);
    const char *why = xc_capture_frame(&cap, &buf, galaxy, idx, time, &meta, traced ? run_traced : run_plain,
                                       &trace_ok, &ran);
    double total_ms = ms_since(t0);
    if (!ran) original_build_cache(galaxy, idx, time);
    if (traced && ran && trace_ok == -1) {
        o->reason = "trace call site could not be restored (stubs left in place, module pinned)";
        o->armed = 0;
    }
    if (why || (traced && trace_ok != 1) || (traced && trace_n > trace_cap)) {
        ++o->failures;
        o->last_skip = why ? why : "trace unavailable";
        return;
    }
    if (traced) {
        xc_u32(&buf, (uint32_t)trace_n);
        xc_put(&buf, trace, trace_n * sizeof *trace);
        if (buf.failed) { ++o->failures; o->last_skip = "out of memory (trace)"; return; }
    }
    LARGE_INTEGER t1;
    QueryPerformanceCounter(&t1);
    int written = write_capture(o, &buf, number);
    o->last_write_ms = ms_since(t1);
    if (!written) { ++o->failures; o->last_skip = "capture file not written"; return; }
    ++o->captures;
    o->last_nodes = cap.count;
    o->last_visits = cap.visits;
    o->last_opens = cap.opens;
    o->last_bytes = buf.n;
    o->last_events = traced ? trace_n : 0;
    o->last_walk_ms = total_ms;
    o->last_time = time;
    o->last_skip = NULL;
}

static void hook_build_cache(void *galaxy, unsigned idx, double time) {
    owner *o = current;
    if (!o || !o->armed) { original_build_cache(galaxy, idx, time); return; }
    ++o->frames;
    if (GetCurrentThreadId() != owner_thread) { ++o->foreign_thread; original_build_cache(galaxy, idx, time); return; }
    void **table = *(void ***)(image + VBI_RVA);
    if (table && *(int *)((unsigned char *)table + 0x130)) { ++o->loading_skips; original_build_cache(galaxy, idx, time); return; }
    if (o->captures + o->failures < o->max_captures && time >= o->next_s) {
        o->next_s = time + o->every_s;
        capture(o, galaxy, idx, time);
        return;
    }
    original_build_cache(galaxy, idx, time);
}

static void disarm(owner *o) {
    if (!o) return;
    if (frame_site.patched && !unpatch(&frame_site))
        o->reason = o->reason ? o->reason : "call site changed by someone else; left in place";
    o->armed = 0;
}

/* Everything start() does after the Lua checks: verify, stub page, sites, arm, patch. */
static const char *arm(double first_s, double every_s, uint32_t count, uint32_t trace_every, int with_file) {
    const char *why = verify_image(with_file);
    if (why) return why;
    void **table = *(void ***)(image + VBI_RVA);
    if (table && *(int *)((unsigned char *)table + 0x130)) return "loading";
    LARGE_INTEGER f;
    if (!QueryPerformanceFrequency(&f) || f.QuadPart <= 0) return "QPC frequency unavailable";
    qpc_freq = f.QuadPart;
    if (!stub_page) {
        stub_page = alloc_near(0x1000);
        if (!stub_page) return "no stub page within 2 GB of the image";
        emit_stub(stub_page, (void *)hook_build_cache);
        emit_stub(stub_page + STUB_BYTES, (void *)hook_propagate);
        emit_stub(stub_page + 2 * STUB_BYTES, (void *)hook_recurse);
        DWORD old;
        if (!VirtualProtect(stub_page, 0x1000, PAGE_EXECUTE_READ, &old)) return "stub page protection failed";
        FlushInstructionCache(GetCurrentProcess(), stub_page, 0x1000);
    }
    if (!prepare_site(&frame_site, stub_page) || !prepare_site(&trace_sites[0], stub_page + STUB_BYTES) ||
        !prepare_site(&trace_sites[1], stub_page + STUB_BYTES) || !prepare_site(&trace_sites[2], stub_page + 2 * STUB_BYTES))
        return "call site mismatch or out of reach";
    original_build_cache = (xc_build_cache_fn)(image + XC_RVA_BUILD_CACHE);
    original_propagate = (propagate_fn)(image + RVA_PROPAGATE);
    owner *o = &state;
    memset(o, 0, sizeof *o);
    o->first_s = first_s;
    o->every_s = every_s;
    o->next_s = first_s;
    o->max_captures = count;
    o->trace_every = trace_every;
    owner_thread = GetCurrentThreadId();
    current = o;
    o->armed = 1;
    if (!patch(&frame_site)) {
        o->armed = 0;
        current = NULL;
        return "call site patch failed";
    }
    return NULL;
}

#ifndef XFORM_PROBE_EMBED
/* ---- Lua ---- */
static int failure(lua_State *L, const char *reason) {
    p_lua_pushnil(L);
    p_lua_pushlstring(L, reason, strlen(reason));
    return 2;
}

static owner *owner_of(lua_State *L) {
    handle *h = p_luaL_checkudata(L, 1, META);
    return current && h->generation == generation ? current : NULL;
}

static int stop(lua_State *L) {
    owner *o = owner_of(L);
    if (o) {
        disarm(o);
        if (!frame_site.patched) current = NULL;
    }
    return 0;
}

static int active(lua_State *L) {
    owner *o = owner_of(L);
    int ok = o && o->armed && frame_site.patched;
    if (ok) {
        int32_t now;
        memcpy(&now, frame_site.site + 1, 4);
        if (now != frame_site.hook_rel) { o->reason = "call site changed by someone else"; o->armed = 0; ok = 0; }
    }
    p_lua_pushboolean(L, ok);
    return 1;
}

static void number_field(lua_State *L, const char *name, double value) {
    p_lua_pushnumber(L, value);
    p_lua_setfield(L, -2, name);
}
static void string_field(lua_State *L, const char *name, const char *value) {
    p_lua_pushlstring(L, value, strlen(value));
    p_lua_setfield(L, -2, name);
}

static int stats(lua_State *L) {
    owner *o = owner_of(L);
    if (!o) o = &state;
    p_lua_createtable(L, 0, 20);
    number_field(L, "frames", (double)o->frames);
    number_field(L, "captures", (double)o->captures);
    number_field(L, "failures", (double)o->failures);
    number_field(L, "loading_skips", (double)o->loading_skips);
    number_field(L, "foreign_thread", (double)o->foreign_thread);
    number_field(L, "nodes", (double)o->last_nodes);
    number_field(L, "visits", (double)o->last_visits);
    number_field(L, "opens", (double)o->last_opens);
    number_field(L, "bytes", (double)o->last_bytes);
    number_field(L, "events", (double)o->last_events);
    number_field(L, "capture_ms", o->last_walk_ms);
    number_field(L, "write_ms", o->last_write_ms);
    number_field(L, "time", o->last_time);
    if (o->last_file[0]) string_field(L, "file", o->last_file);
    if (o->last_skip) string_field(L, "skip", o->last_skip);
    if (o->reason) string_field(L, "reason", o->reason);
    return 1;
}

/* start(dir, first_s, every_s, count, trace_every) on the main Lua thread (MoveWorker, slot 6). */
static int start(lua_State *L) {
    if (p_lua_gettop(L) != 5 || p_lua_type(L, 1) != LUA_TSTRING) return failure(L, "usage: start(dir, first_s, every_s, count, trace_every)");
    for (int i = 2; i <= 5; ++i)
        if (p_lua_type(L, i) != LUA_TNUMBER) return failure(L, "usage: start(dir, first_s, every_s, count, trace_every)");
    if (current) return failure(L, "an owner already exists; stop it first");
    int main_thread = p_lua_pushthread(L);
    p_lua_settop(L, -2);
    if (!main_thread) return failure(L, "main Lua thread required");
    const char *dir = p_lua_tolstring(L, 1, NULL);
    double first_s = p_lua_tonumber(L, 2), every_s = p_lua_tonumber(L, 3), count = p_lua_tonumber(L, 4), trace_every = p_lua_tonumber(L, 5);
    if (!dir || !dir[0] || !(every_s >= 0.0) || !(count >= 1 && count <= 1000) || !(trace_every >= 0 && trace_every <= 1000))
        return failure(L, "invalid arguments");
    if (!MultiByteToWideChar(CP_UTF8, 0, dir, -1, capture_dir, sizeof capture_dir / sizeof capture_dir[0]))
        return failure(L, "capture directory path too long");
    DWORD attributes = GetFileAttributesW(capture_dir);
    if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY)) return failure(L, "capture directory missing");
    HMODULE pinned;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN, (LPCWSTR)&hook_build_cache, &pinned))
        return failure(L, "cannot pin module");
    const char *why = arm(first_s, every_s, (uint32_t)count, (uint32_t)trace_every, 1);
    if (why) return failure(L, why);
    handle *h = p_lua_newuserdata(L, sizeof *h);
    h->generation = ++generation;
    p_lua_getfield(L, REGISTRY, META);
    p_lua_setmetatable(L, -2);
    return 1;
}

static int resolve_lua(void) {
    HMODULE runtime = GetModuleHandleW(L"lua51_64.dll");
    if (!runtime) return 0;
#define RESOLVE(name) p_##name = (void *)GetProcAddress(runtime, #name); if (!p_##name) return 0
    RESOLVE(lua_gettop); RESOLVE(lua_type); RESOLVE(lua_tolstring); RESOLVE(lua_settop); RESOLVE(lua_pushthread);
    RESOLVE(lua_pushvalue); RESOLVE(lua_pushnil); RESOLVE(lua_pushboolean); RESOLVE(lua_pushnumber);
    RESOLVE(lua_tonumber); RESOLVE(lua_pushlstring); RESOLVE(lua_pushcclosure); RESOLVE(lua_getfield);
    RESOLVE(lua_setfield); RESOLVE(lua_createtable); RESOLVE(lua_newuserdata); RESOLVE(lua_setmetatable);
    RESOLVE(luaL_newmetatable); RESOLVE(luaL_checkudata);
#undef RESOLVE
    return 1;
}

static void method(lua_State *L, const char *name, lua_CFunction function) {
    p_lua_pushcclosure(L, function, 0);
    p_lua_setfield(L, -2, name);
}

__declspec(dllexport) int luaopen_x4lan_xform(lua_State *L) {
    if (!resolve_lua()) return 0;
    p_luaL_newmetatable(L, META);
    p_lua_pushvalue(L, -1);
    p_lua_setfield(L, -2, "__index");
    method(L, "__gc", stop);
    method(L, "stop", stop);
    method(L, "active", active);
    method(L, "stats", stats);
    p_lua_settop(L, -2);
    p_lua_createtable(L, 0, 1);
    method(L, "start", start);
    return 1;
}
#endif /* XFORM_PROBE_EMBED */
