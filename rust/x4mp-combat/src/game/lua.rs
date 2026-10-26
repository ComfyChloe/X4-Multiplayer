//! Lua 5.1 module `luaopen_x4mp_combat` for the lane recorder (dev/combat_scenario/combat_capture.lua).
//!
//! ```lua
//! local m = package.loadlib(dll, 'luaopen_x4mp_combat')()
//! local h, why = m.start(capacity, record, shadow[, switch[, verify_every]])  -- main Lua thread
//! h:stats()       -- table: calls, recorded, dropped, foreign, shadowed, mismatched, served, verified,
//!                 --   switch_mismatched, switch_off, mxcsr, mode, failed, first_mismatch
//! h:write(path)   -- records written, or nil, reason (text capture X4CJSTEP-HEX)
//! h:stop()        -- true, or nil, reason (the patch stays and the session leaks when the site changed)
//!
//! local c, why = m.census(frames)  -- combat census (data-only vtable hooks), main Lua thread
//! c:stats()       -- table: frames, dropped_frames, weapon_calls, bullet_calls, shots, missile_shots,
//!                 --   bullet_checks, bullet_hits, rng_weapon, rng_bullet, rng_frame, rng_frame_over_cap,
//!                 --   rng_unmatched, foreign, uninitialised_tls, ticks_per_second, failed
//! c:summary()     -- array of lines "name mean= p50= p95= max=" (per frame)
//! c:write(path)   -- frames written (text table X4MP-COMBATCENSUS 1), or nil, reason
//! c:stop()        -- true, or nil, reason (entries another hook replaced stay hooked)
//! ```
//! Like `x4lan-net`, no Lua library is linked: the C API is resolved from the already loaded
//! `lua51_64.dll`. Every entry point is `C-unwind` and catches Rust panics.
#![allow(non_camel_case_types)]

use std::ffi::{c_char, c_int, c_void, CStr};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::OnceLock;

use super::census::{pinned_sites, CensusSession};
use super::session::{verify_process, Image, Options, Session};

#[repr(C)]
pub struct lua_State {
    _opaque: [u8; 0],
}

type lua_CFunction = unsafe extern "C-unwind" fn(*mut lua_State) -> c_int;

const METATABLE: &CStr = c"x4mp.combat.session.v1";
const CENSUS_METATABLE: &CStr = c"x4mp.combat.census.v1";
const MAX_FRAMES: f64 = 1_000_000.0;
const REGISTRYINDEX: c_int = -10000;
const LUA_TNONE: c_int = -1;
const LUA_TNIL: c_int = 0;
const LUA_TNUMBER: c_int = 3;
const LUA_TSTRING: c_int = 4;
const MAX_CAPACITY: f64 = 2_000_000.0;

struct Api {
    gettop: unsafe extern "C-unwind" fn(*mut lua_State) -> c_int,
    settop: unsafe extern "C-unwind" fn(*mut lua_State, c_int),
    type_: unsafe extern "C-unwind" fn(*mut lua_State, c_int) -> c_int,
    toboolean: unsafe extern "C-unwind" fn(*mut lua_State, c_int) -> c_int,
    tonumber: unsafe extern "C-unwind" fn(*mut lua_State, c_int) -> f64,
    tolstring: unsafe extern "C-unwind" fn(*mut lua_State, c_int, *mut usize) -> *const c_char,
    pushthread: unsafe extern "C-unwind" fn(*mut lua_State) -> c_int,
    pushnil: unsafe extern "C-unwind" fn(*mut lua_State),
    pushboolean: unsafe extern "C-unwind" fn(*mut lua_State, c_int),
    pushnumber: unsafe extern "C-unwind" fn(*mut lua_State, f64),
    pushlstring: unsafe extern "C-unwind" fn(*mut lua_State, *const c_char, usize),
    pushcclosure: unsafe extern "C-unwind" fn(*mut lua_State, lua_CFunction, c_int),
    pushvalue: unsafe extern "C-unwind" fn(*mut lua_State, c_int),
    rawseti: unsafe extern "C-unwind" fn(*mut lua_State, c_int, c_int),
    createtable: unsafe extern "C-unwind" fn(*mut lua_State, c_int, c_int),
    setfield: unsafe extern "C-unwind" fn(*mut lua_State, c_int, *const c_char),
    getfield: unsafe extern "C-unwind" fn(*mut lua_State, c_int, *const c_char),
    newuserdata: unsafe extern "C-unwind" fn(*mut lua_State, usize) -> *mut c_void,
    setmetatable: unsafe extern "C-unwind" fn(*mut lua_State, c_int) -> c_int,
    newmetatable: unsafe extern "C-unwind" fn(*mut lua_State, *const c_char) -> c_int,
    checkudata: unsafe extern "C-unwind" fn(*mut lua_State, c_int, *const c_char) -> *mut c_void,
}

