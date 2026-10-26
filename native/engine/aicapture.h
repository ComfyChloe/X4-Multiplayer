/* AI decision capture (agent ai-orders; reports/REWRITE-AI-ORDERS.md section 7, format X4AITRACE v1).
 *
 * Chains on the ai-driver's observer table (native/engine/aidriver.h `ad_observer`, contrib/ferrite-rw-
 * ai-driver): no hook site of its own, read-only (never writes engine memory). For the AI contexts of
 * up to AC_MAX_ENTS entities whose order script matches a filter (e.g. "order.fight.patrol"), it writes
 * one record per interpreter bracket and per event delivery:
 *   run / irun   ExecState_Run / interrupt-runner start: script, game time (TLS+0x300), wake mask,
 *                TLS+0x318 (interpreter RNG), pc, the pending global-interrupt queue (irun)
 *   yield / irend  end of the bracket: TLS+0x318, pc, yield flag, blocking action keyword id and XML
 *                line, timed wake (ctx+0x190), abort/resume index (ctx+0x1a0)
 *   ev           HandleScriptEvent of the entity's AIScriptContext (path=block) or AICallStackContext
 *                (path=global): event keyword id, object/params, TLS+0x318 before/after, wake mask
 *                before/after, the global-interrupt queue entries the delivery added (frame/handler index)
 * Lines go to X4's stdout prefixed "AITR\t" (dev/x4lane.py collects engine-output.log); the Rust side
 * (x4mp_ai::native) converts and checks them. Engine layouts below are from the decompile notes of
 * 0xb58bb0, 0x109a6a0, 0x1066750, 0x1062f70 and 0x106d250 (X4 9.00, build 23660954); every offset is
 * read only while the ai-driver's build check holds (fail closed: nothing is installed otherwise).
 *
 * Part A (formatting, filter, buffer) is plain C and tested by test_aicapture.c. Part B (engine reads,
 * observer callbacks, Lua methods) is compiled only when included after aidriver.h. */

#ifndef X4LAN_AICAPTURE_H
#define X4LAN_AICAPTURE_H

#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- Part A: records, filter, buffer --------------------------------------------------------- */

#define AC_MAX_ENTS 32
#define AC_BUF_BYTES (512u * 1024u)
#define AC_LINE_BYTES 2048
#define AC_FILTER_BYTES 64
#define AC_PREFIX "AITR\t"

typedef int (*ac_sink_fn)(const char *data, size_t len, void *user); /* returns 0 on failure */

typedef struct {
    int on, failed;
    char filter[AC_FILTER_BYTES];
    unsigned max_ents, n_ents;
    uint64_t ents[AC_MAX_ENTS];
    char buf[AC_BUF_BYTES];
    size_t len;
    uint64_t lines, dropped, flushes, runs, iruns, events, bytes;
    ac_sink_fn sink;
    void *sink_user;
} ac_capture;

static int ac_flush(ac_capture *c) {
    if (!c->len) return 1;
    int ok = c->sink && c->sink(c->buf, c->len, c->sink_user);
    if (!ok) { c->failed = 1; c->on = 0; }
    c->bytes += c->len;
    c->len = 0;
    ++c->flushes;
    return ok;
}

/* Append one record (printf-style, without prefix and newline). Lines that do not fit are dropped and
   counted; the buffer is flushed when it is three quarters full. */
static void ac_line(ac_capture *c, const char *fmt, ...) {
    if (c->failed) { ++c->dropped; return; }
    char line[AC_LINE_BYTES];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= sizeof(line)) { ++c->dropped; return; }
    size_t need = sizeof(AC_PREFIX) - 1 + (size_t)n + 1;
    if (c->len + need > AC_BUF_BYTES && !ac_flush(c)) { ++c->dropped; return; }
    if (c->len + need > AC_BUF_BYTES) { ++c->dropped; return; }
    memcpy(c->buf + c->len, AC_PREFIX, sizeof(AC_PREFIX) - 1);
    c->len += sizeof(AC_PREFIX) - 1;
    memcpy(c->buf + c->len, line, (size_t)n);
    c->len += (size_t)n;
    c->buf[c->len++] = '\n';
    ++c->lines;
    if (c->len > AC_BUF_BYTES / 4 * 3) ac_flush(c);
}

static int ac_tracked(const ac_capture *c, uint64_t ent) {
    for (unsigned i = 0; i < c->n_ents; ++i) if (c->ents[i] == ent) return 1;
    return 0;
}

