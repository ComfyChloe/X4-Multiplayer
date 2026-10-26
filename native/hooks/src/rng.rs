//! Role-keyed seeding and exact draw accounting for X4's per-thread RNG streams.
//!
//! Windows X4 9.00, Steam build 23660954 only. Every engine thread owns one u64
//! LCG state at TLS block + 0x318, seeded at thread start from `_time64 + thread id`
//! (`Random::SeedThreadState`, 0x14dd110). The dedicated simulation threads are all
//! created once in `GameClient::Init`, long before this module loads, so their start
//! hook cannot be used. Instead each stream is bound to its engine singleton, verified
//! through the thread object that `XLib::ThreadEntryTrampoline` stores at TLS + 0x320,
//! and written exactly once at a pass boundary where the owning thread provably cannot
//! draw:
//!
//! * MoveWorker: on its own thread at observer registration (unchanged Stage 1 value).
//! * PathingManager: after the per-frame join in the universe update (`+0x88 == 1`).
//! * MassTraffic, RegionManager, Gravidar: under the engine's own handshake critical
//!   section while the worker's `ready` byte is set (finished, not yet re-kicked).
//! * JobThread/NN: under the job-manager lock while the queue is empty and no worker
//!   reports a running job category.
//!
//! The step `s' = ror64(s * A + C, 30)` is a bijection, so the exact number of draws
//! between two observed states is recovered by walking forward from the earlier one.
//! No engine code is patched; the only engine writes are the role seeds and two
//! optional, verified-empty VBI slot pointers used to split MoveWorker draws by phase.
#![allow(static_mut_refs)]

use core::ffi::{c_char, c_int, c_void};
use std::fmt::Write as _;
use std::ptr;
use std::sync::atomic::{AtomicPtr, Ordering};

use crate::{
    api, context_valid, failure, hex64, method, readable, role_seed, table_now,
    thread_block, GetCurrentThreadId, GetModuleHandleW, GetProcAddress, LeaveCriticalSection,
    LuaState, TryEnterCriticalSection, CURRENT, IMAGE, OWNER_THREAD, REGISTERED_TABLE,
};

pub(crate) const MULTIPLIER: u64 = 0x5851_f42d_4c95_7f2d;
pub(crate) const INCREMENT: u64 = 0x1405_7b7e_f767_814f;
const TLS_GAME_TIME: usize = 0x300;
const TLS_RNG: usize = 0x318;
const TLS_THREAD: usize = 0x320;
const TLS_SPAN: usize = 0x330;
const WALK_CAP: u64 = 1 << 21;
const TICK_HISTORY: usize = 64;

const JOB_MANAGER_RVA: usize = 0x6cf1540;
const JOB_VTABLE_RVA: usize = 0x2cabca8;
const JOB_COUNT: usize = 0x528;
const JOB_RUNNABLES: usize = 0x28;
const JOB_STRIDE: usize = 0x20;
const JOB_THREADS: usize = 0x428;
const JOB_LOCK: usize = 0x540;
const JOB_QUEUED: usize = 0x588;
const JOB_SPAN: usize = 0x590;
const JOB_BUSY: usize = 0x18;
const JOB_OWNER: usize = 0x10;
const JOB_INDEX: usize = 0x1c;
const MAX_JOBS: usize = 32;
const ROLES: usize = 5;
const PHASE_SLOTS: [usize; 2] = [2, 5];

/// Build-pinned code/vtable bytes whose layouts this module relies on. Each entry is
/// also listed in `signatures.json`; `tests/test_rng_streams.py` keeps them in sync.
const CHECKS: &[(usize, usize, &str)] = &[
    (0x14dde20, 97, "b472ced0db2e62425089baf6b0ef72b29b258f3a8a34fdabcf98b389171121b7"), // rng_tls_u64_step
    (0x14b7680, 146, "f75e2551ab0f2693144c5a10c0c634af5bac8d4b372200f21999881c6ba7377c"), // thread_start_layout
    (0x14b7740, 125, "cc0621b1d70386cded5da0181270d6ac97940f66e3c6f068b12fe62fc7093ba9"), // thread_entry_tls_owner
    (0x1226734, 48, "e8d55bbebc1a7d5d69f43d79c5a422b003bde0d52948415348a4a72ed10179bb"), // game_client_worker_starts
    (0xef4390, 97, "d4e9378cc06bf387f4b2157dd337b8b3c153fc6811e41b907090c7e35e51d48c"), // move_worker_create_layout
    (0xef4777, 210, "aff75c93a8aeaa56543cf864c39b2d280a540eadeddf97a380dd3a76f1ca9a3e"), // move_worker_frame_end_kicks
    (0x8c06a0, 33, "f89791ed5f7a8338f3ea5ec8a4d85e9219dc0694ded9ffc8616b575bbcc279f0"), // masstraffic_start_layout
    (0x1233310, 65, "38a32e4995b93fc00cb7c78b4835966b67413572f5c84b6d6330b6f3ac023d15"), // masstraffic_singleton
    (0x8c0781, 192, "15ef507a596778e7b010b06d7b294aa6f1130562ef690be61758a104bba8b992"), // masstraffic_handshake
    (0xef3080, 43, "d742fdb9f97a6cae56734eacae1600fdd7b77810efb9a9b797855161365083b2"), // region_start_layout
    (0x1233060, 65, "35eb68394cb1aeddffde8575ad16c3970fe17557608bbd3d4610eb640cf6b1da"), // region_singleton
    (0xef315a, 164, "d447437fae7340f7c442612f4a73b10de1953062c0d057f9e2319a359419e3c6"), // region_handshake
    (0x10ae0c0, 27, "4902c0b6be7bd97264dced0ebef77ca525d890e8ffbfe93223cc654175cae275"), // gravidar_start_layout
    (0x10ae1cf, 163, "706b54b5fe113cf95771ca4d82773a8c4cdc3f04a3946a8ef18b865dc294a980"), // gravidar_handshake
    (0x10b12c0, 135, "4d48cacb6e9a84f822b24b9174e6f16d9a7d17ea8c0199af2b18f180a12a832d"), // gravidar_publish_kick
    (0xeabee0, 30, "b0488b7dda75cc7de66b8eacbf70fca822599d03566b435e36c9c03353bb62e1"), // pathing_start_layout
    (0xeabf85, 183, "972cd302597e94e5bb83ec909453904acb7d73e43569cc325e501efd720cb7d6"), // pathing_wait_loop
    (0xeac107, 33, "8c969e87489a2b30770ac9b0138727f3ed4efb8b52348a1b46e73ac50452491b"), // pathing_signal_tail
    (0xeac170, 57, "6d41c65081f0ede03920a9d4118242b26d2de95a6ab61f54320aa7586b1cd178"), // pathing_join_flag
    (0x14bc0a4, 552, "63dc9c70b6730a3e1e9b0f5e0d734b8204dc0e5b6b9b4d683ffb8edc87420d3b"), // jobthread_run_busy
    (0x14bd0c1, 129, "182023b818d2cba24738c5f6b3bd4c38ca4043d844ac75e34f926e2a0710ac77"), // jobpool_create_layout
    (0x14bbfc0, 34, "01a6973738c9502d33d2c1ab7a68b6f54fd3169cce25ab51a7ccd5e8a742c41d"), // jobthread_record_ctor
    (0x14bcd25, 70, "e506262005c124ed8872e4a716b265c2a5e59f209b5c67357700b9ea6d3ca2b4"), // jobpool_record_owner_index
    (0x2c49b00, 16, "527c59503b03ce70005bc396e1d277337d12721c46e37cb4f33e0b92659fe794"), // vtable_move_worker_run
    (0x2c4f560, 16, "b3bdb73e69b626c8c57a58fb2022e6560ab08375ba13d0b0166884b06a22e7f4"), // vtable_pathing_run
    (0x2b96c70, 16, "0d028d3cdd8d7fa4144edc8649a7b3296cc25244000aba072650094ea17f02d1"), // vtable_masstraffic_run
    (0x2c49cd0, 16, "ca710fbb2b104314e64faec3cf7e2b2168635f334facc28505bc6b11ad7b8efe"), // vtable_region_run
    (0x2c68860, 16, "c34636c301e4504bf624cce02124f3090d1b312491eaa4dec44cdce48a8e36dc"), // vtable_gravidar_run
    (0x2cabca8, 16, "ec6f7db6766a2bcc360bc1860a58451c15e3744ad9e37e162fc21f28a8944320"), // vtable_jobthread_run
];

