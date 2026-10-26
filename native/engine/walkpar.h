/* Component-walk parallelism: stage 0 census and stage 1 single-threaded deferred replay.
 *
 * Included by fixedtick.c after walkprof.h. This header is the *engine*: the per-frame census
 * counters, the stage-1 post/cancel buffer (with the source UniverseID stored per buffered post),
 * tree-order replay, and the normal-vs-replay listener comparison. All of it is pure data+logic and
 * is exercised offline by test_walkpar.c with synthetic structures in the style of test_fixedtick.c.
 *
 * It does NOT patch the running process. The census and replay are driven by five engine entry
 * points that an in-process layer calls once the native lane is available:
 *   - wp_walk_begin()/wp_walk_end()  bracket the galaxy walk (the 0x9fdf95 call-site wrapper);
 *   - wp_sector_enter()/wp_sector_exit()  set the current-sector key and time Sector::Move;
 *   - wp_post()    PostEvent 0x99a3f0 entry detour: returns 1 when the post was buffered;
 *   - wp_cancel()  CancelPendingEvents 0x99a870 entry detour: returns 1 when handled against the buffer;
 *   - wp_deliver_enter()/exit()/listener()  DeliverEvent 0x99c470 wrapper for the census and the compare log.
 * The detours and the call-site wrapper are build-pinned code patches; they are specified in
 * reports/REWRITE-WALK.md and their target bytes are pinned in signatures.py (x4_walkpar_checks).
 * walkpar_arm() only verifies those signatures and reports readiness; it never writes engine code.
 * Any verification miss fails closed: the engine stays in pass-through and replay is disabled.
 *
 * AddTempZoneEvent (type 0xa) is never buffered: wp_post returns 0 for it so it is delivered inline
 * and later FindZoneAt calls in the same walk see the new zone (design §1.3). */

#ifndef X4LAN_WALKPAR_H
#define X4LAN_WALKPAR_H

#include "wpdetour.h"   /* build-pinned, crash-safe entry-detour installer */

enum {
    WP_OFF = 0,     /* not selected */
    WP_CENSUS = 1,  /* stage 0: counters only, never buffers */
    WP_REPLAY = 2,  /* stage 1: buffer posts/cancels in the walk, replay after it */
};
enum { WP_READY_IDLE = 0, WP_READY_OK = 1, WP_READY_REFUSED = 2 };

/* Event type ids read from each event's vtable slot 1 (mov eax,imm; ret), build 23660954. */
#define WP_TYPE_ADD_TEMP_ZONE 0x0a
#define WP_TYPE_ATTACKED      0x1e
#define WP_TYPE_CHANGED_ATTN  0x46
#define WP_TYPE_TEMP_ZONE_ADD 0x1db
#define WP_TYPE_WARP          0x21f
/* CancelPendingEvents 0x99a870 (RE label parallel-walk): minTime <= -1 cancels all events of the
   type; the engine's own comparisons use the -0.9999 threshold for that sentinel. */
#define WP_CANCEL_ALL (-0.9999)

#define WP_OP_MAX   16384   /* buffered posts+cancels per frame; overflow fails closed */
#define WP_CMP_MAX  16384   /* listener-visible records per compare log */
#define WP_TASK_MAX 256     /* tree-order tasks (sectors) tracked for per-task balance */
#define WP_TYPE_BUCKETS 64  /* post histogram by (type & 63) plus named columns */

enum { WP_OP_POST = 1, WP_OP_CANCEL = 2 };
typedef struct {
    int kind, type, task, imm, dead;
    uint64_t src_id;
    void *event;
    double time;        /* post time, or minTime for a cancel */
    uint32_t seq;
} wp_op;

/* One listener-visible effect, for the normal-vs-replay comparison. A cue activation is recorded as
   a record with type WP_REC_CUE and listener_id = the cue id. */
#define WP_REC_CUE 0x7ffffffe
typedef struct {
    int task, type;
    uint32_t seq;
    uint64_t src_id, listener_id, listener_rtti, payload;
} wp_cmp_rec;

typedef struct {
    uint64_t frames;
    /* time, nanoseconds summed over the window (QPC ticks converted at fold) */
    double sector_ms, sector_max_ms, deliver_ms;
    /* event traffic inside the walk */
    uint64_t posts, posts_imm, posts_sched, posts_temp_zone_inline, posts_rootlevel;
    uint64_t cancels, send_immediate, flush_nonempty;
    uint64_t deliver_calls, deliver_listeners;
    uint64_t xsector_post, xsector_listener, xsector_connect;
    /* creation / global-path candidates */
    uint64_t creates, effects, temp_zones, assign_ids, hits;
    uint64_t releases_in_walk, cue_runactions, exec_runs, request_wakes;
    /* stage 1 health */
    uint64_t buffered, replayed, replay_dropped, cancels_applied, degraded_frames;
    /* post histogram by type bucket */
    uint64_t type_hist[WP_TYPE_BUCKETS];
} wp_window;

