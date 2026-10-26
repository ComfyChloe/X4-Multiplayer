/* Synthetic lifecycle and behaviour tests for framediet.c (no X4 involved).
 * A fake image holds the VBI table pointer, the five vtable entries, RenderFlags, the ID map
 * pointer and the deferred event list; fake engine functions stand in for the wrapped virtuals. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include "framediet.c"

static void *fake_table[40];
static unsigned char fake_tls[0x400];
static unsigned char fake_map[0x200];
static unsigned char *fake_block;
static unsigned char fake_components[6][COMPONENT_WINDOW];
static unsigned char fake_macro[0x50];
static unsigned char *fake_tls_fn(void) { return fake_tls; }

static void **entry_of(int k) { return (void **)(image + x4_framediet_vtables[k].entry); }
static void *fn_of(int k) { return image + x4_framediet_vtables[k].fn; }

/* Side effects a fake engine function performs (to check the audit). */
typedef struct { int rng_steps; int ids; int events; int poke_offset; int poke_refcount; uint64_t reseed; } effects;
static effects walk_fx, camera_fx, render_fx, control_fx;
static int scene_walks, scene_manager_calls, ui_renders, client_renders, camera_renders, spins;

static void apply(const effects *fx) {
    uint64_t *rng = (uint64_t *)(fake_tls + TLS_RNG);
    for (int i = 0; i < fx->rng_steps; ++i) *rng = rng_step(*rng);
    if (fx->reseed) *rng = fx->reseed;
    *(uint64_t *)(fake_map + ID_MAP_INSERTS) += (uint64_t)fx->ids;
    *(uint64_t *)(image + EVENT_LIST_RVA + 8) += (uint64_t)fx->events;
    if (fx->poke_offset) fake_components[1][fx->poke_offset] ^= 0x5a;
    if (fx->poke_refcount) ++*(uint32_t *)(fake_components[2] + REFCOUNT_OFFSET);
}

/* AnarkScene slot 5 shape: clear lists (not modelled), then tail-call slot 12 via the vtable. */
static uintptr_t fake_scene_manager(void *self) {
    ++scene_manager_calls;
    for (int scene = 0; scene < 3; ++scene)
        ((fd_fn2)*entry_of(VS_SCENE_WALK))(self, (void *)(uintptr_t)(scene + 1));
    return 0;
}
static uintptr_t fake_scene_walk(void *self, void *root) {
    (void)self; assert(root);
    ++scene_walks; apply(&walk_fx);
    return 7;
}
static uintptr_t fake_ui_render(void *self) { (void)self; ++ui_renders; apply(&render_fx); return 0; }
static uintptr_t fake_client_render(void *self) {
    ++client_renders;
    for (int camera = 0; camera < 2; ++camera) ((fd_fn1)*entry_of(VS_CAMERA_RENDER))(self);
    return 0;
}
static uintptr_t fake_camera_render(void *self) { (void)self; ++camera_renders; apply(&camera_fx); return 0; }
static void fake_spin(double us) { (void)us; ++spins; apply(&control_fx); }

static void register_component(uint64_t slot, unsigned char *component, uint16_t gen) {
    unsigned char *b = fake_block + (slot / 3) * 32;
    unsigned j = (unsigned)(slot % 3);
    *(unsigned char **)(b + 8 * j) = component;
    *(uint16_t *)(b + 0x18 + 2 * j) = gen;
    *(uint32_t *)(b + 0x1c) |= 1u << (16 + 4 * j);
}

static void reset_counts(void) {
    memset(&walk_fx, 0, sizeof(walk_fx)); memset(&camera_fx, 0, sizeof(camera_fx));
    memset(&render_fx, 0, sizeof(render_fx)); memset(&control_fx, 0, sizeof(control_fx));
    scene_walks = scene_manager_calls = ui_renders = client_renders = camera_renders = spins = 0;
}

/* movabs rax, target; jmp rax: the fake image's "engine functions" jump to the fakes. */
static void stub(size_t rva, void *target) {
    unsigned char *at = image + rva;
    at[0] = 0x48; at[1] = 0xb8;
    memcpy(at + 2, &target, 8);
    at[10] = 0xff; at[11] = 0xe0;
}

