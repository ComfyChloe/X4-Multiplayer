/* View-cache capture core, shared by the in-game probe (xform_probe.c) and the offline oracle
 * (xform_oracle.c "capture" mode).
 *
 * xc_capture_frame(galaxy, idx, time) is called where U::UniverseFrameUpdate calls
 * U::Component::BuildCache(galaxy, idx, time) (0x9fe549). It records everything the build can
 * read, calls the original build, then records what it changed:
 *
 *   1. walk: the same traversal as BuildCache 0x3d4a80 (gate, specification stack, class
 *      truncation through the engine's own IsClass, vtable slot 573), with private truncation
 *      flags and no writes to engine memory; it descends into the child vector of cache idx and,
 *      where the child bit (0x2) is dirty, also into the child container (+0xa8), and records the
 *      attachments (+0xa0) of open components;
 *   2. IsClass of every recorded component for every truncation class found in their lists;
 *   3. pre-state of every recorded component, 4. the original BuildCache, 5. post-state.
 *
 * The includer provides: `unsigned char *image` (X4 image base) and nothing else; all engine
 * access below is reads of components reachable from the galaxy (exactly what BuildCache reads)
 * plus IsClass calls. File layout ("X4XFCAP1"): rust/x4mp-transform/src/capture.rs.
 * Never use outside the MoveWorker, never while BuildCache runs.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define XC_RVA_BUILD_CACHE 0x3d4a80u
#define XC_RVA_SPEC_DEPTH 0x6cf1d40u
#define XC_RVA_TRUNC_DEPTH 0x6cf1d30u
#define XC_RVA_ENTRY_TOTAL 0x6cf1d38u
#define XC_RVA_DROP_POSE 0x2f4b9bcu
#define XC_SLOT_IS_CLASS 573
#define XC_CLASS_NONE 0x79u
#define XC_MAX_STACK 99
#define XC_MAX_ENTRIES 64
#define XC_MAX_DEPTH 64
#define XC_MAX_CLASSES 64
#define XC_HOLE ((uintptr_t)0)
#define XC_BUCKET ((uintptr_t)1)

typedef void (*xc_build_cache_fn)(void *c, unsigned idx, double time);

enum {
    XC_C_ID = 0x08, XC_C_REFS = 0x40, XC_C_MODE = 0x68, XC_C_PARENT = 0x70, XC_C_ATTACH = 0xa0,
    XC_C_LIVE = 0xa8, XC_C_REGISTERED = 0xd1, XC_C_SPEC = 0xd8, XC_C_VC = 0xe0, XC_VC_SIZE = 0x80,
    XC_C_FLAGS = 0x1e0, XC_C_MASK = 0x1e4, XC_C_COUNT = 0x1e8, XC_C_ATTENTION = 0x1ec,
    XC_VC_PARENT = 0x00, XC_VC_KIDS = 0x10, XC_VC_ATT = 0x38, XC_VC_DIRTY = 0x3c,
};

typedef struct { unsigned char *b; size_t n, cap; int failed; } xc_buf;

static void xc_put(xc_buf *w, const void *p, size_t n) {
    if (w->failed) return;
    if (w->n + n > w->cap) {
        size_t cap = w->cap ? w->cap : 1u << 20;
        while (cap < w->n + n) cap *= 2;
        unsigned char *b = realloc(w->b, cap);
        if (!b) { w->failed = 1; return; }
        w->b = b;
        w->cap = cap;
    }
    memcpy(w->b + w->n, p, n);
    w->n += n;
}
static void xc_u32(xc_buf *w, uint32_t v) { xc_put(w, &v, 4); }
static void xc_i32(xc_buf *w, int32_t v) { xc_put(w, &v, 4); }
static void xc_u64(xc_buf *w, uint64_t v) { xc_put(w, &v, 8); }
static void xc_f64(xc_buf *w, double v) { xc_put(w, &v, 8); }

typedef struct {
    unsigned char *ptr;
    uint8_t open;
    int32_t kids, kids_at, live, live_at, att, att_at;
} xc_node;

typedef struct {
    unsigned char *entries;
    uint32_t n;
    uint8_t trunc[XC_MAX_ENTRIES];
} xc_level;

typedef struct {
    xc_node *nodes;
    size_t count, cap;
    uintptr_t *pool;
    size_t pool_n, pool_cap;
    uintptr_t *keys;
    uint32_t *vals;
    size_t mask;
    uint32_t classes[XC_MAX_CLASSES];
    size_t nclasses;
    uint64_t *class_bits; /* 2 per node */
    xc_level level[XC_MAX_STACK + 1];
    size_t depth, total, trunc_total;
    unsigned idx;
    uint64_t visits, opens;
    const char *error;
} xc_capture;