/* Injected resolvers. Native sets them at arm; offline tests set them to stubs. When a resolver is
   null the engine behaves conservatively (no buffering / no cross-sector attribution). */
static uint64_t (*wp_sector_resolver)(void *component);           /* +0x70 walk to U::Sector id, 0 if none */
static void *(*wp_registry_find)(uint64_t id);                    /* ComponentRegistry::Find + cast, null if gone */
static void (*wp_deliver_post)(void *src, void *event, double t, int imm); /* original PostEvent */
static void (*wp_deliver_cancel)(void *src, int type, double minTime);     /* original CancelPendingEvents */

static int wp_mode;
static int wp_ready;
static int wp_phase;          /* 1 while the walk runs AND mode is replay (buffering active) */
static int wp_in_walk;        /* 1 between wp_walk_begin/end (census + replay) */
static int wp_replay_abort;   /* replay produced a degraded frame: disarm at the next safe point */
static DWORD wp_thread;       /* the walk (MoveWorker) thread, 0 until wp_walk_begin runs */
static int64_t wp_qpc_hz_cache;

/* current-sector key for cross-sector attribution and per-task bucketing (single walk thread) */
static uint64_t wp_cur_sector;
static int wp_cur_task = -1;
static int wp_sector_depth;
static int64_t wp_sector_qpc;         /* outermost Sector::Move start */
static int64_t wp_deliver_qpc;        /* outermost in-sector DeliverEvent start */
static int wp_deliver_depth;
static int wp_task_count;
static uint64_t wp_task_id[WP_TASK_MAX];
static double wp_task_ms[WP_TASK_MAX]; /* per-task Sector::Move ms this frame */

static wp_op wp_buf[WP_OP_MAX];
static size_t wp_count;
static uint32_t wp_seq_next;

/* two compare logs: A = normal delivery during the walk, B = delivery during replay */
static wp_cmp_rec wp_logA[WP_CMP_MAX], wp_logB[WP_CMP_MAX];
static size_t wp_logA_n, wp_logB_n;
static wp_cmp_rec *wp_log_active;      /* points at A or B while recording */
static size_t *wp_log_active_n;
static int wp_log_overflow;            /* a log hit WP_CMP_MAX: comparison is not trustworthy */
static uint64_t wp_task_hash[WP_TASK_MAX]; /* per-task running state hash of the active log */
#define WP_CMP_NODATA (-2)             /* wp_compare: a log was empty or overflowed */

static wp_window wp_frame;             /* reset each walk */
static wp_window wp_win;               /* accumulated across the report window */
static uint64_t wp_frame_sector_max_ticks;

/* MD cue activations compared as an order-independent multiset (MD is universe-level, not per task):
   index 0 = normal run, 1 = replay run. */
static uint64_t wp_cue_xor[2], wp_cue_cnt[2];

/* ---- helpers ---------------------------------------------------------------------------------- */

static int wp_on_thread(void) { return wp_thread && GetCurrentThreadId() == wp_thread; }

static uint64_t wp_fnv(uint64_t h, uint64_t x) {
    h ^= x;
    h *= 0x100000001b3ull;
    return h;
}

static int wp_event_type(void *event) {
    void **vt = *(void ***)event;            /* event vtable */
    int (*slot1)(void *) = (int (*)(void *))vt[1];
    return slot1(event);                     /* mov eax, <type>; ret */
}

static uint64_t wp_id_of(void *component) {
    return *(uint64_t *)((char *)component + 8); /* UniverseID at +8 (see PostEventDeferred) */
}

static int wp_task_index(uint64_t sector_id) {
    for (int i = 0; i < wp_task_count; ++i)
        if (wp_task_id[i] == sector_id) return i;
    if (wp_task_count < WP_TASK_MAX) {
        wp_task_id[wp_task_count] = sector_id;
        return wp_task_count++;
    }
    return -1;
}

/* Census is active inside the galaxy walk (in_walk, set by the dispatcher) or inside an explicit
   sub-scope (cur_sector, set by wp_sector_enter or the ai-driver scope). Without a cur_sector
   bracket (the native walk — no Sector::Move body patch, see REWRITE-WALK.md) the per-op sector
   and task come from the source/listener resolver. */
static int wp_active(void) { return wp_in_walk || wp_cur_sector != 0; }
static uint64_t wp_ref_sector(void *comp) {
    if (wp_cur_sector) return wp_cur_sector;
    return wp_sector_resolver ? wp_sector_resolver(comp) : 0;
}
static int wp_ref_task(void *comp) {
    if (wp_cur_task >= 0) return wp_cur_task;
    uint64_t s = wp_sector_resolver ? wp_sector_resolver(comp) : 0;
    return s ? wp_task_index(s) : -1;
}

/* ---- frame / walk lifecycle ------------------------------------------------------------------- */

/* Per-frame reset of the census and buffer. The compare logs are reset per run (not here), so a
   normal run fills A and a later replay run fills B, and the two survive for wp_compare. */