#[link(name = "kernel32")]
extern "system" {
    fn OpenThread(access: u32, inherit: c_int, thread_id: u32) -> *mut c_void;
    fn CloseHandle(handle: *mut c_void) -> c_int;
}

type NtQueryInformationThread =
    unsafe extern "system" fn(*mut c_void, u32, *mut c_void, u32, *mut u32) -> i32;
type Resolver = unsafe fn(u32) -> usize;

pub(crate) fn step(state: u64) -> u64 {
    state
        .wrapping_mul(MULTIPLIER)
        .wrapping_add(INCREMENT)
        .rotate_right(30)
}

/// Exact number of draws that turn `from` into `to`, or `None` beyond `cap` steps.
pub(crate) fn steps_between(from: u64, to: u64, cap: u64) -> Option<u64> {
    let mut state = from;
    for count in 0..=cap {
        if state == to {
            return Some(count);
        }
        state = step(state);
    }
    None
}

#[derive(Clone, Copy)]
enum Gate {
    Owner,
    Joined { joined: usize, state: usize },
    Handshake { lock: usize, ready: usize },
}

struct Spec {
    token: &'static str,
    role: &'static [u8],
    singleton: usize,
    runnable: usize,
    vtable: usize,
    thread: usize,
    span: usize,
    gate: Gate,
}

/// Fixed seeding/observation order. `role` is the seed key; `token` the log key.
const SPECS: [Spec; ROLES] = [
    Spec {
        token: "mw",
        role: b"MoveWorker",
        singleton: 0x38f4ee8,
        runnable: 0,
        vtable: 0x2c49b00,
        thread: 0x18,
        span: 0x38,
        gate: Gate::Owner,
    },
    Spec {
        token: "path",
        role: b"PathingManager",
        singleton: 0x6cf21c8,
        runnable: 0,
        vtable: 0x2c4f560,
        thread: 0x20,
        span: 0x90,
        gate: Gate::Joined {
            joined: 0x88,
            state: 0x84,
        },
    },
    Spec {
        token: "mt",
        role: b"MassTraffic",
        singleton: 0x6cf23e0,
        runnable: 0,
        vtable: 0x2b96c70,
        thread: 0xb0,
        span: 0xd0,
        gate: Gate::Handshake {
            lock: 0x60,
            ready: 0x88,
        },
    },
    Spec {
        token: "rm",
        role: b"RegionManager",
        singleton: 0x6cf21e0,
        runnable: 0x20,
        vtable: 0x2c49cd0,
        thread: 0x168,
        span: 0x190,
        gate: Gate::Handshake {
            lock: 0x118,
            ready: 0x140,
        },
    },
    Spec {
        token: "grav",
        role: b"Gravidar",
        singleton: 0x6cf1650,
        runnable: 0,
        vtable: 0x2c68860,
        thread: 0x28,
        span: 0x69b0,
        gate: Gate::Handshake {
            lock: 0x6960,
            ready: 0x6988,
        },
    },
];

#[derive(Clone, Copy)]
struct Stream {
    bound: bool,
    object: usize,
    tls: usize,
    thread: usize,
    seeded: bool,
    seed_frame: u64,
    steps: u64,
    last: u64,
    passes: u64,
    pass_time: u64,
    lost: bool,
    busy: bool,
}

const EMPTY: Stream = Stream {
    bound: false,
    object: 0,
    tls: 0,
    thread: 0,
    seeded: false,
    seed_frame: 0,
    steps: 0,
    last: 0,
    passes: 0,
    pass_time: 0,
    lost: false,
    busy: false,
};

#[repr(C)]
struct Phase {
    vtable: *const *const c_void,
    index: usize,
}

