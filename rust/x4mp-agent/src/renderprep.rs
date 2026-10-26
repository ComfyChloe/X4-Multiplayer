//! Opt-in render-prep skip for the fixed-step scheduler (`X4MP_SCHED_SKIP_RENDERPREP=1`,
//! Linux only).
//!
//! VBI slot 10 holds X4::GameRender (embedded in the GameClient at +0x1e8). Its Move
//! (0xfb0180) prepares the render lists that the *next* `FrameTick` renders: the UI,
//! character and GameClient camera render-bank handlers. On frames the scheduler will
//! not render, that work is wasted on a headless server.
//!
//! Mechanism (data-only, restored on every disarm path):
//! - The slot-10 pointer is swapped (compare-exchange) for a wrapper handler. The wrapper
//!   always forwards to the original GameRender::Move. On skip frames it clears the
//!   GameRender enabled byte (+8) for the duration of that call, so only the render-bank
//!   loop is skipped and the rest of Move (the deferred `fclose`) still runs. All on the
//!   MoveWorker thread, synchronously.
//! - Whether a frame prepares is decided one frame ahead by the scheduler's lookahead
//!   (`slot13_step(.., ahead)`): slot 10 of frame N prepares exactly when `FrameTick N+1`
//!   renders.
//! - RNG isolation: the camera render draws the MoveWorker TLS RNG (`fs:[-0x2720]`,
//!   Linux counterparts of U::Scene vfunc693 helper 0x1f48560, SpawnEffect 0x17976b0 and
//!   the effect creator 0x1ecefd0 all call Random 0x1a491c0). The wrapper saves the RNG
//!   state before slot 10 and restores it after, on every frame, so the simulation's RNG
//!   stream is identical whether render prep runs or not. (Not covered: effect objects the
//!   camera render spawns, e.g. mass-traffic welding effects, allocate UniverseIDs; with
//!   the skip they are simply not created. Documented, opt-in.)
//!
//! Skipped render prep never leaves stale data: each render-list half and each view slot
//! is emptied when it is consumed (flip 0x218e2c0, finalize 0x20965c0), so a FrameTick
//! that renders an unprepared half renders an empty one. With the lookahead this only
//! happens after a wait timeout or at disarm; it is counted (`empty_renders`).

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicU8, AtomicUsize, Ordering};

use crate::agent::{on_owner_thread, read_u64};
use crate::pinned;

#[repr(C)]
struct Handler {
    vtable: *const [usize; 4],
}
// SAFETY: immutable after `arm`; only its address is shared with the engine.
unsafe impl Sync for Handler {}

extern "C" fn no_op(_this: *mut Handler) {}
static mut VTABLE: [usize; 4] = [0; 4];
static mut HANDLER: Handler = Handler { vtable: std::ptr::null() };

static ARMED: AtomicBool = AtomicBool::new(false);
static TABLE: AtomicU64 = AtomicU64::new(0);
static CLIENT: AtomicU64 = AtomicU64::new(0);
static RENDER: AtomicU64 = AtomicU64::new(0);
/// Decision for the next slot-10 call (set by slot 13 of the previous frame).
static PREP_NEXT: AtomicBool = AtomicBool::new(true);
/// Whether the latest slot-10 call prepared render lists.
static LAST_PREP: AtomicBool = AtomicBool::new(true);
static FAULT: AtomicU32 = AtomicU32::new(0);

static CALLS: AtomicU64 = AtomicU64::new(0);
static KEPT: AtomicU64 = AtomicU64::new(0);
static SKIPPED: AtomicU64 = AtomicU64::new(0);
static RNG_ISOLATED: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RpFault {
    /// Wrapper called on a thread other than the Movement worker.
    ForeignThread = 1,
    /// GameClient/GameRender no longer as armed (slot 3, vtables): not forwarded.
    ObjectChanged = 2,
    /// A Rust panic inside the wrapper.
    Panic = 3,
}

