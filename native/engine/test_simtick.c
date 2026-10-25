/* Standalone synthetic process. Never attaches to or launches X4.
 * Include implementation to exercise lifecycle guards without a production bypass.
 */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include "simtick.c"

struct lua_State { int top; };
static struct lua_State fake_lua;
static owner fake_owner;
static void *fake_table[40];
static unsigned char fake_ui[256], fake_anark[0x400];
static int calls, fail_call, stop_in_call, unrefs;

static int gettop(lua_State *L) { return L->top; }
static void settop(lua_State *L, int top) { L->top = top; }
static void rawgeti(lua_State *L, int index, int ref) {
    assert(index == REGISTRY && ref == 123); ++L->top;
}
static void *checkudata(lua_State *L, int index, const char *name) {
    (void)L; assert(index == 1 && !strcmp(name, META)); return &fake_owner;
}
static void unref(lua_State *L, int index, int ref) {
    (void)L; assert(index == REGISTRY && ref == 123); ++unrefs;
}
static int pcall_mock(lua_State *L, int args, int results, int error) {
    assert(!args && !results && !error); ++calls;
    L->top = 99; /* stack restoration must hold even on errors */
    if (stop_in_call) stop(L);
    return fail_call;
}

static void setup(void) {
    memset(fake_table, 0, sizeof(fake_table));
    memset(fake_anark, 0, sizeof(fake_anark));
    *(void **)fake_ui = image + UI_VTABLE_RVA;
    fake_anark[0x3f0] = 1;
    fake_table[3] = &fake_owner;
    fake_table[6] = fake_ui;
    fake_table[7] = &hook;
    *(void ***)(image + VBI_RVA) = fake_table;
    *(void **)(image + ANARK_RVA) = fake_anark;
    registered_table = fake_table; registered_ui = fake_ui; registered_anark = fake_anark;
    fake_owner.frames = 0; fake_owner.armed = 1;
    current = &fake_owner; state = &fake_lua; callback_ref = 123;
    owner_thread = GetCurrentThreadId(); inside_callback = 0;
    fake_lua.top = 4; calls = fail_call = stop_in_call = unrefs = 0;
}

static HANDLE locked_event, release_event;
static DWORD WINAPI hold_ui_lock(void *unused) {
    (void)unused;
    CRITICAL_SECTION *lock = (CRITICAL_SECTION *)(fake_ui + 0x70);
    EnterCriticalSection(lock); SetEvent(locked_event);
    WaitForSingleObject(release_event, INFINITE);
    LeaveCriticalSection(lock); return 0;
}

int main(void) {
    assert(!verify_image()); /* this test EXE must never pass the X4 build gate */
    image = VirtualAlloc(NULL, IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image);
    InitializeCriticalSection((CRITICAL_SECTION *)(fake_ui + 0x70));
    p_lua_gettop = gettop; p_lua_settop = settop; p_lua_rawgeti = rawgeti;
    p_lua_pcall = pcall_mock; p_luaL_checkudata = checkudata; p_luaL_unref = unref;
    assert(hash_memory("abc", 3,
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
    assert(!hash_memory("abd", 3,
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));

    setup(); move(&hook);
    assert(calls == 1 && fake_owner.frames == 1 && fake_lua.top == 4);
    setup(); *(int *)((unsigned char *)fake_table + 0x130) = 1; move(&hook);
    assert(!calls && fake_owner.armed);
    setup(); fake_anark[0x3f0] = 0; move(&hook);
    assert(!calls && fake_owner.armed);
    setup(); ++owner_thread; move(&hook);
    assert(!calls && !fake_owner.armed && !fake_table[7]);
    setup(); *(void **)(image + ANARK_RVA) = NULL; move(&hook);
    assert(!calls && !fake_owner.armed && !fake_table[7]);
    setup(); fake_table[7] = &fake_owner; move(&hook);
    assert(!calls && !fake_owner.armed && fake_table[7] == &fake_owner);
    setup(); *(void ***)(image + VBI_RVA) = NULL; move(&hook);
    assert(!calls && !fake_owner.armed && fake_table[7] == &hook);
    setup(); fail_call = 1; move(&hook);
    assert(calls == 1 && !fake_owner.frames && !fake_owner.armed && fake_lua.top == 4);
    move(&hook); assert(calls == 1);
    setup(); inside_callback = 1; move(&hook); assert(!calls);
    setup(); stop_in_call = 1; move(&hook);
    assert(calls == 1 && !current && !state && !fake_table[7] && unrefs == 1);
    assert(fake_lua.top == 4);
    stop(&fake_lua); assert(unrefs == 1); /* idempotent stop/GC */

    setup();
    locked_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    release_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    assert(locked_event && release_event);
    HANDLE thread = CreateThread(NULL, 0, hold_ui_lock, NULL, 0, NULL);
    assert(thread && WaitForSingleObject(locked_event, 5000) == WAIT_OBJECT_0);
    move(&hook); assert(!calls && fake_owner.armed);
    SetEvent(release_event); assert(WaitForSingleObject(thread, 5000) == WAIT_OBJECT_0);
    move(&hook); assert(calls == 1);
    CloseHandle(thread); CloseHandle(locked_event); CloseHandle(release_event);
    disarm();
    DeleteCriticalSection((CRITICAL_SECTION *)(fake_ui + 0x70));
    VirtualFree(image, 0, MEM_RELEASE);
    puts("PASS: synthetic simtick lifecycle, build rejection, hashes, thread/lock/loading guards");
    return 0;
}
