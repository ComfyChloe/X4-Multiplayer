//! Combat census (lane runs only): what the weapon and bullet updates do per simulation
//! frame, measured through data-only vtable hooks (rewrite-coord/hooks.tsv):
//!
//! * slot 674 (`Move`) of `U::Weapon`, `U::Turret`, `U::MissileLauncher`, `U::MissileTurret`
//!   (one function, 0x8132d0) and of `U::Bullet` (0xd4f090): pass-through wrappers. On the
//!   owner thread (the simulation thread that started the census) they count the call, read
//!   a few fields of the object before and after, count the steps the call made on the
//!   thread RNG (TLS block + 0x318) and time the call with `rdtsc`;
//! * slot 998 (`FireShot`, one bullet or missile per call) of the same four weapon classes:
//!   signature-free counting thunks (`lock inc` + jump to the original).
//!
//! Nothing in the game is written: the originals run with their own arguments and their
//! results are returned unchanged. Frames are delimited by the frame's game time (offset
//! 0x300 of the TLS block); a frame is stored when the next one begins, so totals between the
//! first combat call of one frame and of the next (RNG steps of every caller on the thread,
//! wall ticks) belong to the earlier frame. Field meanings: reports/REWRITE-COMBAT.md
//! sections 3 and 6.

use std::arch::{asm, global_asm};
use std::ffi::c_void;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicPtr, AtomicU32, AtomicU64, AtomicUsize, Ordering};

use super::vthook::EntryHook;

/// Pinned RVAs (build 23660954).
pub mod rva {
    pub const WEAPON_MOVE: u32 = 0x0081_32d0;
    pub const BULLET_MOVE: u32 = 0x00d4_f090;
    pub const FIRE_SHOT: u32 = 0x0081_85b0;
    pub const MISSILE_LAUNCHER_FIRE_SHOT: u32 = 0x0060_9b50;
    pub const MISSILE_TURRET_FIRE_SHOT: u32 = 0x0060_aaf0;
    /// Primary vtables: Weapon, Turret, MissileLauncher, MissileTurret, Bullet.
    pub const VTABLES: [u32; 5] = [0x02b6_8278, 0x02b6_e910, 0x02b0_2988, 0x02b0_0620, 0x02c4_2ac8];
    /// Byte offset of slot 674 (Move) and slot 998 (FireShot) in a vtable.
    pub const SLOT_MOVE: u32 = 674 * 8;
    pub const SLOT_FIRE_SHOT: u32 = 998 * 8;
}

/// Thread RNG step of the engine (`Random::U64State` 0x14ddb30 and its siblings):
/// `state = rotr64(state * 6364136223846793005 + 1442695040888963407, 30)`.
pub fn rng_step(state: u64) -> u64 {
    state.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1_442_695_040_888_963_407).rotate_right(30)
}

/// Number of RNG steps from `from` to `to`, if at most `cap`.
pub fn steps_between(from: u64, to: u64, cap: u32) -> Option<u32> {
    let mut state = from;
    for n in 0..=cap {
        if state == to {
            return Some(n);
        }
        state = rng_step(state);
    }
    None
}

/// Steps searched per call and per frame.
const CALL_CAP: u32 = 4_096;
const FRAME_CAP: u32 = 200_000;

/// One simulation frame (all counts are calls on the owner thread).
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Frame {
    /// Game time of the frame.
    pub now: f64,
    /// Weapon Move calls (any depth) and how many ran inside another combat Move.
    pub weapon_calls: u32,
    pub weapon_nested: u32,
    /// Weapon calls by class: Weapon, Turret, MissileLauncher, MissileTurret.
    pub by_class: [u32; 4],
    /// The shoot controller (+0x318) had a primary target reference (+0x10) before the
    /// call. This does not cover secondary targets of MultipleShootController or imply idle.
    pub with_target: u32,
    /// Aiming flag (+0x354) / firing flag (+0x30d) after the call.
    pub aiming: u32,
    pub firing: u32,
    /// Last shot time (+0x310) changed in the call (at least one salvo).
    pub fired: u32,
    /// FireShot calls of Weapon/Turret (bullets) and of the missile classes.
    pub shots: u32,
    pub missile_shots: u32,
    /// Bullet Move calls; flight checks (last check +0x268 became now); hits (+0x280 grew)
    /// and hit-counter resets (+0x280 fell, a ricochet).
    pub bullet_calls: u32,
    pub bullet_checks: u32,
    pub bullet_hits: u32,
    pub hit_resets: u32,
    /// Move calls with last check +0x268 still equal to shot time +0x278. This is not a
    /// unique-bullet count: the equality can persist until the first actual flight check.
    /// `fresh_same_frame` is the subset with shot time after the previous observed frame's
    /// game time, not proof of creation in this frame (shot time includes salvo delay/jitter).
    pub bullet_fresh: u32,
    pub fresh_same_frame: u32,
    /// Thread-RNG steps inside outermost weapon / bullet Move calls, the largest in one call,
    /// calls whose steps were not found within the cap.
    pub rng_weapon: u32,
    pub rng_bullet: u32,
    pub rng_max_call: u32,
    pub rng_unmatched: u32,
    /// All RNG steps on the thread from this frame's first combat call to the next frame's
    /// (u32::MAX: more than the cap).
    pub rng_frame: u32,
    /// rdtsc ticks inside outermost weapon / bullet Move calls, and first call to first call.
    pub weapon_ticks: u64,
    pub bullet_ticks: u64,
    pub frame_ticks: u64,
}