/* A context is captured when its entity is tracked, or when its script matches the filter and there
   is room: the entity is then tracked from that bracket on (all its frames, events and callees). */
static int ac_select(ac_capture *c, uint64_t ent, const char *script) {
    if (!ent) return 0;
    if (ac_tracked(c, ent)) return 1;
    if (!script || strcmp(script, c->filter) != 0 || c->n_ents >= c->max_ents) return 0;
    c->ents[c->n_ents++] = ent;
    ac_line(c, "track\tc:0x%llx\t%s\tslot=i:%u", (unsigned long long)ent, script, c->n_ents - 1);
    return 1;
}

static void ac_begin(ac_capture *c, const char *filter, unsigned max_ents, ac_sink_fn sink, void *user) {
    memset(c->ents, 0, sizeof(c->ents));
    c->n_ents = 0;
    c->len = 0;
    c->lines = c->dropped = c->flushes = c->runs = c->iruns = c->events = c->bytes = 0;
    c->failed = 0;
    size_t n = strlen(filter);
    if (n >= AC_FILTER_BYTES) n = AC_FILTER_BYTES - 1;
    memcpy(c->filter, filter, n);
    c->filter[n] = 0;
    c->max_ents = max_ents && max_ents <= AC_MAX_ENTS ? max_ents : AC_MAX_ENTS;
    c->sink = sink;
    c->sink_user = user;
    c->on = 1;
    ac_line(c, "X4AITRACE\t1\tbuild=23660954\tsource=native\tcapture=bracket\tfilter=%s\tmax=%u", c->filter, c->max_ents);
}

static void ac_end(ac_capture *c) {
    if (!c->on && !c->len) return;
    ac_line(c, "# end lines=%llu dropped=%llu runs=%llu iruns=%llu events=%llu entities=%u",
            (unsigned long long)c->lines, (unsigned long long)c->dropped, (unsigned long long)c->runs,
            (unsigned long long)c->iruns, (unsigned long long)c->events, c->n_ents);
    ac_flush(c);
    c->on = 0;
}

/* Encode an engine script Value {u32 tag; u32 pad; u64 payload} as an X4AITRACE token: components
   (tag 10) by UniverseID, plain numbers raw, everything else by tag only (no pointer chasing). */
static void ac_value(char *out, size_t cap, uint32_t tag, uint64_t payload) {
    if (tag == 0) snprintf(out, cap, "n");
    else if (tag == 10) snprintf(out, cap, payload ? "c:0x%llx" : "n", (unsigned long long)payload);
    else if (tag >= 1 && tag <= 3) snprintf(out, cap, "i:%lld", (long long)(tag == 1 ? (int64_t)(int32_t)payload : (int64_t)payload));
    else if (tag >= 4 && tag <= 6) {
        uint32_t b = (uint32_t)payload;
        snprintf(out, cap, "f:%08x", b);
    } else if (tag >= 7 && tag <= 9) snprintf(out, cap, "d:%016llx", (unsigned long long)payload);
    else snprintf(out, cap, "t:%u", tag);
}

static uint64_t ac_bits(double v) {
    uint64_t b;
    memcpy(&b, &v, sizeof(b));
    return b;
}

/* ---- Part B: engine side on ai-driver's observer ------------------------------------------------ */
#ifdef AD_DIRECTOR_RVA

#define AC_C_QUEUE 0xc0       /* AIScriptContext: vector<{u64 handler index (-1 = blocking), record}> */
#define AC_Q_ENTRY 0x50       /* bytes per queue entry */
#define AC_C_ABORT 0x1a0      /* u64: abort/resume action index set by interrupt actions, -1 none */
#define AC_E_NEXT 0x00        /* ExecState: caller's ExecState */
#define AC_E_PROGRAM 0x10     /* ExecState: vector<Action*>* */
#define AC_E_PC 0x18
#define AC_E_BLOCK 0x20       /* index of the blocking action (AICallFrame_GetBlockingAction 0x109a6a0) */
#define AC_E_YIELD 0x30       /* byte: yielded */
#define AC_E_CTXVAL 0x38      /* Value {tag 0x66, payload = context + 0xa8} */
#define AC_CTXVAL_BIAS 0xa8
#define AC_A_KEYWORD 0x08
#define AC_A_LINE 0x0c
#define AC_H_RECORD 0x18      /* ScriptEventHandler: event record (int keyword, then Values at +0x08..) */
#define AC_MAX_FRAMES 16
#define AC_MAX_NEST 8

