//! Opt-in thread-timing diagnostics (`observe`) and frame-boundary joins (`join`).
//!
//! Windows X4 9.00, Steam build 23660954 only. All engine offsets below were
//! taken from original static analysis and are verified by `timing_*` byte
//! signatures before anything is registered. See
//! `reports/THREAD-TIMING-DETERMINISM.txt` for the mechanism of every path.
//!
//! Engine writes are limited to: two empty MoveWorker VBI slot pointers (start
//! slot 0, end slot 15), the TryEnterCriticalSection import slot (counting
//! wrapper that always returns the original result), the engine's own
//! single-waiter flag/event pairs used exactly like its NotifyGameReset path,
//! a peek (acquire + release) of the pathing pass semaphore, and in `join`
//! mode a stable reordering of deferred-event command payloads appended
//! between two frame boundaries. Every wait has a timeout.

use super::*;
use std::sync::atomic::AtomicUsize;

const START_SLOT: usize = 0;
const END_SLOT: usize = 15;
const LOADING_BANK: usize = 17;

const GRAVIDAR_RVA: usize = 0x6cf1650;
const MASS_TRAFFIC_RVA: usize = 0x6cf23e0;
const REGION_RVA: usize = 0x6cf21e0;
const PATHING_RVA: usize = 0x6cf21c8;
const JOB_MANAGER_RVA: usize = 0x6cf1540;
const GRAVIDAR_BUFFER_RVA: usize = 0x2f4b99c;
const QUEUE_LOCK_RVA: usize = 0x398a558;
const QUEUE_HEAD_RVA: usize = 0x39943a8;
const TECS_IAT_RVA: usize = 0x1bc14c8;
const ZONE_TRY_RETURN_RVA: usize = 0x834c74;
const GRAVIDAR_TRY_RETURN_RVA: usize = 0x10aef17;

const SITES: &[(usize, usize, &str)] = &[
    (
        0xef45d0,
        0x4a,
        "62e76fb7327bd69feeb7c2e8ad6a2181f3abd7ad57b2154b40fc7c254300007d",
    ),
    (
        0xef4777,
        0xc7,
        "b83c04896534d19e2f29ff5eef56b9cde1787ed92a29293900142ba0ba58510b",
    ),
    (
        0x10b12c0,
        0x13d,
        "89362a2325ff0149e2e41e08628ad92eb2405e592c3dfc6f3e3e148667399daa",
    ),
    (
        0x10ae1c0,
        0xad,
        "b7dde5defd0c7b65f572af8957fde23c192e30433350caae7a1f50d3b532ed6b",
    ),
    (
        0x8c0770,
        0xcc,
        "90b6e5dbbfdd4be1f048d66ad5fb6e92da5202521a26cc1bf4fa4d70e0d01f17",
    ),
    (
        0xef314b,
        0xb3,
        "bdee3e531fd5ddf05ba217a97b52c806f5d0985b1f0b70fb0f845ad4e22445a5",
    ),
    (
        0x8c0e00,
        0xb2,
        "c741511c1d27beb04a45df5ac2edc6b281301590cdd4b3e1afa36c48e37b910e",
    ),
    (
        0xef3360,
        0xc0,
        "c252a597f5a898735e0d6b3b9e4e2f7efb70f5d06657cef03c7447ec17cebb08",
    ),
    (
        0x834c59,
        0x23,
        "2a71006d46148aff08b5a2285249e319e389c519e76a4d85a95fd4ef3a1ee100",
    ),
    (
        0xeac170,
        0x30,
        "bad63742cdef1987a87cfd3a02d2df6bb10f98fe6aaa3b8a72d126944bf2f89b",
    ),
    (
        0xeae934,
        0x13,
        "2878e41070af43b475e97ea75292171210006ecf70a333bed2783778616fe0d6",
    ),
    (
        0xeaeb60,
        0xe7,
        "ee638427aaa77e88cb409f062831f7dde7512baec968f2f5a9633319806f2437",
    ),
    (
        0x99a7aa,
        0x49,
        "2aae55cc2fd700ea4d19502c09bf83d3c2e1366e6904888f2727223713bcdaa8",
    ),
    (
        0x998430,
        0x7b,
        "449d0bb0bffbf44f1a11f34be34d77731e26d70da48918b9e7a12e3474befbf3",
    ),
    (
        0x99ac47,
        0x4e,
        "4020a3eea47b63416ac7660f0c742a5ff64c07a12978298fd9772ce456e5738b",
    ),
    (
        0x14bd0c1,
        0x1f,
        "9ff96323e029bfa37bdd99d5926b2504d0b4cf47a1b744981a98eebf5fdd0366",
    ),
];

/// Worker manager layout. `waiter` is the engine's {flag: u32, event: HANDLE}
/// single-waiter pair that the worker signals after it sets `done`.
struct Worker {
    global: usize,
    extent: usize,
    cs: usize,
    done: usize,
    waiter: usize,
    state: usize,
    active: usize,
    thread: usize,
}

const GRAVIDAR: Worker = Worker {
    global: GRAVIDAR_RVA,
    extent: 0x69b0,
    cs: 0x6960,
    done: 0x6988,
    waiter: 0x6990,
    state: 0x40,
    active: 0x44,
    thread: 0,
};
const MASS_TRAFFIC: Worker = Worker {
    global: MASS_TRAFFIC_RVA,
    extent: 0xd0,
    cs: 0x60,
    done: 0x88,
    waiter: 0x90,
    state: 0xc8,
    active: 0,
    thread: 0xb8,
};
const REGION: Worker = Worker {
    global: REGION_RVA,
    extent: 0x188,
    cs: 0x118,
    done: 0x140,
    waiter: 0x148,
    state: 0x180,
    active: 0,
    thread: 0x170,
};

const BIT_GRAVIDAR: u32 = 1;
const BIT_MASS_TRAFFIC: u32 = 2;
const BIT_REGION: u32 = 4;
const BIT_PATHING: u32 = 8;
const BIT_ORDER: u32 = 16;
const MAX_CONSECUTIVE_TIMEOUTS: u32 = 30;
const QUEUE_LIMIT: usize = 1 << 20;
const WAIT_OBJECT_0: u32 = 0;

#[link(name = "kernel32")]
extern "system" {
    fn EnterCriticalSection(lock: *mut c_void);
    fn ResetEvent(event: *mut c_void) -> c_int;
    fn WaitForSingleObject(handle: *mut c_void, milliseconds: u32) -> u32;
    fn ReleaseSemaphore(handle: *mut c_void, count: i32, previous: *mut i32) -> c_int;
    #[cfg(test)]
    fn SetEvent(event: *mut c_void) -> c_int;
    #[cfg(test)]
    fn CreateEventW(
        attributes: *mut c_void,
        manual: c_int,
        initial: c_int,
        name: *const u16,
    ) -> *mut c_void;
    #[cfg(test)]
    fn CreateSemaphoreW(
        attributes: *mut c_void,
        initial: i32,
        maximum: i32,
        name: *const u16,
    ) -> *mut c_void;
    #[cfg(test)]
    fn CloseHandle(handle: *mut c_void) -> c_int;
    #[cfg(test)]
    fn Sleep(milliseconds: u32);
}

type QueryThread = unsafe extern "system" fn(*mut c_void, u32, *mut c_void, u32, *mut u32) -> i32;