impl Frame {
    pub const COLUMNS: &'static str = "now\tweapon_calls\tweapon_nested\tweapon\tturret\tmissile_launcher\tmissile_turret\twith_target\taiming\tfiring\tfired\tshots\tmissile_shots\tbullet_calls\tbullet_checks\tbullet_hits\thit_resets\tbullet_fresh\tfresh_same_frame\trng_weapon\trng_bullet\trng_max_call\trng_unmatched\trng_frame\tweapon_ticks\tbullet_ticks\tframe_ticks";

    pub fn line(&self) -> String {
        let c = &self.by_class;
        format!(
            "{:.6}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
            self.now,
            self.weapon_calls,
            self.weapon_nested,
            c[0],
            c[1],
            c[2],
            c[3],
            self.with_target,
            self.aiming,
            self.firing,
            self.fired,
            self.shots,
            self.missile_shots,
            self.bullet_calls,
            self.bullet_checks,
            self.bullet_hits,
            self.hit_resets,
            self.bullet_fresh,
            self.fresh_same_frame,
            self.rng_weapon,
            self.rng_bullet,
            self.rng_max_call,
            self.rng_unmatched,
            if self.rng_frame == u32::MAX { -1 } else { self.rng_frame as i64 },
            self.weapon_ticks,
            self.bullet_ticks,
            self.frame_ticks
        )
    }
}

/// Owner-thread state of a running census.
#[derive(Debug)]
pub struct Census {
    pub frames: Vec<Frame>,
    pub dropped_frames: u64,
    pub uninitialised_tls: u64,
    current: Frame,
    open: bool,
    /// Game time of the previous frame (NaN before the second frame).
    prev_now: f64,
    rng_start: u64,
    tick_start: u64,
    shot_base: [u64; 4],
    depth: u32,
    /// Class vtables: Weapon, Turret, MissileLauncher, MissileTurret.
    classes: [usize; 4],
    pub failed: Option<&'static str>,
}

/// FireShot counts by hooked entry (Weapon, Turret, MissileLauncher, MissileTurret), any thread.
pub static SHOTS: [AtomicU64; 4] = [const { AtomicU64::new(0) }; 4];
/// The originals the thunks jump to; set before the entries are swapped, never cleared.
pub static SHOT_ORIGINALS: [AtomicUsize; 4] = [const { AtomicUsize::new(0) }; 4];
static WEAPON_ORIGINAL: AtomicUsize = AtomicUsize::new(0);
static BULLET_ORIGINAL: AtomicUsize = AtomicUsize::new(0);
static CENSUS: AtomicPtr<Census> = AtomicPtr::new(std::ptr::null_mut());
static OWNER: AtomicU32 = AtomicU32::new(0);
/// Hooked calls on other threads (passed through).
pub static FOREIGN: AtomicU64 = AtomicU64::new(0);

global_asm!(
    ".p2align 4",
    ".globl x4mp_combat_census_shot0",
    "x4mp_combat_census_shot0:",
    "lock inc qword ptr [rip + {shots}]",
    "jmp qword ptr [rip + {originals}]",
    ".p2align 4",
    ".globl x4mp_combat_census_shot1",
    "x4mp_combat_census_shot1:",
    "lock inc qword ptr [rip + {shots} + 8]",
    "jmp qword ptr [rip + {originals} + 8]",
    ".p2align 4",
    ".globl x4mp_combat_census_shot2",
    "x4mp_combat_census_shot2:",
    "lock inc qword ptr [rip + {shots} + 16]",
    "jmp qword ptr [rip + {originals} + 16]",
    ".p2align 4",
    ".globl x4mp_combat_census_shot3",
    "x4mp_combat_census_shot3:",
    "lock inc qword ptr [rip + {shots} + 24]",
    "jmp qword ptr [rip + {originals} + 24]",
    shots = sym SHOTS,
    originals = sym SHOT_ORIGINALS,
);

extern "C" {
    fn x4mp_combat_census_shot0();
    fn x4mp_combat_census_shot1();
    fn x4mp_combat_census_shot2();
    fn x4mp_combat_census_shot3();
}

/// Addresses of the four FireShot counting thunks.
pub fn shot_thunks() -> [usize; 4] {
    type Thunk = unsafe extern "C" fn();
    [
        x4mp_combat_census_shot0 as Thunk as usize,
        x4mp_combat_census_shot1 as Thunk as usize,
        x4mp_combat_census_shot2 as Thunk as usize,
        x4mp_combat_census_shot3 as Thunk as usize,
    ]
}

fn shots_now() -> [u64; 4] {
    [0, 1, 2, 3].map(|i| SHOTS[i].load(Ordering::Relaxed))
}

#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentThreadId() -> u32;
}

/// The executable TLS block (slot 0) of this thread, or null before it is initialised.
fn tls_block() -> *const u8 {
    unsafe {
        let slots: *const *const u8;
        asm!("mov {}, gs:[0x58]", out(reg) slots, options(nostack, preserves_flags, readonly));
        if slots.is_null() {
            return std::ptr::null();
        }
        let block = slots.read();
        if block.is_null() || block.add(0x10).read() == 0 {
            return std::ptr::null();
        }
        block
    }
}

fn rdtsc() -> u64 {
    unsafe { core::arch::x86_64::_rdtsc() }
}

#[derive(Clone, Copy)]
struct WeaponFields {
    aiming: bool,
    firing: bool,
    last_shot: u64,
    target: u64,
}

unsafe fn weapon_fields(weapon: *const u8) -> WeaponFields {
    let controller = (weapon.add(0x318) as *const *const u8).read();
    WeaponFields {
        aiming: weapon.add(0x354).read() != 0,
        firing: weapon.add(0x30d).read() != 0,
        last_shot: (weapon.add(0x310) as *const u64).read(),
        target: if controller.is_null() { 0 } else { (controller.add(0x10) as *const u64).read() },
    }
}