typedef struct {
    void *handler; int kind; uint64_t ent, rng;
    unsigned frames;
    void *ctx[AC_MAX_FRAMES];
    uint32_t mask[AC_MAX_FRAMES];
    size_t queue[AC_MAX_FRAMES];
} ac_delivery;

static ac_capture ac;
static ac_delivery ac_nest[AC_MAX_NEST];
static int ac_depth;
static HANDLE ac_out;
static ad_observer ac_obs;

static int ac_stdout_sink(const char *data, size_t len, void *user) {
    (void)user;
    DWORD written = 0;
    return ac_out && ac_out != INVALID_HANDLE_VALUE && WriteFile(ac_out, data, (DWORD)len, &written, NULL) && written == len;
}

static uint64_t ac_ent_uid(const void *ctx) {
    const unsigned char *e = ad_ctx_entity(ctx);
    return e ? *(const uint64_t *)(e + AD_COMP_ID) : 0;
}

static void ac_ctx_script(const void *ctx, char *out, size_t cap) {
    ad_script_name(ctx ? *(void *const *)((const unsigned char *)ctx + AD_C_SCRIPT) : NULL, out, cap);
    if (!out[0]) snprintf(out, cap, "?");
}

static size_t ac_queue_size(const void *ctx) {
    const unsigned char *c = ctx;
    uintptr_t b = *(const uintptr_t *)(c + AC_C_QUEUE), e = *(const uintptr_t *)(c + AC_C_QUEUE + 8);
    return e > b && (e - b) % AC_Q_ENTRY == 0 && e - b < AC_Q_ENTRY * 4096u ? (e - b) / AC_Q_ENTRY : 0;
}

static uint64_t ac_queue_handler(const void *ctx, size_t i) {
    const unsigned char *b = *(const unsigned char *const *)((const unsigned char *)ctx + AC_C_QUEUE);
    return *(const uint64_t *)(b + i * AC_Q_ENTRY);
}

/* The entity's frames from the top: the context's ExecState, then its callers (es+0), each frame's
   context from the Value at es+0x38 (tag 0x66). */
static unsigned ac_frames(void *ctx, void **out, unsigned cap) {
    unsigned n = 0;
    const unsigned char *es = ctx ? *(const unsigned char *const *)((const unsigned char *)ctx + AD_C_EXEC) : NULL;
    while (es && n < cap) {
        if (*(const uint32_t *)(es + AC_E_CTXVAL) != 0x66) break;
        uintptr_t v = *(const uintptr_t *)(es + AC_E_CTXVAL + 8);
        out[n++] = v ? (void *)(v - AC_CTXVAL_BIAS) : NULL;
        es = *(const unsigned char *const *)(es + AC_E_NEXT);
    }
    return n;
}

static void ac_queue_list(const void *ctx, char *out, size_t cap) {
    size_t n = ac_queue_size(ctx), len = 0;
    out[0] = 0;
    for (size_t i = 0; i < n && i < 16 && len + 24 < cap; ++i) {
        int w = snprintf(out + len, cap - len, "%si:%lld", i ? ";" : "", (long long)ac_queue_handler(ctx, i));
        if (w < 0) break;
        len += (size_t)w;
    }
}

/* The engine TLS block of the calling thread (tests substitute a fake block). */
static unsigned char *(*ac_tls_block)(void) = ad_engine_tls;

static int ac_tls(uint64_t *rng, double *now) {
    unsigned char *tls = ac_tls_block();
    if (!tls) return 0;
    *rng = *(volatile uint64_t *)(tls + AD_TLS_RNG);
    *now = *(volatile double *)(tls + AD_TLS_GAME_TIME);
    return 1;
}

static void ac_run_begin(void *ctx, void *es, unsigned mask, int irq, void *user) {
    (void)user;
    if (!ac.on) return;
    char script[64];
    ac_ctx_script(ctx, script, sizeof(script));
    uint64_t ent = ac_ent_uid(ctx);
    if (!ac_select(&ac, ent, script)) return;
    uint64_t rng; double now;
    if (!ac_tls(&rng, &now)) return;
    uint64_t pc = es ? *(const uint64_t *)((const unsigned char *)es + AC_E_PC) : 0;
    if (irq) {
        char q[400];
        ac_queue_list(ctx, q, sizeof(q));
        ++ac.iruns;
        ac_line(&ac, "irun\tc:0x%llx\t%s\tt=d:%016llx\tmask=x:%x\trng=x:%016llx\tpc=i:%llu\tqueue=l:[%s]\tctx=x:%llx",
                (unsigned long long)ent, script, (unsigned long long)ac_bits(now), mask, (unsigned long long)rng,
                (unsigned long long)pc, q, (unsigned long long)(uintptr_t)ctx);
    } else {
        ++ac.runs;
        ac_line(&ac, "run\tc:0x%llx\t%s\tt=d:%016llx\tmask=x:%x\trng=x:%016llx\tpc=i:%llu\tctx=x:%llx",
                (unsigned long long)ent, script, (unsigned long long)ac_bits(now), mask, (unsigned long long)rng,
                (unsigned long long)pc, (unsigned long long)(uintptr_t)ctx);
    }
}