pub fn fault() -> u32 {
    FAULT.load(Ordering::SeqCst)
}
pub fn armed() -> bool {
    ARMED.load(Ordering::SeqCst)
}
pub fn calls() -> u64 {
    CALLS.load(Ordering::SeqCst)
}
pub fn last_prep() -> bool {
    LAST_PREP.load(Ordering::SeqCst)
}
pub fn set_prep_next(prep: bool) {
    PREP_NEXT.store(prep, Ordering::SeqCst);
}
/// (kept, skipped, rng_isolated)
pub fn counters() -> (u64, u64, u64) {
    (KEPT.load(Ordering::Relaxed), SKIPPED.load(Ordering::Relaxed), RNG_ISOLATED.load(Ordering::Relaxed))
}

fn handler_address() -> usize {
    std::ptr::addr_of!(HANDLER) as usize
}

unsafe fn slot(table: u64, index: usize) -> &'static AtomicUsize {
    &*((table + 8 * index as u64) as *const AtomicUsize)
}

#[inline]
unsafe fn tls_rng() -> u64 {
    let v: u64;
    std::arch::asm!("mov {v}, qword ptr fs:[{o}]", v = out(reg) v, o = const pinned::TLS_RNG_STATE,
        options(nostack, readonly, preserves_flags));
    v
}

#[inline]
unsafe fn set_tls_rng(v: u64) {
    std::arch::asm!("mov qword ptr fs:[{o}], {v}", v = in(reg) v, o = const pinned::TLS_RNG_STATE,
        options(nostack, preserves_flags));
}

/// The object pointer currently in slot 10 is the pinned GameRender of `client`.
pub fn slot_is_game_render(table: u64, client: u64) -> bool {
    // SAFETY: table/client verified mapped by the caller (admission).
    unsafe {
        let render = client + pinned::GAME_RENDER_IN_CLIENT;
        slot(table, pinned::GAME_RENDER_SLOT).load(Ordering::SeqCst) as u64 == render
            && read_u64(render) == pinned::GAME_RENDER_VTABLE
    }
}

/// Swap the wrapper into slot 10 (owner thread, scheduler admission). Fails closed.
pub fn arm(table: u64, client: u64) -> Result<(), &'static str> {
    if !on_owner_thread() {
        return Err("renderprep_arm_foreign_thread");
    }
    if !slot_is_game_render(table, client) {
        return Err("slot10_not_game_render");
    }
    let render = client + pinned::GAME_RENDER_IN_CLIENT;
    // SAFETY: single-threaded initialisation before the pointer is published.
    unsafe {
        VTABLE = [
            no_op as *const () as usize,
            no_op as *const () as usize,
            wrapper_move as *const () as usize,
            no_op as *const () as usize,
        ];
        HANDLER = Handler { vtable: std::ptr::addr_of!(VTABLE) };
    }
    TABLE.store(table, Ordering::SeqCst);
    CLIENT.store(client, Ordering::SeqCst);
    RENDER.store(render, Ordering::SeqCst);
    PREP_NEXT.store(true, Ordering::SeqCst);
    LAST_PREP.store(true, Ordering::SeqCst);
    ARMED.store(true, Ordering::SeqCst);
    // SAFETY: verified slot; compare-exchange never overwrites a foreign pointer.
    let won = unsafe { slot(table, pinned::GAME_RENDER_SLOT) }
        .compare_exchange(render as usize, handler_address(), Ordering::SeqCst, Ordering::SeqCst)
        .is_ok();
    if !won {
        ARMED.store(false, Ordering::SeqCst);
        return Err("slot10_changed_during_arm");
    }
    Ok(())
}