#[derive(Clone, Copy)]
struct BulletFields {
    last_check: u64,
    /// Creation (shot) time; the bullet setup 0xd50f30 also stores it as last and next check.
    created: u64,
    hits: i32,
}

unsafe fn bullet_fields(bullet: *const u8) -> BulletFields {
    BulletFields {
        last_check: (bullet.add(0x268) as *const u64).read(),
        created: (bullet.add(0x278) as *const u64).read(),
        hits: (bullet.add(0x280) as *const i32).read(),
    }
}

impl Census {
    fn new(frames: usize, classes: [usize; 4]) -> Census {
        Census {
            frames: Vec::with_capacity(frames),
            dropped_frames: 0,
            uninitialised_tls: 0,
            current: Frame::default(),
            open: false,
            prev_now: f64::NAN,
            rng_start: 0,
            tick_start: 0,
            shot_base: [0; 4],
            depth: 0,
            classes,
            failed: None,
        }
    }

    /// First combat call of a frame (outermost calls only): close the previous frame.
    fn enter(&mut self, now: f64, rng: u64, tick: u64) {
        if self.open && self.current.now.to_bits() == now.to_bits() {
            return;
        }
        if self.open {
            let shots = shots_now();
            let delta = |i: usize| shots[i].wrapping_sub(self.shot_base[i]) as u32;
            self.current.shots = delta(0).wrapping_add(delta(1));
            self.current.missile_shots = delta(2).wrapping_add(delta(3));
            self.current.rng_frame = steps_between(self.rng_start, rng, FRAME_CAP).unwrap_or(u32::MAX);
            self.current.frame_ticks = tick.wrapping_sub(self.tick_start);
            if self.frames.len() < self.frames.capacity() {
                self.frames.push(self.current);
            } else {
                self.dropped_frames += 1;
            }
            self.prev_now = self.current.now;
        }
        self.current = Frame { now, ..Frame::default() };
        self.open = true;
        self.rng_start = rng;
        self.tick_start = tick;
        self.shot_base = shots_now();
    }

    fn rng(&mut self, before: u64, after: u64) -> u32 {
        match steps_between(before, after, CALL_CAP) {
            Some(n) => {
                self.current.rng_max_call = self.current.rng_max_call.max(n);
                n
            }
            None => {
                self.current.rng_unmatched += 1;
                0
            }
        }
    }

    fn weapon(&mut self, vptr: usize, outer: bool, pre: WeaponFields, post: WeaponFields, rng: (u64, u64), ticks: u64) {
        let f = &mut self.current;
        f.weapon_calls += 1;
        if !outer {
            f.weapon_nested += 1;
        }
        if let Some(class) = self.classes.iter().position(|&c| c == vptr) {
            f.by_class[class] += 1;
        }
        f.with_target += (pre.target != 0) as u32;
        f.aiming += post.aiming as u32;
        f.firing += post.firing as u32;
        f.fired += (post.last_shot != pre.last_shot) as u32;
        if outer {
            let steps = self.rng(rng.0, rng.1);
            let f = &mut self.current;
            f.rng_weapon = f.rng_weapon.wrapping_add(steps);
            f.weapon_ticks = f.weapon_ticks.wrapping_add(ticks);
        }
    }

    fn bullet(&mut self, now: u64, outer: bool, pre: BulletFields, post: BulletFields, rng: (u64, u64), ticks: u64) {
        let prev_now = self.prev_now;
        let f = &mut self.current;
        f.bullet_calls += 1;
        let fresh = pre.last_check == pre.created;
        f.bullet_checks += (post.last_check == now && (pre.last_check != now || fresh)) as u32;
        if fresh {
            f.bullet_fresh += 1;
            // NaN before the second frame: never counted as same-frame
            f.fresh_same_frame += (f64::from_bits(pre.created) > prev_now) as u32;
        }
        if post.hits >= pre.hits {
            f.bullet_hits = f.bullet_hits.wrapping_add(post.hits.wrapping_sub(pre.hits) as u32);
        } else {
            f.hit_resets += 1;
            f.bullet_hits = f.bullet_hits.wrapping_add(post.hits.max(0) as u32);
        }
        if outer {
            let steps = self.rng(rng.0, rng.1);
            let f = &mut self.current;
            f.rng_bullet = f.rng_bullet.wrapping_add(steps);
            f.bullet_ticks = f.bullet_ticks.wrapping_add(ticks);
        }
    }
}

type MoveFn = unsafe extern "system" fn(*mut u8, usize, usize, usize) -> usize;

/// The census if this thread owns it and it has not failed.
fn owned() -> *mut Census {
    let census = CENSUS.load(Ordering::Acquire);
    if census.is_null() {
        return census;
    }
    if unsafe { GetCurrentThreadId() } != OWNER.load(Ordering::Relaxed) {
        FOREIGN.fetch_add(1, Ordering::Relaxed);
        return std::ptr::null_mut();
    }
    // SAFETY: owner thread.
    if unsafe { (*census).failed.is_some() } {
        return std::ptr::null_mut();
    }
    census
}

/// Run `body` on the census, turning a panic into a failed census.
unsafe fn guarded(census: *mut Census, body: impl FnOnce(&mut Census)) {
    // SAFETY: owner thread; no other reference to the census lives during `body`.
    if catch_unwind(AssertUnwindSafe(|| body(&mut *census))).is_err() {
        (*census).failed = Some("bookkeeping panicked");
    }
}

