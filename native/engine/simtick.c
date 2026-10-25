/* Original, opt-in X4 9.00 / Steam 23660954 instrumentation.
 * No disk patches, injection, transport changes, or foreign-thread engine calls.
 * ABI observations: local x4-re/coord/reports/simtick.md.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <stdint.h>
#include <string.h>

typedef struct lua_State lua_State;
typedef int (*lua_CFunction)(lua_State *);
#define API(ret, name, args) static ret (*p_##name) args
API(int, lua_gettop, (lua_State *));
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
API(void, luaL_checktype, (lua_State *, int, int));
API(int, luaL_ref, (lua_State *, int));
API(void, luaL_unref, (lua_State *, int, int));
API(void, lua_rawgeti, (lua_State *, int, int));
API(int, lua_pcall, (lua_State *, int, int, int));

#define REGISTRY (-10000)
#define META "x4lan.simtick.owner.v1"
#define VBI_RVA 0x6cf1570u
#define ANARK_RVA 0x6cf18f8u
#define UI_VTABLE_RVA 0x2bc07d0u
#define IMAGE_SIZE 0x6fa9000u
#define SLOT 7

typedef struct { uint64_t frames; int armed; } owner;
typedef struct { void **vtable; } handler;
static unsigned char *image;
static owner *current;
static lua_State *state;
static int callback_ref = -2;
static void **registered_table;
static void *registered_ui;
static void *registered_anark;
static DWORD owner_thread;
static int inside_callback;

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
    if (!GetFileSizeEx(file, &size) || size.QuadPart != 55412768) goto done;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0) goto done;
    if (BCryptCreateHash(alg, &hash, NULL, 0, NULL, 0, 0) < 0) goto done;
    for (;;) {
        if (!ReadFile(file, buffer, sizeof(buffer), &got, NULL)) goto done;
        if (!got) break;
        if (BCryptHashData(hash, buffer, got, 0) < 0) goto done;
    }
    if (BCryptFinishHash(hash, digest, sizeof(digest), 0) < 0) goto done;
    ok = digest_matches(digest,
        "19750a6563889a970f434b5566eb396c6b2dc29ff814bd3e336f838176ad6891");
done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    CloseHandle(file);
    return ok;
}

static int verify_image(void) {
    image = (unsigned char *)GetModuleHandleW(NULL);
    if (!readable(image, sizeof(IMAGE_DOS_HEADER), 0)) return 0;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)image;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 0 ||
        dos->e_lfanew > 4096) return 0;
    IMAGE_NT_HEADERS64 *nt = (IMAGE_NT_HEADERS64 *)(image + dos->e_lfanew);
    if (!readable(nt, sizeof(*nt), 0) || nt->Signature != IMAGE_NT_SIGNATURE ||
        nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
        nt->FileHeader.TimeDateStamp != 0x6a1eaeed ||
        nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC ||
        nt->OptionalHeader.SizeOfImage != IMAGE_SIZE || !verify_file()) return 0;
    static const struct { size_t rva; ULONG length; const char *sha; } checks[] = {
        {0xef4400, 25, "4449b89ae08c1ebc42ad4bb0ad61acd1c63d68de52c6ae63c74e639390f85e91"},
        {0xef4681, 64, "161234434ac6f861075f55a3b7b5e4643a45c7b72045b225e4c6a3cdbc78cd6d"},
        {0x122b360, 24, "8e123f79687ad689c48d2eaf879253c42d1359f129ddebd2c2d51ea6658a46d0"},
        {0xeac170, 24, "718b690e9f3d2779d2f91c812253fb26bfa1f4ce4cd73accae7229675e261f12"},
        {0xb3bad0, 28, "893307c89a256e16553b164d9d16ec4f0c3187060ebd86513b8f55694f1378e4"},
        {0x14ca580, 42, "896bc70922e9954e08c838c449f51dbcdc428842a4c25dd6a80bdea2c4cc6a26"},
    };
    for (unsigned i = 0; i < sizeof(checks)/sizeof(checks[0]); ++i)
        if (!readable(image + checks[i].rva, checks[i].length, 0) ||
            !hash_memory(image + checks[i].rva, checks[i].length, checks[i].sha)) return 0;
    return readable(image + VBI_RVA, sizeof(void *), 0) &&
           readable(image + ANARK_RVA, sizeof(void *), 0);
}

static void move(handler *self);
static void no_op(handler *self) { (void)self; }
static void *vtable[] = {(void *)no_op, (void *)move, (void *)no_op};
static handler hook = {vtable};

static void **table_now(void) {
    return image ? *(void ***)(image + VBI_RVA) : NULL;
}

/* Never dereference a retained table after the engine replaces its singleton. */
static int table_owned(void) {
    void **table = table_now();
    return table && table == registered_table && readable(table, 0x138, 1) &&
           table[SLOT] == &hook;
}

static void disarm(void) {
    if (current) current->armed = 0;
    if (table_owned())
        InterlockedCompareExchangePointer((void *volatile *)&registered_table[SLOT], NULL, &hook);
}

static int context_valid(void) {
    if (!table_owned() || registered_table[6] != registered_ui ||
        !readable(registered_ui, 0x70 + sizeof(CRITICAL_SECTION), 0) ||
        *(void **)registered_ui != image + UI_VTABLE_RVA ||
        *(void **)(image + ANARK_RVA) != registered_anark ||
        !readable(registered_anark, 0x3f1, 0)) return 0;
    return 1;
}