static API: OnceLock<Api> = OnceLock::new();

#[link(name = "kernel32")]
extern "system" {
    fn GetModuleHandleA(name: *const c_char) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const c_char) -> *mut c_void;
}

#[allow(clippy::missing_transmute_annotations)]
fn resolve() -> Option<&'static Api> {
    if let Some(api) = API.get() {
        return Some(api);
    }
    // SAFETY: NUL-terminated name; no reference count change.
    let runtime = unsafe { GetModuleHandleA(c"lua51_64.dll".as_ptr()) };
    if runtime.is_null() {
        return None;
    }
    macro_rules! get {
        ($name:literal) => {{
            let address = unsafe { GetProcAddress(runtime, $name.as_ptr()) };
            if address.is_null() {
                return None;
            }
            // SAFETY: the named Lua 5.1 C API function with the field's lua.h signature.
            unsafe { std::mem::transmute::<*mut c_void, _>(address) }
        }};
    }
    let api = Api {
        gettop: get!(c"lua_gettop"),
        settop: get!(c"lua_settop"),
        type_: get!(c"lua_type"),
        toboolean: get!(c"lua_toboolean"),
        tonumber: get!(c"lua_tonumber"),
        tolstring: get!(c"lua_tolstring"),
        pushthread: get!(c"lua_pushthread"),
        pushnil: get!(c"lua_pushnil"),
        pushboolean: get!(c"lua_pushboolean"),
        pushnumber: get!(c"lua_pushnumber"),
        pushlstring: get!(c"lua_pushlstring"),
        pushcclosure: get!(c"lua_pushcclosure"),
        pushvalue: get!(c"lua_pushvalue"),
        rawseti: get!(c"lua_rawseti"),
        createtable: get!(c"lua_createtable"),
        setfield: get!(c"lua_setfield"),
        getfield: get!(c"lua_getfield"),
        newuserdata: get!(c"lua_newuserdata"),
        setmetatable: get!(c"lua_setmetatable"),
        newmetatable: get!(c"luaL_newmetatable"),
        checkudata: get!(c"luaL_checkudata"),
    };
    Some(API.get_or_init(|| api))
}

fn api() -> &'static Api {
    API.get().expect("x4mp_combat: Lua API used before luaopen")
}

unsafe fn push_str(l: *mut lua_State, text: &str) {
    (api().pushlstring)(l, text.as_ptr() as *const c_char, text.len());
}

unsafe fn fail(l: *mut lua_State, reason: &str) -> c_int {
    (api().pushnil)(l);
    push_str(l, reason);
    2
}

unsafe fn number_field(l: *mut lua_State, name: &CStr, value: f64) {
    (api().pushnumber)(l, value);
    (api().setfield)(l, -2, name.as_ptr());
}

unsafe fn string_field(l: *mut lua_State, name: &CStr, value: &str) {
    push_str(l, value);
    (api().setfield)(l, -2, name.as_ptr());
}

