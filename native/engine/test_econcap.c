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
    unsigned char *w = alloc(0x140);
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

/* Inline (capacity marker 4) sorted u32 tag vector with one tag. */
static void put_tags(unsigned char *vec, uint32_t tag) {
    memset(vec, 0, 0x20);
    memcpy(vec, &tag, 4);
    uint64_t cap = 4, n = 1;
    memcpy(vec + 0x10, &cap, 8);
    memcpy(vec + 0x18, &n, 8);
}
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
    flags |= 5u << (16 + 4 * k); /* occupied + the flag ComponentRegistry::Find(.., 4) requires */
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
    /* Settings block with the trade reservation duration 3600.0 at +0x5170. */
    unsigned char *settings = alloc(0x5200);
    put_f64(settings + 0x5170, 3600.0);
    put_ptr(image + ECON_SETTINGS_PTR_RVA, settings);

    uintptr_t ecells = make_ware("energycells"), ore = make_ware("ore"), metal = make_ware("refinedmetals");
    put_i32((unsigned char *)ecells + 0xc0, 6); /* WareClass+0xc0 volume */
    put_i32((unsigned char *)ore + 0xc0, 10);
    put_i32((unsigned char *)metal + 0xc0, 14);
    put_tags((unsigned char *)ecells + 0x100, 7); /* WareClass+0x100 tags: 7 container, 8 solid */
    put_tags((unsigned char *)ore + 0x100, 8);
    put_tags((unsigned char *)metal + 0x100, 7);

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
    /* Storage defaults of the operational module: capacity 50000, cargo tag 7 (container). */
    unsigned char *smacro = alloc(0x40), *sdefaults = alloc(0xa80);
    put_ptr(sto1 + 0x60, smacro);
    put_ptr(smacro + 0x18, sdefaults);
    put_i32(sdefaults + 0x9c0, 50000);
    put_tags(sdefaults + 0x9c8, 7);

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
    /* Order registry with one sell offer of refinedmetals (Trade+0x10 seller = the station, +0x18
       buyer empty), and one reservation. */
    unsigned char *omap = alloc(0x158), *oblock = alloc((size_t)32 * 64);
    put_ptr(omap + 0x58, oblock);
    put_ptr(image + ECON_ORDERMAP_PTR_RVA, omap);
    unsigned char *trade = alloc(0x60);
    {
        uint64_t slot = 2, k = slot % 3;
        unsigned char *b = oblock + (slot / 3) * 32;
        put_ptr(b + k * 8, trade);
        uint16_t g = 5;
        memcpy(b + 0x18 + k * 2, &g, 2);
        uint32_t fl;
        memcpy(&fl, b + 0x1c, 4); /* gen[2] shares this word (bits 0..15) */
        fl |= 3u << (16 + 4 * k);
        memcpy(b + 0x1c, &fl, 4);
    }
    uint64_t tid = (2 + 1) | (5ull << 18);
    uint64_t q28 = (uint64_t)(uintptr_t)trade ^ (uint64_t)metal, q30 = q28 ^ 17340ull;
    memcpy(trade + 0x08, &tid, 8);
    memcpy(trade + 0x10, &sid, 8);
    memcpy(trade + 0x28, &q28, 8);
    memcpy(trade + 0x30, &q30, 8);
    uint32_t low = (uint32_t)q28 & 0x7fffffffu, enc;
    enc = low ^ 600u; memcpy(trade + 0x40, &enc, 4);
    enc = low ^ 0u; memcpy(trade + 0x44, &enc, 4);
    enc = low ^ 600u; memcpy(trade + 0x50, &enc, 4);
    uint32_t tflags = 0x40; memcpy(trade + 0x54, &tflags, 4);
    unsigned char *sells = alloc(8);
    memcpy(sells, &tid, 8);
    put_ptr(st + 0x910, sells);
    put_ptr(st + 0x918, sells + 8);
    unsigned char *res = alloc(0x60);
    uint32_t rword = 0x9; memcpy(res, &rword, 4);
    uint64_t reserver = 0x777; memcpy(res + 0x08, &reserver, 8);
    put_ptr(res + 0x18, (void *)ore);
    uint32_t ramount = 300; memcpy(res + 0x20, &ramount, 4);
    put_f64(res + 0x28, 3610.0);
    /* a second entry linked to the trade order: its amount comes from the order */
    uint32_t rword2 = 0x1; memcpy(res + 0x30, &rword2, 4);
    uint64_t reserver2 = 0x778; memcpy(res + 0x38, &reserver2, 8);
    memcpy(res + 0x40, &tid, 8);
    put_ptr(res + 0x48, (void *)metal);
    put_f64(res + 0x58, -1.0);
    put_ptr(st + 0x980, res);
    put_ptr(st + 0x988, res + 0x60);
    memcpy(st + 0x08, &sid, 8);
    /* Production storage allocation (+0x940 -> list, +0x931 valid): refinedmetals 500. */
    unsigned char *alloc_list = alloc(0x30);
    uintptr_t aw[1] = {metal};
    uint32_t aa[1] = {500};
    put_inline_list(alloc_list, 1, aw, aa);
    put_ptr(st + 0x940, alloc_list);
    st[0x931] = 1;

    /* Resolution and decoding. */
    assert(econ_component(sid) == st);
    assert(!econ_component(sid ^ (1ull << 25)));    /* wrong generation */
    assert(!econ_component((sid & ~0x1ffffffull) | 9)); /* free slot */
    {   /* occupied but without flag 4: not found (as Find(.., 4)) */
        unsigned char *probe = alloc(16);
        uint64_t pid = register_id(map, 7, 3, probe);
        unsigned char *bb = block0 + (7 / 3) * 32;
        uint32_t fl;
        memcpy(&fl, bb + 0x1c, 4);
        fl &= ~(4u << (16 + 4 * (7 % 3)));
        memcpy(bb + 0x1c, &fl, 4);
        assert(!econ_component(pid));
    }
    econ_wa l[4];
    assert(econ_list(sto1 + 0x360, l, 4) == 2 && l[0].ware == ecells && l[0].amount == 810 && l[1].amount == 2160);
    char id[64];
    assert(econ_ware_id(metal, id, sizeof(id)) && !strcmp(id, "refinedmetals"));

    assert(econ_order(tid) == trade && !econ_order(tid ^ (1ull << 18)));
    /* First frame: header, ware/method definitions, frame marker, station and module records. */
    {
        int64_t prices[3] = {6600, 4400, 8900};
        memcpy((unsigned char *)metal + 0xc8, prices, sizeof(prices));
    }
    assert(econcap_watch(sid));
    econcap_frame(12.5, 100, 0xabcdef);
    int n = drain(lines, 256);
    assert(n >= 6);
    assert(!strncmp(lines[0], "X4ECON 1 build=23660954", 23));
    assert(count_prefix(lines, n, "P 0x5130 ") == 1);
    for (int i = 0; i < n; ++i)
        if (!strncmp(lines[i], "P ", 2)) assert(strstr(lines[i], " 0x40ac200000000000")); /* +0x5170 = 3600.0 */
    assert(count_prefix(lines, n, "W ") == 3);
    assert(count_prefix(lines, n, "W energycells 6 1 7") == 1 && count_prefix(lines, n, "W refinedmetals 14 1 7") == 1);
    assert(count_prefix(lines, n, "W ore 10 1 8") == 1);
    assert(count_prefix(lines, n, "W refinedmetals 14 1 7 prices 6600 4400 8900") == 1);
    assert(count_prefix(lines, n, "S 0x") == 1);
    for (int i = 0; i < n; ++i)
        if (!strncmp(lines[i], "S ", 2)) assert(strstr(lines[i], " 50000 1 7 2 0 810 1 2160"));
    assert(count_prefix(lines, n, "A 0x") == 1);
    for (int i = 0; i < n; ++i)
        if (!strncmp(lines[i], "A ", 2)) assert(strstr(lines[i], " 1 1 2 500"));
    assert(count_prefix(lines, n, "D 0 ") == 1);
    assert(count_prefix(lines, n, "F 100 0x4029000000000000 0xabcdef") == 1);
    assert(count_prefix(lines, n, "C 0x") == 1 && count_prefix(lines, n, "M 0x123456789 ") == 1);
    assert(count_prefix(lines, n, "O ") == 1 && count_prefix(lines, n, "V ") == 2);
    for (int i = 0; i < n; ++i) {
        if (!strncmp(lines[i], "O ", 2)) assert(strstr(lines[i], " 1 0x140003 2 0xe000005 0x0 ") && strstr(lines[i], " 600 0 600 17340 0x40"));
        if (!strncmp(lines[i], "V ", 2))
            assert(strstr(lines[i], " 0x9 0x777 0x0 1 300 0x40ac340000000000") ||
                   strstr(lines[i], " 0x1 0x778 0x140003 2 0 0xbff0000000000000 600 0x40"));
    }
    for (int i = 0; i < n; ++i)
        if (!strncmp(lines[i], "C ", 2)) {
            /* cargo of the operational storage module only: energycells 810, ore 2160 */
            assert(strstr(lines[i], " 1 2 0 810 1 2160") != NULL);
        }

    /* Unchanged next frame: nothing (not even a frame marker). */
    econcap_frame(12.6, 101, 0xabcdef);
    assert(drain(lines, 256) == 0);

    /* The allocation alone changes (invalidated, then recomputed smaller): a new record. */
    st[0x931] = 0;
    econcap_frame(12.7, 98, 0xabcdef);
    n = drain(lines, 256);
    assert(count_prefix(lines, n, "A 0x") == 1);
    for (int i = 0; i < n; ++i)
        if (!strncmp(lines[i], "A ", 2)) assert(strstr(lines[i], " 0 1 2 500"));
    st[0x931] = 1;
    econcap_frame(12.8, 99, 0xabcdef);
    assert(drain(lines, 256) > 0);
    econcap_frame(12.9, 100, 0xabcdef);
    assert(drain(lines, 256) == 0);

    /* A transition: cycle finished and restarted; cargo changed. */
    put_f64(st + 0xa18, 160.0);
    put_f64(prod + 0x388, 160.0);
    put_f64(prod + 0x390, 310.0);
    uint32_t c3[2] = {720, 1920};
    put_inline_list(sto1 + 0x360, 2, rw, c3);
    econcap_frame(160.0, 102, 0x1);
    n = drain(lines, 256);
    assert(n == 8 && !strncmp(lines[0], "F 102 ", 6) && !strncmp(lines[1], "C ", 2) && !strncmp(lines[2], "A ", 2) &&
           !strncmp(lines[3], "S ", 2) && !strncmp(lines[4], "O ", 2) && !strncmp(lines[5], "V ", 2) &&
           !strncmp(lines[6], "V ", 2) && !strncmp(lines[7], "M ", 2));
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

    printf("PASS: econcap resolves stations and orders by id (generation, Find flags), decodes XOR ware lists, offers and reservations, writes X4ECON records on change, drops stale or unreadable stations\n");
    return 0;
}
