/* Synthetic tests of the mirror capture probe (mirror.h): a fake component tree in a fake image
 * laid out at the pinned offsets, two captured frames, fail-closed verification, and a
 * byte-exact golden file (rust/x4mp-sim/tests/data/mirror_golden.x4cap) that the Rust reader
 * test checks field by field. `test_mirror.exe --write-golden` regenerates the golden file. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include "fixedtick.c"

struct lua_State { int unused; };

static double fake_time(void) { return 123.25; }

/* --- fake objects ------------------------------------------------------------------------------- */

typedef struct { unsigned char *items[8]; } fake_block_items;
typedef struct {
    unsigned char obj[0x28];          /* the container object (+0x08/+0x10 blocks, +0x20 count) */
    unsigned char blocks[4][0x20];    /* block records: items begin/end */
    unsigned char *items[4][8];
    unsigned nitems[4];
} fake_container;

static unsigned char *comp_new(uint32_t vtable_rva, uint64_t uid, int32_t mode, int32_t attention) {
    unsigned char *c = calloc(1, 0x600);
    assert(c);
    *(uintptr_t *)c = (uintptr_t)(image + vtable_rva);
    *(uint64_t *)(c + MO_ID) = uid;
    *(int32_t *)(c + MO_MODE) = mode;
    *(int32_t *)(c + MO_ATTENTION) = attention;
    *(uint8_t *)(c + MO_REGISTERED) = 1;
    return c;
}

static fake_container *container_of(unsigned char *parent, unsigned which) {
    unsigned char **slot = (unsigned char **)(parent + (which ? MO_CHILDREN_SECOND : MO_CHILDREN_FIRST));
    if (!*slot) {
        fake_container *f = calloc(1, sizeof(*f));
        assert(f);
        *slot = f->obj;
    }
    return (fake_container *)*slot;
}

static void container_sync(fake_container *f) {
    unsigned nblocks = 0, count = 0;
    for (unsigned b = 0; b < 4; ++b) {
        if (f->nitems[b] || b < 3) nblocks = b + 1;
        *(unsigned char ***)(f->blocks[b] + MO_BLOCK_ITEMS_BEGIN) = f->items[b];
        *(unsigned char ***)(f->blocks[b] + MO_BLOCK_ITEMS_END) = f->items[b] + f->nitems[b];
        for (unsigned i = 0; i < f->nitems[b]; ++i) count += f->items[b][i] != NULL;
    }
    *(unsigned char **)(f->obj + MO_CONTAINER_BLOCKS_BEGIN) = f->blocks[0];
    *(unsigned char **)(f->obj + MO_CONTAINER_BLOCKS_END) = f->blocks[0] + nblocks * MO_BLOCK_STRIDE;
    *(uint32_t *)(f->obj + MO_CONTAINER_COUNT) = count;
}

/* Appends child (or NULL) to block `block` of the parent's first/second container. */
static void add(unsigned char *parent, unsigned which, unsigned block, unsigned char *child) {
    fake_container *f = container_of(parent, which);
    assert(f->nitems[block] < 8);
    f->items[block][f->nitems[block]++] = child;
    if (child) *(unsigned char **)(child + MO_PARENT) = parent;
    container_sync(f);
}

static void remove_child(unsigned char *parent, unsigned which, unsigned char *child) {
    fake_container *f = container_of(parent, which);
    for (unsigned b = 0; b < 4; ++b)
        for (unsigned i = 0; i < f->nitems[b]; ++i)
            if (f->items[b][i] == child) {
                memmove(&f->items[b][i], &f->items[b][i + 1], (f->nitems[b] - i - 1) * sizeof(void *));
                --f->nitems[b];
                container_sync(f);
                return;
            }
    assert(!"child not found");
}

static void mat(float *m, float base) {
    for (int i = 0; i < 16; ++i) m[i] = base + (float)i * 0.25f;
}

