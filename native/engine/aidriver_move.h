/* Stage 1: our reimplementation of AIDirector::Move 0x106d250 (engine rewrite program, ai-driver;
 * included by aidriver.h). Report: reports/REWRITE-AI-DRIVER.md section 5.
 *
 * Behaviour (from full disassembly + decompile of the build-pinned body; described, not copied):
 * up to 0x40 passes; each pass flips the write index (except pass 0x21), takes the pre-flip write
 * vector as this pass's read vector, harvests due timed wakes into it when it is empty (and leaves
 * the loop when it is still empty), then processes every queued context: dequeue, select the wake
 * mask (timed: the timed mask when the wake time is due, else 0), log the infinite-loop diagnostic in
 * pass 0x20, run global interrupt handlers (mask 0x40), then the generic path: interrupt restart
 * (mask 4), attention-block restart, resume and ExecState_Run, and on script completion return the
 * value to the caller, re-wake it (mask 0x20) and remove the context. At the end the script refresh
 * lists are processed. Every engine operation is the engine's own helper, called with the arguments
 * the original passes; only the control flow is ours. Two places inline engine data-structure edits
 * in the original (timed-tree node release into the pool, in the timed harvest and in the global-
 * interrupt target cleanup); those are reproduced step for step.
 *
 * Modes: "normal" processes each pass in the original order. "rounds" processes each pass grouped
 * by bucket (sector of the owner entity), buckets in order of first appearance in the pass's read
 * vector, stable inside a bucket; with an event buffer bound (walkpar, Q4/Q7) every post made while a
 * bucket runs is deferred and replayed at the end of the pass, before the next flip, so the pass
 * structure (which contexts run in which pass) is the original's. "compare" = rounds plus the
 * order-equivalence counters (cross-bucket ClearWake of a pending context, events delivered to a
 * pending context of another bucket inside a pass); a pass with neither is order-equivalent. */

/* ---- engine helpers (RVAs pinned in signatures.py AIDRIVER_HELPERS) ---------------------------- */

typedef struct { const char *data; size_t len; } ad_strview;
typedef struct { union { char buf[16]; char *ptr; } u; size_t size; size_t cap; } ad_msvc_string;

typedef struct {
    void (*request_wake)(void *dir, void *ctx);
    void (*clear_wake)(void *ctx);
    void (*clear_timed_wake)(void *ctx);
    void (*clear_timed_wake_if_due)(void *ctx, uintptr_t force);
    void (*remove_context)(void *dir, void *ctx);
    void (*tree_extract)(void *node);
    void (*vec_insert)(void *vec, void *pos, void **value);
    void (*exec_run)(void *es);
    uintptr_t (*irq_run)(void *es, void *list, uintptr_t start, uintptr_t count, uintptr_t flag);
    void *(*blocking_action)(void *es);
    uintptr_t (*attention_changed)(void *ctx);
    void *(*attention_block)(void *script, uintptr_t attention);
    void (*jump_to_label)(void *es, uintptr_t restart);
    void (*unregister_all)(void *listener);
    void (*ctx_reset)(void *ctx);
    void (*record_clear)(void *rec);
    void (*record_copy)(void *dst, void *src);
    void (*value_release)(void *v);
    void (*value_type_error)(void *v, uintptr_t expected);
    void (*value_assign)(void *dst, void *src);
    void (*resume_after_restore)(void *ctx);
    void (*return_run_script)(void *act, void *parent_es, void *es);
    void (*return_order_value)(void *dst, void *frame, void *src, void *val);
    void *(*frame_context)(void *frame);
    void (*value_error)(void *v, void *msg, void *where);
    uintptr_t (*eval_string)(void *expr, void *frame, void *out, uintptr_t a4, uintptr_t a5);
    void *(*find_script)(void *dir, void *name_view);
    uintptr_t (*eval_bool)(void *expr, void *frame, uintptr_t a3);
    void (*expr_error)(void *frame, void *msg, void *expr);
    void (*string_assign)(void *dst, const char *src, size_t len);
    void **(*abort_reason)(void *table, void *tmp, uintptr_t kw);
    void (*abort_stack)(void *dir, void *es, uintptr_t a3, uintptr_t a4, uintptr_t a5, uintptr_t a6, void *reason, void *frame);
    void *(*push_script)(void *dir, void *es, void *name_view, void *params, void *frame);
    void (*tree_erase)(void *head, void *out, void *node);
    uintptr_t (*refresh_scripts)(void *dir, void *list, uintptr_t dry);
    void (*list_clear)(void *list);
    void *(*str_format)(void *out, const char *fmt, ...);
    void (*log_error)(const char *fmt, ...);
    void (*id_to_string)(void *out, uint64_t id);
    void (*op_delete)(void *p, size_t n);
    void (*invalid_parameter)(void);
    void (*tls_init)(void);
    double (*game_time)(void);    /* the calling thread's TLS game time (tests substitute a clock) */
    void *abort_table;            /* data 0x3d3cea0 */
    const uint64_t *order_result_key; /* data 0x31be4c0 */
} ad_engine_t;
static ad_engine_t E;

/* offsets used only by the driver (in addition to aidriver.h) */
#define AD_D_FREE_NODES 0x1d0     /* timed-tree node pool (singly linked through node+0x10) */
#define AD_D_ALL 0x148            /* vector of all contexts */
#define AD_D_REFRESH_A 0x240      /* script refresh request list (applied, not dry) */
#define AD_D_REFRESH_B 0x258      /* script refresh request list (dry run) */
#define AD_C_RECORD 0x18          /* ScriptEventHandler event record: int kw +0, values +8/+0x18/+0x28/+0x38 */
#define AD_C_PENDING 0xc0         /* vector of 0x50-byte pending interrupt records */
#define AD_C_CACHED_ATT 0x128
#define AD_C_RESTORING 0x19a      /* byte */
#define AD_C_ATTACHED 0x19b       /* byte: blocking action attached */
#define AD_C_PRIORITY 0x19c       /* int */
#define AD_C_SELECTED 0x1a0       /* u64 handler index chosen by the interrupt runner, -1 none */
#define AD_C_RECORD2 0x1c0        /* second event record (kw +0, values +8 .. +0x38) */
#define AD_E_PARENT 0x00
#define AD_E_CHILD 0x08
#define AD_E_PROGRAM 0x10         /* vector<Action*>* */
#define AD_E_PC 0x18
#define AD_E_SAVED_PC 0x20
#define AD_E_TIME 0x28            /* double */
#define AD_E_YIELD 0x30           /* byte */
#define AD_E_NOATT 0x31           /* byte: no attention restart */
#define AD_E_FRAME 0x38           /* Value (tag +0, ptr +8) holding the owning context (tag 0x66) */
#define AD_E_RESULTS 0x60         /* map header node; root at +0x68 */
#define AD_E_LABEL 0xa0           /* std::string */
#define AD_S_HANDLERS 0x148       /* AIScript: inline handler pointer array, count at +0x248 */
#define AD_S_HANDLER_COUNT 0x248
#define AD_S_PRIORITY 0x5c
#define AD_ENTITY_ATTENTION 0x1ec
#define AD_TAG_CONTEXT 0x66
#define AD_KW_RUN_SCRIPT 0x9e3
#define AD_KW_RUN_ORDER_SCRIPT 0x9ed
#define AD_NO_WAKE (-0.9999)
#define AD_DATA_ABORT_TABLE 0x3d3cea0u   /* abort-reason table passed to 0xacb4e0 */
#define AD_DATA_RESULT_KEY 0x31be4c0u    /* u64 name id of the order-script result variable */
#define AD_MSG_NO_SCRIPT "AI script name '%s' not found"
#define AD_MSG_NO_RETURN "Returning order script '%s' did not provide a return value"