static size_t xc_hash(uintptr_t p) {
    uint64_t x = (uint64_t)p;
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdull;
    x ^= x >> 33;
    return (size_t)x;
}

static int xc_map_grow(xc_capture *k) {
    size_t cap = k->mask ? (k->mask + 1) * 2 : 1u << 16;
    uintptr_t *keys = calloc(cap, sizeof *keys);
    uint32_t *vals = calloc(cap, sizeof *vals);
    if (!keys || !vals) { free(keys); free(vals); return 0; }
    for (size_t i = 0; k->keys && i <= k->mask; ++i) {
        if (!k->keys[i]) continue;
        size_t h = xc_hash(k->keys[i]) & (cap - 1);
        while (keys[h]) h = (h + 1) & (cap - 1);
        keys[h] = k->keys[i];
        vals[h] = k->vals[i];
    }
    free(k->keys);
    free(k->vals);
    k->keys = keys;
    k->vals = vals;
    k->mask = cap - 1;
    return 1;
}

/* Index of a recorded component, or -1. */
static int64_t xc_find(const xc_capture *k, const void *p) {
    if (!p || !k->keys) return -1;
    size_t h = xc_hash((uintptr_t)p) & k->mask;
    while (k->keys[h]) {
        if (k->keys[h] == (uintptr_t)p) return k->vals[h];
        h = (h + 1) & k->mask;
    }
    return -1;
}

/* Index of a component, recording it on first sight. */
static int64_t xc_record(xc_capture *k, unsigned char *p) {
    int64_t found = xc_find(k, p);
    if (found >= 0) return found;
    if ((k->count + 1) * 2 > k->mask + 1 && !xc_map_grow(k)) { k->error = "out of memory (map)"; return -1; }
    if (k->count == k->cap) {
        size_t cap = k->cap ? k->cap * 2 : 4096;
        xc_node *n = realloc(k->nodes, cap * sizeof *n);
        if (!n) { k->error = "out of memory (nodes)"; return -1; }
        k->nodes = n;
        k->cap = cap;
    }
    xc_node *n = &k->nodes[k->count];
    memset(n, 0, sizeof *n);
    n->ptr = p;
    n->kids = n->live = n->att = -1;
    size_t h = xc_hash((uintptr_t)p) & k->mask;
    while (k->keys[h]) h = (h + 1) & k->mask;
    k->keys[h] = (uintptr_t)p;
    k->vals[h] = (uint32_t)k->count;
    return (int64_t)k->count++;
}

static int32_t xc_pool_push(xc_capture *k, uintptr_t v) {
    if (k->pool_n == k->pool_cap) {
        size_t cap = k->pool_cap ? k->pool_cap * 2 : 1u << 16;
        uintptr_t *p = realloc(k->pool, cap * sizeof *p);
        if (!p) { k->error = "out of memory (pool)"; return -1; }
        k->pool = p;
        k->pool_cap = cap;
    }
    k->pool[k->pool_n] = v;
    return (int32_t)k->pool_n++;
}