static unsigned char *nav_ctrl(float base) {
    unsigned char *c = calloc(1, 0x100);
    unsigned char *slots = calloc(2, MO_NAV_SLOT_STRIDE);
    assert(c && slots);
    *(uintptr_t *)c = (uintptr_t)(image + MC_NAVIGATION_VTABLE);
    *(unsigned char **)(c + MO_NAV_SLOTS) = slots;
    for (int s = 0; s < 2; ++s) {
        unsigned char *slot = slots + s * MO_NAV_SLOT_STRIDE;
        mat((float *)(slot + MO_SLOT_POSE), base + (float)s * 1000.0f);   /* slot 1 is current */
        for (int k = 0; k < 4; ++k) {
            ((float *)(slot + MO_SLOT_LINEAR))[k] = base + 10.0f * (float)s + (float)k;
            ((float *)(slot + MO_SLOT_ANGULAR))[k] = -(base + 10.0f * (float)s + (float)k) / 64.0f;
        }
        *(uint64_t *)(slot + MO_SLOT_REF) = s ? 0 : 0xdead;
    }
    mat((float *)(c + MO_NAV_CACHE), -base);
    return c;
}

static unsigned char *static_ctrl(float base) {
    unsigned char *c = calloc(1, 0x100);
    assert(c);
    *(uintptr_t *)c = (uintptr_t)(image + MC_STATIC_VTABLE);
    mat((float *)(c + MO_STATIC_POSE), base);
    return c;
}

static unsigned char *faction(const char *id) {
    unsigned char *f = calloc(1, 0x80);
    size_t n = strlen(id);
    assert(f);
    if (n < 16) {
        memcpy(f + MO_FACTION_ID, id, n);
        *(uint64_t *)(f + MO_FACTION_ID_CAPACITY) = 15;
    } else {
        char *heap = calloc(1, 64);
        memcpy(heap, id, n);
        *(char **)(f + MO_FACTION_ID) = heap;
        *(uint64_t *)(f + MO_FACTION_ID_CAPACITY) = 63;
    }
    *(uint64_t *)(f + MO_FACTION_ID_SIZE) = n;
    return f;
}

#define VT_GALAXY 0x2b15e38u
#define VT_CLUSTER 0x2aa2498u
#define VT_SECTOR 0x2b1ea48u
#define VT_ZONE 0x2b66080u
#define VT_SHIP_S 0x2b7feb8u
#define VT_SHIP_M 0x2b82440u
#define VT_STATION 0x2b764d8u
#define VT_MODULE 0x2b41108u
#define VT_DOCKINGBAY 0x2ad9ca0u
#define VT_ASTEROID 0x2ab3148u
#define VT_GATE 0x2b13b50u
#define VT_HIGHWAY 0x2b091e0u
#define VT_SATELLITE 0x2b22880u

static unsigned char *g_galaxy, *g_zone1, *g_zone2, *g_ship1, *g_ship3, *g_bay, *g_ship2;

