/* Synthetic tests for the AI decision capture (native/engine/aicapture.h). No game is involved.
 *
 * Mode A (default, this branch): records, entity filter, buffer and flush (Part A only).
 * Mode B (-DAC_TEST_ENGINE, in a tree that also has the ai-driver's aidriver.h): includes fixedtick.c
 * and drives the observer callbacks with fake AIScriptContexts, ExecStates, actions and event records
 * laid out as in the decompile notes; checks the emitted run/yield/irun/irend/ev records field by field.
 * Build (mode B): zig cc -target x86_64-windows-gnu -std=c11 -O1 -Wall -Wextra -Werror -DAC_TEST_ENGINE
 *   -I <out dir with fixedtick_signatures.h> native/engine/test_aicapture.c -lbcrypt -o test_aicapture.exe */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>

#ifdef AC_TEST_ENGINE
#include "fixedtick.c"
#else
#include <stdint.h>
#include "aicapture.h"
#endif

static char sink_buf[4 << 20];
static size_t sink_len;
static int sink_fail;
static int mem_sink(const char *data, size_t len, void *user) {
    (void)user;
    if (sink_fail || sink_len + len > sizeof(sink_buf)) return 0;
    memcpy(sink_buf + sink_len, data, len);
    sink_len += len;
    sink_buf[sink_len] = 0;
    return 1;
}

static int starts(const char *s, const char *prefix) { return !strncmp(s, prefix, strlen(prefix)); }

/* n-th line (0-based) of the sink, without prefix; NULL if absent. */
static const char *line_at(int n, char *out, size_t cap) {
    const char *p = sink_buf;
    for (int i = 0; i < n; ++i) {
        p = strchr(p, '\n');
        if (!p) return NULL;
        ++p;
    }
    if (!*p) return NULL;
    assert(!strncmp(p, AC_PREFIX, strlen(AC_PREFIX)));
    p += strlen(AC_PREFIX);
    const char *e = strchr(p, '\n');
    size_t n2 = e ? (size_t)(e - p) : strlen(p);
    if (n2 >= cap) n2 = cap - 1;
    memcpy(out, p, n2);
    out[n2] = 0;
    return out;
}

