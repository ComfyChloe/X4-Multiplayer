/* Mirror capture probe (included by fixedtick.c): data-only snapshots of the X4 component tree
 * in the capture format of rust/x4mp-sim (x4mp_sim::capture, format v1; spec in
 * rust/x4mp-sim/src/capture/mod.rs).
 *
 * Where: inside the fixed-tick probe's VBI slot-5 callback on the MoveWorker thread, i.e. after
 * slot 3 (universe update: component walk, pathing join and slot flip, physics, BuildCache) and
 * slot 4 (input), before the UI (slot 6). Nothing in the universe changes between this point and
 * the UI Lua of the same frame.
 *
 * What: a pre-order walk from the galaxy (*g_Universe + 0x228) through every component's child
 * containers in Component::Move order (+0xa8 then +0xa0; blocks in order; null entries skipped).
 * Each sector and each cluster starts a new world root (record), the galaxy is root 0. Recorded
 * per component: uid, class id (GetClassID semantics: +0x68 selects the alternate id), parent uid,
 * depth below its root, attention, state flags, vtable RVA, movement controller kind/vtable and
 * the local pose as the engine holds it (navigation: current pathing slot mat4 + velocity +
 * reference id + cached mat4; static: stored mat4; none: identity, which is what
 * Component::GetLocalTransform returns), hull, true/cover owner (faction dictionary) and order
 * queue length/index. Offsets, class table and code pins: mirror_layout.py (layout SHA-256 goes
 * into every capture header).
 *
 * How (safety): only reads. No engine function is called, nothing in the engine is written.
 * Arming re-verifies every pinned code range and vtable entry (fail closed). Every pointer is
 * range-checked against committed readable memory (VirtualQuery, cached per frame) before it is
 * dereferenced; malformed containers, unknown vtables and depth overflow are skipped and counted
 * (walk_errors). Frames are serialized on the MoveWorker and written by a writer thread; when the
 * writer is still busy the frame is dropped (counted, next frame flagged), never queued.
 * Default record set: every class with an Object or Space base (ships, stations, gates, missiles,
 * asteroids, zones, sectors, ...) plus classes requested by visitors; MIRROR_RECORD_ALL records
 * every component. The walk always descends (docked ships live under station modules). */

#include "mirror_layout.h"

#define MIRROR_FORMAT_MAJOR 1u
#define MIRROR_FORMAT_MINOR 0u
#define MIRROR_MAX_DEPTH 48u
#define MIRROR_MAX_ROOTS 8192u
#define MIRROR_MAX_BLOCKS 4096u
#define MIRROR_MAX_ITEMS (1u << 20)
#define MIRROR_MAX_ROWS (1u << 22)
#define MIRROR_MAX_FACTIONS 512u
#define MIRROR_MAX_VISITORS 4u
#define MIRROR_MAX_VCOLS 16u
#define MIRROR_SAMPLE_MAX 64u
#define MIRROR_REGION_CACHE 8192u /* sorted readable intervals, reset every captured frame */
#define MIRROR_QUEUE 8u /* frames the writer may lag behind before frames are dropped */
#define MIRROR_RECORD_ALL 1u
#define MIRROR_NO_NAV_CACHE 2u
#define MIRROR_CLASS_SECTOR 0x58u
#define MIRROR_CLASS_CLUSTER 0x0fu
#define MIRROR_CLASS_GALAXY 0x2fu
#define MIRROR_UNKNOWN_CLASS 0xffu

/* Capture element types (x4mp_sim::capture::Elem). */
enum { ME_U8 = 1, ME_U16 = 2, ME_U32 = 3, ME_U64 = 4, ME_I32 = 7, ME_F32 = 9, ME_F64 = 10 };
/* Controller kinds (x4mp_sim::comp::CtrlKind). */
enum { MK_NONE = 0, MK_NAVIGATION = 1, MK_STATIC = 2, MK_PLAYER = 3, MK_OTHER = 4 };
/* Row flag bits (capture column `flags`). */
#define MRF_REGISTERED 0x01u
#define MRF_DEATH_SHIFT 1u
#define MRF_ALT_MODE 0x08u
#define MRF_KNOWN 0x10u
#define MRF_POSE 0x20u
#define MRF_VEL 0x40u
#define MRF_NAV_CACHE 0x80u

typedef struct {
    uint64_t uid, parent, pose_ref;
    float pose[16], vel[8], nav_cache[16];
    double hull;
    uint32_t vtable, ctrl_vtable, orders[2], root;
    int32_t attention;
    uint16_t depth, owner, cover, class_flags;
    uint8_t class_id, flags, ctrl, pad;
} mirror_row;

/* --- visitor API (other probes compiled into this module, e.g. econcap.h) ------------------ */
typedef struct { uint16_t id; uint8_t elem; uint16_t count; const char *name; } mirror_coldesc;
typedef struct {
    const unsigned char *comp;   /* the component (read-only; your offsets, your pins) */
    uint64_t uid;
    uint32_t vtable_rva;
    uint16_t class_flags;        /* MF_* of its C++ class */
    uint8_t class_id;
    uint16_t depth;
    uint32_t root_index;         /* world record index (= SectorIx in x4mp-sim) */
    uint32_t row_index;          /* row inside the frame (all roots) */
} mirror_rowinfo;
typedef struct {
    const char *name;
    const mirror_coldesc *columns; unsigned ncolumns;   /* ids >= 0x100, reserved in interfaces.md */
    const uint8_t *classes; unsigned nclasses;           /* class ids recorded even if not default */
    void (*begin_frame)(void *user, uint64_t frame);
    /* slots[i]: zeroed storage of columns[i] for this row (count x element size bytes) */
    void (*on_row)(void *user, const mirror_rowinfo *row, void *const *slots);
    void (*end_frame)(void *user);
    void *user;
} mirror_visitor;

typedef struct { uintptr_t base, end; } mirror_region;
/* One serialized frame waiting for the writer (FRAM checksum appended by the writer). */
typedef struct { unsigned char *buf; size_t len, cap, fram; } mirror_slot;
typedef struct { uint64_t uid; uint8_t kind; uint32_t rows; } mirror_root;