static void wp_frame_reset(void) {
    memset(&wp_frame, 0, sizeof(wp_frame));
    wp_count = 0;
    wp_task_count = 0;
    wp_seq_next = 0;
    wp_cur_sector = 0;
    wp_cur_task = -1;
    wp_sector_depth = 0;
    wp_deliver_depth = 0;
    wp_frame_sector_max_ticks = 0;
    for (int i = 0; i < WP_TASK_MAX; ++i) wp_task_ms[i] = 0.0;
}

/* Start of the galaxy walk (the 0x9fdf95 wrapper). Records the walk thread and turns buffering on.
   A replay run records listener deliveries into B, every other run into A. */
static void wp_walk_begin(void) {
    if (wp_mode == WP_OFF) return;
    wp_thread = GetCurrentThreadId();
    wp_frame_reset();
    wp_phase = (wp_mode == WP_REPLAY);
    wp_in_walk = 1;
    if (wp_mode == WP_REPLAY) { wp_log_active = wp_logB; wp_log_active_n = &wp_logB_n; }
    else { wp_log_active = wp_logA; wp_log_active_n = &wp_logA_n; }
    *wp_log_active_n = 0;
    wp_log_overflow = 0;
    wp_cue_xor[wp_mode == WP_REPLAY] = 0;
    wp_cue_cnt[wp_mode == WP_REPLAY] = 0;
    for (int i = 0; i < WP_TASK_MAX; ++i) wp_task_hash[i] = 0xcbf29ce484222325ull;
}

static void wp_replay(void);

/* End of the galaxy walk: replay the buffer (stage 1) before returning to 0x1092e80, then fold. */
static void wp_walk_end(void) {
    if (wp_mode == WP_OFF) return;
    if (wp_phase) { wp_phase = 0; wp_replay(); }
    /* review #2: replay must not silently run degraded; ask for disarm at the next safe point */
    if (wp_mode == WP_REPLAY && wp_frame.degraded_frames) wp_replay_abort = 1;
    /* fold frame -> window */
    ++wp_win.frames;
    wp_win.sector_ms += wp_frame.sector_ms;
    wp_win.deliver_ms += wp_frame.deliver_ms;
    if (wp_qpc_hz_cache > 0) {
        double mx = (double)wp_frame_sector_max_ticks * 1000.0 / (double)wp_qpc_hz_cache;
        wp_win.sector_max_ms += mx;
    }
#define WP_FOLD(f) wp_win.f += wp_frame.f
    WP_FOLD(posts); WP_FOLD(posts_imm); WP_FOLD(posts_sched); WP_FOLD(posts_temp_zone_inline);
    WP_FOLD(posts_rootlevel);
    WP_FOLD(cancels); WP_FOLD(send_immediate); WP_FOLD(flush_nonempty);
    WP_FOLD(deliver_calls); WP_FOLD(deliver_listeners);
    WP_FOLD(xsector_post); WP_FOLD(xsector_listener); WP_FOLD(xsector_connect);
    WP_FOLD(creates); WP_FOLD(effects); WP_FOLD(temp_zones); WP_FOLD(assign_ids); WP_FOLD(hits);
    WP_FOLD(releases_in_walk); WP_FOLD(cue_runactions); WP_FOLD(exec_runs); WP_FOLD(request_wakes);
    WP_FOLD(buffered); WP_FOLD(replayed); WP_FOLD(replay_dropped); WP_FOLD(cancels_applied);
    WP_FOLD(degraded_frames);
#undef WP_FOLD
    for (int i = 0; i < WP_TYPE_BUCKETS; ++i) wp_win.type_hist[i] += wp_frame.type_hist[i];
    wp_in_walk = 0;
}

/* ---- sector bracket (chained from Sector::Move) ----------------------------------------------- */

static void wp_sector_enter(void *sector) {
    if (wp_mode == WP_OFF || !wp_on_thread()) return;
    if (wp_sector_depth++ == 0) {
        LARGE_INTEGER a;
        wp_cur_sector = wp_id_of(sector);
        wp_cur_task = wp_task_index(wp_cur_sector);
        if (qpc_read(&a)) wp_sector_qpc = a.QuadPart;
    }
}

static void wp_sector_exit(void *sector) {
    (void)sector;
    if (wp_mode == WP_OFF || !wp_on_thread() || wp_sector_depth == 0) return;
    if (--wp_sector_depth == 0) {
        LARGE_INTEGER b;
        if (wp_sector_qpc && qpc_read(&b) && wp_qpc_hz_cache > 0) {
            int64_t ticks = b.QuadPart - wp_sector_qpc;
            if (ticks > 0) {
                double ms = (double)ticks * 1000.0 / (double)wp_qpc_hz_cache;
                wp_frame.sector_ms += ms;
                if (wp_cur_task >= 0 && wp_cur_task < WP_TASK_MAX) wp_task_ms[wp_cur_task] += ms;
                if ((uint64_t)ticks > wp_frame_sector_max_ticks) wp_frame_sector_max_ticks = (uint64_t)ticks;
            }
        }
        wp_cur_sector = 0;
        wp_cur_task = -1;
    }
}