static mut STREAMS: [Stream; ROLES + MAX_JOBS] = [EMPTY; ROLES + MAX_JOBS];
static mut JOBS: usize = 0;
static mut JOB_MANAGER: usize = 0;
static mut ARMED: bool = false;
static mut OWNER: u32 = 0;
static mut SESSION: u64 = 0;
static mut FRAME: u64 = 0;
static mut LAST_END: u64 = 0;
/// MoveWorker draws: a = slots 13..16 + pre-pass + slots 0..1, b = slots 3..4
/// (GameClient/universe update, input), c = slots 6..11 (UI .. JobEngine),
/// o = this module's own observer callback, x = unattributed (phases unavailable).
static mut PHASE_STEPS: [u64; 5] = [0; 5];
static mut PHASE_SNAP: [(u64, u64); 2] = [(0, u64::MAX); 2];
static mut TICK_GEN: u64 = 0;
static mut PHASE_INSTALLED: [bool; 2] = [false; 2];
static mut PHASE_VTABLE: [*const c_void; 3] = [ptr::null(); 3];
static mut PHASES: [Phase; 2] = [
    Phase {
        vtable: ptr::null(),
        index: 0,
    },
    Phase {
        vtable: ptr::null(),
        index: 1,
    },
];
static mut TICKS: [u64; TICK_HISTORY] = [0; TICK_HISTORY];
static mut TICK_COUNT: usize = 0;
static mut TICK_FIRST: u64 = 0;
static mut TICK_DROPPED: bool = false;
static mut RESOLVER: Resolver = teb_tls_block;

unsafe fn verify() -> bool {
    CHECKS.iter().all(|&(rva, length, expected)| {
        crate::site_matches(rva, length, expected)
    })
}

/// TLS block of another thread in this process: TEB -> ThreadLocalStoragePointer[0].
unsafe fn teb_tls_block(thread_id: u32) -> usize {
    let name: Vec<u16> = "ntdll.dll\0".encode_utf16().collect();
    let ntdll = GetModuleHandleW(name.as_ptr());
    if ntdll.is_null() {
        return 0;
    }
    let query = GetProcAddress(ntdll, b"NtQueryInformationThread\0".as_ptr());
    if query.is_null() {
        return 0;
    }
    let query: NtQueryInformationThread = core::mem::transmute(query);
    let handle = OpenThread(0x0040 | 0x0800, 0, thread_id);
    if handle.is_null() {
        return 0;
    }
    let mut info = [0u64; 6];
    let mut returned = 0u32;
    let status = query(handle, 0, info.as_mut_ptr() as *mut c_void, 48, &mut returned);
    CloseHandle(handle);
    if status != 0 || returned < 32 || info[3] != thread_id as u64 {
        return 0;
    }
    let teb = info[1] as usize;
    if !readable((teb + 0x58) as *const c_void, 8, false) {
        return 0;
    }
    let array = ptr::read((teb + 0x58) as *const usize);
    if !readable(array as *const c_void, 8, false) {
        return 0;
    }
    ptr::read(array as *const usize)
}

/// Resolves and verifies one thread's TLS block from its XLib thread object.
unsafe fn bind_thread(runnable: usize, vtable_rva: usize, thread: usize) -> Option<usize> {
    if !readable(runnable as *const c_void, 0x20, false)
        || ptr::read(runnable as *const usize) != IMAGE + vtable_rva
        || ptr::read((runnable + 8) as *const usize) != thread
        || !readable(thread as *const c_void, 0x18, false)
        || ptr::read((thread + 0x14) as *const i32) != 1
    {
        return None;
    }
    let thread_id = ptr::read((thread + 0x10) as *const u32);
    if thread_id == 0 {
        return None;
    }
    let tls = RESOLVER(thread_id);
    if !readable(tls as *const c_void, TLS_SPAN, true)
        || ptr::read((tls + TLS_THREAD) as *const usize) != thread
    {
        return None;
    }
    Some(tls)
}

unsafe fn bind_role(index: usize) -> Stream {
    let spec = &SPECS[index];
    let slot = IMAGE + spec.singleton;
    if !readable(slot as *const c_void, 8, false) {
        return EMPTY;
    }
    let object = ptr::read(slot as *const usize);
    if !readable(object as *const c_void, spec.span, false) {
        return EMPTY;
    }
    let thread = object + spec.thread;
    match bind_thread(object + spec.runnable, spec.vtable, thread) {
        Some(tls) => Stream {
            bound: true,
            object,
            tls,
            thread,
            ..EMPTY
        },
        None => EMPTY,
    }
}

unsafe fn bind_pool() {
    JOBS = 0;
    JOB_MANAGER = 0;
    let slot = IMAGE + JOB_MANAGER_RVA;
    if !readable(slot as *const c_void, 8, false) {
        return;
    }
    let manager = ptr::read(slot as *const usize);
    if !readable(manager as *const c_void, JOB_SPAN, false) {
        return;
    }
    let count = ptr::read((manager + JOB_COUNT) as *const u32) as usize;
    if !(1..=MAX_JOBS).contains(&count) {
        return;
    }
    for index in 0..count {
        let runnable = manager + JOB_RUNNABLES + index * JOB_STRIDE;
        let thread = ptr::read((manager + JOB_THREADS + index * 8) as *const usize);
        if ptr::read((runnable + JOB_OWNER) as *const usize) != manager
            || ptr::read((runnable + JOB_INDEX) as *const u32) as usize != index
        {
            continue;
        }
        if let Some(tls) = bind_thread(runnable, JOB_VTABLE_RVA, thread) {
            STREAMS[ROLES + index] = Stream {
                bound: true,
                object: runnable,
                tls,
                thread,
                ..EMPTY
            };
        }
    }
    JOBS = count;
    JOB_MANAGER = manager;
}

fn job_role(index: usize) -> [u8; 12] {
    let mut role = *b"JobThread/00";
    role[10] = b'0' + (index / 10) as u8;
    role[11] = b'0' + (index % 10) as u8;
    role
}