static void move(handler *self) {
    (void)self;
    if (!current || !current->armed || inside_callback) return;
    if (GetCurrentThreadId() != owner_thread || !context_valid()) { disarm(); return; }
    if (*(int *)((unsigned char *)registered_table + 0x130)) return;
    CRITICAL_SECTION *lock = (CRITICAL_SECTION *)((unsigned char *)registered_ui + 0x70);
    if (!TryEnterCriticalSection(lock)) return;
    if (!context_valid()) { disarm(); LeaveCriticalSection(lock); return; }
    if (*(int *)((unsigned char *)registered_table + 0x130) ||
        !*((unsigned char *)registered_anark + 0x3f0)) {
        LeaveCriticalSection(lock); return;
    }
    owner *calling = current;
    lua_State *L = state;
    int top = p_lua_gettop(L);
    inside_callback = 1;
    p_lua_rawgeti(L, REGISTRY, callback_ref);
    int result = p_lua_pcall(L, 0, 0, 0);
    p_lua_settop(L, top);
    inside_callback = 0;
    /* The callback can stop its own handle. Never touch it after that. */
    if (current == calling) {
        if (result) disarm();
        else ++calling->frames;
    }
    LeaveCriticalSection(lock);
}

static int stop(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    if (o == current) {
        disarm();
        int ref = callback_ref;
        current = NULL; state = NULL; callback_ref = -2;
        p_luaL_unref(L, REGISTRY, ref);
    }
    return 0;
}

static int active(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    if (o == current && o->armed && !context_valid()) disarm();
    p_lua_pushboolean(L, o == current && o->armed);
    return 1;
}

static int frames(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    p_lua_pushnumber(L, (double)o->frames);
    return 1;
}

static int failure(lua_State *L, const char *reason) {
    p_lua_pushnil(L);
    p_lua_pushlstring(L, reason, strlen(reason));
    return 2;
}

static int start(lua_State *L) {
    p_luaL_checktype(L, 1, 6); /* LUA_TFUNCTION */
    if (current || inside_callback) return failure(L, "an owner already exists; stop it first");
    int main_thread = p_lua_pushthread(L);
    p_lua_settop(L, -2);
    if (!main_thread) return failure(L, "main Lua thread required");
    if (!verify_image()) return failure(L, "unsupported executable or modified engine signatures");
    void **table = table_now();
    if (!readable(table, 0x138, 1) || table[7] || table[17+7] || !table[3])
        return failure(L, "VBI slot occupied or unavailable");
    if (*(int *)((unsigned char *)table + 0x130)) return failure(L, "loading");
    void *ui = table[6];
    void *anark = *(void **)(image + ANARK_RVA);
    if (!readable(ui, 0x70 + sizeof(CRITICAL_SECTION), 0) ||
        *(void **)ui != image + UI_VTABLE_RVA || !readable(anark, 0x3f1, 0) ||
        !*((unsigned char *)anark + 0x3f0))
        return failure(L, "UI lifetime guard unavailable");
    HMODULE pinned;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                           GET_MODULE_HANDLE_EX_FLAG_PIN, (LPCWSTR)&hook, &pinned))
        return failure(L, "cannot pin callback module");
    owner *o = p_lua_newuserdata(L, sizeof(*o));
    o->armed = 0; o->frames = 0;
    p_lua_getfield(L, REGISTRY, META);
    p_lua_setmetatable(L, -2);
    p_lua_pushvalue(L, 1);
    int ref = p_luaL_ref(L, REGISTRY);
    registered_table = table; registered_ui = ui; registered_anark = anark;
    owner_thread = GetCurrentThreadId();
    state = L; current = o; callback_ref = ref;
    if (InterlockedCompareExchangePointer((void *volatile *)&table[SLOT], &hook, NULL)) {
        current = NULL; state = NULL; callback_ref = -2;
        p_luaL_unref(L, REGISTRY, ref);
        return failure(L, "VBI slot changed during registration");
    }
    o->armed = 1;
    return 1;
}

static int resolve_lua(void) {
    HMODULE runtime = GetModuleHandleW(L"lua51_64.dll");
    if (!runtime) return 0;
#define RESOLVE(name) p_##name = (void *)GetProcAddress(runtime, #name); if (!p_##name) return 0
    RESOLVE(lua_gettop); RESOLVE(lua_settop); RESOLVE(lua_pushthread);
    RESOLVE(lua_pushvalue); RESOLVE(lua_pushnil); RESOLVE(lua_pushboolean);
    RESOLVE(lua_pushnumber); RESOLVE(lua_pushlstring); RESOLVE(lua_pushcclosure);
    RESOLVE(lua_getfield); RESOLVE(lua_setfield); RESOLVE(lua_createtable);
    RESOLVE(lua_newuserdata); RESOLVE(lua_setmetatable); RESOLVE(luaL_newmetatable);
    RESOLVE(luaL_checkudata); RESOLVE(luaL_checktype); RESOLVE(luaL_ref);
    RESOLVE(luaL_unref); RESOLVE(lua_rawgeti); RESOLVE(lua_pcall);
#undef RESOLVE
    return 1;
}

static void method(lua_State *L, const char *name, lua_CFunction function) {
    p_lua_pushcclosure(L, function, 0); p_lua_setfield(L, -2, name);
}

__declspec(dllexport) int luaopen_x4lan_simtick(lua_State *L) {
    if (!resolve_lua()) return 0;
    p_luaL_newmetatable(L, META);
    p_lua_pushvalue(L, -1); p_lua_setfield(L, -2, "__index");
    method(L, "__gc", stop); method(L, "stop", stop);
    method(L, "active", active); method(L, "frames", frames);
    p_lua_settop(L, -2);
    p_lua_createtable(L, 0, 1); method(L, "start", start);
    return 1;
}