/// The session slot inside the userdata (null once stopped).
unsafe fn slot(l: *mut lua_State) -> *mut *mut Session {
    (api().checkudata)(l, 1, METATABLE.as_ptr()) as *mut *mut Session
}

fn guarded(l: *mut lua_State, body: impl FnOnce() -> c_int) -> c_int {
    match catch_unwind(AssertUnwindSafe(body)) {
        Ok(n) => n,
        Err(_) => unsafe { fail(l, "x4mp_combat internal error") },
    }
}

unsafe extern "C-unwind" fn start(l: *mut lua_State) -> c_int {
    let a = api();
    // Argument checks may raise Lua errors only before any Rust value with a destructor lives.
    let capacity = if (a.type_)(l, 1) == LUA_TNUMBER { (a.tonumber)(l, 1) } else { 200_000.0 };
    let record = matches!((a.type_)(l, 2), LUA_TNONE | LUA_TNIL) || (a.toboolean)(l, 2) != 0;
    let shadow = matches!((a.type_)(l, 3), LUA_TNONE | LUA_TNIL) || (a.toboolean)(l, 3) != 0;
    let switch = (a.toboolean)(l, 4) != 0;
    let verify_every = if (a.type_)(l, 5) == LUA_TNUMBER { (a.tonumber)(l, 5) } else { 1.0 };
    let main = (a.pushthread)(l);
    (a.settop)(l, -2);
    guarded(l, || {
        if main != 1 {
            return fail(l, "main Lua thread required");
        }
        if !(0.0..=MAX_CAPACITY).contains(&capacity) {
            return fail(l, "invalid capacity");
        }
        if !(0.0..=1e9).contains(&verify_every) || verify_every.fract() != 0.0 {
            return fail(l, "invalid verify interval");
        }
        let options =
            Options { capacity: capacity as usize, record, shadow, switch, verify_every: verify_every as u32 };
        match Session::start(Image::Process, options) {
            Ok(session) => {
                let cell = (a.newuserdata)(l, std::mem::size_of::<*mut Session>()) as *mut *mut Session;
                cell.write(Box::into_raw(Box::new(session)));
                (a.getfield)(l, REGISTRYINDEX, METATABLE.as_ptr());
                (a.setmetatable)(l, -2);
                1
            }
            Err(reason) => fail(l, reason),
        }
    })
}

unsafe extern "C-unwind" fn stats(l: *mut lua_State) -> c_int {
    let cell = slot(l);
    guarded(l, || {
        let a = api();
        if (*cell).is_null() {
            return fail(l, "stopped");
        }
        let s = (**cell).stats();
        (a.createtable)(l, 0, 16);
        number_field(l, c"calls", s.calls as f64);
        number_field(l, c"recorded", s.recorded as f64);
        number_field(l, c"dropped", s.dropped as f64);
        number_field(l, c"foreign", s.foreign as f64);
        number_field(l, c"uninitialised_tls", s.uninitialised_tls as f64);
        number_field(l, c"shadowed", s.shadowed as f64);
        number_field(l, c"mismatched", s.mismatched as f64);
        number_field(l, c"skipped_mode", s.skipped_mode as f64);
        number_field(l, c"mxcsr", s.mxcsr as f64);
        number_field(l, c"mxcsr_changes", s.mxcsr_changes as f64);
        number_field(l, c"mode", s.mode as f64);
        number_field(l, c"served", s.served as f64);
        number_field(l, c"verified", s.verified as f64);
        number_field(l, c"switch_mismatched", s.switch_mismatched as f64);
        number_field(l, c"foreign_class", s.foreign_class as f64);
        if let Some(reason) = s.switch_off {
            string_field(l, c"switch_off", reason);
        }
        if let Some(reason) = s.failed {
            string_field(l, c"failed", reason);
        }
        if let Some(text) = &s.first_mismatch {
            string_field(l, c"first_mismatch", text);
        }
        1
    })
}

