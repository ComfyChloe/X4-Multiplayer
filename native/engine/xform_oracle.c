/* Offline oracle for the transforms rewrite (rust/x4mp-transform).
 *
 * Runs the ORIGINAL machine code of four X4 9.00 functions (Steam build 23660954) on
 * synthetic components, so that the Rust models can be compared bit for bit without
 * starting the game:
 *   U::Component::BuildCache            0x3d4a80   (view-cache build, global stacks)
 *   maybe_U::Component::PropagateCacheFlags 0x3d46b0 (per-component refresh)
 *   U::Positional::GetLocalTransform    0x7461c0   (pose composition)
 *   U::Component::GetTransformRelativeTo 0x3db8a0  (relative pose, inverse frame)
 *
 * How: the X4.exe given on the command line must be byte-identical to the pinned build
 * (size and SHA-256 from native/engine/signatures.py, via fixedtick_signatures.h). Its
 * sections are copied into a private allocation of this process; no import is resolved,
 * no relocation applied, no initialiser, TLS callback or entry point runs, and nothing is
 * written back to any file. Only the functions above are called. The paths they take use
 * rip-relative globals only (which live in the private copy) and virtual calls through the
 * synthetic components' vtables, which point at the callbacks below; the scenario
 * generator avoids every path that would reach the engine allocator or error handlers.
 * The engine reads its thread-local block through TEB->ThreadLocalStoragePointer[0];
 * while engine code runs, entry 0 points at a private block (game time, init flags).
 *
 * Usage:
 *   xform_oracle cache <X4.exe> <scenario.scn> <result.res>
 *   xform_oracle pose  <X4.exe> <cases.pos> <result.por>
 *   xform_oracle capture <X4.exe> <scenario.scn> <result.res> <prefix>
 *       as cache, but every frame goes through the in-game capture core (xform_capture.h),
 *       writing <prefix>_NNN.cap, so the capture format and replay are proven offline too
 *   xform_oracle probe <X4.exe> <scenario.scn> <result.res> <dir>
 *       every frame goes through the in-game probe's hook (xform_probe.c, embedded): the call
 *       sites of this private copy are patched as in game, every second capture is traced, and
 *       all code bytes must be back to the pinned hashes afterwards
 * File formats: rust/x4mp-transform/src/scenario.rs and tests/oracle.rs.
 * Exit status: 0 ok, 2 usage/input, 3 refused (file mismatch), 4 engine fault or
 * unexpected virtual call.
 *
 * Development tool only: never loaded into the game, never used on the X4 installation.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <intrin.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fixedtick_signatures.h"

#define RVA_BUILD_CACHE 0x3d4a80u
#define RVA_PROPAGATE 0x3d46b0u
#define RVA_LOCAL_TRANSFORM 0x7461c0u
#define RVA_RELATIVE 0x3db8a0u
#define RVA_DROP_POSE_BLOCKS 0x2f4b9bcu

/* U::Component layout (see rust/x4mp-transform/src/cache.rs). */
enum {
    C_REFS = 0x40, C_STATE = 0x44, C_MODE = 0x68, C_PARENT = 0x70, C_CONN = 0x78, C_ATTACH = 0xa0,
    C_LIVE = 0xa8, C_REGISTERED = 0xd1, C_SPEC = 0xd8, C_VC = 0xe0, VC_SIZE = 0x80, C_FLAGS = 0x1e0,
    C_MASK = 0x1e4, C_COUNT = 0x1e8, C_ATTENTION = 0x1ec, C_CONTROLLER = 0x200, C_TAG = 0x3f8, C_SIZE = 0x400,
};
enum { VC_PARENT = 0x00, VC_KIDS = 0x10, VC_POSE = 0x28, VC_ATT = 0x38, VC_DIRTY = 0x3c };
enum { SPEC_CAP = 0x80, SPEC_COUNT = 0x88, SPEC_SIZE = 0x90, ENTRY_SIZE = 0x20 };
enum { VTBL_SLOTS = 0x1600 / 8 };

typedef void (*build_cache_fn)(void *c, unsigned idx, double time);
typedef void (*propagate_fn)(void *c, unsigned idx, const uint32_t *flags);
typedef void (*local_transform_fn)(void *c, float *mat, double time, unsigned char reset, unsigned char incl);
typedef void (*relative_fn)(void *c, float *mat, double time, void *target, unsigned char flag);

static unsigned char *image;
static size_t image_size;
static unsigned char *text_start, *text_end;

#include "xform_capture.h"
#define XFORM_PROBE_EMBED
#include "xform_probe.c"

/* Negative control in this offline executable only: the native probe has no fault switch. */
static int shadow_fault_mode;
static unsigned shadow_fault_calls;
static shadow_check_fn clean_shadow_check;
static int32_t fault_shadow_check(const uint8_t *data, size_t length, xform_check *out) {
    ++shadow_fault_calls;
    int32_t rc = clean_shadow_check(data, length, out);
    if (rc != 0) return rc;
    if (shadow_fault_mode == 2) return -2;
    ++out->mismatches;
    strcpy(out->message, "injected offline mismatch");
    return 0;
}