// Counting TryEnterCriticalSection wrapper. It forwards to the original
// import, returns its exact result, and on failure counts the two call sites
// whose skipped work is timing-dependent (Zone::Move traffic, Gravidar pass).
static X4LAN_TIMING_TECS_ORIGINAL: AtomicUsize = AtomicUsize::new(0);
static X4LAN_TIMING_TECS_ZONE_SITE: AtomicUsize = AtomicUsize::new(usize::MAX);
static X4LAN_TIMING_TECS_GRAVIDAR_SITE: AtomicUsize = AtomicUsize::new(usize::MAX);
static X4LAN_TIMING_TECS_LAST_FAIL: AtomicUsize = AtomicUsize::new(0);
static X4LAN_TIMING_TECS_ZONE_FAILS: AtomicU64 = AtomicU64::new(0);
static X4LAN_TIMING_TECS_GRAVIDAR_FAILS: AtomicU64 = AtomicU64::new(0);

core::arch::global_asm!(
    ".globl x4lan_timing_tecs_hook",
    ".def x4lan_timing_tecs_hook",
    ".scl 2",
    ".type 32",
    ".endef",
    ".seh_proc x4lan_timing_tecs_hook",
    "x4lan_timing_tecs_hook:",
    "push rbx",
    ".seh_pushreg rbx",
    "sub rsp, 32",
    ".seh_stackalloc 32",
    ".seh_endprologue",
    "mov rbx, qword ptr [rsp + 40]",
    "call qword ptr [rip + {original}]",
    "test eax, eax",
    "jnz 2f",
    "mov qword ptr [rip + {last}], rbx",
    "cmp rbx, qword ptr [rip + {zone_site}]",
    "jne 3f",
    "lock inc qword ptr [rip + {zone_fails}]",
    "jmp 2f",
    "3:",
    "cmp rbx, qword ptr [rip + {gravidar_site}]",
    "jne 2f",
    "lock inc qword ptr [rip + {gravidar_fails}]",
    "2:",
    "add rsp, 32",
    "pop rbx",
    "ret",
    ".seh_endproc",
    original = sym X4LAN_TIMING_TECS_ORIGINAL,
    last = sym X4LAN_TIMING_TECS_LAST_FAIL,
    zone_site = sym X4LAN_TIMING_TECS_ZONE_SITE,
    zone_fails = sym X4LAN_TIMING_TECS_ZONE_FAILS,
    gravidar_site = sym X4LAN_TIMING_TECS_GRAVIDAR_SITE,
    gravidar_fails = sym X4LAN_TIMING_TECS_GRAVIDAR_FAILS,
);

