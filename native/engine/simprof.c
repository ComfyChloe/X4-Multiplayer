/* Sampling profiler for one target thread (the X4 MoveWorker), included by fixedtick.c
   and by test_simprof.c. Read-only towards the target: it never writes engine memory.

   Suspended window (between SuspendThread and ResumeThread) runs only:
     GetThreadContext (syscall), QueryPerformanceCounter, reads of the target's TEB and stack,
     our own binary search over in-memory .pdata, and RtlVirtualUnwind.
   No heap, no locks, no loader calls, no CRT. RtlLookupFunctionEntry is deliberately NOT
   used: on a miss in the static tables it consults the dynamic function table list under
   an SRW lock (and older Windows versions take the inverted-function-table lock), which the
   suspended thread may hold. RtlVirtualUnwind is a pure function of (image base, function
   entry, context): it reads the unwind info in the image, the code bytes at Rip (epilogue
   detection) and the target stack, and follows chained unwind info without a lookup.
   Stack reads stay inside [leaf Rsp, StackBase): the leaf Rsp must be at or above the
   TEB's StackLimit, so every page there is committed (never a guard page).

   Modules usable for unwinding are fixed at start: the main image, this module, ntdll,
   kernelbase, kernel32 and the main image's static imports (which cannot unload); each
   holds a reference until stop. A frame in any other code (JIT code, drivers) is only
   recovered at the leaf, by a bounded stack scan for a return address that follows a call
   instruction in a listed module; frames after a scan are checked more strictly. */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif

#define SP_MAX_DEPTH 64
#define SP_MIN_DEPTH 8
#define SP_DEFAULT_DEPTH 32
#define SP_MIN_INTERVAL_US 500
#define SP_MAX_INTERVAL_US 10000
#define SP_DEFAULT_INTERVAL_US 1000
#define SP_MAX_MODULES 31            /* ids: module index << 27 | rva; index 31 = pseudo */
#define SP_RVA_BITS 27
#define SP_ID(m, rva) (((uint32_t)(m) << SP_RVA_BITS) | (uint32_t)(rva))
#define SP_PSEUDO (31u << SP_RVA_BITS)
#define SP_EXT_CODE (SP_PSEUDO | 1u)  /* code outside every listed module (JIT, drivers) */
#define SP_TRUNCATED (SP_PSEUDO | 2u)
#define SP_WAIT_ID (SP_PSEUDO | 0x100u)
#define SP_NO_ID 0xffffffffu
#define SP_RING 64
#define SP_FN_CAP (1u << 16)
#define SP_FOLD_CAP (1u << 14)
#define SP_SCAN_SLOTS 512            /* leaf scan covers at most 4 KB of target stack */
#define SP_TOP 20
#define SP_SLOTS 17
#define SP_FAIL_LIMIT 5

enum { SP_PH_FRAME = SP_SLOTS, SP_PH_IDLE, SP_PH_LOADING, SP_PH_LOST, SP_PH_LOST_OUT, SP_PHASES };
static const char *const sp_phase_names[SP_PHASES] = {
    "slot0", "slot1", "slot2", "slot3", "slot4", "slot5", "slot6", "slot7", "slot8", "slot9",
    "slot10", "slot11", "slot12", "slot13", "slot14", "slot15", "slot16",
    "frame", "idle", "loading", "lost", "lostout"};

/* Wait syscalls: a sample whose leaf Rip lies in one of these stubs is "waiting". */
static const char *const sp_wait_names[] = {
    "NtWaitForSingleObject", "NtWaitForMultipleObjects", "NtDelayExecution",
    "NtWaitForAlertByThreadId", "NtWaitForKeyedEvent", "NtRemoveIoCompletion",
    "NtRemoveIoCompletionEx", "NtSignalAndWaitForSingleObject", "NtYieldExecution"};
#define SP_WAITS (sizeof(sp_wait_names) / sizeof(sp_wait_names[0]))
#define SP_WAIT_STUB 0x20

enum { SP_F_TRUNC = 1, SP_F_SCANNED = 2, SP_F_UNKNOWN = 4, SP_F_RUN = 8, SP_F_LOADING = 16 };

typedef struct {
    uintptr_t base, end;
    const RUNTIME_FUNCTION *pdata;
    uint32_t count;
    HMODULE ref;
    char name[24];
} sp_module;

typedef struct {
    uint32_t suspend_ticks, walk_ticks;
    uint8_t depth, flags, loop_state, pad;
    uint64_t run_reg;              /* slot register as restored in the Run frame */
    uint64_t rip[SP_MAX_DEPTH];
} sp_raw;

typedef struct { uint32_t id, self, incl; } sp_fn;

typedef struct {
    uint32_t hash, count;
    uint8_t phase, depth, pad[2];
    uint32_t ids[SP_MAX_DEPTH + 1]; /* root -> leaf */
} sp_fold;

typedef struct {
    sp_raw ring[SP_RING];
    sp_fn fn[SP_FN_CAP];
    sp_fold folds[2][SP_FOLD_CAP];
} sp_tables;

typedef struct {
    uint64_t samples, phase_run[SP_PHASES], phase_wait[SP_PHASES];
    uint64_t truncated, scanned, failures, skipped, dropped_fn, dropped_fold, frames;
    uint64_t suspend_ticks, suspend_max, walk_ticks, fn_used, fold_used;
    int64_t start_qpc;
} sp_window;

/* Injectable for tests; the defaults are the real kernel32 functions. */
static DWORD (WINAPI *sp_suspend)(HANDLE) = SuspendThread;
static DWORD (WINAPI *sp_resume)(HANDLE) = ResumeThread;
static BOOL (WINAPI *sp_get_context)(HANDLE, CONTEXT *) = GetThreadContext;

