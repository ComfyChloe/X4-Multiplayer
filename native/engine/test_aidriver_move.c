/* Synthetic tests for the stage-1 AI driver (native/engine/aidriver_move.h): our reimplementation of
 * AIDirector::Move in normal, rounds and compare modes. No game: a fake engine implements every helper
 * the driver calls with the engine's behaviour as reverse engineered (wake vectors and slot indices,
 * ClearWake nulling, the timed tree with its node pool), records a call trace, and runs scripted
 * context behaviours (yield, finish, wake/clear another context, post/cancel events). Each test checks
 * the helper-call sequence of one branch against the original's (reports/REWRITE-AI-DRIVER.md 5),
 * the pass structure, normal-vs-rounds equivalence (run multisets, per-sector order, state hash), the
 * order-visible counters, and the event-buffer ordering cases from the walk review (a cancel in one
 * bucket versus a post in another bucket for the same source and type, both orders). */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include "fixedtick.c"

struct lua_State { int unused; };
static owner fake_owner;
static LARGE_INTEGER fake_qpc_now;
static BOOL WINAPI fake_qpc(LARGE_INTEGER *v) { fake_qpc_now.QuadPart += 10; *v = fake_qpc_now; return TRUE; }

/* ---- trace ---------------------------------------------------------------------------------- */
enum { T_REQWAKE = 1, T_CLRWAKE, T_CLRTIMED, T_CLRTIMEDDUE, T_REMOVE, T_RUN, T_IRQ, T_BLOCK, T_ATTCHG, T_ATTBLK,
       T_JUMP, T_UNREG, T_RESET, T_RECCLR, T_RECCPY, T_VASSIGN, T_RESUME, T_RETRUN, T_RETORDER, T_VERR,
       T_EVALSTR, T_FIND, T_EVALBOOL, T_EXPRERR, T_STRASSIGN, T_ABORT, T_PUSH, T_REFRESH, T_LISTCLR, T_LOG };
typedef struct { int op; void *a; uintptr_t x; } tr_t;
static tr_t tr[8192];
static int ntr;
static void TR(int op, void *a, uintptr_t x) { if (ntr < 8192) tr[ntr++] = (tr_t){op, a, x}; }
static int trace_has(int op, void *a) { for (int i = 0; i < ntr; ++i) if (tr[i].op == op && (!a || tr[i].a == a)) return 1; return 0; }
static int trace_count(int op, void *a) { int n = 0; for (int i = 0; i < ntr; ++i) if (tr[i].op == op && (!a || tr[i].a == a)) ++n; return n; }
/* The ops recorded for one context (in order), compared with an expected list. */
static int trace_seq_is(void *a, const int *want, int nwant) {
    int k = 0;
    for (int i = 0; i < ntr; ++i) {
        if (tr[i].a != a) continue;
        if (k >= nwant || tr[i].op != want[k]) return 0;
        ++k;
    }
    return k == nwant;
}

/* ---- fake world ------------------------------------------------------------------------------ */
#define NCTX 24
enum { S_YIELD = 1, S_FINISH, S_WAKE, S_CLEAR, S_POST, S_POSTI, S_CANCEL, S_WAKESELF, S_DRAW };
#define STEP(kind, a, b) (((kind) << 16) | ((a) << 8) | (b))
typedef struct {
    unsigned char ctx[0x300];
    unsigned char es[0x120];
    unsigned char ent[0x200];
    void *progv[3];
    void *prog[4];
    unsigned char act[2][0x100];
    unsigned char bl[0x80];
    int steps[48], nsteps, runs;
    int blocking, att_changed, irq_restart;
    uint64_t gsel;                       /* handler index the global-interrupt runner selects */
} fake_ctx;
static fake_ctx C[NCTX];
static unsigned char SECT[4][0x80];
static unsigned char DIR[0x300];
static void *W0[512], *W1[512], *ALL[64];
static unsigned char SCRIPT[4][0x300];   /* AIScript objects: name at +0x10, prio +0x5c, handlers +0x148 */
static double now_game = 10.0;
static unsigned char fake_tls[0x400];   /* engine TLS block: init flag +0x10, random stream +0x318 */
static int run_log[4096], nrun;
static uint32_t stamp_pass[4096];   /* ad.run_pass / ad.run_pos seen by each run (Q14 stamp) */
static int64_t stamp_pos[4096];
static char last_log[512], last_msg[256], last_push[64];

static void *CX(int i) { return C[i].ctx; }
static int ctx_index(const void *p) {
    for (int i = 0; i < NCTX; ++i)
        if ((const unsigned char *)p >= (const unsigned char *)&C[i] && (const unsigned char *)p < (const unsigned char *)&C[i + 1]) return i;
    return -1;
}

static void set_str(void *s, const char *text) {   /* MSVC SSO std::string (< 16 chars) */
    memset(s, 0, 32);
    strcpy((char *)s, text);
    U64(s, 0x10) = strlen(text);
    U64(s, 0x18) = 15;
}

static void world_reset(void) {
    memset(C, 0, sizeof(C));
    memset(DIR, 0, sizeof(DIR));
    memset(W0, 0, sizeof(W0)); memset(W1, 0, sizeof(W1)); memset(ALL, 0, sizeof(ALL));
    void **v0 = (void **)(DIR + AD_D_WAKE), **v1 = (void **)(DIR + AD_D_WAKE + 0x18);
    v0[0] = W0; v0[1] = W0; v0[2] = W0 + 512;
    v1[0] = W1; v1[1] = W1; v1[2] = W1 + 512;
    U32(DIR, AD_D_WIDX) = 0;
    unsigned char *head = DIR + AD_D_TIMED;
    PTR(head, AD_N_PARENT) = head; PTR(head, AD_N_LEFT) = NULL; /* head->left = root */
    PTR(DIR, AD_D_TIMED_LEFT) = head;
    I64(DIR, AD_D_TIMED_SIZE) = 0;
    PTR(DIR, AD_D_FREE_NODES) = NULL;
    void **all = (void **)(DIR + AD_D_ALL); all[0] = ALL; all[1] = ALL; all[2] = ALL + 64;
    void **ra = (void **)(DIR + AD_D_REFRESH_A), **rb = (void **)(DIR + AD_D_REFRESH_B);
    static void *rla[4], *rlb[4];
    ra[0] = rla; ra[1] = rla; ra[2] = rla + 4; rb[0] = rlb; rb[1] = rlb; rb[2] = rlb + 4;
    for (int s = 0; s < 4; ++s) {
        memset(SECT[s], 0, sizeof(SECT[s]));
        PTR(SECT[s], 0) = image + AD_SECTOR_VTABLE_RVA;
        U64(SECT[s], AD_COMP_ID) = 0x500 + s;
        memset(SCRIPT[s], 0, sizeof(SCRIPT[s]));
    }
    set_str(SCRIPT[0] + AD_SCRIPT_NAME, "order.fight");
    set_str(SCRIPT[1] + AD_SCRIPT_NAME, "handler.a");
    I32(SCRIPT[1], AD_S_PRIORITY) = 5;
    set_str(SCRIPT[2] + AD_SCRIPT_NAME, "order.trade");
    nrun = 0; ntr = 0; last_log[0] = last_msg[0] = last_push[0] = 0;
    memset(&ad.win, 0, sizeof(ad.win));
    ad.f_passes = ad.f_runs = ad.f_irq = 0;
}