static void setup_engine(void) {
    memset(fake_table, 0, sizeof(fake_table));
    fake_table[3] = fake_table;  /* GameClient present */
    *(void ***)(image + VBI_RVA) = fake_table;
    void *fakes[VS_COUNT] = {(void *)fake_scene_manager, (void *)fake_scene_walk, (void *)fake_ui_render,
                             (void *)fake_client_render, (void *)fake_camera_render};
    for (int k = 0; k < VS_COUNT; ++k) { stub(x4_framediet_vtables[k].fn, fakes[k]); *entry_of(k) = fn_of(k); }
    FlushInstructionCache(GetCurrentProcess(), image, X4_IMAGE_SIZE);
    *(uint64_t *)(image + RENDER_FLAGS_RVA) = 0x20;          /* noshadows */
    *(uint64_t *)(image + RENDER_FLAGS_STAGED_RVA) = 0x20;
    *(unsigned char **)(image + ID_MAP_PTR_RVA) = fake_map;
    *(unsigned char **)(fake_map + ID_MAP_BLOCKS) = fake_block;
    *(uint64_t *)(fake_map + ID_MAP_INSERTS) = 1000;
    *(uint64_t *)(image + EVENT_LIST_RVA + 8) = 3;
    *(uint64_t *)(fake_tls + TLS_RNG) = 0x1234567890abcdefull;
    *(double *)(fake_tls + TLS_GAME_TIME) = 42.0;
    memset(fake_components, 0, sizeof(fake_components));
    *(unsigned char **)(fake_macro) = NULL;
    *(uint32_t *)(fake_macro + 0x44) = 0x75;   /* ship */
    for (int c = 0; c < 6; ++c) *(unsigned char **)(fake_components[c] + 0x60) = fake_macro;
    reset_counts();
}

/* Arms like start() does after the executable checks. */
static const char *arm(int mode, int sites, int period, int audit_every, unsigned digest_max) {
    static uint64_t generation = 100;
    return fd_arm(++generation, mode, sites, period, audit_every, digest_max);
}

static void disarm_all(void) {
    fd_disarm(NULL);
    fd_current = NULL;
}

/* One MoveWorker frame: slot 2 guard, slot 6 scene update, slot 10 UI and camera render banks. */
static void frame(void) {
    fd_slot_move(&fd_hook);
    ((fd_fn1)*entry_of(VS_SCENE_MANAGER))(NULL);
    ((fd_fn1)*entry_of(VS_UI_RENDER))(NULL);
    ((fd_fn1)*entry_of(VS_CLIENT_RENDER))(NULL);
}

static volatile LONG other_done;
static DWORD WINAPI other_thread_frame(LPVOID unused) {
    (void)unused;
    ((fd_fn1)*entry_of(VS_SCENE_MANAGER))(NULL);
    ((fd_fn1)*entry_of(VS_UI_RENDER))(NULL);
    ((fd_fn1)*entry_of(VS_CLIENT_RENDER))(NULL);
    InterlockedExchange(&other_done, 1);
    return 0;
}