static void build_scene(void) {
    unsigned char *universe = calloc(1, 0x300);
    *(unsigned char **)(image + MG_UNIVERSE) = universe;
    *(uint32_t *)(image + MG_PATHING_INDEX) = 1;
    *(uint64_t *)(image + MG_SESSION_SEED) = 0x1122334455667788ull;
    unsigned char *argon = faction("argon"), *teladi = faction("teladi_long_faction_id");

    g_galaxy = comp_new(VT_GALAXY, 1, 0, 3);
    *(unsigned char **)(universe + MG_UNIVERSE_GALAXY) = g_galaxy;
    unsigned char *cluster = comp_new(VT_CLUSTER, 2, 0, 3);
    add(g_galaxy, 0, 0, cluster);
    unsigned char *sector1 = comp_new(VT_SECTOR, 3, 0, 5);
    add(cluster, 0, 0, sector1);
    g_zone1 = comp_new(VT_ZONE, 4, 0, 8);
    *(unsigned char **)(g_zone1 + MO_CONTROLLER) = static_ctrl(500.0f);
    add(sector1, 0, 0, g_zone1);

    g_ship1 = comp_new(VT_SHIP_S, 5, 0, 9);
    *(unsigned char **)(g_ship1 + MO_CONTROLLER) = nav_ctrl(7.0f);
    *(double *)(g_ship1 + MO_HULL) = 1234.5;
    *(unsigned char **)(g_ship1 + MO_OWNER_TRUE) = argon;
    {
        void **orders = calloc(3, sizeof(void *));
        *(void ***)(g_ship1 + MO_ORDERS_BEGIN) = orders;
        *(void ***)(g_ship1 + MO_ORDERS_END) = orders + 3;
        *(uint64_t *)(g_ship1 + MO_ORDER_INDEX) = 1;
    }
    add(g_zone1, 0, 0, g_ship1);
    add(g_zone1, 0, 0, NULL); /* null entries are skipped */
    unsigned char *station = comp_new(VT_STATION, 6, 0, 8);
    *(unsigned char **)(station + MO_CONTROLLER) = static_ctrl(-300.0f);
    *(double *)(station + MO_HULL) = 50000.0;
    *(unsigned char **)(station + MO_OWNER_TRUE) = teladi;
    *(unsigned char **)(station + MO_OWNER_COVER) = argon;
    add(g_zone1, 0, 0, station);
    unsigned char *module = comp_new(VT_MODULE, 7, 0, 8);      /* not recorded by default */
    add(station, 0, 0, module);
    g_bay = comp_new(VT_DOCKINGBAY, 8, 0, 8);                  /* not recorded by default */
    add(module, 0, 0, g_bay);
    g_ship2 = comp_new(VT_SHIP_M, 9, 1, 8);                    /* mode 1: reports class object */
    *(double *)(g_ship2 + MO_HULL) = 77.0;
    *(uint8_t *)(g_ship2 + MO_DEATH) = 1;
    add(g_bay, 0, 0, g_ship2);
    /* block 1 stays empty; block 2: four null entries (skipped; the A/B prefetch reaches past
       them), an asteroid and a component with an unknown vtable */
    unsigned char *asteroid = comp_new(VT_ASTEROID, 10, 0, 8);
    *(double *)(asteroid + MO_HULL) = 3.0;
    for (int k = 0; k < 4; ++k) add(g_zone1, 0, 2, NULL);
    add(g_zone1, 0, 2, asteroid);
    add(g_zone1, 0, 2, comp_new(0x100, 11, 0, 8));
    unsigned char *gate = comp_new(VT_GATE, 12, 0, 8);         /* second container: walked after +0xa8 */
    add(g_zone1, 1, 0, gate);

    unsigned char *sector2 = comp_new(VT_SECTOR, 13, 0, 2);
    add(cluster, 0, 1, sector2);
    g_zone2 = comp_new(VT_ZONE, 14, 0, 2);
    add(sector2, 0, 0, g_zone2);
    g_ship3 = comp_new(VT_SHIP_S, 15, 0, 2);
    *(unsigned char **)(g_ship3 + MO_CONTROLLER) = nav_ctrl(-50.0f);
    *(double *)(g_ship3 + MO_HULL) = 600.0;
    *(unsigned char **)(g_ship3 + MO_OWNER_TRUE) = teladi;
    add(g_zone2, 0, 0, g_ship3);

    unsigned char *highway = comp_new(VT_HIGHWAY, 16, 0, 3); /* cluster-level highway */
    *(unsigned char **)(highway + MO_CONTROLLER) = static_ctrl(1000.0f);
    float hoff[4] = {1.5f, -2.5f, 4.0f, 0.0f};                /* Highway::GetLocalTransform adds it to row 0 */
    memcpy(highway + MO_HIGHWAY_OFFSET, hoff, sizeof(hoff));
    add(cluster, 1, 0, highway);
    add(g_galaxy, 1, 0, comp_new(VT_SATELLITE, 17, 0, 3));   /* galaxy-level object */
}