/* A context in sector `sec` (-1: keyless) running script 0, program of 2 actions, pc 0. */
static void *mk(int i, int sec) {
    fake_ctx *c = &C[i];
    PTR(c->ent, 0) = (void *)0x1234;
    PTR(c->ent, AD_COMP_PARENT) = sec < 0 ? NULL : SECT[sec];
    U64(c->ent, AD_COMP_ID) = 0x100 + (unsigned)i;
    I32(c->ent, AD_ENTITY_ATTENTION) = 5;
    PTR(c->ctx, AD_C_ENTITY) = c->ent;
    PTR(c->ctx, AD_C_SCRIPT) = SCRIPT[0];
    PTR(c->ctx, AD_C_EXEC) = c->es;
    U64(c->ctx, AD_COMP_ID) = 0x900 + (unsigned)i;
    I64(c->ctx, AD_C_SLOT) = -1; I64(c->ctx, AD_C_SLOT + 8) = -1;
    F64(c->ctx, AD_C_WAKETIME) = -1.0;
    PTR(c->ctx, AD_C_TITER) = DIR + AD_D_TIMED;
    PTR(c->ctx, AD_C_PENDING) = NULL; PTR(c->ctx, AD_C_PENDING + 8) = NULL;
    c->progv[0] = c->prog; c->progv[1] = c->prog + 2; c->progv[2] = c->prog + 4;
    c->prog[0] = c->act[0]; c->prog[1] = c->act[1];
    PTR(c->es, AD_E_PROGRAM) = c->progv;
    U64(c->es, AD_E_PC) = 0;
    I32(c->es, AD_E_FRAME) = AD_TAG_CONTEXT;
    PTR(c->es, AD_E_FRAME + 8) = c->ctx + 0xa8;
    void **all = (void **)(DIR + AD_D_ALL);
    *(void **)all[1] = c->ctx; all[1] = (void **)all[1] + 1;
    return c->ctx;
}
static void steps(int i, int n, const int *s) { memcpy(C[i].steps, s, n * sizeof(int)); C[i].nsteps = n; }

/* Queue a context into the current write vector with mask (what a waker does before RequestWake). */
static void f_request_wake(void *dir, void *ctx);
static void queue(void *ctx, uint32_t mask) { U32(ctx, AD_C_MASK) |= mask; f_request_wake(DIR, ctx); ntr = 0; }

/* Timed tree as a right chain in time order (valid BST); head->left = root. */
static unsigned char NODES[64][0x30];
static int nnodes;
static void timed_add(void *ctx, double t, uint32_t tmask) {
    unsigned char *head = DIR + AD_D_TIMED, *n = NODES[nnodes++];
    memset(n, 0, 0x30);
    F64(n, AD_N_TIME) = t; PTR(n, AD_N_CTX) = ctx;
    unsigned char *p = head, *cur = PTR(head, AD_N_LEFT);
    while (cur && F64(cur, AD_N_TIME) <= t) { p = cur; cur = PTR(cur, AD_N_RIGHT); }
    if (p == head) { PTR(n, AD_N_RIGHT) = PTR(head, AD_N_LEFT); PTR(head, AD_N_LEFT) = n; }
    else { PTR(n, AD_N_RIGHT) = PTR(p, AD_N_RIGHT); PTR(p, AD_N_RIGHT) = n; }
    PTR(n, AD_N_PARENT) = p;
    if (PTR(n, AD_N_RIGHT)) PTR(PTR(n, AD_N_RIGHT), AD_N_PARENT) = n;
    PTR(DIR, AD_D_TIMED_LEFT) = PTR(head, AD_N_LEFT);
    ++I64(DIR, AD_D_TIMED_SIZE);
    PTR(ctx, AD_C_TITER) = n;
    F64(ctx, AD_C_WAKETIME) = t;
    U32(ctx, AD_C_TMASK) = tmask;
}
static void chain_unlink(unsigned char *node) {
    unsigned char *p = PTR(node, AD_N_PARENT), *r = PTR(node, AD_N_RIGHT), *head = DIR + AD_D_TIMED;
    if (p == head) PTR(head, AD_N_LEFT) = r; else PTR(p, AD_N_RIGHT) = r;
    if (r) PTR(r, AD_N_PARENT) = p;
}

/* ---- walkpar-semantics event buffer + engine event queue ----------------------------------------- */
typedef struct { int kind, imm, dead; uint64_t src; int type, task; } fop;
static fop B[256];
static size_t nb_ops;
static int cur_task;                    /* 0 = no scope */
static struct { uint64_t src; int type; } Q[256];   /* engine queue of scheduled events */
static int nq, delivered_imm;
static int evbuf_on;
static int fbo_on;                      /* ordered variant (Q20): keys per op, key-aware cancel */
static uint64_t cur_key, B_key[256];
static void q_add(uint64_t src, int type) { Q[nq].src = src; Q[nq].type = type; ++nq; }
static void q_cancel(uint64_t src, int type) {
    int k = 0;
    for (int i = 0; i < nq; ++i) if (!(Q[i].src == src && Q[i].type == type)) Q[k++] = Q[i];
    nq = k;
}
static void ev_post(uint64_t src, int type, int imm) {
    if (evbuf_on && cur_task) { B_key[nb_ops] = cur_key; B[nb_ops++] = (fop){AD_OP_POST, imm, 0, src, type, cur_task}; return; }
    if (imm) ++delivered_imm; else q_add(src, type);
}
static void ev_cancel(uint64_t src, int type) {
    if (evbuf_on && cur_task) {   /* walkpar: kill buffered SCHEDULED posts of (src,type) of any task */
        for (size_t i = 0; i < nb_ops; ++i)   /* ordered: only posts emitted earlier in the original order */
            if (B[i].kind == AD_OP_POST && !B[i].dead && !B[i].imm && B[i].src == src && B[i].type == type &&
                (!fbo_on || B_key[i] <= cur_key)) B[i].dead = 1;
        B_key[nb_ops] = cur_key;
        B[nb_ops++] = (fop){AD_OP_CANCEL, 0, 0, src, type, cur_task};
        return;
    }
    q_cancel(src, type);
}
static void fb_replay_one(fop *o) {
    if (o->dead) return;
    o->dead = 1;
    if (o->kind == AD_OP_POST) { if (o->imm) ++delivered_imm; else q_add(o->src, o->type); }
    else q_cancel(o->src, o->type);
}
static void fb_scope_begin(uint64_t t) { cur_task = (int)t; }
static void fb_scope_end(void) { cur_task = 0; }
static int reverse_replay;              /* negative control: replay tasks in reverse order */
static void fb_replay_tasks(const uint64_t *order, int n) {
    for (int k = 0; k < n; ++k) {
        int t = (int)order[reverse_replay ? n - 1 - k : k];
        for (size_t i = 0; i < nb_ops; ++i) if (B[i].task == t) fb_replay_one(&B[i]);
    }
    for (size_t i = 0; i < nb_ops; ++i) fb_replay_one(&B[i]);
    nb_ops = 0;
}
static size_t fb_count(void) { return nb_ops; }
static int fb_op(size_t i, uint64_t *src, int *task, int *type) {
    if (i >= nb_ops) return 0;
    *src = B[i].src; *task = B[i].task; *type = B[i].type;
    return B[i].kind;
}
static const ad_event_buffer FB = {fb_scope_begin, fb_scope_end, fb_replay_tasks, fb_count, fb_op, NULL, NULL};
static void fbo_set_order(uint64_t k) { cur_key = k; }
static void fbo_replay_ordered(void) {   /* by (key, emission): smallest key first, ties in buffer order */
    for (;;) {
        size_t best = SIZE_MAX;
        for (size_t i = 0; i < nb_ops; ++i) if (!B[i].dead && (best == SIZE_MAX || B_key[i] < B_key[best])) best = i;
        if (best == SIZE_MAX) break;
        fb_replay_one(&B[best]);
    }
    nb_ops = 0;
}
static const ad_event_buffer FBO = {fb_scope_begin, fb_scope_end, fb_replay_tasks, fb_count, fb_op,
                                    fbo_set_order, fbo_replay_ordered};

