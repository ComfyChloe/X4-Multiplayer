/* Economy capture for the engine rewrite program (economy agent; included by fixedtick.c).
 *
 * A data-only sampler of production modules and station cargo, for the X4ECON replay in
 * rust/x4mp-economy (capture.rs documents the record format). It runs in the fixed-tick
 * probe's slot-5 callback on the MoveWorker, after the universe update of the frame, and:
 *   - never writes engine memory and never calls engine code;
 *   - resolves watched stations from their UniverseID through the AutoIDMap layout
 *     (generation-checked, so a destroyed station is dropped, not misread);
 *   - accepts an object only when its primary vtable is the build-pinned one, and checks
 *     every pointer it follows against the readable regions of the frame (VirtualQuery,
 *     cached per frame); any mismatch drops that station (counted).
 * Production in X4 is caught up lazily (U::Container::UpdateProduction 0x41a910, time stamp
 * at Container+0xa18), so a station's record changes only when something read the station.
 * The sampler writes a station only when one of its captured fields changed.
 *
 * Layout (X4 9.00, Steam build 23660954; reports/REWRITE-ECONOMY.md section 3):
 *   Container: +0x931 u8 production allocation valid, +0x940 -> production allocation ware
 *     list (NULL = empty; 0x420aa0 recomputes it when +0x931 is clear),
 *   Container: +0x08 UniverseID, +0xa8 child container (+0x08 bucket vector begin, +0x10 end,
 *     +0x20 int count; bucket = 32 bytes {Component** begin, end, ...}; production modules
 *     from bucket 8, storage modules from bucket 10, iteration continues into later buckets
 *     with a class filter), +0xa18 last UpdateProduction time, +0xa28/+0xa30 f64, +0xa4c u8.
 *   Module: +0x68 u32 component state (0 = operational).
 *   StorageModule: +0x360 ware amount list (station cargo = sum over operational modules),
 *     +0x60 macro -> +0x18 storage defaults: +0x9c0 capacity (i32 volume), +0x9c8 cargo tags.
 *   WareClass: +0xc0 volume, +0x100 tags (sorted u32 vector; a storage module accepts a ware when
 *     their tag sets intersect, 0x806e10).
 *   ProductionModule: +0x388 start, +0x390 next, +0x398 paused since, +0x3a0 paused total (f64),
 *     +0x3a8 item, +0x3ac cycle (i32), +0x3b0..+0x3d8 factors (5 x f64), +0x3d8/+0x3e0 item
 *     vector (0x60 each), +0x3f0 state, +0x3f8 pending ware amount list.
 *   Item: +0x00 WareClass*, +0x08 WareProduction*, +0x10..+0x30 factors (5 x f64), +0x38 cycles,
 *     +0x3c repeat (u8).
 *   WareProduction: +0xb8 product WareClass*, +0xc0 kind pointer (== *(0x2f61000): research),
 *     +0xe8 time, +0xf8 amount (i32), +0x170/+0x1a0/+0x1d0 resource lists.
 *   WareClass: +0x10 id (MSVC std::string), +0xc0 volume (i32).
 *   Ware amount list (small vector): data inline at +0 when +0x20 == 2, else pointer at +0;
 *     count at +0x28; entry of 16 bytes at address A: q = *(u64*)A, ware = A ^ q,
 *     amount = ((u32)q & 0x7fffffff) ^ *(u32*)(A + 8).
 *   AutoIDMap at *(image + 0x6cf1500): 32 block pointers at +0x58, 2^20 slots per block,
 *     3 slots per 32-byte bucket {ptr[3]; u16 gen[3] at +0x18; flags u32 at +0x1c, 4 bits per
 *     slot from bit 16, bit 0 = occupied, Find requires flag 4}; id bits 0..24 = slot + 1,
 *     bits 25..40 = generation.
 *   Offers: Container+0x8f8 and +0x910, vectors of order ids resolved in the order registry
 *     *(image + 0x6cf1750) (same bucket layout, 8 blocks of 2^15, id bits 0..17 = slot + 1, bits
 *     18.. = generation, required flag 2). Trade: +0x10 seller, +0x18 buyer (component ids; a station's
 *     sell offer has +0x10 = station, its buy offer +0x18 = station), +0x28 ware tag
 *     (= Trade ^ ware), +0x30 price tag (= tag ^ unit price in cents), +0x40 amount, +0x44 minimum,
 *     +0x50 desired (each XOR low31(tag)), +0x54 flags.
 *   Reservations: Container+0x980..+0x988, 48 bytes {u32 word; u64 reserver @8; u64 order @0x10;
 *     WareClass* @0x18; u32 amount @0x20; f64 expiry @0x28}.
 */

#include <stdarg.h>

#define ECON_MAX_WATCH 512
#define ECON_MAX_MODULES 64
#define ECON_MAX_ITEMS 16
#define ECON_MAX_LIST 64
#define ECON_MAX_WARES 1024
#define ECON_MAX_METHODS 1024
#define ECON_RING_BYTES (4u << 20)
#define ECON_LINE_BYTES 8192