unsafe extern "C-unwind" fn write(l: *mut lua_State) -> c_int {
    let cell = slot(l);
    let a = api();
    let mut length = 0usize;
    let text = if (a.type_)(l, 2) == LUA_TSTRING { (a.tolstring)(l, 2, &mut length) } else { std::ptr::null() };
    guarded(l, || {
        if (*cell).is_null() {
            return fail(l, "stopped");
        }
        if text.is_null() {
            return fail(l, "path required");
        }
        let bytes = std::slice::from_raw_parts(text as *const u8, length);
        let Ok(path) = std::str::from_utf8(bytes) else { return fail(l, "path is not UTF-8") };
        match (**cell).write(std::path::Path::new(path)) {
            Ok(count) => {
                (a.pushnumber)(l, count as f64);
                1
            }
            Err(e) => fail(l, &format!("write failed: {e}")),
        }
    })
}

unsafe extern "C-unwind" fn stop(l: *mut lua_State) -> c_int {
    let cell = slot(l);
    guarded(l, || {
        let a = api();
        if (*cell).is_null() {
            (a.pushboolean)(l, 1);
            return 1;
        }
        let session = Box::from_raw(*cell);
        *cell = std::ptr::null_mut();
        match session.stop() {
            Ok(()) => {
                (a.pushboolean)(l, 1);
                1
            }
            Err(reason) => fail(l, reason),
        }
    })
}

/// The census slot inside the userdata (null once stopped).
unsafe fn census_slot(l: *mut lua_State) -> *mut *mut CensusSession {
    (api().checkudata)(l, 1, CENSUS_METATABLE.as_ptr()) as *mut *mut CensusSession
}

unsafe extern "C-unwind" fn census(l: *mut lua_State) -> c_int {
    let a = api();
    let frames = if (a.type_)(l, 1) == LUA_TNUMBER { (a.tonumber)(l, 1) } else { 20_000.0 };
    let main = (a.pushthread)(l);
    (a.settop)(l, -2);
    guarded(l, || {
        if main != 1 {
            return fail(l, "main Lua thread required");
        }
        if !(1.0..=MAX_FRAMES).contains(&frames) || frames.fract() != 0.0 {
            return fail(l, "invalid frame capacity");
        }
        let base = match verify_process() {
            Ok(base) => base,
            Err(reason) => return fail(l, reason),
        };
        let (sites, classes) = pinned_sites(base);
        // SAFETY: the image is the pinned build (verified above); the entries are its
        // vtable slots 674/998 whose originals have the wrappers' ABI.
        match CensusSession::start(&sites, classes, frames as usize) {
            Ok(session) => {
                let cell = (a.newuserdata)(l, std::mem::size_of::<*mut CensusSession>()) as *mut *mut CensusSession;
                cell.write(Box::into_raw(Box::new(session)));
                (a.getfield)(l, REGISTRYINDEX, CENSUS_METATABLE.as_ptr());
                (a.setmetatable)(l, -2);
                1
            }
            Err(reason) => fail(l, reason),
        }
    })
}

unsafe extern "C-unwind" fn census_stats(l: *mut lua_State) -> c_int {
    let cell = census_slot(l);
    guarded(l, || {
        let a = api();
        if (*cell).is_null() {
            return fail(l, "stopped");
        }
        let s = (**cell).stats();
        (a.createtable)(l, 0, 17);
        number_field(l, c"frames", s.frames as f64);
        number_field(l, c"dropped_frames", s.dropped_frames as f64);
        number_field(l, c"weapon_calls", s.weapon_calls as f64);
        number_field(l, c"bullet_calls", s.bullet_calls as f64);
        number_field(l, c"shots", s.shots as f64);
        number_field(l, c"missile_shots", s.missile_shots as f64);
        number_field(l, c"bullet_checks", s.bullet_checks as f64);
        number_field(l, c"bullet_hits", s.bullet_hits as f64);
        number_field(l, c"rng_weapon", s.rng_weapon as f64);
        number_field(l, c"rng_bullet", s.rng_bullet as f64);
        number_field(l, c"rng_frame", s.rng_frame as f64);
        number_field(l, c"rng_frame_over_cap", s.rng_frame_over_cap as f64);
        number_field(l, c"rng_unmatched", s.rng_unmatched as f64);
        number_field(l, c"foreign", s.foreign as f64);
        number_field(l, c"uninitialised_tls", s.uninitialised_tls as f64);
        number_field(l, c"ticks_per_second", s.ticks_per_second);
        if let Some(reason) = s.failed {
            string_field(l, c"failed", reason);
        }
        1
    })
}