/// Pass-through wrapper for slot 674 of the four weapon classes.
///
/// # Safety
/// Only reachable through the hooked vtable entries with the original's arguments.
pub unsafe extern "system" fn weapon_move(this: *mut u8, a2: usize, a3: usize, a4: usize) -> usize {
    let original: MoveFn = std::mem::transmute(WEAPON_ORIGINAL.load(Ordering::Relaxed));
    let census = owned();
    if census.is_null() {
        return original(this, a2, a3, a4);
    }
    let block = tls_block();
    if block.is_null() {
        (*census).uninitialised_tls += 1;
        return original(this, a2, a3, a4);
    }
    let now = (block.add(0x300) as *const f64).read();
    let rng_before = (block.add(0x318) as *const u64).read();
    let outer = (*census).depth == 0;
    let tick = rdtsc();
    if outer {
        guarded(census, |c| c.enter(now, rng_before, tick));
    }
    let pre = weapon_fields(this);
    (*census).depth += 1;
    let result = original(this, a2, a3, a4);
    (*census).depth -= 1;
    let ticks = rdtsc().wrapping_sub(tick);
    let rng_after = (block.add(0x318) as *const u64).read();
    let post = weapon_fields(this);
    let vptr = (this as *const usize).read();
    guarded(census, |c| c.weapon(vptr, outer, pre, post, (rng_before, rng_after), ticks));
    result
}

/// Pass-through wrapper for slot 674 of `U::Bullet`.
///
/// # Safety
/// Only reachable through the hooked vtable entry with the original's arguments.
pub unsafe extern "system" fn bullet_move(this: *mut u8, a2: usize, a3: usize, a4: usize) -> usize {
    let original: MoveFn = std::mem::transmute(BULLET_ORIGINAL.load(Ordering::Relaxed));
    let census = owned();
    if census.is_null() {
        return original(this, a2, a3, a4);
    }
    let block = tls_block();
    if block.is_null() {
        (*census).uninitialised_tls += 1;
        return original(this, a2, a3, a4);
    }
    let now = (block.add(0x300) as *const f64).read();
    let rng_before = (block.add(0x318) as *const u64).read();
    let outer = (*census).depth == 0;
    let tick = rdtsc();
    if outer {
        guarded(census, |c| c.enter(now, rng_before, tick));
    }
    let pre = bullet_fields(this);
    (*census).depth += 1;
    let result = original(this, a2, a3, a4);
    (*census).depth -= 1;
    let ticks = rdtsc().wrapping_sub(tick);
    let rng_after = (block.add(0x318) as *const u64).read();
    // Bullets are removed through a posted event, never inside their own Move, so the
    // object is still alive here.
    let post = bullet_fields(this);
    guarded(census, |c| c.bullet(now.to_bits(), outer, pre, post, (rng_before, rng_after), ticks));
    result
}

/// What a hooked entry is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    WeaponMove,
    BulletMove,
    /// FireShot entry with its counter index (0..4).
    Shot(usize),
}

/// One entry to hook.
#[derive(Debug, Clone, Copy)]
pub struct Site {
    pub entry: *mut usize,
    pub original: usize,
    pub kind: Kind,
}

/// The pinned sites and class vtables in an image based at `base`.
///
/// # Safety
/// `base` is the base of a mapped pinned image (the process executable or an oracle copy).
pub unsafe fn pinned_sites(base: *mut u8) -> (Vec<Site>, [usize; 4]) {
    let at = |rva: u32| unsafe { base.add(rva as usize) };
    let [weapon, turret, launcher, missile_turret, bullet] = rva::VTABLES;
    let mut sites = Vec::new();
    for vt in [weapon, turret, launcher, missile_turret] {
        sites.push(Site {
            entry: at(vt + rva::SLOT_MOVE) as *mut usize,
            original: at(rva::WEAPON_MOVE) as usize,
            kind: Kind::WeaponMove,
        });
    }
    sites.push(Site {
        entry: at(bullet + rva::SLOT_MOVE) as *mut usize,
        original: at(rva::BULLET_MOVE) as usize,
        kind: Kind::BulletMove,
    });
    let shots = [rva::FIRE_SHOT, rva::FIRE_SHOT, rva::MISSILE_LAUNCHER_FIRE_SHOT, rva::MISSILE_TURRET_FIRE_SHOT];
    for (i, (vt, original)) in [weapon, turret, launcher, missile_turret].into_iter().zip(shots).enumerate() {
        sites.push(Site {
            entry: at(vt + rva::SLOT_FIRE_SHOT) as *mut usize,
            original: at(original) as usize,
            kind: Kind::Shot(i),
        });
    }
    let classes = [weapon, turret, launcher, missile_turret].map(|vt| at(vt) as usize);
    (sites, classes)
}

#[link(name = "kernel32")]
extern "system" {
    fn QueryPerformanceCounter(count: *mut i64) -> i32;
    fn QueryPerformanceFrequency(frequency: *mut i64) -> i32;
    fn GetModuleHandleExW(flags: u32, name: *const u16, module: *mut *mut c_void) -> i32;
}

fn qpc() -> (u64, i64) {
    let mut count = 0i64;
    unsafe { QueryPerformanceCounter(&mut count) };
    (rdtsc(), count)
}

/// Summary statistics of one per-frame quantity.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Dist {
    pub mean: f64,
    pub p50: f64,
    pub p95: f64,
    pub max: f64,
}

pub fn dist(values: impl Iterator<Item = f64>) -> Dist {
    let mut v: Vec<f64> = values.collect();
    if v.is_empty() {
        return Dist::default();
    }
    v.sort_by(|a, b| a.total_cmp(b));
    let at = |q: f64| v[((v.len() - 1) as f64 * q).round() as usize];
    Dist { mean: v.iter().sum::<f64>() / v.len() as f64, p50: at(0.5), p95: at(0.95), max: v[v.len() - 1] }
}

/// A running census: the hooks plus the owner-thread state.
pub struct CensusSession {
    hooks: Vec<EntryHook>,
    census: *mut Census,
    started: (u64, i64),
}