static struct {
    volatile LONG stop;     /* sampler thread should exit */
    volatile LONG state;    /* 0 off, 1 running, 2 disabled by the sampler */
    const char *reason;
    char reason_buf[96];
    HANDLE thread, target, stop_event, timer;
    DWORD target_id;
    const NT_TIB *tib;
    uintptr_t stack_base;
    const volatile int *loading_flag;
    uint32_t run_id;
    uintptr_t run_begin;
    uintptr_t slot_return;  /* return address of Run's slot call; 0 = unknown */
    int slot_register;      /* nonvolatile register holding the slot index there */
    unsigned interval_us, depth;
    sp_module modules[SP_MAX_MODULES];
    unsigned module_count;
    uintptr_t wait_addr[SP_WAITS];
    sp_tables *tables;
    unsigned active_fold;
    unsigned ring_head;
    sp_window win;
    SRWLOCK lock;
    int64_t qpc_frequency;
    uint32_t rng;
    uint32_t slot_ids[SP_SLOTS];
    uintptr_t slot_cached[SP_SLOTS];
    /* Lua-thread-only fold drain state */
    int snapshot;          /* index of the table being drained, or -1 */
    unsigned snapshot_cursor, snapshot_left;
    int leaked;            /* the sampler did not exit in time: tables are never freed */
} sp = {.lock = SRWLOCK_INIT, .snapshot = -1};

/* Written by the target thread; read by the sampler. */
static volatile uintptr_t sp_slot_vfunc[SP_SLOTS];
static volatile LONG sp_loop_state; /* 0 before the first slot-0 bracket, 1 in loop, 2 after slot 15 */

/* ---------- module table and .pdata lookup (lock-free, read-only) ---------- */

static int sp_add_module(HMODULE handle, const char *name) {
    if (!handle) return 0;
    for (unsigned i = 0; i < sp.module_count; ++i)
        if (sp.modules[i].base == (uintptr_t)handle) return 1;
    if (sp.module_count >= SP_MAX_MODULES - 1) return 0; /* index 31 is reserved for pseudo ids */
    const unsigned char *b = (const unsigned char *)handle;
    const IMAGE_DOS_HEADER *dos = (const IMAGE_DOS_HEADER *)b;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0 || dos->e_lfanew > 4096) return 0;
    const IMAGE_NT_HEADERS64 *nt = (const IMAGE_NT_HEADERS64 *)(b + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return 0;
    uint32_t size = nt->OptionalHeader.SizeOfImage;
    if (size >= (1u << SP_RVA_BITS)) return 0;
    const IMAGE_DATA_DIRECTORY *d = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXCEPTION];
    if (!d->VirtualAddress || d->Size < sizeof(RUNTIME_FUNCTION) ||
        (uint64_t)d->VirtualAddress + d->Size > size) return 0;
    HMODULE ref = NULL;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS, (LPCWSTR)handle, &ref) || ref != handle) {
        if (ref) FreeLibrary(ref);
        return 0;
    }
    sp_module *m = &sp.modules[sp.module_count++];
    m->base = (uintptr_t)b;
    m->end = m->base + size;
    m->pdata = (const RUNTIME_FUNCTION *)(b + d->VirtualAddress);
    m->count = d->Size / sizeof(RUNTIME_FUNCTION);
    m->ref = ref;
    unsigned n = 0;
    for (const char *s = name; *s && *s != '.' && n + 1 < sizeof(m->name); ++s)
        m->name[n++] = (*s >= 'A' && *s <= 'Z') ? (char)(*s + 32) : *s;
    m->name[n] = 0;
    return 1;
}

static void sp_add_imports(void) {
    const unsigned char *b = (const unsigned char *)sp.modules[0].base;
    const IMAGE_NT_HEADERS64 *nt = (const IMAGE_NT_HEADERS64 *)(b + ((const IMAGE_DOS_HEADER *)b)->e_lfanew);
    const IMAGE_DATA_DIRECTORY *d = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!d->VirtualAddress) return;
    const IMAGE_IMPORT_DESCRIPTOR *imp = (const IMAGE_IMPORT_DESCRIPTOR *)(b + d->VirtualAddress);
    for (unsigned i = 0; i < 256 && imp[i].Name; ++i) {
        const char *name = (const char *)(b + imp[i].Name);
        HMODULE h = GetModuleHandleA(name);
        if (h) sp_add_module(h, name);
    }
}

static const sp_module *sp_module_of(uint64_t address, unsigned *index) {
    for (unsigned i = 0; i < sp.module_count; ++i)
        if (address >= sp.modules[i].base && address < sp.modules[i].end) {
            if (index) *index = i;
            return &sp.modules[i];
        }
    return NULL;
}

static const RUNTIME_FUNCTION *sp_resolve(const sp_module *m, const RUNTIME_FUNCTION *f) {
    /* An odd UnwindData points to another RUNTIME_FUNCTION (indirect entry). */
    for (unsigned guard = 0; f && (f->UnwindData & 1) && guard < 4; ++guard) {
        uint32_t rva = f->UnwindData & ~1u;
        if (rva + sizeof(RUNTIME_FUNCTION) > m->end - m->base) return NULL;
        f = (const RUNTIME_FUNCTION *)(m->base + rva);
    }
    return f && !(f->UnwindData & 1) ? f : NULL;
}

static const RUNTIME_FUNCTION *sp_lookup(const sp_module *m, uint32_t rva) {
    uint32_t lo = 0, hi = m->count;
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        const RUNTIME_FUNCTION *f = &m->pdata[mid];
        if (rva < f->BeginAddress) hi = mid;
        else if (rva >= f->EndAddress) lo = mid + 1;
        else return sp_resolve(m, f);
    }
    return NULL;
}

typedef struct { uint8_t version_flags, prolog, count, frame; uint16_t codes[1]; } sp_unwind_info;

static const sp_unwind_info *sp_info(const sp_module *m, const RUNTIME_FUNCTION *f) {
    if (f->UnwindData + 4u > m->end - m->base) return NULL;
    const sp_unwind_info *u = (const sp_unwind_info *)(m->base + f->UnwindData);
    if (f->UnwindData + 4u + 2u * u->count + 12u > m->end - m->base) return NULL;
    return u;
}