static void test_part_a(void) {
    static ac_capture c;
    sink_len = 0; sink_fail = 0;
    ac_begin(&c, "order.fight.patrol", 2, mem_sink, NULL);
    assert(c.on && c.max_ents == 2);
    assert(!ac_select(&c, 0x10, "move.generic"));          /* not the filter: ignored */
    assert(ac_select(&c, 0x10, "order.fight.patrol"));     /* tracked from now on */
    assert(ac_select(&c, 0x10, "move.seekenemies"));       /* its callees too */
    assert(ac_select(&c, 0x11, "order.fight.patrol"));
    assert(!ac_select(&c, 0x12, "order.fight.patrol"));    /* cap 2 */
    assert(!ac_select(&c, 0, "order.fight.patrol"));       /* no entity */
    char v[48];
    ac_value(v, sizeof(v), 10, 0x1a2b); assert(!strcmp(v, "c:0x1a2b"));
    ac_value(v, sizeof(v), 10, 0); assert(!strcmp(v, "n"));
    ac_value(v, sizeof(v), 0, 5); assert(!strcmp(v, "n"));
    ac_value(v, sizeof(v), 1, 0xffffffffull); assert(!strcmp(v, "i:-1"));
    ac_value(v, sizeof(v), 5, 0x3f800000ull); assert(!strcmp(v, "f:3f800000"));
    ac_value(v, sizeof(v), 8, 0x3ff0000000000000ull); assert(!strcmp(v, "d:3ff0000000000000"));
    ac_value(v, sizeof(v), 0x55, 0x1234); assert(!strcmp(v, "t:85"));
    assert(ac_bits(1.0) == 0x3ff0000000000000ull);
    /* buffer: many lines flush without loss */
    for (int i = 0; i < 20000; ++i) ac_line(&c, "x\tc:0x1\t-\ti=i:%d\tpad=s:%s", i, "0123456789abcdef0123456789abcdef");
    ac_end(&c);
    assert(!c.on && !c.dropped && c.flushes >= 2);
    char l[AC_LINE_BYTES];
    assert(line_at(0, l, sizeof(l)) && starts(l, "X4AITRACE\t1\tbuild=23660954\tsource=native"));
    assert(line_at(1, l, sizeof(l)) && !strcmp(l, "track\tc:0x10\torder.fight.patrol\tslot=i:0"));
    assert(line_at(2, l, sizeof(l)) && !strcmp(l, "track\tc:0x11\torder.fight.patrol\tslot=i:1"));
    assert(line_at(3, l, sizeof(l)) && !strcmp(l, "x\tc:0x1\t-\ti=i:0\tpad=s:0123456789abcdef0123456789abcdef"));
    assert(line_at(20002, l, sizeof(l)) && !strcmp(l, "x\tc:0x1\t-\ti=i:19999\tpad=s:0123456789abcdef0123456789abcdef"));
    assert(line_at(20003, l, sizeof(l)) && starts(l, "# end lines=20003 "));
    /* a failing sink stops the capture */
    static ac_capture d;
    sink_len = 0; sink_fail = 1;
    ac_begin(&d, "p", 1, mem_sink, NULL);
    for (int i = 0; i < 20000 && d.on; ++i) ac_line(&d, "y %d %s", i, "0123456789abcdef0123456789abcdef0123456789");
    assert(d.failed && !d.on);
    printf("PASS: aicapture records, entity filter, buffer and flush, failing sink\n");
}

#ifdef AC_TEST_ENGINE
struct lua_State { int unused; };
static owner fake_owner;
static unsigned char fake_tls_block[0x400];
static unsigned char *fake_tls(void) { return fake_tls_block; }
static void set_u64(void *p, size_t off, unsigned long long v) { *(unsigned long long *)((unsigned char *)p + off) = v; }
static void set_u32(void *p, size_t off, unsigned v) { *(uint32_t *)((unsigned char *)p + off) = v; }
static void set_ptr(void *p, size_t off, const void *v) { *(const void **)((unsigned char *)p + off) = v; }
static void set_f64(void *p, size_t off, double v) { *(double *)((unsigned char *)p + off) = v; }

static unsigned char def_patrol[0x60], def_seek[0x60];
static void make_def(unsigned char *d, const char *name) {
    unsigned char *s = d + AD_SCRIPT_NAME;
    strcpy((char *)s, name);
    *(size_t *)(s + 0x10) = strlen(name);
    *(size_t *)(s + 0x18) = 15;      /* SSO: inline text (names < 16 chars only in this fake) */
}