/* ---- fake engine helpers ------------------------------------------------------------------------ */
static void f_request_wake(void *dir, void *ctx) {
    TR(T_REQWAKE, ctx, 0);
    uint32_t w = U32(dir, AD_D_WIDX);
    if (I64(ctx, AD_C_SLOT + 8 * w) != -1) return;
    void **v = ad_vec(dir, w);
    I64(ctx, AD_C_SLOT + 8 * w) = (int64_t)(((uintptr_t)v[1] - (uintptr_t)v[0]) / 8);
    *(void **)v[1] = ctx; v[1] = (void **)v[1] + 1;
}
static void f_clear_wake(void *ctx) {
    TR(T_CLRWAKE, ctx, 0);
    U32(ctx, AD_C_MASK) = 0;
    PTR(ctx, AD_C_PENDING + 8) = PTR(ctx, AD_C_PENDING);
    for (unsigned b = 0; b < 2; ++b)
        if (I64(ctx, AD_C_SLOT + 8 * b) != -1) {
            ((void **)ad_vec(DIR, b)[0])[I64(ctx, AD_C_SLOT + 8 * b)] = NULL;
            I64(ctx, AD_C_SLOT + 8 * b) = -1;
        }
}
static void timed_erase(void *ctx) {
    unsigned char *head = DIR + AD_D_TIMED, *node = PTR(ctx, AD_C_TITER);
    if (node == head) return;
    if (PTR(DIR, AD_D_TIMED_LEFT) == node) PTR(DIR, AD_D_TIMED_LEFT) = PTR(node, AD_N_RIGHT) ? PTR(node, AD_N_RIGHT) : head;
    chain_unlink(node);
    --I64(DIR, AD_D_TIMED_SIZE);
    PTR(ctx, AD_C_TITER) = head;
}
static void f_clear_timed_wake(void *ctx) {
    TR(T_CLRTIMED, ctx, 0);
    int keep = ntr; f_clear_wake(ctx); ntr = keep;   /* inner ClearWake is part of this call */
    F64(ctx, AD_C_WAKETIME) = -1.0; U32(ctx, AD_C_TMASK) = 0;
    timed_erase(ctx);
}
static void f_clear_timed_wake_if_due(void *ctx, uintptr_t force) {
    TR(T_CLRTIMEDDUE, ctx, force);
    double wt = F64(ctx, AD_C_WAKETIME);
    if (!force && (wt <= AD_NO_WAKE || wt > now_game)) return;
    F64(ctx, AD_C_WAKETIME) = -1.0; U32(ctx, AD_C_TMASK) = 0;
    timed_erase(ctx);
}
static void f_remove_context(void *dir, void *ctx) {
    TR(T_REMOVE, ctx, 0);
    void **all = (void **)P(dir, AD_D_ALL);
    void **b = all[0], **e = all[1];
    for (void **p = b; p < e; ++p) if (*p == ctx) { memmove(p, p + 1, (size_t)((char *)e - (char *)(p + 1))); all[1] = e - 1; break; }
}
static void f_tree_extract(void *node) { chain_unlink(node); }
static void f_vec_insert(void *vec, void *pos, void **value) { (void)vec; (void)pos; (void)value; assert(!"vector full"); }
static void f_exec_run(void *es) {
    int i = ctx_index(es);
    fake_ctx *c = &C[i];
    TR(T_RUN, c->ctx, (uintptr_t)c->runs);
    stamp_pass[nrun] = ad.run_pass; stamp_pos[nrun] = ad.run_pos;
    run_log[nrun++] = i;
    int s = c->runs < c->nsteps ? c->steps[c->runs] : STEP(S_FINISH, 0, 0);
    ++c->runs;
    int kind = s >> 16, a = (s >> 8) & 0xff, b = s & 0xff;
    switch (kind) {
    case S_WAKE: U32(CX(a), AD_C_MASK) |= 1; f_request_wake(DIR, CX(a)); break;
    case S_WAKESELF: U32(c->ctx, AD_C_MASK) |= 1; f_request_wake(DIR, c->ctx); break;
    case S_DRAW: U64(fake_tls, AD_TLS_RNG) = U64(fake_tls, AD_TLS_RNG) * 6364136223846793005ull + 1; break;
    case S_CLEAR: f_clear_wake(CX(a)); break;
    case S_POST: ev_post(0x7000 + (uint64_t)a, b, 0); break;
    case S_POSTI: ev_post(0x7000 + (uint64_t)a, b, 1); break;
    case S_CANCEL: ev_cancel(0x7000 + (uint64_t)a, b); break;
    default: break;
    }
    if (kind == S_FINISH) { U64(c->es, AD_E_PC) = 2; U8(c->es, AD_E_YIELD) = 0; }
    else { U8(c->es, AD_E_YIELD) = 1; }
}
static uintptr_t f_irq_run(void *es, void *list, uintptr_t start, uintptr_t count, uintptr_t flag) {
    int i = ctx_index(es);
    TR(T_IRQ, C[i].ctx, flag);
    (void)list; (void)start; (void)count;
    if (!flag) { U64(C[i].ctx, AD_C_SELECTED) = C[i].gsel; return 0; }
    return (uintptr_t)C[i].irq_restart;
}
static void *f_blocking_action(void *es) { int i = ctx_index(es); TR(T_BLOCK, C[i].ctx, 0); return C[i].blocking ? C[i].bl : NULL; }
static uintptr_t f_attention_changed(void *ctx) { TR(T_ATTCHG, ctx, 0); return (uintptr_t)C[ctx_index(ctx)].att_changed; }
static void *f_attention_block(void *script, uintptr_t att) { (void)script; (void)att; TR(T_ATTBLK, NULL, att); return C[0].progv; }
static void f_jump_to_label(void *es, uintptr_t restart) { TR(T_JUMP, C[ctx_index(es)].ctx, restart); U64(es, AD_E_PC) = 0; }
static void f_unregister_all(void *l) { TR(T_UNREG, l, 0); }
static void f_ctx_reset(void *ctx) { TR(T_RESET, ctx, 0); }
static void f_record_clear(void *rec) { int i = ctx_index(rec); TR(T_RECCLR, i >= 0 ? C[i].ctx : rec, (uintptr_t)((unsigned char *)rec - (i >= 0 ? C[i].ctx : (unsigned char *)rec))); }
static void f_record_copy(void *dst, void *src) { (void)src; TR(T_RECCPY, C[ctx_index(dst)].ctx, 0); }
static void f_value_release(void *v) { if (ad_refcounted(I32(v, 0)) && PTR(v, 8)) --*(int32_t *)PTR(v, 8); }
static void f_value_type_error(void *v, uintptr_t e) { (void)v; (void)e; }
static void f_value_assign(void *dst, void *src) { TR(T_VASSIGN, C[ctx_index(dst)].ctx, 0); memcpy(dst, src, 16); }
static void f_resume_after_restore(void *ctx) { TR(T_RESUME, ctx, 0); U8(ctx, AD_C_RESTORING) = 0; U32(ctx, AD_C_MASK) |= 0x80; f_request_wake(DIR, ctx); }
static void f_return_run_script(void *act, void *parent, void *es) { (void)act; (void)parent; TR(T_RETRUN, C[ctx_index(es)].ctx, 0); }
static void f_return_order_value(void *d, void *f, void *s, void *v) { (void)d; (void)s; (void)v; TR(T_RETORDER, C[ctx_index(f)].ctx, 0); }
static void *f_frame_context(void *frame) { return C[ctx_index(frame)].ctx; }
static void f_value_error(void *v, void *msg, void *where) { (void)v; (void)where; strncpy(last_msg, ad_str_c(msg), sizeof(last_msg) - 1); TR(T_VERR, NULL, 0); }
static const char *g_eval_name;          /* name the handler's script expression evaluates to */
static uintptr_t f_eval_string(void *expr, void *frame, void *out, uintptr_t a4, uintptr_t a5) {
    (void)expr; (void)frame; (void)a4; (void)a5;
    TR(T_EVALSTR, NULL, 0);
    if (!g_eval_name) return 0;
    set_str(out, g_eval_name);
    return 1;
}
static void *f_find_script(void *dir, void *view) {
    (void)dir;
    const ad_strview *v = view;
    TR(T_FIND, NULL, v->len);
    for (int s = 0; s < 4; ++s) if (!strcmp(ad_engine_str_c(SCRIPT[s] + AD_SCRIPT_NAME), v->data) && v->len) return SCRIPT[s];
    return NULL;
}
/* The handler's +0x80 expression: the original evaluates it once to pick the priority holder and
   again to decide the abort, so one scripted value serves both. */