/* --- checks ---------------------------------------------------------------------------------- */

static unsigned char *read_file(const wchar_t *path, size_t *n) {
    HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) return NULL;
    LARGE_INTEGER size;
    GetFileSizeEx(h, &size);
    unsigned char *p = malloc((size_t)size.QuadPart + 1);
    DWORD got = 0;
    ReadFile(h, p, (DWORD)size.QuadPart, &got, NULL);
    CloseHandle(h);
    *n = got;
    return p;
}

static int write_file(const wchar_t *path, const unsigned char *p, size_t n) {
    HANDLE h = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) return 0;
    DWORD done = 0;
    int ok = WriteFile(h, p, (DWORD)n, &done, NULL) && done == n;
    CloseHandle(h);
    return ok;
}

/* Offset just past the HEAD record (the first record after the 16-byte file header). */
static size_t head_end(const unsigned char *p) {
    uint32_t len;
    memcpy(&len, p + 16 + 4, 4);
    return 16 + 16 + (size_t)len;
}

/* The test's two frames: frame 1; then ship1 moves (slot index flips to 0), ship2 undocks out of
   the world, ship3 moves to zone1 (sector 1) in front of the gate container order (appended to
   zone1's first block) and ship1's hull changes; frame 2. */
static void two_frames(void) {
    mirror_frame(1);
    *(uint32_t *)(image + MG_PATHING_INDEX) = 0;
    remove_child(g_bay, 0, g_ship2);
    remove_child(g_zone2, 0, g_ship3);
    add(g_zone1, 0, 0, g_ship3);
    *(double *)(g_ship1 + MO_HULL) = 1000.25;
    mirror_frame(2);
}

/* Walks the records of a capture; returns the number of FRAM records, checks every checksum. */
static unsigned check_records(const unsigned char *p, size_t n, uint64_t *stat_frames) {
    assert(n > 16 && !memcmp(p, "X4MPCAP\0", 8));
    size_t at = 16;
    unsigned frames = 0;
    int head = 0;
    while (at < n) {
        assert(at + 16 <= n);
        uint32_t len;
        memcpy(&len, p + at + 4, 4);
        assert(at + 16 + len <= n);
        uint64_t check;
        memcpy(&check, p + at + 8 + len, 8);
        assert(check == mirror_fnv(MIRROR_FNV_OFFSET, p + at, 8 + (size_t)len));
        if (!memcmp(p + at, "HEAD", 4)) head = 1;
        if (!memcmp(p + at, "FRAM", 4)) ++frames;
        if (!memcmp(p + at, "STAT", 4)) memcpy(stat_frames, p + at + 8, 8);
        at += 16 + len;
    }
    assert(head && at == n);
    return frames;
}