/* Chained unwind info (cold fragments) is attributed to the primary function. */
static const RUNTIME_FUNCTION *sp_primary(const sp_module *m, const RUNTIME_FUNCTION *f) {
    for (unsigned guard = 0; f && guard < 8; ++guard) {
        const sp_unwind_info *u = sp_info(m, f);
        if (!u || !((u->version_flags >> 3) & UNW_FLAG_CHAININFO)) return f;
        f = sp_resolve(m, (const RUNTIME_FUNCTION *)&u->codes[(u->count + 1) & ~1u]);
    }
    return f;
}

/* ---------- unwinding (runs while the target is suspended) ---------- */

static uint64_t sp_reg(const CONTEXT *c, unsigned r) {
    const DWORD64 *regs = &c->Rax; /* Rax..R15 are contiguous in CONTEXT */
    return r < 16 ? regs[r] : 0;
}

/* Conservative frame check for frames after a stack scan: the frame register (if any) must
   point into the stack, and the frame described by the unwind codes must end below
   StackBase. Unknown unwind opcodes make the frame unsafe. */
static int sp_frame_safe(const sp_module *m, const RUNTIME_FUNCTION *f, const CONTEXT *c, uintptr_t stack_hi) {
    uint64_t extent = 0, base = c->Rsp;
    for (unsigned guard = 0; f && guard < 8; ++guard) {
        const sp_unwind_info *u = sp_info(m, f);
        if (!u) return 0;
        unsigned version = u->version_flags & 7, flags = u->version_flags >> 3;
        if (u->frame & 15) {
            uint64_t fp = sp_reg(c, u->frame & 15);
            if (fp < c->Rsp || fp >= stack_hi) return 0;
            base = fp - 16ull * (u->frame >> 4);
        }
        for (unsigned i = 0; i < u->count; ) {
            unsigned op = (u->codes[i] >> 8) & 15, info = u->codes[i] >> 12;
            switch (op) {
            case 0: extent += 8; i += 1; break;                                 /* PUSH_NONVOL */
            case 1:                                                             /* ALLOC_LARGE */
                if (info == 0) { if (i + 1 >= u->count) return 0; extent += 8ull * u->codes[i + 1]; i += 2; }
                else { if (i + 2 >= u->count) return 0;
                       extent += (uint64_t)u->codes[i + 1] | ((uint64_t)u->codes[i + 2] << 16); i += 3; }
                break;
            case 2: extent += 8ull * info + 8; i += 1; break;                   /* ALLOC_SMALL */
            case 3: i += 1; break;                                              /* SET_FPREG */
            case 4: case 8: i += 2; break;                                      /* SAVE_NONVOL/XMM128 */
            case 5: case 9: i += 3; break;                                      /* ..._FAR */
            case 10: extent += 48; i += 1; break;                               /* PUSH_MACHFRAME */
            case 6: if (version >= 2) return 0; i += 2; break;                  /* v2 EPILOG: unsure */
            default: return 0;
            }
        }
        if (!(flags & UNW_FLAG_CHAININFO)) break;
        f = sp_resolve(m, (const RUNTIME_FUNCTION *)&u->codes[(u->count + 1) & ~1u]);
    }
    return base + extent + 8 <= stack_hi;
}

/* Does `ret` follow a call instruction inside the code of `f`? */
static int sp_after_call(const sp_module *m, const RUNTIME_FUNCTION *f, uint64_t ret) {
    uint64_t begin = m->base + f->BeginAddress;
    const unsigned char *p = (const unsigned char *)(uintptr_t)ret;
    if (ret < begin + 2) return 0;
    uint64_t room = ret - begin;
    if (room >= 5 && p[-5] == 0xe8) return 1;                                         /* call rel32 */
    /* FF /2 with full ModRM bytes: reg=2, so the byte is 0x10 | mod<<6 | rm. */
    if (p[-2] == 0xff && (p[-1] & 0xf8) == 0xd0) return 1;                            /* call reg */
    if (room >= 3 && p[-3] == 0xff && (p[-2] == 0x14 ||
        ((p[-2] & 0xf8) == 0x50 && p[-2] != 0x54))) return 1;                         /* [sib] / [reg+d8] */
    if (room >= 4 && p[-4] == 0xff && p[-3] == 0x54) return 1;                        /* [sib+d8] */
    if (room >= 6 && p[-6] == 0xff && (p[-5] == 0x15 ||
        ((p[-5] & 0xf8) == 0x90 && p[-5] != 0x94))) return 1;                         /* [rip+d32] / [reg+d32] */
    if (room >= 7 && p[-7] == 0xff && p[-6] == 0x94) return 1;                        /* [sib+d32] */
    return 0;
}

static int sp_frame_safe(const sp_module *m, const RUNTIME_FUNCTION *f, const CONTEXT *c, uintptr_t stack_hi);

/* Leaf recovery for code without unwind data: the first stack slot holding a return address
   (after a call, in a listed module) whose own frame also unwinds to such an address. The
   second check rejects most stale return addresses left in uninitialised locals. `trial`
   is caller-provided scratch (no allocation while the target is suspended). */
static int sp_scan(CONTEXT *c, CONTEXT *trial, uintptr_t stack_hi) {
    uint64_t p = c->Rsp;
    for (unsigned i = 0; i < SP_SCAN_SLOTS && p + 8 <= stack_hi; ++i, p += 8) {
        uint64_t v = *(const volatile uint64_t *)(uintptr_t)p;
        const sp_module *m = sp_module_of(v, NULL);
        if (!m) continue;
        const RUNTIME_FUNCTION *f = sp_lookup(m, (uint32_t)(v - m->base));
        if (!f || !sp_after_call(m, f, v)) continue;
        memcpy(trial, c, sizeof(*trial));
        trial->Rip = v;
        trial->Rsp = p + 8;
        if (!sp_frame_safe(m, f, trial, stack_hi)) continue;
        PVOID handler_data = NULL;
        DWORD64 establisher = 0;
        RtlVirtualUnwind(UNW_FLAG_NHANDLER, m->base, v, (PRUNTIME_FUNCTION)f, trial, &handler_data, &establisher, NULL);
        if (trial->Rsp <= p + 8 || trial->Rsp + 8 > stack_hi) continue;
        const sp_module *m2 = sp_module_of(trial->Rip, NULL);
        const RUNTIME_FUNCTION *f2 = m2 ? sp_lookup(m2, (uint32_t)(trial->Rip - m2->base)) : NULL;
        if (!f2 || !sp_after_call(m2, f2, trial->Rip)) continue;
        c->Rip = v;
        c->Rsp = p + 8;
        return 1;
    }
    return 0;
}