static unsigned char *xc_ptr(const unsigned char *c, size_t off) { return *(unsigned char *const *)(c + off); }
static uint32_t xc_u32_at(const unsigned char *c, size_t off) { uint32_t v; memcpy(&v, c + off, 4); return v; }
static unsigned char *xc_vc(const unsigned char *c, unsigned idx) { return (unsigned char *)c + XC_C_VC + idx * XC_VC_SIZE; }

/* Specification list: entries, count. Inline when capacity is 4. */
static unsigned char *xc_spec_entries(const unsigned char *spec, uint64_t *count) {
    uint64_t cap, n;
    memcpy(&cap, spec + 0x80, 8);
    memcpy(&n, spec + 0x88, 8);
    *count = n;
    return cap == 4 ? (unsigned char *)spec : *(unsigned char **)spec;
}

static int xc_is_class(unsigned char *c, uint32_t cls) {
    void **vtbl = *(void ***)c;
    unsigned char (*fn)(void *, int) = (unsigned char (*)(void *, int))vtbl[XC_SLOT_IS_CLASS];
    return fn(c, (int)cls) != 0;
}

/* Non-null entries of a bucketed component container (with markers for the attachment form). */
static void xc_container(xc_capture *k, unsigned char *cont, int markers, int32_t *n_out, int32_t *at_out) {
    *n_out = -1;
    if (!cont) return;
    int32_t count;
    memcpy(&count, cont + 0x20, 4);
    unsigned char *b = xc_ptr(cont, 0x08), *e = xc_ptr(cont, 0x10);
    int32_t n = 0, at = (int32_t)k->pool_n;
    if (markers && count == 0) b = e; /* the attachment iterator 0x314fa0 yields nothing */
    for (unsigned char *bucket = b; bucket < e && !k->error; bucket += 0x20) {
        unsigned char **p = *(unsigned char ***)bucket, **end = *(unsigned char ***)(bucket + 8);
        if (markers && bucket != b) { xc_pool_push(k, XC_BUCKET); ++n; }
        for (; p < end && !k->error; ++p) {
            if (!*p && !markers) continue;
            xc_pool_push(k, (uintptr_t)*p);
            ++n;
        }
    }
    *n_out = n;
    *at_out = at;
}