int main(int argc, char **argv) {
    int write_golden = argc > 1 && !strcmp(argv[1], "--write-golden");
    image = VirtualAlloc(NULL, X4_IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image);
    LARGE_INTEGER f;
    QueryPerformanceFrequency(&f);
    qpc_frequency = f.QuadPart;
    mirror_game_time = fake_time;
    build_scene();

    wchar_t exe[MAX_PATH], out[MAX_PATH], golden[MAX_PATH];
    DWORD len = GetModuleFileNameW(NULL, exe, MAX_PATH);
    assert(len && len < MAX_PATH);
    wchar_t *slash = wcsrchr(exe, L'\\');
    assert(slash);
    *slash = 0;                                   /* <root>\.local\engine */
    swprintf(out, MAX_PATH, L"%ls\\test_mirror.x4cap", exe);
    swprintf(golden, MAX_PATH, L"%ls\\..\\..\\rust\\x4mp-sim\\tests\\data\\mirror_golden.x4cap", exe);
    DeleteFileW(out);

    /* Fail closed: the fake image has none of the pinned code, so arming with checks refuses. */
    const char *why = mirror_arm(out, 1, 2, 0, 0);
    assert(why && !strcmp(why, "mirror code signature mismatch"));
    assert(!ms.armed && GetFileAttributesW(out) == INVALID_FILE_ATTRIBUTES);
    /* Parameter refusals. */
    assert(mirror_arm_unchecked(out, 0, 2, 0, 0) && !ms.armed);

    assert(!mirror_arm_unchecked(out, 1, 2, 0, 0));
    assert(ms.armed);
    mirror_frame(1);
    assert(ms.frames_written == 1 && ms.last_rows == 14 && ms.last_roots == 4);
    assert(ms.walk_errors == 1);                  /* the unknown vtable */
    assert(ms.nsample == 5);                      /* ship1, station, ship3, zone1 and the highway */
    /* Frame 2: the changes of two_frames(). */
    *(uint32_t *)(image + MG_PATHING_INDEX) = 0;
    remove_child(g_bay, 0, g_ship2);
    remove_child(g_zone2, 0, g_ship3);
    add(g_zone1, 0, 0, g_ship3);
    *(double *)(g_ship1 + MO_HULL) = 1000.25;
    mirror_frame(2);
    assert(!ms.armed && !ms.file);                /* two frames requested: stopped and closed */
    assert(ms.frames_written == 2 && ms.frames_dropped == 0 && ms.last_rows == 13);
    /* Visit statistics of the two frames: the unknown component once per frame. */
    assert(ms.visited_by_class[MIRROR_UNKNOWN_CLASS] == 2 && ms.frames_plain == 2 && ms.frames_prefetch == 0);
    {
        char top[256], want[32];
        mirror_top_classes(top, sizeof top, 16);
        snprintf(want, sizeof want, "0x%02x:4", MIRROR_CLASS_SECTOR);
        assert(!strncmp(top, "0x", 2) && strstr(top, want));
    }

    size_t n = 0;
    unsigned char *bytes = read_file(out, &n);
    assert(bytes && n > 1000);
    uint64_t stat_frames = 0;
    assert(check_records(bytes, n, &stat_frames) == 2 && stat_frames == 2);
    /* The second arm refuses an existing path. */
    assert(mirror_arm_unchecked(out, 1, 1, 0, 0) != NULL);

    if (write_golden) {
        assert(write_file(golden, bytes, n));
        printf("test_mirror: golden written (%zu bytes)\n", n);
    } else {
        size_t gn = 0;
        unsigned char *g = read_file(golden, &gn);
        if (!g) {
            printf("test_mirror: no golden file; run test_mirror.exe --write-golden\n");
            return 1;
        }
        if (gn != n || memcmp(g, bytes, n)) {
            printf("test_mirror: output differs from the golden file (%zu vs %zu bytes)\n", n, gn);
            return 1;
        }
    }
    /* A/B prefetch (flags 4) on a rebuilt scene: every second frame prefetches, and the capture
       equals the plain one after the HEAD record (which carries the flags). */
    build_scene();
    wchar_t ab[MAX_PATH];
    swprintf(ab, MAX_PATH, L"%ls\\test_mirror_ab.x4cap", exe);
    DeleteFileW(ab);
    assert(!mirror_arm_unchecked(ab, 1, 2, MIRROR_AB_PREFETCH, 0));
    two_frames();
    assert(!ms.armed && ms.frames_written == 2 && ms.frames_plain == 1 && ms.frames_prefetch == 1);
    size_t abn = 0;
    unsigned char *ab_bytes = read_file(ab, &abn);
    assert(ab_bytes && abn == n && head_end(ab_bytes) == head_end(bytes));
    assert(!memcmp(ab_bytes + head_end(ab_bytes), bytes + head_end(bytes), n - head_end(bytes)));
    assert(memcmp(ab_bytes, bytes, head_end(bytes)) != 0); /* the flags differ */

    /* Round-robin visits only selected sector subtrees; galaxy/cluster objects remain current.
       A full initial frame establishes root identity; two-frame holds support later local replay.
       Sector selection never holds an engine pointer between frames. */
    build_scene();
    wchar_t rr[MAX_PATH];
    swprintf(rr, MAX_PATH, L"%ls\\test_mirror_rr.x4cap", exe);
    DeleteFileW(rr);
    mirror_rr_next = (mirror_rr_opts){1, 1, 0};
    assert(!mirror_arm_unchecked(rr, 1, 5, 0, 0));
    assert(!mirror_rr_next.sectors && ms.rr.sectors == 1);
    mirror_frame(1);
    assert(!ms.subset_now && ms.sectors_last == 2 && ms.nroots == 4);
    uint64_t full_visited = ms.last_visited;
    for (unsigned fnum = 2; fnum <= 5; ++fnum) {
        mirror_frame(fnum);
        assert(ms.subset_now && ms.nroots == 3 && ms.sectors_last == 2);
        assert(ms.roots[0].uid == 1 && ms.roots[1].uid == 2);
        assert(ms.roots[2].uid == (fnum <= 3 ? 3 : 13));
        assert(ms.last_visited < full_visited);
        int galaxy_object = 0, cluster_object = 0;
        for (size_t i = 0; i < ms.nrows; ++i) {
            if (ms.rows[i].uid == 17) galaxy_object = 1;
            if (ms.rows[i].uid == 16) cluster_object = 1;
            assert(ms.rows[i].uid != (fnum <= 3 ? 15 : 5));
        }
        assert(galaxy_object && cluster_object);
    }
    assert(!ms.armed && ms.frames_written == 5 && ms.frames_subset == 4);
    size_t rrn = 0;
    unsigned char *rrbytes = read_file(rr, &rrn);
    assert(check_records(rrbytes, rrn, &stat_frames) == 5 && stat_frames == 5);
    unsigned frame_ix = 0;
    for (size_t at = 16; at < rrn;) {
        uint32_t len;
        memcpy(&len, rrbytes + at + 4, 4);
        if (!memcmp(rrbytes + at, "FRAM", 4)) {
            uint32_t flags;
            memcpy(&flags, rrbytes + at + 8 + 20, 4);
            assert(flags == (frame_ix++ ? MIRROR_FRAME_SUBSET : 0));
        }
        at += 16 + len;
    }
    free(rrbytes);

    /* Hot-sector inclusion is additive, and rotation wraps without starving the cold sector. */
    DeleteFileW(rr);
    mirror_rr_next = (mirror_rr_opts){1, 0, 5};
    assert(!mirror_arm_unchecked(rr, 1, 4, 0, 0));
    mirror_frame(1);
    mirror_frame(2);
    assert(ms.nroots == 3 && ms.roots[2].uid == 3);
    mirror_frame(3);
    assert(ms.nroots == 4 && ms.roots[2].uid == 3 && ms.roots[3].uid == 13);
    mirror_frame(4);
    assert(ms.nroots == 3 && ms.roots[2].uid == 3 && ms.rr_start == 1);
    /* Sector births cannot alias an old rotation slot and exceed the cold-sector budget. */
    ms.rr.hot_attention = 0;
    assert(mirror_rr_selected(1, 2));
    assert(!mirror_rr_selected(0, 2));
    assert(!mirror_rr_selected(3, 2));

    /* A new ordinary arm cannot accidentally inherit the previous round-robin setting. */
    DeleteFileW(rr);
    assert(!mirror_arm_unchecked(rr, 1, 1, 0, 0));
    mirror_frame(1);
    assert(!ms.rr.sectors && !ms.subset_now && ms.nroots == 4);

    printf("test_mirror: all passed (%zu bytes, layout %s)\n", n, MIRROR_LAYOUT_SHA256);
    return 0;
}