/* ---- PostEvent / CancelPendingEvents entry detours -------------------------------------------- */

static void wp_record(int task, int type, uint32_t seq, uint64_t src_id, uint64_t listener_id,
                      uint64_t listener_rtti, uint64_t payload) {
    if (!wp_log_active) return;
    if (*wp_log_active_n >= WP_CMP_MAX) { wp_log_overflow = 1; return; }
    wp_cmp_rec *r = &wp_log_active[(*wp_log_active_n)++];
    r->task = task; r->type = type; r->seq = seq; r->src_id = src_id;
    r->listener_id = listener_id; r->listener_rtti = listener_rtti; r->payload = payload;
    if (task >= 0 && task < WP_TASK_MAX) {
        uint64_t h = wp_task_hash[task];
        h = wp_fnv(h, (uint64_t)(uint32_t)type);
        h = wp_fnv(h, src_id);
        h = wp_fnv(h, listener_id);
        h = wp_fnv(h, payload);
        wp_task_hash[task] = h;
    }
}

/* Returns 1 when the post was buffered (the detour must then skip the original), 0 to pass through. */
static int wp_post(void *src, void *event, double time, int imm) {
    if (wp_mode == WP_OFF || !wp_on_thread() || !wp_active()) return 0;
    int type = wp_event_type(event);
    int task = wp_ref_task(src);
    ++wp_frame.posts;
    if (imm) ++wp_frame.posts_imm; else ++wp_frame.posts_sched;
    ++wp_frame.type_hist[type & (WP_TYPE_BUCKETS - 1)];
    if (task < 0) ++wp_frame.posts_rootlevel;      /* cluster/highway-level (no sector) */
    else if (wp_cur_sector && wp_sector_resolver) {  /* source outside the explicit scope (rare) */
        uint64_t s = wp_sector_resolver(src);
        if (s && s != wp_cur_sector) ++wp_frame.xsector_post;
    }
    if (!wp_phase) return 0;                        /* census-only */
    if (type == WP_TYPE_ADD_TEMP_ZONE) {            /* must stay synchronous (design §1.3) */
        ++wp_frame.posts_temp_zone_inline;
        return 0;
    }
    if (wp_count >= WP_OP_MAX) {                     /* fail closed: deliver inline, flag the frame */
        ++wp_frame.degraded_frames;
        return 0;
    }
    wp_op *op = &wp_buf[wp_count++];
    op->kind = WP_OP_POST; op->type = type; op->task = task; op->imm = imm;
    op->src_id = wp_id_of(src); op->event = event; op->time = time;
    op->seq = wp_seq_next++; op->dead = 0;
    ++wp_frame.buffered;
    return 1;
}

/* Returns 1 when the cancel was applied to the buffer (detour skips the original), 0 to pass through. */
static int wp_cancel(void *src, int type, double minTime) {
    if (wp_mode == WP_OFF || !wp_on_thread() || !wp_active()) return 0;
    ++wp_frame.cancels;
    if (!wp_phase) return 0;
    uint64_t sid = wp_id_of(src);
    /* Kill only this task's buffered SCHEDULED posts (imm==0): an immediate post was already
       delivered before the cancel in normal mode, so replay must still deliver it (review #2).
       Free the killed event, as the engine's CancelPendingEvents deletes the queued events. */
    for (size_t i = 0; i < wp_count; ++i) {
        wp_op *o = &wp_buf[i];
        if (o->kind == WP_OP_POST && !o->dead && o->imm == 0 && o->src_id == sid && o->type == type &&
            (minTime <= WP_CANCEL_ALL || o->time >= minTime)) {
            if (o->event) {
                void **vt = *(void ***)o->event;
                ((void (*)(void *, int))vt[0])(o->event, 1);
                o->event = NULL;
            }
            o->dead = 1;
        }
    }
    if (wp_count >= WP_OP_MAX) {                    /* review #3: pass through so the schedule sees it */
        ++wp_frame.degraded_frames;
        return 0;
    }
    wp_op *op = &wp_buf[wp_count++];
    op->kind = WP_OP_CANCEL; op->type = type; op->task = wp_ref_task(src); op->imm = 0;
    op->src_id = sid; op->event = NULL; op->time = minTime;
    op->seq = wp_seq_next++; op->dead = 0;
    return 1;
}

/* ---- DeliverEvent census + compare log -------------------------------------------------------- */

static void wp_deliver_enter(void *src, void *event) {
    (void)src; (void)event;
    if (wp_mode == WP_OFF || !wp_on_thread() || !wp_active()) return;
    if (wp_deliver_depth++ == 0) {
        LARGE_INTEGER a;
        if (qpc_read(&a)) wp_deliver_qpc = a.QuadPart; else wp_deliver_qpc = 0;
        ++wp_frame.deliver_calls;
    }
}