static int g_abort_cond;
static uintptr_t f_eval_bool(void *expr, void *frame, uintptr_t a3) {
    (void)expr; (void)frame; (void)a3;
    TR(T_EVALBOOL, NULL, 0);
    return (uintptr_t)g_abort_cond;
}
static void f_expr_error(void *frame, void *msg, void *expr) { (void)frame; (void)expr; strncpy(last_msg, ad_str_c(msg), sizeof(last_msg) - 1); TR(T_EXPRERR, NULL, 0); }
static void f_string_assign(void *dst, const char *src, size_t len) { (void)dst; (void)src; (void)len; TR(T_STRASSIGN, NULL, len); }
static void *g_reason;
static void **f_abort_reason(void *table, void *tmp, uintptr_t kw) { (void)table; (void)tmp; (void)kw; return &g_reason; }
static void f_abort_stack(void *dir, void *es, uintptr_t a3, uintptr_t a4, uintptr_t a5, uintptr_t a6, void *reason, void *frame) {
    (void)dir; (void)es; (void)a3; (void)a4; (void)a5; (void)a6; (void)reason; TR(T_ABORT, C[ctx_index(frame)].ctx, 0);
}
static int g_push_ok;
static void *f_push_script(void *dir, void *es, void *view, void *params, void *frame) {
    (void)dir; (void)params; (void)frame;
    const ad_strview *v = view;
    snprintf(last_push, sizeof(last_push), "%.*s", (int)v->len, v->data);
    TR(T_PUSH, C[ctx_index(es)].ctx, 0);
    return g_push_ok ? (void *)1 : NULL;
}
static void f_tree_erase(void *head, void *out, void *node) { (void)head; (void)out; (void)node; }
static uintptr_t f_refresh_scripts(void *dir, void *list, uintptr_t dry) { (void)dir; TR(T_REFRESH, list, dry); return 1; }
static void f_list_clear(void *list) { TR(T_LISTCLR, list, 0); PTR(list, 8) = PTR(list, 0); }
static void *f_str_format(void *out, const char *fmt, ...) {
    char buf[512];
    va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
    size_t n = strlen(buf);
    ad_msvc_string *s = out;
    if (n < 16) { memset(s, 0, sizeof(*s)); memcpy(s->u.buf, buf, n + 1); s->size = n; s->cap = 15; }
    else { s->u.ptr = malloc(n + 1); memcpy(s->u.ptr, buf, n + 1); s->size = n; s->cap = n; }
    return out;
}
static void f_log_error(const char *fmt, ...) {
    va_list ap; va_start(ap, fmt); vsnprintf(last_log, sizeof(last_log), fmt, ap); va_end(ap);
    TR(T_LOG, NULL, 0);
}
static void f_id_to_string(void *out, uint64_t id) { char b[32]; snprintf(b, sizeof(b), "%llu", (unsigned long long)id); set_str(out, b); }
static void f_op_delete(void *p, size_t n) { (void)n; free(p); }
static void f_invalid_parameter(void) { assert(!"invalid parameter"); }
static void f_tls_init(void) {}
static double f_game_time(void) { return now_game; }
static unsigned char *f_engine_tls(void) { return fake_tls; }
static const uint64_t result_key = 0x4242;

static void fake_engine(void) {
    E.request_wake = f_request_wake; E.clear_wake = f_clear_wake; E.clear_timed_wake = f_clear_timed_wake;
    E.clear_timed_wake_if_due = f_clear_timed_wake_if_due; E.remove_context = f_remove_context;
    E.tree_extract = f_tree_extract; E.vec_insert = f_vec_insert; E.exec_run = f_exec_run; E.irq_run = f_irq_run;
    E.blocking_action = f_blocking_action; E.attention_changed = f_attention_changed; E.attention_block = f_attention_block;
    E.jump_to_label = f_jump_to_label; E.unregister_all = f_unregister_all; E.ctx_reset = f_ctx_reset;
    E.record_clear = f_record_clear; E.record_copy = f_record_copy; E.value_release = f_value_release;
    E.value_type_error = f_value_type_error; E.value_assign = f_value_assign; E.resume_after_restore = f_resume_after_restore;
    E.return_run_script = f_return_run_script; E.return_order_value = f_return_order_value; E.frame_context = f_frame_context;
    E.value_error = f_value_error; E.eval_string = f_eval_string; E.find_script = f_find_script; E.eval_bool = f_eval_bool;
    E.expr_error = f_expr_error; E.string_assign = f_string_assign; E.abort_reason = f_abort_reason;
    E.abort_stack = f_abort_stack; E.push_script = f_push_script; E.tree_erase = f_tree_erase;
    E.refresh_scripts = f_refresh_scripts; E.list_clear = f_list_clear; E.str_format = f_str_format;
    E.log_error = f_log_error; E.id_to_string = f_id_to_string; E.op_delete = f_op_delete;
    E.invalid_parameter = f_invalid_parameter; E.tls_init = f_tls_init; E.game_time = f_game_time;
    E.abort_table = NULL; E.order_result_key = &result_key;
    fake_tls[AD_TLS_INIT] = 1; U64(fake_tls, AD_TLS_RNG) = 0x1234; ad_engine_tls = f_engine_tls;
}

static void frame(int mode) {
    ad.director = DIR;
    ad.in_phase = 1;
    ad_move_impl(DIR, mode);
    ad.in_phase = 0;
}