/* Walks from the captured context. Stops after the frame of run_begin (MoveWorker::Run),
   at the depth cap, at the thread root, or at the first frame it cannot unwind safely. */
static unsigned sp_walk(CONTEXT *c, uintptr_t stack_lo, uintptr_t stack_hi, uintptr_t run_begin,
                        uint64_t *rips, unsigned max, uint8_t *flags, uint64_t *run_reg) {
    unsigned n = 0;
    int tainted = 0;
    for (;;) {
        uint64_t rip = c->Rip, rsp = c->Rsp;
        if (!rip) break;
        if (n == max) { *flags |= SP_F_TRUNC; break; }
        rips[n++] = rip;
        if (rsp < stack_lo || rsp + 8 > stack_hi || (rsp & 7)) { *flags |= SP_F_UNKNOWN; break; }
        const sp_module *m = sp_module_of(rip, NULL);
        if (!m) {
            static CONTEXT trial __attribute__((aligned(16))); /* sampler thread only */
            if (n != 1 || !sp_scan(c, &trial, stack_hi)) { *flags |= SP_F_UNKNOWN; break; }
            *flags |= SP_F_SCANNED;
            tainted = 1;
            continue;
        }
        const RUNTIME_FUNCTION *f = sp_lookup(m, (uint32_t)(rip - m->base));
        if (!f) {
            if (n != 1) { *flags |= SP_F_UNKNOWN; break; }
            c->Rip = *(const volatile uint64_t *)(uintptr_t)rsp; /* leaf without unwind data */
            c->Rsp = rsp + 8;
            continue;
        }
        if (run_begin) {
            const RUNTIME_FUNCTION *primary = sp_primary(m, f);
            if (primary && m->base + primary->BeginAddress == run_begin) {
                *flags |= SP_F_RUN;
                *run_reg = sp.slot_register >= 0 ? sp_reg(c, (unsigned)sp.slot_register) : ~0ull;
                break;
            }
        }
        if (tainted && !sp_frame_safe(m, f, c, stack_hi)) { *flags |= SP_F_UNKNOWN; break; }
        PVOID handler_data = NULL;
        DWORD64 establisher = 0;
        RtlVirtualUnwind(UNW_FLAG_NHANDLER, m->base, rip, (PRUNTIME_FUNCTION)f, c,
                         &handler_data, &establisher, NULL);
        if (c->Rip && (c->Rsp <= rsp || c->Rsp > stack_hi)) { *flags |= SP_F_UNKNOWN; break; }
    }
    return n;
}

/* ---------- aggregation (sampler thread, target running) ---------- */

static uint32_t sp_id_of(uint64_t rip, int leaf) {
    unsigned index;
    const sp_module *m = sp_module_of(rip, &index);
    if (!m) return SP_EXT_CODE;
    uint32_t rva = (uint32_t)(rip - m->base);
    const RUNTIME_FUNCTION *f = sp_lookup(m, leaf ? rva : rva - 1);
    if (!f) return SP_ID(index, rva);
    f = sp_primary(m, f);
    return f ? SP_ID(index, f->BeginAddress) : SP_ID(index, rva);
}

static int sp_wait_index(uint64_t rip) {
    for (unsigned i = 0; i < SP_WAITS; ++i)
        if (sp.wait_addr[i] && rip >= sp.wait_addr[i] && rip < sp.wait_addr[i] + SP_WAIT_STUB) return (int)i;
    return -1;
}

static uint32_t sp_hash(uint32_t h, uint32_t v) {
    for (unsigned i = 0; i < 4; ++i) { h ^= (v >> (8 * i)) & 0xff; h *= 16777619u; }
    return h;
}

static sp_fn *sp_fn_slot(uint32_t id) {
    sp_fn *table = sp.tables->fn;
    uint32_t i = sp_hash(2166136261u, id) & (SP_FN_CAP - 1);
    for (unsigned probe = 0; probe < 64; ++probe, i = (i + 1) & (SP_FN_CAP - 1)) {
        if (table[i].id == id) return &table[i];
        if (table[i].id == SP_NO_ID) {
            if (sp.win.fn_used >= SP_FN_CAP * 3 / 4) return NULL;
            table[i].id = id;
            table[i].self = table[i].incl = 0;
            ++sp.win.fn_used;
            return &table[i];
        }
    }
    return NULL;
}

static void sp_fold_add(uint8_t phase, const uint32_t *ids, unsigned depth) {
    sp_fold *table = sp.tables->folds[sp.active_fold];
    uint32_t h = sp_hash(2166136261u, phase);
    for (unsigned i = 0; i < depth; ++i) h = sp_hash(h, ids[i]);
    if (h == 0) h = 1;
    uint32_t i = h & (SP_FOLD_CAP - 1);
    for (unsigned probe = 0; probe < 64; ++probe, i = (i + 1) & (SP_FOLD_CAP - 1)) {
        sp_fold *e = &table[i];
        if (!e->hash) {
            if (sp.win.fold_used >= SP_FOLD_CAP * 3 / 4) break;
            e->hash = h; e->count = 1; e->phase = phase; e->depth = (uint8_t)depth;
            memcpy(e->ids, ids, depth * sizeof(uint32_t));
            ++sp.win.fold_used;
            return;
        }
        if (e->hash == h && e->phase == phase && e->depth == depth &&
            !memcmp(e->ids, ids, depth * sizeof(uint32_t))) { ++e->count; return; }
    }
    ++sp.win.dropped_fold;
}