static void wp_deliver_exit(void *src, void *event) {
    (void)src; (void)event;
    if (wp_mode == WP_OFF || !wp_on_thread() || wp_deliver_depth == 0) return;
    if (--wp_deliver_depth == 0 && wp_deliver_qpc && wp_qpc_hz_cache > 0) {
        LARGE_INTEGER b;
        if (qpc_read(&b)) {
            int64_t ticks = b.QuadPart - wp_deliver_qpc;
            if (ticks > 0) wp_frame.deliver_ms += (double)ticks * 1000.0 / (double)wp_qpc_hz_cache;
        }
    }
}

/* One dispatched (event -> listener) pair, for the census histogram and the compare log. */
static void wp_deliver_listener(void *src, void *event, void *listener, uint64_t payload) {
    if (wp_mode == WP_OFF || !wp_on_thread()) return;
    int type = wp_event_type(event);
    uint64_t lid = wp_id_of(listener);
    uint64_t rtti = (uint64_t)(*(void ***)listener)[0]; /* vtable ptr stands in for the listener class */
    ++wp_frame.deliver_listeners;
    /* key the record on the listener's sector so the invariant is "each listener sees the same
       ordered sequence"; cross-sector = listener sector differs from the source's sector */
    uint64_t ls = wp_sector_resolver ? wp_sector_resolver(listener) : 0;
    int task = ls ? wp_task_index(ls) : wp_ref_task(src);
    uint64_t ref = wp_ref_sector(src);
    if (ls && ref && ls != ref) ++wp_frame.xsector_listener;
    wp_record(task, type, 0, wp_id_of(src), lid, rtti, payload);
}

/* ---- creation / global-path census counters --------------------------------------------------- */

static void wp_note_connect(void *child, void *new_parent) {
    if (wp_mode == WP_OFF || !wp_on_thread() || !wp_active()) return;
    if (wp_sector_resolver) {
        uint64_t ps = wp_sector_resolver(new_parent ? new_parent : child);
        uint64_t cs = wp_ref_sector(child);
        if (ps && cs && ps != cs) ++wp_frame.xsector_connect;
    }
}
static void wp_note_create(void)   { if (wp_on_thread() && wp_active()) ++wp_frame.creates; }
static void wp_note_effect(void)   { if (wp_on_thread() && wp_active()) ++wp_frame.effects; }
static void wp_note_tempzone(void) { if (wp_on_thread() && wp_active()) ++wp_frame.temp_zones; }
static void wp_note_assignid(void) { if (wp_on_thread() && wp_active()) ++wp_frame.assign_ids; }
static void wp_note_hit(void)      { if (wp_on_thread() && wp_active()) ++wp_frame.hits; }
static void wp_note_release(void)  { if (wp_on_thread() && wp_phase) ++wp_frame.releases_in_walk; }
static void wp_note_cue(uint64_t cue_id) {
    if (wp_mode == WP_OFF || !wp_on_thread()) return;
    ++wp_frame.cue_runactions;
    int idx = wp_mode == WP_REPLAY;     /* MD compared as a multiset (order-independent) */
    wp_cue_xor[idx] ^= cue_id;
    ++wp_cue_cnt[idx];
}
static void wp_note_exec(void)     { if (wp_on_thread() && wp_active()) ++wp_frame.exec_runs; }
static void wp_note_wake(void)     { if (wp_on_thread() && wp_active()) ++wp_frame.request_wakes; }
static void wp_note_send_immediate(void) { if (wp_on_thread() && wp_active()) ++wp_frame.send_immediate; }
static void wp_note_flush_nonempty(void) { if (wp_on_thread() && wp_active()) ++wp_frame.flush_nonempty; }

/* ---- replay ----------------------------------------------------------------------------------- */

/* Replays the buffered ops in tree order (phase is off, so nested immediate posts deliver inline,
   recording into the active log). Deliveries here record into B for the comparison. */
/* Replay one buffered op through the original PostEvent / CancelPendingEvents (phase is off, so a
   nested immediate post from a handler delivers inline and records into the active log). Marks the
   op consumed so an ordered replay cannot run it twice. */
static void wp_replay_one(wp_op *o) {
    if (o->dead) return;
    o->dead = 1;
    void *src = wp_registry_find ? wp_registry_find(o->src_id) : NULL;
    if (o->kind == WP_OP_POST) {
        if (!src) {
            if (o->event) {
                void **vt = *(void ***)o->event;
                void (*free0)(void *, int) = (void (*)(void *, int))vt[0];
                free0(o->event, 1);
            }
            ++wp_frame.replay_dropped;
            return;
        }
        if (wp_deliver_post) wp_deliver_post(src, o->event, o->time, o->imm);
        ++wp_frame.replayed;
    } else { /* WP_OP_CANCEL */
        if (src && wp_deliver_cancel) wp_deliver_cancel(src, o->type, o->time);
        ++wp_frame.cancels_applied;
    }
}

/* Walk replay: all ops in post order (= tree order), before 0x1092e80. */
static void wp_replay(void) {
    for (size_t i = 0; i < wp_count; ++i) wp_replay_one(&wp_buf[i]);
    wp_count = 0;
}