static void xc_walk(xc_capture *k, unsigned char *c, int depth) {
    if (k->error) return;
    if (depth > XC_MAX_DEPTH) { k->error = "tree deeper than 64"; return; }
    int64_t me = xc_record(k, c);
    if (me < 0) return;
    ++k->visits;
    int32_t count = (int32_t)xc_u32_at(c, XC_C_COUNT);
    if (k->total == k->trunc_total && count == 0) return;
    ++k->opens;
    k->nodes[me].open = 1;
    unsigned char *spec = xc_ptr(c, XC_C_SPEC);
    int pushed = 0;
    if (spec) {
        if (k->depth == XC_MAX_STACK) { k->error = "specification stack overrun"; return; }
        uint64_t n;
        unsigned char *entries = xc_spec_entries(spec, &n);
        if (n > XC_MAX_ENTRIES) { k->error = "specification list longer than 64"; return; }
        xc_level *l = &k->level[k->depth++];
        l->entries = entries;
        l->n = (uint32_t)n;
        for (uint32_t i = 0; i < l->n; ++i) l->trunc[i] = entries[i * 0x20 + 0xc] != 0;
        k->total += n;
        pushed = 1;
    }
    /* truncation at this component, undone after the subtree */
    struct { uint8_t s, i; } mine[XC_MAX_ENTRIES];
    int nmine = 0;
    for (size_t s = 0; s < k->depth; ++s) {
        xc_level *l = &k->level[s];
        for (uint32_t i = 0; i < l->n; ++i) {
            if (l->trunc[i]) continue;
            uint32_t cls = xc_u32_at(l->entries + i * 0x20, 8);
            if (cls == XC_CLASS_NONE || !xc_is_class(c, cls)) continue;
            if (nmine == XC_MAX_ENTRIES) { k->error = "more than 64 truncations at one component"; return; }
            l->trunc[i] = 1;
            mine[nmine].s = (uint8_t)s;
            mine[nmine].i = (uint8_t)i;
            ++nmine;
            ++k->trunc_total;
        }
    }
    /* children: the cached vector, plus the live container where a child refresh is pending */
    unsigned char *vc = xc_vc(c, k->idx);
    unsigned char **kb = *(unsigned char ***)(vc + XC_VC_KIDS), **ke = *(unsigned char ***)(vc + XC_VC_KIDS + 8);
    xc_node *n = &k->nodes[me];
    n->kids = 0;
    n->kids_at = (int32_t)k->pool_n;
    for (unsigned char **p = kb; p < ke && !k->error; ++p) {
        xc_pool_push(k, (uintptr_t)*p);
        ++k->nodes[me].kids;
    }
    if (xc_u32_at(vc, XC_VC_DIRTY) & 0x2) {
        int32_t ln, la;
        xc_container(k, xc_ptr(c, XC_C_LIVE), 0, &ln, &la);
        k->nodes[me].live = ln;
        k->nodes[me].live_at = la;
    }
    int32_t att_n, att_at;
    xc_container(k, xc_ptr(c, XC_C_ATTACH), 1, &att_n, &att_at);
    k->nodes[me].att = att_n;
    k->nodes[me].att_at = att_at;
    xc_node snap = k->nodes[me];
    for (int32_t i = 0; i < snap.kids && !k->error; ++i) xc_walk(k, (unsigned char *)k->pool[snap.kids_at + i], depth + 1);
    for (int32_t i = 0; snap.live > 0 && i < snap.live && !k->error; ++i)
        xc_walk(k, (unsigned char *)k->pool[snap.live_at + i], depth + 1);
    for (int32_t i = 0; snap.att > 0 && i < snap.att && !k->error; ++i) {
        uintptr_t a = k->pool[snap.att_at + i];
        if (a != XC_HOLE && a != XC_BUCKET) xc_record(k, (unsigned char *)a);
    }
    for (int m = 0; m < nmine; ++m) {
        k->level[mine[m].s].trunc[mine[m].i] = 0;
        --k->trunc_total;
    }
    if (pushed) {
        k->total -= k->level[--k->depth].n;
    }
}

static void xc_add_class(xc_capture *k, uint32_t cls) {
    if (cls == XC_CLASS_NONE || cls >= 128) return;
    for (size_t i = 0; i < k->nclasses; ++i)
        if (k->classes[i] == cls) return;
    if (k->nclasses < XC_MAX_CLASSES) k->classes[k->nclasses++] = cls;
    else k->error = "too many truncation classes";
}

static void xc_evaluate_classes(xc_capture *k) {
    for (size_t i = 0; i < k->count && !k->error; ++i) {
        unsigned char *spec = xc_ptr(k->nodes[i].ptr, XC_C_SPEC);
        if (!spec) continue;
        uint64_t n;
        unsigned char *e = xc_spec_entries(spec, &n);
        for (uint64_t j = 0; j < n && j < XC_MAX_ENTRIES; ++j) xc_add_class(k, xc_u32_at(e + j * 0x20, 8));
    }
    free(k->class_bits);
    k->class_bits = calloc(k->count * 2 + 2, sizeof *k->class_bits);
    if (!k->class_bits) { k->error = "out of memory (classes)"; return; }
    for (size_t i = 0; i < k->count; ++i)
        for (size_t c = 0; c < k->nclasses; ++c)
            if (xc_is_class(k->nodes[i].ptr, k->classes[c]))
                k->class_bits[i * 2 + k->classes[c] / 64] |= 1ull << (k->classes[c] % 64);
}