static void test_part_b(void) {
    setbuf(stdout, NULL);
    memset(&fake_owner, 0, sizeof(fake_owner));
    fake_owner.armed = 1; current = &fake_owner; owner_thread = GetCurrentThreadId();
    ac_tls_block = fake_tls;
    make_def(def_patrol, "order.fight.p");     /* 13 chars: SSO */
    make_def(def_seek, "move.seek");
    static unsigned char entity[0x100], ctx_patrol[0x300], ctx_seek[0x300], es_patrol[0x60], es_seek[0x60];
    static unsigned char act_wait[0x30], act_move[0x30], queue[2][AC_Q_ENTRY], callstack[0x100];
    static uintptr_t prog_vec[3];
    static const void *prog[2];
    set_u64(entity, AD_COMP_ID, 0x7001);
    *(int32_t *)(entity + AC_ENT_ATTENTION) = 3;
    for (int k = 0; k < 2; ++k) {
        unsigned char *c = k ? ctx_seek : ctx_patrol, *es = k ? es_seek : es_patrol;
        set_ptr(c, AD_C_ENTITY, entity);
        set_ptr(c, AD_C_SCRIPT, k ? def_seek : def_patrol);
        set_ptr(c, AD_C_EXEC, es);
        set_f64(c, AD_C_WAKETIME, -1.0);
        set_u64(c, AC_C_ABORT, (unsigned long long)-1);
        set_u32(es, AC_E_CTXVAL, 0x66);
        set_ptr(es, AC_E_CTXVAL + 8, c + AC_CTXVAL_BIAS);
    }
    set_ptr(es_seek, AC_E_NEXT, es_patrol);    /* seek was called by patrol */
    set_u32(act_wait, AC_A_KEYWORD, 2558); set_u32(act_wait, AC_A_LINE, 509);
    set_u32(act_move, AC_A_KEYWORD, 2488); set_u32(act_move, AC_A_LINE, 187);
    prog[0] = act_wait; prog[1] = act_move;
    prog_vec[0] = (uintptr_t)&prog[0]; prog_vec[1] = (uintptr_t)&prog[2]; prog_vec[2] = prog_vec[1];
    set_ptr(es_patrol, AC_E_PROGRAM, prog_vec);
    set_ptr(callstack, AD_S_CURRENT, ctx_seek);

    sink_len = 0; sink_fail = 0;
    ac_begin(&ac, "order.fight.p", 4, mem_sink, NULL);
    ac_obs.run_begin = ac_run_begin; ac_obs.run_end = ac_run_end;
    ac_obs.event_begin = ac_event_begin; ac_obs.event_end = ac_event_end;

    /* a run of patrol that yields on the wait at line 509, timed wake 101.5 */
    set_u64(fake_tls_block, AD_TLS_RNG, 0x1111);
    set_f64(fake_tls_block, AD_TLS_GAME_TIME, 100.0);
    set_u64(es_patrol, AC_E_PC, 7);
    ac_run_begin(ctx_patrol, es_patrol, 0x20, 0, NULL);
    set_u64(fake_tls_block, AD_TLS_RNG, 0x2222);
    set_u64(es_patrol, AC_E_PC, 9);
    es_patrol[AC_E_YIELD] = 1;
    set_u64(es_patrol, AC_E_BLOCK, 0);
    set_f64(ctx_patrol, AD_C_WAKETIME, 101.5);
    ac_run_end(ctx_patrol, es_patrol, 0, NULL);

    /* a run of an untracked entity's script is ignored */
    static unsigned char other_ent[0x100], other_ctx[0x300];
    set_u64(other_ent, AD_COMP_ID, 0x7002);
    set_ptr(other_ctx, AD_C_ENTITY, other_ent);
    set_ptr(other_ctx, AD_C_SCRIPT, def_seek);
    ac_run_begin(other_ctx, es_seek, 1, 0, NULL);
    ac_run_end(other_ctx, es_seek, 0, NULL);

    /* a global delivery: the call-stack context sees an event; patrol's handler 9 matches (queue grows) */
    set_u32(callstack, AC_H_RECORD, 1340);                       /* event_object_target_invalid */
    set_u32(callstack, AC_H_RECORD + 0x08, 10); set_u64(callstack, AC_H_RECORD + 0x10, 0x7001);
    set_ptr(ctx_patrol, AC_C_QUEUE, queue[0]); set_ptr(ctx_patrol, AC_C_QUEUE + 8, queue[0]);
    set_ptr(ctx_seek, AC_C_QUEUE, queue[1]); set_ptr(ctx_seek, AC_C_QUEUE + 8, queue[1]);
    set_u64(fake_tls_block, AD_TLS_RNG, 0x3333);
    ac_event_begin(callstack, AD_V_STACK, NULL);
    set_u64(queue[0], 0, 9);
    set_ptr(ctx_patrol, AC_C_QUEUE + 8, queue[0] + AC_Q_ENTRY);
    set_u32(ctx_patrol, AD_C_MASK, 0x40);
    ac_event_end(callstack, AD_V_STACK, NULL);

    /* the interrupt run of that handler, then a blocking delivery to seek (flag 4 set, one draw) */
    set_f64(fake_tls_block, AD_TLS_GAME_TIME, 100.25);
    set_u64(fake_tls_block, AD_TLS_RNG, 0x4444);
    ac_run_begin(ctx_patrol, es_patrol, 0x40, 1, NULL);
    set_u64(fake_tls_block, AD_TLS_RNG, 0x5555);
    set_u64(ctx_patrol, AC_C_ABORT, 2);
    ac_run_end(ctx_patrol, es_patrol, 1, NULL);
    set_u32(ctx_seek, AC_H_RECORD, 1286);                        /* event_gravidar_has_scanned */
    set_u32(ctx_seek, AC_H_RECORD + 0x08, 10); set_u64(ctx_seek, AC_H_RECORD + 0x10, 0x7001);
    set_u64(fake_tls_block, AD_TLS_RNG, 0x6666);
    ac_event_begin(ctx_seek, AD_V_CTX, NULL);
    set_u64(fake_tls_block, AD_TLS_RNG, 0x7777);
    set_u32(ctx_seek, AD_C_MASK, 4);
    ac_event_end(ctx_seek, AD_V_CTX, NULL);
    ac_end(&ac);

    char l[AC_LINE_BYTES];
    assert(line_at(1, l, sizeof(l)) && !strcmp(l, "track\tc:0x7001\torder.fight.p\tslot=i:0"));
    assert(line_at(2, l, sizeof(l)));
    assert(starts(l, "run\tc:0x7001\torder.fight.p\tt=d:4059000000000000\tmask=x:20\trng=x:0000000000001111\tpc=i:7\tatt=i:3\tctx=x:"));
    assert(line_at(3, l, sizeof(l)));
    assert(!strcmp(l, "yield\tc:0x7001\torder.fight.p\trng=x:0000000000002222\tpc=i:9\tyielded=b:1\tkw=i:2558\tline=i:509\tuntil=d:4059600000000000"));
    assert(line_at(4, l, sizeof(l)));
    assert(starts(l, "ev\tc:0x7001\tkw:1340\tpath=k:global\tt=d:4059000000000000\trng=x:0000000000003333\trng_after=x:0000000000003333\tobject=c:0x7001\tparam=n\tparam2=n\tparam3=n\tmask=x:0\tmask_after=x:0\tmatched=l:[s:order.fight.p/9]"));
    assert(line_at(5, l, sizeof(l)));
    assert(starts(l, "irun\tc:0x7001\torder.fight.p\tt=d:4059100000000000\tmask=x:40\trng=x:0000000000004444\tpc=i:9\tqueue=l:[i:9]\tctx=x:"));
    assert(line_at(6, l, sizeof(l)) && !strcmp(l, "irend\tc:0x7001\torder.fight.p\trng=x:0000000000005555\tpc=i:9\tabortidx=i:2"));
    assert(line_at(7, l, sizeof(l)));
    assert(!strcmp(l, "ev\tc:0x7001\tkw:1286\tpath=k:block\tt=d:4059100000000000\trng=x:0000000000006666\trng_after=x:0000000000007777\tobject=c:0x7001\tparam=n\tparam2=n\tparam3=n\tmask=x:0\tmask_after=x:4\tmatched=l:[]"));
    assert(line_at(8, l, sizeof(l)) && starts(l, "# end"));
    assert(ac.runs == 1 && ac.iruns == 1 && ac.events == 2 && ac.n_ents == 1);
    printf("PASS: aicapture observer records runs, yields, interrupt runs and global/blocking deliveries\n");
}
#endif

int main(void) {
    test_part_a();
#ifdef AC_TEST_ENGINE
    test_part_b();
#endif
    return 0;
}