static void sp_refresh_slots(void) {
    for (unsigned i = 0; i < SP_SLOTS; ++i) {
        uintptr_t v = sp_slot_vfunc[i];
        if (v == sp.slot_cached[i]) continue;
        sp.slot_cached[i] = v;
        sp.slot_ids[i] = v ? sp_id_of(v, 1) : SP_NO_ID;
        if (sp.slot_ids[i] == SP_EXT_CODE) sp.slot_ids[i] = SP_NO_ID;
    }
}

static void sp_aggregate(const sp_raw *r) {
    sp_window *w = &sp.win;
    ++w->samples;
    w->suspend_ticks += r->suspend_ticks;
    w->walk_ticks += r->walk_ticks;
    if (r->suspend_ticks > w->suspend_max) w->suspend_max = r->suspend_ticks;
    if (r->flags & SP_F_LOADING) { ++w->phase_run[SP_PH_LOADING]; return; }
    uint32_t ids[SP_MAX_DEPTH + 1];
    unsigned n = r->depth;
    int wait = n && !(r->flags & SP_F_SCANNED) ? sp_wait_index(r->rip[0]) : -1;
    for (unsigned i = 0; i < n; ++i) ids[i] = sp_id_of(r->rip[i], i == 0);
    if (wait >= 0) ids[0] = SP_WAIT_ID + (uint32_t)wait;
    if (r->flags & SP_F_TRUNC) ++w->truncated;
    if (r->flags & SP_F_SCANNED) ++w->scanned;
    w->frames += n;
    unsigned phase;
    unsigned k = n;
    if (r->flags & SP_F_RUN) k = n - 1;
    if (k < n && ids[k] == sp.run_id) {
        phase = wait >= 0 ? SP_PH_IDLE : SP_PH_FRAME;
        /* Primary: the slot index register restored by the unwinder in the Run frame, valid
           when Run is at its slot call. Not trusted after a stack scan (the skipped frames may
           have saved and clobbered it). Fallback: match the frame below Run against the slot
           handlers' Move() functions (misses tail calls and thunks: those count as "frame"). */
        if (sp.slot_return && !(r->flags & SP_F_SCANNED) && r->rip[k] == sp.slot_return) {
            if (r->run_reg < SP_SLOTS) phase = (unsigned)r->run_reg;
        } else if (k > 0) {
            sp_refresh_slots();
            for (unsigned s = 0; s < SP_SLOTS; ++s)
                if (sp.slot_ids[s] != SP_NO_ID && ids[k - 1] == sp.slot_ids[s]) { phase = s; break; }
        }
    } else {
        phase = r->loop_state == 1 ? SP_PH_LOST : SP_PH_LOST_OUT;
    }
    if (wait >= 0) ++w->phase_wait[phase]; else ++w->phase_run[phase];
    for (unsigned i = 0; i < n; ++i) {
        unsigned seen = 0;
        for (unsigned j = 0; j < i; ++j) if (ids[j] == ids[i]) { seen = 1; break; }
        sp_fn *e = sp_fn_slot(ids[i]);
        if (!e) { ++w->dropped_fn; continue; }
        if (i == 0) ++e->self;
        if (!seen) ++e->incl;
    }
    uint32_t folded[SP_MAX_DEPTH + 1];
    unsigned d = 0;
    if (r->flags & (SP_F_TRUNC | SP_F_UNKNOWN)) folded[d++] = SP_TRUNCATED;
    for (unsigned i = n; i-- > 0; ) folded[d++] = ids[i];
    sp_fold_add((uint8_t)phase, folded, d);
}

static void sp_window_reset(void) {
    memset(&sp.win, 0, sizeof(sp.win));
    memset(sp.tables->fn, 0xff, sizeof(sp.tables->fn));
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    sp.win.start_qpc = now.QuadPart;
}

/* ---------- sampler thread ---------- */

static void sp_disable(const char *what, DWORD error) {
    const char *p = what;
    unsigned n = 0;
    while (*p && n < sizeof(sp.reason_buf) - 24) sp.reason_buf[n++] = *p++;
    static const char tag[] = " err=";
    for (p = tag; *p; ) sp.reason_buf[n++] = *p++;
    char digits[12];
    unsigned d = 0;
    do { digits[d++] = (char)('0' + error % 10); error /= 10; } while (error && d < sizeof(digits));
    while (d) sp.reason_buf[n++] = digits[--d];
    sp.reason_buf[n] = 0;
    sp.reason = sp.reason_buf;
    InterlockedExchange(&sp.state, 2);
}

static unsigned sp_next_interval_us(void) {
    sp.rng ^= sp.rng << 13; sp.rng ^= sp.rng >> 17; sp.rng ^= sp.rng << 5;
    unsigned spread = sp.interval_us / 5; /* +-10% jitter avoids locking to the frame rate */
    return sp.interval_us - spread / 2 + (spread ? sp.rng % (spread + 1) : 0);
}

/* Absolute schedule: timer lateness is absorbed by the next wait, so the mean rate stays at
   the configured interval when the timer is late by less than one interval. */
static void sp_wait_next(int64_t *due) {
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    int64_t interval = (int64_t)sp.interval_us * sp.qpc_frequency / 1000000;
    if (!*due || now.QuadPart - *due > 4 * interval) *due = now.QuadPart;
    *due += (int64_t)sp_next_interval_us() * sp.qpc_frequency / 1000000;
    int64_t us = (*due - now.QuadPart) * 1000000 / sp.qpc_frequency;
    if (us < 50) { if (us < 0) return; us = 50; }
    HANDLE handles[2] = {sp.stop_event, sp.timer};
    if (sp.timer) {
        LARGE_INTEGER due;
        due.QuadPart = -(LONGLONG)us * 10;
        if (SetWaitableTimer(sp.timer, &due, 0, NULL, NULL, FALSE)) {
            WaitForMultipleObjects(2, handles, FALSE, INFINITE);
            return;
        }
    }
    WaitForSingleObject(sp.stop_event, (DWORD)((us + 999) / 1000));
}