static int digest_hex(const unsigned char d[32], const char *expected) {
    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < 32; ++i) {
        char hi = hex[d[i] >> 4], lo = hex[d[i] & 15];
        char e0 = expected[2 * i] | 0x20, e1 = expected[2 * i + 1] | 0x20;
        if (hi != e0 || lo != e1) return 0;
    }
    return expected[64] == 0;
}

static unsigned char *read_file(const char *path, size_t *size) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long n = ftell(f);
    if (n < 0 || fseek(f, 0, SEEK_SET) != 0) { fclose(f); return NULL; }
    unsigned char *b = malloc((size_t)n + 1);
    if (!b || fread(b, 1, (size_t)n, f) != (size_t)n) { fclose(f); free(b); return NULL; }
    fclose(f);
    *size = (size_t)n;
    return b;
}

static int sha256_matches(const unsigned char *data, size_t n, const char *expected) {
    /* expected: hex digest */
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    unsigned char digest[32];
    int ok = 0;
    if (n > 0xffffffffu) return 0;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0) goto done;
    if (BCryptCreateHash(alg, &hash, NULL, 0, NULL, 0, 0) < 0) goto done;
    if (BCryptHashData(hash, (PUCHAR)data, (ULONG)n, 0) < 0) goto done;
    if (BCryptFinishHash(hash, digest, sizeof(digest), 0) < 0) goto done;
    ok = digest_hex(digest, expected);
done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    return ok;
}

/* Copies the pinned executable's sections into a private allocation. */
static int map_image(const char *path) {
    size_t n = 0;
    unsigned char *file = read_file(path, &n);
    if (!file) { fprintf(stderr, "xform_oracle: cannot read %s\n", path); return 2; }
    if (n != (size_t)X4_FILE_SIZE || !sha256_matches(file, n, X4_FILE_SHA256)) {
        fprintf(stderr, "xform_oracle: refused: %s is not the pinned X4.exe (build 23660954)\n", path);
        free(file);
        return 3;
    }
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)file;
    IMAGE_NT_HEADERS64 *nt = (IMAGE_NT_HEADERS64 *)(file + dos->e_lfanew);
    if (nt->FileHeader.TimeDateStamp != X4_PE_TIMESTAMP || nt->OptionalHeader.SizeOfImage != X4_IMAGE_SIZE) {
        fprintf(stderr, "xform_oracle: refused: PE header mismatch\n");
        free(file);
        return 3;
    }
    image_size = nt->OptionalHeader.SizeOfImage;
    image = VirtualAlloc(NULL, image_size, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!image) { free(file); return 2; }
    memcpy(image, file, nt->OptionalHeader.SizeOfHeaders);
    IMAGE_SECTION_HEADER *s = IMAGE_FIRST_SECTION(nt);
    for (unsigned i = 0; i < nt->FileHeader.NumberOfSections; ++i) {
        DWORD raw = s[i].SizeOfRawData < s[i].Misc.VirtualSize ? s[i].SizeOfRawData : s[i].Misc.VirtualSize;
        if (s[i].VirtualAddress + (size_t)s[i].Misc.VirtualSize > image_size || s[i].PointerToRawData + (size_t)raw > n) {
            free(file);
            return 3;
        }
        memcpy(image + s[i].VirtualAddress, file + s[i].PointerToRawData, raw);
        if (memcmp(s[i].Name, ".text", 6) == 0) {
            DWORD old;
            text_start = image + s[i].VirtualAddress;
            text_end = text_start + s[i].Misc.VirtualSize;
            if (!VirtualProtect(text_start, s[i].Misc.VirtualSize, PAGE_EXECUTE_READ, &old)) { free(file); return 2; }
        }
    }
    free(file);
    if (!text_start) return 3;
    FlushInstructionCache(GetCurrentProcess(), image, image_size);
    return 0;
}

/* ---- thread-local block seen by engine code ---- */
static _Alignas(64) unsigned char fake_tls[0x400];
static void *tls_array[64];
static void **saved_tls_pointer;

static void tls_enter(double time) {
    fake_tls[0x10] = 1;        /* on-demand TLS initialised */
    fake_tls[0x310] = 1;       /* allocator thread initialised */
    *(int *)(fake_tls + 0x314) = 1;  /* MoveWorker role */
    memcpy(fake_tls + 0x300, &time, sizeof(time));
    memcpy(fake_tls + 0x328, &time, sizeof(time));
    /* Engine code reads ThreadLocalStoragePointer[0]. Swap only that entry (this executable's own
       slot) so other modules' implicit TLS, e.g. the Rust library's, keeps working. */
    unsigned char *teb = (unsigned char *)__readgsqword(0x30);
    void **array = *(void ***)(teb + 0x58);
    if (array) {
        saved_tls_pointer = (void **)array[0];
        array[0] = fake_tls;
    } else {
        tls_array[0] = fake_tls;
        saved_tls_pointer = NULL;
        *(void ***)(teb + 0x58) = tls_array;
    }
}