unsafe fn intact(stream: &Stream) -> bool {
    readable(stream.tls as *const c_void, TLS_SPAN, true)
        && ptr::read((stream.tls + TLS_THREAD) as *const usize) == stream.thread
}

/// Seeds or counts one quiescent stream. Caller guarantees its thread cannot draw.
unsafe fn observe(index: usize, role: &[u8]) {
    let stream = &mut STREAMS[index];
    if !intact(stream) {
        stream.bound = false;
        return;
    }
    let state_slot = (stream.tls + TLS_RNG) as *mut u64;
    let time = ptr::read((stream.tls + TLS_GAME_TIME) as *const u64);
    if !stream.seeded {
        let seed = role_seed(SESSION, role);
        ptr::write_volatile(state_slot, seed);
        stream.seeded = true;
        stream.seed_frame = FRAME;
        stream.last = seed;
        stream.steps = 0;
        stream.passes = 0;
        stream.pass_time = time;
    } else {
        let state = ptr::read_volatile(state_slot);
        if state != stream.last {
            match steps_between(stream.last, state, WALK_CAP) {
                Some(count) => stream.steps += count,
                None => stream.lost = true,
            }
            stream.last = state;
        }
        if time != stream.pass_time {
            stream.passes += 1;
            stream.pass_time = time;
        }
    }
    stream.busy = false;
}

unsafe fn observe_role(index: usize) {
    if !STREAMS[index].bound {
        return;
    }
    let spec = &SPECS[index];
    let object = STREAMS[index].object;
    match spec.gate {
        Gate::Owner => {}
        Gate::Joined { joined, state } => {
            if ptr::read((object + joined) as *const u8) == 1
                && ptr::read((object + state) as *const i32) == 2
            {
                observe(index, spec.role);
            } else {
                STREAMS[index].busy = true;
            }
        }
        Gate::Handshake { lock, ready } => {
            let lock = (object + lock) as *mut c_void;
            if TryEnterCriticalSection(lock) == 0 {
                STREAMS[index].busy = true;
                return;
            }
            if ptr::read((object + ready) as *const u8) == 1 {
                observe(index, spec.role);
            } else {
                STREAMS[index].busy = true;
            }
            LeaveCriticalSection(lock);
        }
    }
}

unsafe fn observe_pool() {
    if JOB_MANAGER == 0 {
        return;
    }
    let manager = JOB_MANAGER;
    let lock = (manager + JOB_LOCK) as *mut c_void;
    let mut quiet = TryEnterCriticalSection(lock) != 0;
    let locked = quiet;
    if quiet && ptr::read((manager + JOB_QUEUED) as *const u64) != 0 {
        quiet = false;
    }
    for index in 0..JOBS {
        let runnable = manager + JOB_RUNNABLES + index * JOB_STRIDE;
        if quiet && ptr::read_volatile((runnable + JOB_BUSY) as *const u32) != 0 {
            quiet = false;
        }
    }
    for index in 0..JOBS {
        let slot = ROLES + index;
        if !STREAMS[slot].bound {
            continue;
        }
        if quiet {
            observe(slot, &job_role(index));
        } else {
            STREAMS[slot].busy = true;
        }
    }
    if locked {
        LeaveCriticalSection(lock);
    }
}

fn account(from: u64, to: u64, phase: usize) -> bool {
    match steps_between(from, to, WALK_CAP) {
        Some(count) => {
            unsafe {
                PHASE_STEPS[phase] += count;
                STREAMS[0].steps += count;
            }
            true
        }
        None => false,
    }
}

unsafe fn record_tick(delta: u64) {
    if TICK_COUNT == 0 {
        TICK_FIRST = FRAME;
    }
    if TICK_COUNT == TICK_HISTORY {
        TICKS.copy_within(1.., 0);
        TICK_COUNT -= 1;
        TICK_FIRST += 1;
        TICK_DROPPED = true;
    }
    TICKS[TICK_COUNT] = delta;
    TICK_COUNT += 1;
}

/// Called once per observer frame on MoveWorker (VBI slot 12) before the callback.
pub(crate) unsafe fn tick(frame: u64) {
    if !ARMED || GetCurrentThreadId() != OWNER {
        return;
    }
    FRAME = frame;
    let tls = thread_block() as usize;
    if tls != STREAMS[0].tls || !intact(&STREAMS[0]) {
        disarm();
        return;
    }
    let now = ptr::read((tls + TLS_RNG) as *const u64);
    let before = STREAMS[0].steps;
    let fresh = PHASE_SNAP.iter().all(|&(_, generation)| generation == TICK_GEN);
    let counted = if fresh {
        let (a, b) = (PHASE_SNAP[0].0, PHASE_SNAP[1].0);
        account(LAST_END, a, 0) && account(a, b, 1) && account(b, now, 2)
    } else {
        account(LAST_END, now, 4)
    };
    if !counted {
        STREAMS[0].lost = true;
    }
    LAST_END = now;
    STREAMS[0].last = now;
    record_tick(STREAMS[0].steps - before);
    TICK_GEN += 1;
    for index in 1..ROLES {
        observe_role(index);
    }
    observe_pool();
}

/// Attributes draws made by this module's own Lua callback to phase `o`.
pub(crate) unsafe fn after_callback() {
    if !ARMED || GetCurrentThreadId() != OWNER {
        return;
    }
    let now = ptr::read((STREAMS[0].tls + TLS_RNG) as *const u64);
    if now != LAST_END {
        let before = STREAMS[0].steps;
        if !account(LAST_END, now, 3) {
            STREAMS[0].lost = true;
        }
        if let Some(last) = TICKS[..TICK_COUNT].last_mut() {
            *last += STREAMS[0].steps - before;
        }
        LAST_END = now;
        STREAMS[0].last = now;
    }
}