static DWORD WINAPI sp_thread(LPVOID unused) {
    (void)unused;
    static CONTEXT context __attribute__((aligned(16)));
    unsigned failures = 0, backoff_ms = 0;
    int64_t due = 0;
    while (!sp.stop) {
        if (backoff_ms) { WaitForSingleObject(sp.stop_event, backoff_ms); backoff_ms = 0; due = 0; if (sp.stop) break; }
        sp_wait_next(&due);
        if (sp.stop) break;
        sp_raw *r = &sp.tables->ring[sp.ring_head % SP_RING];
        r->depth = 0; r->flags = 0;
        r->loop_state = (uint8_t)sp_loop_state;
        if (sp.loading_flag && *sp.loading_flag) {
            r->flags = SP_F_LOADING; r->suspend_ticks = r->walk_ticks = 0;
        } else {
            LARGE_INTEGER t0, t1, w0, w1;
            QueryPerformanceCounter(&t0);
            if (sp_suspend(sp.target) == (DWORD)-1) {
                DWORD error = GetLastError();
                if (++failures >= SP_FAIL_LIMIT) { sp_disable("SuspendThread failed", error); break; }
                backoff_ms = 50u << failures;
                AcquireSRWLockExclusive(&sp.lock);
                ++sp.win.failures;
                ReleaseSRWLockExclusive(&sp.lock);
                continue;
            }
            /* GetThreadContext also waits until the suspension has taken effect. */
            memset(&context, 0, sizeof(context));
            context.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
            BOOL ok = sp_get_context(sp.target, &context);
            DWORD error = ok ? 0 : GetLastError();
            QueryPerformanceCounter(&w0);
            w1 = w0;
            if (ok) {
                uintptr_t stack_lo = (uintptr_t)((volatile const NT_TIB *)sp.tib)->StackLimit;
                r->loop_state = (uint8_t)sp_loop_state;
                if (context.Rsp < stack_lo || context.Rsp >= sp.stack_base) r->flags = SP_F_UNKNOWN;
                else r->depth = (uint8_t)sp_walk(&context, (uintptr_t)context.Rsp, sp.stack_base,
                                                 sp.run_begin, r->rip, sp.depth, &r->flags, &r->run_reg);
                QueryPerformanceCounter(&w1);
            }
            DWORD resumed = sp_resume(sp.target);
            for (unsigned retry = 0; resumed == (DWORD)-1 && retry < 3; ++retry) resumed = sp_resume(sp.target);
            QueryPerformanceCounter(&t1);
            if (resumed == (DWORD)-1) { sp_disable("ResumeThread failed", GetLastError()); break; }
            r->suspend_ticks = (uint32_t)(t1.QuadPart - t0.QuadPart);
            r->walk_ticks = (uint32_t)(w1.QuadPart - w0.QuadPart);
            if (!ok) {
                if (++failures >= SP_FAIL_LIMIT) { sp_disable("GetThreadContext failed", error); break; }
                backoff_ms = 50u << failures;
                AcquireSRWLockExclusive(&sp.lock);
                ++sp.win.failures;
                ReleaseSRWLockExclusive(&sp.lock);
                continue;
            }
            failures = 0;
            if (r->flags == SP_F_UNKNOWN && !r->depth) {
                AcquireSRWLockExclusive(&sp.lock);
                ++sp.win.skipped;
                ReleaseSRWLockExclusive(&sp.lock);
                continue;
            }
        }
        unsigned produced = ++sp.ring_head;
        AcquireSRWLockExclusive(&sp.lock);
        sp_aggregate(&sp.tables->ring[(produced - 1) % SP_RING]);
        ReleaseSRWLockExclusive(&sp.lock);
    }
    return 0;
}

/* ---------- control (target/Lua thread) ---------- */

typedef struct {
    DWORD thread_id;
    const NT_TIB *tib;            /* the target's TEB (NtCurrentTeb() on the target) */
    uintptr_t run_address;        /* any address inside MoveWorker::Run, or 0 */
    uintptr_t slot_return;        /* return address of Run's slot call, or 0 */
    int slot_register;            /* CONTEXT register index (Rax=0 .. R15=15) holding the slot */
    const volatile int *loading_flag;
    unsigned interval_us, depth;
} sp_params;

/* Called on the target thread once per frame: snapshot the handler Move() vfuncs. The engine
   itself calls these entries in the same frame, so the dereferences are as safe as its own. */
static void simprof_note_slots(void *const *table) {
    if (sp.state != 1) return;
    for (unsigned i = 0; i < SP_SLOTS; ++i) {
        void **const *h = (void **const *)table[i];
        sp_slot_vfunc[i] = h ? (uintptr_t)(*h)[1] : 0;
    }
}

static void simprof_loop(LONG state) { InterlockedExchange(&sp_loop_state, state); }

static void sp_release(void) {
    if (sp.stop_event) { CloseHandle(sp.stop_event); sp.stop_event = NULL; }
    if (sp.timer) { CloseHandle(sp.timer); sp.timer = NULL; }
    if (sp.target) { CloseHandle(sp.target); sp.target = NULL; }
    for (unsigned i = 0; i < sp.module_count; ++i) if (sp.modules[i].ref) FreeLibrary(sp.modules[i].ref);
    sp.module_count = 0;
    if (sp.tables && !sp.leaked) VirtualFree(sp.tables, 0, MEM_RELEASE);
    sp.tables = NULL;
}

/* Stops the sampler and waits for its thread. Safe to call repeatedly. */
static void simprof_stop(void) {
    if (!sp.thread && !sp.tables) return;
    InterlockedExchange(&sp.stop, 1);
    if (sp.stop_event) SetEvent(sp.stop_event);
    if (sp.thread) {
        if (WaitForSingleObject(sp.thread, 2000) != WAIT_OBJECT_0) sp.leaked = 1;
        CloseHandle(sp.thread);
        sp.thread = NULL;
    }
    if (sp.state == 1) InterlockedExchange(&sp.state, 0);
    sp.snapshot = -1;
    if (sp.leaked) { sp.tables = NULL; sp.stop_event = NULL; sp.timer = NULL; sp.target = NULL; sp.module_count = 0; return; }
    sp_release();
}