static inline unsigned char *P(const void *p, size_t off) { return (unsigned char *)p + off; }
#define U8(p, o) (*(uint8_t *)P(p, o))
#define I32(p, o) (*(int32_t *)P(p, o))
#define U32(p, o) (*(uint32_t *)P(p, o))
#define I64(p, o) (*(int64_t *)P(p, o))
#define U64(p, o) (*(uint64_t *)P(p, o))
#define F64(p, o) (*(double *)P(p, o))
#define PTR(p, o) (*(void **)P(p, o))

static int ad_refcounted(int tag) { return (unsigned)(tag - 0x54) < 0x13u; }

/* Value copy with refcount, as the original does when it saves the chosen global-interrupt record. */
typedef struct { int32_t tag; int32_t pad; int32_t *ref; } ad_value;
static void ad_value_take(ad_value *v, const void *src) {
    v->tag = I32(src, 0); v->pad = 0; v->ref = (int32_t *)PTR(src, 8);
    if (ad_refcounted(v->tag) && v->ref) ++*v->ref;
}
static void ad_value_drop(ad_value *v) { if (ad_refcounted(v->tag)) E.value_release(v); }
/* Assign a saved value into an engine Value slot: release the old, store, add a reference. */
static void ad_value_store(void *dst, const ad_value *v) {
    if ((const void *)dst == (const void *)v) return;
    if (ad_refcounted(I32(dst, 0))) E.value_release(dst);
    I32(dst, 0) = v->tag;
    PTR(dst, 8) = v->ref;
    if (ad_refcounted(v->tag) && v->ref) ++*v->ref;
}

static void ad_str_init(ad_msvc_string *s) { s->u.buf[0] = 0; s->size = 0; s->cap = 15; }
static const char *ad_str_c(const ad_msvc_string *s) { return s->cap > 15 ? s->u.ptr : s->u.buf; }
/* MSVC std::string destructor: aligned big blocks keep the real pointer just before the data. */
static void ad_str_free(ad_msvc_string *s) {
    if (s->cap > 15) {
        size_t n = s->cap + 1;
        char *p = s->u.ptr;
        if (n >= 0x1000) {
            char *real = ((char **)p)[-1];
            if ((size_t)(p - real) - 8 > 0x1f) E.invalid_parameter();
            n += 0x27;
            p = real;
        }
        E.op_delete(p, n);
    }
    ad_str_init(s);
}
static const char *ad_engine_str_c(const void *s) {   /* engine std::string at s */
    return U64(s, 0x18) > 15 ? (const char *)PTR(s, 0) : (const char *)s;
}

/* The calling thread's engine game time, initialising the engine TLS block on demand (as the
   original does before each read of TLS+0x300). */
static double ad_tls_game_time(void) {
    unsigned char *teb = (unsigned char *)NtCurrentTeb();
    void **array = *(void ***)(teb + 0x58);
    unsigned index = *(volatile unsigned *)(image + AD_TLS_INDEX_RVA);
    unsigned char *block = array[index];
    if (!block[AD_TLS_INIT]) E.tls_init();
    return *(volatile double *)(block + AD_TLS_GAME_TIME);
}
static double ad_now_game(void) { return E.game_time(); }

/* In-order successor in the timed tree, exactly as the original walks it: right subtree's leftmost,
   else climb until we arrive from a left child (the head node terminates the climb). */
static unsigned char *ad_tree_next(unsigned char *node) {
    unsigned char *r = PTR(node, AD_N_RIGHT);
    if (r) {
        while (PTR(r, AD_N_LEFT)) r = PTR(r, AD_N_LEFT);
        return r;
    }
    unsigned char *child = node, *parent = PTR(node, AD_N_PARENT);
    while (PTR(parent, AD_N_LEFT) != child) { child = parent; parent = PTR(parent, AD_N_PARENT); }
    return parent;
}

/* Release a timed-tree node the way Move does inline: fix the leftmost cache, extract, push the node
   onto the director's node pool, decrement the size. Returns the successor. */
static unsigned char *ad_tree_release(void *dir, unsigned char *node) {
    unsigned char *next = ad_tree_next(node);
    if (PTR(dir, AD_D_TIMED_LEFT) == node) PTR(dir, AD_D_TIMED_LEFT) = next;
    E.tree_extract(node);
    PTR(node, AD_N_PARENT) = NULL;
    PTR(node, AD_N_LEFT) = NULL;
    PTR(node, AD_N_RIGHT) = PTR(dir, AD_D_FREE_NODES);
    PTR(dir, AD_D_FREE_NODES) = node;
    --I64(dir, AD_D_TIMED_SIZE);
    return next;
}

/* Append ctx to wake vector `vec`, recording its slot in `slot` (the inline push of the harvest). */
static void ad_vec_push(void **vec, void *ctx) {
    void **end = (void **)vec[1];
    if (end == (void **)vec[2]) { void *value = ctx; E.vec_insert(vec, end, &value); }
    else { *end = ctx; vec[1] = end + 1; }
}

/* Timed harvest into the (empty) read vector of buffer `r`: every node whose time is due. */
static void ad_harvest_timed(void *dir, unsigned r) {
    unsigned char *head = P(dir, AD_D_TIMED);
    unsigned char *node = PTR(dir, AD_D_TIMED_LEFT);
    void **vec = ad_vec(dir, r);
    while (node != head) {
        if (ad_now_game() < F64(node, AD_N_TIME)) break;
        void *ctx = PTR(node, AD_N_CTX);
        if (ctx) {
            PTR(ctx, AD_C_TITER) = head;
            I64(ctx, AD_C_SLOT + 8 * r) = (int64_t)(((uintptr_t)vec[1] - (uintptr_t)vec[0]) / sizeof(void *));
            ad_vec_push(vec, ctx);
        }
        node = ad_tree_release(dir, node);
    }
}