int main(void) {
    image = VirtualAlloc(NULL, X4_IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image);
    *(void **)(image + AD_DIRECTOR_RVA) = DIR;
    memset(&fake_owner, 0, sizeof(fake_owner));
    fake_owner.armed = 1; current = &fake_owner; owner_thread = GetCurrentThreadId();
    qpc_frequency = 10000000; qpc_read = fake_qpc;
    fake_engine();
    ad.mode = AD_NORMAL;

    /* 1. Outer loop: event pass, then a timed pass harvesting due nodes in time order, then exit. */
    {
        world_reset(); nnodes = 0;
        void *c0 = mk(0, 0), *c1 = mk(1, 0), *c2 = mk(2, 1);
        static const int fin[] = {STEP(S_FINISH, 0, 0)}, yld[] = {STEP(S_YIELD, 0, 0)};
        steps(0, 1, fin); steps(1, 1, yld);
        queue(c0, 1);
        timed_add(c1, 5.0, 2); timed_add(c2, 7.0, 2);
        now_game = 6.0;
        frame(AD_NORMAL);
        /* c0 resumed and finished (no parent) -> removed; c1 harvested (due), c2 not (7 > 6) */
        static const int seq0[] = {T_ATTCHG, T_CLRTIMED, T_UNREG, T_RESET, T_RECCLR, T_RUN, T_REMOVE};
        assert(trace_seq_is(c0, seq0, 7));
        assert(trace_has(T_RUN, c1) && !trace_has(T_RUN, c2));
        assert(ad.f_passes == 2);                                  /* event pass + timed pass */
        assert(I64(DIR, AD_D_TIMED_SIZE) == 1);                    /* c1's node went to the pool */
        assert(PTR(DIR, AD_D_TIMED_LEFT) == PTR(c2, AD_C_TITER));  /* leftmost now c2 */
        assert(PTR(DIR, AD_D_FREE_NODES) == NODES[0]);              /* released into the node pool */
        assert(PTR(DIR, AD_D_ENTITY) == NULL);
        assert(I64(c1, AD_C_SLOT) == -1 && I64(c1, AD_C_SLOT + 8) == -1);
    }

    /* 2. Cleanup paths: mask 0 (event), timed-but-not-due tmask 0, child frame present. */
    {
        world_reset(); nnodes = 0;
        void *c0 = mk(0, 0), *c1 = mk(1, 0);
        queue(c0, 0);                                   /* queued with no reason bits */
        static unsigned char child[0x120];
        PTR(C[1].es, AD_E_CHILD) = child;               /* a nested frame is running */
        queue(c1, 1);
        frame(AD_NORMAL);
        assert(trace_count(T_CLRWAKE, c0) == 1 && !trace_has(T_RUN, c0));
        assert(trace_count(T_CLRWAKE, c1) == 1 && !trace_has(T_RUN, c1));
        world_reset(); nnodes = 0;
        void *c2 = mk(2, 0);
        timed_add(c2, 5.0, 0);                          /* due, timed mask 0 */
        now_game = 6.0;
        frame(AD_NORMAL);
        assert(trace_count(T_CLRTIMEDDUE, c2) == 1 && !trace_has(T_RUN, c2));
    }

    /* 3. Interrupt (mask 4): pending record copied, interrupt runner with flag 1, restart label. */
    {
        world_reset();
        void *c0 = mk(0, 0);
        C[0].blocking = 1; C[0].irq_restart = 1;
        static unsigned char recs[2][0x50];
        memset(recs, 0, sizeof(recs));
        I64(recs[0], 0) = 3; I64(recs[1], 0) = -1;      /* second record is the local interrupt */
        PTR(c0, AD_C_PENDING) = recs; PTR(c0, AD_C_PENDING + 8) = recs + 2;
        static const int yld[] = {STEP(S_YIELD, 0, 0)};
        steps(0, 1, yld);
        queue(c0, 4);
        frame(AD_NORMAL);
        static const int seq[] = {T_BLOCK, T_RECCPY, T_IRQ, T_RECCLR, T_ATTCHG, T_CLRTIMED, T_UNREG, T_RESET, T_RECCLR, T_JUMP, T_RUN};
        assert(trace_seq_is(c0, seq, 11));
        for (int i = 0; i < ntr; ++i) if (tr[i].op == T_JUMP) assert(tr[i].x == 1);   /* restart label */
        assert(ad.f_irq == 1 && ad.f_runs == 1);
    }

    /* 4. Attention-block change: no 0x81 bits -> attention handler, then new block and label 0. */
    {
        world_reset();
        void *c0 = mk(0, 0);
        C[0].att_changed = 1; C[0].blocking = 1;
        PTR(C[0].bl, 0x50) = (void *)0x77;               /* attention handler list present */
        I32(C[0].ent, AD_ENTITY_ATTENTION) = 7;
        static const int yld[] = {STEP(S_YIELD, 0, 0)};
        steps(0, 1, yld);
        queue(c0, 2);                                    /* blocking action done */
        frame(AD_NORMAL);
        static const int seq[] = {T_ATTCHG, T_BLOCK, T_IRQ, T_JUMP, T_CLRTIMED, T_UNREG, T_RESET, T_RECCLR, T_RUN};
        assert(trace_seq_is(c0, seq, 9));
        assert(I32(c0, AD_C_CACHED_ATT) == 7 && ad.win.attention_restarts == 1);
        /* start/return bit (1) suppresses the attention handler; only 0x10 and no change -> cleanup */
        world_reset();
        void *c1 = mk(1, 0);
        C[1].att_changed = 0;
        queue(c1, 0x10);
        frame(AD_NORMAL);
        assert(trace_has(T_CLRWAKE, c1) && !trace_has(T_RUN, c1));
    }

    /* 5. Script finished: return to the caller (run_script / run_order_script), wake it, remove. */
    {
        world_reset();
        void *c0 = mk(0, 0), *c1 = mk(1, 0);
        PTR(C[0].es, AD_E_PARENT) = C[1].es;            /* c0's script was called from c1's frame */
        U8(C[1].es, AD_E_YIELD) = 1; U64(C[1].es, AD_E_SAVED_PC) = 0;
        I32(C[1].act[0], 8) = AD_KW_RUN_SCRIPT;
        static const int fin[] = {STEP(S_FINISH, 0, 0)};
        steps(0, 1, fin);
        queue(c0, 1);
        frame(AD_NORMAL);
        assert(trace_has(T_RETRUN, c0) && trace_has(T_REMOVE, c0));
        assert(trace_has(T_REQWAKE, c1));                /* caller woken with "called script returned" */
        assert(trace_has(T_RUN, c1));                    /* and it ran in the next pass (mask consumed) */
        /* run_order_script without a result -> the engine's error, then still woken */
        world_reset();
        c0 = mk(0, 0); c1 = mk(1, 0);
        PTR(C[0].es, AD_E_PARENT) = C[1].es;
        U8(C[1].es, AD_E_YIELD) = 1;
        I32(C[1].act[0], 8) = AD_KW_RUN_ORDER_SCRIPT; I32(C[1].act[0], 0x88) = 1;
        PTR(C[0].es, AD_E_RESULTS + 8) = NULL;          /* empty result map */
        PTR(c0, AD_C_SCRIPT) = SCRIPT[2];
        steps(0, 1, fin);
        queue(c0, 1);
        frame(AD_NORMAL);
        assert(trace_has(T_VERR, NULL) && strstr(last_msg, "Returning order script 'order.trade' did not provide a return value"));
        assert(trace_has(T_REQWAKE, c1) && trace_has(T_RUN, c1));
        /* caller restoring -> ResumeAfterRestore instead of the 0x20 wake */
        world_reset();
        c0 = mk(0, 0); c1 = mk(1, 0);
        PTR(C[0].es, AD_E_PARENT) = C[1].es;
        U8(c1, AD_C_RESTORING) = 1;
        steps(0, 1, fin);
        queue(c0, 1);
        frame(AD_NORMAL);
        assert(trace_has(T_RESUME, c1) && trace_has(T_REMOVE, c0));
    }

    /* 6. Global interrupts (mask 0x40). */
    {
        static unsigned char handler[0x40], list_v[3 * 8], cand[0x100], rec[0x50];
        void *list_items[1] = {cand};
        /* the handler's condition list: one candidate */
        PTR(list_v, 0) = list_items; PTR(list_v, 8) = list_items + 1; PTR(list_v, 16) = list_items + 1;
        PTR(handler, 0x20) = list_v;
        /* (a) no handler registered: falls through to the generic path without 0x40 */
        world_reset();
        void *c0 = mk(0, 0);
        static const int yld[] = {STEP(S_YIELD, 0, 0)};
        steps(0, 1, yld);
        queue(c0, 0x41);
        frame(AD_NORMAL);
        assert(trace_has(T_RUN, c0) && ad.win.global_applied == 0);
        /* (b) handler with a script that beats the context's priority: pushed, applied to itself */
        world_reset();
        c0 = mk(0, 0);
#define ARM_HANDLER() do { PTR(SCRIPT[0], AD_S_HANDLERS) = handler; U64(SCRIPT[0], AD_S_HANDLER_COUNT) = 1; \
                           PTR(c0, AD_C_PENDING) = rec; PTR(c0, AD_C_PENDING + 8) = rec + 0x50; C[0].gsel = 0; } while (0)
        memset(rec, 0, sizeof(rec)); U64(rec, 0) = 0; I32(rec, 8) = 0x1234;
        ARM_HANDLER();
        memset(cand, 0, sizeof(cand)); I32(cand, 0x28) = 0x54;   /* has a script-name expression */
        g_eval_name = "handler.a"; g_push_ok = 1; g_abort_cond = 0;
        queue(c0, 0x40);
        frame(AD_NORMAL);
        assert(ad.win.global_applied == 1 && !strcmp(last_push, "handler.a"));
        assert(I32(c0, AD_C_RECORD) == 0x1234);                  /* event record handed to the handler */
        assert(!trace_has(T_RUN, c0));                            /* handler applied instead of a resume */
        assert(trace_has(T_JUMP, c0));                            /* not attached: jump to label 0 */
        /* (c) unknown script name: the engine's error, no handler, generic path continues */
        world_reset();
        c0 = mk(0, 0);
        ARM_HANDLER();
        g_eval_name = "nope";
        steps(0, 1, yld);
        queue(c0, 0x41);
        frame(AD_NORMAL);
        assert(trace_has(T_EXPRERR, NULL) && strstr(last_msg, "AI script name 'nope' not found"));
        assert(ad.win.global_applied == 0 && trace_has(T_RUN, c0));
        /* (d) priority not higher than the holder's: not chosen */
        world_reset();
        c0 = mk(0, 0);
        ARM_HANDLER();
        I32(c0, AD_C_PRIORITY) = 9; g_eval_name = "handler.a";
        steps(0, 1, yld);
        queue(c0, 0x41);
        frame(AD_NORMAL);
        assert(ad.win.global_applied == 0 && trace_has(T_RUN, c0));
        /* (e) handler without a script: restart at label 0 and re-wake (mask 0x80); the re-woken
               context runs in the next pass */
        world_reset();
        c0 = mk(0, 0);
        ARM_HANDLER();
        I32(cand, 0x28) = 0;
        queue(c0, 0x40);
        frame(AD_NORMAL);
        assert(ad.win.global_applied == 1 && trace_has(T_JUMP, c0) && trace_has(T_REQWAKE, c0) && trace_has(T_RUN, c0));
        /* (f) abort condition: the context's stack is aborted and the handler applies to it */
        world_reset();
        c0 = mk(0, 0);
        ARM_HANDLER();
        I32(cand, 0x28) = 0x54; g_eval_name = "handler.a"; g_abort_cond = 1; g_push_ok = 1;
        queue(c0, 0x40);
        frame(AD_NORMAL);
        assert(trace_has(T_ABORT, c0) && !strcmp(last_push, "handler.a") && ad.win.global_applied == 1);
        g_abort_cond = 0;
#undef ARM_HANDLER
    }

    /* 7. Pass 0x20 diagnostic, pass 0x21 without a flip, and the frame ends with the wake queued. */
    {
        world_reset();
        void *c0 = mk(0, 0);
        static int self[48];
        for (int i = 0; i < 48; ++i) self[i] = STEP(S_WAKESELF, 0, 0);
        steps(0, 48, self);
        queue(c0, 1);
        frame(AD_NORMAL);
        assert(C[0].runs == 0x21);                                  /* passes 0..0x20 */
        assert(strstr(last_log, "AIDirector::Move(): Possibly infinite wake request loop: order.fight on entity 256, wake reason mask=0001 (Most likely cause: Script starts and returns immediately)"));
        assert(ad.win.loop_logs == 1);
        uint32_t w = U32(DIR, AD_D_WIDX);
        assert(I64(c0, AD_C_SLOT + 8 * w) != -1);                    /* still queued for the next frame */
    }

    /* 8. normal vs rounds: interleaved sectors, a cross-sector wake of a context that is not queued
          this pass (it lands in the next pass in both); same multiset, per-sector order, final state. */
    {
        static const int run_a[] = {STEP(S_WAKE, 5, 0), STEP(S_FINISH, 0, 0)};
        static const int run_y[] = {STEP(S_YIELD, 0, 0)};
        int order_n[64], nn = 0, order_r[64], nr = 0;
        uint64_t hn = 0, hr = 1;
        for (int pass = 0; pass < 2; ++pass) {
            world_reset();
            void *a1 = mk(0, 0), *b1 = mk(1, 1), *a2 = mk(2, 0), *c1 = mk(3, 2), *b2 = mk(4, 1);
            mk(5, 2);                                                 /* d1: not queued */
            steps(0, 2, run_a); steps(1, 1, run_y); steps(2, 1, run_y); steps(3, 1, run_y); steps(4, 1, run_y);
            queue(a1, 1); queue(b1, 1); queue(a2, 1); queue(c1, 1); queue(b2, 1);
            frame(pass == 0 ? AD_NORMAL : AD_COMPARE);
            if (pass == 0) { memcpy(order_n, run_log, sizeof(int) * (size_t)nrun); nn = nrun; hn = ad_state_hash(DIR); }
            else { memcpy(order_r, run_log, sizeof(int) * (size_t)nrun); nr = nrun; hr = ad_state_hash(DIR); }
        }
        assert(nn == nr && nn == 6);                                  /* 5 + d1 woken by a1 */
        int cnt_n[NCTX] = {0}, cnt_r[NCTX] = {0};
        for (int i = 0; i < nn; ++i) { ++cnt_n[order_n[i]]; ++cnt_r[order_r[i]]; }
        assert(!memcmp(cnt_n, cnt_r, sizeof(cnt_n)));
        static const int sector_of[] = {0, 1, 0, 2, 1, 2};
        for (int s = 0; s < 3; ++s) {                                  /* per-sector order identical */
            int pn[16], ln = 0, pr[16], lr = 0;
            for (int i = 0; i < nn; ++i) { if (sector_of[order_n[i]] == s) pn[ln++] = order_n[i]; if (sector_of[order_r[i]] == s) pr[lr++] = order_r[i]; }
            assert(ln == lr && !memcmp(pn, pr, sizeof(int) * (size_t)ln));
        }
        assert(order_r[0] == 0 && order_r[1] == 2 && order_r[2] == 1 && order_r[3] == 4 && order_r[4] == 3 && order_r[5] == 5);
        assert(hn == hr);                                             /* same final AI state */
        assert(ad.win.cmp_passes_dirty == 0 && ad.win.cmp_passes_clean == 2 && ad.win.buckets_run == 4);
    }

    /* 8b. wake absorption: a2 (s0, index 2) wakes b1 (s1, index 1). The original already ran b1, so
           the wake survives and b1 runs again next pass; rounds runs b1 after a2, which absorbs it. */
    {
        static const int wake1[] = {STEP(S_WAKE, 1, 0)};
        static const int y[] = {STEP(S_YIELD, 0, 0)};
        int b1_n = 0, b1_r = 0;
        for (int pass = 0; pass < 2; ++pass) {
            world_reset();
            void *a1 = mk(0, 0), *b1 = mk(1, 1), *a2 = mk(2, 0);
            steps(0, 1, y); steps(1, 2, (const int[]){STEP(S_YIELD, 0, 0), STEP(S_YIELD, 0, 0)}); steps(2, 1, wake1);
            queue(a1, 1); queue(b1, 1); queue(a2, 1);
            frame(pass == 0 ? AD_NORMAL : AD_COMPARE);
            if (pass == 0) b1_n = C[1].runs; else b1_r = C[1].runs;
        }
        assert(b1_n == 2 && b1_r == 1);
        assert(ad.win.cmp_wake_absorb == 1 && ad.win.cmp_passes_dirty == 1);
    }

    /* 9. rounds order skips a context the original would have run (cross-bucket ClearWake). */
    {
        static const int clear1[] = {STEP(S_CLEAR, 1, 0)};
        static const int y[] = {STEP(S_YIELD, 0, 0)};
        int runs_n[NCTX], runs_r[NCTX];
        for (int pass = 0; pass < 2; ++pass) {
            world_reset();
            void *a1 = mk(0, 0), *b1 = mk(1, 1), *a2 = mk(2, 0);
            steps(0, 1, y); steps(1, 1, y); steps(2, 1, clear1);
            queue(a1, 1); queue(b1, 1); queue(a2, 1);
            frame(pass == 0 ? AD_NORMAL : AD_COMPARE);
            for (int i = 0; i < NCTX; ++i) (pass == 0 ? runs_n : runs_r)[i] = C[i].runs;
        }
        assert(runs_n[1] == 1 && runs_r[1] == 0);                     /* b1 ran in normal, not in rounds */
        assert(ad.win.cmp_null_lost == 1 && ad.win.cmp_passes_dirty == 1);
    }

    /* 10. Event buffer ordering (walk review): post in bucket B, cancel in bucket A, same source/type.
           Replay follows execution order; a cross-bucket pair is order-visible only when the rounds
           order inverts the original order of the two contexts. */
    {
        ad_evbuf = &FB; evbuf_on = 1;
        static const int post[] = {STEP(S_POST, 1, 9)}, cancel[] = {STEP(S_CANCEL, 1, 9)}, y[] = {STEP(S_YIELD, 0, 0)};
        /* (a) original order b(post) then a(cancel); bucket order keeps it: queue empty in both */
        for (int mode = 0; mode < 2; ++mode) {
            world_reset(); nq = 0; nb_ops = 0; delivered_imm = 0;
            void *b = mk(0, 1), *a = mk(1, 0);
            steps(0, 1, post); steps(1, 1, cancel);
            queue(b, 1); queue(a, 1);
            frame(mode == 0 ? AD_NORMAL : AD_COMPARE);
            assert(nq == 0);
            if (mode == 1) assert(ad.win.cmp_post_cancel == 0 && ad.win.deferred == 2);
        }
        /* (b) original order a(cancel) then b(post): the post survives in both */
        for (int mode = 0; mode < 2; ++mode) {
            world_reset(); nq = 0; nb_ops = 0;
            void *a = mk(0, 0), *b = mk(1, 1);
            steps(0, 1, cancel); steps(1, 1, post);
            queue(a, 1); queue(b, 1);
            frame(mode == 0 ? AD_NORMAL : AD_COMPARE);
            assert(nq == 1);
            if (mode == 1) assert(ad.win.cmp_post_cancel == 0);
        }
        /* (c) rounds inverts the order: a0(s0) b0(s1, post) a1(s0, cancel). Original: post then cancel
               -> empty. Rounds: s0 = [a0, a1] first -> cancel, then b0's post -> survives. Flagged. */
        int qn = -1, qr = -1;
        for (int mode = 0; mode < 2; ++mode) {
            world_reset(); nq = 0; nb_ops = 0;
            void *a0 = mk(0, 0), *b0 = mk(1, 1), *a1 = mk(2, 0);
            steps(0, 1, y); steps(1, 1, post); steps(2, 1, cancel);
            queue(a0, 1); queue(b0, 1); queue(a1, 1);
            frame(mode == 0 ? AD_NORMAL : AD_COMPARE);
            if (mode == 0) qn = nq; else qr = nq;
        }
        assert(qn == 0 && qr == 1);
        assert(ad.win.cmp_post_cancel == 1 && ad.win.cmp_passes_dirty == 1);
        /* (d) negative control: replaying tasks against execution order breaks case (b) */
        world_reset(); nq = 0; nb_ops = 0; reverse_replay = 1;
        {
            void *a = mk(0, 0), *b = mk(1, 1);
            steps(0, 1, cancel); steps(1, 1, post);
            queue(a, 1); queue(b, 1);
            frame(AD_ROUNDS);
            assert(nq == 0);                                        /* the post was wrongly cancelled */
        }
        reverse_replay = 0;
        /* (e) an immediate post is never cancelled (as in the original): delivered at the barrier */
        world_reset(); nq = 0; nb_ops = 0; delivered_imm = 0;
        {
            static const int posti[] = {STEP(S_POSTI, 1, 9)};
            void *b = mk(0, 1), *a = mk(1, 0);
            steps(0, 1, posti); steps(1, 1, cancel);
            queue(b, 1); queue(a, 1);
            frame(AD_ROUNDS);
            assert(delivered_imm == 1 && nq == 0);
        }
        /* (f) original-order barrier (ecs-core Q5, walk Q20): keys per op, key-aware cancel, replay by
               (key, emission). Case (c) now matches the original and is not flagged; case (g) -
               x0(s0) b0(s1, cancel) a1(s0, post): original keeps the post - is lost with the bucket
               order (flagged) and kept with the ordered barrier. */
        for (int ord = 0; ord < 2; ++ord) {
            ad_evbuf = ord ? &FBO : &FB; fbo_on = ord;
            world_reset(); nq = 0; nb_ops = 0;
            void *a0 = mk(0, 0), *b0 = mk(1, 1), *a1 = mk(2, 0);
            steps(0, 1, y); steps(1, 1, post); steps(2, 1, cancel);
            queue(a0, 1); queue(b0, 1); queue(a1, 1);
            frame(AD_COMPARE);
            assert(nq == (ord ? 0 : 1) && ad.win.cmp_post_cancel == (ord ? 0u : 1u));
            world_reset(); nq = 0; nb_ops = 0;
            void *x0 = mk(0, 0), *c0 = mk(1, 1), *p1 = mk(2, 0);
            steps(0, 1, y); steps(1, 1, cancel); steps(2, 1, post);
            queue(x0, 1); queue(c0, 1); queue(p1, 1);
            frame(AD_COMPARE);
            assert(nq == (ord ? 1 : 0) && ad.win.cmp_post_cancel == (ord ? 0u : 1u));
        }
        /* (h) interleaved buckets A1(s0) B1(s1) A2(s0) posting sources 1, 2, 3: the ordered barrier
               queues them 1, 2, 3 (original); the bucket order 1, 3, 2 */
        for (int ord = 0; ord < 2; ++ord) {
            static const int p1s[] = {STEP(S_POST, 1, 9)}, p2s[] = {STEP(S_POST, 2, 9)}, p3s[] = {STEP(S_POST, 3, 9)};
            ad_evbuf = ord ? &FBO : &FB; fbo_on = ord;
            world_reset(); nq = 0; nb_ops = 0;
            void *x = mk(0, 0), *yb = mk(1, 1), *z = mk(2, 0);
            steps(0, 1, p1s); steps(1, 1, p2s); steps(2, 1, p3s);
            queue(x, 1); queue(yb, 1); queue(z, 1);
            frame(AD_ROUNDS);
            assert(nq == 3 && Q[0].src == 0x7001 && Q[1].src == (ord ? 0x7002u : 0x7003u) && Q[2].src == (ord ? 0x7003u : 0x7002u));
        }
        fbo_on = 0;
        ad_evbuf = NULL; evbuf_on = 0;
    }


    /* 11. RNG order: the interpreter's random stream is shared; rounds runs a0, a1 (s0) before b0 (s1).
           a0 b0 a1 all draw -> drawing positions 0,2,1 in rounds order: inverted against the original. */
    {
        static const int draw[] = {STEP(S_DRAW, 0, 0)}, y[] = {STEP(S_YIELD, 0, 0)};
        world_reset();
        void *a0 = mk(0, 0), *b0 = mk(1, 1), *a1 = mk(2, 0);
        steps(0, 1, draw); steps(1, 1, draw); steps(2, 1, draw);
        queue(a0, 1); queue(b0, 1); queue(a1, 1);
        frame(AD_COMPARE);
        assert(ad.win.rng_runs == 3 && ad.win.cmp_rng_passes == 1);
        /* b0 does not draw: drawing positions 0,2 stay in order -> no RNG inversion */
        world_reset();
        a0 = mk(0, 0); b0 = mk(1, 1); a1 = mk(2, 0);
        steps(0, 1, draw); steps(1, 1, y); steps(2, 1, draw);
        queue(a0, 1); queue(b0, 1); queue(a1, 1);
        frame(AD_COMPARE);
        assert(ad.win.rng_runs == 2 && ad.win.cmp_rng_passes == 0);
    }

    /* 12. Original-order stamp (Q14): every run sees (pass, index in that pass's read vector), in normal
           and in rounds order. a0 b0 a1 queued; a1 wakes b1 -> b1 runs in pass 1 at index 0. */
    for (int m = 0; m < 2; ++m) {
        static const int y[] = {STEP(S_YIELD, 0, 0)}, wk[] = {STEP(S_WAKE, 3, 0)};
        world_reset();
        void *a0 = mk(0, 0), *b0 = mk(1, 1), *a1 = mk(2, 0);
        mk(3, 1);
        steps(0, 1, y); steps(1, 1, y); steps(2, 1, wk); steps(3, 1, y);
        queue(a0, 1); queue(b0, 1); queue(a1, 1);
        frame(m ? AD_ROUNDS : AD_NORMAL);
        static const int want_normal[] = {0, 1, 2, 3}, want_rounds[] = {0, 2, 1, 3};
        static const int64_t pos_normal[] = {0, 1, 2, 0}, pos_rounds[] = {0, 2, 1, 0};
        static const uint32_t pass_all[] = {0, 0, 0, 1};
        assert(nrun == 4);
        for (int k = 0; k < 4; ++k) {
            assert(run_log[k] == (m ? want_rounds : want_normal)[k]);
            assert(stamp_pos[k] == (m ? pos_rounds : pos_normal)[k] && stamp_pass[k] == pass_all[k]);
        }
    }
    /* 13. Write-vector order (ecs-core Q5): A1(s0) wakes X, B1(s1) wakes Y, A2(s0) wakes Z, A3(s0)
           clears X; X Y Z in s2. Rounds appends X Z Y (and nulls X); restored to the original
           [null, Y, Z], so pass 1 runs Y then Z in both modes, slot indices follow the entries. */
    for (int m = 0; m < 2; ++m) {
        static const int wx[] = {STEP(S_WAKE, 4, 0)}, wy[] = {STEP(S_WAKE, 5, 0)}, wz[] = {STEP(S_WAKE, 6, 0)},
                         cx[] = {STEP(S_CLEAR, 4, 0)}, y[] = {STEP(S_YIELD, 0, 0)};
        world_reset();
        void *a1 = mk(0, 0), *b1 = mk(1, 1), *a2 = mk(2, 0), *a3 = mk(3, 0);
        mk(4, 2); mk(5, 2); mk(6, 2);
        steps(0, 1, wx); steps(1, 1, wy); steps(2, 1, wz); steps(3, 1, cx);
        steps(4, 1, y); steps(5, 1, y); steps(6, 1, y);
        queue(a1, 1); queue(b1, 1); queue(a2, 1); queue(a3, 1);
        frame(m ? AD_COMPARE : AD_NORMAL);
        assert(nrun == 6 && run_log[4] == 5 && run_log[5] == 6);
        assert(stamp_pass[4] == 1 && stamp_pos[4] == 1 && stamp_pos[5] == 2);
        if (m) assert(ad.win.wake_reordered == 1 && ad.win.wake_order_skipped == 0 && run_log[1] == 2 && run_log[3] == 1);
        for (int k = 0; k < 7; ++k) assert(I64(CX(k), AD_C_SLOT) == -1 && I64(CX(k), AD_C_SLOT + 8) == -1);
    }
    /* same wakers without the reorder would have run Z before Y: the slot check refuses a corrupt
       entry (fail closed, counted) and leaves the vector as it is */
    {
        static const int wx[] = {STEP(S_WAKE, 4, 0)}, wy[] = {STEP(S_WAKE, 5, 0)}, wz[] = {STEP(S_WAKE, 6, 0)}, y[] = {STEP(S_YIELD, 0, 0)};
        world_reset();
        void *a1 = mk(0, 0), *b1 = mk(1, 1), *a2 = mk(2, 0);
        mk(4, 2); mk(5, 2); mk(6, 2);
        steps(0, 1, wx); steps(1, 1, wy); steps(2, 1, wz); steps(4, 1, y); steps(5, 1, y); steps(6, 1, y);
        queue(a1, 1); queue(b1, 1); queue(a2, 1);
        ad_wake_ok = 1;
        unsigned w = U32(DIR, AD_D_WIDX);       /* simulate a rounds pass's appends: X(0) Z(2) Y(1) */
        f_request_wake(DIR, CX(4)); f_request_wake(DIR, CX(6)); f_request_wake(DIR, CX(5));
        size_t base = ad_vec_len(ad_vec(DIR, w)) - 3;
        ad_wake_note(base, base + 1, 0); ad_wake_note(base + 1, base + 2, 2); ad_wake_note(base + 2, base + 3, 1);
        I64(CX(6), AD_C_SLOT + 8 * w) = 99;     /* corrupt slot: refused */
        ad_wake_restore(DIR, w, base);
        assert(ad.win.wake_order_skipped == 1 && ((void **)ad_vec(DIR, w)[0])[base + 1] == CX(6));
        I64(CX(6), AD_C_SLOT + 8 * w) = (int64_t)(base + 1);
        ad_wake_restore(DIR, w, base);
        assert(ad.win.wake_reordered == 1 && ((void **)ad_vec(DIR, w)[0])[base + 1] == CX(5) &&
               ((void **)ad_vec(DIR, w)[0])[base + 2] == CX(6) && I64(CX(5), AD_C_SLOT + 8 * w) == (int64_t)(base + 1) &&
               I64(CX(6), AD_C_SLOT + 8 * w) == (int64_t)(base + 2));
    }
    VirtualFree(image, 0, MEM_RELEASE);
    puts("PASS: aidriver stage 1 outer loop: flip, timed harvest in time order into the node pool, exit");
    puts("PASS: aidriver stage 1 cleanup, interrupt-restart and attention-block paths match the original call order");
    puts("PASS: aidriver stage 1 completion: run_script/run_order_script return, caller wake, restore, remove");
    puts("PASS: aidriver stage 1 global interrupts: priority, unknown script, no-script restart, push");
    puts("PASS: aidriver stage 1 pass 0x20 diagnostic text, pass 0x21 without flip, wake carried to next frame");
    puts("PASS: aidriver stage 1 rounds == normal (multiset, per-sector order, state hash) without cross-bucket interaction");
    puts("PASS: aidriver stage 1 compare mode flags a cross-bucket ClearWake that changes which contexts run");
    puts("PASS: aidriver stage 1 compare mode flags a cross-bucket wake absorbed in one order but not the other");
    puts("PASS: aidriver stage 1 event buffer: post/cancel across buckets in both orders, inversion flagged, reverse-replay control");
    puts("PASS: aidriver stage 1 original-order barrier (pass, wake index, emission): post/cancel and interleaved buckets match the original");
    puts("PASS: aidriver stage 1 compare mode flags passes whose random draws run in another relative order");
    puts("PASS: aidriver stage 1 original-order stamp (pass, read-vector index) in normal and rounds order");
    puts("PASS: aidriver stage 1 rounds puts the pass's wakes back in the original order (slots follow, corrupt slot refused)");
    return 0;
}