/* ---- shared buffer for the ai-driver AI phase (one buffer, scope API; design PW H3-H5) ---------
   The ai-driver opens a scope per bucket during the AI phase, posts/cancels route to this same
   buffer, and it replays per task in its own bucket order at the round barrier. */

static void wp_scope_begin(uint64_t task_key) {
    if (wp_mode == WP_OFF || !wp_on_thread()) return;
    wp_cur_sector = task_key;                     /* 0 is reserved for "no scope" */
    wp_cur_task = wp_task_index(task_key);
    wp_phase = (wp_mode == WP_REPLAY);
}

static void wp_scope_end(void) {
    if (wp_mode == WP_OFF || !wp_on_thread()) return;
    wp_cur_sector = 0;
    wp_cur_task = -1;
    wp_phase = 0;
}

/* Replay grouped by task, tasks in the caller's order (ties kept in post order); ops whose task is
   not listed are replayed afterwards in post order so nothing is dropped. */
static void wp_replay_tasks(const uint64_t *order, int n) {
    for (int k = 0; k < n; ++k) {
        int t = wp_task_index(order[k]);
        for (size_t i = 0; i < wp_count; ++i)
            if (!wp_buf[i].dead && wp_buf[i].task == t) wp_replay_one(&wp_buf[i]);
    }
    for (size_t i = 0; i < wp_count; ++i)
        if (!wp_buf[i].dead) wp_replay_one(&wp_buf[i]);
    wp_count = 0;
}

/* Census readout of the buffer (ai-driver counts foreign targets): returns the op kind, or 0. */
static size_t wp_buffer_count(void) { return wp_count; }
static int wp_buffer_op(size_t i, uint64_t *src_id, int *task, int *type) {
    if (i >= wp_count) return 0;
    if (src_id) *src_id = wp_buf[i].src_id;
    if (task) *task = wp_buf[i].task;
    if (type) *type = wp_buf[i].type;
    return wp_buf[i].kind;
}

/* ---- normal-vs-replay comparison -------------------------------------------------------------- */

/* Compares the two logs per task. For each task the ordered sequence of records must be identical
   (same-sector delivery order is preserved); the cross-task interleaving is allowed to differ, so
   the comparison is per task. Returns 0 when equivalent, else packs (task<<16 | type_of_first_diff)
   into *detail and returns -1. */
static int wp_seen_before(const wp_cmp_rec *log, size_t upto, uint64_t id) {
    for (size_t i = 0; i < upto; ++i) if (log[i].listener_id == id) return 1;
    return 0;
}

/* Compare the subsequence each listener (by id) sees in A against the one it sees in B. */
static int wp_listener_cmp(uint64_t id, int *detail) {
    size_t ia = 0, ib = 0;
    for (;;) {
        while (ia < wp_logA_n && wp_logA[ia].listener_id != id) ++ia;
        while (ib < wp_logB_n && wp_logB[ib].listener_id != id) ++ib;
        int ea = ia >= wp_logA_n, eb = ib >= wp_logB_n;
        if (ea && eb) return 0;
        if (ea != eb) {                                 /* lengths differ */
            if (detail) *detail = (ea ? wp_logB[ib].type : wp_logA[ia].type) & 0xffff;
            return -1;
        }
        wp_cmp_rec *a = &wp_logA[ia], *b = &wp_logB[ib];
        if (a->type != b->type || a->src_id != b->src_id || a->payload != b->payload) {
            if (detail) *detail = a->type & 0xffff;     /* the baseline (normal) type at the diff */
            return -1;
        }
        ++ia; ++ib;
    }
}

static int wp_compare(int *detail) {
    if (wp_logA_n == 0 || wp_logB_n == 0 || wp_log_overflow) {
        if (detail) *detail = 0;
        return WP_CMP_NODATA;                 /* no per-listener data (e.g. no native listener hook) */
    }
    for (size_t i = 0; i < wp_logA_n; ++i) {
        uint64_t id = wp_logA[i].listener_id;
        if (!wp_seen_before(wp_logA, i, id) && wp_listener_cmp(id, detail)) return -1;
    }
    for (size_t i = 0; i < wp_logB_n; ++i) {            /* ids present only in B */
        uint64_t id = wp_logB[i].listener_id;
        if (!wp_seen_before(wp_logB, i, id) && !wp_seen_before(wp_logA, wp_logA_n, id) &&
            wp_listener_cmp(id, detail)) return -1;
    }
    if (wp_cue_cnt[0] != wp_cue_cnt[1] || wp_cue_xor[0] != wp_cue_xor[1]) {
        if (detail) *detail = WP_REC_CUE & 0xffff;      /* MD cue-activation multiset differs */
        return -1;
    }
    return 0;
}

/* ---- window readout --------------------------------------------------------------------------- */

static double wp_ideal_speedup(void) {
    return wp_win.sector_max_ms > 0.0 ? wp_win.sector_ms / wp_win.sector_max_ms : 0.0;
}

static void wp_window_clear(void) { memset(&wp_win, 0, sizeof(wp_win)); }