static void tls_leave(void) {
    unsigned char *teb = (unsigned char *)__readgsqword(0x30);
    void **array = *(void ***)(teb + 0x58);
    if (array == tls_array) *(void ***)(teb + 0x58) = NULL;
    else if (array) array[0] = saved_tls_pointer;
}

/* ---- fault and unexpected-call reporting ---- */
static volatile LONG unexpected_calls;
static LONG CALLBACK on_fault(EXCEPTION_POINTERS *e) {
    DWORD code = e->ExceptionRecord->ExceptionCode;
    if (code == EXCEPTION_ACCESS_VIOLATION || code == EXCEPTION_ILLEGAL_INSTRUCTION || code == EXCEPTION_PRIV_INSTRUCTION) {
        unsigned char *ip = (unsigned char *)e->ContextRecord->Rip;
        if (ip >= image && ip < image + image_size)
            fprintf(stderr, "xform_oracle: engine fault 0x%lx at rva 0x%llx\n", code, (unsigned long long)(ip - image));
        else {
            HMODULE m = NULL;
            char name[MAX_PATH] = "?";
            if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                   (LPCSTR)ip, &m))
                GetModuleFileNameA(m, name, sizeof name);
            fprintf(stderr, "xform_oracle: fault 0x%lx at %p = %s+0x%llx (access %llu at %p)\n", code, (void *)ip, name,
                    (unsigned long long)(ip - (unsigned char *)m), (unsigned long long)e->ExceptionRecord->ExceptionInformation[0],
                    (void *)e->ExceptionRecord->ExceptionInformation[1]);
        }
        fflush(stderr);
        ExitProcess(4);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

static uint64_t trap(void) {
    InterlockedIncrement(&unexpected_calls);
    return 0;
}

/* ---- input reader ---- */
typedef struct { const unsigned char *p, *end; int bad; } reader;
static uint32_t rd_u32(reader *r) {
    uint32_t v = 0;
    if (r->end - r->p < 4) { r->bad = 1; return 0; }
    memcpy(&v, r->p, 4);
    r->p += 4;
    return v;
}
static uint64_t rd_u64(reader *r) {
    uint64_t v = 0;
    if (r->end - r->p < 8) { r->bad = 1; return 0; }
    memcpy(&v, r->p, 8);
    r->p += 8;
    return v;
}
static double rd_f64(reader *r) {
    uint64_t b = rd_u64(r);
    double d;
    memcpy(&d, &b, 8);
    return d;
}

/* ---- output writer ---- */
typedef struct { unsigned char *b; size_t n, cap; } writer;
static void wr(writer *w, const void *p, size_t n) {
    if (w->n + n > w->cap) {
        w->cap = (w->n + n) * 2 + 4096;
        w->b = realloc(w->b, w->cap);
        if (!w->b) ExitProcess(2);
    }
    memcpy(w->b + w->n, p, n);
    w->n += n;
}
static void wr_u32(writer *w, uint32_t v) { wr(w, &v, 4); }
static void wr_i32(writer *w, int32_t v) { wr(w, &v, 4); }
static int write_file(const char *path, const writer *w) {
    FILE *f = fopen(path, "wb");
    if (!f) return 0;
    int ok = fwrite(w->b, 1, w->n, f) == w->n;
    return fclose(f) == 0 && ok;
}

/* ================= view-cache build ================= */
typedef struct {
    uint64_t classes_lo, classes_hi;
} node_info;

static unsigned char **comp;     /* synthetic components */
static node_info *info;
static uint32_t node_count;
static double frame_time;
static uint32_t (*events)[4];
static size_t event_count, event_cap;
static volatile LONG time_mismatch;

static void log_event(uint32_t kind, uint32_t node, uint32_t a, uint32_t b) {
    if (event_count < event_cap) {
        events[event_count][0] = kind;
        events[event_count][1] = node;
        events[event_count][2] = a;
        events[event_count][3] = b;
    }
    ++event_count;
}
static uint32_t tag_of(const void *c) { return *(const uint32_t *)((const unsigned char *)c + C_TAG); }
static uint32_t block_idx(const float *block_field, size_t offset) {
    return *(const uint32_t *)((const unsigned char *)block_field - offset - 16 + 4);
}