pub struct CensusStats {
    pub frames: usize,
    pub dropped_frames: u64,
    pub weapon_calls: u64,
    pub bullet_calls: u64,
    pub shots: u64,
    pub missile_shots: u64,
    pub bullet_checks: u64,
    pub bullet_hits: u64,
    pub bullet_fresh: u64,
    pub fresh_same_frame: u64,
    pub rng_weapon: u64,
    pub rng_bullet: u64,
    pub rng_frame: u64,
    pub rng_frame_over_cap: u64,
    pub rng_unmatched: u64,
    pub foreign: u64,
    pub uninitialised_tls: u64,
    pub failed: Option<&'static str>,
    /// rdtsc ticks per second (calibrated against QueryPerformanceCounter since start).
    pub ticks_per_second: f64,
}

impl CensusSession {
    /// Verify every entry, publish the census and swap the entries (all or none).
    ///
    /// # Safety
    /// `sites` are vtable entries of this process whose originals have the ABI of the
    /// wrappers (`Move(this)`, any `FireShot`); the caller is the simulation thread.
    pub unsafe fn start(sites: &[Site], classes: [usize; 4], frames: usize) -> Result<CensusSession, &'static str> {
        if !CENSUS.load(Ordering::Acquire).is_null() {
            return Err("a census is already running");
        }
        let weapon: Vec<usize> = sites.iter().filter(|s| s.kind == Kind::WeaponMove).map(|s| s.original).collect();
        let bullet: Vec<usize> = sites.iter().filter(|s| s.kind == Kind::BulletMove).map(|s| s.original).collect();
        if weapon.is_empty() || weapon.iter().any(|&w| w != weapon[0]) || bullet.len() != 1 {
            return Err("census sites are inconsistent");
        }
        // Keep this module loaded for good: wrappers and thunks live here.
        let mut module = std::ptr::null_mut();
        let anchor = weapon_move as MoveFn as *const u16;
        if GetModuleHandleExW(0x4 | 0x1, anchor, &mut module) == 0 {
            return Err("cannot pin the module");
        }
        let thunks = shot_thunks();
        let mut hooks = Vec::with_capacity(sites.len());
        for site in sites {
            let wrapper = match site.kind {
                Kind::WeaponMove => weapon_move as MoveFn as usize,
                Kind::BulletMove => bullet_move as MoveFn as usize,
                Kind::Shot(i) if i < 4 => thunks[i],
                Kind::Shot(_) => return Err("shot counter index out of range"),
            };
            hooks.push(EntryHook::prepare(site.entry, site.original, wrapper)?);
        }
        for site in sites {
            if let Kind::Shot(i) = site.kind {
                let previous = SHOT_ORIGINALS[i].load(Ordering::Acquire);
                if previous != 0 && previous != site.original {
                    return Err("shot thunk already bound to another original");
                }
                SHOT_ORIGINALS[i].store(site.original, Ordering::Release);
            }
        }
        let previous = WEAPON_ORIGINAL.load(Ordering::Acquire);
        if (previous != 0 && previous != weapon[0])
            || (BULLET_ORIGINAL.load(Ordering::Acquire) != 0 && BULLET_ORIGINAL.load(Ordering::Acquire) != bullet[0])
        {
            return Err("wrappers already bound to other originals");
        }
        WEAPON_ORIGINAL.store(weapon[0], Ordering::Release);
        BULLET_ORIGINAL.store(bullet[0], Ordering::Release);
        let census = Box::into_raw(Box::new(Census::new(frames, classes)));
        OWNER.store(GetCurrentThreadId(), Ordering::Release);
        FOREIGN.store(0, Ordering::Relaxed);
        CENSUS.store(census, Ordering::Release);
        for index in 0..hooks.len() {
            if let Err(reason) = hooks[index].install() {
                for hook in hooks[..index].iter_mut().rev() {
                    let _ = hook.remove();
                }
                if hooks.iter().all(|h| !h.installed()) {
                    CENSUS.store(std::ptr::null_mut(), Ordering::Release);
                    drop(Box::from_raw(census));
                } else {
                    // An entry could not be restored: its wrapper stays reachable and passes through.
                    (*census).failed = Some("install rollback failed");
                }
                return Err(reason);
            }
        }
        Ok(CensusSession { hooks, census, started: qpc() })
    }

    fn census(&self) -> &Census {
        // SAFETY: owner thread; alive until stop.
        unsafe { &*self.census }
    }

    /// Recorded frames so far (owner thread).
    pub fn frames(&self) -> &[Frame] {
        &self.census().frames
    }

    pub fn stats(&self) -> CensusStats {
        let c = self.census();
        let sum = |f: fn(&Frame) -> u32| c.frames.iter().map(|x| f(x) as u64).sum::<u64>();
        let (tick, count) = qpc();
        let mut frequency = 0i64;
        unsafe { QueryPerformanceFrequency(&mut frequency) };
        let seconds = (count - self.started.1) as f64 / frequency.max(1) as f64;
        let ticks_per_second = if seconds > 0.0 { tick.wrapping_sub(self.started.0) as f64 / seconds } else { 0.0 };
        CensusStats {
            frames: c.frames.len(),
            dropped_frames: c.dropped_frames,
            weapon_calls: sum(|f| f.weapon_calls),
            bullet_calls: sum(|f| f.bullet_calls),
            shots: sum(|f| f.shots),
            missile_shots: sum(|f| f.missile_shots),
            bullet_checks: sum(|f| f.bullet_checks),
            bullet_hits: sum(|f| f.bullet_hits),
            bullet_fresh: sum(|f| f.bullet_fresh),
            fresh_same_frame: sum(|f| f.fresh_same_frame),
            rng_weapon: sum(|f| f.rng_weapon),
            rng_bullet: sum(|f| f.rng_bullet),
            rng_frame: c.frames.iter().filter(|f| f.rng_frame != u32::MAX).map(|f| f.rng_frame as u64).sum(),
            rng_frame_over_cap: c.frames.iter().filter(|f| f.rng_frame == u32::MAX).count() as u64,
            rng_unmatched: sum(|f| f.rng_unmatched),
            foreign: FOREIGN.load(Ordering::Relaxed),
            uninitialised_tls: c.uninitialised_tls,
            failed: c.failed.or(if self.hooks.iter().all(|h| h.intact()) {
                None
            } else {
                Some("vtable entry changed")
            }),
            ticks_per_second,
        }
    }

    /// Per-frame distributions as `name mean p50 p95 max` lines (times in ms).
    pub fn summary(&self) -> Vec<String> {
        let c = self.census();
        let tps = self.stats().ticks_per_second.max(1.0);
        let ms = move |t: u64| t as f64 * 1000.0 / tps;
        type Row = (&'static str, Box<dyn Fn(&Frame) -> f64>);
        let rows: [Row; 17] = [
            ("weapon_calls", Box::new(|f| f.weapon_calls as f64)),
            ("with_target", Box::new(|f| f.with_target as f64)),
            ("aiming", Box::new(|f| f.aiming as f64)),
            ("fired", Box::new(|f| f.fired as f64)),
            ("shots", Box::new(|f| f.shots as f64)),
            ("missile_shots", Box::new(|f| f.missile_shots as f64)),
            ("bullet_calls", Box::new(|f| f.bullet_calls as f64)),
            ("bullet_checks", Box::new(|f| f.bullet_checks as f64)),
            ("bullet_hits", Box::new(|f| f.bullet_hits as f64)),
            ("bullet_fresh", Box::new(|f| f.bullet_fresh as f64)),
            ("fresh_same_frame", Box::new(|f| f.fresh_same_frame as f64)),
            ("rng_weapon", Box::new(|f| f.rng_weapon as f64)),
            ("rng_bullet", Box::new(|f| f.rng_bullet as f64)),
            ("rng_frame", Box::new(|f| if f.rng_frame == u32::MAX { f64::NAN } else { f.rng_frame as f64 })),
            ("weapon_ms", Box::new(move |f| ms(f.weapon_ticks))),
            ("bullet_ms", Box::new(move |f| ms(f.bullet_ticks))),
            ("frame_ms", Box::new(move |f| ms(f.frame_ticks))),
        ];
        rows.iter()
            .map(|(name, get)| {
                let d = dist(c.frames.iter().map(get).filter(|v| !v.is_nan()));
                format!("{name} mean={:.3} p50={:.3} p95={:.3} max={:.3}", d.mean, d.p50, d.p95, d.max)
            })
            .collect()
    }

    /// Write the per-frame table (`X4MP-COMBATCENSUS 1`) and the summary.
    pub fn write(&self, path: &std::path::Path) -> std::io::Result<usize> {
        use std::io::Write;
        let s = self.stats();
        let mut out = std::io::BufWriter::new(std::fs::File::create(path)?);
        writeln!(out, "X4MP-COMBATCENSUS 1 steam_build=23660954 source=native")?;
        writeln!(
            out,
            "# frames={} dropped_frames={} foreign={} uninitialised_tls={} rng_unmatched={} rng_frame_over_cap={} ticks_per_second={:.0} primary_target_offset=16",
            s.frames, s.dropped_frames, s.foreign, s.uninitialised_tls, s.rng_unmatched, s.rng_frame_over_cap, s.ticks_per_second
        )?;
        for line in self.summary() {
            writeln!(out, "# {line}")?;
        }
        writeln!(out, "{}", Frame::COLUMNS)?;
        for frame in self.frames() {
            writeln!(out, "{}", frame.line())?;
        }
        out.flush()?;
        Ok(s.frames)
    }

    /// Restore every entry. The census is freed only when all entries were restored;
    /// otherwise it stays (failed) for the wrappers that are still reachable.
    pub fn stop(mut self) -> Result<(), &'static str> {
        let mut result = Ok(());
        for hook in self.hooks.iter_mut().rev() {
            if let Err(reason) = unsafe { hook.remove() } {
                result = Err(reason);
            }
        }
        if result.is_ok() {
            CENSUS.store(std::ptr::null_mut(), Ordering::Release);
            drop(unsafe { Box::from_raw(self.census) });
        } else {
            unsafe { (*self.census).failed = Some("stopped with an entry left hooked") };
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rng_steps_are_counted_and_bounded() {
        let a = 0x0123_4567_89ab_cdef;
        let mut s = a;
        for _ in 0..37 {
            s = rng_step(s);
        }
        assert_eq!(steps_between(a, a, 10), Some(0));
        assert_eq!(steps_between(a, s, 100), Some(37));
        assert_eq!(steps_between(a, s, 36), None);
        assert_eq!(rng_step(0), 1_442_695_040_888_963_407u64.rotate_right(30));
    }

    #[test]
    fn distributions() {
        let d = dist([3.0, 1.0, 2.0, 10.0].into_iter());
        assert_eq!((d.mean, d.p50, d.max), (4.0, 3.0, 10.0));
        assert_eq!(dist(std::iter::empty()), Dist::default());
    }

    // A synthetic engine: fake objects whose fake Move/FireShot originals sit behind fake
    // vtable entries on a read-only page, the engine TLS block swapped in for the calls.
    // Object bytes 0x3f0.. drive the fakes: draws, hits/aiming, shots, child object.
    const DRAWS: usize = 0x3f0;
    const HITS: usize = 0x3f1;
    const SHOTS_TO_FIRE: usize = 0x3f2;
    const AIMING_AFTER: usize = 0x3f3;
    const BROKEN: usize = 0x3f4;
    const CHILD: usize = 0x3f8;

    static WEAPON_ENTRY: AtomicUsize = AtomicUsize::new(0);
    static SHOT_ENTRY: AtomicUsize = AtomicUsize::new(0);
    static SHOT_SUM: AtomicU64 = AtomicU64::new(0);

    #[repr(C, align(16))]
    struct Obj([u8; 0x400]);

    impl Obj {
        fn new(vptr: usize) -> Box<Obj> {
            let mut o = Box::new(Obj([0; 0x400]));
            o.0[..8].copy_from_slice(&vptr.to_le_bytes());
            o
        }
        fn ptr(&mut self) -> *mut u8 {
            self.0.as_mut_ptr()
        }
        fn put(&mut self, at: usize, bytes: &[u8]) {
            self.0[at..at + bytes.len()].copy_from_slice(bytes);
        }
    }

    unsafe fn draw(block: *mut u8, n: u8) {
        let state = block.add(0x318) as *mut u64;
        for _ in 0..n {
            state.write(rng_step(state.read()));
        }
    }

    type Fire = unsafe extern "C" fn(usize, usize, usize, usize, f64, u64) -> f64;

    unsafe extern "C" fn fake_fire_shot(a: usize, b: usize, c: usize, d: usize, e: f64, f: u64) -> f64 {
        SHOT_SUM.fetch_add((a + b + c + d) as u64 + f, Ordering::Relaxed);
        e * 2.0
    }

    unsafe extern "system" fn fake_weapon_move(this: *mut u8, _: usize, _: usize, _: usize) -> usize {
        let draws = this.add(DRAWS).read();
        let shots = this.add(SHOTS_TO_FIRE).read();
        if draws > 0 || shots > 0 {
            let block = tls_block() as *mut u8;
            draw(block, draws);
            if shots > 0 {
                (this.add(0x310) as *mut f64).write((block.add(0x300) as *const f64).read());
                let fire: Fire = std::mem::transmute((SHOT_ENTRY.load(Ordering::Relaxed) as *const usize).read());
                for i in 0..shots {
                    if fire(1, 2, 3, 4, 0.25 * i as f64, 10) != 0.5 * i as f64 {
                        this.add(BROKEN).write(0xee);
                    }
                }
            }
        }
        this.add(0x354).write(this.add(AIMING_AFTER).read());
        let child = (this.add(CHILD) as *const *mut u8).read();
        if !child.is_null() {
            let nested: MoveFn = std::mem::transmute((WEAPON_ENTRY.load(Ordering::Relaxed) as *const usize).read());
            nested(child, 0, 0, 0);
        }
        0x77
    }

    unsafe extern "system" fn fake_bullet_move(this: *mut u8, _: usize, _: usize, _: usize) -> usize {
        let block = tls_block() as *mut u8;
        draw(block, this.add(DRAWS).read());
        let now = (block.add(0x300) as *const f64).read();
        if now >= (this.add(0x270) as *const f64).read() {
            (this.add(0x268) as *mut f64).write(now);
            (this.add(0x270) as *mut f64).write(now + 0.1);
            let hits = this.add(0x280) as *mut i32;
            hits.write(hits.read() + this.add(HITS).read() as i32);
        }
        0x55
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn VirtualAlloc(address: *mut c_void, size: usize, allocation: u32, protect: u32) -> *mut c_void;
        fn VirtualProtect(address: *mut c_void, size: usize, protect: u32, old: *mut u32) -> i32;
    }

    #[test]
    fn counts_frames_shots_rng_and_restores_the_entries() {
        let page = unsafe { VirtualAlloc(std::ptr::null_mut(), 4096, 0x3000, 0x04) } as *mut usize;
        assert!(!page.is_null());
        let weapon_original = fake_weapon_move as MoveFn as usize;
        let bullet_original = fake_bullet_move as MoveFn as usize;
        let shot_original = fake_fire_shot as Fire as usize;
        let mut sites = Vec::new();
        for i in 0..9 {
            let (original, kind) = match i {
                0..=3 => (weapon_original, Kind::WeaponMove),
                4 => (bullet_original, Kind::BulletMove),
                n => (shot_original, Kind::Shot(n - 5)),
            };
            let entry = unsafe { page.add(i * 2) };
            unsafe { entry.write(original) };
            sites.push(Site { entry, original, kind });
        }
        let mut old = 0;
        assert_ne!(unsafe { VirtualProtect(page as *mut c_void, 4096, 0x02, &mut old) }, 0);
        WEAPON_ENTRY.store(sites[1].entry as usize, Ordering::Relaxed); // the Turret entry
        SHOT_ENTRY.store(sites[6].entry as usize, Ordering::Relaxed); // Turret FireShot
        let classes = [0x1000, 0x2000, 0x3000, 0x4000];
        let mut bad = sites.clone();
        bad[2].original = bullet_original;
        assert_eq!(unsafe { CensusSession::start(&bad, classes, 10) }.err(), Some("census sites are inconsistent"));
        let mut wrong = sites.clone();
        wrong[7].original += 1;
        assert!(unsafe { CensusSession::start(&wrong, classes, 10) }.is_err());
        assert!(sites.iter().all(|s| unsafe { s.entry.read() } == s.original), "refused start leaves entries");

        let census = unsafe { CensusSession::start(&sites, classes, 10) }.unwrap();
        assert!(sites.iter().all(|s| unsafe { s.entry.read() } != s.original));
        assert!(unsafe { CensusSession::start(&sites, classes, 10) }.is_err(), "one census at a time");

        // a turret aiming at a target and firing 3 shots (2 draws) with a nested child weapon
        // (1 draw); an idle weapon; two bullets (one draws 1 and hits twice, one is not due)
        let mut controller = Obj::new(0);
        controller.put(0x10, &77u64.to_le_bytes());
        let mut child = Obj::new(0x1000);
        child.put(DRAWS, &[1]);
        let mut turret = Obj::new(0x2000);
        turret.put(DRAWS, &[2]);
        turret.put(SHOTS_TO_FIRE, &[3]);
        turret.put(AIMING_AFTER, &[1]);
        turret.put(0x318, &(controller.ptr() as usize).to_le_bytes());
        turret.put(CHILD, &(child.ptr() as usize).to_le_bytes());
        let mut idle = Obj::new(0x1000);
        let mut bullet_a = Obj::new(0x9000);
        bullet_a.put(DRAWS, &[1]);
        bullet_a.put(HITS, &[2]);
        let mut bullet_b = Obj::new(0x9000);
        bullet_b.put(0x270, &1e9f64.to_le_bytes());
        // fired after frame 0 (creation time 10.02 stored as last and next check too)
        let mut bullet_c = Obj::new(0x9000);
        for at in [0x268, 0x270, 0x278] {
            bullet_c.put(at, &10.02f64.to_le_bytes());
        }

        let mut tls = vec![0u8; 0x1000 + 64];
        let block = ((tls.as_mut_ptr() as usize + 63) & !63) as *mut u8;
        let slots: [*mut u8; 1] = [block];
        unsafe {
            block.add(0x10).write(1);
            (block.add(0x318) as *mut u64).write(0x5eed);
        }
        let entry = |i: usize| -> MoveFn { unsafe { std::mem::transmute(sites[i].entry.read()) } };
        let (weapon_call, turret_call, bullet_call) = (entry(0), entry(1), entry(4));
        let objects = [turret.ptr(), idle.ptr(), bullet_a.ptr(), bullet_b.ptr(), bullet_c.ptr()];
        let mut results = [0usize; 8];
        unsafe {
            crate::oracle::with_tls_slots(slots.as_ptr(), || {
                for (frame, now) in [10.0f64, 10.05, 10.1].into_iter().enumerate() {
                    (block.add(0x300) as *mut f64).write(now);
                    draw(block, 5); // other callers on the thread between frames
                    if frame == 2 {
                        results[6] = weapon_call(objects[1], 0, 0, 0);
                        break;
                    }
                    results[frame * 3] = turret_call(objects[0], 0, 0, 0);
                    results[frame * 3 + 1] = weapon_call(objects[1], 0, 0, 0);
                    results[frame * 3 + 2] = bullet_call(objects[2], 0, 0, 0);
                    bullet_call(objects[3], 0, 0, 0);
                    if frame == 1 {
                        bullet_call(objects[4], 0, 0, 0);
                    }
                }
            });
        }
        assert_eq!(results[..7], [0x77, 0x77, 0x55, 0x77, 0x77, 0x55, 0x77]);
        assert_eq!(turret.0[BROKEN], 0, "FireShot thunk passed the arguments and the result through");
        assert_eq!(SHOT_SUM.load(Ordering::Relaxed), 6 * 20);

        let frames = census.frames().to_vec();
        assert_eq!(frames.len(), 2, "the third frame is still open");
        let f = frames[0];
        assert_eq!(f.now, 10.0);
        assert_eq!((f.weapon_calls, f.weapon_nested, f.by_class), (3, 1, [2, 1, 0, 0]));
        assert_eq!((f.with_target, f.aiming, f.fired, f.shots, f.missile_shots), (1, 1, 1, 3, 0));
        assert_eq!((f.bullet_calls, f.bullet_checks, f.bullet_hits, f.hit_resets), (2, 1, 2, 0));
        // both bullets were never checked before (fresh), but frame 0 has no previous frame
        assert_eq!((f.bullet_fresh, f.fresh_same_frame), (2, 0));
        assert_eq!((f.rng_weapon, f.rng_bullet, f.rng_max_call, f.rng_unmatched), (3, 1, 3, 0));
        assert_eq!(f.rng_frame, 4 + 5, "combat draws plus the 5 between frames");
        let g = frames[1];
        assert_eq!((g.weapon_calls, g.fired, g.shots, g.bullet_checks, g.bullet_hits), (3, 1, 3, 1, 0));
        assert_eq!((g.bullet_calls, g.bullet_fresh, g.fresh_same_frame), (3, 2, 1), "bullet_c is new this frame");
        assert_eq!(g.rng_bullet, 1);
        let s = census.stats();
        assert_eq!((s.frames, s.shots, s.bullet_hits, s.rng_weapon, s.rng_unmatched), (2, 6, 2, 6, 0));
        assert_eq!(s.failed, None);
        assert_eq!(census.summary().len(), 17);
        assert!(census.summary()[4].starts_with("shots mean=3.000 p50=3.000"), "{:?}", census.summary());

        // another thread passes straight through and is counted
        let idle_ptr = idle.ptr() as usize;
        let foreign = FOREIGN.load(Ordering::Relaxed);
        std::thread::spawn(move || unsafe { weapon_call(idle_ptr as *mut u8, 0, 0, 0) }).join().unwrap();
        assert_eq!(FOREIGN.load(Ordering::Relaxed), foreign + 1);

        let dir = std::env::temp_dir().join(format!("x4mp-census-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("x4mp-combatcensus.txt");
        assert_eq!(census.write(&path).unwrap(), 2);
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.starts_with("X4MP-COMBATCENSUS 1 "));
        assert!(text.contains(Frame::COLUMNS));
        assert_eq!(text.lines().filter(|l| l.starts_with("10.")).count(), 2);
        std::fs::remove_dir_all(&dir).unwrap();

        census.stop().unwrap();
        assert!(sites.iter().all(|s| unsafe { s.entry.read() } == s.original), "entries restored");
    }
}