extern "system" {
    fn x4lan_timing_tecs_hook(lock: *mut c_void) -> c_int;
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Mode {
    Off,
    Observe,
    Join,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Wait {
    Done,
    Timeout,
    Skipped,
}

#[derive(Clone, Copy)]
struct Stats {
    frames: u64,
    grav_pub: u64,
    grav_lag1: u64,
    grav_lag_n: u64,
    grav_lag_max: u64,
    grav_busy0: u64,
    grav_timeouts: u64,
    grav_wait_us: u64,
    grav_nolock: u64,
    mt_skip: u64,
    mt_busy0: u64,
    mt_busy15: u64,
    mt_old: u64,
    mt_new: u64,
    mt_timeouts: u64,
    mt_wait_us: u64,
    rm_busy0: u64,
    rm_busy15: u64,
    rm_old: u64,
    rm_new: u64,
    rm_timeouts: u64,
    rm_wait_us: u64,
    path_timeouts: u64,
    path_wait_us: u64,
    ev_nodes: u64,
    ev_seg: u64,
    ev_moved: u64,
    ev_groups: u64,
    ev_mixed: u64,
    ev_skip: u64,
    ev_raw: u64,
    ev_ord: u64,
    ev_set: u64,
}

const FNV_OFFSET: u64 = 0xcbf29ce484222325;

impl Stats {
    const fn new() -> Self {
        Stats {
            frames: 0,
            grav_pub: 0,
            grav_lag1: 0,
            grav_lag_n: 0,
            grav_lag_max: 0,
            grav_busy0: 0,
            grav_timeouts: 0,
            grav_wait_us: 0,
            grav_nolock: 0,
            mt_skip: 0,
            mt_busy0: 0,
            mt_busy15: 0,
            mt_old: 0,
            mt_new: 0,
            mt_timeouts: 0,
            mt_wait_us: 0,
            rm_busy0: 0,
            rm_busy15: 0,
            rm_old: 0,
            rm_new: 0,
            rm_timeouts: 0,
            rm_wait_us: 0,
            path_timeouts: 0,
            path_wait_us: 0,
            ev_nodes: 0,
            ev_seg: 0,
            ev_moved: 0,
            ev_groups: 0,
            ev_mixed: 0,
            ev_skip: 0,
            ev_raw: FNV_OFFSET,
            ev_ord: FNV_OFFSET,
            ev_set: FNV_OFFSET,
        }
    }
}

struct State {
    mode: Mode,
    timeout_ms: u32,
    table: *mut *mut c_void,
    iat_installed: bool,
    iat_original: usize,
    disabled: u32,
    reason: &'static str,
    consecutive: [u32; 4],
    snapshot: Option<(usize, usize)>,
    grav_index: Option<u32>,
    grav_since: u64,
    zone_fails: u64,
    grav_fails: u64,
    jobs: u32,
    query_thread: Option<QueryThread>,
    window: Stats,
}

static mut T: State = State {
    mode: Mode::Off,
    timeout_ms: 50,
    table: ptr::null_mut(),
    iat_installed: false,
    iat_original: 0,
    disabled: 0,
    reason: "",
    consecutive: [0; 4],
    snapshot: None,
    grav_index: None,
    grav_since: 0,
    zone_fails: 0,
    grav_fails: 0,
    jobs: 0,
    query_thread: None,
    window: Stats::new(),
};

static mut START_VTABLE: [*const c_void; 3] = [ptr::null(); 3];
static mut END_VTABLE: [*const c_void; 3] = [ptr::null(); 3];
static mut START_HOOK: Handler = Handler {
    vtable: ptr::null(),
};
static mut END_HOOK: Handler = Handler {
    vtable: ptr::null(),
};

#[inline]
unsafe fn state() -> &'static mut State {
    &mut *addr_of_mut!(T)
}

fn mix(hash: u64, value: u64) -> u64 {
    let mut fnv = Fnv(hash);
    fnv.update(&value.to_le_bytes());
    fnv.0
}

fn elapsed_us(started: Option<f64>) -> u64 {
    match (started, qpc_seconds()) {
        (Some(a), Some(b)) if b >= a => ((b - a) * 1e6) as u64,
        _ => 0,
    }
}

unsafe fn read_u32(address: usize) -> u32 {
    ptr::read_volatile(address as *const u32)
}

unsafe fn verify_sites() -> bool {
    IMAGE != 0
        && SITES.iter().all(|&(rva, length, expected)| {
            readable((IMAGE + rva) as *const c_void, length, false)
                && sha256(
                    slice::from_raw_parts((IMAGE + rva) as *const u8, length),
                    expected,
                )
        })
        && [
            GRAVIDAR_RVA,
            MASS_TRAFFIC_RVA,
            REGION_RVA,
            PATHING_RVA,
            JOB_MANAGER_RVA,
            GRAVIDAR_BUFFER_RVA,
            QUEUE_LOCK_RVA,
            QUEUE_HEAD_RVA,
            TECS_IAT_RVA,
        ]
        .iter()
        .all(|rva| readable((IMAGE + rva) as *const c_void, 16, false))
}

unsafe fn worker_object(worker: &Worker) -> *mut u8 {
    let object = ptr::read_volatile((IMAGE + worker.global) as *const *mut u8);
    if readable(object as *const c_void, worker.extent, true) {
        object
    } else {
        ptr::null_mut()
    }
}

unsafe fn worker_running(worker: &Worker, object: *mut u8) -> bool {
    read_u32(object as usize + worker.state).wrapping_sub(1) <= 1
        && (worker.active == 0 || read_u32(object as usize + worker.active) != 0)
}

unsafe fn worker_done(worker: &Worker, object: *mut u8) -> bool {
    let lock = object.add(worker.cs) as *mut c_void;
    EnterCriticalSection(lock);
    let done = ptr::read_volatile(object.add(worker.done)) != 0;
    LeaveCriticalSection(lock);
    done
}

/// Waits for a worker pass with the engine's own single-waiter protocol
/// (the NotifyGameReset pattern), bounded by `timeout_ms`.
unsafe fn wait_worker(worker: &Worker, object: *mut u8, timeout_ms: u32) -> Wait {
    let lock = object.add(worker.cs) as *mut c_void;
    let flag = &*(object.add(worker.waiter) as *const AtomicU32Compat);
    let event = ptr::read_volatile(object.add(worker.waiter + 8) as *const *mut c_void);
    EnterCriticalSection(lock);
    if ptr::read_volatile(object.add(worker.done)) != 0 {
        LeaveCriticalSection(lock);
        return Wait::Done;
    }
    if event.is_null() || flag.load() != 0 {
        LeaveCriticalSection(lock);
        return Wait::Skipped;
    }
    ResetEvent(event);
    flag.swap(1);
    LeaveCriticalSection(lock);
    WaitForSingleObject(event, timeout_ms);
    flag.swap(0);
    EnterCriticalSection(lock);
    let done = ptr::read_volatile(object.add(worker.done)) != 0;
    LeaveCriticalSection(lock);
    if done {
        Wait::Done
    } else {
        Wait::Timeout
    }
}

#[repr(transparent)]
struct AtomicU32Compat(std::sync::atomic::AtomicU32);

impl AtomicU32Compat {
    fn load(&self) -> u32 {
        self.0.load(Ordering::SeqCst)
    }
    fn swap(&self, value: u32) -> u32 {
        self.0.swap(value, Ordering::SeqCst)
    }
}

/// Peeks the pathing pass semaphore: acquire with timeout, then release, so
/// the engine's own WaitAndSwapBuffers still consumes exactly one count.
unsafe fn join_pathing(timeout_ms: u32) -> Wait {
    let manager = ptr::read_volatile((IMAGE + PATHING_RVA) as *const *mut u8);
    if !readable(manager as *const c_void, 0x90, true)
        || ptr::read_volatile(manager.add(0x88)) != 0
        || read_u32(manager as usize + 0x84).wrapping_sub(1) > 1
    {
        return Wait::Skipped;
    }
    let semaphore = ptr::read_volatile(manager.add(0x10) as *const *mut c_void);
    if semaphore.is_null() {
        return Wait::Skipped;
    }
    if WaitForSingleObject(semaphore, timeout_ms) == WAIT_OBJECT_0 {
        ReleaseSemaphore(semaphore, 1, ptr::null_mut());
        Wait::Done
    } else {
        Wait::Timeout
    }
}

unsafe fn worker_time(handle: *mut c_void) -> Option<f64> {
    let query = state().query_thread?;
    if handle.is_null() {
        return None;
    }
    let mut info = [0u8; 0x30];
    if query(
        handle,
        0,
        info.as_mut_ptr() as *mut c_void,
        0x30,
        ptr::null_mut(),
    ) < 0
    {
        return None;
    }
    let teb = ptr::read_unaligned(info.as_ptr().add(8) as *const usize);
    if !readable((teb + 0x58) as *const c_void, 8, false) {
        return None;
    }
    let slots = ptr::read((teb + 0x58) as *const usize);
    if !readable(slots as *const c_void, 8, false) {
        return None;
    }
    let block = ptr::read(slots as *const usize);
    if !readable((block + 0x300) as *const c_void, 8, false) {
        return None;
    }
    Some(ptr::read_volatile((block + 0x300) as *const f64))
}

/// 0 = unknown, 1 = previous frame time (old), 2 = current frame time (new).
fn classify_time(worker: f64, current: f64, previous: f64) -> u8 {
    if worker.to_bits() == current.to_bits() {
        2
    } else if worker.to_bits() == previous.to_bits() {
        1
    } else {
        0
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct Key {
    source: u64,
    time: u64,
}

unsafe fn node_fields(node: usize) -> (u32, u64, u64, u64) {
    let kind = ptr::read((node + 0x10) as *const u32);
    let source = ptr::read((node + 0x18) as *const u64);
    let event = ptr::read((node + 0x20) as *const usize);
    let time = ptr::read((node + 0x28) as *const u64);
    // Only kind 3 (insert event) is known to carry an event pointer at +0x20;
    // other kinds are hashed without that field and never dereferenced.
    let vtable = if kind == 3 && event != 0 {
        let table = ptr::read(event as *const usize);
        if table >= IMAGE && table < IMAGE + IMAGE_SIZE {
            (table - IMAGE) as u64
        } else {
            u64::MAX
        }
    } else {
        0
    };
    (kind, source, vtable, time)
}

fn node_hash(kind: u32, source: u64, vtable: u64, time: u64) -> u64 {
    let mut fnv = Fnv::new();
    fnv.update(&kind.to_le_bytes());
    fnv.update(&source.to_le_bytes());
    fnv.update(&vtable.to_le_bytes());
    fnv.update(&time.to_le_bytes());
    fnv.0
}

struct Scan {
    count: u64,
    order: u64,
    set: u64,
}

/// Walks the whole command list (lock held by caller) and returns an order
/// hash and an order-independent set hash.
unsafe fn scan_queue(sentinel: usize, size: usize) -> Option<Scan> {
    let mut node = ptr::read(sentinel as *const usize);
    let mut previous = sentinel;
    let mut scan = Scan {
        count: 0,
        order: FNV_OFFSET,
        set: 0,
    };
    while node != sentinel {
        if node == 0
            || scan.count as usize > size.min(QUEUE_LIMIT)
            || ptr::read((node + 8) as *const usize) != previous
        {
            return None;
        }
        let (kind, source, vtable, time) = node_fields(node);
        let hash = node_hash(kind, source, vtable, time);
        scan.order = mix(scan.order, hash);
        scan.set = scan.set.wrapping_add(hash);
        scan.count += 1;
        previous = node;
        node = ptr::read(node as *const usize);
    }
    (scan.count as usize == size).then_some(scan)
}

struct Canonical {
    moved: u64,
    groups: u64,
    mixed: bool,
}

/// Stable-sorts the payloads of the last `segment` command nodes by
/// (source id, event time). Relative order inside one source/time group is
/// preserved; only commands of kind 3 (deferred event insert) are reordered.
unsafe fn canonicalize(sentinel: usize, segment: usize) -> Option<Canonical> {
    let mut nodes = Vec::with_capacity(segment);
    let mut node = ptr::read((sentinel + 8) as *const usize);
    let mut next = sentinel;
    for _ in 0..segment {
        if node == 0 || node == sentinel || ptr::read(node as *const usize) != next {
            return None;
        }
        nodes.push(node);
        next = node;
        node = ptr::read((node + 8) as *const usize);
    }
    nodes.reverse();
    let mut rows = Vec::with_capacity(segment);
    for (index, &node) in nodes.iter().enumerate() {
        let (kind, source, _, time) = node_fields(node);
        if kind != 3 {
            return Some(Canonical {
                moved: 0,
                groups: 0,
                mixed: true,
            });
        }
        rows.push((
            Key { source, time },
            index,
            ptr::read((node + 0x10) as *const [u8; 0x30]),
        ));
    }
    rows.sort_by(|a, b| (a.0.source, a.0.time, a.1).cmp(&(b.0.source, b.0.time, b.1)));
    let mut moved = 0;
    let mut groups = 0;
    for (position, row) in rows.iter().enumerate() {
        if row.1 != position {
            moved += 1;
        }
        if position > 0
            && rows[position - 1].0 == row.0
            && (position < 2 || rows[position - 2].0 != row.0)
        {
            groups += 1;
        }
        ptr::write((nodes[position] + 0x10) as *mut [u8; 0x30], row.2);
    }
    Some(Canonical {
        moved,
        groups,
        mixed: false,
    })
}

unsafe fn queue_head() -> (usize, usize) {
    (
        ptr::read_volatile((IMAGE + QUEUE_HEAD_RVA) as *const usize),
        ptr::read_volatile((IMAGE + QUEUE_HEAD_RVA + 8) as *const usize),
    )
}

unsafe fn record_wait(bit: u32, index: usize, result: Wait, waited_us: u64) {
    let t = state();
    let (timeouts, wait_max) = match bit {
        BIT_GRAVIDAR => (&mut t.window.grav_timeouts, &mut t.window.grav_wait_us),
        BIT_MASS_TRAFFIC => (&mut t.window.mt_timeouts, &mut t.window.mt_wait_us),
        BIT_REGION => (&mut t.window.rm_timeouts, &mut t.window.rm_wait_us),
        _ => (&mut t.window.path_timeouts, &mut t.window.path_wait_us),
    };
    *wait_max = (*wait_max).max(waited_us);
    match result {
        Wait::Timeout => {
            *timeouts += 1;
            t.consecutive[index] += 1;
            if t.consecutive[index] >= MAX_CONSECUTIVE_TIMEOUTS {
                t.disabled |= bit;
                t.reason = "consecutive join timeouts";
            }
        }
        Wait::Done => t.consecutive[index] = 0,
        Wait::Skipped => {}
    }
}

unsafe fn join_worker(worker: &Worker, bit: u32, index: usize) -> Option<*mut u8> {
    let object = worker_object(worker);
    if object.is_null() || !worker_running(worker, object) {
        return None;
    }
    let busy = !worker_done(worker, object);
    let (join, timeout_ms) = {
        let t = state();
        match bit {
            BIT_GRAVIDAR => t.window.grav_busy0 += busy as u64,
            BIT_MASS_TRAFFIC => t.window.mt_busy0 += busy as u64,
            _ => t.window.rm_busy0 += busy as u64,
        }
        (t.mode == Mode::Join && t.disabled & bit == 0, t.timeout_ms)
    };
    if join && busy {
        let started = qpc_seconds();
        let result = wait_worker(worker, object, timeout_ms);
        record_wait(bit, index, result, elapsed_us(started));
        if result != Wait::Done {
            return None;
        }
    } else if busy {
        return None;
    }
    Some(object)
}

unsafe fn sample_time(worker: &Worker, object: *mut u8, bit: u32) {
    if worker.thread == 0 {
        return;
    }
    let tls = thread_block();
    if !readable(tls as *const c_void, 0x330, false) {
        return;
    }
    let handle = ptr::read_volatile(object.add(worker.thread) as *const *mut c_void);
    let Some(time) = worker_time(handle) else {
        return;
    };
    let class = classify_time(
        time,
        ptr::read(tls.add(0x300) as *const f64),
        ptr::read(tls.add(0x328) as *const f64),
    );
    let t = state();
    match (bit, class) {
        (BIT_MASS_TRAFFIC, 1) => t.window.mt_old += 1,
        (BIT_MASS_TRAFFIC, 2) => t.window.mt_new += 1,
        (BIT_REGION, 1) => t.window.rm_old += 1,
        (BIT_REGION, 2) => t.window.rm_new += 1,
        _ => {}
    }
}

unsafe fn sample_gravidar_publish() {
    let t = state();
    let index = read_u32(IMAGE + GRAVIDAR_BUFFER_RVA);
    t.grav_since += 1;
    match t.grav_index {
        Some(previous) if previous != index => {
            t.window.grav_pub += 1;
            if t.grav_since == 1 {
                t.window.grav_lag1 += 1;
            } else {
                t.window.grav_lag_n += 1;
            }
            t.window.grav_lag_max = t.window.grav_lag_max.max(t.grav_since);
            t.grav_since = 0;
        }
        None => t.grav_since = 0,
        _ => {}
    }
    t.grav_index = Some(index);
}

unsafe fn process_queue() {
    let t = state();
    let lock = (IMAGE + QUEUE_LOCK_RVA) as *mut c_void;
    EnterCriticalSection(lock);
    let (sentinel, size) = queue_head();
    if sentinel == 0 || !readable(sentinel as *const c_void, 16, false) {
        LeaveCriticalSection(lock);
        t.window.ev_skip += 1;
        return;
    }
    let Some(raw) = scan_queue(sentinel, size) else {
        LeaveCriticalSection(lock);
        t.window.ev_skip += 1;
        t.disabled |= BIT_ORDER;
        t.reason = "deferred queue integrity";
        return;
    };
    t.window.ev_nodes = raw.count;
    t.window.ev_raw = mix(t.window.ev_raw, raw.order);
    let mut ordered = raw.order;
    let mut set = raw.set;
    match t.snapshot {
        Some((snap_sentinel, snap_size)) if snap_sentinel == sentinel && size >= snap_size => {
            let segment = size - snap_size;
            t.window.ev_seg += segment as u64;
            if t.mode == Mode::Join && t.disabled & BIT_ORDER == 0 && segment > 1 {
                match canonicalize(sentinel, segment) {
                    Some(result) if result.mixed => t.window.ev_mixed += 1,
                    Some(result) => {
                        t.window.ev_moved += result.moved;
                        t.window.ev_groups += result.groups;
                        if result.moved != 0 {
                            if let Some(after) = scan_queue(sentinel, size) {
                                ordered = after.order;
                                set = after.set;
                            }
                        }
                    }
                    None => {
                        t.window.ev_skip += 1;
                        t.disabled |= BIT_ORDER;
                        t.reason = "deferred queue segment integrity";
                    }
                }
            }
        }
        _ => t.window.ev_skip += 1,
    }
    LeaveCriticalSection(lock);
    t.window.ev_ord = mix(t.window.ev_ord, ordered);
    t.window.ev_set = mix(t.window.ev_set, set);
}

unsafe fn slots_owned() -> bool {
    let t = state();
    !t.table.is_null()
        && table_now() == t.table
        && ptr::read_volatile(t.table.add(START_SLOT)) == addr_of_mut!(START_HOOK) as *mut c_void
        && ptr::read_volatile(t.table.add(END_SLOT)) == addr_of_mut!(END_HOOK) as *mut c_void
}

/// Common per-callback guard; disarms on any ownership or context loss.
unsafe fn guard() -> bool {
    let t = state();
    if t.mode == Mode::Off {
        return false;
    }
    if CURRENT.is_null()
        || !(*CURRENT).armed
        || GetCurrentThreadId() != OWNER_THREAD
        || !slots_owned()
        || !context_valid()
    {
        disarm_with("context lost");
        return false;
    }
    if ptr::read((t.table as *mut u8).add(0x130) as *const c_int) != 0 {
        t.snapshot = None;
        t.grav_index = None;
        return false;
    }
    true
}

extern "C" fn frame_start(_: *mut Handler) {
    unsafe {
        if !guard() {
            return;
        }
        state().window.frames += 1;
        sample_gravidar_publish();
        for (index, (worker, bit)) in [
            (&GRAVIDAR, BIT_GRAVIDAR),
            (&MASS_TRAFFIC, BIT_MASS_TRAFFIC),
            (&REGION, BIT_REGION),
        ]
        .into_iter()
        .enumerate()
        {
            if let Some(object) = join_worker(worker, bit, index) {
                sample_time(worker, object, bit);
            }
        }
        let (join, timeout_ms) = {
            let t = state();
            (
                t.mode == Mode::Join && t.disabled & BIT_PATHING == 0,
                t.timeout_ms,
            )
        };
        if join {
            let started = qpc_seconds();
            let result = join_pathing(timeout_ms);
            record_wait(BIT_PATHING, 3, result, elapsed_us(started));
        }
        process_queue();
        let t = state();
        t.snapshot = None;
        let zone = X4LAN_TIMING_TECS_ZONE_FAILS.load(Ordering::Relaxed);
        let gravidar = X4LAN_TIMING_TECS_GRAVIDAR_FAILS.load(Ordering::Relaxed);
        t.window.mt_skip += zone.wrapping_sub(t.zone_fails);
        t.window.grav_nolock += gravidar.wrapping_sub(t.grav_fails);
        t.zone_fails = zone;
        t.grav_fails = gravidar;
    }
}

extern "C" fn frame_end(_: *mut Handler) {
    unsafe {
        if !guard() {
            return;
        }
        let t = state();
        for (worker, bit) in [(&MASS_TRAFFIC, BIT_MASS_TRAFFIC), (&REGION, BIT_REGION)] {
            let object = worker_object(worker);
            if object.is_null() || !worker_running(worker, object) {
                continue;
            }
            let busy = !worker_done(worker, object) as u64;
            if bit == BIT_MASS_TRAFFIC {
                t.window.mt_busy15 += busy;
            } else {
                t.window.rm_busy15 += busy;
            }
        }
        let lock = (IMAGE + QUEUE_LOCK_RVA) as *mut c_void;
        EnterCriticalSection(lock);
        let head = queue_head();
        LeaveCriticalSection(lock);
        t.snapshot = (head.0 != 0).then_some(head);
    }
}

unsafe fn install_iat() -> bool {
    let name: Vec<u16> = "kernel32.dll\0".encode_utf16().collect();
    let kernel = GetModuleHandleW(name.as_ptr());
    let original = if kernel.is_null() {
        ptr::null_mut()
    } else {
        GetProcAddress(kernel, b"TryEnterCriticalSection\0".as_ptr())
    };
    if original.is_null() {
        return false;
    }
    X4LAN_TIMING_TECS_ORIGINAL.store(original as usize, Ordering::SeqCst);
    X4LAN_TIMING_TECS_ZONE_SITE.store(IMAGE + ZONE_TRY_RETURN_RVA, Ordering::SeqCst);
    X4LAN_TIMING_TECS_GRAVIDAR_SITE.store(IMAGE + GRAVIDAR_TRY_RETURN_RVA, Ordering::SeqCst);
    if !swap_attention_slot(
        TECS_IAT_RVA,
        original,
        x4lan_timing_tecs_hook as *mut c_void,
    ) {
        return false;
    }
    let t = state();
    t.iat_installed = true;
    t.iat_original = original as usize;
    true
}

unsafe fn uninstall_iat() {
    let t = state();
    if t.iat_installed
        && swap_attention_slot(
            TECS_IAT_RVA,
            x4lan_timing_tecs_hook as *mut c_void,
            t.iat_original as *mut c_void,
        )
    {
        t.iat_installed = false;
    }
}

unsafe fn resolve_query_thread() -> Option<QueryThread> {
    let name: Vec<u16> = "ntdll.dll\0".encode_utf16().collect();
    let ntdll = GetModuleHandleW(name.as_ptr());
    if ntdll.is_null() {
        return None;
    }
    let address = GetProcAddress(ntdll, b"NtQueryInformationThread\0".as_ptr());
    (!address.is_null()).then(|| core::mem::transmute::<*mut c_void, QueryThread>(address))
}

unsafe fn job_workers() -> u32 {
    let manager = ptr::read_volatile((IMAGE + JOB_MANAGER_RVA) as *const *mut u8);
    if readable(manager as *const c_void, 0x52c, false) {
        read_u32(manager as usize + 0x528)
    } else {
        0
    }
}

/// Registers the two slots and the import wrapper. Caller has verified the
/// build, the timing signatures and the observer context.
unsafe fn install(mode: Mode, timeout_ms: u32) -> Result<(), &'static str> {
    if state().mode != Mode::Off || mode == Mode::Off {
        return Err("timing already active");
    }
    let table = table_now();
    if table.is_null()
        || table != REGISTERED_TABLE
        || !readable(table as *const c_void, 0x138, true)
    {
        return Err("VBI table unavailable");
    }
    for slot in [START_SLOT, END_SLOT] {
        if !ptr::read(table.add(slot)).is_null()
            || !ptr::read(table.add(LOADING_BANK + slot)).is_null()
        {
            return Err("timing VBI slot occupied");
        }
    }
    START_VTABLE = [
        no_op as *const c_void,
        frame_start as *const c_void,
        no_op as *const c_void,
    ];
    END_VTABLE = [
        no_op as *const c_void,
        frame_end as *const c_void,
        no_op as *const c_void,
    ];
    START_HOOK.vtable = addr_of!(START_VTABLE) as *const *const c_void;
    END_HOOK.vtable = addr_of!(END_VTABLE) as *const *const c_void;
    let jobs = job_workers();
    let query_thread = resolve_query_thread();
    *state() = State {
        mode: Mode::Off,
        timeout_ms,
        table,
        iat_installed: false,
        iat_original: 0,
        disabled: 0,
        reason: "",
        consecutive: [0; 4],
        snapshot: None,
        grav_index: None,
        grav_since: 0,
        zone_fails: X4LAN_TIMING_TECS_ZONE_FAILS.load(Ordering::Relaxed),
        grav_fails: X4LAN_TIMING_TECS_GRAVIDAR_FAILS.load(Ordering::Relaxed),
        jobs,
        query_thread,
        window: Stats::new(),
    };
    if !install_iat() {
        state().table = ptr::null_mut();
        return Err("TryEnterCriticalSection import unexpected");
    }
    for (slot, hook) in [
        (START_SLOT, addr_of_mut!(START_HOOK)),
        (END_SLOT, addr_of_mut!(END_HOOK)),
    ] {
        let cell = &*(table.add(slot) as *const AtomicPtr<c_void>);
        if cell
            .compare_exchange(
                ptr::null_mut(),
                hook as *mut c_void,
                Ordering::SeqCst,
                Ordering::SeqCst,
            )
            .is_err()
        {
            disarm_with("timing VBI slot changed during registration");
            return Err("timing VBI slot changed during registration");
        }
    }
    state().mode = mode;
    Ok(())
}

unsafe fn disarm_with(reason: &'static str) {
    let table = {
        let t = state();
        if t.mode != Mode::Off && t.reason.is_empty() {
            t.reason = reason;
        }
        t.mode = Mode::Off;
        t.table
    };
    if !table.is_null() {
        for (slot, hook) in [
            (START_SLOT, addr_of_mut!(START_HOOK)),
            (END_SLOT, addr_of_mut!(END_HOOK)),
        ] {
            if readable(table.add(slot) as *const c_void, 8, true) {
                let cell = &*(table.add(slot) as *const AtomicPtr<c_void>);
                let _ = cell.compare_exchange(
                    hook as *mut c_void,
                    ptr::null_mut(),
                    Ordering::SeqCst,
                    Ordering::SeqCst,
                );
            }
        }
    }
    uninstall_iat();
    let t = state();
    t.table = ptr::null_mut();
    t.snapshot = None;
}

/// Called from the observer's disarm path; idempotent.
pub(crate) unsafe fn disarm() {
    disarm_with("observer disarmed");
}

fn mode_name(mode: Mode) -> &'static str {
    match mode {
        Mode::Off => "off",
        Mode::Observe => "observe",
        Mode::Join => "join",
    }
}

unsafe fn take_line() -> String {
    let t = state();
    let w = t.window;
    t.window = Stats::new();
    let hex = |value: u64| String::from_utf8_lossy(&hex64(value)).into_owned();
    format!(
        "mode={} frames={} grav_pub={} grav_lag1={} grav_lagN={} grav_lagmax={} grav_busy0={} grav_to={} grav_wait_us={} grav_nolock={} \
mt_skip={} mt_busy0={} mt_busy15={} mt_old={} mt_new={} mt_to={} mt_wait_us={} \
rm_busy0={} rm_busy15={} rm_old={} rm_new={} rm_to={} rm_wait_us={} path_to={} path_wait_us={} \
ev_n={} ev_seg={} ev_moved={} ev_groups={} ev_mixed={} ev_skip={} ev_raw={} ev_ord={} ev_set={} jobs={} disabled={} reason={}",
        mode_name(t.mode),
        w.frames,
        w.grav_pub,
        w.grav_lag1,
        w.grav_lag_n,
        w.grav_lag_max,
        w.grav_busy0,
        w.grav_timeouts,
        w.grav_wait_us,
        w.grav_nolock,
        w.mt_skip,
        w.mt_busy0,
        w.mt_busy15,
        w.mt_old,
        w.mt_new,
        w.mt_timeouts,
        w.mt_wait_us,
        w.rm_busy0,
        w.rm_busy15,
        w.rm_old,
        w.rm_new,
        w.rm_timeouts,
        w.rm_wait_us,
        w.path_timeouts,
        w.path_wait_us,
        w.ev_nodes,
        w.ev_seg,
        w.ev_moved,
        w.ev_groups,
        w.ev_mixed,
        w.ev_skip,
        hex(w.ev_raw),
        hex(w.ev_ord),
        hex(w.ev_set),
        t.jobs,
        t.disabled,
        if t.reason.is_empty() { "none" } else { t.reason }.replace(' ', "_"),
    )
}

unsafe fn push_string(lua: *mut LuaState, text: &str) {
    (api().pushlstring.unwrap())(lua, text.as_ptr() as *const c_char, text.len());
}

/// `timing_start(mode, timeoutMs)`: mode is "observe" or "join".
pub(crate) unsafe extern "C-unwind" fn lua_start(lua: *mut LuaState) -> c_int {
    let functions = api();
    let mut length = 0usize;
    let text = (functions.checklstring.unwrap())(lua, 1, &mut length);
    let timeout = (functions.checknumber.unwrap())(lua, 2);
    let mode = match slice::from_raw_parts(text as *const u8, length) {
        b"observe" => Mode::Observe,
        b"join" => Mode::Join,
        _ => return failure(lua, "timing mode must be observe or join"),
    };
    if !(1.0..=1000.0).contains(&timeout) || timeout != timeout.trunc() {
        return failure(lua, "timing timeout must be an integer from 1 to 1000 ms");
    }
    if CURRENT.is_null()
        || !(*CURRENT).armed
        || GetCurrentThreadId() != OWNER_THREAD
        || !context_valid()
    {
        return failure(lua, "timing requires the armed observer on its thread");
    }
    if !verify_sites() {
        return failure(lua, "timing signatures do not match this executable");
    }
    match install(mode, timeout as u32) {
        Ok(()) => {
            (functions.pushboolean.unwrap())(lua, 1);
            1
        }
        Err(reason) => failure(lua, reason),
    }
}

pub(crate) unsafe extern "C-unwind" fn lua_stop(lua: *mut LuaState) -> c_int {
    disarm_with("stopped");
    (api().pushboolean.unwrap())(lua, 1);
    1
}

/// Returns the formatted window counters (and resets the window) plus
/// whether timing is still active.
pub(crate) unsafe extern "C-unwind" fn lua_stats(lua: *mut LuaState) -> c_int {
    let active = state().mode != Mode::Off;
    let line = take_line();
    push_string(lua, &line);
    (api().pushboolean.unwrap())(lua, active as c_int);
    2
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeWorker {
        memory: Vec<u8>,
    }

    impl FakeWorker {
        unsafe fn new(worker: &Worker) -> Self {
            let mut memory = vec![0u8; worker.extent + 64];
            InitializeCriticalSection(memory.as_mut_ptr().add(worker.cs) as *mut c_void);
            let event = CreateEventW(ptr::null_mut(), 1, 0, ptr::null());
            ptr::write(
                memory.as_mut_ptr().add(worker.waiter + 8) as *mut *mut c_void,
                event,
            );
            ptr::write(memory.as_mut_ptr().add(worker.state) as *mut u32, 2);
            if worker.active != 0 {
                ptr::write(memory.as_mut_ptr().add(worker.active) as *mut u32, 1);
            }
            FakeWorker { memory }
        }
        fn base(&mut self) -> *mut u8 {
            self.memory.as_mut_ptr()
        }
    }

    /// Emulates the worker loop head: set done under the lock, then signal
    /// the single waiter if its flag is set.
    unsafe fn finish(
        worker: &'static Worker,
        address: usize,
        delay_ms: u32,
    ) -> std::thread::JoinHandle<()> {
        std::thread::spawn(move || {
            Sleep(delay_ms);
            let object = address as *mut u8;
            EnterCriticalSection(object.add(worker.cs) as *mut c_void);
            ptr::write_volatile(object.add(worker.done), 1);
            if ptr::read_volatile(object.add(worker.waiter) as *const u32) != 0 {
                SetEvent(ptr::read(
                    object.add(worker.waiter + 8) as *const *mut c_void
                ));
            }
            LeaveCriticalSection(object.add(worker.cs) as *mut c_void);
        })
    }

    static mut FAKE_RESULT: c_int = 1;
    unsafe extern "system" fn fake_try(_: *mut c_void) -> c_int {
        FAKE_RESULT
    }

    #[test]
    fn timing_import_wrapper_counts_only_known_sites() {
        unsafe {
            X4LAN_TIMING_TECS_ORIGINAL.store(fake_try as *const () as usize, Ordering::SeqCst);
            X4LAN_TIMING_TECS_ZONE_SITE.store(usize::MAX, Ordering::SeqCst);
            X4LAN_TIMING_TECS_GRAVIDAR_SITE.store(usize::MAX, Ordering::SeqCst);
            let zone = X4LAN_TIMING_TECS_ZONE_FAILS.load(Ordering::SeqCst);
            FAKE_RESULT = 1;
            assert_eq!(x4lan_timing_tecs_hook(ptr::null_mut()), 1);
            FAKE_RESULT = 0;
            for round in 0..3 {
                assert_eq!(x4lan_timing_tecs_hook(ptr::null_mut()), 0);
                if round == 0 {
                    let site = X4LAN_TIMING_TECS_LAST_FAIL.load(Ordering::SeqCst);
                    assert_ne!(site, 0);
                    X4LAN_TIMING_TECS_ZONE_SITE.store(site, Ordering::SeqCst);
                }
            }
            assert_eq!(
                X4LAN_TIMING_TECS_ZONE_FAILS.load(Ordering::SeqCst),
                zone + 2
            );
            FAKE_RESULT = 1;
            assert_eq!(x4lan_timing_tecs_hook(ptr::null_mut()), 1);
            assert_eq!(
                X4LAN_TIMING_TECS_ZONE_FAILS.load(Ordering::SeqCst),
                zone + 2
            );
        }
    }

    #[test]
    fn timing_worker_join_waits_and_times_out() {
        unsafe {
            let mut fake = FakeWorker::new(&REGION);
            let base = fake.base();
            let handle = finish(&REGION, base as usize, 20);
            let started = qpc_seconds();
            assert_eq!(wait_worker(&REGION, base, 2000), Wait::Done);
            handle.join().unwrap();
            assert!(elapsed_us(started) < 1_500_000);
            assert_eq!(ptr::read(base.add(REGION.waiter) as *const u32), 0);
            assert!(worker_done(&REGION, base));
            ptr::write(base.add(REGION.done), 0);
            assert_eq!(wait_worker(&REGION, base, 15), Wait::Timeout);
            assert_eq!(ptr::read(base.add(REGION.waiter) as *const u32), 0);
            ptr::write(base.add(REGION.waiter) as *mut u32, 1);
            assert_eq!(wait_worker(&REGION, base, 15), Wait::Skipped);
            ptr::write(base.add(REGION.waiter) as *mut u32, 0);
            assert!(worker_running(&REGION, base));
            ptr::write(base.add(REGION.state) as *mut u32, 3);
            assert!(!worker_running(&REGION, base));
            let mut gravidar = FakeWorker::new(&GRAVIDAR);
            assert!(worker_running(&GRAVIDAR, gravidar.base()));
            ptr::write(gravidar.base().add(GRAVIDAR.active) as *mut u32, 0);
            assert!(!worker_running(&GRAVIDAR, gravidar.base()));
            CloseHandle(ptr::read(base.add(REGION.waiter + 8) as *const *mut c_void));
        }
    }

    #[test]
    fn timing_time_classification() {
        assert_eq!(classify_time(2.0, 2.0, 1.0), 2);
        assert_eq!(classify_time(1.0, 2.0, 1.0), 1);
        assert_eq!(classify_time(1.5, 2.0, 1.0), 0);
    }

    unsafe fn list() -> usize {
        let sentinel = Box::leak(Box::new([0usize; 8])).as_mut_ptr() as usize;
        ptr::write(sentinel as *mut usize, sentinel);
        ptr::write((sentinel + 8) as *mut usize, sentinel);
        sentinel
    }

    unsafe fn append(sentinel: usize, kind: u32, source: u64, event: usize, time: f64) {
        let node = Box::leak(Box::new([0usize; 8])).as_mut_ptr() as usize;
        let last = ptr::read((sentinel + 8) as *const usize);
        ptr::write(node as *mut usize, sentinel);
        ptr::write((node + 8) as *mut usize, last);
        ptr::write(last as *mut usize, node);
        ptr::write((sentinel + 8) as *mut usize, node);
        ptr::write((node + 0x10) as *mut u32, kind);
        ptr::write((node + 0x18) as *mut u64, source);
        ptr::write((node + 0x20) as *mut usize, event);
        ptr::write((node + 0x28) as *mut f64, time);
    }

    #[test]
    fn timing_queue_canonical_order_is_completion_independent() {
        unsafe {
            if IMAGE == 0 {
                IMAGE = Box::leak(vec![0u8; IMAGE_SIZE].into_boxed_slice()).as_mut_ptr() as usize;
            }
            let events: Vec<usize> = (0..4)
                .map(|index| {
                    let event = Box::leak(Box::new([0usize; 2])).as_mut_ptr() as usize;
                    ptr::write(event as *mut usize, IMAGE + 0x2ba9120 + index * 0x38);
                    event
                })
                .collect();
            // Two job batches whose completion order differs between games.
            let batch_a = [
                (7u64, events[0], 10.0),
                (7, events[1], 10.0),
                (3, events[2], 10.0),
            ];
            let batch_b = [(5u64, events[3], 10.0), (3, events[0], 9.0)];
            let mut hashes = Vec::new();
            for order in [[&batch_a[..], &batch_b[..]], [&batch_b[..], &batch_a[..]]] {
                let sentinel = list();
                append(sentinel, 3, 1, events[0], 1.0);
                let before = 1;
                for batch in order {
                    for &(source, event, time) in batch {
                        append(sentinel, 3, source, event, time);
                    }
                }
                let raw = scan_queue(sentinel, 6).expect("intact list");
                let result = canonicalize(sentinel, 6 - before).expect("segment");
                assert!(!result.mixed);
                let after = scan_queue(sentinel, 6).expect("intact list");
                assert_eq!(raw.set, after.set);
                hashes.push((raw.order, after.order));
                // Same source keeps its original relative order (7: events 0 then 1).
                let mut node = ptr::read(sentinel as *const usize);
                let mut rows = Vec::new();
                while node != sentinel {
                    rows.push(node_fields(node));
                    node = ptr::read(node as *const usize);
                }
                let sevens: Vec<u64> = rows
                    .iter()
                    .filter(|row| row.1 == 7)
                    .map(|row| row.2)
                    .collect();
                assert_eq!(sevens, vec![0x2ba9120, 0x2ba9120 + 0x38]);
                assert_eq!(rows[0].1, 1);
                assert_eq!((rows[1].1, f64::from_bits(rows[1].3)), (3, 9.0));
            }
            assert_ne!(hashes[0].0, hashes[1].0);
            assert_eq!(hashes[0].1, hashes[1].1);

            let sentinel = list();
            append(sentinel, 3, 9, events[0], 1.0);
            append(sentinel, 4, 2, 0x10, 1.0); // kind 4 carries no event pointer
            let raw = scan_queue(sentinel, 2).unwrap();
            assert!(canonicalize(sentinel, 2).unwrap().mixed);
            assert_eq!(scan_queue(sentinel, 2).unwrap().order, raw.order);
            assert!(scan_queue(sentinel, 3).is_none());
            assert!(canonicalize(sentinel, 3).is_none());
        }
    }

    #[test]
    fn timing_pathing_peek_preserves_one_count() {
        unsafe {
            if IMAGE == 0 {
                IMAGE = Box::leak(vec![0u8; IMAGE_SIZE].into_boxed_slice()).as_mut_ptr() as usize;
            }
            let mut manager = vec![0u8; 0x100];
            let semaphore = CreateSemaphoreW(ptr::null_mut(), 0, 1, ptr::null());
            ptr::write(
                manager.as_mut_ptr().add(0x10) as *mut *mut c_void,
                semaphore,
            );
            ptr::write(manager.as_mut_ptr().add(0x84) as *mut u32, 2);
            ptr::write((IMAGE + PATHING_RVA) as *mut *mut u8, manager.as_mut_ptr());
            assert_eq!(join_pathing(10), Wait::Timeout);
            let address = semaphore as usize;
            let releaser = std::thread::spawn(move || {
                Sleep(15);
                ReleaseSemaphore(address as *mut c_void, 1, ptr::null_mut());
            });
            assert_eq!(join_pathing(2000), Wait::Done);
            releaser.join().unwrap();
            assert_eq!(WaitForSingleObject(semaphore, 0), WAIT_OBJECT_0);
            assert_ne!(WaitForSingleObject(semaphore, 0), WAIT_OBJECT_0);
            manager[0x88] = 1;
            assert_eq!(join_pathing(10), Wait::Skipped);
            ptr::write((IMAGE + PATHING_RVA) as *mut *mut u8, ptr::null_mut());
            assert_eq!(join_pathing(10), Wait::Skipped);
            CloseHandle(semaphore);
        }
    }

    #[test]
    fn timing_lifecycle_registration_join_and_disarm() {
        unsafe {
            IMAGE = Box::leak(vec![0u8; IMAGE_SIZE].into_boxed_slice()).as_mut_ptr() as usize;
            assert!(!verify_sites());
            let mut table = vec![ptr::null_mut::<c_void>(); 40];
            let mut ui = vec![0u8; 256];
            let mut anark = vec![0u8; 0x400];
            let mut owner = Owner {
                frames: 0,
                armed: true,
            };
            ptr::write(
                ui.as_mut_ptr() as *mut *const c_void,
                (IMAGE + UI_VTABLE_RVA) as *const c_void,
            );
            ptr::write((IMAGE + ANARK_RVA) as *mut *mut u8, anark.as_mut_ptr());
            ptr::write(
                (IMAGE + VBI_RVA) as *mut *mut *mut c_void,
                table.as_mut_ptr(),
            );
            anark[0x3f0] = 1;
            table[3] = addr_of_mut!(owner) as *mut c_void;
            table[6] = ui.as_mut_ptr() as *mut c_void;
            table[SLOT] = addr_of_mut!(HOOK) as *mut c_void;
            REGISTERED_TABLE = table.as_mut_ptr();
            REGISTERED_UI = ui.as_mut_ptr();
            REGISTERED_ANARK = anark.as_mut_ptr();
            CURRENT = addr_of_mut!(owner);
            OWNER_THREAD = GetCurrentThreadId();

            let name: Vec<u16> = "kernel32.dll\0".encode_utf16().collect();
            let original = GetProcAddress(
                GetModuleHandleW(name.as_ptr()),
                b"TryEnterCriticalSection\0".as_ptr(),
            );
            ptr::write((IMAGE + TECS_IAT_RVA) as *mut *mut c_void, original);

            table[START_SLOT] = 0x1usize as *mut c_void;
            assert_eq!(install(Mode::Join, 50), Err("timing VBI slot occupied"));
            table[START_SLOT] = ptr::null_mut();
            ptr::write((IMAGE + TECS_IAT_RVA) as *mut usize, 0x1234);
            assert_eq!(
                install(Mode::Join, 50),
                Err("TryEnterCriticalSection import unexpected")
            );
            assert!(table[START_SLOT].is_null() && table[END_SLOT].is_null());
            ptr::write((IMAGE + TECS_IAT_RVA) as *mut *mut c_void, original);

            // Fake engine: a command queue with its lock and one busy worker.
            InitializeCriticalSection((IMAGE + QUEUE_LOCK_RVA) as *mut c_void);
            let sentinel = list();
            ptr::write((IMAGE + QUEUE_HEAD_RVA) as *mut usize, sentinel);
            ptr::write((IMAGE + QUEUE_HEAD_RVA + 8) as *mut usize, 0);
            let mut region = FakeWorker::new(&REGION);
            let region_base = region.base();
            ptr::write((IMAGE + REGION_RVA) as *mut *mut u8, region_base);

            assert_eq!(install(Mode::Join, 200), Ok(()));
            assert_eq!(table[START_SLOT], addr_of_mut!(START_HOOK) as *mut c_void);
            assert_eq!(table[END_SLOT], addr_of_mut!(END_HOOK) as *mut c_void);
            assert_eq!(
                ptr::read((IMAGE + TECS_IAT_RVA) as *const usize),
                x4lan_timing_tecs_hook as *const () as usize
            );
            assert_eq!(install(Mode::Join, 50), Err("timing already active"));

            frame_end(addr_of_mut!(END_HOOK));
            assert_eq!(state().window.rm_busy15, 1);
            assert_eq!(state().snapshot, Some((sentinel, 0)));
            append(sentinel, 3, 9, 0, 2.0);
            append(sentinel, 3, 4, 0, 2.0);
            ptr::write((IMAGE + QUEUE_HEAD_RVA + 8) as *mut usize, 2);
            let worker = finish(&REGION, region_base as usize, 10);
            frame_start(addr_of_mut!(START_HOOK));
            worker.join().unwrap();
            let line = take_line();
            assert!(line.contains("mode=join frames=1 "), "{line}");
            assert!(
                line.contains(" rm_busy0=1 ") && line.contains(" rm_to=0 "),
                "{line}"
            );
            assert!(line.contains(" ev_n=2 ev_seg=2 ev_moved=2 "), "{line}");
            assert_eq!(
                ptr::read((ptr::read(sentinel as *const usize) + 0x18) as *const u64),
                4
            );

            // Loading pauses work without disarming.
            ptr::write((table.as_mut_ptr() as *mut u8).add(0x130) as *mut c_int, 1);
            frame_start(addr_of_mut!(START_HOOK));
            assert!(take_line().contains(" frames=0 "));
            ptr::write((table.as_mut_ptr() as *mut u8).add(0x130) as *mut c_int, 0);

            // A busy worker that never finishes times out and falls back.
            ptr::write(region_base.add(REGION.done), 0);
            state().timeout_ms = 5;
            frame_start(addr_of_mut!(START_HOOK));
            assert!(take_line().contains(" rm_to=1 "));

            // Foreign thread: everything is restored.
            OWNER_THREAD += 1;
            frame_start(addr_of_mut!(START_HOOK));
            assert!(table[START_SLOT].is_null() && table[END_SLOT].is_null());
            assert_eq!(
                ptr::read((IMAGE + TECS_IAT_RVA) as *const *mut c_void),
                original
            );
            assert_eq!(state().mode, Mode::Off);
            assert!(take_line().contains("reason=context_lost"));
            OWNER_THREAD = GetCurrentThreadId();

            // Observe mode never waits or reorders.
            assert_eq!(install(Mode::Observe, 50), Ok(()));
            frame_end(addr_of_mut!(END_HOOK));
            append(sentinel, 3, 8, 0, 3.0);
            append(sentinel, 3, 1, 0, 3.0);
            ptr::write((IMAGE + QUEUE_HEAD_RVA + 8) as *mut usize, 4);
            frame_start(addr_of_mut!(START_HOOK));
            let line = take_line();
            assert!(
                line.contains("mode=observe frames=1 ") && line.contains(" ev_seg=2 ev_moved=0 "),
                "{line}"
            );
            assert!(
                line.contains(" rm_busy0=1 ") && line.contains(" rm_to=0 "),
                "{line}"
            );
            disarm();
            assert!(table[START_SLOT].is_null() && table[END_SLOT].is_null());
            assert_eq!(
                ptr::read((IMAGE + TECS_IAT_RVA) as *const *mut c_void),
                original
            );
            disarm();

            CURRENT = ptr::null_mut();
            REGISTERED_TABLE = ptr::null_mut();
            CloseHandle(ptr::read(
                region_base.add(REGION.waiter + 8) as *const *mut c_void
            ));
        }
    }
}