/* ---- per-context processing --------------------------------------------------------------------- */

static size_t ad_vec_len(const void *vec) { return ((uintptr_t)PTR(vec, 8) - (uintptr_t)PTR(vec, 0)) / sizeof(void *); }
static void *ad_vec_at(const void *vec, size_t i) { return i < ad_vec_len(vec) ? ((void **)PTR(vec, 0))[i] : NULL; }
static void *ad_dir(void) { return *(void **)(image + AD_DIRECTOR_RVA); }

/* The context a frame Value (tag 0x66) refers to; the engine's own type error otherwise. */
static void *ad_frame_owner(void *frame) {
    if (I32(frame, 0) != AD_TAG_CONTEXT) { E.value_type_error(frame, AD_TAG_CONTEXT); return NULL; }
    unsigned char *p = PTR(frame, 8);
    return p ? p - 0xa8 : NULL;
}

static void ad_loop_diagnostic(void *ctx, uint32_t mask) {
    const char *why = "";
    if (mask & 1) why = " (Most likely cause: Script starts and returns immediately)";
    else if (mask & 0x20) why = " (Most likely cause: Script calls a script that returns immediately)";
    else if (mask & 0x40) why = " (Most likely cause: Global interrupt handler triggers itself)";
    else if (mask & 2) why = " (Most likely cause: A blocking action that is completed immediately, running in a loop)";
    else if (mask & 4) why = " (Most likely cause: A blocking action is repeatedly interrupted by an <interrupt> event that gets triggered immediately)";
    else if (mask & 8) why = " (Most likely cause: A blocking action is repeatedly interrupted by <interrupt_after_time> with a non-positive time value)";
    void *entity = PTR(ctx, AD_C_ENTITY);
    ad_msvc_string ent, msg;
    ad_str_init(&ent); ad_str_init(&msg);
    E.id_to_string(&ent, entity ? U64(entity, AD_COMP_ID) : 0);
    E.str_format(&msg, "%s on entity %s", ad_engine_str_c(P(PTR(ctx, AD_C_SCRIPT), AD_SCRIPT_NAME)), ad_str_c(&ent));
    ad_str_free(&ent);
    E.log_error("AIDirector::Move(): Possibly infinite wake request loop: %s, wake reason mask=%04x%s", ad_str_c(&msg), mask, why);
    ad_str_free(&msg);
    ++ad.win.loop_logs;
}

/* Interrupt-runner call with census accounting around it. */
static uintptr_t ad_irq(void *ctx, void *es, void *list, uintptr_t start, uintptr_t count, uintptr_t flag, uint32_t mask) {
    ad_run_frame f;
    ad_run_begin(&f, ctx, es, mask, 1);
    uintptr_t result = E.irq_run(es, list, start, count, flag);
    ad_run_end(&f, es);
    return result;
}

/* Global interrupt handlers (mask 0x40): choose at most one handler over the pending records (the
   first record whose selected handler either has no script, or has a script whose priority beats the
   current owner's), then apply it. Returns 1 when a handler was applied (done with this context for
   the pass), 0 when none was chosen (the generic path continues without bit 0x40). */