#define ECON_IDMAP_PTR_RVA 0x6cf1500u
#define ECON_VT_STATION_RVA 0x2b764d8u
#define ECON_VT_PRODUCTION_RVA 0x2b2e9c0u
#define ECON_VT_STORAGE_RVA 0x2b74548u
#define ECON_RESEARCH_KIND_RVA 0x2f61000u
#define ECON_ORDERMAP_PTR_RVA 0x6cf1750u
#define ECON_SETTINGS_PTR_RVA 0x6cf1538u
#define ECON_MAX_OFFERS 256
#define ECON_MAX_RESERVATIONS 512

typedef struct { uint64_t ware; int32_t amount; } econ_wa;

typedef struct {
    uint64_t uid;
    uint64_t last_hash;
    int dropped;
} econ_watch_entry;

typedef struct {
    int armed;
    int watched;
    econ_watch_entry watch[ECON_MAX_WATCH];
    uintptr_t wares[ECON_MAX_WARES];
    int nwares;
    uintptr_t methods[ECON_MAX_METHODS];
    int nmethods;
    char *ring;
    size_t head, tail; /* bytes; lines end with '\n' */
    uint64_t records, dropped_lines, dropped_stations, frames;
    char line[ECON_LINE_BYTES];
    size_t len;
    char frame_marker[96]; /* written before the first record of a frame */
    int marker_pending;
} econ_state;

static econ_state econ;

/* --- memory helpers (readable() comes from fixedtick.c) ---------------------------------- */

/* Readable regions seen in this frame (VirtualQuery results), so that the sampler does not make
   one system call per field: a lane run with 12 watched stations spent a quarter of the
   MoveWorker time in VirtualQuery (stress3). Same acceptance rule as readable(): committed,
   no guard/no-access, a readable protection, and the range inside one region. Reset at the
   start of every frame. */
#define ECON_REGIONS 16
static struct { uintptr_t base, end; } econ_region[ECON_REGIONS];
static int econ_nregions, econ_region_next;
static uint64_t econ_region_queries;

static int econ_readable(const void *p, size_t n) {
    uintptr_t a = (uintptr_t)p, e = a + n;
    if (!p || e < a) return 0;
    for (int i = 0; i < econ_nregions; ++i)
        if (a >= econ_region[i].base && e <= econ_region[i].end) return 1;
    MEMORY_BASIC_INFORMATION m;
    ++econ_region_queries;
    if (!VirtualQuery(p, &m, sizeof(m))) return 0;
    if (m.State != MEM_COMMIT || (m.Protect & (PAGE_GUARD | PAGE_NOACCESS))) return 0;
    DWORD pr = m.Protect & 0xff;
    if (!(pr == PAGE_READONLY || pr == PAGE_READWRITE || pr == PAGE_WRITECOPY || pr == PAGE_EXECUTE_READ ||
          pr == PAGE_EXECUTE_READWRITE || pr == PAGE_EXECUTE_WRITECOPY))
        return 0;
    uintptr_t base = (uintptr_t)m.BaseAddress, end = base + m.RegionSize;
    if (e > end) return 0;
    int slot = econ_region_next++ % ECON_REGIONS;
    econ_region[slot].base = base;
    econ_region[slot].end = end;
    if (econ_nregions < ECON_REGIONS) ++econ_nregions;
    return 1;
}

static void econ_regions_reset(void) {
    econ_nregions = 0;
    econ_region_next = 0;
}

static int econ_read(const void *p, void *out, size_t n) {
    if (!p || !econ_readable(p, n)) return 0;
    memcpy(out, p, n);
    return 1;
}

static uint64_t econ_q(const unsigned char *p, int *ok) {
    uint64_t v = 0;
    if (!econ_read(p, &v, 8)) *ok = 0;
    return v;
}

static uint32_t econ_d(const unsigned char *p, int *ok) {
    uint32_t v = 0;
    if (!econ_read(p, &v, 4)) *ok = 0;
    return v;
}

static int econ_vtable_is(const unsigned char *object, uint32_t rva) {
    uintptr_t vt = 0;
    return object && econ_read(object, &vt, sizeof(vt)) && vt == (uintptr_t)(image + rva);
}

/* Component pointer for a UniverseID, or NULL (stale generation, free slot, bad memory). */
static unsigned char *econ_component(uint64_t id) {
    int ok = 1;
    unsigned char *map = (unsigned char *)(uintptr_t)econ_q(image + ECON_IDMAP_PTR_RVA, &ok);
    uint64_t slot1 = id & 0x1ffffffu;
    if (!ok || !map || !slot1) return NULL;
    uint64_t slot = slot1 - 1, block = slot >> 20, within = slot & 0xfffffu, bucket = within / 3, k = within % 3;
    if (block >= 32) return NULL;
    unsigned char *blk = (unsigned char *)(uintptr_t)econ_q(map + 0x58 + block * 8, &ok);
    if (!ok || !blk) return NULL;
    unsigned char b[32];
    if (!econ_read(blk + bucket * 32, b, sizeof(b))) return NULL;
    uint16_t gen;
    memcpy(&gen, b + 0x18 + k * 2, 2);
    uint32_t flags;
    memcpy(&flags, b + 0x1c, 4);
    /* ComponentRegistry::Find 0xced70 as the FFI calls it: occupied (bit 0) and required flag 4. */
    uint32_t slot_flags = (flags >> (16 + 4 * k)) & 0xfu;
    if (gen != (uint16_t)((id >> 25) & 0xffffu) || !(slot_flags & 1u) || (slot_flags & 4u) != 4u) return NULL;
    uintptr_t ptr;
    memcpy(&ptr, b + k * 8, 8);
    return (unsigned char *)ptr;
}

