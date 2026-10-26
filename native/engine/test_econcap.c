/* Synthetic test of the economy capture (econcap.h): fake engine objects in a fake image, laid
   out as documented in econcap.h; the sampler must resolve the station by UniverseID, decode the
   XOR-encoded ware lists, name wares and methods, write X4ECON records only on change, drop a
   station whose generation is stale, and never follow a pointer that is not readable. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include "fixedtick.c"

struct lua_State { int unused; };

static unsigned char *heap;
static size_t heap_used;

static unsigned char *alloc(size_t n) {
    unsigned char *p = heap + heap_used;
    heap_used += (n + 15) & ~(size_t)15;
    assert(heap_used < (4u << 20));
    return p;
}

/* Self-relative XOR entry encoding of a ware amount list entry at address a. */
static void put_entry(unsigned char *a, uintptr_t ware, uint32_t amount) {
    uint64_t q = (uint64_t)(uintptr_t)a ^ (uint64_t)ware;
    uint32_t d = ((uint32_t)q & 0x7fffffffu) ^ amount;
    memcpy(a, &q, 8);
    memcpy(a + 8, &d, 4);
}

/* Inline (capacity marker 2) list with up to 2 entries at `list`. */
static void put_inline_list(unsigned char *list, int n, const uintptr_t *wares, const uint32_t *amounts) {
    assert(n <= 2);
    memset(list, 0, 0x30);
    for (int i = 0; i < n; ++i) put_entry(list + 16 * i, wares[i], amounts[i]);
    uint64_t cap = 2, count = (uint64_t)n;
    memcpy(list + 0x20, &cap, 8);
    memcpy(list + 0x28, &count, 8);
}

static uintptr_t make_ware(const char *id) {
    unsigned char *w = alloc(0x80);
    size_t len = strlen(id);
    if (len < 16) {
        memcpy(w + 0x10, id, len + 1);
        uint64_t cap = 15;
        memcpy(w + 0x28, &cap, 8);
    } else {
        unsigned char *heapstr = alloc(len + 1);
        memcpy(heapstr, id, len + 1);
        uintptr_t p = (uintptr_t)heapstr;
        memcpy(w + 0x10, &p, 8);
        uint64_t cap = len;
        memcpy(w + 0x28, &cap, 8);
    }
    uint64_t size = len;
    memcpy(w + 0x20, &size, 8);
    return (uintptr_t)w;
}

static void set_vtable(unsigned char *object, uint32_t rva) {
    uintptr_t vt = (uintptr_t)(image + rva);
    memcpy(object, &vt, 8);
}

static void put_f64(unsigned char *p, double v) { memcpy(p, &v, 8); }
static void put_i32(unsigned char *p, int32_t v) { memcpy(p, &v, 4); }
static void put_ptr(unsigned char *p, const void *v) { uintptr_t x = (uintptr_t)v; memcpy(p, &x, 8); }

/* Registers `object` under slot `slot` (0-based) with generation `gen`; returns the UniverseID. */
static uint64_t register_id(unsigned char *map, uint64_t slot, uint16_t gen, void *object) {
    uint64_t block = slot >> 20, within = slot & 0xfffffu, bucket = within / 3, k = within % 3;
    unsigned char *blk;
    memcpy(&blk, map + 0x58 + block * 8, 8);
    unsigned char *b = blk + bucket * 32;
    put_ptr(b + k * 8, object);
    memcpy(b + 0x18 + k * 2, &gen, 2);
    uint32_t flags;
    memcpy(&flags, b + 0x1c, 4);
    flags |= 1u << (16 + 4 * k);
    memcpy(b + 0x1c, &flags, 4);
    return (slot + 1) | ((uint64_t)gen << 25);
}

static int drain(char lines[][ECON_LINE_BYTES], int max) {
    int n = 0;
    while (n < max && econ_next_line(lines[n], ECON_LINE_BYTES)) ++n;
    return n;
}

static int count_prefix(char lines[][ECON_LINE_BYTES], int n, const char *prefix) {
    int c = 0;
    for (int i = 0; i < n; ++i) c += !strncmp(lines[i], prefix, strlen(prefix));
    return c;
}