/// Put GameRender back into slot 10 (any thread, idempotent). The wrapper itself keeps
/// forwarding if the dispatcher had already loaded its pointer.
pub fn disarm() -> bool {
    if !ARMED.swap(false, Ordering::SeqCst) {
        return false;
    }
    let table = TABLE.load(Ordering::SeqCst);
    let render = RENDER.load(Ordering::SeqCst);
    // SAFETY: the table lives for the process once created; checked against the global.
    unsafe {
        if table != 0 && read_u64(pinned::VBI_TABLE_GLOBAL) == table {
            let _ = slot(table, pinned::GAME_RENDER_SLOT).compare_exchange(
                handler_address(),
                render as usize,
                Ordering::SeqCst,
                Ordering::SeqCst,
            );
        }
    }
    true
}

/// Slot 10 still holds the wrapper (checked by slot 13 every frame while armed).
pub fn slot_holds_wrapper() -> bool {
    let table = TABLE.load(Ordering::SeqCst);
    // SAFETY: table verified at arm time.
    table != 0 && unsafe { slot(table, pinned::GAME_RENDER_SLOT).load(Ordering::SeqCst) } == handler_address()
}

fn set_fault(f: RpFault) {
    let _ = FAULT.compare_exchange(0, f as u32, Ordering::SeqCst, Ordering::SeqCst);
}

/// GameRender::Move of the original object, through its (pinned) vtable.
unsafe fn call_original(render: u64) {
    let vtable = read_u64(render) as *const usize;
    let mv: extern "C" fn(u64) = std::mem::transmute(*vtable.add(2));
    mv(render);
}

extern "C" fn wrapper_move(_this: *mut Handler) {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let table = TABLE.load(Ordering::SeqCst);
        let client = CLIENT.load(Ordering::SeqCst);
        let render = RENDER.load(Ordering::SeqCst);
        // SAFETY: slot 3 is read without dereferencing first; client and render are
        // dereferenced only while slot 3 still holds the armed GameClient.
        let objects_ok = unsafe {
            read_u64(pinned::VBI_TABLE_GLOBAL) == table
                && slot(table, pinned::GAME_CLIENT_SLOT).load(Ordering::SeqCst) as u64 == client
                && read_u64(client) == pinned::GAME_CLIENT_VTABLE
                && read_u64(render) == pinned::GAME_RENDER_VTABLE
        };
        if !objects_ok {
            set_fault(RpFault::ObjectChanged);
            return; // do not touch a possibly destroyed object; one frame without prep
        }
        if !on_owner_thread() {
            set_fault(RpFault::ForeignThread);
            // SAFETY: vanilla behaviour on an unexpected thread: forward unchanged.
            unsafe { call_original(render) };
            return;
        }
        let armed = ARMED.load(Ordering::SeqCst);
        let prep = !armed || PREP_NEXT.load(Ordering::SeqCst);
        // SAFETY: MoveWorker thread; executable static TLS.
        let rng = unsafe { tls_rng() };
        if prep {
            // SAFETY: verified GameRender, owner thread (the engine's own call context).
            unsafe { call_original(render) };
            KEPT.fetch_add(1, Ordering::Relaxed);
        } else {
            // SAFETY: GameRender+8 is a bool inside the verified object; only GameClient
            // methods on this same thread write it (to 1).
            let enabled = unsafe { &*((render + pinned::GAME_RENDER_ENABLED) as *const AtomicU8) };
            let cleared = enabled.compare_exchange(1, 0, Ordering::SeqCst, Ordering::SeqCst).is_ok();
            // SAFETY: as above; render bank skipped, the rest of Move runs.
            unsafe { call_original(render) };
            if cleared {
                let _ = enabled.compare_exchange(0, 1, Ordering::SeqCst, Ordering::SeqCst);
            }
            SKIPPED.fetch_add(1, Ordering::Relaxed);
        }
        if armed {
            // SAFETY: same thread, same TLS slot.
            unsafe {
                if tls_rng() != rng {
                    set_tls_rng(rng);
                    RNG_ISOLATED.fetch_add(1, Ordering::Relaxed);
                }
            }
        }
        LAST_PREP.store(prep, Ordering::SeqCst);
        CALLS.fetch_add(1, Ordering::SeqCst);
    }));
    if result.is_err() {
        set_fault(RpFault::Panic);
    }
}