static int ad_global_interrupts(void *dir, void *ctx, void *es, uint32_t mask) {
    unsigned char *handlers = P(PTR(ctx, AD_C_SCRIPT), AD_S_HANDLERS);
    void *inner = es;
    while (PTR(inner, AD_E_CHILD)) inner = PTR(inner, AD_E_CHILD);
    void *owner_ctx = ad_frame_owner(P(inner, AD_E_FRAME));
    void *frame = P(es, AD_E_FRAME);
    void *best_rec = NULL, *best_script = NULL;
    size_t best_idx = (size_t)-1;
    size_t count0 = (U64(ctx, AD_C_PENDING + 8) - U64(ctx, AD_C_PENDING)) / 0x50;
    for (size_t i = 0; i < count0;) {
        unsigned char *rec = (unsigned char *)PTR(ctx, AD_C_PENDING) + i * 0x50;
        uint64_t hidx = U64(rec, 0);
        void *h = hidx < U64(handlers, AD_S_HANDLER_COUNT - AD_S_HANDLERS) ? PTR(handlers, hidx * 8) : NULL;
        void *list = h ? PTR(h, 0x20) : NULL;
        if (list && PTR(list, 0) != PTR(list, 8)) {
            I32(ctx, AD_C_RECORD2) = I32(rec, 8);
            E.value_assign(P(ctx, AD_C_RECORD2 + 0x08), rec + 0x10);
            E.value_assign(P(ctx, AD_C_RECORD2 + 0x18), rec + 0x20);
            E.value_assign(P(ctx, AD_C_RECORD2 + 0x28), rec + 0x30);
            E.value_assign(P(ctx, AD_C_RECORD2 + 0x38), rec + 0x40);
            ad_irq(ctx, es, list, 0, ad_vec_len(list), 0, mask);
            uint64_t sel = U64(ctx, AD_C_SELECTED);
            if (sel < ad_vec_len(list)) {
                U64(ctx, AD_C_SELECTED) = (uint64_t)-1;
                if (!best_rec) {
                    void *cand = ad_vec_at(list, sel);
                    if (!I32(cand, 0x28)) { best_rec = cand; best_idx = i; }   /* handler without a script */
                    else {
                        ad_msvc_string name;
                        ad_str_init(&name);
                        if (E.eval_string(P(cand, 0x28), frame, &name, 0, 0) & 0xff) {
                            ad_strview view = {ad_str_c(&name), name.size};
                            void *scr = E.find_script(dir, &view);
                            if (!scr) {
                                ad_msvc_string msg;
                                ad_str_init(&msg);
                                E.str_format(&msg, AD_MSG_NO_SCRIPT, ad_str_c(&name));
                                E.expr_error(frame, &msg, P(cand, 0x28));
                                ad_str_free(&msg);
                            } else {
                                void *holder = (E.eval_bool(P(cand, 0x80), frame, 0) & 0xff) ? ctx : owner_ctx;
                                if (holder && I32(scr, AD_S_PRIORITY) > I32(holder, AD_C_PRIORITY)) {
                                    best_rec = cand; best_script = scr; best_idx = i;
                                }
                            }
                        }
                        ad_str_free(&name);
                    }
                }
            }
            E.record_clear(P(ctx, AD_C_RECORD2));
        }
        ++i;
        if (!(i < (U64(ctx, AD_C_PENDING + 8) - U64(ctx, AD_C_PENDING)) / 0x50)) break;
    }
    if (!best_rec) return 0;

    /* apply the chosen handler */
    unsigned char *rec = (unsigned char *)PTR(ctx, AD_C_PENDING) + best_idx * 0x50;
    int32_t kw = I32(rec, 8);
    ad_value v[4];
    for (int k = 0; k < 4; ++k) ad_value_take(&v[k], rec + 0x10 + 0x10 * k);
    if (U64(best_rec, 0xa0)) {
        void *dst = P(es, AD_E_LABEL), *src = P(best_rec, 0x90);
        if (dst != src) E.string_assign(dst, ad_engine_str_c(src), U64(best_rec, 0xa0));
        U8(ctx, AD_C_ATTACHED) = 0;
    }
    void *target_es = inner, *target = owner_ctx;
    if (E.eval_bool(P(best_rec, 0x80), frame, 0) & 0xff) {
        unsigned char tmp[64];
        void **reason = E.abort_reason(E.abort_table, tmp, (uintptr_t)(uint32_t)I32(best_rec, 8));
        E.abort_stack(dir, PTR(es, AD_E_CHILD), 0, 0, 0, 0, *reason, frame);
        U8(ctx, AD_C_RESTORING) = 0;
        target_es = es; target = ctx;
    }
    if (target) {
        void *g = ad_dir();
        ++ad.win.global_applied;   /* counted at ClearWake(target), as the census does */
        E.clear_wake(target);
        U32(target, AD_C_TMASK) = 0;
        F64(target, AD_C_WAKETIME) = -1.0;
        unsigned char *head = P(g, AD_D_TIMED), *node = PTR(target, AD_C_TITER);
        if (node != head) { ad_tree_release(g, node); PTR(target, AD_C_TITER) = head; }
        if (!best_script) {
            uint8_t attached = U8(target, AD_C_ATTACHED);
            if (!attached) { E.jump_to_label(target_es, 0); attached = U8(target, AD_C_ATTACHED); }
            U8(target, AD_C_RESTORING) = 0;
            int reattached = 0;
            void *tes = PTR(target, AD_C_EXEC);
            if (attached && U8(tes, AD_E_YIELD)) {
                void *act = ad_vec_at(PTR(tes, AD_E_PROGRAM), U64(tes, AD_E_SAVED_PC));
                if (act) {
                    uintptr_t (**vt)(void *, uintptr_t) = *(uintptr_t (***)(void *, uintptr_t))act;
                    reattached = (vt[1](act, 0x7fffffff) & 0xff) && (vt[6](act, (uintptr_t)PTR(target, AD_C_EXEC)) & 0xff);
                }
            }
            if (!reattached) { U32(target, AD_C_MASK) |= 0x80; E.request_wake(g, target); }
        } else {
            uint64_t saved_pc = U8(target_es, AD_E_YIELD) ? U64(target_es, AD_E_SAVED_PC) : U64(target_es, AD_E_PC);
            if (!U8(target, AD_C_ATTACHED)) { F64(target_es, AD_E_TIME) = -1.0; U8(target_es, AD_E_YIELD) = 0; }
            E.unregister_all(target);
            E.ctx_reset(target);
            E.record_clear(P(target, AD_C_RECORD));
            U8(target, AD_C_RESTORING) = 1;
            I32(ctx, AD_C_RECORD) = kw;
            for (int k = 0; k < 4; ++k) ad_value_store(P(ctx, AD_C_RECORD + 8 + 0x10 * k), &v[k]);
            ad_strview view = {ad_engine_str_c(P(best_script, AD_SCRIPT_NAME)), U64(best_script, AD_SCRIPT_NAME + 0x10)};
            if (!E.push_script(g, target_es, &view, P(best_rec, 0x40), frame)) {
                if (!U8(target_es, AD_E_YIELD)) {
                    U64(target_es, AD_E_SAVED_PC) = saved_pc;
                    F64(target_es, AD_E_TIME) = ad_now_game();
                    U8(target_es, AD_E_YIELD) = 1;
                }
                U8(target, AD_C_RESTORING) = 0;
                void *bl;
                if (!U8(target, AD_C_ATTACHED) || !(bl = E.blocking_action(PTR(target, AD_C_EXEC))) ||
                    !((*(uintptr_t (***)(void *, void *))bl)[6](bl, PTR(target, AD_C_EXEC)) & 0xff)) {
                    U32(target, AD_C_MASK) |= 0x80;
                    E.request_wake(g, target);
                }
            }
            if (!U8(target, AD_C_ATTACHED)) E.jump_to_label(target_es, 0);
            if (ctx == target) E.record_clear(P(ctx, AD_C_RECORD));
            else E.clear_timed_wake(ctx);
        }
    }
    for (int k = 3; k >= 0; --k) ad_value_drop(&v[k]);
    return 1;
}

/* Lower-bound lookup in a frame's named-result map (header at es+0x60, root at header+8). */
static void *ad_result_lookup(void *es, uint64_t key) {
    unsigned char *header = P(es, AD_E_RESULTS), *found = header, *n = PTR(header, AD_N_LEFT);
    while (n) {
        if (U64(n, 0x20) < key) n = PTR(n, AD_N_RIGHT);
        else { found = n; n = PTR(n, AD_N_LEFT); }
    }
    return found != header && U64(found, 0x20) <= key ? found : NULL;
}

/* The finished script returns to its caller: hand back the return value, re-wake the caller. */
static void ad_return_to_caller(void *es) {
    void *parent = PTR(es, AD_E_PARENT);
    void *frame = P(parent, AD_E_FRAME);
    void *caller = ad_frame_owner(frame);
    if (!caller) { ++ad.win.bad_caller; return; }   /* the original would fault here */
    if (U8(caller, AD_C_RESTORING)) { E.resume_after_restore(caller); return; }
    if (U8(parent, AD_E_YIELD)) {
        unsigned char *act = ad_vec_at(PTR(parent, AD_E_PROGRAM), U64(parent, AD_E_SAVED_PC));
        if (act) {
            if (I32(act, 8) == AD_KW_RUN_SCRIPT) E.return_run_script(act, parent, es);
            else if (I32(act, 8) == AD_KW_RUN_ORDER_SCRIPT && I32(act, 0x88)) {
                unsigned char *node = ad_result_lookup(es, *E.order_result_key);
                if (node) E.return_order_value(act + 0x88, frame, act + 0x98, node + 0x48);
                else {
                    void *fctx = E.frame_context(P(es, AD_E_FRAME));
                    ad_msvc_string msg;
                    ad_str_init(&msg);
                    E.str_format(&msg, AD_MSG_NO_RETURN, ad_engine_str_c(P(PTR(fctx, AD_C_SCRIPT), AD_SCRIPT_NAME)));
                    E.value_error(frame, &msg, act + 0x88);
                    ad_str_free(&msg);
                }
            }
        }
    }
    U32(caller, AD_C_MASK) |= 0x20;
    ++ad.win.caller_wakes;
    E.request_wake(ad_dir(), caller);
}