static void ac_run_end(void *ctx, void *es, int irq, void *user) {
    (void)user;
    if (!ac.on) return;
    uint64_t ent = ac_ent_uid(ctx);
    if (!ac_tracked(&ac, ent)) return;
    uint64_t rng; double now;
    if (!ac_tls(&rng, &now)) return;
    char script[64];
    ac_ctx_script(ctx, script, sizeof(script));
    const unsigned char *e = es;
    uint64_t pc = e ? *(const uint64_t *)(e + AC_E_PC) : 0;
    const unsigned char *c = ctx;
    if (irq) {
        ac_line(&ac, "irend\tc:0x%llx\t%s\trng=x:%016llx\tpc=i:%llu\tabortidx=i:%lld",
                (unsigned long long)ent, script, (unsigned long long)rng, (unsigned long long)pc,
                (long long)*(const int64_t *)(c + AC_C_ABORT));
        return;
    }
    int yielded = e && e[AC_E_YIELD];
    uint32_t kw = 0, line = 0;
    if (yielded) {
        const uintptr_t *vec = *(const uintptr_t *const *)(e + AC_E_PROGRAM);
        uint64_t bi = *(const uint64_t *)(e + AC_E_BLOCK);
        if (vec && vec[1] > vec[0] && bi < (vec[1] - vec[0]) / sizeof(void *)) {
            const unsigned char *a = ((const unsigned char *const *)vec[0])[bi];
            if (a) { kw = *(const uint32_t *)(a + AC_A_KEYWORD); line = *(const uint32_t *)(a + AC_A_LINE); }
        }
    }
    double wake = *(const double *)(c + AD_C_WAKETIME);
    ac_line(&ac, "yield\tc:0x%llx\t%s\trng=x:%016llx\tpc=i:%llu\tyielded=b:%d\tkw=i:%u\tline=i:%u\tuntil=d:%016llx",
            (unsigned long long)ent, script, (unsigned long long)rng, (unsigned long long)pc, yielded, kw, line,
            (unsigned long long)ac_bits(wake));
}

static void ac_event_begin(void *handler, int kind, void *user) {
    (void)user;
    if (!ac.on || kind == AD_V_CUE) return;
    if (ac_depth >= AC_MAX_NEST) { ++ac_depth; return; }
    ac_delivery *d = &ac_nest[ac_depth++];
    d->handler = NULL;
    void *ctx = kind == AD_V_CTX ? handler : *(void **)((unsigned char *)handler + AD_S_CURRENT);
    uint64_t ent = ac_ent_uid(ctx);
    if (!ac_tracked(&ac, ent)) return;
    double now;
    if (!ac_tls(&d->rng, &now)) return;
    d->handler = handler; d->kind = kind; d->ent = ent;
    d->frames = ac_frames(ctx, d->ctx, AC_MAX_FRAMES);
    if (kind == AD_V_CTX) { d->frames = 1; d->ctx[0] = ctx; }
    for (unsigned i = 0; i < d->frames; ++i) {
        d->mask[i] = d->ctx[i] ? *(const uint32_t *)((const unsigned char *)d->ctx[i] + AD_C_MASK) : 0;
        d->queue[i] = d->ctx[i] ? ac_queue_size(d->ctx[i]) : 0;
    }
}