/* -2 = a component outside the capture, -1 = null. */
static int32_t xc_ref(const xc_capture *k, const void *p) {
    if (!p) return -1;
    int64_t i = xc_find(k, p);
    return i < 0 ? -2 : (int32_t)i;
}

static void xc_spec_out(xc_buf *w, const unsigned char *c) {
    unsigned char *spec = xc_ptr(c, XC_C_SPEC);
    if (!spec) { xc_i32(w, -1); return; }
    uint64_t n;
    unsigned char *e = xc_spec_entries(spec, &n);
    xc_i32(w, (int32_t)n);
    for (uint64_t j = 0; j < n; ++j) {
        const unsigned char *x = e + j * 0x20;
        xc_put(w, x, 12);
        xc_u32(w, x[0xc]);
        xc_put(w, x + 0x10, 16);
    }
}

static void xc_list_out(xc_buf *w, const xc_capture *k, int32_t n, int32_t at, int markers) {
    xc_i32(w, n);
    for (int32_t i = 0; i < n; ++i) {
        uintptr_t v = k->pool[at + i];
        if (markers && v == XC_HOLE) xc_u32(w, 0xffffffffu);
        else if (markers && v == XC_BUCKET) xc_u32(w, 0xfffffffeu);
        else {
            int32_t r = xc_ref(k, (const void *)v);
            xc_u32(w, r < 0 ? 0xfffffffdu : (uint32_t)r);
        }
    }
}

static void xc_pre_out(xc_buf *w, const xc_capture *k) {
    for (size_t i = 0; i < k->count; ++i) {
        const xc_node *n = &k->nodes[i];
        const unsigned char *c = n->ptr;
        uint64_t id;
        memcpy(&id, c + XC_C_ID, 8);
        xc_u64(w, id);
        xc_u64(w, (uint64_t)(uintptr_t)c);
        xc_i32(w, xc_ref(k, xc_ptr(c, XC_C_PARENT)));
        xc_u32(w, xc_u32_at(c, XC_C_FLAGS));
        xc_u32(w, xc_u32_at(c, XC_C_MASK));
        xc_u32(w, xc_u32_at(c, XC_C_COUNT));
        xc_u32(w, xc_u32_at(xc_vc(c, 0), XC_VC_DIRTY));
        xc_u32(w, xc_u32_at(xc_vc(c, 1), XC_VC_DIRTY));
        xc_u64(w, k->class_bits[i * 2]);
        xc_u64(w, k->class_bits[i * 2 + 1]);
        xc_u32(w, c[XC_C_REGISTERED] != 0);
        xc_u32(w, xc_u32_at(c, XC_C_MODE));
        xc_u32(w, xc_u32_at(c, XC_C_ATTENTION));
        xc_u32(w, xc_u32_at(xc_vc(c, 0), XC_VC_ATT));
        xc_u32(w, xc_u32_at(xc_vc(c, 1), XC_VC_ATT));
        xc_i32(w, xc_ref(k, xc_ptr(xc_vc(c, 0), XC_VC_PARENT)));
        xc_i32(w, xc_ref(k, xc_ptr(xc_vc(c, 1), XC_VC_PARENT)));
        xc_u32(w, xc_u32_at(c, XC_C_REFS));
        xc_u32(w, n->open);
        xc_list_out(w, k, n->kids, n->kids_at, 0);
        xc_list_out(w, k, n->live, n->live_at, 0);
        xc_list_out(w, k, n->att, n->att_at, 1);
        xc_spec_out(w, c);
    }
}