/// Verifies signatures and every role binding, seeds MoveWorker immediately and
/// leaves the other streams pending until their first quiescent observation.
pub(crate) unsafe fn arm(session_seed: u64) -> Result<(), &'static str> {
    disarm();
    STREAMS = [EMPTY; ROLES + MAX_JOBS];
    PHASE_STEPS = [0; 5];
    PHASE_SNAP = [(0, u64::MAX); 2];
    TICK_GEN = 0;
    TICK_COUNT = 0;
    TICK_DROPPED = false;
    FRAME = 0;
    if !verify() {
        return Err("RNG stream signatures differ");
    }
    let moveworker = bind_role(0);
    let tls = thread_block() as usize;
    if !moveworker.bound
        || moveworker.tls != tls
        || ptr::read((moveworker.thread + 0x10) as *const u32) != GetCurrentThreadId()
    {
        return Err("MoveWorker RNG binding refused");
    }
    OWNER = GetCurrentThreadId();
    SESSION = session_seed;
    let seed = role_seed(session_seed, SPECS[0].role);
    ptr::write((tls + TLS_RNG) as *mut u64, seed);
    STREAMS[0] = Stream {
        seeded: true,
        last: seed,
        pass_time: ptr::read((tls + TLS_GAME_TIME) as *const u64),
        ..moveworker
    };
    LAST_END = seed;
    for index in 1..ROLES {
        STREAMS[index] = bind_role(index);
    }
    bind_pool();
    ARMED = true;
    Ok(())
}

pub(crate) unsafe fn disarm() {
    uninstall_phases();
    ARMED = false;
}

extern "C" fn phase_no_op(_: *mut Phase) {}

extern "C" fn phase_move(this: *mut Phase) {
    unsafe {
        if !ARMED || GetCurrentThreadId() != OWNER {
            return;
        }
        let index = (*this).index;
        if index < PHASE_SLOTS.len() {
            PHASE_SNAP[index] = (
                ptr::read((STREAMS[0].tls + TLS_RNG) as *const u64),
                TICK_GEN,
            );
        }
    }
}

unsafe fn uninstall_phases() {
    let table = REGISTERED_TABLE;
    for (index, slot) in PHASE_SLOTS.iter().enumerate() {
        if !PHASE_INSTALLED[index] {
            continue;
        }
        if !table.is_null()
            && table_now() == table
            && readable(table as *const c_void, 0x138, true)
        {
            let _ = (&*(table.add(*slot) as *const AtomicPtr<c_void>)).compare_exchange(
                &mut PHASES[index] as *mut Phase as *mut c_void,
                ptr::null_mut(),
                Ordering::SeqCst,
                Ordering::SeqCst,
            );
        }
        PHASE_INSTALLED[index] = false;
    }
    PHASE_SNAP = [(0, u64::MAX); 2];
}

/// Registers the two phase markers in verified-empty VBI slots; diagnostic only.
unsafe fn install_phases() -> Result<(), &'static str> {
    if PHASE_INSTALLED.iter().any(|installed| *installed) {
        return Ok(());
    }
    let table = REGISTERED_TABLE;
    if table.is_null() || table_now() != table || !readable(table as *const c_void, 0x138, true)
    {
        return Err("VBI table unavailable");
    }
    PHASE_VTABLE = [
        phase_no_op as *const c_void,
        phase_move as *const c_void,
        phase_no_op as *const c_void,
    ];
    for (index, slot) in PHASE_SLOTS.iter().enumerate() {
        PHASES[index].vtable = addr_of_vtable();
        PHASES[index].index = index;
        let exchanged = ptr::read(table.add(17 + *slot)).is_null()
            && (&*(table.add(*slot) as *const AtomicPtr<c_void>))
                .compare_exchange(
                    ptr::null_mut(),
                    &mut PHASES[index] as *mut Phase as *mut c_void,
                    Ordering::SeqCst,
                    Ordering::SeqCst,
                )
                .is_ok();
        if !exchanged {
            uninstall_phases();
            return Err("VBI phase slot occupied");
        }
        PHASE_INSTALLED[index] = true;
    }
    Ok(())
}

fn addr_of_vtable() -> *const *const c_void {
    ptr::addr_of!(PHASE_VTABLE) as *const *const c_void
}

fn flag(stream: &Stream) -> String {
    let mut text = String::from(if !stream.bound {
        "u"
    } else if !stream.seeded {
        "p"
    } else if stream.busy {
        "b"
    } else {
        "q"
    });
    if stream.lost {
        text.push('l');
    }
    text
}

fn token(out: &mut String, key: &str, stream: &Stream) {
    let state = hex64(stream.last);
    let _ = write!(
        out,
        " {}={}:{}:{}:{}:{}",
        key,
        stream.steps,
        core::str::from_utf8(&state).unwrap_or("0"),
        stream.passes,
        flag(stream),
        stream.seed_frame
    );
}

/// One-line summary consumed by `engine_hooks.lua` (RNGSTREAMS) and the comparator.
pub(crate) unsafe fn summary() -> String {
    let mut out = String::with_capacity(2048);
    let seed = hex64(SESSION);
    let _ = write!(
        out,
        "v=1 frame={} seed={}",
        FRAME,
        core::str::from_utf8(&seed).unwrap_or("0")
    );
    token(&mut out, SPECS[0].token, &STREAMS[0]);
    for (name, value) in ["a", "b", "c", "o", "x"].iter().zip(PHASE_STEPS.iter()) {
        let _ = write!(out, " mw_{}={}", name, value);
    }
    let _ = write!(
        out,
        " phases={}",
        PHASE_INSTALLED.iter().all(|installed| *installed) as u8
    );
    if TICK_COUNT > 0 {
        let _ = write!(out, " mw_tick0={} mw_ticks=", TICK_FIRST);
        for (index, value) in TICKS[..TICK_COUNT].iter().enumerate() {
            let _ = write!(out, "{}{}", if index == 0 { "" } else { "," }, value);
        }
        if TICK_DROPPED {
            out.push_str(" mw_ticks_dropped=1");
        }
    }
    TICK_COUNT = 0;
    TICK_DROPPED = false;
    for index in 1..ROLES {
        token(&mut out, SPECS[index].token, &STREAMS[index]);
    }
    let pool: u64 = STREAMS[ROLES..ROLES + JOBS]
        .iter()
        .map(|stream| stream.steps)
        .sum();
    let _ = write!(out, " pool={} pool_steps={}", JOBS, pool);
    for index in 0..JOBS {
        token(&mut out, &format!("j{:02}", index), &STREAMS[ROLES + index]);
    }
    out
}