/* Process-detach: never wait (the loader lock is held); just ask the thread to exit. */
static __attribute__((unused)) void simprof_signal_exit(void) {
    InterlockedExchange(&sp.stop, 1);
    if (sp.stop_event) SetEvent(sp.stop_event);
}

static const char *simprof_start(const sp_params *p) {
    if (sp.thread) return "sampler already running";
    if (p->interval_us < SP_MIN_INTERVAL_US || p->interval_us > SP_MAX_INTERVAL_US) return "invalid interval";
    if (p->depth < SP_MIN_DEPTH || p->depth > SP_MAX_DEPTH) return "invalid depth";
    memset(&sp.win, 0, sizeof(sp.win));
    sp.stop = 0; sp.state = 0; sp.reason = NULL; sp.leaked = 0; sp.snapshot = -1;
    sp.module_count = 0; sp.ring_head = 0; sp.active_fold = 0;
    for (unsigned i = 0; i < SP_SLOTS; ++i) { sp_slot_vfunc[i] = 0; sp.slot_cached[i] = 0; sp.slot_ids[i] = SP_NO_ID; }
    sp_loop_state = 0;
    sp.interval_us = p->interval_us; sp.depth = p->depth;
    sp.target_id = p->thread_id; sp.tib = p->tib; sp.loading_flag = p->loading_flag;
    sp.stack_base = (uintptr_t)p->tib->StackBase;
    sp.rng = 0x9e3779b9u ^ p->thread_id;
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    sp.qpc_frequency = frequency.QuadPart;
    const char *why = NULL;
    HMODULE self = NULL;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCWSTR)(void *)sp_thread, &self);
    if (!sp_add_module(GetModuleHandleW(NULL), "main")) { why = "main image has no unwind data"; goto fail; }
    sp_add_module(self, "simprof");
    sp_add_module(GetModuleHandleW(L"ntdll.dll"), "ntdll");
    sp_add_module(GetModuleHandleW(L"kernelbase.dll"), "kernelbase");
    sp_add_module(GetModuleHandleW(L"kernel32.dll"), "kernel32");
    sp_add_imports();
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    for (unsigned i = 0; i < SP_WAITS; ++i)
        sp.wait_addr[i] = ntdll ? (uintptr_t)GetProcAddress(ntdll, sp_wait_names[i]) : 0;
    sp.run_id = SP_NO_ID;
    sp.run_begin = 0;
    sp.slot_return = p->slot_return;
    sp.slot_register = p->slot_register >= 0 && p->slot_register < 16 ? p->slot_register : -1;
    if (sp.slot_register < 0) sp.slot_return = 0;
    if (p->run_address) {
        const sp_module *m = &sp.modules[0];
        if (p->run_address < m->base || p->run_address >= m->end) { why = "run address outside main image"; goto fail; }
        const RUNTIME_FUNCTION *f = sp_lookup(m, (uint32_t)(p->run_address - m->base));
        f = f ? sp_primary(m, f) : NULL;
        if (!f) { why = "run function has no unwind data"; goto fail; }
        sp.run_id = SP_ID(0, f->BeginAddress);
        sp.run_begin = m->base + f->BeginAddress;
    }
    sp.tables = VirtualAlloc(NULL, sizeof(sp_tables), MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!sp.tables) { why = "table allocation failed"; goto fail; }
    memset(sp.tables->folds, 0, sizeof(sp.tables->folds));
    sp_window_reset();
    sp.target = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_LIMITED_INFORMATION,
                           FALSE, p->thread_id);
    if (!sp.target) { why = "OpenThread failed"; goto fail; }
    sp.stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!sp.stop_event) { why = "CreateEvent failed"; goto fail; }
    sp.timer = CreateWaitableTimerExW(NULL, NULL, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    InterlockedExchange(&sp.state, 1);
    sp.thread = CreateThread(NULL, 64 * 1024, sp_thread, NULL, CREATE_SUSPENDED, NULL);
    if (!sp.thread) { InterlockedExchange(&sp.state, 0); why = "CreateThread failed"; goto fail; }
    /* Keep preemption of the sampler (while the target is suspended) unlikely. */
    SetThreadPriority(sp.thread, THREAD_PRIORITY_TIME_CRITICAL);
    ResumeThread(sp.thread);
    return NULL;
fail:
    sp_release();
    return why;
}

/* ---------- reporting (target/Lua thread) ---------- */

static int sp_format_id(uint32_t id, char *out, size_t size) {
    unsigned m = id >> SP_RVA_BITS, rva = id & ((1u << SP_RVA_BITS) - 1);
    if (id == SP_EXT_CODE) return snprintf(out, size, "[ext]");
    if (id == SP_TRUNCATED) return snprintf(out, size, "[truncated]");
    if (id >= SP_WAIT_ID && id < SP_WAIT_ID + SP_WAITS) return snprintf(out, size, "ntdll!%s", sp_wait_names[id - SP_WAIT_ID]);
    if (m == 0) return snprintf(out, size, "%x", rva);
    if (m < sp.module_count) return snprintf(out, size, "%s+%x", sp.modules[m].name, rva);
    return snprintf(out, size, "[m%u]+%x", m, rva);
}

typedef struct {
    double window_s, rate_hz, depth_mean, suspend_us_mean, suspend_us_max, walk_us_mean, overhead_pct;
    uint64_t samples, truncated, scanned, failures, skipped, dropped_fn, dropped_fold, loading, lost;
    unsigned fold_pending;
    char phases[640], self_top[800], incl_top[800];
} sp_report;