static volatile LONG multithreaded_is_class;
static DWORD oracle_thread;
static unsigned char cb_is_class(void *self, int cls) {
    uint32_t n = tag_of(self);
    unsigned c = (unsigned)cls;
    int r = c < 64 ? (int)((info[n].classes_lo >> c) & 1) : c < 128 ? (int)((info[n].classes_hi >> (c - 64)) & 1) : 0;
    /* parallel loading calls IsClass from worker threads (pure in the engine); log only on ours */
    if (GetCurrentThreadId() == oracle_thread) log_event(1, n, c, (uint32_t)r);
    else InterlockedIncrement(&multithreaded_is_class);
    return (unsigned char)r;
}
static void check_time(double t) {
    if (memcmp(&t, &frame_time, sizeof t) != 0) InterlockedIncrement(&time_mismatch);
}
static void cb_pose(void *self, float *block, double time, unsigned char reset, unsigned char incl) {
    check_time(time);
    if (reset != 1 || incl != 1) InterlockedIncrement(&unexpected_calls);
    log_event(2, tag_of(self), block_idx(block, 0), 0);
}
static void cb_scale(void *self, float *scale, double time, unsigned char reset, unsigned char incl) {
    check_time(time);
    if (reset != 1 || incl != 1) InterlockedIncrement(&unexpected_calls);
    log_event(3, tag_of(self), block_idx(scale, 0x40), 0);
}
static void cb_velocity(void *self, float *vel, double time, unsigned char reset, unsigned char incl, void *pose) {
    check_time(time);
    if (reset != 1 || incl != 1) InterlockedIncrement(&unexpected_calls);
    log_event(4, tag_of(self), block_idx(vel, 0x50), pose != NULL);
}
static void cb_connections(void *self, void *vc, void *other) {
    uint32_t idx = (uint32_t)(((unsigned char *)vc - (unsigned char *)self - C_VC) / VC_SIZE);
    log_event(5, tag_of(self), idx, other != NULL);
}

static void *alloc0(size_t n) {
    void *p = _aligned_malloc(n, 64);
    if (!p) ExitProcess(2);
    memset(p, 0, n);
    return p;
}

static void oracle_run_original(void *galaxy, unsigned idx, double time, void *ctx) {
    (void)ctx;
    ((build_cache_fn)(image + RVA_BUILD_CACHE))(galaxy, idx, time);
}

/* probe_dir: run every frame through the in-game probe's hook (xform_probe.c, embedded), which
   patches the call sites of this private copy exactly as in game, captures with and without trace,
   and must restore every byte; captures go to probe_dir. */