static char lines[256][ECON_LINE_BYTES];

int main(void) {
    image = VirtualAlloc(NULL, X4_IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    heap = VirtualAlloc(NULL, 4u << 20, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image && heap);

    /* AutoIDMap with one block. */
    unsigned char *map = alloc(0x158);
    unsigned char *block0 = alloc((size_t)32 * 1024);
    put_ptr(map + 0x58, block0);
    put_ptr(image + ECON_IDMAP_PTR_RVA, map);
    put_ptr(image + ECON_RESEARCH_KIND_RVA, (void *)0x5151);

    uintptr_t ecells = make_ware("energycells"), ore = make_ware("ore"), metal = make_ware("refinedmetals");

    /* Method: refinedmetals from energycells + ore. */
    unsigned char *method = alloc(0x200);
    put_f64(method + 0xe8, 150.0);
    put_ptr(method + 0xb8, (void *)metal);
    put_i32(method + 0xf8, 88);
    put_ptr(method + 0xc0, (void *)0x1234);
    uintptr_t rw[2] = {ecells, ore};
    uint32_t ra[2] = {90, 240};
    put_inline_list(method + 0x170, 2, rw, ra);
    put_inline_list(method + 0x1a0, 0, NULL, NULL);
    put_inline_list(method + 0x1d0, 0, NULL, NULL);

    /* Production module. */
    unsigned char *prod = alloc(0x440);
    set_vtable(prod, ECON_VT_PRODUCTION_RVA);
    uint64_t prod_uid = 0x0123456789ull;
    memcpy(prod + 0x08, &prod_uid, 8);
    put_i32(prod + 0x68, 0);
    put_f64(prod + 0x388, 10.0);
    put_f64(prod + 0x390, 160.0);
    put_f64(prod + 0x398, -1.0);
    put_f64(prod + 0x3a0, 0.0);
    put_i32(prod + 0x3a8, 0);
    put_i32(prod + 0x3ac, 0);
    for (int k = 0; k < 5; ++k) put_f64(prod + 0x3b0 + 8 * k, 1.0);
    unsigned char *items = alloc(0x60);
    put_ptr(items + 0x00, (void *)metal);
    put_ptr(items + 0x08, method);
    for (int k = 0; k < 5; ++k) put_f64(items + 0x10 + 8 * k, 1.0);
    put_i32(items + 0x38, 1);
    items[0x3c] = 1;
    put_ptr(prod + 0x3d8, items);
    put_ptr(prod + 0x3e0, items + 0x60);
    put_i32(prod + 0x3f0, 8);
    put_inline_list(prod + 0x3f8, 0, NULL, NULL);

    /* Two storage modules, one non-operational (ignored). */
    unsigned char *sto1 = alloc(0x3a0), *sto2 = alloc(0x3a0);
    set_vtable(sto1, ECON_VT_STORAGE_RVA);
    set_vtable(sto2, ECON_VT_STORAGE_RVA);
    put_i32(sto1 + 0x68, 0);
    put_i32(sto2 + 0x68, 3);
    uint32_t c1[2] = {810, 2160};
    put_inline_list(sto1 + 0x360, 2, rw, c1);
    uint32_t c2[2] = {5, 5};
    put_inline_list(sto2 + 0x360, 2, rw, c2);

    /* Station with a child container: bucket 8 = production, bucket 10 = storage. */
    unsigned char *st = alloc(0xa60);
    set_vtable(st, ECON_VT_STATION_RVA);
    unsigned char *cc = alloc(0x30), *buckets = alloc(16 * 32);
    unsigned char *prodvec = alloc(8), *stovec = alloc(16);
    put_ptr(prodvec, prod);
    put_ptr(stovec, sto1);
    put_ptr(stovec + 8, sto2);
    put_ptr(buckets + 8 * 32, prodvec);
    put_ptr(buckets + 8 * 32 + 8, prodvec + 8);
    put_ptr(buckets + 10 * 32, stovec);
    put_ptr(buckets + 10 * 32 + 8, stovec + 16);
    put_ptr(cc + 0x08, buckets);
    put_ptr(cc + 0x10, buckets + 16 * 32);
    put_i32(cc + 0x20, 3);
    put_ptr(st + 0xa8, cc);
    put_f64(st + 0xa18, 10.0);
    put_f64(st + 0xa28, -1.0);
    put_f64(st + 0xa30, 160.0);
    st[0xa4c] = 0;
    uint64_t sid = register_id(map, 4, 7, st);
    memcpy(st + 0x08, &sid, 8);

    /* Resolution and decoding. */
    assert(econ_component(sid) == st);
    assert(!econ_component(sid ^ (1ull << 25)));    /* wrong generation */
    assert(!econ_component((sid & ~0x1ffffffull) | 9)); /* free slot */
    econ_wa l[4];
    assert(econ_list(sto1 + 0x360, l, 4) == 2 && l[0].ware == ecells && l[0].amount == 810 && l[1].amount == 2160);
    char id[64];
    assert(econ_ware_id(metal, id, sizeof(id)) && !strcmp(id, "refinedmetals"));

    /* First frame: header, ware/method definitions, frame marker, station and module records. */
    assert(econcap_watch(sid));
    econcap_frame(12.5, 100, 0xabcdef);
    int n = drain(lines, 256);
    assert(n >= 6);
    assert(!strncmp(lines[0], "X4ECON 1 build=23660954", 23));
    assert(count_prefix(lines, n, "W ") == 3);
    assert(count_prefix(lines, n, "D 0 ") == 1);
    assert(count_prefix(lines, n, "F 100 0x4029000000000000 0xabcdef") == 1);
    assert(count_prefix(lines, n, "C 0x") == 1 && count_prefix(lines, n, "M 0x123456789 ") == 1);
    for (int i = 0; i < n; ++i)
        if (!strncmp(lines[i], "C ", 2)) {
            /* cargo of the operational storage module only: energycells 810, ore 2160 */
            assert(strstr(lines[i], " 1 2 0 810 1 2160") != NULL);
        }

    /* Unchanged next frame: nothing (not even a frame marker). */
    econcap_frame(12.6, 101, 0xabcdef);
    assert(drain(lines, 256) == 0);

    /* A transition: cycle finished and restarted; cargo changed. */
    put_f64(st + 0xa18, 160.0);
    put_f64(prod + 0x388, 160.0);
    put_f64(prod + 0x390, 310.0);
    uint32_t c3[2] = {720, 1920};
    put_inline_list(sto1 + 0x360, 2, rw, c3);
    econcap_frame(160.0, 102, 0x1);
    n = drain(lines, 256);
    assert(n == 3 && !strncmp(lines[0], "F 102 ", 6) && !strncmp(lines[1], "C ", 2) && !strncmp(lines[2], "M ", 2));
    assert(strstr(lines[1], " 0 720 1 1920") != NULL);

    /* Station destroyed (generation changed): dropped once, counted, no partial record. */
    uint16_t gen = 8;
    memcpy(block0 + (4 / 3) * 32 + 0x18 + (4 % 3) * 2, &gen, 2);
    econcap_frame(161.0, 103, 0x2);
    assert(drain(lines, 256) == 0);
    assert(econ.dropped_stations == 1 && econ.watch[0].dropped);

    /* A bad pointer inside a station drops it without touching unreadable memory. */
    econcap_disarm();
    gen = 7;
    memcpy(block0 + (4 / 3) * 32 + 0x18 + (4 % 3) * 2, &gen, 2);
    put_ptr(prod + 0x3d8, (void *)0x10);
    put_ptr(prod + 0x3e0, (void *)0x70);
    assert(econcap_watch(sid));
    econcap_frame(162.0, 104, 0x3);
    n = drain(lines, 256);
    assert(count_prefix(lines, n, "C ") == 0 && count_prefix(lines, n, "M ") == 0);
    assert(econ.dropped_stations == 2);

    printf("PASS: econcap resolves stations by UniverseID, decodes XOR ware lists, writes X4ECON records on change, drops stale or unreadable stations\n");
    return 0;
}