static void xc_post_out(xc_buf *w, const xc_capture *k) {
    for (size_t i = 0; i < k->count; ++i) {
        const unsigned char *c = k->nodes[i].ptr;
        xc_u32(w, xc_u32_at(c, XC_C_FLAGS));
        xc_u32(w, xc_u32_at(c, XC_C_COUNT));
        xc_u32(w, xc_u32_at(xc_vc(c, 0), XC_VC_DIRTY));
        xc_u32(w, xc_u32_at(xc_vc(c, 1), XC_VC_DIRTY));
        xc_u32(w, xc_u32_at(xc_vc(c, 0), XC_VC_ATT));
        xc_u32(w, xc_u32_at(xc_vc(c, 1), XC_VC_ATT));
        xc_i32(w, xc_ref(k, xc_ptr(xc_vc(c, 0), XC_VC_PARENT)));
        xc_i32(w, xc_ref(k, xc_ptr(xc_vc(c, 1), XC_VC_PARENT)));
        xc_u32(w, xc_u32_at(c, XC_C_REFS));
        xc_spec_out(w, c);
    }
}

typedef struct {
    uint32_t number;
    uint64_t frame;
    uint32_t trace; /* the caller appends a trace section after the post-state: u32 n, then n x
                       {u32 kind, u32 node, u32 a, u32 b}: 6 PCF (flags, dirty before),
                       8 PCF done (dirty after), 7 BuildCache entered (node) */
} xc_meta;

/* Records, runs the original build (through `run`), records again; writes the capture into w.
   Returns NULL on success (w holds the file, minus the trace section) or the reason nothing
   was captured; *ran says whether the original build has run. */
static const char *xc_capture_frame(xc_capture *k, xc_buf *w, void *galaxy, unsigned idx, double time,
                                    const xc_meta *meta, void (*run)(void *, unsigned, double, void *), void *ctx,
                                    int *ran) {
    *ran = 0;
    uint64_t spec_depth, trunc_depth, total;
    memcpy(&spec_depth, image + XC_RVA_SPEC_DEPTH, 8);
    memcpy(&trunc_depth, image + XC_RVA_TRUNC_DEPTH, 8);
    memcpy(&total, image + XC_RVA_ENTRY_TOTAL, 8);
    if (spec_depth || trunc_depth || total) return "engine stacks not empty";
    k->count = k->pool_n = k->nclasses = 0;
    k->depth = k->total = k->trunc_total = 0;
    k->visits = k->opens = 0;
    k->idx = idx;
    k->error = NULL;
    if (k->keys) memset(k->keys, 0, (k->mask + 1) * sizeof *k->keys);
    xc_walk(k, (unsigned char *)galaxy, 0);
    /* live parent chains, so that expiry counts (which follow +0x70) stay inside the capture */
    for (size_t i = 0; i < k->count && !k->error; ++i) {
        unsigned char *p = xc_ptr(k->nodes[i].ptr, XC_C_PARENT);
        for (int guard = 0; p && guard < XC_MAX_DEPTH && !k->error; ++guard) {
            if (xc_find(k, p) >= 0) break;
            xc_record(k, p);
            p = xc_ptr(p, XC_C_PARENT);
        }
    }
    if (!k->error) xc_evaluate_classes(k);
    if (k->error) return k->error;
    w->n = 0;
    w->failed = 0;
    xc_put(w, "X4XFCAP1", 8);
    xc_u32(w, 1);
    xc_u32(w, meta->number);
    xc_u64(w, meta->frame);
    xc_f64(w, time);
    xc_u32(w, idx);
    xc_u32(w, (uint32_t)(image[XC_RVA_DROP_POSE] != 0) | (meta->trace ? 2u : 0u));
    xc_u64(w, k->visits);
    xc_u64(w, k->opens);
    xc_u32(w, (uint32_t)k->nclasses);
    for (size_t i = 0; i < k->nclasses; ++i) xc_u32(w, k->classes[i]);
    xc_u32(w, (uint32_t)k->count);
    xc_pre_out(w, k);
    if (w->failed) return "out of memory (pre-state)";
    run(galaxy, idx, time, ctx);
    *ran = 1;
    xc_post_out(w, k);
    return w->failed ? "out of memory (post-state)" : NULL;
}