/* One queued context of the read vector `r` (dequeued here), as the original processes it. */
static void ad_process_context(void *dir, void *ctx, unsigned r, int timed, unsigned pass) {
    void *es = PTR(ctx, AD_C_EXEC);
    I64(ctx, AD_C_SLOT + 8 * r) = -1;
    PTR(dir, AD_D_ENTITY) = PTR(ctx, AD_C_ENTITY);
    uint32_t mask;
    if (timed) {
        double wt = F64(ctx, AD_C_WAKETIME);
        mask = (AD_NO_WAKE < wt && wt <= ad_now_game()) ? U32(ctx, AD_C_TMASK) : 0;
    } else mask = U32(ctx, AD_C_MASK);
    if (pass == AD_LOOP_PASS) ad_loop_diagnostic(ctx, mask);
    if (mask & 0x40) {
        if (ad_global_interrupts(dir, ctx, es, mask)) return;
        mask &= ~0x40u;
    }
    if (!mask || PTR(es, AD_E_CHILD)) {
        if (timed) E.clear_timed_wake_if_due(ctx, 0); else E.clear_wake(ctx);
        return;
    }
    uint8_t restart = 0;
    uint32_t saved = mask;
    if (mask & 4) {
        void *bl = E.blocking_action(es);
        if (bl) {
            for (unsigned char *rec = PTR(ctx, AD_C_PENDING); rec != PTR(ctx, AD_C_PENDING + 8); rec += 0x50)
                if (I64(rec, 0) == -1) { E.record_copy(P(ctx, AD_C_RECORD2), rec + 8); break; }
            restart = (uint8_t)ad_irq(ctx, es, PTR(es, AD_E_PROGRAM), (uintptr_t)PTR(bl, 0x40), (uintptr_t)PTR(bl, 0x48), 1, mask);
            E.record_clear(P(ctx, AD_C_RECORD2));
        }
        mask = saved;
    }
    if (!(E.attention_changed(ctx) & 0xff) || U8(es, AD_E_NOATT)) {
        if (!(mask & ~0x10u)) {
            if (timed) E.clear_timed_wake_if_due(ctx, 0); else E.clear_wake(ctx);
            return;
        }
    } else {
        if (!(mask & 0x81)) {
            void *bl = E.blocking_action(es);
            if (!bl) ++ad.win.bad_caller;   /* the original dereferences it unchecked (would fault) */
            else if (PTR(bl, 0x50)) ad_irq(ctx, es, PTR(es, AD_E_PROGRAM), (uintptr_t)PTR(bl, 0x50), (uintptr_t)PTR(bl, 0x58), 1, mask);
        }
        if (!U8(es, AD_E_NOATT)) {
            ++ad.win.attention_restarts;   /* counted at GetAttentionBlock, as the census does */
            int32_t attention = I32(PTR(ctx, AD_C_ENTITY), AD_ENTITY_ATTENTION);
            I32(ctx, AD_C_CACHED_ATT) = attention;
            PTR(es, AD_E_PROGRAM) = E.attention_block(PTR(ctx, AD_C_SCRIPT), (uintptr_t)(uint32_t)attention);
            E.jump_to_label(es, 0);
        }
        restart = 0;
    }
    E.clear_timed_wake(ctx);
    F64(es, AD_E_TIME) = -1.0;
    U8(es, AD_E_YIELD) = 0;
    E.unregister_all(ctx);
    E.ctx_reset(ctx);
    E.record_clear(P(ctx, AD_C_RECORD));
    U8(ctx, AD_C_ATTACHED) = 0;
    if (restart) E.jump_to_label(es, 1);
    if (U64(es, AD_E_PC) < ad_vec_len(PTR(es, AD_E_PROGRAM))) {
        ad_run_frame f;
        ad_run_begin(&f, ctx, es, mask, 0);
        E.exec_run(es);
        ad_run_end(&f, es);
    }
    if (U64(es, AD_E_PC) >= ad_vec_len(PTR(es, AD_E_PROGRAM)) && !U8(es, AD_E_YIELD)) {
        if (PTR(es, AD_E_PARENT)) ad_return_to_caller(es);
        ++ad.win.removed;
        E.remove_context(dir, ctx);
    }
}

/* ---- rounds: bucket order, event-buffer scopes, barrier, order-equivalence counters ------------ */

/* Event buffer reused from the walk agent (walkpar.h, Q4/Q7): one buffer, scoped by task id. NULL in
   a build without walkpar: rounds then only reorders (events stay synchronous). */
typedef struct {
    void (*scope_begin)(uint64_t task);
    void (*scope_end)(void);
    void (*replay_tasks)(const uint64_t *order, int n);
    size_t (*buffer_count)(void);
    int (*buffer_op)(size_t i, uint64_t *src_id, int *task, int *type);   /* AD_OP_*; 0 = none */
} ad_event_buffer;
static const ad_event_buffer *ad_evbuf;
enum { AD_OP_POST = 1, AD_OP_CANCEL = 2 };   /* walkpar WP_OP_POST / WP_OP_CANCEL */
/* Task key of a bucket: the sector's UniverseID (walkpar's per-sector task convention); the keyless
   (serial) bucket gets a reserved key. 0 means "no scope" in walkpar. */
#define AD_TASK_SERIAL 0xfffffffffffffffeull
static uint64_t ad_task_key(const void *sector) { return sector ? U64(sector, AD_COMP_ID) : AD_TASK_SERIAL; }

#ifdef X4LAN_WALKPAR_H
/* Integrated build (walkpar.h is included before this file): bind the walk agent's one buffer. Its
   scopes defer posts only while walkpar runs in WP_REPLAY (which also defers the walk's own events). */
static const ad_event_buffer *ad_walkpar_buffer(void) {
    static ad_event_buffer b;
    const wp_api *w = walkpar_api();
    b.scope_begin = w->scope_begin; b.scope_end = w->scope_end; b.replay_tasks = w->replay_tasks;
    b.buffer_count = w->buffer_count; b.buffer_op = w->buffer_op;
    return &b;
}
#endif

/* Read-vector position of the context that issued each buffered op of the current pass (ops are
   appended in rounds execution order, so the buffer index is the rounds order of the op). */
static size_t *ad_op_pos;
static size_t ad_op_pos_cap;
static void ad_op_pos_fill(size_t from, size_t to, size_t pos) {
    if (to > ad_op_pos_cap) {
        size_t cap = ad_op_pos_cap ? ad_op_pos_cap : 1024;
        while (cap < to) cap *= 2;
        size_t *p = realloc(ad_op_pos, cap * sizeof(size_t));
        if (!p) return;
        ad_op_pos = p; ad_op_pos_cap = cap;
    }
    for (size_t j = from; j < to; ++j) ad_op_pos[j] = pos;
}