static void sp_top(const sp_fn *table, int self, uint64_t total, char *out, size_t size) {
    uint32_t best_id[SP_TOP], best[SP_TOP];
    unsigned n = 0;
    for (unsigned i = 0; i < SP_FN_CAP; ++i) {
        if (table[i].id == SP_NO_ID) continue;
        uint32_t v = self ? table[i].self : table[i].incl;
        if (!v || (n == SP_TOP && v <= best[n - 1])) continue;
        unsigned j = n < SP_TOP ? n++ : SP_TOP - 1;
        while (j > 0 && best[j - 1] < v) { best[j] = best[j - 1]; best_id[j] = best_id[j - 1]; --j; }
        best[j] = v; best_id[j] = table[i].id;
    }
    size_t used = 0;
    out[0] = 0;
    for (unsigned i = 0; i < n && used + 48 < size; ++i) {
        char name[40];
        sp_format_id(best_id[i], name, sizeof(name));
        used += (size_t)snprintf(out + used, size - used, "%s%s=%.1f%%", i ? " " : "", name,
                                 total ? 100.0 * best[i] / (double)total : 0.0);
    }
}

/* Closes the current window: fills `rep`, resets the window, and swaps the fold table into
   the drain snapshot when the previous snapshot is fully drained. */
static int simprof_report(sp_report *rep) {
    if (!sp.tables) return 0;
    memset(rep, 0, sizeof(*rep));
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    AcquireSRWLockExclusive(&sp.lock);
    sp_window *w = &sp.win;
    double ticks_us = sp.qpc_frequency > 0 ? 1e6 / (double)sp.qpc_frequency : 0.0;
    double window_us = (double)(now.QuadPart - w->start_qpc) * ticks_us;
    uint64_t walked = w->samples - w->phase_run[SP_PH_LOADING];
    rep->window_s = window_us / 1e6;
    rep->samples = w->samples;
    rep->rate_hz = window_us > 0 ? w->samples * 1e6 / window_us : 0.0;
    rep->depth_mean = walked ? (double)w->frames / (double)walked : 0.0;
    rep->suspend_us_mean = walked ? (double)w->suspend_ticks * ticks_us / (double)walked : 0.0;
    rep->suspend_us_max = (double)w->suspend_max * ticks_us;
    rep->walk_us_mean = walked ? (double)w->walk_ticks * ticks_us / (double)walked : 0.0;
    rep->overhead_pct = window_us > 0 ? 100.0 * (double)w->suspend_ticks * ticks_us / window_us : 0.0;
    rep->truncated = w->truncated; rep->scanned = w->scanned; rep->failures = w->failures;
    rep->skipped = w->skipped; rep->dropped_fn = w->dropped_fn; rep->dropped_fold = w->dropped_fold;
    rep->loading = w->phase_run[SP_PH_LOADING];
    rep->lost = w->phase_run[SP_PH_LOST] + w->phase_wait[SP_PH_LOST] +
                w->phase_run[SP_PH_LOST_OUT] + w->phase_wait[SP_PH_LOST_OUT];
    /* Phase shares, largest first: name=share%(wwait%). */
    unsigned order[SP_PHASES], count = 0;
    for (unsigned i = 0; i < SP_PHASES; ++i) if (w->phase_run[i] + w->phase_wait[i]) order[count++] = i;
    for (unsigned i = 1; i < count; ++i)
        for (unsigned j = i; j > 0 && w->phase_run[order[j]] + w->phase_wait[order[j]] >
                                      w->phase_run[order[j - 1]] + w->phase_wait[order[j - 1]]; --j) {
            unsigned t = order[j]; order[j] = order[j - 1]; order[j - 1] = t;
        }
    size_t used = 0;
    for (unsigned i = 0; i < count && used + 40 < sizeof(rep->phases); ++i) {
        unsigned ph = order[i];
        double total = w->samples ? 100.0 / (double)w->samples : 0.0;
        used += (size_t)snprintf(rep->phases + used, sizeof(rep->phases) - used, "%s%s=%.1f%%(w%.1f)",
                                 i ? " " : "", sp_phase_names[ph],
                                 (w->phase_run[ph] + w->phase_wait[ph]) * total, w->phase_wait[ph] * total);
    }
    sp_top(sp.tables->fn, 1, w->samples, rep->self_top, sizeof(rep->self_top));
    sp_top(sp.tables->fn, 0, w->samples, rep->incl_top, sizeof(rep->incl_top));
    if (sp.snapshot < 0 && w->fold_used) {
        sp.snapshot = (int)sp.active_fold;
        sp.snapshot_cursor = 0;
        sp.snapshot_left = (unsigned)w->fold_used;
        sp.active_fold ^= 1;
        memset(sp.tables->folds[sp.active_fold], 0, sizeof(sp.tables->folds[0]));
        w->fold_used = 0;
    }
    uint64_t fold_used = w->fold_used;
    sp_window_reset();
    sp.win.fold_used = fold_used; /* the active fold table keeps accumulating while a drain is pending */
    ReleaseSRWLockExclusive(&sp.lock);
    rep->fold_pending = sp.snapshot >= 0 ? sp.snapshot_left : 0;
    return 1;
}

/* Next folded stack from the drain snapshot ("phase;root;...;leaf count"), or 0 when done. */
static int simprof_next_fold(char *out, size_t size) {
    if (sp.snapshot < 0 || !sp.tables) return 0;
    sp_fold *table = sp.tables->folds[sp.snapshot];
    while (sp.snapshot_cursor < SP_FOLD_CAP) {
        sp_fold *e = &table[sp.snapshot_cursor++];
        if (!e->hash) continue;
        size_t used = (size_t)snprintf(out, size, "%s", sp_phase_names[e->phase]);
        for (unsigned i = 0; i < e->depth && used + 48 < size; ++i) {
            out[used++] = ';';
            used += (size_t)sp_format_id(e->ids[i], out + used, size - used);
        }
        snprintf(out + used, size - used, " %u", e->count);
        if (sp.snapshot_left) --sp.snapshot_left;
        return 1;
    }
    sp.snapshot = -1;
    sp.snapshot_left = 0;
    return 0;
}