/* ---- arm / verify (no code patch) ------------------------------------------------------------- */

/* Verifies the build-pinned bytes of the detour targets and the galaxy call site. On success the
   engine is READY for the native layer to install the detours; on any miss it fails closed. This
   function never writes engine code. Returns 1 when ready. */
static int walkpar_verify(void) {
#ifdef X4_HAVE_WALKPAR_SITES
    if (!verify_checks(x4_walkpar_checks, sizeof(x4_walkpar_checks) / sizeof(x4_walkpar_checks[0])))
        return 0;
    /* the galaxy call site must still be `call [rax+0x1510]` (ff 90 10 15 00 00) */
    if (!readable(image + WP_GALAXY_CALL_RVA, 6, 0)) return 0;
    {
        const unsigned char *p = image + WP_GALAXY_CALL_RVA;
        if (!(p[0] == 0xff && p[1] == 0x90 && p[2] == 0x10 && p[3] == 0x15 && p[4] == 0 && p[5] == 0))
            return 0;
    }
    return 1;
#else
    return 0;
#endif
}

/* Verify the pinned bytes and clear state. Does not enable the engine: wp_mode stays OFF until the
   native detour layer (or a test) calls walkpar_set_mode once the detours are installed. */
static void walkpar_arm(void) {
    wp_mode = WP_OFF;
    wp_phase = 0;
    wp_window_clear();
    wp_qpc_hz_cache = qpc_frequency;
    wp_ready = walkpar_verify() ? WP_READY_OK : WP_READY_REFUSED;
}

/* The native layer flips the engine on only after its detours are installed; fails closed if the
   signatures did not verify. WP_REPLAY is refused until the galaxy-call dispatcher is installed, so
   the replay is bracketed by the walk (review #1), never by the whole sim frame. */
static int walkpar_dispatcher_on(void);
static void walkpar_set_mode(int mode) {
    if (wp_ready != WP_READY_OK) { wp_mode = WP_OFF; return; }
    /* both modes are bracketed by the galaxy-call dispatcher; refuse until it is installed */
    if ((mode == WP_CENSUS || mode == WP_REPLAY) && !walkpar_dispatcher_on()) { wp_mode = WP_OFF; return; }
    wp_mode = (mode == WP_CENSUS || mode == WP_REPLAY) ? mode : WP_OFF;
}

static void walkpar_disarm(void) {
    wp_mode = WP_OFF;
    wp_phase = 0;
    wp_replay_abort = 0;
    wp_thread = 0;
    wp_count = 0;
    wp_ready = WP_READY_IDLE;
}

/* ABI surface for the native detour layer: the installed PostEvent / CancelPendingEvents /
   DeliverEvent / Sector::Move / galaxy-call detours call through this table. Taking the addresses
   here also keeps every entry point referenced in the DLL build (where no detours exist yet). */
typedef struct {
    void (*walk_begin)(void);
    void (*walk_end)(void);
    void (*sector_enter)(void *);
    void (*sector_exit)(void *);
    int (*post)(void *, void *, double, int);
    int (*cancel)(void *, int, double);
    void (*deliver_enter)(void *, void *);
    void (*deliver_exit)(void *, void *);
    void (*deliver_listener)(void *, void *, void *, uint64_t);
    void (*note_connect)(void *, void *);
    void (*note_create)(void);
    void (*note_effect)(void);
    void (*note_tempzone)(void);
    void (*note_assignid)(void);
    void (*note_hit)(void);
    void (*note_release)(void);
    void (*note_cue)(uint64_t);
    void (*note_exec)(void);
    void (*note_wake)(void);
    void (*note_send_immediate)(void);
    void (*note_flush_nonempty)(void);
    int (*compare)(int *);
    void (*set_mode)(int);
    /* ai-driver AI-phase surface (Q4): one buffer, scope + ordered replay + op readout */
    void (*scope_begin)(uint64_t);
    void (*scope_end)(void);
    void (*replay_tasks)(const uint64_t *, int);
    size_t (*buffer_count)(void);
    int (*buffer_op)(size_t, uint64_t *, int *, int *);
} wp_api;

static const wp_api WP_API = {
    wp_walk_begin, wp_walk_end, wp_sector_enter, wp_sector_exit, wp_post, wp_cancel,
    wp_deliver_enter, wp_deliver_exit, wp_deliver_listener, wp_note_connect, wp_note_create,
    wp_note_effect, wp_note_tempzone, wp_note_assignid, wp_note_hit, wp_note_release, wp_note_cue,
    wp_note_exec, wp_note_wake, wp_note_send_immediate, wp_note_flush_nonempty, wp_compare,
    walkpar_set_mode, wp_scope_begin, wp_scope_end, wp_replay_tasks, wp_buffer_count, wp_buffer_op,
};

static const wp_api *walkpar_api(void) { return &WP_API; }

/* ---- native arming: install the entry detours that feed the engine ---------------------------- */
static int wp_want;     /* requested mode, armed lazily each frame until the thread freeze takes */
#ifdef X4_HAVE_WALKPAR_SITES