static int run_cache(const char *scn_path, const char *res_path, const char *capture_prefix, const char *probe_dir) {
    size_t size = 0;
    unsigned char *buf = read_file(scn_path, &size);
    if (!buf) return 2;
    reader r = {buf, buf + size, 0};
    if (size < 8 || memcmp(buf, "X4XFSCN1", 8) != 0) return 2;
    r.p += 8;
    node_count = rd_u32(&r);
    uint32_t root = rd_u32(&r), frames = rd_u32(&r), first_idx = rd_u32(&r), flags = rd_u32(&r);
    double time0 = rd_f64(&r), dt = rd_f64(&r);
    if (r.bad || node_count == 0 || root >= node_count || node_count > 10000000) return 2;

    void **vtbl = alloc0(VTBL_SLOTS * sizeof(void *));
    for (unsigned i = 0; i < VTBL_SLOTS; ++i) vtbl[i] = (void *)trap;
    vtbl[573] = (void *)cb_is_class;
    vtbl[648] = (void *)cb_pose;
    vtbl[649] = (void *)cb_scale;
    vtbl[650] = (void *)cb_velocity;
    vtbl[622] = (void *)cb_connections;

    comp = calloc(node_count, sizeof *comp);
    info = calloc(node_count, sizeof *info);
    if (!comp || !info) return 2;
    for (uint32_t i = 0; i < node_count; ++i) {
        comp[i] = alloc0(C_SIZE);
        *(void ***)comp[i] = vtbl;
        *(uint32_t *)(comp[i] + C_TAG) = i;
    }
    for (uint32_t i = 0; i < node_count && !r.bad; ++i) {
        unsigned char *c = comp[i];
        uint64_t id = rd_u64(&r);
        memcpy(c + 8, &id, 8);
        int32_t parent = (int32_t)rd_u32(&r);
        if (parent >= (int32_t)node_count) return 2;
        *(void **)(c + C_PARENT) = parent >= 0 ? comp[parent] : NULL;
        *(uint32_t *)(c + C_FLAGS) = rd_u32(&r);
        *(uint32_t *)(c + C_MASK) = rd_u32(&r);
        *(int32_t *)(c + C_COUNT) = (int32_t)rd_u32(&r);
        for (int k = 0; k < 2; ++k) *(uint32_t *)(c + C_VC + k * VC_SIZE + VC_DIRTY) = rd_u32(&r);
        info[i].classes_lo = rd_u64(&r);
        info[i].classes_hi = rd_u64(&r);
        c[C_REGISTERED] = (unsigned char)(rd_u32(&r) != 0);
        *(int32_t *)(c + C_MODE) = (int32_t)rd_u32(&r);
        *(int32_t *)(c + C_ATTENTION) = (int32_t)rd_u32(&r);
        for (int k = 0; k < 2; ++k) *(int32_t *)(c + C_VC + k * VC_SIZE + VC_ATT) = (int32_t)rd_u32(&r);
        for (int k = 0; k < 2; ++k) {
            int32_t p = (int32_t)rd_u32(&r);
            if (p >= (int32_t)node_count) return 2;
            *(void **)(c + C_VC + k * VC_SIZE + VC_PARENT) = p >= 0 ? comp[p] : NULL;
        }
        *(int32_t *)(c + C_REFS) = (int32_t)rd_u32(&r);
        *(int32_t *)(c + C_STATE) = 2;
        *(uint64_t *)(c + C_CONN) = 0x5000 + i;
        for (int k = 0; k < 2; ++k) {
            uint32_t n = rd_u32(&r);
            if (n > node_count) return 2;
            void **v = alloc0((n + 1) * sizeof(void *));
            for (uint32_t j = 0; j < n; ++j) {
                uint32_t ch = rd_u32(&r);
                if (ch >= node_count) return 2;
                v[j] = comp[ch];
            }
            void **kids = (void **)(c + C_VC + k * VC_SIZE + VC_KIDS);
            kids[0] = v;
            kids[1] = v + n;
            kids[2] = v + n;
            /* pose block with a 16-byte header {node, idx} */
            unsigned char *block = alloc0(16 + 0x70);
            *(uint32_t *)block = i;
            *(uint32_t *)(block + 4) = (uint32_t)k;
            *(void **)(c + C_VC + k * VC_SIZE + VC_POSE) = block + 16;
        }
        int32_t nl = (int32_t)rd_u32(&r);
        if (nl >= 0) return 2; /* child containers are not supported (refresh allocates) */
        int32_t na = (int32_t)rd_u32(&r);
        if (na >= 0) {
            /* attachment container: buckets separated by 0xfffffffe, holes 0xffffffff */
            uint32_t *slots = calloc((size_t)na + 1, 4);
            uint32_t buckets = 1, live = 0;
            for (int32_t j = 0; j < na; ++j) {
                slots[j] = rd_u32(&r);
                if (slots[j] == 0xfffffffeu) ++buckets;
                else if (slots[j] != 0xffffffffu) ++live;
            }
            unsigned char *cont = alloc0(0x30);
            void ***bk = alloc0(buckets * 0x20);
            uint32_t b = 0;
            int32_t start = 0;
            for (int32_t j = 0; j <= na; ++j) {
                if (j == na || slots[j] == 0xfffffffeu) {
                    int32_t len = j - start;
                    void **elems = alloc0(((size_t)len + 1) * sizeof(void *));
                    for (int32_t m = 0; m < len; ++m) {
                        uint32_t s = slots[start + m];
                        if (s != 0xffffffffu && s >= node_count) return 2;
                        elems[m] = s == 0xffffffffu ? NULL : comp[s];
                    }
                    bk[b * 4 + 0] = elems;
                    bk[b * 4 + 1] = elems + len;
                    bk[b * 4 + 2] = elems + len;
                    ++b;
                    start = j + 1;
                }
            }
            *(void **)(cont + 0x08) = bk;
            *(void **)(cont + 0x10) = (unsigned char *)bk + buckets * 0x20;
            *(int32_t *)(cont + 0x20) = (int32_t)live;
            *(void **)(c + C_ATTACH) = cont;
            free(slots);
        }
        int32_t ns = (int32_t)rd_u32(&r);
        if (ns >= 0) {
            unsigned char *spec = alloc0(SPEC_SIZE);
            unsigned char *entries = spec;
            uint64_t cap = 4;
            if (ns > 4) {
                cap = (uint64_t)ns + 4;
                entries = alloc0((size_t)cap * ENTRY_SIZE);
                *(void **)spec = entries;
            }
            for (int32_t j = 0; j < ns; ++j) {
                unsigned char *e = entries + (size_t)j * ENTRY_SIZE;
                *(int32_t *)(e + 0) = (int32_t)rd_u32(&r);
                *(uint32_t *)(e + 4) = rd_u32(&r);
                *(uint32_t *)(e + 8) = rd_u32(&r);
                e[0xc] = (unsigned char)(rd_u32(&r) != 0);
                uint64_t iv = rd_u64(&r), last = rd_u64(&r);
                memcpy(e + 0x10, &iv, 8);
                memcpy(e + 0x18, &last, 8);
            }
            *(uint64_t *)(spec + SPEC_CAP) = cap;
            *(uint64_t *)(spec + SPEC_COUNT) = (uint64_t)ns;
            *(void **)(c + C_SPEC) = spec;
        }
    }
    if (r.bad) return 2;
    image[RVA_DROP_POSE_BLOCKS] = (unsigned char)(flags & 1);

    build_cache_fn build_cache = (build_cache_fn)(image + RVA_BUILD_CACHE);
    propagate_fn propagate = (propagate_fn)(image + RVA_PROPAGATE);
    if (probe_dir) {
        if (!MultiByteToWideChar(CP_UTF8, 0, probe_dir, -1, capture_dir, sizeof capture_dir / sizeof capture_dir[0])) return 2;
        /* XFORM_ORACLE_SHADOW=<x4mp_transform_ffi.dll>: shadow every frame instead of capturing */
        /* XFORM_ORACLE_SWITCH=<threads>: our BuildCache runs (with the shadow check every frame) */
        WCHAR shadow_path[400] = L"", switch_text[16] = L"";
        DWORD sl = GetEnvironmentVariableW(L"XFORM_ORACLE_SHADOW", shadow_path, 400);
        DWORD wl = GetEnvironmentVariableW(L"XFORM_ORACLE_SWITCH", switch_text, 16);
        int shadowing = sl > 0 && sl < 400;
        uint32_t switch_threads = wl > 0 && wl < 16 ? (uint32_t)_wtoi(switch_text) : 0;
        /* XFORM_ORACLE_ALTERNATE=1: frames alternate between the original and ours (the in-game A/B mode) */
        WCHAR alternate_text[4] = L"";
        DWORD al = GetEnvironmentVariableW(L"XFORM_ORACLE_ALTERNATE", alternate_text, 4);
        uint32_t switch_alternate = al == 1 && alternate_text[0] == L'1';
        /* XFORM_ORACLE_IMPL=1|2: copy + model (1) or the exact walk (2, default) */
        WCHAR impl_text[4] = L"";
        DWORD il = GetEnvironmentVariableW(L"XFORM_ORACLE_IMPL", impl_text, 4);
        uint32_t switch_impl = il == 1 && impl_text[0] == L'1' ? 1 : 2;
        const char *why = shadowing ? arm(1e300, 0.0, 1, 0, 0, 1, shadow_path, switch_threads, switch_alternate, switch_impl)
                                    : arm(time0, 0.0, frames, 2, 0, 0, NULL, 0, 0, 2);
        if (why) { fprintf(stderr, "xform_oracle: probe refused: %s\n", why); return 3; }
        WCHAR fault_text[4] = L"";
        DWORD fl = GetEnvironmentVariableW(L"XFORM_ORACLE_SHADOW_FAULT", fault_text, 4);
        if (fl == 1 && (fault_text[0] == L'1' || fault_text[0] == L'2')) {
            if (!shadowing || !switch_threads || switch_alternate) return 2;
            shadow_fault_mode = fault_text[0] - L'0';
            clean_shadow_check = shadow_check;
            shadow_check = fault_shadow_check;
        }
    }
    event_cap = 1u << 22;
    events = malloc(event_cap * sizeof *events);
    if (!events) return 2;
    writer w = {0};
    wr(&w, "X4XFRES1", 8);
    wr_u32(&w, frames);
    for (uint32_t k = 0; k < frames; ++k) {
        unsigned idx = (first_idx ^ (k & 1)) & 1;
        frame_time = time0 + (double)k * dt;
        event_count = 0;
        unsigned root_flags = *(uint32_t *)(comp[root] + C_FLAGS) | 0x6bu;
        tls_enter(frame_time);
        if (root_flags) propagate(comp[root], idx, &root_flags);
        if (probe_dir) {
            /* enter exactly as UniverseFrameUpdate does: through the patched rel32 of 0x9fe549 */
            int32_t rel;
            memcpy(&rel, image + SITE_FRAME + 1, 4);
            xc_build_cache_fn via = (xc_build_cache_fn)(image + SITE_FRAME + 5 + rel);
            if ((unsigned char *)via != stub_page) { fprintf(stderr, "xform_oracle: frame site not redirected\n"); return 4; }
            via(comp[root], idx, frame_time);
            tls_leave();
        } else if (capture_prefix) {
            static xc_capture cap;
            static xc_buf buf;
            xc_meta meta = {k, k, 0};
            int ran = 0;
            const char *why = xc_capture_frame(&cap, &buf, comp[root], idx, frame_time, &meta, oracle_run_original, NULL, &ran);
            if (!ran) build_cache(comp[root], idx, frame_time);
            tls_leave();
            if (why) { fprintf(stderr, "xform_oracle: capture %u: %s\n", k, why); return 2; }
            char path[4096];
            snprintf(path, sizeof path, "%s_%03u.cap", capture_prefix, k);
            writer cw = {buf.b, buf.n, buf.cap};
            if (!write_file(path, &cw)) return 2;
        } else {
            build_cache(comp[root], idx, frame_time);
            tls_leave();
        }
        if (event_count > event_cap) { fprintf(stderr, "xform_oracle: event buffer overflow\n"); return 2; }
        wr_u32(&w, (uint32_t)event_count);
        wr(&w, events, event_count * sizeof *events);
    }
    if (probe_dir) {
        owner *o = current;
        uint64_t got = o ? o->captures : 0, failed = o ? o->failures : 0;
        const char *skip = o && o->last_skip ? o->last_skip : "";
        disarm(o);
        current = NULL;
        int32_t now;
        memcpy(&now, image + SITE_FRAME + 1, 4);
        unsigned char d[32];
        int restored = !frame_site.patched && now == frame_site.original_rel && !trace_sites[0].patched &&
                       !trace_sites[1].patched && !trace_sites[2].patched &&
                       sha256(image + 0x3d4a80u, 1792, d) && digest_matches(d, code_checks[0].sha) &&
                       sha256(image + 0x9fe500u, 128, d) && digest_matches(d, code_checks[2].sha);
        if (o && o->switch_threads) {
            printf("SWITCH impl=%u threads=%u frames=%llu fallbacks=%llu components=%llu tasks=%llu refusal=%s\n",
                   o->switch_impl, o->switch_threads, (unsigned long long)o->switch_frames, (unsigned long long)o->switch_fallbacks,
                   (unsigned long long)o->switch_components, (unsigned long long)o->switch_tasks, o->switch_refusal);
            /* a refusal falls back to the original for that frame: allowed, reported, and the final
               state is still compared with the original run */
            uint32_t ours = frames;
            if (o->switch_alternate)
                for (uint32_t k = ours = 0; k < frames; ++k) ours += (k >> 1) & 1;
            if (o->switch_frames + o->switch_fallbacks != (shadow_fault_mode ? 1u : ours)) return 4;
        }
        if (o && o->shadow_every) {
            int clean = o->shadow_frames == frames && !o->shadow_bad_frames && !o->shadow_errors && restored;
            printf("SHADOW frames=%llu bad=%llu mismatches=%llu refs=%llu errors=%llu components=%llu switched=%llu first=%s\n",
                   (unsigned long long)o->shadow_frames, (unsigned long long)o->shadow_bad_frames,
                   (unsigned long long)o->shadow_mismatches, (unsigned long long)o->shadow_refs,
                   (unsigned long long)o->shadow_errors, (unsigned long long)o->shadow_components,
                   (unsigned long long)o->shadow_switch_frames, o->shadow_first);
            if (shadow_fault_mode) {
                clean = shadow_fault_calls == 1 && o->frames == 1 && o->switch_frames == 1 &&
                        !o->switch_fallbacks && !o->armed && o->reason && restored &&
                        o->shadow_bad_frames == (uint64_t)(shadow_fault_mode == 1) &&
                        o->shadow_errors == (uint64_t)(shadow_fault_mode == 2);
                printf("FAILCLOSED mode=%d checks=%u hook_frames=%llu switched=%llu restored=%d verdict=%s\n",
                       shadow_fault_mode, shadow_fault_calls, (unsigned long long)o->frames,
                       (unsigned long long)o->switch_frames, restored, clean ? "pass" : "fail");
            }
            if (!clean) return 4;
            got = frames;
        }
        if (got != frames || failed || !restored) {
            fprintf(stderr, "xform_oracle: probe captured %llu of %u frames, %llu failures (%s), code restored=%d\n",
                    (unsigned long long)got, frames, (unsigned long long)failed, skip, restored);
            return 4;
        }
    }
    for (uint32_t i = 0; i < node_count; ++i) {
        unsigned char *c = comp[i];
        wr_u32(&w, *(uint32_t *)(c + C_FLAGS));
        wr_i32(&w, *(int32_t *)(c + C_COUNT));
        for (int k = 0; k < 2; ++k) wr_u32(&w, *(uint32_t *)(c + C_VC + k * VC_SIZE + VC_DIRTY));
        for (int k = 0; k < 2; ++k) wr_i32(&w, *(int32_t *)(c + C_VC + k * VC_SIZE + VC_ATT));
        for (int k = 0; k < 2; ++k) {
            unsigned char *p = *(unsigned char **)(c + C_VC + k * VC_SIZE + VC_PARENT);
            wr_i32(&w, p ? (int32_t)tag_of(p) : -1);
        }
        wr_i32(&w, *(int32_t *)(c + C_REFS));
        unsigned char *spec = *(unsigned char **)(c + C_SPEC);
        if (!spec) {
            wr_i32(&w, -1);
            continue;
        }
        uint64_t n = *(uint64_t *)(spec + SPEC_COUNT);
        unsigned char *entries = *(uint64_t *)(spec + SPEC_CAP) == 4 ? spec : *(unsigned char **)spec;
        wr_i32(&w, (int32_t)n);
        for (uint64_t j = 0; j < n; ++j) {
            unsigned char *e = entries + j * ENTRY_SIZE;
            wr(&w, e, 12);
            wr_u32(&w, e[0xc]);
            wr(&w, e + 0x10, 16);
        }
    }
    if (unexpected_calls || time_mismatch) {
        fprintf(stderr, "xform_oracle: %ld unexpected virtual calls, %ld getter time mismatches\n",
                (long)unexpected_calls, (long)time_mismatch);
        return 4;
    }
    return write_file(res_path, &w) ? 0 : 2;
}