typedef struct {
    int armed, failed;
    const char *reason;
    HANDLE file;
    unsigned interval, flags;
    int32_t min_attention;
    uint64_t frames_left, frame_seen, last_frame;
    uint64_t frames_written, frames_dropped, bytes, walk_errors, partial_frames;
    double last_ms, max_ms, sum_ms;
    uint64_t last_rows, last_roots;
    mirror_row *rows; size_t rows_cap, nrows;
    unsigned char *vrows; size_t vrow_bytes;
    mirror_root *roots; unsigned nroots;
    uint32_t *order; size_t order_cap;
    const void *faction_ptr[MIRROR_MAX_FACTIONS];
    char faction_name[MIRROR_MAX_FACTIONS][64];
    unsigned nfactions, factions_emitted;
    int drop_flag;
    uint64_t sample[MIRROR_SAMPLE_MAX]; unsigned nsample;
    unsigned nregions, galaxy_misses;
    uint64_t region_queries;
    unsigned char *buf; size_t buf_cap, buf_len, fram_at;
    /* writer thread (lock and condition: mirror_cs / mirror_cv, never reset) */
    HANDLE thread;
    int stop, stuck;
    mirror_slot queue[MIRROR_QUEUE]; unsigned q_head, q_tail, q_count;
    DWORD write_error;
} mirror_state;

static mirror_state ms;
static mirror_region mirror_regions[MIRROR_REGION_CACHE];
static CRITICAL_SECTION mirror_cs;
static CONDITION_VARIABLE mirror_cv;
static int mirror_sync_ready;
static unsigned mirror_dict_upto;
static const mirror_visitor *mirror_visitors[MIRROR_MAX_VISITORS];
static unsigned mirror_nvisitors;
static size_t mirror_vcol_offset[MIRROR_MAX_VISITORS][MIRROR_MAX_VCOLS];

static double mirror_tls_game_time(void) {
    unsigned char **tls = *(unsigned char ***)((unsigned char *)NtCurrentTeb() + 0x58);
    unsigned char *block = tls ? tls[0] : NULL;
    if (!block || !*(volatile unsigned char *)(block + MO_TLS_INITIALIZED)) return NAN;
    return *(volatile double *)(block + MO_TLS_GAME_TIME);
}
/* Replaceable for the synthetic tests (the test executable has no X4 TLS block). */
static double (*mirror_game_time)(void) = mirror_tls_game_time;

static size_t mirror_elem_size(uint8_t elem) {
    switch (elem) {
    case ME_U8: return 1;
    case ME_U16: return 2;
    case ME_U32: case ME_I32: case ME_F32: return 4;
    case ME_U64: case ME_F64: return 8;
    default: return 0;
    }
}

/* Registers a visitor (before arming). Returns 0 on refusal. Unused until a visitor probe is
   compiled into the module. */
__attribute__((unused)) static int mirror_add_visitor(const mirror_visitor *v) {
    if (ms.armed || !v || mirror_nvisitors >= MIRROR_MAX_VISITORS || v->ncolumns > MIRROR_MAX_VCOLS) return 0;
    for (unsigned i = 0; i < v->ncolumns; ++i) {
        if (v->columns[i].id < 0x100 || !mirror_elem_size(v->columns[i].elem) || !v->columns[i].count) return 0;
        for (unsigned a = 0; a < mirror_nvisitors; ++a)
            for (unsigned b = 0; b < mirror_visitors[a]->ncolumns; ++b)
                if (mirror_visitors[a]->columns[b].id == v->columns[i].id) return 0;
    }
    mirror_visitors[mirror_nvisitors++] = v;
    return 1;
}

/* --- memory checks ------------------------------------------------------------------------ */

static int mirror_protect_readable(DWORD protect) {
    if (protect & (PAGE_GUARD | PAGE_NOACCESS)) return 0;
    DWORD p = protect & 0xff;
    return p == PAGE_READONLY || p == PAGE_READWRITE || p == PAGE_WRITECOPY || p == PAGE_EXECUTE_READ ||
           p == PAGE_EXECUTE_READWRITE || p == PAGE_EXECUTE_WRITECOPY;
}