int main(void) {
    assert(!verify_image());   /* the test executable is not X4 */
    image = VirtualAlloc(NULL, X4_IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    fake_block = VirtualAlloc(NULL, (ID_BLOCK_SLOTS / 3 + 1) * 32, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image && fake_block);
    fd_tls = fake_tls_fn;
    fd_spin = fake_spin;

    /* Site parsing and modes. */
    {
        int s, m;
        assert(parse_sites("server", &s) && s == (FD_UI_SCENES | FD_DRAW));
        assert(parse_sites("ui_scenes,camera", &s) && s == (FD_UI_SCENES | FD_CAMERA));
        assert(parse_sites("all", &s) && s == FD_ALL);
        assert(parse_sites("", &s) && s == 0);
        assert(!parse_sites("ui_scene", &s) && !parse_sites("draw,bogus", &s));
        assert(parse_mode("diet", &m) && m == FD_DIET && parse_mode("pass", &m) && m == FD_PASS);
        assert(parse_mode("audit", &m) && m == FD_AUDIT && !parse_mode("skip", &m) && !parse_mode(NULL, &m));
    }

    /* RNG distance on X4's thread LCG, including the bound. */
    {
        uint64_t a = 99, b = a;
        for (int i = 0; i < 17; ++i) b = rng_step(b);
        assert(rng_distance(a, a) == 0 && rng_distance(a, b) == 17);
        assert(rng_distance(b, a) == RNG_STEP_BOUND + 1);
    }

    /* Refusals leave everything untouched: occupied slot, loading, foreign vtable entry. */
    setup_engine();
    fake_table[VBI_SLOT] = fake_table;
    assert(!strcmp(arm(FD_DIET, FD_ALL, 0, 0, 0), "VBI slot occupied or unavailable"));
    fake_table[VBI_SLOT] = NULL; fake_table[17 + VBI_SLOT] = fake_table;
    assert(!strcmp(arm(FD_DIET, FD_ALL, 0, 0, 0), "VBI slot occupied or unavailable"));
    fake_table[17 + VBI_SLOT] = NULL;
    *(int *)((unsigned char *)fake_table + LOADING_OFFSET) = 1;
    assert(!strcmp(arm(FD_DIET, FD_ALL, 0, 0, 0), "loading"));
    *(int *)((unsigned char *)fake_table + LOADING_OFFSET) = 0;
    *entry_of(VS_CAMERA_RENDER) = image + 5;   /* someone else wrapped Camera::Render */
    assert(!strcmp(arm(FD_DIET, FD_ALL, 0, 0, 0), "vtable entry is not the verified original"));
    assert(!fd_current && !fake_table[VBI_SLOT]);
    for (int k = 0; k < VS_CAMERA_RENDER; ++k) assert(*entry_of(k) == fn_of(k));
    assert(*entry_of(VS_CAMERA_RENDER) == image + 5);
    /* The same foreign entry does not block a set that does not need it. */
    assert(!arm(FD_DIET, FD_UI_SCENES | FD_DRAW, 0, 0, 0));
    assert(fd_current && fd_current->armed && fake_table[VBI_SLOT] == &fd_hook);
    assert(*entry_of(VS_SCENE_MANAGER) == (void *)fd_scene_manager && *entry_of(VS_SCENE_WALK) == (void *)fd_scene_walk);
    assert(*entry_of(VS_UI_RENDER) == fn_of(VS_UI_RENDER) && *entry_of(VS_CAMERA_RENDER) == image + 5);
    disarm_all();
    assert(*entry_of(VS_SCENE_MANAGER) == fn_of(VS_SCENE_MANAGER) && *entry_of(VS_SCENE_WALK) == fn_of(VS_SCENE_WALK));
    assert(*entry_of(VS_CAMERA_RENDER) == image + 5 && !fake_table[VBI_SLOT]);
    puts("PASS: refusals (occupied slot, loading bank, loading, foreign entry) leave the engine untouched");

    /* Diet: the scene layer walk is skipped inside the scene manager only; draw bit held. */
    setup_engine();
    assert(!arm(FD_DIET, FD_UI_SCENES | FD_DRAW, 0, 0, 0));
    for (int f = 0; f < 10; ++f) frame();
    assert(scene_manager_calls == 10 && scene_walks == 0);
    assert(fd_state.st[ST_UI_SCENES].calls == 30 && fd_state.st[ST_UI_SCENES].skipped == 30);
    assert(ui_renders == 10 && camera_renders == 20);   /* not installed: untouched originals */
    assert(fd_state.frames == 10 && fd_state.draw_set == 1);
    assert(*(uint64_t *)(image + RENDER_FLAGS_RVA) == (0x20 | DISABLE_RENDERING));
    assert(*(uint64_t *)(image + RENDER_FLAGS_STAGED_RVA) == (0x20 | DISABLE_RENDERING));
    /* Outside the scene manager the walk passes through. */
    ((fd_fn2)*entry_of(VS_SCENE_WALK))(NULL, (void *)1);
    assert(scene_walks == 1);
    /* An options apply copies a staged value without the bit: re-applied at the next frame. */
    *(uint64_t *)(image + RENDER_FLAGS_STAGED_RVA) = 0x80;
    *(uint64_t *)(image + RENDER_FLAGS_RVA) = 0x80;
    frame();
    assert(fd_state.draw_reapplied == 1 && *(uint64_t *)(image + RENDER_FLAGS_RVA) == (0x80 | DISABLE_RENDERING));
    /* Other threads always pass through. */
    {
        int walks = scene_walks;
        HANDLE t = CreateThread(NULL, 0, other_thread_frame, NULL, 0, NULL);
        assert(t && WaitForSingleObject(t, 5000) == WAIT_OBJECT_0 && other_done);
        CloseHandle(t);
        assert(scene_walks == walks + 3);
    }
    /* Loading: everything passes through, the bit stays (main thread renders the loading screen). */
    *(int *)((unsigned char *)fake_table + LOADING_OFFSET) = 1;
    {
        int walks = scene_walks;
        frame();
        assert(scene_walks == walks + 3 && fd_state.loading_frames == 1);
    }
    *(int *)((unsigned char *)fake_table + LOADING_OFFSET) = 0;
    /* stop: entries restored, only our bit cleared, slot released. */
    disarm_all();
    assert(*(uint64_t *)(image + RENDER_FLAGS_RVA) == 0x80 && *(uint64_t *)(image + RENDER_FLAGS_STAGED_RVA) == 0x80);
    for (int k = 0; k < VS_COUNT; ++k) assert(*entry_of(k) == fn_of(k));
    assert(!fake_table[VBI_SLOT]);
    puts("PASS: diet skips the Anark layer walk only inside the scene update, holds and releases disable_rendering");

    /* A bit that was already set by someone else is never cleared by us. */
    setup_engine();
    *(uint64_t *)(image + RENDER_FLAGS_RVA) |= DISABLE_RENDERING;
    *(uint64_t *)(image + RENDER_FLAGS_STAGED_RVA) |= DISABLE_RENDERING;
    assert(!arm(FD_DIET, FD_DRAW, 0, 0, 0));
    frame(); frame();
    assert(!fd_draw_owned && fd_state.draw_set == 0);
    disarm_all();
    assert(*(uint64_t *)(image + RENDER_FLAGS_RVA) & DISABLE_RENDERING);
    puts("PASS: a disable_rendering bit set by someone else is left alone");

    /* All sites, period 4: every 4th frame presents (all originals run). */
    setup_engine();
    assert(!arm(FD_DIET, FD_ALL, 4, 0, 0));
    for (int f = 0; f < 8; ++f) frame();
    assert(scene_walks == 2 * 3 && ui_renders == 2 && camera_renders == 2 * 2);
    assert(fd_state.present_frames == 2 && client_renders == 8);
    assert(fd_state.st[ST_CAMERA].skipped == 12 && fd_state.st[ST_UI_RENDER].skipped == 6);
    /* pass mode: same wrappers, nothing skipped, draw bit released. */
    fd_state.mode = FD_PASS; fd_draw_release();
    reset_counts();
    for (int f = 0; f < 3; ++f) frame();
    assert(scene_walks == 9 && ui_renders == 3 && camera_renders == 6 && !fd_draw_owned);
    assert(!(*(uint64_t *)(image + RENDER_FLAGS_RVA) & DISABLE_RENDERING));
    disarm_all();
    puts("PASS: all sites skip, period frames present everything, pass mode is inert");

    /* Fail closed: a vtable entry changed under us, the slot replaced, the table replaced. */
    setup_engine();
    assert(!arm(FD_DIET, FD_ALL, 0, 0, 0));
    frame();
    *entry_of(VS_UI_RENDER) = image + 9;
    fd_slot_move(&fd_hook);
    assert(!fd_state.armed && fd_state.reason && !strcmp(fd_state.reason, "vtable entry changed"));
    assert(*entry_of(VS_UI_RENDER) == image + 9);   /* foreign value kept */
    assert(*entry_of(VS_CAMERA_RENDER) == fn_of(VS_CAMERA_RENDER) && !fake_table[VBI_SLOT]);
    assert(!(*(uint64_t *)(image + RENDER_FLAGS_RVA) & DISABLE_RENDERING));
    {
        int walks = scene_walks;   /* disarmed wrappers pass through (entries restored anyway) */
        ((fd_fn2)(void *)fd_scene_walk)(NULL, (void *)1);
        assert(scene_walks == walks + 1);
    }
    fd_current = NULL;
    setup_engine();
    assert(!arm(FD_DIET, FD_ALL, 0, 0, 0));
    fake_table[VBI_SLOT] = fake_table;   /* someone took slot 2 */
    fd_slot_move(&fd_hook);
    assert(!fd_state.armed && !strcmp(fd_state.reason, "callback ownership lost"));
    assert(fake_table[VBI_SLOT] == fake_table);
    for (int k = 0; k < VS_COUNT; ++k) assert(*entry_of(k) == fn_of(k));
    fd_current = NULL;
    setup_engine();
    assert(!arm(FD_DIET, FD_ALL, 0, 0, 0));
    {
        static void *other_table[40];
        *(void ***)(image + VBI_RVA) = other_table;  /* engine replaced its table singleton */
        int walks = scene_walks;
        ((fd_fn1)*entry_of(VS_SCENE_MANAGER))(NULL);
        assert(scene_walks == walks + 3);       /* wrappers pass through at once */
        fd_slot_move(&fd_hook);
        assert(!fd_state.armed && !strcmp(fd_state.reason, "callback ownership lost"));
        assert(!other_table[VBI_SLOT] && fake_table[VBI_SLOT] == &fd_hook);  /* stale table never written */
    }
    fd_current = NULL;
    puts("PASS: fail closed on a changed entry, a taken slot or a replaced table; foreign values kept");

    /* Audit: originals run; RNG steps, ID insertions, deferred events and component writes are
       measured per region; refcount writes are ignored; the control window sees background writes. */
    setup_engine();
    for (int c = 0; c < 6; ++c) register_component((uint64_t)c * 7, fake_components[c], (uint16_t)(c + 1));
    assert(!arm(FD_AUDIT, FD_UI_SCENES | FD_UI_RENDER | FD_CAMERA, 0, 1, 64));
    walk_fx.poke_refcount = 1;                       /* ignored word */
    camera_fx.rng_steps = 5; camera_fx.ids = 2; camera_fx.events = 1;
    camera_fx.poke_offset = 0x1ec;                   /* attention of component 1 */
    control_fx.poke_offset = 0x70;                   /* a "background thread" write */
    for (int f = 0; f < 8; ++f) frame();
    assert(scene_walks == 24 && camera_renders == 16 && ui_renders == 8);   /* nothing skipped */
    {
        const fd_stats *w = &fd_state.st[ST_UI_SCENES], *c = &fd_state.st[ST_CAMERA];
        const fd_stats *r = &fd_state.st[ST_UI_RENDER], *k = &fd_state.st[ST_CONTROL];
        assert(w->audited == 24 && w->rng_calls == 0 && w->ids == 0 && w->event_calls == 0);
        assert(w->digests == 2 && w->digest_components == 12 && w->digest_changed == 0);
        assert(c->audited == 16 && c->rng_calls == 16 && c->rng_steps == 80 && c->rng_unknown == 0);
        assert(c->ids == 32 && c->id_calls == 16 && c->events_up == 16 && c->time_changed == 0);
        assert(c->digests == 2 && c->digest_changed == 2 && c->histogram[0x1e8 / 8] == 2);
        assert(strstr(c->first, "class=0x75 off=0x1e8"));
        assert(r->audited == 8 && r->digests == 2 && r->digest_changed == 0 && r->rng_calls == 0);
        assert(k->digests == 2 && spins == 2 && k->digest_changed == 2 && k->histogram[0x70 / 8] == 2);
        assert(fd_state.st[ST_CAMERA].skipped == 0);
        char words[128];
        top_words(c, words, sizeof(words));
        assert(!strcmp(words, "0x1e8:2"));
    }
    /* A reseed (or more than RNG_STEP_BOUND draws) is counted as unknown, not as steps. */
    memset(&camera_fx, 0, sizeof(camera_fx));
    camera_fx.reseed = 77;
    {
        uint64_t steps = fd_state.st[ST_CAMERA].rng_steps, unknown = fd_state.st[ST_CAMERA].rng_unknown;
        frame();
        assert(fd_state.st[ST_CAMERA].rng_steps == steps);
        assert(fd_state.st[ST_CAMERA].rng_unknown == unknown + 1);   /* first camera reseeds, second keeps 77 */
    }
    disarm_all();
    puts("PASS: audit measures RNG steps, ID insertions, events and component writes per region, control window");

    /* Histogram summary orders by count and lists at most six words. */
    {
        fd_stats s0;
        char words[128];
        memset(&s0, 0, sizeof(s0));
        for (unsigned w = 0; w < 8; ++w) s0.histogram[w + 1] = w + 1;
        top_words(&s0, words, sizeof(words));
        assert(!strcmp(words, "0x40:8,0x38:7,0x30:6,0x28:5,0x20:4,0x18:3"));
    }

    VirtualFree(fake_block, 0, MEM_RELEASE);
    VirtualFree(image, 0, MEM_RELEASE);
    puts("PASS: framediet synthetic lifecycle");
    return 0;
}