/* ================= pose math ================= */
typedef struct {
    void **vtbl;
    uint32_t node;
    uint32_t pad;
    _Alignas(16) float local[16];
} fake_controller;

static fake_controller *controllers;

static void cb_controller_transform(fake_controller *ctrl, float *mat, double time, unsigned char reset) {
    (void)time;
    if (reset != 1) {
        InterlockedIncrement(&unexpected_calls);
        return;
    }
    memcpy(mat, ctrl->local, sizeof ctrl->local);
}
static void cb_child_frame(void *parent, float *mat) {
    (void)parent;
    (void)mat;
}

static int run_pose(const char *in_path, const char *out_path) {
    size_t size = 0;
    unsigned char *buf = read_file(in_path, &size);
    if (!buf) return 2;
    reader r = {buf, buf + size, 0};
    if (size < 8 || memcmp(buf, "X4XFPOS1", 8) != 0) return 2;
    r.p += 8;
    uint32_t cases = rd_u32(&r);
    void **comp_vtbl = alloc0(VTBL_SLOTS * sizeof(void *));
    void **ctrl_vtbl = alloc0(64 * sizeof(void *));
    for (unsigned i = 0; i < VTBL_SLOTS; ++i) comp_vtbl[i] = (void *)trap;
    for (unsigned i = 0; i < 64; ++i) ctrl_vtbl[i] = (void *)trap;
    comp_vtbl[648] = image + RVA_LOCAL_TRANSFORM;
    comp_vtbl[676] = (void *)cb_child_frame;
    ctrl_vtbl[6] = (void *)cb_controller_transform;
    local_transform_fn local_transform = (local_transform_fn)(image + RVA_LOCAL_TRANSFORM);
    relative_fn relative = (relative_fn)(image + RVA_RELATIVE);
    enum { MAX_NODES = 16 };
    unsigned char *nodes[MAX_NODES];
    controllers = alloc0(MAX_NODES * sizeof *controllers);
    for (int i = 0; i < MAX_NODES; ++i) {
        nodes[i] = alloc0(C_SIZE);
        *(void ***)nodes[i] = comp_vtbl;
        controllers[i].vtbl = ctrl_vtbl;
        controllers[i].node = (uint32_t)i;
        *(void **)(nodes[i] + C_CONTROLLER) = &controllers[i];
    }
    writer w = {0};
    wr(&w, "X4XFPOR1", 8);
    wr_u32(&w, cases);
    _Alignas(16) float mat[16];
    for (uint32_t k = 0; k < cases && !r.bad; ++k) {
        uint32_t kind = rd_u32(&r), n = rd_u32(&r);
        if (n < 1 || n > MAX_NODES) return 2;
        for (uint32_t i = 0; i < n; ++i) {
            int32_t parent = (int32_t)rd_u32(&r);
            if (parent >= (int32_t)n) return 2;
            *(void **)(nodes[i] + C_PARENT) = parent >= 0 ? nodes[parent] : NULL;
            for (int j = 0; j < 16; ++j) {
                uint32_t b = rd_u32(&r);
                memcpy(&controllers[i].local[j], &b, 4);
            }
        }
        uint32_t node = rd_u32(&r);
        int32_t target = (int32_t)rd_u32(&r);
        for (int j = 0; j < 16; ++j) {
            uint32_t b = rd_u32(&r);
            memcpy(&mat[j], &b, 4);
        }
        if (r.bad || node >= n || target >= (int32_t)n) return 2;
        tls_enter(0.0);
        if (kind == 1)
            local_transform(nodes[node], mat, 0.0, 0, 1);
        else
            relative(nodes[node], mat, 0.0, target >= 0 ? nodes[target] : NULL, 0);
        tls_leave();
        wr(&w, mat, sizeof mat);
    }
    if (r.bad) return 2;
    if (unexpected_calls) {
        fprintf(stderr, "xform_oracle: %ld unexpected virtual calls\n", (long)unexpected_calls);
        return 4;
    }
    return write_file(out_path, &w) ? 0 : 2;
}