static void ac_event_end(void *handler, int kind, void *user) {
    (void)user;
    if (kind == AD_V_CUE || ac_depth <= 0) return;
    ac_delivery *d = &ac_nest[--ac_depth];
    if (ac_depth >= AC_MAX_NEST || !ac.on || d->handler != handler) return;
    uint64_t rng; double now;
    if (!ac_tls(&rng, &now)) return;
    const unsigned char *rec = (const unsigned char *)handler + AC_H_RECORD;
    char v[4][40];
    for (int i = 0; i < 4; ++i) {
        const unsigned char *val = rec + 0x08 + 0x10 * (size_t)i;
        ac_value(v[i], sizeof(v[i]), *(const uint32_t *)val, *(const uint64_t *)(val + 8));
    }
    char matched[600];
    size_t len = 0;
    matched[0] = 0;
    uint32_t mask_after = 0;
    for (unsigned i = 0; i < d->frames; ++i) {
        if (!d->ctx[i]) continue;
        uint32_t m = *(const uint32_t *)((const unsigned char *)d->ctx[i] + AD_C_MASK);
        if (i == 0) mask_after = m;
        size_t q = ac_queue_size(d->ctx[i]);
        char script[64];
        for (size_t k = d->queue[i]; k < q && len + 96 < sizeof(matched); ++k) {
            ac_ctx_script(d->ctx[i], script, sizeof(script));
            int w = snprintf(matched + len, sizeof(matched) - len, "%ss:%s/%lld", len ? ";" : "", script,
                             (long long)ac_queue_handler(d->ctx[i], k));
            if (w < 0) break;
            len += (size_t)w;
        }
    }
    ++ac.events;
    ac_line(&ac, "ev\tc:0x%llx\tkw:%d\tpath=k:%s\tt=d:%016llx\trng=x:%016llx\trng_after=x:%016llx\tobject=%s\tparam=%s\tparam2=%s\tparam3=%s\tmask=x:%x\tmask_after=x:%x\tmatched=l:[%s]",
            (unsigned long long)d->ent, *(const int32_t *)rec, kind == AD_V_CTX ? "block" : "global",
            (unsigned long long)ac_bits(now), (unsigned long long)d->rng, (unsigned long long)rng, v[0], v[1], v[2], v[3],
            d->frames ? d->mask[0] : 0, mask_after, matched);
}

static void ac_uninstall(void) {
    if (ad.observer == &ac_obs) ad.observer = NULL;
    ac_end(&ac);
    ac_depth = 0;
}

/* owner:aicapture(filter[, max_entities]): attach to the armed ai-driver census. "off" detaches. */
static int ac_lua_start(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    const char *filter = p_lua_type(L, 2) == LUA_TSTRING ? p_lua_tolstring(L, 2, NULL) : NULL;
    if (!filter) return failure(L, "aicapture filter required");
    if (o != current || !o->armed) return failure(L, "probe not armed");
    if (GetCurrentThreadId() != owner_thread) return failure(L, "owner thread required");
    if (!strcmp(filter, "off")) { ac_uninstall(); p_lua_pushboolean(L, 1); return 1; }
    if (!ad.installed || ad.mode != AD_CENSUS) return failure(L, "aidriver census not installed");
    if (ad.observer && ad.observer != &ac_obs) return failure(L, "observer slot taken");
    ac_out = GetStdHandle(STD_OUTPUT_HANDLE);
    if (!ac_out || ac_out == INVALID_HANDLE_VALUE || GetFileType(ac_out) == FILE_TYPE_UNKNOWN)
        return failure(L, "no stdout for the capture");
    unsigned max = p_lua_type(L, 3) == LUA_TNUMBER ? (unsigned)p_lua_tonumber(L, 3) : 8u;
    ac_begin(&ac, filter, max, ac_stdout_sink, NULL);
    ac_obs.run_begin = ac_run_begin; ac_obs.run_end = ac_run_end;
    ac_obs.event_begin = ac_event_begin; ac_obs.event_end = ac_event_end; ac_obs.user = NULL;
    ac_depth = 0;
    ad.observer = &ac_obs;
    p_lua_pushboolean(L, 1);
    return 1;
}

/* owner:aicapstats(): flush and report counters. */
static int ac_lua_stats(lua_State *L) {
    owner *o = p_luaL_checkudata(L, 1, META);
    (void)o;
    if (ac.on) ac_flush(&ac);
    p_lua_createtable(L, 0, 12);
    number_field(L, "on", ac.on);
    number_field(L, "failed", ac.failed);
    number_field(L, "entities", ac.n_ents);
    number_field(L, "lines", (double)ac.lines);
    number_field(L, "dropped", (double)ac.dropped);
    number_field(L, "runs", (double)ac.runs);
    number_field(L, "iruns", (double)ac.iruns);
    number_field(L, "events", (double)ac.events);
    number_field(L, "bytes", (double)ac.bytes);
    return 1;
}

#endif /* AD_DIRECTOR_RVA */
#endif /* X4LAN_AICAPTURE_H */