/* Trade/order object for an order id (registry *(0x6cf1750), the lookup 0x33d420 does with
   required flag 2): 18-bit slot + 1, 16-bit generation from bit 18, 8 blocks of 2^15 slots. */
static unsigned char *econ_order(uint64_t id) {
    int ok = 1;
    unsigned char *map = (unsigned char *)(uintptr_t)econ_q(image + ECON_ORDERMAP_PTR_RVA, &ok);
    uint64_t slot1 = id & 0x3ffffu;
    if (!ok || !map || !slot1) return NULL;
    uint64_t slot = slot1 - 1, block = slot >> 15, within = slot & 0x7fffu, bucket = within / 3, k = within % 3;
    if (block >= 8) return NULL;
    unsigned char *blk = (unsigned char *)(uintptr_t)econ_q(map + 0x58 + block * 8, &ok);
    if (!ok || !blk) return NULL;
    unsigned char b[32];
    if (!econ_read(blk + bucket * 32, b, sizeof(b))) return NULL;
    uint16_t gen;
    memcpy(&gen, b + 0x18 + k * 2, 2);
    uint32_t flags;
    memcpy(&flags, b + 0x1c, 4);
    uint32_t slot_flags = (flags >> (16 + 4 * k)) & 0xfu;
    if (gen != (uint16_t)((id >> 18) & 0xffffu) || !(slot_flags & 1u) || (slot_flags & 2u) != 2u) return NULL;
    uintptr_t ptr;
    memcpy(&ptr, b + k * 8, 8);
    return (unsigned char *)ptr;
}

/* Decodes a ware amount list; returns the entry count or -1. */
static int econ_list(const unsigned char *list, econ_wa *out, int max) {
    int ok = 1;
    uint64_t cap = econ_q(list + 0x20, &ok), n = econ_q(list + 0x28, &ok);
    if (!ok || n > (uint64_t)max) return -1;
    const unsigned char *data = cap == 2 ? list : (const unsigned char *)(uintptr_t)econ_q(list, &ok);
    if (!ok || (n && (!data || !econ_readable(data, (size_t)n * 16)))) return -1;
    for (uint64_t i = 0; i < n; ++i) {
        const unsigned char *a = data + i * 16;
        uint64_t q;
        uint32_t d;
        memcpy(&q, a, 8);
        memcpy(&d, a + 8, 4);
        out[i].ware = (uint64_t)(uintptr_t)a ^ q;
        out[i].amount = (int32_t)(((uint32_t)q & 0x7fffffffu) ^ d);
    }
    return (int)n;
}

/* Sorted u32 tag vector (inline capacity 4: data at +0 when +0x10 == 4, else a pointer at +0;
   count at +0x18), as WareClass+0x100 and the storage defaults +0x9c8 hold them (the engine's
   any-intersection test 0xcb100 walks two such vectors). Returns the count, -1 on failure. */
static int econ_tags(const unsigned char *vec, uint32_t *out, int max) {
    int ok = 1;
    uint64_t cap = econ_q(vec + 0x10, &ok), n = econ_q(vec + 0x18, &ok);
    if (!ok || n > (uint64_t)max) return -1;
    const unsigned char *data = cap == 4 ? vec : (const unsigned char *)(uintptr_t)econ_q(vec, &ok);
    if (!ok || (n && (!data || !econ_read(data, out, (size_t)n * 4)))) return -1;
    return (int)n;
}

/* Ware id string (MSVC std::string at ware+0x10) into buf; returns 0 on failure. */
static int econ_ware_id(uintptr_t ware, char *buf, size_t cap) {
    int ok = 1;
    const unsigned char *s = (const unsigned char *)ware + 0x10;
    uint64_t size = econ_q(s + 0x10, &ok), capacity = econ_q(s + 0x18, &ok);
    if (!ok || size == 0 || size >= cap || size > 128) return 0;
    const unsigned char *chars = capacity >= 16 ? (const unsigned char *)(uintptr_t)econ_q(s, &ok) : s;
    if (!ok || !econ_read(chars, buf, (size_t)size)) return 0;
    buf[size] = 0;
    for (uint64_t i = 0; i < size; ++i)
        if ((unsigned char)buf[i] <= ' ' || (unsigned char)buf[i] > '~') return 0;
    return 1;
}

/* --- output ---------------------------------------------------------------------------- */