int main(int argc, char **argv) {
    int capture = argc == 6 && strcmp(argv[1], "capture") == 0;
    int probe = argc == 6 && strcmp(argv[1], "probe") == 0;
    if (!capture && !probe && (argc != 5 || (strcmp(argv[1], "cache") != 0 && strcmp(argv[1], "pose") != 0))) {
        fprintf(stderr, "usage: xform_oracle cache|pose <X4.exe> <input> <output>\n"
                        "       xform_oracle capture <X4.exe> <scenario> <result> <capture prefix>\n"
                        "       xform_oracle probe <X4.exe> <scenario> <result> <capture dir>\n");
        return 2;
    }
    AddVectoredExceptionHandler(1, on_fault);
    oracle_thread = GetCurrentThreadId();
    int rc = map_image(argv[2]);
    if (rc) return rc;
    if (capture) rc = run_cache(argv[3], argv[4], argv[5], NULL);
    else if (probe) rc = run_cache(argv[3], argv[4], NULL, argv[5]);
    else rc = strcmp(argv[1], "cache") == 0 ? run_cache(argv[3], argv[4], NULL, NULL) : run_pose(argv[3], argv[4]);
    if (rc) fprintf(stderr, "xform_oracle: %s failed (%d)\n", argv[1], rc);
    return rc;
}