unsafe fn owner_ready() -> bool {
    ARMED
        && !CURRENT.is_null()
        && (*CURRENT).armed
        && GetCurrentThreadId() == OWNER_THREAD
        && GetCurrentThreadId() == OWNER
        && context_valid()
}

unsafe extern "C-unwind" fn lua_rng_streams(lua: *mut LuaState) -> c_int {
    if !owner_ready() {
        return failure(lua, "RNG streams unavailable on this thread");
    }
    let text = summary();
    (api().pushlstring.unwrap())(lua, text.as_ptr() as *const c_char, text.len());
    1
}

unsafe extern "C-unwind" fn lua_rng_phases(lua: *mut LuaState) -> c_int {
    if !owner_ready() {
        return failure(lua, "RNG phases unavailable on this thread");
    }
    if let Err(reason) = install_phases() {
        return failure(lua, reason);
    }
    (api().pushboolean.unwrap())(lua, 1);
    1
}

/// Adds `rng_streams` and `rng_phases` to the module table on top of the Lua stack.
pub(crate) unsafe fn register_lua(lua: *mut LuaState) {
    method(lua, b"rng_streams\0", Some(lua_rng_streams));
    method(lua, b"rng_phases\0", Some(lua_rng_phases));
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{DeleteCriticalSection, InitializeCriticalSection, IMAGE_SIZE};

    static mut FAKE_TLS: [(u32, usize); 40] = [(0, 0); 40];

    unsafe fn fake_resolver(thread_id: u32) -> usize {
        FAKE_TLS
            .iter()
            .find(|(id, _)| *id == thread_id)
            .map(|(_, tls)| *tls)
            .unwrap_or(0)
    }

    fn advance(state: u64, count: u64) -> u64 {
        (0..count).fold(state, |value, _| step(value))
    }

    #[test]
    fn step_matches_engine_and_counts_exactly() {
        // Random::SeedThreadState: two steps of (time + tid); same constants as 0x14dde20.
        let mut manual = 1234u64;
        for _ in 0..2 {
            manual = (manual
                .wrapping_mul(0x5851f42d4c957f2d)
                .wrapping_add(0x14057b7ef767814f))
            .rotate_right(30);
        }
        assert_eq!(advance(1234, 2), manual);
        let seed = role_seed(123456789, b"MoveWorker");
        assert_eq!(seed, 0xf3da54e488294596);
        // Stage 1 native runs C/D both logged 28acd4a7fac17879 at their first sample.
        assert_eq!(steps_between(seed, 0x28acd4a7fac17879, 1000), Some(150));
        assert_eq!(steps_between(seed, advance(seed, 5000), 10000), Some(5000));
        assert_eq!(steps_between(seed, advance(seed, 5000), 4999), None);
        assert_eq!(steps_between(seed, seed, 0), Some(0));
        assert_eq!(&job_role(7), b"JobThread/07");
        assert_eq!(&job_role(31), b"JobThread/31");
        let roles: Vec<u64> = SPECS
            .iter()
            .map(|spec| role_seed(42, spec.role))
            .chain((0..MAX_JOBS).map(|index| role_seed(42, &job_role(index))))
            .collect();
        for (index, value) in roles.iter().enumerate() {
            assert!(roles[index + 1..].iter().all(|other| other != value));
        }
    }

    struct World {
        tls: Vec<Vec<u8>>,
        objects: Vec<Vec<u8>>,
        threads: Vec<Vec<u8>>,
        manager: Vec<u8>,
    }

    unsafe fn world(jobs: usize) -> World {
        let image = Box::leak(vec![0u8; IMAGE_SIZE].into_boxed_slice());
        IMAGE = image.as_mut_ptr() as usize;
        let mut world = World {
            tls: (0..ROLES + jobs).map(|_| vec![0u8; TLS_SPAN]).collect(),
            objects: SPECS.iter().map(|spec| vec![0u8; spec.span]).collect(),
            threads: (0..jobs).map(|_| vec![0u8; 0x18]).collect(),
            manager: vec![0u8; JOB_SPAN],
        };
        FAKE_TLS = [(0, 0); 40];
        for (index, spec) in SPECS.iter().enumerate() {
            let object = world.objects[index].as_mut_ptr() as usize;
            ptr::write((IMAGE + spec.singleton) as *mut usize, object);
            ptr::write((object + spec.runnable) as *mut usize, IMAGE + spec.vtable);
            ptr::write((object + spec.runnable + 8) as *mut usize, object + spec.thread);
            ptr::write((object + spec.thread + 0x10) as *mut u32, 100 + index as u32);
            ptr::write((object + spec.thread + 0x14) as *mut i32, 1);
            let tls = world.tls[index].as_mut_ptr() as usize;
            ptr::write((tls + TLS_THREAD) as *mut usize, object + spec.thread);
            ptr::write((tls + TLS_RNG) as *mut u64, 0x1111 * (index as u64 + 1));
            FAKE_TLS[index] = (100 + index as u32, tls);
            if let Gate::Handshake { lock, .. } = spec.gate {
                InitializeCriticalSection((object + lock) as *mut c_void);
            }
        }
        let manager = world.manager.as_mut_ptr() as usize;
        ptr::write((IMAGE + JOB_MANAGER_RVA) as *mut usize, manager);
        ptr::write((manager + JOB_COUNT) as *mut u32, jobs as u32);
        InitializeCriticalSection((manager + JOB_LOCK) as *mut c_void);
        for index in 0..jobs {
            let runnable = manager + JOB_RUNNABLES + index * JOB_STRIDE;
            let thread = world.threads[index].as_mut_ptr() as usize;
            ptr::write((manager + JOB_THREADS + index * 8) as *mut usize, thread);
            ptr::write(runnable as *mut usize, IMAGE + JOB_VTABLE_RVA);
            ptr::write((runnable + 8) as *mut usize, thread);
            ptr::write((runnable + JOB_OWNER) as *mut usize, manager);
            ptr::write((runnable + JOB_INDEX) as *mut u32, index as u32);
            ptr::write((thread + 0x10) as *mut u32, 200 + index as u32);
            ptr::write((thread + 0x14) as *mut i32, 1);
            let tls = world.tls[ROLES + index].as_mut_ptr() as usize;
            ptr::write((tls + TLS_THREAD) as *mut usize, thread);
            FAKE_TLS[ROLES + index] = (200 + index as u32, tls);
        }
        RESOLVER = fake_resolver;
        world
    }

    unsafe fn state(world: &World, index: usize) -> u64 {
        ptr::read((world.tls[index].as_ptr() as usize + TLS_RNG) as *const u64)
    }

    unsafe fn draw(world: &mut World, index: usize, count: u64) {
        let slot = (world.tls[index].as_mut_ptr() as usize + TLS_RNG) as *mut u64;
        ptr::write(slot, advance(ptr::read(slot), count));
    }

    unsafe fn set_byte(world: &mut World, role: usize, offset: usize, value: u8) {
        world.objects[role][offset] = value;
    }

    /// Binding, gated seeding and exact counting without verify(): drives the same
    /// internals `arm` uses after its signature check.
    unsafe fn arm_unverified(world: &mut World, session: u64) {
        disarm();
        STREAMS = [EMPTY; ROLES + MAX_JOBS];
        PHASE_STEPS = [0; 5];
        PHASE_SNAP = [(0, u64::MAX); 2];
        TICK_GEN = 0;
        TICK_COUNT = 0;
        FRAME = 0;
        let moveworker = bind_role(0);
        assert!(moveworker.bound);
        OWNER = GetCurrentThreadId();
        SESSION = session;
        let seed = role_seed(session, b"MoveWorker");
        ptr::write((world.tls[0].as_mut_ptr() as usize + TLS_RNG) as *mut u64, seed);
        STREAMS[0] = Stream {
            seeded: true,
            last: seed,
            ..moveworker
        };
        LAST_END = seed;
        for index in 1..ROLES {
            STREAMS[index] = bind_role(index);
        }
        bind_pool();
        ARMED = true;
    }

    fn field<'a>(summary: &'a str, key: &str) -> &'a str {
        summary
            .split_whitespace()
            .find_map(|part| part.strip_prefix(&format!("{}=", key)))
            .unwrap_or_else(|| panic!("missing {} in {}", key, summary))
    }

    #[test]
    fn synthetic_role_seeding_is_gated_and_counts_are_exact() {
        unsafe {
            let mut world = world(4);
            let slots = {
                let value: *mut *mut c_void;
                core::arch::asm!("mov {}, gs:[0x58]", out(reg) value, options(nostack, preserves_flags, readonly));
                value
            };
            let saved = ptr::read(slots);
            ptr::write(slots, world.tls[0].as_mut_ptr() as *mut c_void);
            // MoveWorker must report the current OS thread id for its own binding.
            ptr::write(
                (world.objects[0].as_mut_ptr() as usize + 0x18 + 0x10) as *mut u32,
                GetCurrentThreadId(),
            );
            FAKE_TLS[0] = (GetCurrentThreadId(), world.tls[0].as_mut_ptr() as usize);
            let session = 0x75bcd15;
            arm_unverified(&mut world, session);
            assert_eq!(state(&world, 0), role_seed(session, b"MoveWorker"));

            // Frame 1: pathing not joined, workers busy, job queue non-empty.
            let before: Vec<u64> = (1..ROLES + 4).map(|index| state(&world, index)).collect();
            let manager = world.manager.as_mut_ptr() as usize;
            ptr::write((manager + JOB_QUEUED) as *mut u64, 3);
            draw(&mut world, 0, 17);
            tick(1);
            let after: Vec<u64> = (1..ROLES + 4).map(|index| state(&world, index)).collect();
            assert_eq!(before, after, "no busy stream may be written");
            assert_eq!(STREAMS[0].steps, 17);
            assert_eq!(PHASE_STEPS[4], 17, "no phase markers: unattributed");
            let text = summary();
            assert_eq!(field(&text, "frame"), "1");
            assert!(field(&text, "path").ends_with(":p:0"));
            assert!(field(&text, "j00").ends_with(":p:0"));
            assert_eq!(field(&text, "mw_ticks"), "17");

            // Frame 2: everything quiescent except Gravidar; a busy job blocks the pool.
            set_byte(&mut world, 1, 0x88, 1);
            ptr::write((world.objects[1].as_mut_ptr() as usize + 0x84) as *mut i32, 2);
            set_byte(&mut world, 2, 0x88, 1);
            set_byte(&mut world, 3, 0x140, 1);
            ptr::write((manager + JOB_QUEUED) as *mut u64, 0);
            ptr::write((manager + JOB_RUNNABLES + JOB_BUSY) as *mut u32, 1);
            tick(2);
            for (index, role) in [(1, &b"PathingManager"[..]), (2, b"MassTraffic"), (3, b"RegionManager")] {
                assert_eq!(state(&world, index), role_seed(session, role));
                assert_eq!(STREAMS[index].seed_frame, 2);
            }
            assert_ne!(state(&world, 4), role_seed(session, b"Gravidar"));
            assert!(!STREAMS[ROLES].seeded);

            // Frame 3: Gravidar and the idle pool seed; held handshake lock defers nothing else.
            set_byte(&mut world, 4, 0x6988, 1);
            ptr::write((manager + JOB_RUNNABLES + JOB_BUSY) as *mut u32, 0);
            tick(3);
            assert_eq!(state(&world, 4), role_seed(session, b"Gravidar"));
            for index in 0..4 {
                assert_eq!(state(&world, ROLES + index), role_seed(session, &job_role(index)));
                assert_eq!(STREAMS[ROLES + index].seed_frame, 3);
            }

            // Exact counting, pass detection, and busy streams keeping stale values.
            draw(&mut world, 1, 40);
            draw(&mut world, 2, 7);
            draw(&mut world, ROLES + 2, 9);
            ptr::write((world.tls[2].as_mut_ptr() as usize + TLS_GAME_TIME) as *mut f64, 5.0);
            set_byte(&mut world, 3, 0x140, 0);
            draw(&mut world, 3, 11);
            tick(4);
            assert_eq!(STREAMS[1].steps, 40);
            assert_eq!((STREAMS[2].steps, STREAMS[2].passes), (7, 1));
            assert_eq!(STREAMS[ROLES + 2].steps, 9);
            assert!(STREAMS[3].busy && STREAMS[3].steps == 0);
            let text = summary();
            assert!(field(&text, "rm").ends_with(":b:2"));
            assert_eq!(field(&text, "pool"), "4");
            assert_eq!(field(&text, "pool_steps"), "9");
            assert!(field(&text, "mt").starts_with("7:"));

            // A count beyond the walk cap is flagged lost, never guessed.
            draw(&mut world, 1, WALK_CAP + 5);
            tick(5);
            assert!(STREAMS[1].lost);
            assert!(field(&summary(), "path").contains(":ql:"));

            // A TLS block that no longer belongs to the bound thread is never written.
            ptr::write((world.tls[2].as_mut_ptr() as usize + TLS_THREAD) as *mut usize, 1);
            let frozen = state(&world, 2);
            tick(6);
            assert!(!STREAMS[2].bound);
            assert_eq!(state(&world, 2), frozen);
            assert!(field(&summary(), "mt").contains(":u"));

            disarm();
            ptr::write(slots, saved);
            for (index, spec) in SPECS.iter().enumerate() {
                if let Gate::Handshake { lock, .. } = spec.gate {
                    DeleteCriticalSection((world.objects[index].as_mut_ptr() as usize + lock) as *mut c_void);
                }
            }
            DeleteCriticalSection((manager + JOB_LOCK) as *mut c_void);
            RESOLVER = teb_tls_block;
        }
    }

    #[test]
    fn synthetic_binding_refuses_mismatched_layouts() {
        unsafe {
            let mut world = world(2);
            // Wrong runnable vtable, unstarted thread, foreign TLS owner, bad pool index.
            ptr::write(world.objects[1].as_mut_ptr() as *mut usize, IMAGE + 8);
            ptr::write((world.objects[2].as_mut_ptr() as usize + 0xb0 + 0x14) as *mut i32, 0);
            ptr::write((world.tls[3].as_mut_ptr() as usize + TLS_THREAD) as *mut usize, 7);
            let manager = world.manager.as_mut_ptr() as usize;
            ptr::write((manager + JOB_RUNNABLES + JOB_STRIDE + JOB_INDEX) as *mut u32, 9);
            for index in 1..4 {
                assert!(!bind_role(index).bound, "role {} must be refused", index);
            }
            assert!(bind_role(4).bound);
            STREAMS = [EMPTY; ROLES + MAX_JOBS];
            bind_pool();
            assert_eq!(JOBS, 2);
            assert!(STREAMS[ROLES].bound && !STREAMS[ROLES + 1].bound);
            ptr::write((manager + JOB_COUNT) as *mut u32, 33);
            STREAMS = [EMPTY; ROLES + MAX_JOBS];
            bind_pool();
            assert_eq!(JOBS, 0);
            // Wrong build: the real signature check refuses the synthetic image.
            assert!(!verify());
            assert_eq!(arm(1), Err("RNG stream signatures differ"));
            assert!(!ARMED);
            RESOLVER = teb_tls_block;
        }
    }

    #[test]
    fn synthetic_phase_markers_split_moveworker_draws() {
        unsafe {
            let mut world = world(0);
            let slots = {
                let value: *mut *mut c_void;
                core::arch::asm!("mov {}, gs:[0x58]", out(reg) value, options(nostack, preserves_flags, readonly));
                value
            };
            let saved = ptr::read(slots);
            ptr::write(slots, world.tls[0].as_mut_ptr() as *mut c_void);
            ptr::write(
                (world.objects[0].as_mut_ptr() as usize + 0x18 + 0x10) as *mut u32,
                GetCurrentThreadId(),
            );
            FAKE_TLS[0] = (GetCurrentThreadId(), world.tls[0].as_mut_ptr() as usize);
            arm_unverified(&mut world, 9);
            let mut table = vec![ptr::null_mut::<c_void>(); 40];
            ptr::write((IMAGE + crate::VBI_RVA) as *mut *mut *mut c_void, table.as_mut_ptr());
            REGISTERED_TABLE = table.as_mut_ptr();
            table[5] = 0x1234 as *mut c_void;
            assert_eq!(install_phases(), Err("VBI phase slot occupied"));
            assert!(table[2].is_null(), "partial install rolled back");
            table[5] = ptr::null_mut();
            assert_eq!(install_phases(), Ok(()));
            let first = &mut PHASES[0] as *mut Phase;
            let second = &mut PHASES[1] as *mut Phase;
            assert_eq!(table[2], first as *mut c_void);
            // Frame: 3 draws before slot 2, 20 in GameClient/universe, 4 in UI..JobEngine.
            draw(&mut world, 0, 3);
            let vtable = (*first).vtable;
            let mover: extern "C" fn(*mut Phase) = core::mem::transmute(ptr::read(vtable.add(1)));
            mover(first);
            draw(&mut world, 0, 20);
            mover(second);
            draw(&mut world, 0, 4);
            tick(1);
            assert_eq!(PHASE_STEPS[..3], [3, 20, 4]);
            draw(&mut world, 0, 2);
            after_callback();
            assert_eq!(PHASE_STEPS[3], 2);
            // Stale markers (not refreshed this frame) fall back to unattributed.
            draw(&mut world, 0, 6);
            tick(2);
            assert_eq!(PHASE_STEPS[4], 6);
            assert_eq!(STREAMS[0].steps, 35);
            let text = summary();
            assert_eq!(field(&text, "phases"), "1");
            assert_eq!(field(&text, "mw_ticks"), "29,6");
            assert_eq!(field(&text, "mw_tick0"), "1");
            disarm();
            assert!(table[2].is_null() && table[5].is_null());
            REGISTERED_TABLE = ptr::null_mut();
            ptr::write(slots, saved);
            RESOLVER = teb_tls_block;
        }
    }
}