static void econ_put(const char *fmt, ...) {
    if (econ.len >= sizeof(econ.line) - 1) return;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(econ.line + econ.len, sizeof(econ.line) - econ.len, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    econ.len += (size_t)n;
    if (econ.len >= sizeof(econ.line)) econ.len = sizeof(econ.line) - 1;
}

static int econ_ring_put(const char *text, size_t n) {
    if (!econ.ring || n == 0 || econ.head - econ.tail + n + 1 > ECON_RING_BYTES) return 0;
    for (size_t i = 0; i < n; ++i) econ.ring[(econ.head + i) % ECON_RING_BYTES] = text[i];
    econ.ring[(econ.head + n) % ECON_RING_BYTES] = '\n';
    econ.head += n + 1;
    return 1;
}

/* Moves the current line into the ring (dropped when full or truncated); a pending frame
   marker goes first. */
static void econ_end_line(void) {
    size_t n = econ.len;
    econ.len = 0;
    if (n == 0 || n >= sizeof(econ.line) - 1) { ++econ.dropped_lines; return; }
    if (econ.marker_pending) {
        if (!econ_ring_put(econ.frame_marker, strlen(econ.frame_marker))) { ++econ.dropped_lines; return; }
        econ.marker_pending = 0;
    }
    if (!econ_ring_put(econ.line, n)) { ++econ.dropped_lines; return; }
    ++econ.records;
}

/* Next complete line from the ring into out (without '\n'); 0 when empty. */
static size_t econ_next_line(char *out, size_t cap) {
    size_t n = 0;
    while (econ.tail + n < econ.head && econ.ring[(econ.tail + n) % ECON_RING_BYTES] != '\n') ++n;
    if (econ.tail + n >= econ.head) return 0;
    size_t copy = n < cap - 1 ? n : cap - 1;
    for (size_t i = 0; i < copy; ++i) out[i] = econ.ring[(econ.tail + i) % ECON_RING_BYTES];
    out[copy] = 0;
    econ.tail += n + 1;
    return copy ? copy : 1;
}

static void econ_put_f64(double v) {
    uint64_t b;
    memcpy(&b, &v, 8);
    econ_put(" 0x%llx", (unsigned long long)b);
}

/* Index of a ware in the capture's ware table; emits the W line on first use, -1 on failure. */
static int econ_ware_index(uintptr_t ware) {
    for (int i = 0; i < econ.nwares; ++i)
        if (econ.wares[i] == ware) return i;
    char id[160];
    if (econ.nwares >= ECON_MAX_WARES || !econ_ware_id(ware, id, sizeof(id))) return -1;
    econ.wares[econ.nwares] = ware;
    size_t keep = econ.len;
    char saved[ECON_LINE_BYTES];
    memcpy(saved, econ.line, keep);
    int ok = 1;
    int32_t volume = (int32_t)econ_d((const unsigned char *)ware + 0xc0, &ok);
    /* Capture the loaded game's reference prices, rather than assuming an extracted
       wares.xml includes every content patch. WareClass+0xc8/+0xd0/+0xd8: avg/min/max. */
    int prices_ok = 1;
    int64_t price_avg = (int64_t)econ_q((const unsigned char *)ware + 0xc8, &prices_ok);
    int64_t price_min = (int64_t)econ_q((const unsigned char *)ware + 0xd0, &prices_ok);
    int64_t price_max = (int64_t)econ_q((const unsigned char *)ware + 0xd8, &prices_ok);
    uint32_t tags[16];
    int nt = econ_tags((const unsigned char *)ware + 0x100, tags, 16);
    econ.len = 0;
    econ_put("W %s %d %d", id, ok ? volume : 0, nt < 0 ? 0 : nt);
    for (int i = 0; i < nt; ++i) econ_put(" %u", tags[i]);
    if (prices_ok)
        econ_put(" prices %lld %lld %lld", (long long)price_avg, (long long)price_min, (long long)price_max);
    econ_end_line();
    memcpy(econ.line, saved, keep);
    econ.len = keep;
    return econ.nwares++;
}

/* Writes " n {ware# amount}*" for a list; returns 0 when a ware cannot be named. */
static int econ_put_list(const econ_wa *l, int n) {
    econ_put(" %d", n);
    for (int i = 0; i < n; ++i) {
        int w = econ_ware_index((uintptr_t)l[i].ware);
        if (w < 0) return 0;
        econ_put(" %d %d", w, l[i].amount);
    }
    return 1;
}

/* Index of a production method; emits the D line on first use, -1 on failure. */
static int econ_method_index(uintptr_t m) {
    for (int i = 0; i < econ.nmethods; ++i)
        if (econ.methods[i] == m) return i;
    if (econ.nmethods >= ECON_MAX_METHODS) return -1;
    const unsigned char *p = (const unsigned char *)m;
    int ok = 1;
    double time;
    if (!econ_read(p + 0xe8, &time, 8)) return -1;
    uintptr_t product = (uintptr_t)econ_q(p + 0xb8, &ok);
    int32_t amount = (int32_t)econ_d(p + 0xf8, &ok);
    uintptr_t kind = (uintptr_t)econ_q(p + 0xc0, &ok);
    uintptr_t research = (uintptr_t)econ_q(image + ECON_RESEARCH_KIND_RVA, &ok);
    if (!ok) return -1;
    econ_wa lists[3][ECON_MAX_LIST];
    int counts[3];
    static const unsigned offsets[3] = {0x170, 0x1a0, 0x1d0};
    for (int k = 0; k < 3; ++k)
        if ((counts[k] = econ_list(p + offsets[k], lists[k], ECON_MAX_LIST)) < 0) return -1;
    int pw = econ_ware_index(product);
    if (pw < 0) return -1;
    size_t keep = econ.len;
    char saved[ECON_LINE_BYTES];
    memcpy(saved, econ.line, keep);
    econ.len = 0;
    int index = econ.nmethods;
    econ_put("D %d", index);
    econ_put_f64(time);
    econ_put(" %d %d %d", pw, amount, kind == research);
    int good = 1;
    for (int k = 0; k < 3 && good; ++k) good = econ_put_list(lists[k], counts[k]);
    if (good) econ_end_line(); else econ.len = 0;
    memcpy(econ.line, saved, keep);
    econ.len = keep;
    if (!good) return -1;
    econ.methods[econ.nmethods++] = m;
    return index;
}

/* --- sampling --------------------------------------------------------------------------- */

static uint64_t econ_hash_bytes(uint64_t h, const void *p, size_t n) {
    const unsigned char *b = p;
    for (size_t i = 0; i < n; ++i) { h ^= b[i]; h *= 0x100000001b3ull; }
    return h;
}

/* Ware amount lists field by field: econ_wa has tail padding that is never written. */
static uint64_t econ_hash_wa(uint64_t h, const econ_wa *l, int n) {
    for (int i = 0; i < n; ++i) {
        h = econ_hash_bytes(h, &l[i].ware, sizeof(l[i].ware));
        h = econ_hash_bytes(h, &l[i].amount, sizeof(l[i].amount));
    }
    return h;
}

/* Components of a child-container bucket and the following buckets whose primary vtable is
   `vtable_rva` and whose state (+0x68) is 0, in container order. Returns the count or -1. */
static int econ_children(const unsigned char *container, unsigned first_bucket, uint32_t vtable_rva,
                         const unsigned char **out, int max) {
    int ok = 1;
    const unsigned char *cc = (const unsigned char *)(uintptr_t)econ_q(container + 0xa8, &ok);
    if (!ok) return -1;
    if (!cc) return 0;
    uint64_t begin = econ_q(cc + 0x08, &ok), end = econ_q(cc + 0x10, &ok);
    uint32_t count = econ_d(cc + 0x20, &ok);
    if (!ok) return -1;
    if (!count || end < begin || (end - begin) / 32 <= first_bucket) return 0;
    int n = 0;
    for (uint64_t b = begin + first_bucket * 32ull; b < end; b += 32) {
        uint64_t vb = econ_q((const unsigned char *)(uintptr_t)b, &ok), ve = econ_q((const unsigned char *)(uintptr_t)b + 8, &ok);
        if (!ok || ve < vb || (ve - vb) / 8 > 4096) return -1;
        for (uint64_t e = vb; e < ve; e += 8) {
            const unsigned char *c = (const unsigned char *)(uintptr_t)econ_q((const unsigned char *)(uintptr_t)e, &ok);
            if (!ok) return -1;
            if (!c || !econ_vtable_is(c, vtable_rva)) continue;
            if (econ_d(c + 0x68, &ok) != 0 || !ok) { ok = 1; continue; }
            if (n >= max) return -1;
            out[n++] = c;
        }
    }
    return n;
}

/* Builds the C and M records of one station into the ring; returns 0 if it must be dropped. */
static int econ_station(econ_watch_entry *w, int force) {
    unsigned char *st = econ_component(w->uid);
    if (!st || !econ_vtable_is(st, ECON_VT_STATION_RVA)) return 0;
    int ok = 1;
    double last, a28, a30;
    uint8_t a4c = 0;
    if (!econ_read(st + 0xa18, &last, 8) || !econ_read(st + 0xa28, &a28, 8) || !econ_read(st + 0xa30, &a30, 8) ||
        !econ_read(st + 0xa4c, &a4c, 1))
        return 0;
    const unsigned char *storage[ECON_MAX_MODULES], *prod[ECON_MAX_MODULES];
    int ns = econ_children(st, 10, ECON_VT_STORAGE_RVA, storage, ECON_MAX_MODULES);
    int np = econ_children(st, 8, ECON_VT_PRODUCTION_RVA, prod, ECON_MAX_MODULES);
    if (ns < 0 || np < 0) return 0;
    /* Cargo: sum of the storage modules' lists, merged by ware in first-seen order (0x8f12e0). */
    econ_wa cargo[ECON_MAX_LIST * 4];
    int nc = 0;
    for (int s = 0; s < ns; ++s) {
        econ_wa l[ECON_MAX_LIST];
        int n = econ_list(storage[s] + 0x360, l, ECON_MAX_LIST);
        if (n < 0) return 0;
        for (int i = 0; i < n; ++i) {
            int j = 0;
            while (j < nc && cargo[j].ware != l[i].ware) ++j;
            if (j == nc) {
                if (nc >= (int)(sizeof(cargo) / sizeof(cargo[0]))) return 0;
                cargo[nc++] = l[i];
            } else {
                cargo[j].amount += l[i].amount;
            }
        }
    }
    /* Storage modules: capacity (storage defaults +0x9c0, defaults = *(*(module+0x60)+0x18), as
       0x85cb80 returns them for storage macros), cargo tags (+0x9c8) and own contents (+0x360). */
    static int32_t sto_cap[ECON_MAX_MODULES];
    static uint32_t sto_tags[ECON_MAX_MODULES][8];
    static int sto_ntags[ECON_MAX_MODULES], sto_n[ECON_MAX_MODULES];
    static econ_wa sto_list[ECON_MAX_MODULES][ECON_MAX_LIST];
    for (int m = 0; m < ns; ++m) {
        uint64_t macro = econ_q(storage[m] + 0x60, &ok);
        uint64_t defaults = ok && macro ? econ_q((const unsigned char *)(uintptr_t)macro + 0x18, &ok) : 0;
        if (!ok || !defaults) return 0;
        sto_cap[m] = (int32_t)econ_d((const unsigned char *)(uintptr_t)defaults + 0x9c0, &ok);
        sto_ntags[m] = econ_tags((const unsigned char *)(uintptr_t)defaults + 0x9c8, sto_tags[m], 8);
        sto_n[m] = econ_list(storage[m] + 0x360, sto_list[m], ECON_MAX_LIST);
        if (!ok || sto_ntags[m] < 0 || sto_n[m] < 0) return 0;
    }
    /* Change detection over everything the record contains. */
    uint64_t h = 0xcbf29ce484222325ull;
    h = econ_hash_bytes(h, &last, 8);
    h = econ_hash_bytes(h, &a28, 8);
    h = econ_hash_bytes(h, &a30, 8);
    h = econ_hash_wa(h, cargo, nc);
    for (int m = 0; m < ns; ++m) {
        h = econ_hash_bytes(h, &sto_cap[m], 4);
        h = econ_hash_bytes(h, sto_tags[m], (size_t)sto_ntags[m] * 4);
        h = econ_hash_wa(h, sto_list[m], sto_n[m]);
    }
    unsigned char raw[ECON_MAX_MODULES][0x70];
    for (int m = 0; m < np; ++m) {
        if (!econ_read(prod[m] + 0x388, raw[m], 0x70)) return 0; /* +0x388..+0x3f8 */
        h = econ_hash_bytes(h, raw[m], 0x70);
        econ_wa pend[ECON_MAX_LIST];
        int npend = econ_list(prod[m] + 0x3f8, pend, ECON_MAX_LIST);
        if (npend < 0) return 0;
        h = econ_hash_wa(h, pend, npend);
    }
    /* Offers (Container+0x8f8 / +0x910: vectors of order ids) and reservations (+0x980, 48 bytes). */
    static unsigned char offers[ECON_MAX_OFFERS][0x58];
    static uint8_t offer_list[ECON_MAX_OFFERS];
    static uint64_t offer_id[ECON_MAX_OFFERS];
    static uintptr_t offer_ptr[ECON_MAX_OFFERS];
    int no = 0;
    for (int list = 0; list < 2; ++list) {
        uint64_t vb = econ_q(st + 0x8f8 + 0x18 * list, &ok), ve = econ_q(st + 0x900 + 0x18 * list, &ok);
        if (!ok || ve < vb || (ve - vb) % 8 || (ve - vb) / 8 > ECON_MAX_OFFERS) return 0;
        for (uint64_t e = vb; e < ve; e += 8) {
            uint64_t tid = econ_q((const unsigned char *)(uintptr_t)e, &ok);
            if (!ok) return 0;
            unsigned char *t = tid ? econ_order(tid) : NULL;
            if (!t) continue;
            if (no >= ECON_MAX_OFFERS || !econ_read(t, offers[no], 0x58)) return 0;
            offer_list[no] = (uint8_t)list;
            offer_id[no] = tid;
            offer_ptr[no] = (uintptr_t)t;
            h = econ_hash_bytes(h, offers[no] + 0x08, 0x50);
            ++no;
        }
    }
    /* Production storage allocation (Container+0x940 -> ware amount list, NULL = empty; valid flag
       +0x931: when clear, the next reader recomputes it, 0x420aa0). */
    uint8_t alloc_valid = 0;
    if (!econ_read(st + 0x931, &alloc_valid, 1)) return 0;
    uint64_t alloc_ptr = econ_q(st + 0x940, &ok);
    if (!ok) return 0;
    econ_wa alloc[ECON_MAX_LIST * 4];
    int na = 0;
    if (alloc_ptr) {
        na = econ_list((const unsigned char *)(uintptr_t)alloc_ptr, alloc, ECON_MAX_LIST * 4);
        if (na < 0) return 0;
    }
    h = econ_hash_bytes(h, &alloc_valid, 1);
    h = econ_hash_wa(h, alloc, na);
    uint64_t rb = econ_q(st + 0x980, &ok), re = econ_q(st + 0x988, &ok);
    if (!ok || re < rb || (re - rb) % 0x30 || (re - rb) / 0x30 > ECON_MAX_RESERVATIONS) return 0;
    int nr = (int)((re - rb) / 0x30);
    static unsigned char resv[ECON_MAX_RESERVATIONS][0x30];
    if (nr && !econ_read((const unsigned char *)(uintptr_t)rb, resv, (size_t)nr * 0x30)) return 0;
    h = econ_hash_bytes(h, resv, (size_t)nr * 0x30);
    for (int i = 0; i < nr; ++i) { /* linked orders' amounts and flags change during a trade */
        uint64_t order;
        memcpy(&order, resv[i] + 0x10, 8);
        const unsigned char *o = order ? econ_order(order) : NULL;
        uint32_t of[2] = {0, 0};
        uint64_t oq28 = 0;
        if (o && econ_read(o + 0x28, &oq28, 8) && econ_read(o + 0x40, &of[0], 4) && econ_read(o + 0x54, &of[1], 4)) {
            of[0] ^= (uint32_t)oq28 & 0x7fffffffu;
            h = econ_hash_bytes(h, of, sizeof(of));
        }
    }
    if (!force && h == w->last_hash) return 1;
    w->last_hash = h;
    econ.len = 0;
    econ_put("C 0x%llx", (unsigned long long)w->uid);
    econ_put_f64(last);
    econ_put_f64(a28);
    econ_put_f64(a30);
    econ_put(" %d %d", a4c ? 1 : 0, np);
    if (!econ_put_list(cargo, nc)) { econ.len = 0; return 0; }
    econ_end_line();
    econ.len = 0;
    econ_put("A 0x%llx %d", (unsigned long long)w->uid, alloc_valid ? 1 : 0);
    if (!econ_put_list(alloc, na)) { econ.len = 0; return 0; }
    econ_end_line();
    for (int m = 0; m < ns; ++m) {
        econ.len = 0;
        econ_put("S 0x%llx 0x%llx %d %d", (unsigned long long)w->uid, (unsigned long long)(uintptr_t)storage[m],
                 sto_cap[m], sto_ntags[m]);
        for (int k = 0; k < sto_ntags[m]; ++k) econ_put(" %u", sto_tags[m][k]);
        if (!econ_put_list(sto_list[m], sto_n[m])) { econ.len = 0; return 0; }
        econ_end_line();
    }
    for (int i = 0; i < no; ++i) {
        const unsigned char *t = offers[i];
        uint64_t q28, q30, seller, buyer;
        uint32_t d40, d44, d50, flags;
        memcpy(&seller, t + 0x10, 8);
        memcpy(&buyer, t + 0x18, 8);
        memcpy(&q28, t + 0x28, 8);
        memcpy(&q30, t + 0x30, 8);
        memcpy(&d40, t + 0x40, 4);
        memcpy(&d44, t + 0x44, 4);
        memcpy(&d50, t + 0x50, 4);
        memcpy(&flags, t + 0x54, 4);
        /* the tags are relative to the trade object's own address */
        uintptr_t ware = offer_ptr[i] ^ (uintptr_t)q28;
        uint32_t low = (uint32_t)q28 & 0x7fffffffu;
        int wi = ware ? econ_ware_index(ware) : -1;
        econ.len = 0;
        econ_put("O 0x%llx %d 0x%llx %d 0x%llx 0x%llx %d %d %d %lld 0x%x", (unsigned long long)w->uid, offer_list[i],
                 (unsigned long long)offer_id[i], wi, (unsigned long long)seller, (unsigned long long)buyer,
                 (int32_t)(low ^ d40), (int32_t)(low ^ d44), (int32_t)(low ^ d50), (long long)(q28 ^ q30), flags);
        econ_end_line();
    }
    for (int i = 0; i < nr; ++i) {
        const unsigned char *r = resv[i];
        uint32_t word, amount;
        uint64_t reserver, order;
        uintptr_t ware;
        double expiry;
        memcpy(&word, r, 4);
        memcpy(&reserver, r + 0x08, 8);
        memcpy(&order, r + 0x10, 8);
        memcpy(&ware, r + 0x18, 8);
        memcpy(&amount, r + 0x20, 4);
        memcpy(&expiry, r + 0x28, 8);
        int wi = ware ? econ_ware_index(ware) : -1;
        econ.len = 0;
        econ_put("V 0x%llx 0x%x 0x%llx 0x%llx %d %u", (unsigned long long)w->uid, word, (unsigned long long)reserver,
                 (unsigned long long)order, wi, amount);
        econ_put_f64(expiry);
        /* A linked trade order's own amount (Trade+0x40, decoded) and flags (+0x54): the engine
           takes a linked reservation's amount from its order (0x3ef840). -1 0 when not found. */
        if (order) {
            const unsigned char *o = econ_order(order);
            uint64_t oq28 = 0;
            uint32_t od40 = 0, oflags = 0;
            if (o && econ_read(o + 0x28, &oq28, 8) && econ_read(o + 0x40, &od40, 4) && econ_read(o + 0x54, &oflags, 4))
                econ_put(" %d 0x%x", (int32_t)(((uint32_t)oq28 & 0x7fffffffu) ^ od40), oflags);
            else
                econ_put(" -1 0x0");
        }
        econ_end_line();
    }
    for (int m = 0; m < np; ++m) {
        const unsigned char *p = prod[m];
        uint64_t muid = econ_q(p + 0x08, &ok);
        uint8_t functional = 1; /* 0x51e8b0 is code; operational state (+0x68 == 0) is checked */
        int32_t state, item, cycle;
        double start, next, psince, ptotal, f[5];
        memcpy(&start, raw[m] + 0x00, 8);
        memcpy(&next, raw[m] + 0x08, 8);
        memcpy(&psince, raw[m] + 0x10, 8);
        memcpy(&ptotal, raw[m] + 0x18, 8);
        memcpy(&item, raw[m] + 0x20, 4);
        memcpy(&cycle, raw[m] + 0x24, 4);
        memcpy(f, raw[m] + 0x28, 40);
        memcpy(&state, raw[m] + 0x68, 4);
        econ_wa pend[ECON_MAX_LIST];
        int npend = econ_list(p + 0x3f8, pend, ECON_MAX_LIST);
        uint64_t ib = econ_q(p + 0x3d8, &ok), ie = econ_q(p + 0x3e0, &ok);
        if (!ok || npend < 0 || ie < ib || (ie - ib) % 0x60 || (ie - ib) / 0x60 > ECON_MAX_ITEMS) return 0;
        econ.len = 0;
        econ_put("M 0x%llx 0x%llx %d %d", (unsigned long long)muid, (unsigned long long)(uintptr_t)p, functional, state);
        econ_put_f64(start);
        econ_put_f64(next);
        econ_put_f64(psince);
        econ_put_f64(ptotal);
        econ_put(" %d %d", item, cycle);
        for (int k = 0; k < 5; ++k) econ_put_f64(f[k]);
        if (!econ_put_list(pend, npend)) { econ.len = 0; return 0; }
        econ_put(" %d", (int)((ie - ib) / 0x60));
        for (uint64_t it = ib; it < ie; it += 0x60) {
            unsigned char e[0x40];
            if (!econ_read((const unsigned char *)(uintptr_t)it, e, sizeof(e))) { econ.len = 0; return 0; }
            uintptr_t ware, method;
            double tf[5];
            int32_t cycles;
            memcpy(&ware, e + 0x00, 8);
            memcpy(&method, e + 0x08, 8);
            memcpy(tf, e + 0x10, 40);
            memcpy(&cycles, e + 0x38, 4);
            int wi = econ_ware_index(ware);
            int mi = method ? econ_method_index(method) : -1;
            if (wi < 0 || (method && mi < 0)) { econ.len = 0; return 0; }
            econ_put(" %d %d", wi, mi);
            for (int k = 0; k < 5; ++k) econ_put_f64(tf[k]);
            econ_put(" %d %d", cycles, e[0x3c] ? 1 : 0);
        }
        econ_end_line();
    }
    return 1;
}

/* Per frame (slot 5, MoveWorker), after the probe's own accounting. `rng` is the MoveWorker's
   TLS+0x318 random state at this point (0 when unknown). */
static void econcap_frame(double game_time, uint64_t frame, uint64_t rng) {
    if (!econ.armed || !econ.watched) return;
    econ_regions_reset();
    uint64_t tb;
    memcpy(&tb, &game_time, 8);
    snprintf(econ.frame_marker, sizeof(econ.frame_marker), "F %llu 0x%llx 0x%llx", (unsigned long long)frame,
             (unsigned long long)tb, (unsigned long long)rng);
    econ.marker_pending = 1;
    for (int i = 0; i < econ.watched; ++i) {
        econ_watch_entry *w = &econ.watch[i];
        if (w->dropped) continue;
        /* A dropped station must not leave half a record, or ware/method lines whose table
           entries are rolled back. */
        size_t head = econ.head;
        int nwares = econ.nwares, nmethods = econ.nmethods, pending = econ.marker_pending;
        uint64_t records = econ.records;
        if (!econ_station(w, 0)) {
            econ.head = head;
            econ.nwares = nwares;
            econ.nmethods = nmethods;
            econ.marker_pending = pending;
            econ.records = records;
            w->dropped = 1;
            ++econ.dropped_stations;
        }
    }
    econ.marker_pending = 0;
    ++econ.frames;
}

/* MoveWorker TLS+0x318 (the engine's per-thread random state; the executable's static TLS block
   is index 0, as its own code reads it: gs:[0x58] -> [0]). 0 when unreadable. */
static uint64_t econ_tls_rng(void) {
    void **slots = (void **)__readgsqword(0x58);
    uint64_t v = 0;
    unsigned char *block = NULL;
    if (!slots || !econ_read(slots, &block, sizeof(block)) || !block) return 0;
    econ_read(block + 0x318, &v, sizeof(v));
    return v;
}

static int econcap_arm(void) {
    if (econ.armed) return 1;
    if (!econ.ring) econ.ring = (char *)VirtualAlloc(NULL, ECON_RING_BYTES, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!econ.ring) return 0;
    econ.head = econ.tail = 0;
    econ.armed = 1;
    econ.len = 0;
    econ_put("X4ECON 1 build=23660954");
    econ_end_line();
    /* Economy settings block (*(0x6cf1538) +0x5130..+0x51b8, parameters.xml <economy>): raw
       qwords, so the replay and the price model use the values the engine loaded. */
    int ok = 1;
    uint64_t settings = econ_q(image + ECON_SETTINGS_PTR_RVA, &ok);
    if (ok && settings) {
        uint64_t q[18];
        if (econ_read((const unsigned char *)(uintptr_t)settings + 0x5130, q, sizeof(q))) {
            econ.len = 0;
            econ_put("P 0x5130");
            for (int i = 0; i < 18; ++i) econ_put(" 0x%llx", (unsigned long long)q[i]);
            econ_end_line();
        }
    }
    return 1;
}

static int econcap_watch(uint64_t uid) {
    if (!econcap_arm()) return 0;
    for (int i = 0; i < econ.watched; ++i)
        if (econ.watch[i].uid == uid) return 1;
    if (econ.watched >= ECON_MAX_WATCH) return 0;
    econ.watch[econ.watched].uid = uid;
    econ.watch[econ.watched].last_hash = 0;
    econ.watch[econ.watched].dropped = 0;
    ++econ.watched;
    return 1;
}

static void econcap_disarm(void) {
    econ.armed = 0;
    econ.watched = 0;
    econ.nwares = econ.nmethods = 0;
}