/* One contiguous descriptor array (the installer freezes/patches them together). Census installs
   post + deliver; replay also installs cancel. The walk is bracketed by the galaxy-call dispatcher
   (wp_cs), NOT by a Sector::Move body patch: the attention hooks pin and re-verify a 738-byte range
   at 0x784230, so patching that entry trips their integrity check (HOOKS_DISABLED). Per-sector
   attribution comes from the source/listener resolver instead. */
enum { WPD_POST = 0, WPD_DELIVER, WPD_CANCEL, WPD_N };
static wp_detour wp_det[WPD_N] = {
    {0x99a3f0, 14, 3, NULL, 32, NULL, {0}, NULL, 0},
    {0x99c470, 16, -1, NULL, 32, NULL, {0}, NULL, 0},
    {0x99a870, 17, -1, NULL, 32, NULL, {0}, NULL, 0},
};
static int wp_det_n;    /* how many of wp_det are armed */

static void wp_d_post(void *src, void *event, double time, int imm) {
    if (wp_post(src, event, time, imm)) return;      /* buffered: skip the original */
    ((void (*)(void *, void *, double, int))wp_det[WPD_POST].tramp)(src, event, time, imm);
}
static void wp_d_cancel(void *src, int type, double minTime) {
    if (wp_cancel(src, type, minTime)) return;
    ((void (*)(void *, int, double))wp_det[WPD_CANCEL].tramp)(src, type, minTime);
}
static void wp_d_deliver(void *src, void *event, uint32_t t) {
    wp_deliver_enter(src, event);
    ((void (*)(void *, void *, uint32_t))wp_det[WPD_DELIVER].tramp)(src, event, t);
    wp_deliver_exit(src, event);
}

/* Galaxy walk dispatcher (stage 1): brackets the whole walk so the deferred replay runs right after
   it, before 0x1092e80. The real walk is the galaxy's own Move slot 674 (0x1510/8), which is
   walkprof's timing wrapper when that is installed. */
static wp_callsite wp_cs = {WP_GALAXY_CALL_RVA, {0}, NULL, 0};
static void wp_dispatch(void *galaxy) {
    wp_walk_begin();
    void **vt = *(void ***)galaxy;
    ((void (*)(void *))vt[674])(galaxy);
    wp_walk_end();
}
static int walkpar_dispatcher_on(void) { return wp_cs.installed; }

/* Install the entry detours and, on success, enable the engine. Fail closed: if the detours do not
   all install, the engine stays OFF. Returns 1 when armed; a transient 0 (a thread was inside a
   patch range) is the caller's retry. */
static int walkpar_arm_native(int mode) {
    if (wp_ready != WP_READY_OK || (mode != WP_CENSUS && mode != WP_REPLAY)) return 0;
    if (wp_det_n) return 1;                            /* already armed */
    wp_det[WPD_POST].detour = (void *)wp_d_post;       wp_det[WPD_POST].sha = x4_walkpar_checks[0].sha;
    wp_det[WPD_CANCEL].detour = (void *)wp_d_cancel;   wp_det[WPD_CANCEL].sha = x4_walkpar_checks[1].sha;
    wp_det[WPD_DELIVER].detour = (void *)wp_d_deliver; wp_det[WPD_DELIVER].sha = x4_walkpar_checks[2].sha;
    int n = (mode == WP_REPLAY) ? WPD_N : WPD_N - 1;   /* census: post+deliver; replay: +cancel */
    if (!wp_detour_install(wp_det, n)) return 0;
    /* both modes need the galaxy-call dispatcher to bracket the walk (census for an accurate
       in-walk window; replay so the deferred replay runs before 0x1092e80) */
    if (!wp_callsite_install(&wp_cs, (void *)wp_dispatch)) {
        wp_detour_uninstall(wp_det, n);                /* roll back the entry detours too */
        return 0;
    }
    wp_det_n = n;
    walkpar_set_mode(mode);
    return 1;
}

static void walkpar_disarm_native(void) {
    wp_want = 0;
    wp_callsite_uninstall(&wp_cs);
    if (!wp_det_n) return;
    wp_detour_uninstall(wp_det, wp_det_n);
    wp_det_n = 0;
}

/* Armed lazily once per frame until the thread freeze takes (a Rip clash on a hot function entry is
   transient); set by start(), cleared on disarm. A degraded replay frame disarms here (safe point). */
static void walkpar_tick(void) {
    if (wp_replay_abort) { walkpar_disarm_native(); walkpar_disarm(); return; }
    if (wp_want && !wp_det_n) walkpar_arm_native(wp_want);
}
#else
static int walkpar_arm_native(int mode) { (void)mode; return 0; }
static void walkpar_disarm_native(void) { wp_want = 0; }
static void walkpar_tick(void) {}
static int walkpar_dispatcher_on(void) { return 0; }
#endif /* X4_HAVE_WALKPAR_SITES */

#endif /* X4LAN_WALKPAR_H */