/* Barrier check (compare mode): a cancel in one bucket and a post in another bucket for the same
   source and type inside one pass. Replay follows the rounds execution order (buffer order), so the
   outcome is the rounds order's; it differs from the original only when the original order of the
   two issuing contexts (their read-vector positions) is the opposite of the buffer order. The buffer
   readout has no immediate flag, so immediate posts are counted too (conservative). */
static unsigned ad_post_cancel_conflicts(size_t ops) {
    if (!ad_evbuf || !ad_evbuf->buffer_op || ops > ad_op_pos_cap) return 0;
    unsigned conflicts = 0;
    for (size_t i = 0; i < ops; ++i) {
        uint64_t src; int task, type;
        if (ad_evbuf->buffer_op(i, &src, &task, &type) != AD_OP_CANCEL) continue;
        for (size_t j = 0; j < ops; ++j) {
            uint64_t src2; int task2, type2;
            if (j == i || ad_evbuf->buffer_op(j, &src2, &task2, &type2) != AD_OP_POST) continue;
            if (src2 != src || type2 != type || task2 == task) continue;
            if ((j < i) != (ad_op_pos[j] < ad_op_pos[i])) { ++conflicts; break; }
        }
    }
    return conflicts;
}

#define AD_POS_SLOTS 8192u            /* ctx -> read-vector position map (power of two) */
#define AD_CMP_SCAN_LIMIT 1024u       /* passes above this size skip the O(n^2) null scan */
typedef struct {
    size_t cap;
    size_t *order;                    /* positions in rounds order */
    uint32_t *bucket;                 /* bucket of each position (UINT32_MAX: empty slot) */
    uint8_t *done;                    /* processed this pass */
    uint8_t *alive;                   /* non-null at the last scan */
    uint32_t *count;                  /* per-bucket sizes (counting sort) */
    uint64_t *tasks;                  /* 1..nb for replay */
    const void **bkey;                /* bucket key (sector) */
    struct { const void *ctx; uint32_t pos; } pmap[AD_POS_SLOTS];
    uint16_t pmap_used[AD_POS_SLOTS];
    unsigned pmap_n;
    /* active pass (for the event wrappers) */
    int active;
    void **vec;
    unsigned r;
    size_t cur_pos;
    uint32_t cur_bucket;
    int pass_dirty;                   /* an order-visible interaction happened in this pass */
} ad_rounds_t;
static ad_rounds_t ad_rd;

static int ad_rd_reserve(size_t n) {
    if (n <= ad_rd.cap) return 1;
    size_t cap = ad_rd.cap ? ad_rd.cap : 256;
    while (cap < n) cap *= 2;
    void *a = realloc(ad_rd.order, cap * sizeof(size_t)), *b = realloc(ad_rd.bucket, cap * sizeof(uint32_t)),
         *c = realloc(ad_rd.done, cap), *d = realloc(ad_rd.alive, cap), *e = realloc(ad_rd.count, (cap + 1) * sizeof(uint32_t)),
         *f = realloc(ad_rd.tasks, cap * sizeof(uint64_t)), *g = realloc(ad_rd.bkey, cap * sizeof(void *));
    if (a) ad_rd.order = a;
    if (b) ad_rd.bucket = b;
    if (c) ad_rd.done = c;
    if (d) ad_rd.alive = d;
    if (e) ad_rd.count = e;
    if (f) ad_rd.tasks = f;
    if (g) ad_rd.bkey = (const void **)g;
    if (!a || !b || !c || !d || !e || !f || !g) return 0;
    ad_rd.cap = cap;
    return 1;
}

static unsigned ad_pmap_slot(const void *ctx) {
    return (unsigned)((((uintptr_t)ctx >> 4) * 0x9E3779B97F4A7C15ull) >> 51) & (AD_POS_SLOTS - 1);
}
static void ad_pmap_clear(void) {
    for (unsigned k = 0; k < ad_rd.pmap_n; ++k) ad_rd.pmap[ad_rd.pmap_used[k]].ctx = NULL;
    ad_rd.pmap_n = 0;
}
static void ad_pmap_put(const void *ctx, uint32_t pos) {
    if (ad_rd.pmap_n >= AD_POS_SLOTS / 2) return;     /* lookups then miss: counted as unchecked */
    for (unsigned i = ad_pmap_slot(ctx), n = 0; n < AD_POS_SLOTS; ++n, i = (i + 1) & (AD_POS_SLOTS - 1))
        if (!ad_rd.pmap[i].ctx) { ad_rd.pmap[i].ctx = ctx; ad_rd.pmap[i].pos = pos; ad_rd.pmap_used[ad_rd.pmap_n++] = (uint16_t)i; return; }
}
static int ad_pmap_get(const void *ctx, uint32_t *pos) {
    for (unsigned i = ad_pmap_slot(ctx), n = 0; n < AD_POS_SLOTS; ++n, i = (i + 1) & (AD_POS_SLOTS - 1)) {
        if (!ad_rd.pmap[i].ctx) return 0;
        if (ad_rd.pmap[i].ctx == ctx) { *pos = ad_rd.pmap[i].pos; return 1; }
    }
    return 0;
}

/* Event delivered to an AI context while a pass runs: order-visible when the receiver sits in this
   pass's read vector in another bucket and either has not run yet, or ran but would have run after
   the poster in the original order. Called from the slot-565 wrappers. */
static void ad_rounds_event(void *receiver) {
    uint32_t pos;
    if (!ad_rd.active || !ad.in_run || !receiver || !ad_pmap_get(receiver, &pos)) return;
    if (ad_rd.bucket[pos] == ad_rd.cur_bucket) return;
    if (!ad_rd.done[pos]) { ++ad.win.cmp_event_pending; ad_rd.pass_dirty = 1; }
    else if (pos > ad_rd.cur_pos) { ++ad.win.cmp_event_ran; ad_rd.pass_dirty = 1; }
}

/* After a context ran: wakes it appended to the write vector. A woken context that sits in this
   pass's read vector in another bucket absorbs the wake if it is processed later in the pass (its
   ClearWake nulls the new write entry), otherwise the wake survives to the next pass. The original
   absorbs it iff the wakee's index is larger than the waker's; rounds iff it has not run yet. */