unsafe extern "C-unwind" fn census_summary(l: *mut lua_State) -> c_int {
    let cell = census_slot(l);
    guarded(l, || {
        let a = api();
        if (*cell).is_null() {
            return fail(l, "stopped");
        }
        let lines = (**cell).summary();
        (a.createtable)(l, lines.len() as c_int, 0);
        for (i, line) in lines.iter().enumerate() {
            push_str(l, line);
            (a.rawseti)(l, -2, i as c_int + 1);
        }
        1
    })
}

unsafe extern "C-unwind" fn census_write(l: *mut lua_State) -> c_int {
    let cell = census_slot(l);
    let a = api();
    let mut length = 0usize;
    let text = if (a.type_)(l, 2) == LUA_TSTRING { (a.tolstring)(l, 2, &mut length) } else { std::ptr::null() };
    guarded(l, || {
        if (*cell).is_null() {
            return fail(l, "stopped");
        }
        if text.is_null() {
            return fail(l, "path required");
        }
        let bytes = std::slice::from_raw_parts(text as *const u8, length);
        let Ok(path) = std::str::from_utf8(bytes) else { return fail(l, "path is not UTF-8") };
        match (**cell).write(std::path::Path::new(path)) {
            Ok(count) => {
                (a.pushnumber)(l, count as f64);
                1
            }
            Err(e) => fail(l, &format!("write failed: {e}")),
        }
    })
}

unsafe extern "C-unwind" fn census_stop(l: *mut lua_State) -> c_int {
    let cell = census_slot(l);
    guarded(l, || {
        let a = api();
        if (*cell).is_null() {
            (a.pushboolean)(l, 1);
            return 1;
        }
        let session = Box::from_raw(*cell);
        *cell = std::ptr::null_mut();
        match session.stop() {
            Ok(()) => {
                (a.pushboolean)(l, 1);
                1
            }
            Err(reason) => fail(l, reason),
        }
    })
}

unsafe fn method(l: *mut lua_State, name: &CStr, function: lua_CFunction) {
    (api().pushcclosure)(l, function, 0);
    (api().setfield)(l, -2, name.as_ptr());
}

/// # Safety
/// Called by Lua's `package.loadlib` with a valid state.
#[no_mangle]
pub unsafe extern "C-unwind" fn luaopen_x4mp_combat(l: *mut lua_State) -> c_int {
    let Some(a) = resolve() else { return 0 };
    (a.newmetatable)(l, METATABLE.as_ptr());
    (a.pushvalue)(l, -1);
    (a.setfield)(l, -2, c"__index".as_ptr());
    method(l, c"__gc", stop);
    method(l, c"stop", stop);
    method(l, c"stats", stats);
    method(l, c"write", write);
    (a.settop)(l, -2);
    (a.newmetatable)(l, CENSUS_METATABLE.as_ptr());
    (a.pushvalue)(l, -1);
    (a.setfield)(l, -2, c"__index".as_ptr());
    method(l, c"__gc", census_stop);
    method(l, c"stop", census_stop);
    method(l, c"stats", census_stats);
    method(l, c"summary", census_summary);
    method(l, c"write", census_write);
    (a.settop)(l, -2);
    (a.createtable)(l, 0, 2);
    method(l, c"start", start);
    method(l, c"census", census);
    let _ = (a.gettop)(l);
    1
}