/* Index of the first cached interval with end > a (intervals are sorted and disjoint). */
static unsigned mirror_region_find(uintptr_t a) {
    unsigned lo = 0, hi = ms.nregions;
    while (lo < hi) {
        unsigned mid = (lo + hi) / 2;
        if (mirror_regions[mid].end <= a) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

static void mirror_region_add(uintptr_t base, uintptr_t end) {
    unsigned i = mirror_region_find(base);
    if (i < ms.nregions && mirror_regions[i].base <= base) return; /* already covered */
    if (ms.nregions >= MIRROR_REGION_CACHE) return;                /* full: queries keep working */
    memmove(&mirror_regions[i + 1], &mirror_regions[i], (ms.nregions - i) * sizeof(mirror_region));
    mirror_regions[i] = (mirror_region){base, end};
    ++ms.nregions;
}

/* [p, p+len) is committed readable memory (possibly across several regions). Readable regions
   seen this frame are cached as sorted intervals, so most checks are a binary search. */
static int mirror_ok(const void *p, size_t len) {
    uintptr_t a = (uintptr_t)p, b = a + len;
    if (!p || b < a) return 0;
    uintptr_t at = a;
    while (at < b) {
        unsigned i = mirror_region_find(at);
        if (i < ms.nregions && mirror_regions[i].base <= at) {
            at = mirror_regions[i].end;
            continue;
        }
        MEMORY_BASIC_INFORMATION m;
        ++ms.region_queries;
        if (!VirtualQuery((const void *)at, &m, sizeof(m)) || m.State != MEM_COMMIT || !mirror_protect_readable(m.Protect))
            return 0;
        uintptr_t rb = (uintptr_t)m.BaseAddress, re = rb + m.RegionSize;
        mirror_region_add(rb, re);
        at = re;
    }
    return 1;
}

static const mirror_class *mirror_find_class(uint32_t vt) {
    unsigned lo = 0, hi = MIRROR_CLASS_COUNT;
    while (lo < hi) {
        unsigned mid = (lo + hi) / 2;
        if (mirror_classes[mid].vtable < vt) lo = mid + 1;
        else hi = mid;
    }
    return lo < MIRROR_CLASS_COUNT && mirror_classes[lo].vtable == vt ? &mirror_classes[lo] : NULL;
}

static uint32_t mirror_rva(uintptr_t p) {
    uintptr_t base = (uintptr_t)image;
    return p >= base && p < base + X4_IMAGE_SIZE ? (uint32_t)(p - base) : 0;
}

/* --- verification at arm (fail closed) ---------------------------------------------------- */

static const char *mirror_verify(void) {
    for (size_t i = 0; i < sizeof(mirror_checks) / sizeof(mirror_checks[0]); ++i) {
        const mirror_check *c = &mirror_checks[i];
        if (!readable(image + c->rva, c->length, 0) || !hash_memory(image + c->rva, c->length, c->sha))
            return "mirror code signature mismatch";
    }
    static unsigned char code[MIRROR_CLASS_COUNT * MIRROR_CLASSID_CODE_LENGTH];
    for (unsigned i = 0; i < MIRROR_CLASS_COUNT; ++i) {
        const mirror_class *k = &mirror_classes[i];
        void **slot = (void **)(image + k->vtable + 8u * MIRROR_GETCLASSID_SLOT);
        if (!readable(slot, sizeof(void *), 0) || *slot != (void *)(image + k->classid_fn)) return "mirror class table mismatch";
        if (!readable(image + k->classid_fn, MIRROR_CLASSID_CODE_LENGTH, 0)) return "mirror class table mismatch";
        memcpy(code + i * MIRROR_CLASSID_CODE_LENGTH, image + k->classid_fn, MIRROR_CLASSID_CODE_LENGTH);
    }
    if (!hash_memory(code, sizeof(code), MIRROR_CLASSID_CODE_SHA256)) return "mirror class id code mismatch";
    const uint32_t ctrl[3][2] = {{MC_NAVIGATION_VTABLE, MC_NAVIGATION_TRANSFORM}, {MC_STATIC_VTABLE, MC_STATIC_TRANSFORM},
                                 {MC_PLAYER_VTABLE, MC_PLAYER_TRANSFORM}};
    for (unsigned i = 0; i < 3; ++i) {
        void **slot = (void **)(image + ctrl[i][0] + 8u * MIRROR_TRANSFORM_SLOT);
        if (!readable(slot, sizeof(void *), 0) || *slot != (void *)(image + ctrl[i][1])) return "mirror controller vtable mismatch";
    }
    if (!readable(image + MG_UNIVERSE, 8, 0) || !readable(image + MG_PATHING_INDEX, 4, 0) ||
        !readable(image + MG_SESSION_SEED, 8, 0))
        return "mirror globals unavailable";
    return NULL;
}

/* --- buffers ---------------------------------------------------------------------------------- */

static int mirror_reserve(unsigned char **buf, size_t *cap, size_t need) {
    if (need <= *cap) return 1;
    size_t n = *cap ? *cap : 1u << 16;
    while (n < need) n *= 2;
    unsigned char *p = realloc(*buf, n);
    if (!p) return 0;
    *buf = p;
    *cap = n;
    return 1;
}

static int mirror_put(const void *data, size_t n) {
    if (!mirror_reserve(&ms.buf, &ms.buf_cap, ms.buf_len + n)) return 0;
    memcpy(ms.buf + ms.buf_len, data, n);
    ms.buf_len += n;
    return 1;
}
static int mirror_put_u8(uint8_t v) { return mirror_put(&v, 1); }
static int mirror_put_u16(uint16_t v) { return mirror_put(&v, 2); }
static int mirror_put_u32(uint32_t v) { return mirror_put(&v, 4); }
static int mirror_put_u64(uint64_t v) { return mirror_put(&v, 8); }
static int mirror_put_str(const char *s) {
    size_t n = strlen(s);
    if (n > 0xffff) n = 0xffff;
    return mirror_put_u16((uint16_t)n) && mirror_put(s, n);
}

static uint64_t mirror_fnv(uint64_t h, const unsigned char *p, size_t n) {
    for (size_t i = 0; i < n; ++i) h = (h ^ p[i]) * 0x100000001b3ull;
    return h;
}
#define MIRROR_FNV_OFFSET 0xcbf29ce484222325ull

/* Record framing: tag, len placeholder; mirror_end_record patches len and appends the check. */
static size_t mirror_begin_record(const char tag[4]) {
    size_t at = ms.buf_len;
    mirror_put(tag, 4);
    mirror_put_u32(0);
    return at;
}
static void mirror_patch_len(size_t at) {
    uint32_t len = (uint32_t)(ms.buf_len - at - 8);
    memcpy(ms.buf + at + 4, &len, 4);
}
static int mirror_end_record(size_t at) {
    mirror_patch_len(at);
    uint64_t check = mirror_fnv(MIRROR_FNV_OFFSET, ms.buf + at, ms.buf_len - at);
    return mirror_put_u64(check);
}

/* --- writer thread ---------------------------------------------------------------------------- */

static int mirror_write_all(const unsigned char *p, size_t n) {
    while (n) {
        DWORD chunk = n > (1u << 30) ? (1u << 30) : (DWORD)n, done = 0;
        if (!WriteFile(ms.file, p, chunk, &done, NULL) || !done) return 0;
        p += done;
        n -= done;
    }
    return 1;
}

static DWORD WINAPI mirror_writer(LPVOID unused) {
    (void)unused;
    EnterCriticalSection(&mirror_cs);
    for (;;) {
        while (!ms.q_count && !ms.stop) SleepConditionVariableCS(&mirror_cv, &mirror_cs, INFINITE);
        if (!ms.q_count && ms.stop) break;
        mirror_slot *slot = &ms.queue[ms.q_tail];
        unsigned char *p = slot->buf;
        size_t n = slot->len, fram = slot->fram;
        LeaveCriticalSection(&mirror_cs);
        /* the FRAM record's checksum is computed here, off the MoveWorker */
        uint64_t check = mirror_fnv(MIRROR_FNV_OFFSET, p + fram, n - fram);
        memcpy(p + n, &check, 8);
        int ok = mirror_write_all(p, n + 8);
        EnterCriticalSection(&mirror_cs);
        if (!ok) ms.write_error = GetLastError() ? GetLastError() : 1;
        else ms.bytes += n + 8;
        ms.q_tail = (ms.q_tail + 1) % MIRROR_QUEUE;
        --ms.q_count;
        WakeAllConditionVariable(&mirror_cv);
    }
    LeaveCriticalSection(&mirror_cs);
    return 0;
}

/* --- the walk ----------------------------------------------------------------------------------- */

static unsigned mirror_new_root(uint64_t uid, uint8_t kind) {
    if (ms.nroots >= MIRROR_MAX_ROOTS) return UINT32_MAX;
    ms.roots[ms.nroots] = (mirror_root){uid, kind, 0};
    return ms.nroots++;
}

static uint16_t mirror_faction(const unsigned char *f) {
    if (!f) return 0xffff;
    for (unsigned i = 0; i < ms.nfactions; ++i)
        if (ms.faction_ptr[i] == f) return (uint16_t)i;
    if (ms.nfactions >= MIRROR_MAX_FACTIONS) return 0xfffe;
    unsigned i = ms.nfactions++;
    ms.faction_ptr[i] = f;
    strcpy(ms.faction_name[i], "?");
    if (mirror_ok(f + MO_FACTION_ID, 0x20)) {
        uint64_t size = *(const uint64_t *)(f + MO_FACTION_ID_SIZE);
        uint64_t cap = *(const uint64_t *)(f + MO_FACTION_ID_CAPACITY);
        const char *data = cap >= 16 ? *(const char *const *)(f + MO_FACTION_ID) : (const char *)(f + MO_FACTION_ID);
        if (size && size < sizeof(ms.faction_name[i]) && size <= cap && mirror_ok(data, size)) {
            memcpy(ms.faction_name[i], data, size);
            ms.faction_name[i][size] = 0;
            for (uint64_t k = 0; k < size; ++k)
                if ((unsigned char)ms.faction_name[i][k] < 0x20 || (unsigned char)ms.faction_name[i][k] > 0x7e)
                    ms.faction_name[i][k] = '?';
        }
    }
    return (uint16_t)i;
}

static int mirror_wants(const mirror_class *cls, uint8_t cid) {
    if (ms.flags & MIRROR_RECORD_ALL) return 1;
    if (cls && (cls->flags & (MF_OBJECT | MF_SPACE))) return 1;
    for (unsigned v = 0; v < mirror_nvisitors; ++v)
        for (unsigned k = 0; k < mirror_visitors[v]->nclasses; ++k)
            if (mirror_visitors[v]->classes[k] == cid) return 1;
    return 0;
}

static mirror_row *mirror_append(void) {
    if (ms.nrows >= MIRROR_MAX_ROWS) return NULL;
    if (ms.nrows >= ms.rows_cap) {
        size_t n = ms.rows_cap ? ms.rows_cap * 2 : 65536;
        mirror_row *r = realloc(ms.rows, n * sizeof(mirror_row));
        if (!r) return NULL;
        ms.rows = r;
        if (ms.vrow_bytes) {
            unsigned char *v = realloc(ms.vrows, n * ms.vrow_bytes);
            if (!v) return NULL;
            ms.vrows = v;
        }
        ms.rows_cap = n;
    }
    if (ms.vrow_bytes) memset(ms.vrows + ms.nrows * ms.vrow_bytes, 0, ms.vrow_bytes);
    return &ms.rows[ms.nrows++];
}

static const float mirror_identity[16] = {0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0};

static void mirror_fill(mirror_row *r, const unsigned char *comp, const mirror_class *cls, uint8_t cid, uint32_t vt,
                        uint64_t uid, uint64_t parent, unsigned root, unsigned depth) {
    memset(r, 0, sizeof(*r));
    r->uid = uid;
    r->parent = parent;
    r->root = root;
    r->depth = (uint16_t)depth;
    r->class_id = cid;
    r->vtable = vt;
    r->class_flags = cls->flags;
    r->hull = NAN;
    r->owner = r->cover = 0xffff;
    r->attention = *(const int32_t *)(comp + MO_ATTENTION);
    uint8_t f = MRF_KNOWN;
    if (*(const uint8_t *)(comp + MO_REGISTERED)) f |= MRF_REGISTERED;
    f |= (uint8_t)((*(const uint8_t *)(comp + MO_DEATH) & 3u) << MRF_DEATH_SHIFT);
    if (*(const int32_t *)(comp + MO_MODE)) f |= MRF_ALT_MODE;
    if ((cls->flags & MF_POSITIONAL) && mirror_ok(comp, MO_CONTROLLER + 8)) {
        const unsigned char *ctrl = *(const unsigned char *const *)(comp + MO_CONTROLLER);
        if (!ctrl) {
            r->ctrl = MK_NONE;
            memcpy(r->pose, mirror_identity, sizeof(r->pose));
            f |= MRF_POSE;
        } else if (((uintptr_t)ctrl & 7) == 0 && mirror_ok(ctrl, MO_NAV_CACHE + 64)) {
            uint32_t cvt = mirror_rva(*(const uintptr_t *)ctrl);
            r->ctrl_vtable = cvt;
            if (cvt == MC_NAVIGATION_VTABLE) {
                r->ctrl = MK_NAVIGATION;
                const unsigned char *slots = *(const unsigned char *const *)(ctrl + MO_NAV_SLOTS);
                uint32_t idx = *(const volatile uint32_t *)(image + MG_PATHING_INDEX);
                if (slots && idx < 2 && mirror_ok(slots + idx * MO_NAV_SLOT_STRIDE, MO_SLOT_REF + 8)) {
                    const unsigned char *slot = slots + idx * MO_NAV_SLOT_STRIDE;
                    memcpy(r->pose, slot + MO_SLOT_POSE, 64);
                    memcpy(r->vel, slot + MO_SLOT_LINEAR, 16);
                    memcpy(r->vel + 4, slot + MO_SLOT_ANGULAR, 16);
                    r->pose_ref = *(const uint64_t *)(slot + MO_SLOT_REF);
                    f |= MRF_POSE | MRF_VEL;
                }
                if (!(ms.flags & MIRROR_NO_NAV_CACHE)) {
                    memcpy(r->nav_cache, ctrl + MO_NAV_CACHE, 64);
                    f |= MRF_NAV_CACHE;
                }
            } else if (cvt == MC_STATIC_VTABLE) {
                r->ctrl = MK_STATIC;
                memcpy(r->pose, ctrl + MO_STATIC_POSE, 64);
                f |= MRF_POSE;
            } else {
                r->ctrl = cvt == MC_PLAYER_VTABLE ? MK_PLAYER : MK_OTHER;
            }
        } else {
            r->ctrl = MK_OTHER;
            ++ms.walk_errors;
        }
    }
    if ((cls->flags & MF_DESTRUCTIBLE) && mirror_ok(comp, MO_HULL + 8)) r->hull = *(const double *)(comp + MO_HULL);
    if ((cls->flags & MF_OBJECT) && mirror_ok(comp, MO_OWNER_COVER + 8)) {
        r->owner = mirror_faction(*(const unsigned char *const *)(comp + MO_OWNER_TRUE));
        r->cover = mirror_faction(*(const unsigned char *const *)(comp + MO_OWNER_COVER));
    }
    if ((cls->flags & MF_CONTROLLABLE) && mirror_ok(comp, MO_ORDER_INDEX + 8)) {
        uintptr_t b = *(const uintptr_t *)(comp + MO_ORDERS_BEGIN), e = *(const uintptr_t *)(comp + MO_ORDERS_END);
        r->orders[0] = e >= b && (e - b) % 8 == 0 && (e - b) / 8 < 0x100000 ? (uint32_t)((e - b) / 8) : 0xffffffffu;
        r->orders[1] = (uint32_t)*(const uint64_t *)(comp + MO_ORDER_INDEX);
    }
    r->flags = f;
}

static void mirror_visit(const unsigned char *comp, uint64_t parent, unsigned root, unsigned depth, unsigned level);

static void mirror_children(const unsigned char *field, uint64_t parent, unsigned root, unsigned depth, unsigned level) {
    const unsigned char *c = *(const unsigned char *const *)field;
    if (!c) return;
    if (((uintptr_t)c & 7) || !mirror_ok(c, MO_CONTAINER_COUNT + 4)) { ++ms.walk_errors; return; }
    if (!*(const uint32_t *)(c + MO_CONTAINER_COUNT)) return;
    const unsigned char *b = *(const unsigned char *const *)(c + MO_CONTAINER_BLOCKS_BEGIN);
    const unsigned char *e = *(const unsigned char *const *)(c + MO_CONTAINER_BLOCKS_END);
    if (b == e) return;
    if (!b || e < b || (size_t)(e - b) % MO_BLOCK_STRIDE || (size_t)(e - b) / MO_BLOCK_STRIDE > MIRROR_MAX_BLOCKS ||
        !mirror_ok(b, (size_t)(e - b))) { ++ms.walk_errors; return; }
    for (const unsigned char *blk = b; blk < e; blk += MO_BLOCK_STRIDE) {
        const unsigned char *ib = *(const unsigned char *const *)(blk + MO_BLOCK_ITEMS_BEGIN);
        const unsigned char *ie = *(const unsigned char *const *)(blk + MO_BLOCK_ITEMS_END);
        if (ib == ie) continue;
        if (!ib || ie < ib || (size_t)(ie - ib) % 8 || (size_t)(ie - ib) / 8 > MIRROR_MAX_ITEMS || !mirror_ok(ib, (size_t)(ie - ib))) {
            ++ms.walk_errors;
            continue;
        }
        for (const unsigned char *p = ib; p < ie; p += 8) {
            const unsigned char *child = *(const unsigned char *const *)p;
            if (child) mirror_visit(child, parent, root, depth, level);
        }
    }
}

static int mirror_full;

static void mirror_visit(const unsigned char *comp, uint64_t parent, unsigned root, unsigned depth, unsigned level) {
    if (level > MIRROR_MAX_DEPTH || ((uintptr_t)comp & 7) || !mirror_ok(comp, 0x200)) { ++ms.walk_errors; return; }
    uint32_t vt = mirror_rva(*(const uintptr_t *)comp);
    const mirror_class *cls = vt ? mirror_find_class(vt) : NULL;
    if (!cls) { ++ms.walk_errors; return; }
    uint8_t cid = *(const int32_t *)(comp + MO_MODE) ? cls->id1 : cls->id0;
    uint64_t uid = *(const uint64_t *)(comp + MO_ID);
    if (cid == MIRROR_CLASS_SECTOR || cid == MIRROR_CLASS_CLUSTER) {
        unsigned r = mirror_new_root(uid, cid == MIRROR_CLASS_SECTOR ? 0 : 1);
        if (r == UINT32_MAX) { ++ms.walk_errors; return; }
        root = r;
        depth = 0;
    }
    if (mirror_wants(cls, cid)) {
        mirror_row *row = mirror_append();
        if (!row) {
            mirror_full = 1;
        } else {
            mirror_fill(row, comp, cls, cid, vt, uid, parent, root, depth);
            ms.roots[root].rows++;
            if (mirror_nvisitors) {
                mirror_rowinfo info = {comp, uid, vt, cls->flags, cid, (uint16_t)depth, root, (uint32_t)(ms.nrows - 1)};
                for (unsigned v = 0; v < mirror_nvisitors; ++v) {
                    const mirror_visitor *vis = mirror_visitors[v];
                    if (!vis->on_row || !vis->ncolumns) continue;
                    void *slots[MIRROR_MAX_VCOLS];
                    for (unsigned k = 0; k < vis->ncolumns; ++k)
                        slots[k] = ms.vrows + (ms.nrows - 1) * ms.vrow_bytes + mirror_vcol_offset[v][k];
                    vis->on_row(vis->user, &info, slots);
                }
            }
        }
    }
    if (cid == MIRROR_CLASS_SECTOR && ms.min_attention > 0 && *(const int32_t *)(comp + MO_ATTENTION) < ms.min_attention)
        return;
    mirror_children(comp + MO_CHILDREN_FIRST, uid, root, depth + 1, level + 1);
    mirror_children(comp + MO_CHILDREN_SECOND, uid, root, depth + 1, level + 1);
}

/* --- serialization ------------------------------------------------------------------------------ */

typedef struct { uint16_t id; uint8_t elem; uint16_t count; const char *name; } mirror_core_col;
static const mirror_core_col mirror_core_cols[] = {
    {0x01, ME_U64, 1, "uid"}, {0x02, ME_U8, 1, "class"}, {0x03, ME_U64, 1, "parent"}, {0x04, ME_U16, 1, "depth"},
    {0x05, ME_I32, 1, "attention"}, {0x06, ME_U8, 1, "flags"}, {0x07, ME_U32, 1, "vtable"}, {0x08, ME_U8, 1, "ctrl"},
    {0x09, ME_U32, 1, "ctrl_vtable"}, {0x0a, ME_F32, 16, "pose"}, {0x0b, ME_F32, 8, "vel"}, {0x0c, ME_U64, 1, "pose_ref"},
    {0x0d, ME_F32, 16, "nav_cache"}, {0x0e, ME_F64, 1, "hull"}, {0x0f, ME_U16, 1, "owner"}, {0x10, ME_U16, 1, "cover"},
    {0x11, ME_U32, 2, "orders"}, {0x12, ME_U16, 1, "class_flags"},
};
#define MIRROR_CORE_COLS (sizeof(mirror_core_cols) / sizeof(mirror_core_cols[0]))

static const void *mirror_core_value(const mirror_row *r, uint16_t id) {
    switch (id) {
    case 0x01: return &r->uid;
    case 0x02: return &r->class_id;
    case 0x03: return &r->parent;
    case 0x04: return &r->depth;
    case 0x05: return &r->attention;
    case 0x06: return &r->flags;
    case 0x07: return &r->vtable;
    case 0x08: return &r->ctrl;
    case 0x09: return &r->ctrl_vtable;
    case 0x0a: return r->pose;
    case 0x0b: return r->vel;
    case 0x0c: return &r->pose_ref;
    case 0x0d: return r->nav_cache;
    case 0x0e: return &r->hull;
    case 0x0f: return &r->owner;
    case 0x10: return &r->cover;
    case 0x12: return &r->class_flags;
    default: return r->orders;
    }
}

static int mirror_write_header(uint64_t session_seed) {
    ms.buf_len = 0;
    mirror_put("X4MPCAP\0", 8);
    mirror_put_u16(MIRROR_FORMAT_MAJOR);
    mirror_put_u16(MIRROR_FORMAT_MINOR);
    mirror_put_u32(0);
    size_t at = mirror_begin_record("HEAD");
    mirror_put_u16(MIRROR_FORMAT_MAJOR);
    mirror_put_u16(MIRROR_FORMAT_MINOR);
    mirror_put_u8(1); /* windows */
    mirror_put_u8(0); mirror_put_u8(0); mirror_put_u8(0);
    mirror_put_u32(23660954u);
    unsigned char sha[32];
    for (unsigned i = 0; i < 32; ++i) {
        unsigned hi = (unsigned)(X4_FILE_SHA256[2 * i] <= '9' ? X4_FILE_SHA256[2 * i] - '0' : X4_FILE_SHA256[2 * i] - 'a' + 10);
        unsigned lo = (unsigned)(X4_FILE_SHA256[2 * i + 1] <= '9' ? X4_FILE_SHA256[2 * i + 1] - '0' : X4_FILE_SHA256[2 * i + 1] - 'a' + 10);
        sha[i] = (unsigned char)(hi << 4 | lo);
    }
    mirror_put(sha, 32);
    mirror_put_u32(X4_PE_TIMESTAMP);
    mirror_put_u32(X4_IMAGE_SIZE);
    mirror_put(mirror_layout_digest, 32);
    mirror_put_u64(session_seed);
    mirror_put_u32(ms.interval);
    mirror_put_u32(ms.flags);
    mirror_put_str("x4lan_fixedtick mirror v1");
    unsigned ncols = (unsigned)MIRROR_CORE_COLS;
    for (unsigned v = 0; v < mirror_nvisitors; ++v) ncols += mirror_visitors[v]->ncolumns;
    mirror_put_u16((uint16_t)ncols);
    for (unsigned i = 0; i < MIRROR_CORE_COLS; ++i) {
        mirror_put_u16(mirror_core_cols[i].id);
        mirror_put_u8(mirror_core_cols[i].elem);
        mirror_put_u8(0);
        mirror_put_u16(mirror_core_cols[i].count);
        mirror_put_str(mirror_core_cols[i].name);
    }
    for (unsigned v = 0; v < mirror_nvisitors; ++v)
        for (unsigned k = 0; k < mirror_visitors[v]->ncolumns; ++k) {
            const mirror_coldesc *c = &mirror_visitors[v]->columns[k];
            mirror_put_u16(c->id);
            mirror_put_u8(c->elem);
            mirror_put_u8(0);
            mirror_put_u16(c->count);
            mirror_put_str(c->name ? c->name : "");
        }
    return mirror_end_record(at);
}

/* Serializes DICT (new factions) and FRAM into ms.buf; the FRAM checksum is left to the writer. */
static int mirror_serialize(uint64_t frame, double game_time, uint32_t flags) {
    ms.buf_len = 0;
    mirror_dict_upto = ms.factions_emitted;
    if (ms.nfactions > ms.factions_emitted) {
        size_t at = mirror_begin_record("DICT");
        mirror_put_u8(1);
        mirror_put_u8(0); mirror_put_u8(0); mirror_put_u8(0);
        mirror_put_u32(ms.nfactions - ms.factions_emitted);
        for (unsigned i = ms.factions_emitted; i < ms.nfactions; ++i) {
            mirror_put_u32(i);
            mirror_put_str(ms.faction_name[i]);
        }
        if (!mirror_end_record(at)) return 0;
        mirror_dict_upto = ms.nfactions;
    }
    /* rows grouped by root (stable): counting sort */
    if (ms.order_cap < ms.nrows) {
        uint32_t *o = realloc(ms.order, ms.nrows * sizeof(uint32_t));
        if (!o) return 0;
        ms.order = o;
        ms.order_cap = ms.nrows;
    }
    static uint32_t start[MIRROR_MAX_ROOTS + 1];
    uint32_t acc = 0;
    for (unsigned r = 0; r < ms.nroots; ++r) { start[r] = acc; acc += ms.roots[r].rows; }
    start[ms.nroots] = acc;
    for (size_t i = 0; i < ms.nrows; ++i) ms.order[start[ms.rows[i].root]++] = (uint32_t)i;
    for (unsigned r = ms.nroots; r > 0; --r) start[r] = start[r - 1];
    start[0] = 0;
    size_t fram = mirror_begin_record("FRAM");
    ms.fram_at = fram;
    uint64_t tbits;
    memcpy(&tbits, &game_time, 8);
    mirror_put_u64(frame);
    mirror_put_u64(tbits);
    mirror_put_u32(ms.nroots);
    mirror_put_u32(flags);
    unsigned ncols = (unsigned)MIRROR_CORE_COLS;
    for (unsigned v = 0; v < mirror_nvisitors; ++v) ncols += mirror_visitors[v]->ncolumns;
    for (unsigned r = 0; r < ms.nroots; ++r) {
        uint32_t n = ms.roots[r].rows;
        const uint32_t *idx = ms.order + start[r];
        mirror_put_u64(ms.roots[r].uid);
        mirror_put_u8(ms.roots[r].kind);
        mirror_put_u8(0);
        mirror_put_u16((uint16_t)ncols);
        mirror_put_u32(n);
        for (unsigned c = 0; c < MIRROR_CORE_COLS; ++c) {
            size_t w = mirror_elem_size(mirror_core_cols[c].elem) * mirror_core_cols[c].count;
            mirror_put_u16(mirror_core_cols[c].id);
            mirror_put_u16(0);
            mirror_put_u32((uint32_t)(w * n));
            if (!mirror_reserve(&ms.buf, &ms.buf_cap, ms.buf_len + w * n)) return 0;
            for (uint32_t k = 0; k < n; ++k) {
                memcpy(ms.buf + ms.buf_len, mirror_core_value(&ms.rows[idx[k]], mirror_core_cols[c].id), w);
                ms.buf_len += w;
            }
        }
        for (unsigned v = 0; v < mirror_nvisitors; ++v)
            for (unsigned k = 0; k < mirror_visitors[v]->ncolumns; ++k) {
                const mirror_coldesc *cd = &mirror_visitors[v]->columns[k];
                size_t w = mirror_elem_size(cd->elem) * cd->count;
                mirror_put_u16(cd->id);
                mirror_put_u16(0);
                mirror_put_u32((uint32_t)(w * n));
                if (!mirror_reserve(&ms.buf, &ms.buf_cap, ms.buf_len + w * n)) return 0;
                for (uint32_t i = 0; i < n; ++i) {
                    memcpy(ms.buf + ms.buf_len, ms.vrows + (size_t)idx[i] * ms.vrow_bytes + mirror_vcol_offset[v][k], w);
                    ms.buf_len += w;
                }
            }
    }
    mirror_patch_len(fram);
    /* room for the checksum the writer appends */
    return mirror_reserve(&ms.buf, &ms.buf_cap, ms.buf_len + 8);
}

/* Hands the serialized frame to the writer, or drops it when the writer is busy. */
static int mirror_hand_off(void) {
    int handed = 0;
    EnterCriticalSection(&mirror_cs);
    if (ms.q_count < MIRROR_QUEUE) {
        mirror_slot *slot = &ms.queue[ms.q_head];
        unsigned char *b = slot->buf;
        size_t cap = slot->cap;
        slot->buf = ms.buf;
        slot->cap = ms.buf_cap;
        slot->len = ms.buf_len;
        slot->fram = ms.fram_at;
        ms.q_head = (ms.q_head + 1) % MIRROR_QUEUE;
        ++ms.q_count;
        ms.buf = b;
        ms.buf_cap = cap;
        handed = 1;
        ms.factions_emitted = mirror_dict_upto;
        WakeAllConditionVariable(&mirror_cv);
    }
    LeaveCriticalSection(&mirror_cs);
    ms.buf_len = 0;
    return handed;
}

/* --- lifecycle ---------------------------------------------------------------------------------- */

static void mirror_stop(void) {
    if (!ms.armed && !ms.file) return;
    ms.armed = 0;
    if (ms.stuck) return;
    if (ms.thread) {
        EnterCriticalSection(&mirror_cs);
        for (int i = 0; i < 5 && ms.q_count && !ms.write_error; ++i)
            SleepConditionVariableCS(&mirror_cv, &mirror_cs, 2000);
        ms.stop = 1;
        WakeAllConditionVariable(&mirror_cv);
        LeaveCriticalSection(&mirror_cs);
        if (WaitForSingleObject(ms.thread, 5000) != WAIT_OBJECT_0) {
            /* The writer may still touch the file and buffers: leak them, never re-arm. */
            ms.stuck = 1;
            ms.reason = "capture writer did not stop";
            return;
        }
        CloseHandle(ms.thread);
        ms.thread = NULL;
    }
    if (ms.file && ms.file != INVALID_HANDLE_VALUE) {
        ms.buf_len = 0;
        size_t at = mirror_begin_record("STAT");
        mirror_put_u64(ms.frames_written);
        mirror_put_u64(ms.frames_dropped);
        mirror_put_u64(ms.bytes + 16 + 32);
        mirror_put_u64(ms.walk_errors);
        mirror_end_record(at);
        mirror_write_all(ms.buf, ms.buf_len);
        FlushFileBuffers(ms.file);
        CloseHandle(ms.file);
    }
    ms.file = NULL;
    ms.buf_len = 0;
}

/* Arms the capture without the signature checks (mirror_arm verifies first). */
static const char *mirror_arm_unchecked(const wchar_t *path, unsigned interval, uint64_t frames, unsigned flags,
                                        int32_t min_attention) {
    if (ms.armed) return "mirror already armed";
    if (ms.stuck) return "capture writer did not stop";
    if (!interval || !frames) return "invalid mirror interval or frame count";
    free(ms.rows); free(ms.vrows); free(ms.roots); free(ms.order); free(ms.buf);
    for (unsigned i = 0; i < MIRROR_QUEUE; ++i) free(ms.queue[i].buf);
    memset(&ms, 0, sizeof(ms));
    if (!mirror_sync_ready) {
        InitializeCriticalSection(&mirror_cs);
        InitializeConditionVariable(&mirror_cv);
        mirror_sync_ready = 1;
    }
    ms.interval = interval;
    ms.frames_left = frames;
    ms.flags = flags;
    ms.min_attention = min_attention;
    ms.vrow_bytes = 0;
    for (unsigned v = 0; v < mirror_nvisitors; ++v)
        for (unsigned k = 0; k < mirror_visitors[v]->ncolumns; ++k) {
            mirror_vcol_offset[v][k] = ms.vrow_bytes;
            ms.vrow_bytes += mirror_elem_size(mirror_visitors[v]->columns[k].elem) * mirror_visitors[v]->columns[k].count;
        }
    ms.roots = calloc(MIRROR_MAX_ROOTS, sizeof(mirror_root));
    if (!ms.roots) return "out of memory";
    ms.file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (ms.file == INVALID_HANDLE_VALUE) { ms.file = NULL; return "cannot create capture file (exists or bad path)"; }
    uint64_t seed = readable(image + MG_SESSION_SEED, 8, 0) ? *(const uint64_t *)(image + MG_SESSION_SEED) : 0;
    if (!mirror_write_header(seed) || !mirror_write_all(ms.buf, ms.buf_len)) {
        CloseHandle(ms.file);
        ms.file = NULL;
        return "cannot write capture header";
    }
    ms.bytes = ms.buf_len;
    ms.buf_len = 0;
    ms.thread = CreateThread(NULL, 0, mirror_writer, NULL, 0, NULL);
    if (!ms.thread) {
        CloseHandle(ms.file);
        ms.file = NULL;
        return "cannot start writer thread";
    }
    ms.armed = 1;
    return NULL;
}

static const char *mirror_arm(const wchar_t *path, unsigned interval, uint64_t frames, unsigned flags, int32_t min_attention) {
    const char *why = mirror_verify();
    if (why) return why;
    return mirror_arm_unchecked(path, interval, frames, flags, min_attention);
}

/* Once per simulation frame from the slot-5 callback (MoveWorker, not loading). */
static void mirror_frame(uint64_t frame) {
    if (!ms.armed) return;
    if (ms.write_error) { ms.reason = "capture write failed"; mirror_stop(); return; }
    if (ms.frame_seen++ % ms.interval) return;
    LARGE_INTEGER t0, t1;
    qpc_read(&t0);
    ms.nrows = 0;
    ms.nroots = 0;
    ms.nregions = 0;
    mirror_full = 0;
    for (unsigned v = 0; v < mirror_nvisitors; ++v)
        if (mirror_visitors[v]->begin_frame) mirror_visitors[v]->begin_frame(mirror_visitors[v]->user, frame);
    const unsigned char *universe = *(const unsigned char *const *)(image + MG_UNIVERSE);
    const unsigned char *galaxy = universe && mirror_ok(universe, MG_UNIVERSE_GALAXY + 8)
                                      ? *(const unsigned char *const *)(universe + MG_UNIVERSE_GALAXY) : NULL;
    uint32_t gvt = galaxy && mirror_ok(galaxy, 8) ? mirror_rva(*(const uintptr_t *)galaxy) : 0;
    const mirror_class *gcls = gvt ? mirror_find_class(gvt) : NULL;
    if (!gcls || gcls->id0 != MIRROR_CLASS_GALAXY) {
        ++ms.walk_errors;
        ms.reason = "galaxy not found";
        if (++ms.galaxy_misses >= 600) mirror_stop(); /* about 10 s without a world: give up */
        return; /* nothing captured this frame */
    }
    ms.galaxy_misses = 0;
    unsigned root = mirror_new_root(*(const uint64_t *)(galaxy + MO_ID), 2);
    mirror_visit(galaxy, 0, root, 0, 0);
    for (unsigned v = 0; v < mirror_nvisitors; ++v)
        if (mirror_visitors[v]->end_frame) mirror_visitors[v]->end_frame(mirror_visitors[v]->user);
    uint32_t flags = (mirror_full ? 1u : 0u) | (ms.drop_flag ? 2u : 0u);
    if (mirror_full) ++ms.partial_frames;
    /* Sample for the Lua cross-check: rows with a captured pose, spread over the frame. */
    ms.nsample = 0;
    size_t posed = 0;
    for (size_t i = 0; i < ms.nrows; ++i)
        if ((ms.rows[i].flags & MRF_POSE) && ms.rows[i].ctrl != MK_NONE) ++posed;
    size_t step = posed > MIRROR_SAMPLE_MAX ? posed / MIRROR_SAMPLE_MAX : 1, seen = 0;
    for (size_t i = 0; i < ms.nrows && ms.nsample < MIRROR_SAMPLE_MAX; ++i)
        if ((ms.rows[i].flags & MRF_POSE) && ms.rows[i].ctrl != MK_NONE && seen++ % step == 0)
            ms.sample[ms.nsample++] = ms.rows[i].uid;
    double game_time = mirror_game_time();
    if (!mirror_serialize(frame, game_time, flags)) {
        ms.reason = "out of memory";
        mirror_stop();
        return;
    }
    if (mirror_hand_off()) {
        ++ms.frames_written;
        ms.drop_flag = 0;
    } else {
        ++ms.frames_dropped;
        ms.drop_flag = 1;
    }
    ms.last_frame = frame;
    ms.last_rows = ms.nrows;
    ms.last_roots = ms.nroots;
    qpc_read(&t1);
    double t = qpc_frequency > 0 ? (double)(t1.QuadPart - t0.QuadPart) * 1000.0 / (double)qpc_frequency : 0.0;
    ms.last_ms = t;
    ms.sum_ms += t;
    if (t > ms.max_ms) ms.max_ms = t;
    if (--ms.frames_left == 0) mirror_stop();
}

/* --- Lua methods (on the fixed-tick owner userdata; MoveWorker, slot 6) ----------------------- */

static void mirror_num(lua_State *L, const char *name, double value) {
    p_lua_pushnumber(L, value);
    p_lua_setfield(L, -2, name);
}

/* owner:mirror(path, interval, frames[, flags[, min_attention]]) -> true | nil, reason */
static int l_mirror(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    const char *why = NULL;
    size_t n = 0;
    const char *path = p_lua_type(L, 2) == LUA_TSTRING ? p_lua_tolstring(L, 2, &n) : NULL;
    double interval = p_lua_type(L, 3) == LUA_TNUMBER ? p_lua_tonumber(L, 3) : -1;
    double frames = p_lua_type(L, 4) == LUA_TNUMBER ? p_lua_tonumber(L, 4) : -1;
    double flags = p_lua_type(L, 5) == LUA_TNUMBER ? p_lua_tonumber(L, 5) : 0;
    double attention = p_lua_type(L, 6) == LUA_TNUMBER ? p_lua_tonumber(L, 6) : 0;
    wchar_t wpath[MAX_PATH];
    if (o != current || !o->armed) why = "probe not armed";
    else if (GetCurrentThreadId() != owner_thread) why = "wrong thread";
    else if (!path || n < 3 || n >= MAX_PATH || path[1] != ':' || (path[2] != '\\' && path[2] != '/') ||
             !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, (int)n + 1, wpath, MAX_PATH))
        why = "invalid capture path (absolute drive path required)";
    else if (!(interval >= 1 && interval <= 3600 && interval == (double)(unsigned)interval)) why = "invalid interval";
    else if (!(frames >= 1 && frames <= 1e7 && frames == (double)(uint64_t)frames)) why = "invalid frame count";
    else if (!(flags >= 0 && flags <= 3 && flags == (double)(unsigned)flags)) why = "invalid flags";
    else if (!(attention >= 0 && attention <= 11)) why = "invalid min_attention";
    else why = mirror_arm(wpath, (unsigned)interval, (uint64_t)frames, (unsigned)flags, (int32_t)attention);
    if (why) {
        p_lua_pushnil(L);
        p_lua_pushlstring(L, why, strlen(why));
        return 2;
    }
    p_lua_pushboolean(L, 1);
    return 1;
}

static int l_mirror_stats(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    (void)o;
    p_lua_createtable(L, 0, 20);
    mirror_num(L, "armed", ms.armed);
    mirror_num(L, "frames_written", (double)ms.frames_written);
    mirror_num(L, "frames_dropped", (double)ms.frames_dropped);
    mirror_num(L, "frames_left", (double)ms.frames_left);
    mirror_num(L, "partial_frames", (double)ms.partial_frames);
    mirror_num(L, "last_frame", (double)ms.last_frame);
    mirror_num(L, "probe_frame", current ? (double)current->frames : 0.0);
    mirror_num(L, "rows", (double)ms.last_rows);
    mirror_num(L, "roots", (double)ms.last_roots);
    mirror_num(L, "bytes", (double)ms.bytes);
    mirror_num(L, "walk_errors", (double)ms.walk_errors);
    mirror_num(L, "factions", ms.nfactions);
    mirror_num(L, "last_ms", ms.last_ms);
    mirror_num(L, "max_ms", ms.max_ms);
    mirror_num(L, "mean_ms", ms.frames_written + ms.frames_dropped ? ms.sum_ms / (double)(ms.frames_written + ms.frames_dropped) : 0);
    mirror_num(L, "visitors", mirror_nvisitors);
    mirror_num(L, "region_queries", (double)ms.region_queries);
    p_lua_pushlstring(L, MIRROR_LAYOUT_SHA256, strlen(MIRROR_LAYOUT_SHA256));
    p_lua_setfield(L, -2, "layout");
    if (ms.reason) {
        p_lua_pushlstring(L, ms.reason, strlen(ms.reason));
        p_lua_setfield(L, -2, "reason");
    }
    return 1;
}

/* owner:mirror_sample() -> { uid, ... } of posed rows of the last captured frame */
static int l_mirror_sample(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    (void)o;
    p_lua_createtable(L, (int)ms.nsample, 0);
    for (unsigned i = 0; i < ms.nsample; ++i) {
        p_lua_pushnumber(L, (double)ms.sample[i]); /* ids < 2^41: exact in a double */
        p_lua_rawseti(L, -2, (int)i + 1);
    }
    return 1;
}

static int l_mirror_stop(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    (void)o;
    mirror_stop();
    return l_mirror_stats(L);
}