static void ad_rounds_wakes(void **wvec, size_t from, size_t i) {
    size_t to = ((uintptr_t)wvec[1] - (uintptr_t)wvec[0]) / sizeof(void *);
    for (size_t k = from; k < to; ++k) {
        void *woken = ((void **)wvec[0])[k];
        uint32_t pos;
        if (!woken || !ad_pmap_get(woken, &pos) || ad_rd.bucket[pos] == ad_rd.cur_bucket) continue;
        if ((pos > i) != !ad_rd.done[pos]) { ++ad.win.cmp_wake_absorb; ad_rd.pass_dirty = 1; }
    }
}

/* After a context ran: a pending entry of another bucket that the original would already have run
   (smaller index) and that is now null was skipped only because of the rounds order. */
static void ad_rounds_scan(size_t n, size_t i) {
    void **v = (void **)ad_rd.vec[0];
    for (size_t j = 0; j < i; ++j) {
        if (ad_rd.done[j] || !ad_rd.alive[j] || v[j]) continue;
        ad_rd.alive[j] = 0;
        if (ad_rd.bucket[j] != ad_rd.cur_bucket) { ++ad.win.cmp_null_lost; ad_rd.pass_dirty = 1; }
    }
    (void)n;
}

/* One pass in rounds order. Positions keep their read-vector index (the vector does not grow during
   a pass); entries nulled meanwhile by ClearWake are skipped exactly as in the original. */
static void ad_rounds_pass(void *dir, void **vec, size_t n, unsigned r, int timed, unsigned pass, int compare) {
    if (!ad_rd_reserve(n)) {   /* allocation failure: run this pass in the original order */
        ++ad.win.rounds_fallback;
        for (size_t i = 0; i < n; ++i) { void *ctx = ((void **)vec[0])[i]; if (ctx) ad_process_context(dir, ctx, r, timed, pass); }
        return;
    }
    void **v = (void **)vec[0];
    uint32_t nb = 0;
    ad_btable_clear(&ad.f_scratch);
    for (size_t i = 0; i < n; ++i) {
        ad_rd.done[i] = 0;
        ad_rd.alive[i] = v[i] != NULL;
        if (!v[i]) { ad_rd.bucket[i] = UINT32_MAX; continue; }
        const void *key = ad_ctx_sector(v[i]);
        ad_bucket *b = ad_bucket_get(&ad.f_scratch, key);
        if (b && !b->runs) { b->runs = 1; b->ms = (double)nb; ad_rd.bkey[nb] = key; ++nb; }
        ad_rd.bucket[i] = b ? (uint32_t)b->ms : 0;   /* bucket index kept in ms (scratch table) */
    }
    for (uint32_t b = 0; b <= nb; ++b) ad_rd.count[b] = 0;
    for (size_t i = 0; i < n; ++i) if (ad_rd.bucket[i] != UINT32_MAX) ++ad_rd.count[ad_rd.bucket[i] + 1];
    for (uint32_t b = 1; b <= nb; ++b) ad_rd.count[b] += ad_rd.count[b - 1];
    size_t total = nb ? ad_rd.count[nb] : 0;
    for (size_t i = 0; i < n; ++i) if (ad_rd.bucket[i] != UINT32_MAX) ad_rd.order[ad_rd.count[ad_rd.bucket[i]]++] = i;
    ad_pmap_clear();
    for (size_t i = 0; i < n; ++i) if (v[i]) ad_pmap_put(v[i], (uint32_t)i);
    ad_rd.active = 1; ad_rd.vec = vec; ad_rd.r = r; ad_rd.pass_dirty = 0;
    int scan = compare && n <= AD_CMP_SCAN_LIMIT;
    if (compare && !scan) ++ad.win.cmp_unchecked;
    ad.win.buckets_run += nb;
    size_t k = 0;
    for (uint32_t b = 0; b < nb; ++b) {
        ad_rd.cur_bucket = b;
        ad_rd.tasks[b] = ad_task_key(ad_rd.bkey[b]);
        if (ad_evbuf) ad_evbuf->scope_begin(ad_rd.tasks[b]);
        for (; k < total && ad_rd.bucket[ad_rd.order[k]] == b; ++k) {
            size_t i = ad_rd.order[k];
            ad_rd.cur_pos = i;
            void *ctx = ((void **)vec[0])[i];
            size_t ops0 = ad_evbuf ? ad_evbuf->buffer_count() : 0;
            void **wvec = ad_vec(dir, 1u - r);
            size_t w0 = ((uintptr_t)wvec[1] - (uintptr_t)wvec[0]) / sizeof(void *);
            if (ctx) ad_process_context(dir, ctx, r, timed, pass);
            if (ad_evbuf && compare) ad_op_pos_fill(ops0, ad_evbuf->buffer_count(), i);
            ad_rd.done[i] = 1;
            if (compare) ad_rounds_wakes(wvec, w0, i);
            if (scan) ad_rounds_scan(n, i);
        }
        if (ad_evbuf) ad_evbuf->scope_end();
    }
    ad_rd.active = 0;
    if (ad_evbuf) {   /* barrier: deliver this pass's deferred events in bucket order, before the flip */
        size_t before = ad_evbuf->buffer_count();
        if (compare) {
            unsigned c = ad_post_cancel_conflicts(before);
            if (c) { ad.win.cmp_post_cancel += c; ad_rd.pass_dirty = 1; }
        }
        ad_evbuf->replay_tasks(ad_rd.tasks, (int)nb);   /* bucket order = rounds execution order */
        ad.win.deferred += before;
    }
    if (compare) { if (ad_rd.pass_dirty) ++ad.win.cmp_passes_dirty; else ++ad.win.cmp_passes_clean; }
}

/* ---- the frame -------------------------------------------------------------------------------- */

/* Our AIDirector::Move. mode: AD_NORMAL (original order), AD_ROUNDS, AD_COMPARE (rounds + counters). */
static void ad_move_impl(void *dir, int mode) {
    unsigned pass = 0;
    for (;;) {
        uint32_t w;
        if (pass == 0x21) w = U32(dir, AD_D_WIDX);
        else { w = 1u - U32(dir, AD_D_WIDX); U32(dir, AD_D_WIDX) = w; }
        unsigned r = 1u - w;
        void **vec = ad_vec(dir, r);
        int timed = 0;
        if (vec[0] == vec[1]) {
            timed = 1;
            ad_harvest_timed(dir, r);
            if (vec[0] == vec[1]) break;
        }
        size_t n = ((uintptr_t)vec[1] - (uintptr_t)vec[0]) / sizeof(void *);
        ++ad.f_passes;
        if (mode == AD_NORMAL) {
            for (size_t i = 0; i < n; ++i) {
                void *ctx = ((void **)vec[0])[i];
                if (ctx) ad_process_context(dir, ctx, r, timed, pass);
            }
        } else ad_rounds_pass(dir, vec, n, r, timed, pass, mode == AD_COMPARE);
        vec[1] = vec[0];
        if (++pass >= AD_PASS_LIMIT) break;
    }
    PTR(dir, AD_D_ENTITY) = NULL;
    if (PTR(dir, AD_D_REFRESH_B) != PTR(dir, AD_D_REFRESH_B + 8)) {
        E.refresh_scripts(dir, P(dir, AD_D_REFRESH_B), 1);
        E.list_clear(P(dir, AD_D_REFRESH_B));
    }
    if (PTR(dir, AD_D_REFRESH_A) != PTR(dir, AD_D_REFRESH_A + 8)) {
        E.refresh_scripts(dir, P(dir, AD_D_REFRESH_A), 0);
        E.list_clear(P(dir, AD_D_REFRESH_A));
    }
}

/* Structural pre-flight before our loop takes a frame; any failure runs the original this frame. */
static const char *ad_preflight(void *dir) {
    uint32_t w = U32(dir, AD_D_WIDX);
    if (w > 1) return "AI director write index out of range";
    for (unsigned b = 0; b < 2; ++b) {
        void **v = ad_vec(dir, b);
        uintptr_t lo = (uintptr_t)v[0], hi = (uintptr_t)v[1], cap = (uintptr_t)v[2];
        if (lo > hi || hi > cap || ((hi - lo) & 7) || (hi - lo) / 8 > 4000000) return "AI wake vector malformed";
    }
    void **v = ad_vec(dir, w);   /* becomes the first read vector */
    size_t n = ((uintptr_t)v[1] - (uintptr_t)v[0]) / 8;
    for (size_t i = 0; i < n; ++i) {
        void *ctx = ((void **)v[0])[i];
        if (ctx && I64(ctx, AD_C_SLOT + 8 * w) != (int64_t)i) return "AI wake slot index inconsistent";
    }
    if (!PTR(dir, AD_D_TIMED_LEFT)) return "AI timed-wake tree malformed";
    return NULL;
}

/* FNV-1a over the AI state after the phase (contexts in the director's all-contexts vector). Used by
   the comparison mode and the offline normal-vs-rounds equivalence tests. */
static uint64_t ad_state_hash(void *dir) {
    uint64_t h = 1469598103934665603ull;
#define AD_MIX(x) do { uint64_t _x = (uint64_t)(x); for (int _b = 0; _b < 8; ++_b) { h ^= (_x >> (8 * _b)) & 0xff; h *= 1099511628211ull; } } while (0)
    void **all = (void **)PTR(dir, AD_D_ALL);
    size_t n = ad_vec_len(P(dir, AD_D_ALL));
    AD_MIX(n);
    for (size_t i = 0; i < n; ++i) {
        unsigned char *ctx = all[i];
        if (!ctx) { AD_MIX(0); continue; }
        AD_MIX(U64(ctx, AD_COMP_ID));
        AD_MIX(U32(ctx, AD_C_MASK)); AD_MIX(U32(ctx, AD_C_TMASK));
        uint64_t wt; memcpy(&wt, P(ctx, AD_C_WAKETIME), 8); AD_MIX(wt);
        AD_MIX(I64(ctx, AD_C_SLOT) != -1); AD_MIX(I64(ctx, AD_C_SLOT + 8) != -1);
        AD_MIX(U8(ctx, AD_C_RESTORING)); AD_MIX(U8(ctx, AD_C_ATTACHED));
        for (unsigned char *es = PTR(ctx, AD_C_EXEC), depth = 0; es && depth < 32; es = PTR(es, AD_E_CHILD), ++depth) {
            AD_MIX(U64(es, AD_E_PC)); AD_MIX(U8(es, AD_E_YIELD)); AD_MIX(U64(es, AD_E_SAVED_PC));
        }
    }
    for (unsigned b = 0; b < 2; ++b) {
        void **v = ad_vec(dir, b);
        size_t m = ((uintptr_t)v[1] - (uintptr_t)v[0]) / 8;
        AD_MIX(m);
        for (size_t i = 0; i < m; ++i) { unsigned char *c = ((void **)v[0])[i]; AD_MIX(c ? U64(c, AD_COMP_ID) : 0); }
    }
#undef AD_MIX
    return h;
}

/* ---- engine helper resolution (signatures.py AIDRIVER_HELPERS, verified at install) ------------ */

static void ad_resolve_engine(void) {
#define AD_FN(field, rva) E.field = (void *)(image + (rva))
    AD_FN(request_wake, 0x106f910); AD_FN(clear_wake, 0x10669c0); AD_FN(clear_timed_wake, 0x1066950);
    AD_FN(clear_timed_wake_if_due, 0x1066a80); AD_FN(remove_context, 0x106f710); AD_FN(tree_extract, 0xb2cf0);
    AD_FN(vec_insert, 0xb1880); AD_FN(exec_run, 0xb58bb0); AD_FN(irq_run, 0xb58ce0);
    AD_FN(blocking_action, 0x109a6a0); AD_FN(attention_changed, 0x10664a0); AD_FN(attention_block, 0x1074850);
    AD_FN(jump_to_label, 0xb59f30); AD_FN(unregister_all, 0x998b20); AD_FN(ctx_reset, 0xcbd2c0);
    AD_FN(record_clear, 0xc58820); AD_FN(record_copy, 0x106e660); AD_FN(value_release, 0xcccaa0);
    AD_FN(value_type_error, 0xcccda0); AD_FN(value_assign, 0xccd540); AD_FN(resume_after_restore, 0x10666e0);
    AD_FN(return_run_script, 0x10a5a90); AD_FN(return_order_value, 0xcc6710); AD_FN(frame_context, 0xcc1a00);
    AD_FN(value_error, 0xcc9a70); AD_FN(eval_string, 0xcc7f10); AD_FN(find_script, 0x106d0a0);
    AD_FN(eval_bool, 0xcc7da0); AD_FN(expr_error, 0xcc8e20); AD_FN(string_assign, 0x975a0);
    AD_FN(abort_reason, 0xacb4e0); AD_FN(abort_stack, 0x106fd90); AD_FN(push_script, 0x106eca0);
    AD_FN(tree_erase, 0xb4b810); AD_FN(refresh_scripts, 0x1068140); AD_FN(list_clear, 0x10c4220);
    AD_FN(str_format, 0x14b7a50); AD_FN(log_error, 0x14b9a30); AD_FN(id_to_string, 0x1303b0);
    AD_FN(op_delete, 0x174f310); AD_FN(invalid_parameter, 0x19fc9c0); AD_FN(tls_init, 0x174f5fc);
#undef AD_FN
    E.abort_table = image + AD_DATA_ABORT_TABLE;
    E.order_result_key = (const uint64_t *)(image + AD_DATA_RESULT_KEY);
    E.game_time = ad_tls_game_time;
}
