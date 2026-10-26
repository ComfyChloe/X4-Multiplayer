#![cfg(target_os = "windows")]

use core::ffi::{c_char, c_double, c_int, c_void};
use std::ptr;
use std::ptr::{addr_of, addr_of_mut};
use std::slice;
use std::sync::atomic::{AtomicBool, AtomicPtr, AtomicU64, Ordering};

mod timing;

const REGISTRYINDEX: c_int = -10000;
const META: &[u8] = b"x4lan.hooks.owner.v1\0";
const VBI_RVA: usize = 0x6cf1570;
const ANARK_RVA: usize = 0x6cf18f8;
const UI_VTABLE_RVA: usize = 0x2bc07d0;
const REGISTRY_RVA: usize = 0x6cf1500;
const GAME_TIME_RVA: usize = 0x6b60e38;
const SESSION_SEED_RVA: usize = 0x3d19860;
const REGISTRY_FIND_RVA: usize = 0x0ced70;
const SHIELD_PERCENT_RVA: usize = 0x51f5b0;
const GET_TRANSFORM_RVA: usize = 0x3db8a0;
const DISTANCE_SQ_RVA: usize = 0x3e0c40;
const SET_ATTENTION_RVA: usize = 0x3d5660;
const ZONE_COMPUTE_RVA: usize = 0x843b20;
const SHIP_COMPUTE_RVA: usize = 0x7d5110;
const ZONE_ATTENTION_SLOTS: [usize; 2] = [0x2b0a7d8, 0x2b67678];
const SHIP_ATTENTION_SLOTS: [usize; 7] = [
    0x2b86090, 0x2b9b080, 0x2b7ed30, 0x2b9d5f0, 0x2b83a38, 0x2b814b0, 0x2b79fa8,
];
const IMAGE_SIZE: usize = 0x6fa9000;
const SLOT: usize = 12;
const MEM_COMMIT: u32 = 0x1000;
const PAGE_NOACCESS: u32 = 0x01;
const PAGE_GUARD: u32 = 0x100;
const PAGE_READONLY: u32 = 0x02;
const PAGE_READWRITE: u32 = 0x04;
const PAGE_WRITECOPY: u32 = 0x08;
const PAGE_EXECUTE_READ: u32 = 0x20;
const PAGE_EXECUTE_READWRITE: u32 = 0x40;
const PAGE_EXECUTE_WRITECOPY: u32 = 0x80;

#[repr(C)]
struct MemoryBasicInformation {
    base_address: *mut c_void,
    allocation_base: *mut c_void,
    allocation_protect: u32,
    partition_id: u16,
    pad: u16,
    region_size: usize,
    state: u32,
    protect: u32,
    kind: u32,
}

#[link(name = "kernel32")]
extern "system" {
    fn GetModuleHandleW(name: *const u16) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const u8) -> *mut c_void;
    fn GetModuleFileNameW(module: *mut c_void, path: *mut u16, size: u32) -> u32;
    fn VirtualQuery(
        address: *const c_void,
        info: *mut MemoryBasicInformation,
        length: usize,
    ) -> usize;
    fn VirtualProtect(
        address: *mut c_void,
        length: usize,
        new_protect: u32,
        old_protect: *mut u32,
    ) -> c_int;
    fn GetCurrentThreadId() -> u32;
    fn QueryPerformanceCounter(value: *mut i64) -> c_int;
    fn QueryPerformanceFrequency(value: *mut i64) -> c_int;
    fn TryEnterCriticalSection(lock: *mut c_void) -> c_int;
    fn LeaveCriticalSection(lock: *mut c_void);
    fn GetModuleHandleExW(flags: u32, name: *const u16, module: *mut *mut c_void) -> c_int;
    #[cfg(test)]
    fn InitializeCriticalSection(lock: *mut c_void);
    #[cfg(test)]
    fn DeleteCriticalSection(lock: *mut c_void);
}

#[link(name = "bcrypt")]
extern "system" {
    fn BCryptOpenAlgorithmProvider(
        handle: *mut *mut c_void,
        algorithm: *const u16,
        implementation: *const u16,
        flags: u32,
    ) -> i32;
    fn BCryptCreateHash(
        algorithm: *mut c_void,
        handle: *mut *mut c_void,
        object: *mut u8,
        object_length: u32,
        secret: *mut u8,
        secret_length: u32,
        flags: u32,
    ) -> i32;
    fn BCryptHashData(handle: *mut c_void, input: *const u8, length: u32, flags: u32) -> i32;
    fn BCryptFinishHash(handle: *mut c_void, output: *mut u8, length: u32, flags: u32) -> i32;
    fn BCryptDestroyHash(handle: *mut c_void) -> i32;
    fn BCryptCloseAlgorithmProvider(handle: *mut c_void, flags: u32) -> i32;
}

type LuaState = c_void;
type LuaFunction = Option<unsafe extern "C-unwind" fn(*mut LuaState) -> c_int>;
type LuaGettop = unsafe extern "C-unwind" fn(*mut LuaState) -> c_int;
type LuaSettop = unsafe extern "C-unwind" fn(*mut LuaState, c_int);
type LuaPushthread = unsafe extern "C-unwind" fn(*mut LuaState) -> c_int;
type LuaPushvalue = unsafe extern "C-unwind" fn(*mut LuaState, c_int);
type LuaPushboolean = unsafe extern "C-unwind" fn(*mut LuaState, c_int);
type LuaPushnumber = unsafe extern "C-unwind" fn(*mut LuaState, c_double);
type LuaPushlstring = unsafe extern "C-unwind" fn(*mut LuaState, *const c_char, usize);
type LuaPushcclosure = unsafe extern "C-unwind" fn(*mut LuaState, LuaFunction, c_int);
type LuaGetfield = unsafe extern "C-unwind" fn(*mut LuaState, c_int, *const c_char);
type LuaSetfield = unsafe extern "C-unwind" fn(*mut LuaState, c_int, *const c_char);
type LuaCreatetable = unsafe extern "C-unwind" fn(*mut LuaState, c_int, c_int);
type LuaNewuserdata = unsafe extern "C-unwind" fn(*mut LuaState, usize) -> *mut c_void;
type LuaSetmetatable = unsafe extern "C-unwind" fn(*mut LuaState, c_int) -> c_int;
type LuaLNewmetatable = unsafe extern "C-unwind" fn(*mut LuaState, *const c_char) -> c_int;
type LuaLCheckudata =
    unsafe extern "C-unwind" fn(*mut LuaState, c_int, *const c_char) -> *mut c_void;
type LuaLChecklstring =
    unsafe extern "C-unwind" fn(*mut LuaState, c_int, *mut usize) -> *const c_char;
type LuaLChecknumber = unsafe extern "C-unwind" fn(*mut LuaState, c_int) -> c_double;
type LuaLChecktype = unsafe extern "C-unwind" fn(*mut LuaState, c_int, c_int);
type LuaLRef = unsafe extern "C-unwind" fn(*mut LuaState, c_int) -> c_int;
type LuaLUnref = unsafe extern "C-unwind" fn(*mut LuaState, c_int, c_int);
type LuaRawgeti = unsafe extern "C-unwind" fn(*mut LuaState, c_int, c_int);
type LuaObjlen = unsafe extern "C-unwind" fn(*mut LuaState, c_int) -> usize;
type LuaPcall = unsafe extern "C-unwind" fn(*mut LuaState, c_int, c_int, c_int) -> c_int;

#[derive(Default, Copy, Clone)]
struct LuaApi {
    gettop: Option<LuaGettop>,
    settop: Option<LuaSettop>,
    pushthread: Option<LuaPushthread>,
    pushvalue: Option<LuaPushvalue>,
    pushboolean: Option<LuaPushboolean>,
    pushnumber: Option<LuaPushnumber>,
    pushlstring: Option<LuaPushlstring>,
    pushcclosure: Option<LuaPushcclosure>,
    getfield: Option<LuaGetfield>,
    setfield: Option<LuaSetfield>,
    createtable: Option<LuaCreatetable>,
    newuserdata: Option<LuaNewuserdata>,
    setmetatable: Option<LuaSetmetatable>,
    newmetatable: Option<LuaLNewmetatable>,
    checkudata: Option<LuaLCheckudata>,
    checklstring: Option<LuaLChecklstring>,
    checknumber: Option<LuaLChecknumber>,
    checktype: Option<LuaLChecktype>,
    reference: Option<LuaLRef>,
    unref: Option<LuaLUnref>,
    rawgeti: Option<LuaRawgeti>,
    objlen: Option<LuaObjlen>,
    pcall: Option<LuaPcall>,
}

static mut API: LuaApi = LuaApi {
    gettop: None,
    settop: None,
    pushthread: None,
    pushvalue: None,
    pushboolean: None,
    pushnumber: None,
    pushlstring: None,
    pushcclosure: None,
    getfield: None,
    setfield: None,
    createtable: None,
    newuserdata: None,
    setmetatable: None,
    newmetatable: None,
    checkudata: None,
    checklstring: None,
    checknumber: None,
    checktype: None,
    reference: None,
    unref: None,
    rawgeti: None,
    objlen: None,
    pcall: None,
};

#[inline]
unsafe fn api() -> LuaApi {
    API
}

unsafe fn readable(address: *const c_void, length: usize, write: bool) -> bool {
    if address.is_null() || address as usize > usize::MAX - length {
        return false;
    }
    let mut info: MemoryBasicInformation = core::mem::zeroed();
    if VirtualQuery(
        address,
        &mut info,
        core::mem::size_of::<MemoryBasicInformation>(),
    ) == 0
    {
        return false;
    }
    if info.state != MEM_COMMIT || info.protect & (PAGE_GUARD | PAGE_NOACCESS) != 0 {
        return false;
    }
    if address as usize + length > info.base_address as usize + info.region_size {
        return false;
    }
    let protect = info.protect & 0xff;
    if write {
        matches!(
            protect,
            PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY
        )
    } else {
        matches!(
            protect,
            PAGE_READONLY
                | PAGE_READWRITE
                | PAGE_WRITECOPY
                | PAGE_EXECUTE_READ
                | PAGE_EXECUTE_READWRITE
                | PAGE_EXECUTE_WRITECOPY
        )
    }
}

fn digest_matches(digest: &[u8; 32], expected: &str) -> bool {
    expected
        .as_bytes()
        .chunks_exact(2)
        .zip(digest)
        .all(|(pair, byte)| {
            let digit = |value: u8| -> u8 {
                match value {
                    b'0'..=b'9' => value - b'0',
                    b'a'..=b'f' => value - b'a' + 10,
                    _ => 255,
                }
            };
            digit(pair[0]) << 4 | digit(pair[1]) == *byte
        })
}

unsafe fn sha256(data: &[u8], expected: &str) -> bool {
    let algorithm_name: Vec<u16> = "SHA256\0".encode_utf16().collect();
    let mut algorithm = ptr::null_mut();
    let mut hash = ptr::null_mut();
    let mut ok = false;
    if BCryptOpenAlgorithmProvider(&mut algorithm, algorithm_name.as_ptr(), ptr::null(), 0) >= 0 {
        if BCryptCreateHash(
            algorithm,
            &mut hash,
            ptr::null_mut(),
            0,
            ptr::null_mut(),
            0,
            0,
        ) >= 0
        {
            if BCryptHashData(hash, data.as_ptr(), data.len() as u32, 0) >= 0 {
                let mut digest = [0u8; 32];
                if BCryptFinishHash(hash, digest.as_mut_ptr(), 32, 0) >= 0 {
                    ok = digest_matches(&digest, expected);
                }
            }
        }
    }
    if !hash.is_null() {
        BCryptDestroyHash(hash);
    }
    if !algorithm.is_null() {
        BCryptCloseAlgorithmProvider(algorithm, 0);
    }
    ok
}

unsafe fn verify_file() -> bool {
    let mut path = vec![0u16; 32768];
    let length = GetModuleFileNameW(ptr::null_mut(), path.as_mut_ptr(), path.len() as u32) as usize;
    if length == 0 || length >= path.len() {
        return false;
    }
    let path = String::from_utf16_lossy(&path[..length]);
    let Ok(data) = std::fs::read(path) else {
        return false;
    };
    data.len() == 55_412_768
        && sha256(
            &data,
            "19750a6563889a970f434b5566eb396c6b2dc29ff814bd3e336f838176ad6891",
        )
}

static mut IMAGE: usize = 0;

unsafe fn verify_image() -> bool {
    IMAGE = GetModuleHandleW(ptr::null()) as usize;
    if !readable(IMAGE as *const c_void, 0x400, false) {
        return false;
    }
    let pe_offset = ptr::read((IMAGE + 0x3c) as *const u32) as usize;
    if pe_offset > 4096 || pe_offset + 24 + 112 > IMAGE_SIZE {
        return false;
    }
    if slice::from_raw_parts(IMAGE as *const u8, 2) != b"MZ"
        || slice::from_raw_parts((IMAGE + pe_offset) as *const u8, 4) != b"PE\0\0"
    {
        return false;
    }
    let machine = ptr::read((IMAGE + pe_offset + 4) as *const u16);
    let timestamp = ptr::read((IMAGE + pe_offset + 8) as *const u32);
    let optional = IMAGE + pe_offset + 24;
    let magic = ptr::read(optional as *const u16);
    let image_size = ptr::read((optional + 56) as *const u32) as usize;
    if machine != 0x8664
        || timestamp != 0x6a1eaded
        || magic != 0x20b
        || image_size != IMAGE_SIZE
        || !verify_file()
    {
        return false;
    }
    const CHECKS: &[(usize, usize, &str)] = &[
        (
            0xef4400,
            25,
            "4449b89ae08c1ebc42ad4bb0ad61acd1c63d68de52c6ae63c74e639390f85e91",
        ),
        (
            0xef4681,
            64,
            "161234434ac6f861075f55a3b7b5e4643a45c7b72045b225e4c6a3cdbc78cd6d",
        ),
        (
            0x122b360,
            24,
            "8e123f79687ad689c48d2eaf879253c42d1359f129ddebd2c2d51ea6658a46d0",
        ),
        (
            0xeac170,
            24,
            "718b690e9f3d2779d2f91c812253fb26bfa1f4ce4cd73accae7229675e261f12",
        ),
        (
            0xb3bad0,
            28,
            "893307c89a256e16553b164d9d16ec4f0c3187060ebd86513b8f55694f1378e4",
        ),
        (
            0x14ca580,
            42,
            "896bc70922e9954e08c838c449f51dbcdc428842a4c25dd6a80bdea2c4cc6a26",
        ),
        (
            0x14dd110,
            32,
            "c3b011f86c6f481ec3b6fda61f936b3655b877f68b0f86b4914be297099d76c6",
        ),
        (
            0x96350,
            32,
            "a47138b202ba1574f1071f2b794644d07278a7a9c53e5f24b3807c8f5a999922",
        ),
        (
            0xfa32d0,
            96,
            "a8c17a7c4630ea80df70466732b321646ae7caa0ee094fb3a7ad32aff110f25a",
        ),
        (
            0xced70,
            64,
            "99aa931616db6c9f92574b2408563aa3b9f09a452bc5456ce0ec25fa8a9bb9cf",
        ),
        (
            0x14b8e20,
            32,
            "47607eb1de710e125f0bc5ea084b067f798f48fd75b9e74ebe396990eea6ab2e",
        ),
        (
            0xafcdc0,
            32,
            "16eb923df8d37ae6b7daefbccf3398925b0e3ee360d688410ba1d7571cf1e169",
        ),
        (
            0x51f5b0,
            32,
            "a2b4364ca7de1e4d30388ad9e5f9432dbf97a2faaa272084693b0008bcd0090a",
        ),
        (
            0x3db8a0,
            32,
            "2964d204251dcce8a745407fe85cfe69106ba70a2507937577c7fe220422c6b2",
        ),
        (
            0x843b20,
            1192,
            "b92e75128f6a0849de28ef5c913cd2e3c4d724c37edd30c87e25fd9afe099516",
        ),
        (
            0x7d5110,
            350,
            "f77821b926165edbf624dd73550b7b5aad739efda149f619fc75b9e19ea852be",
        ),
        (
            0x3e0c40,
            554,
            "7e70525adbeb32c7d9cc9a10ea41ffe712641c3a5729486a33d33294f1f87f73",
        ),
        (
            0x3d5660,
            534,
            "6e03eb394d53282c5b5b7c00daff0692fa4f00ed5f5bf4c365d204496a9a7805",
        ),
        (
            0x2b0a7d8,
            8,
            "3e7a57cf45cd92955126302541edce77815801e93b28108b6aa0e46c360bd375",
        ),
        (
            0x2b67678,
            8,
            "3e7a57cf45cd92955126302541edce77815801e93b28108b6aa0e46c360bd375",
        ),
        (
            0x2b86090,
            8,
            "13abb71be0dba16645b4a165a28fa302611a0ad23692a31389567286a3ce43e1",
        ),
        (
            0x2b9b080,
            8,
            "13abb71be0dba16645b4a165a28fa302611a0ad23692a31389567286a3ce43e1",
        ),
        (
            0x2b7ed30,
            8,
            "13abb71be0dba16645b4a165a28fa302611a0ad23692a31389567286a3ce43e1",
        ),
        (
            0x2b9d5f0,
            8,
            "13abb71be0dba16645b4a165a28fa302611a0ad23692a31389567286a3ce43e1",
        ),
        (
            0x2b83a38,
            8,
            "13abb71be0dba16645b4a165a28fa302611a0ad23692a31389567286a3ce43e1",
        ),
        (
            0x2b814b0,
            8,
            "13abb71be0dba16645b4a165a28fa302611a0ad23692a31389567286a3ce43e1",
        ),
        (
            0x2b79fa8,
            8,
            "13abb71be0dba16645b4a165a28fa302611a0ad23692a31389567286a3ce43e1",
        ),
    ];
    for &(rva, length, expected) in CHECKS {
        if !readable((IMAGE + rva) as *const c_void, length, false)
            || !sha256(
                slice::from_raw_parts((IMAGE + rva) as *const u8, length),
                expected,
            )
        {
            return false;
        }
    }
    readable((IMAGE + VBI_RVA) as *const c_void, 8, false)
        && readable((IMAGE + ANARK_RVA) as *const c_void, 8, false)
        && readable((IMAGE + REGISTRY_RVA) as *const c_void, 8, false)
        && readable((IMAGE + GAME_TIME_RVA) as *const c_void, 8, false)
        && readable((IMAGE + SESSION_SEED_RVA) as *const c_void, 8, false)
}

#[repr(C)]
struct Owner {
    frames: u64,
    armed: bool,
}
#[repr(C)]
struct Handler {
    vtable: *const *const c_void,
}
static mut VTABLE: [*const c_void; 3] = [ptr::null(); 3];
static mut HOOK: Handler = Handler {
    vtable: ptr::null(),
};
static mut CURRENT: *mut Owner = ptr::null_mut();
static mut STATE: *mut LuaState = ptr::null_mut();
static mut CALLBACK_REF: c_int = -2;
static mut REGISTERED_TABLE: *mut *mut c_void = ptr::null_mut();
static mut REGISTERED_UI: *mut u8 = ptr::null_mut();
static mut REGISTERED_ANARK: *mut u8 = ptr::null_mut();
static mut OWNER_THREAD: u32 = 0;
static mut INSIDE_CALLBACK: bool = false;
static mut TICK: u64 = 0;
static mut CADENCE: u64 = 1;
static mut REGISTERED_REGISTRY: *mut c_void = ptr::null_mut();
static mut ATTENTION_INSTALLED: [bool; 9] = [false; 9];
static ATTENTION_ENABLED: AtomicBool = AtomicBool::new(false);
static REMOTE_PROXY: AtomicU64 = AtomicU64::new(0);

extern "C" fn no_op(_: *mut Handler) {}

type ComputeAttention = unsafe extern "C" fn(*mut u8) -> i32;
type SetAttention = unsafe extern "C" fn(*mut u8, i32);
type IsClass = unsafe extern "C" fn(*mut u8, u32) -> bool;
type AttentionPredicate = unsafe extern "C" fn(*mut u8, i32, c_int) -> bool;
type AttentionRadius = unsafe extern "C" fn(*mut u8) -> f32;
type GetTransform = unsafe extern "C" fn(*mut u8, *mut f32, f64, *mut u8, c_int) -> *mut c_void;
type GetDistanceSq =
    unsafe extern "C" fn(*mut u8, *const f32, *mut u8, f64, c_int, bool, f32) -> f32;

unsafe fn is_class(object: *mut u8, class: u32) -> bool {
    if object.is_null() {
        return false;
    }
    let vtable = ptr::read(object as *const *const c_void);
    let function: IsClass =
        core::mem::transmute(ptr::read((vtable as *const *const c_void).add(0x11e8 / 8)));
    function(object, class)
}

unsafe fn ancestor(object: *mut u8, class: u32) -> *mut u8 {
    let mut current = object;
    for _ in 0..32 {
        if current.is_null() {
            return ptr::null_mut();
        }
        if is_class(current, class) {
            return current;
        }
        current = ptr::read(current.add(0x70) as *const *mut u8);
    }
    ptr::null_mut()
}

unsafe fn recompute_attention(proxy: *mut u8) {
    if proxy.is_null() {
        return;
    }
    let set_attention: SetAttention = core::mem::transmute(IMAGE + SET_ATTENTION_RVA);
    let zone = ancestor(proxy, 0x6d);
    if !zone.is_null() {
        set_attention(zone, 1);
    }
    set_attention(proxy, 1);
}

unsafe fn remote_proxy() -> *mut u8 {
    if !ATTENTION_ENABLED.load(Ordering::Acquire) || GetCurrentThreadId() != OWNER_THREAD {
        return ptr::null_mut();
    }
    let id = REMOTE_PROXY.load(Ordering::Acquire);
    if id == 0 || IMAGE == 0 || REGISTERED_TABLE.is_null() {
        return ptr::null_mut();
    }
    if ptr::read((IMAGE + VBI_RVA) as *const *mut *mut c_void) != REGISTERED_TABLE
        || ptr::read((REGISTERED_TABLE as *mut u8).add(0x130) as *const c_int) != 0
    {
        return ptr::null_mut();
    }
    let registry = ptr::read((IMAGE + REGISTRY_RVA) as *const *mut c_void);
    if registry.is_null() || registry != REGISTERED_REGISTRY {
        return ptr::null_mut();
    }
    let find: unsafe extern "C" fn(*mut c_void, u64, u32) -> *mut u8 =
        core::mem::transmute(IMAGE + REGISTRY_FIND_RVA);
    let proxy = find(registry, id, 4);
    if proxy.is_null() || ptr::read(proxy.add(0xd1)) == 0 || ptr::read(proxy.add(0xd3)) != 0 {
        return ptr::null_mut();
    }
    proxy
}

fn union_zone_attention(base: i32, contains_proxy: bool) -> i32 {
    if contains_proxy {
        base.max(8)
    } else {
        base
    }
}

fn union_ship_attention(base: i32, near_proxy: bool) -> i32 {
    if near_proxy {
        base.max(9)
    } else {
        base
    }
}

unsafe extern "C" fn zone_attention(zone: *mut u8) -> i32 {
    let original: ComputeAttention = core::mem::transmute(IMAGE + ZONE_COMPUTE_RVA);
    let base = original(zone);
    if base >= 8 {
        return base;
    }
    let proxy = remote_proxy();
    union_zone_attention(base, !proxy.is_null() && ancestor(proxy, 0x6d) == zone)
}

unsafe extern "C" fn ship_attention(ship: *mut u8) -> i32 {
    let original: ComputeAttention = core::mem::transmute(IMAGE + SHIP_COMPUTE_RVA);
    let base = original(ship);
    if base >= 9 {
        return base;
    }
    let proxy = remote_proxy();
    if proxy.is_null() {
        return base;
    }
    if proxy == ship {
        return union_ship_attention(base, true);
    }
    let parent = ptr::read(ship.add(0x70) as *const *mut u8);
    if parent.is_null() || !is_class(parent, 0x6d) {
        return base;
    }
    let cluster = ancestor(ship, 0x0f);
    if cluster.is_null() || cluster != ancestor(proxy, 0x0f) {
        return base;
    }
    let zone_vtable = ptr::read(parent as *const *const c_void);
    let active: AttentionPredicate = core::mem::transmute(ptr::read(
        (zone_vtable as *const *const c_void).add(0x1580 / 8),
    ));
    if !active(parent, ptr::read(parent.add(0x1ec) as *const i32), 0) {
        return base;
    }
    let ship_vtable = ptr::read(ship as *const *const c_void);
    let radius_fn: AttentionRadius = core::mem::transmute(ptr::read(
        (ship_vtable as *const *const c_void).add(0x1ea8 / 8),
    ));
    let radius = radius_fn(ship);
    if radius <= 0.0 {
        return base;
    }
    let tls = thread_block();
    if tls.is_null() {
        return base;
    }
    let time = ptr::read(tls.add(0x300) as *const f64);
    let mut transform = [0.0f32; 16];
    let get_transform: GetTransform = core::mem::transmute(IMAGE + GET_TRANSFORM_RVA);
    get_transform(proxy, transform.as_mut_ptr(), time, parent, 0);
    let distance_sq: GetDistanceSq = core::mem::transmute(IMAGE + DISTANCE_SQ_RVA);
    let distance = distance_sq(ship, transform.as_ptr(), parent, time, 2, true, 0.0);
    union_ship_attention(base, distance <= radius * radius)
}

unsafe fn swap_attention_slot(rva: usize, expected: *mut c_void, replacement: *mut c_void) -> bool {
    let slot = (IMAGE + rva) as *mut *mut c_void;
    if !readable(slot as *const c_void, 8, false) || ptr::read(slot) != expected {
        return false;
    }
    let mut old_protect = 0u32;
    if VirtualProtect(slot as *mut c_void, 8, PAGE_READWRITE, &mut old_protect) == 0 {
        return false;
    }
    let exchanged = (&*(slot as *const AtomicPtr<c_void>))
        .compare_exchange(expected, replacement, Ordering::SeqCst, Ordering::SeqCst)
        .is_ok();
    let restored = VirtualProtect(slot as *mut c_void, 8, old_protect, &mut old_protect) != 0;
    if exchanged && !restored {
        let _ = (&*(slot as *const AtomicPtr<c_void>)).compare_exchange(
            replacement,
            expected,
            Ordering::SeqCst,
            Ordering::SeqCst,
        );
        VirtualProtect(slot as *mut c_void, 8, old_protect, &mut old_protect);
    }
    exchanged && restored
}

unsafe fn uninstall_attention() {
    ATTENTION_ENABLED.store(false, Ordering::Release);
    REMOTE_PROXY.store(0, Ordering::Release);
    let hooks = ZONE_ATTENTION_SLOTS
        .iter()
        .map(|rva| (*rva, ZONE_COMPUTE_RVA, zone_attention as *const c_void))
        .chain(
            SHIP_ATTENTION_SLOTS
                .iter()
                .map(|rva| (*rva, SHIP_COMPUTE_RVA, ship_attention as *const c_void)),
        );
    for (index, (rva, original, replacement)) in hooks.enumerate() {
        if ATTENTION_INSTALLED[index]
            && swap_attention_slot(
                rva,
                replacement as *mut c_void,
                (IMAGE + original) as *mut c_void,
            )
        {
            ATTENTION_INSTALLED[index] = false;
        }
    }
}

unsafe fn install_attention() -> bool {
    if (0..9).any(|index| ATTENTION_INSTALLED[index]) {
        return false;
    }
    REMOTE_PROXY.store(0, Ordering::Release);
    ATTENTION_ENABLED.store(false, Ordering::Release);
    let hooks = ZONE_ATTENTION_SLOTS
        .iter()
        .map(|rva| (*rva, ZONE_COMPUTE_RVA, zone_attention as *const c_void))
        .chain(
            SHIP_ATTENTION_SLOTS
                .iter()
                .map(|rva| (*rva, SHIP_COMPUTE_RVA, ship_attention as *const c_void)),
        );
    for (index, (rva, original, replacement)) in hooks.enumerate() {
        if !swap_attention_slot(
            rva,
            (IMAGE + original) as *mut c_void,
            replacement as *mut c_void,
        ) {
            uninstall_attention();
            return false;
        }
        ATTENTION_INSTALLED[index] = true;
    }
    ATTENTION_ENABLED.store(true, Ordering::Release);
    true
}

unsafe fn table_now() -> *mut *mut c_void {
    if IMAGE == 0 {
        ptr::null_mut()
    } else {
        ptr::read((IMAGE + VBI_RVA) as *const *mut *mut c_void)
    }
}

unsafe fn table_owned() -> bool {
    let table = table_now();
    !table.is_null()
        && table == REGISTERED_TABLE
        && readable(table as *const c_void, 0x138, true)
        && ptr::read(table.add(SLOT)) == addr_of_mut!(HOOK) as *mut c_void
}

unsafe fn disarm() {
    timing::disarm();
    uninstall_attention();
    if !CURRENT.is_null() {
        (*CURRENT).armed = false;
    }
    if table_owned() {
        let slot = &*(REGISTERED_TABLE.add(SLOT) as *const AtomicPtr<c_void>);
        let _ = slot.compare_exchange(
            addr_of_mut!(HOOK) as *mut c_void,
            ptr::null_mut(),
            Ordering::SeqCst,
            Ordering::SeqCst,
        );
    }
}

unsafe fn context_valid() -> bool {
    if !table_owned()
        || ptr::read(REGISTERED_TABLE.add(6)) != REGISTERED_UI as *mut c_void
        || !readable(REGISTERED_UI as *const c_void, 0x70 + 40, false)
        || ptr::read(REGISTERED_UI as *const *const c_void)
            != (IMAGE + UI_VTABLE_RVA) as *const c_void
        || ptr::read((IMAGE + ANARK_RVA) as *const *mut u8) != REGISTERED_ANARK
        || !readable(REGISTERED_ANARK as *const c_void, 0x3f1, false)
    {
        return false;
    }
    true
}

unsafe fn thread_block() -> *mut u8 {
    let slots: *const *mut c_void;
    core::arch::asm!("mov {}, gs:[0x58]", out(reg) slots, options(nostack, preserves_flags, readonly));
    if slots.is_null() {
        ptr::null_mut()
    } else {
        ptr::read(slots) as *mut u8
    }
}

fn hex64(value: u64) -> [u8; 16] {
    let mut result = [0u8; 16];
    for (index, byte) in result.iter_mut().enumerate() {
        *byte = b"0123456789abcdef"[((value >> (60 - 4 * index)) & 15) as usize];
    }
    result
}

fn qpc_seconds() -> Option<f64> {
    static mut FREQUENCY: i64 = 0;
    unsafe {
        if FREQUENCY == 0 && QueryPerformanceFrequency(addr_of_mut!(FREQUENCY)) == 0 {
            return None;
        }
        if FREQUENCY <= 0 {
            return None;
        }
        let mut counter = 0;
        if QueryPerformanceCounter(&mut counter) == 0 {
            return None;
        }
        Some(counter as f64 / FREQUENCY as f64)
    }
}

struct Fnv(u64);

impl Fnv {
    fn new() -> Self {
        Self(0xcbf29ce484222325)
    }

    fn update(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.0 = (self.0 ^ *byte as u64).wrapping_mul(0x100000001b3);
        }
    }
}

fn role_seed(session_seed: u64, role: &[u8]) -> u64 {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in session_seed.to_le_bytes().iter().chain(role) {
        hash = (hash ^ *byte as u64).wrapping_mul(0x100000001b3);
    }
    hash
}

extern "C" fn move_observer(_: *mut Handler) {
    unsafe {
        if CURRENT.is_null() || !(*CURRENT).armed || INSIDE_CALLBACK {
            return;
        }
        if GetCurrentThreadId() != OWNER_THREAD || !context_valid() {
            disarm();
            return;
        }
        if ptr::read((REGISTERED_TABLE as *mut u8).add(0x130) as *const c_int) != 0 {
            return;
        }
        TICK += 1;
        if TICK % CADENCE != 0 {
            return;
        }
        let tls = thread_block();
        if !readable(tls as *const c_void, 0x330, false)
            || !readable((IMAGE + GAME_TIME_RVA) as *const c_void, 8, false)
        {
            disarm();
            return;
        }
        let lock = REGISTERED_UI.add(0x70) as *mut c_void;
        if TryEnterCriticalSection(lock) == 0 {
            return;
        }
        if !context_valid() {
            disarm();
            LeaveCriticalSection(lock);
            return;
        }
        if ptr::read((REGISTERED_TABLE as *mut u8).add(0x130) as *const c_int) != 0
            || ptr::read(REGISTERED_ANARK.add(0x3f0)) == 0
        {
            LeaveCriticalSection(lock);
            return;
        }
        let calling = CURRENT;
        let lua = STATE;
        let functions = api();
        let top = (functions.gettop.unwrap())(lua);
        let game_time = ptr::read((IMAGE + GAME_TIME_RVA) as *const f64);
        let previous = ptr::read(tls.add(0x328) as *const f64);
        let rng = hex64(ptr::read(tls.add(0x318) as *const u64));
        let session_seed = hex64(ptr::read((IMAGE + SESSION_SEED_RVA) as *const u64));
        INSIDE_CALLBACK = true;
        (functions.rawgeti.unwrap())(lua, REGISTRYINDEX, CALLBACK_REF);
        (functions.pushnumber.unwrap())(lua, TICK as f64);
        (functions.pushnumber.unwrap())(lua, game_time);
        (functions.pushnumber.unwrap())(lua, game_time - previous);
        (functions.pushlstring.unwrap())(lua, rng.as_ptr() as *const c_char, 16);
        (functions.pushlstring.unwrap())(lua, session_seed.as_ptr() as *const c_char, 16);
        let result = (functions.pcall.unwrap())(lua, 5, 0, 0);
        (functions.settop.unwrap())(lua, top);
        INSIDE_CALLBACK = false;
        if CURRENT == calling {
            if result != 0 {
                disarm();
            } else {
                (*calling).frames += 1;
            }
        }
        LeaveCriticalSection(lock);
    }
}

unsafe extern "C-unwind" fn stop(lua: *mut LuaState) -> c_int {
    let functions = api();
    let owner =
        (functions.checkudata.unwrap())(lua, 1, META.as_ptr() as *const c_char) as *mut Owner;
    if owner == CURRENT {
        disarm();
        let reference = CALLBACK_REF;
        CURRENT = ptr::null_mut();
        STATE = ptr::null_mut();
        CALLBACK_REF = -2;
        TICK = 0;
        CADENCE = 1;
        (functions.unref.unwrap())(lua, REGISTRYINDEX, reference);
    }
    0
}

unsafe extern "C-unwind" fn active(lua: *mut LuaState) -> c_int {
    let functions = api();
    let owner =
        (functions.checkudata.unwrap())(lua, 1, META.as_ptr() as *const c_char) as *mut Owner;
    if owner == CURRENT && (*owner).armed && !context_valid() {
        disarm();
    }
    (functions.pushboolean.unwrap())(lua, (owner == CURRENT && (*owner).armed) as c_int);
    1
}

unsafe extern "C-unwind" fn frames(lua: *mut LuaState) -> c_int {
    let functions = api();
    let owner =
        (functions.checkudata.unwrap())(lua, 1, META.as_ptr() as *const c_char) as *mut Owner;
    (functions.pushnumber.unwrap())(lua, (*owner).frames as f64);
    1
}

unsafe fn failure(lua: *mut LuaState, reason: &str) -> c_int {
    let functions = api();
    (functions.pushboolean.unwrap())(lua, 0);
    (functions.pushlstring.unwrap())(lua, reason.as_ptr() as *const c_char, reason.len());
    2
}

unsafe extern "C-unwind" fn start(lua: *mut LuaState) -> c_int {
    let functions = api();
    (functions.checktype.unwrap())(lua, 1, 6);
    let requested = (functions.checknumber.unwrap())(lua, 2);
    if requested < 1.0 || requested > 3600.0 || requested != requested.trunc() {
        return failure(lua, "cadence must be an integer from 1 to 3600");
    }
    if !CURRENT.is_null() || INSIDE_CALLBACK {
        return failure(lua, "an owner already exists; stop it first");
    }
    let main = (functions.pushthread.unwrap())(lua);
    (functions.settop.unwrap())(lua, -2);
    if main == 0 {
        return failure(lua, "main Lua thread required");
    }
    if !verify_image() {
        return failure(lua, "unsupported executable or modified engine signatures");
    }
    let table = table_now();
    if !readable(table as *const c_void, 0x138, true)
        || !ptr::read(table.add(SLOT)).is_null()
        || !ptr::read(table.add(17 + SLOT)).is_null()
        || ptr::read(table.add(3)).is_null()
    {
        return failure(lua, "VBI slot occupied or unavailable");
    }
    if ptr::read((table as *mut u8).add(0x130) as *const c_int) != 0 {
        return failure(lua, "loading");
    }
    let ui = ptr::read(table.add(6)) as *mut u8;
    let anark = ptr::read((IMAGE + ANARK_RVA) as *const *mut u8);
    let registry = ptr::read((IMAGE + REGISTRY_RVA) as *const *mut c_void);
    if !readable(ui as *const c_void, 0x70 + 40, false)
        || ptr::read(ui as *const *const c_void) != (IMAGE + UI_VTABLE_RVA) as *const c_void
        || !readable(anark as *const c_void, 0x3f1, false)
        || ptr::read(anark.add(0x3f0)) == 0
        || !readable(registry as *const c_void, 0x158, false)
    {
        return failure(lua, "UI lifetime guard unavailable");
    }
    let mut pinned = ptr::null_mut();
    if GetModuleHandleExW(1 | 4, addr_of!(HOOK) as *const u16, &mut pinned) == 0 {
        return failure(lua, "cannot pin callback module");
    }
    let owner = (functions.newuserdata.unwrap())(lua, core::mem::size_of::<Owner>()) as *mut Owner;
    ptr::write(
        owner,
        Owner {
            frames: 0,
            armed: false,
        },
    );
    (functions.getfield.unwrap())(lua, REGISTRYINDEX, META.as_ptr() as *const c_char);
    (functions.setmetatable.unwrap())(lua, -2);
    (functions.pushvalue.unwrap())(lua, 1);
    let reference = (functions.reference.unwrap())(lua, REGISTRYINDEX);
    REGISTERED_TABLE = table;
    REGISTERED_UI = ui;
    REGISTERED_ANARK = anark;
    REGISTERED_REGISTRY = registry;
    OWNER_THREAD = GetCurrentThreadId();
    STATE = lua;
    CURRENT = owner;
    CALLBACK_REF = reference;
    TICK = 0;
    CADENCE = requested as u64;
    let slot = &*(table.add(SLOT) as *const AtomicPtr<c_void>);
    if slot
        .compare_exchange(
            ptr::null_mut(),
            addr_of_mut!(HOOK) as *mut c_void,
            Ordering::SeqCst,
            Ordering::SeqCst,
        )
        .is_err()
    {
        CURRENT = ptr::null_mut();
        STATE = ptr::null_mut();
        CALLBACK_REF = -2;
        TICK = 0;
        CADENCE = 1;
        (functions.unref.unwrap())(lua, REGISTRYINDEX, reference);
        return failure(lua, "VBI slot changed during registration");
    }
    let tls = thread_block();
    if !readable(tls as *const c_void, 0x330, false)
        || ptr::read(tls.add(0x314) as *const c_int) != 1
    {
        disarm();
        CURRENT = ptr::null_mut();
        STATE = ptr::null_mut();
        CALLBACK_REF = -2;
        TICK = 0;
        CADENCE = 1;
        (functions.unref.unwrap())(lua, REGISTRYINDEX, reference);
        return failure(lua, "MoveWorker TLS unavailable");
    }
    if !install_attention() {
        disarm();
        CURRENT = ptr::null_mut();
        STATE = ptr::null_mut();
        CALLBACK_REF = -2;
        TICK = 0;
        CADENCE = 1;
        (functions.unref.unwrap())(lua, REGISTRYINDEX, reference);
        return failure(lua, "shared-attention vtable registration refused");
    }
    let session_seed = ptr::read((IMAGE + SESSION_SEED_RVA) as *const u64);
    ptr::write(
        tls.add(0x318) as *mut u64,
        role_seed(session_seed, b"MoveWorker"),
    );
    (*owner).armed = true;
    1
}

fn parse_id(text: &[u8]) -> Option<u64> {
    if text.is_empty() || text.len() > 20 {
        return None;
    }
    let mut value = 0u64;
    for byte in text {
        if !byte.is_ascii_digit() {
            return None;
        }
        value = value.checked_mul(10)?.checked_add((byte - b'0') as u64)?;
    }
    Some(value)
}

unsafe extern "C-unwind" fn set_remote_attention(lua: *mut LuaState) -> c_int {
    let functions = api();
    let mut length = 0usize;
    let text = (functions.checklstring.unwrap())(lua, 1, &mut length);
    if CURRENT.is_null()
        || !(*CURRENT).armed
        || GetCurrentThreadId() != OWNER_THREAD
        || !context_valid()
    {
        return failure(lua, "shared attention unavailable on this thread");
    }
    let previous = remote_proxy();
    REMOTE_PROXY.store(0, Ordering::Release);
    let Some(id) = parse_id(slice::from_raw_parts(text as *const u8, length)) else {
        return failure(lua, "invalid remote proxy id");
    };
    let mut current = ptr::null_mut();
    if id != 0 {
        let registry = ptr::read((IMAGE + REGISTRY_RVA) as *const *mut c_void);
        if registry != REGISTERED_REGISTRY || !readable(registry as *const c_void, 0x158, false) {
            return failure(lua, "component registry unavailable");
        }
        let find: unsafe extern "C" fn(*mut c_void, u64, u32) -> *mut u8 =
            core::mem::transmute(IMAGE + REGISTRY_FIND_RVA);
        current = find(registry, id, 4);
        if !readable(current as *const c_void, 0x350, false)
            || ptr::read(current.add(0xd1)) == 0
            || ptr::read(current.add(0xd3)) != 0
            || !is_class(current, 0x75)
        {
            return failure(lua, "remote proxy is not an operational ship");
        }
        REMOTE_PROXY.store(id, Ordering::Release);
    }
    recompute_attention(previous);
    recompute_attention(current);
    (functions.pushboolean.unwrap())(lua, 1);
    1
}

unsafe fn field_number(lua: *mut LuaState, name: &[u8], value: f64) {
    let functions = api();
    (functions.pushnumber.unwrap())(lua, value);
    (functions.setfield.unwrap())(lua, -2, name.as_ptr() as *const c_char);
}

unsafe extern "C-unwind" fn walltime(lua: *mut LuaState) -> c_int {
    let Some(seconds) = qpc_seconds() else {
        return failure(lua, "performance counter unavailable");
    };
    (api().pushnumber.unwrap())(lua, seconds);
    1
}

unsafe extern "C-unwind" fn hash(lua: *mut LuaState) -> c_int {
    let functions = api();
    (functions.checktype.unwrap())(lua, 1, 5);
    let length = (functions.objlen.unwrap())(lua, 1);
    if length > c_int::MAX as usize {
        return failure(lua, "too many hash rows");
    }
    let mut hash = Fnv::new();
    for index in 1..=length as c_int {
        (functions.rawgeti.unwrap())(lua, 1, index);
        let mut text_length = 0usize;
        let text = (functions.checklstring.unwrap())(lua, -1, &mut text_length);
        hash.update(slice::from_raw_parts(text as *const u8, text_length));
        hash.update(b"\n");
        (functions.settop.unwrap())(lua, -2);
    }
    let digest = hex64(hash.0);
    (functions.pushlstring.unwrap())(lua, digest.as_ptr() as *const c_char, digest.len());
    1
}

unsafe extern "C-unwind" fn inspect(lua: *mut LuaState) -> c_int {
    let functions = api();
    let mut length = 0usize;
    let text = (functions.checklstring.unwrap())(lua, 1, &mut length);
    if CURRENT.is_null()
        || !(*CURRENT).armed
        || GetCurrentThreadId() != OWNER_THREAD
        || !context_valid()
    {
        return failure(lua, "observer unavailable on this thread");
    }
    let Some(id) = parse_id(slice::from_raw_parts(text as *const u8, length)).filter(|id| *id != 0)
    else {
        return failure(lua, "invalid component id");
    };
    let registry = ptr::read((IMAGE + REGISTRY_RVA) as *const *mut c_void);
    if !readable(registry as *const c_void, 0x158, false) {
        return failure(lua, "component registry unavailable");
    }
    let find: unsafe extern "C" fn(*mut c_void, u64, u32) -> *mut u8 =
        core::mem::transmute(IMAGE + REGISTRY_FIND_RVA);
    let object = find(registry, id, 4);
    if !readable(object as *const c_void, 0x350, false) {
        return failure(lua, "component unavailable");
    }
    let shield: unsafe extern "C" fn(*mut u8) -> f64 =
        core::mem::transmute(IMAGE + SHIELD_PERCENT_RVA);
    let owner = ptr::read(object.add(0x348) as *const usize);
    let owner_hex = hex64(owner as u64);
    (functions.createtable.unwrap())(lua, 0, 6);
    field_number(
        lua,
        b"attention\0",
        ptr::read(object.add(0x1ec) as *const c_int) as f64,
    );
    field_number(lua, b"hull\0", ptr::read(object.add(0x280) as *const f64));
    field_number(lua, b"shield\0", shield(object));
    field_number(lua, b"generation\0", ((id >> 25) & 0xffff) as f64);
    (functions.pushlstring.unwrap())(lua, owner_hex.as_ptr() as *const c_char, 16);
    (functions.setfield.unwrap())(lua, -2, b"owner_pointer\0".as_ptr() as *const c_char);
    (functions.pushboolean.unwrap())(lua, 1);
    (functions.setfield.unwrap())(lua, -2, b"registry\0".as_ptr() as *const c_char);
    1
}

unsafe fn resolve_lua() -> bool {
    let name: Vec<u16> = "lua51_64.dll\0".encode_utf16().collect();
    let runtime = GetModuleHandleW(name.as_ptr());
    if runtime.is_null() {
        return false;
    }
    let mut functions = LuaApi::default();
    macro_rules! resolve {
        ($field:ident, $name:literal) => {{
            let address = GetProcAddress(runtime, concat!($name, "\0").as_ptr());
            if address.is_null() {
                return false;
            }
            functions.$field = Some(core::mem::transmute(address));
        }};
    }
    resolve!(gettop, "lua_gettop");
    resolve!(settop, "lua_settop");
    resolve!(pushthread, "lua_pushthread");
    resolve!(pushvalue, "lua_pushvalue");
    resolve!(pushboolean, "lua_pushboolean");
    resolve!(pushnumber, "lua_pushnumber");
    resolve!(pushlstring, "lua_pushlstring");
    resolve!(pushcclosure, "lua_pushcclosure");
    resolve!(getfield, "lua_getfield");
    resolve!(setfield, "lua_setfield");
    resolve!(createtable, "lua_createtable");
    resolve!(newuserdata, "lua_newuserdata");
    resolve!(setmetatable, "lua_setmetatable");
    resolve!(newmetatable, "luaL_newmetatable");
    resolve!(checkudata, "luaL_checkudata");
    resolve!(checklstring, "luaL_checklstring");
    resolve!(checknumber, "luaL_checknumber");
    resolve!(checktype, "luaL_checktype");
    resolve!(reference, "luaL_ref");
    resolve!(unref, "luaL_unref");
    resolve!(rawgeti, "lua_rawgeti");
    resolve!(objlen, "lua_objlen");
    resolve!(pcall, "lua_pcall");
    API = functions;
    true
}

unsafe fn method(lua: *mut LuaState, name: &[u8], function: LuaFunction) {
    let functions = api();
    (functions.pushcclosure.unwrap())(lua, function, 0);
    (functions.setfield.unwrap())(lua, -2, name.as_ptr() as *const c_char);
}

#[no_mangle]
pub unsafe extern "C-unwind" fn luaopen_x4lan_hooks(lua: *mut LuaState) -> c_int {
    if !resolve_lua() {
        return 0;
    }
    VTABLE = [
        no_op as *const c_void,
        move_observer as *const c_void,
        no_op as *const c_void,
    ];
    HOOK.vtable = addr_of!(VTABLE) as *const *const c_void;
    let functions = api();
    (functions.newmetatable.unwrap())(lua, META.as_ptr() as *const c_char);
    (functions.pushvalue.unwrap())(lua, -1);
    (functions.setfield.unwrap())(lua, -2, b"__index\0".as_ptr() as *const c_char);
    method(lua, b"__gc\0", Some(stop));
    method(lua, b"stop\0", Some(stop));
    method(lua, b"active\0", Some(active));
    method(lua, b"frames\0", Some(frames));
    (functions.settop.unwrap())(lua, -2);
    (functions.createtable.unwrap())(lua, 0, 8);
    method(lua, b"start\0", Some(start));
    method(lua, b"inspect\0", Some(inspect));
    method(lua, b"walltime\0", Some(walltime));
    method(lua, b"hash\0", Some(hash));
    method(lua, b"set_remote_attention\0", Some(set_remote_attention));
    method(lua, b"timing_start\0", Some(timing::lua_start));
    method(lua, b"timing_stop\0", Some(timing::lua_stop));
    method(lua, b"timing_stats\0", Some(timing::lua_stats));
    1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[repr(C)]
    struct FakeLua {
        top: c_int,
    }
    static mut FAKE_LUA: FakeLua = FakeLua { top: 0 };
    static mut CALLS: c_int = 0;
    static mut UNREFS: c_int = 0;
    static mut NUMBER_COUNT: usize = 0;
    static mut NUMBERS: [f64; 32] = [0.0; 32];
    static mut RNG: [u8; 16] = [0; 16];
    static mut FAIL_CALL: bool = false;
    static mut STOP_IN_CALL: bool = false;

    unsafe extern "C-unwind" fn mock_gettop(lua: *mut LuaState) -> c_int {
        (*(lua as *mut FakeLua)).top
    }
    unsafe extern "C-unwind" fn mock_settop(lua: *mut LuaState, top: c_int) {
        (*(lua as *mut FakeLua)).top = top;
    }
    unsafe extern "C-unwind" fn mock_pushnumber(lua: *mut LuaState, value: f64) {
        NUMBERS[NUMBER_COUNT] = value;
        NUMBER_COUNT += 1;
        (*(lua as *mut FakeLua)).top += 1;
    }
    unsafe extern "C-unwind" fn mock_pushlstring(
        _: *mut LuaState,
        text: *const c_char,
        length: usize,
    ) {
        if length == 16 && ptr::read(addr_of!(RNG)) == [0; 16] {
            ptr::copy_nonoverlapping(text as *const u8, addr_of_mut!(RNG) as *mut u8, 16);
        }
    }
    unsafe extern "C-unwind" fn mock_rawgeti(lua: *mut LuaState, index: c_int, reference: c_int) {
        assert_eq!((index, reference), (REGISTRYINDEX, 123));
        (*(lua as *mut FakeLua)).top += 1;
    }
    unsafe extern "C-unwind" fn mock_checkudata(
        _: *mut LuaState,
        index: c_int,
        name: *const c_char,
    ) -> *mut c_void {
        assert_eq!(index, 1);
        assert_eq!(slice::from_raw_parts(name as *const u8, META.len()), META);
        CURRENT as *mut c_void
    }
    unsafe extern "C-unwind" fn mock_unref(_: *mut LuaState, index: c_int, reference: c_int) {
        assert_eq!((index, reference), (REGISTRYINDEX, 123));
        UNREFS += 1;
    }
    unsafe extern "C-unwind" fn mock_pcall(
        lua: *mut LuaState,
        args: c_int,
        results: c_int,
        error: c_int,
    ) -> c_int {
        assert_eq!((args, results, error), (5, 0, 0));
        CALLS += 1;
        (*(lua as *mut FakeLua)).top = 99;
        if STOP_IN_CALL {
            stop(lua);
        }
        FAIL_CALL as c_int
    }

    unsafe fn install_mock_api() {
        API = LuaApi::default();
        API.gettop = Some(mock_gettop);
        API.settop = Some(mock_settop);
        API.pushnumber = Some(mock_pushnumber);
        API.pushlstring = Some(mock_pushlstring);
        API.rawgeti = Some(mock_rawgeti);
        API.pcall = Some(mock_pcall);
        API.checkudata = Some(mock_checkudata);
        API.unref = Some(mock_unref);
    }

    #[test]
    fn primitive_validation_and_hashing() {
        assert_eq!(parse_id(b"18446744073709551615"), Some(u64::MAX));
        assert_eq!(parse_id(b"18446744073709551616"), None);
        assert_eq!(parse_id(b"1x"), None);
        assert_eq!(parse_id(b""), None);
        assert_eq!(hex64(0x0123456789abcdef), *b"0123456789abcdef");
        assert!(digest_matches(&[0xab; 32], &"ab".repeat(32)));
        assert!(!digest_matches(&[0xab; 32], &"ac".repeat(32)));
        let mut empty = Fnv::new();
        assert_eq!(hex64(empty.0), *b"cbf29ce484222325");
        empty.update(b"hello\n");
        assert_eq!(hex64(empty.0), *b"a9bc80cca21f28b3");
        let first = qpc_seconds().expect("QPC available");
        let second = qpc_seconds().expect("QPC available");
        assert!(first.is_finite() && second >= first);
        assert_eq!(role_seed(42, b"MoveWorker"), role_seed(42, b"MoveWorker"));
        assert_ne!(
            role_seed(42, b"MoveWorker"),
            role_seed(42, b"RegionManager")
        );
        assert_ne!(role_seed(42, b"MoveWorker"), role_seed(43, b"MoveWorker"));
    }

    #[test]
    fn synthetic_attention_union_and_wrong_build() {
        unsafe {
            IMAGE = 0;
            ATTENTION_ENABLED.store(false, Ordering::Release);
            REMOTE_PROXY.store(0, Ordering::Release);
            ATTENTION_INSTALLED = [false; 9];
            assert!(!verify_image());
            assert!(remote_proxy().is_null());
            assert!(!ATTENTION_ENABLED.load(Ordering::Acquire));
            assert_eq!(REMOTE_PROXY.load(Ordering::Acquire), 0);
        }
        for base in 1..=11 {
            assert_eq!(union_zone_attention(base, false), base);
            assert_eq!(union_ship_attention(base, false), base);
            assert_eq!(union_zone_attention(base, true), base.max(8));
            assert_eq!(union_ship_attention(base, true), base.max(9));
        }
    }

    #[test]
    fn synthetic_attention_vtable_lifecycle() {
        unsafe {
            ATTENTION_ENABLED.store(false, Ordering::Release);
            REMOTE_PROXY.store(0, Ordering::Release);
            ATTENTION_INSTALLED = [false; 9];
            let image = Box::leak(vec![0u8; IMAGE_SIZE].into_boxed_slice());
            IMAGE = image.as_mut_ptr() as usize;
            for rva in ZONE_ATTENTION_SLOTS {
                ptr::write(
                    (IMAGE + rva) as *mut *mut c_void,
                    (IMAGE + ZONE_COMPUTE_RVA) as *mut c_void,
                );
            }
            for rva in SHIP_ATTENTION_SLOTS {
                ptr::write(
                    (IMAGE + rva) as *mut *mut c_void,
                    (IMAGE + SHIP_COMPUTE_RVA) as *mut c_void,
                );
            }
            assert!(install_attention());
            assert!(ATTENTION_ENABLED.load(Ordering::Acquire));
            assert_eq!(
                ptr::read((IMAGE + ZONE_ATTENTION_SLOTS[0]) as *const *const c_void),
                zone_attention as *const c_void
            );
            assert_eq!(
                ptr::read((IMAGE + SHIP_ATTENTION_SLOTS[0]) as *const *const c_void),
                ship_attention as *const c_void
            );
            REMOTE_PROXY.store(77, Ordering::Release);
            uninstall_attention();
            assert!(!ATTENTION_ENABLED.load(Ordering::Acquire));
            assert_eq!(REMOTE_PROXY.load(Ordering::Acquire), 0);
            for rva in ZONE_ATTENTION_SLOTS {
                assert_eq!(
                    ptr::read((IMAGE + rva) as *const *const c_void),
                    (IMAGE + ZONE_COMPUTE_RVA) as *const c_void
                );
            }
            for rva in SHIP_ATTENTION_SLOTS {
                assert_eq!(
                    ptr::read((IMAGE + rva) as *const *const c_void),
                    (IMAGE + SHIP_COMPUTE_RVA) as *const c_void
                );
            }

            assert!(install_attention());
            let foreign = 0x1234usize as *mut c_void;
            assert!(swap_attention_slot(
                ZONE_ATTENTION_SLOTS[0],
                zone_attention as *mut c_void,
                foreign
            ));
            uninstall_attention();
            assert_eq!(
                ptr::read((IMAGE + ZONE_ATTENTION_SLOTS[0]) as *const *const c_void),
                foreign
            );
            assert!(swap_attention_slot(
                ZONE_ATTENTION_SLOTS[0],
                foreign,
                (IMAGE + ZONE_COMPUTE_RVA) as *mut c_void
            ));
            ATTENTION_INSTALLED[0] = false;

            let blocked = SHIP_ATTENTION_SLOTS[0];
            assert!(swap_attention_slot(
                blocked,
                (IMAGE + SHIP_COMPUTE_RVA) as *mut c_void,
                foreign
            ));
            assert!(!install_attention());
            assert!(!ATTENTION_ENABLED.load(Ordering::Acquire));
            for rva in ZONE_ATTENTION_SLOTS {
                assert_eq!(
                    ptr::read((IMAGE + rva) as *const *const c_void),
                    (IMAGE + ZONE_COMPUTE_RVA) as *const c_void
                );
            }
            assert_eq!(
                ptr::read((IMAGE + blocked) as *const *const c_void),
                foreign
            );
            assert!(swap_attention_slot(
                blocked,
                foreign,
                (IMAGE + SHIP_COMPUTE_RVA) as *mut c_void
            ));
            ATTENTION_INSTALLED = [false; 9];
        }
    }

    #[test]
    fn synthetic_observer_lifecycle() {
        unsafe {
            assert!(!verify_image());
            let image = Box::leak(vec![0u8; IMAGE_SIZE].into_boxed_slice());
            IMAGE = image.as_mut_ptr() as usize;
            VTABLE = [
                no_op as *const c_void,
                move_observer as *const c_void,
                no_op as *const c_void,
            ];
            HOOK.vtable = addr_of!(VTABLE) as *const *const c_void;
            let mut table = vec![ptr::null_mut(); 40];
            let mut ui = vec![0u8; 256];
            let mut anark = vec![0u8; 0x400];
            let mut tls = vec![0u8; 0x330];
            let mut owner = Owner {
                frames: 0,
                armed: true,
            };
            InitializeCriticalSection(ui.as_mut_ptr().add(0x70) as *mut c_void);
            let slots = {
                let value: *mut *mut c_void;
                core::arch::asm!("mov {}, gs:[0x58]", out(reg) value, options(nostack, preserves_flags, readonly));
                value
            };
            let saved_tls = ptr::read(slots);
            ptr::write(slots, tls.as_mut_ptr() as *mut c_void);
            ptr::write((IMAGE + GAME_TIME_RVA) as *mut f64, 100.5);
            ptr::write(tls.as_mut_ptr().add(0x328) as *mut f64, 100.25);
            ptr::write(tls.as_mut_ptr().add(0x318) as *mut u64, 0x0123456789abcdef);
            ptr::write(
                ui.as_mut_ptr() as *mut *const c_void,
                (IMAGE + UI_VTABLE_RVA) as *const c_void,
            );
            ptr::write((IMAGE + ANARK_RVA) as *mut *mut u8, anark.as_mut_ptr());
            ptr::write(
                (IMAGE + VBI_RVA) as *mut *mut *mut c_void,
                table.as_mut_ptr(),
            );
            table[3] = addr_of_mut!(owner) as *mut c_void;
            table[6] = ui.as_mut_ptr() as *mut c_void;
            table[SLOT] = addr_of_mut!(HOOK) as *mut c_void;
            REGISTERED_TABLE = table.as_mut_ptr();
            REGISTERED_UI = ui.as_mut_ptr();
            REGISTERED_ANARK = anark.as_mut_ptr();
            CURRENT = addr_of_mut!(owner);
            STATE = addr_of_mut!(FAKE_LUA) as *mut LuaState;
            CALLBACK_REF = 123;
            OWNER_THREAD = GetCurrentThreadId();
            INSIDE_CALLBACK = false;
            TICK = 0;
            CADENCE = 1;
            anark[0x3f0] = 1;
            FAKE_LUA.top = 4;
            install_mock_api();

            move_observer(addr_of_mut!(HOOK));
            assert_eq!((CALLS, owner.frames, FAKE_LUA.top), (1, 1, 4));
            assert_eq!(ptr::read(addr_of!(NUMBERS))[..3], [1.0, 100.5, 0.25]);
            assert_eq!(ptr::read(addr_of!(RNG)), *b"0123456789abcdef");

            TICK = 0;
            CADENCE = 5;
            CALLS = 0;
            NUMBER_COUNT = 0;
            owner.frames = 0;
            move_observer(addr_of_mut!(HOOK));
            assert_eq!((CALLS, TICK), (0, 1));
            for _ in 0..4 {
                move_observer(addr_of_mut!(HOOK));
            }
            assert_eq!((CALLS, TICK, NUMBERS[0]), (1, 5, 5.0));

            TICK = 0;
            CADENCE = 1;
            CALLS = 0;
            ptr::write((REGISTERED_TABLE as *mut u8).add(0x130) as *mut c_int, 1);
            move_observer(addr_of_mut!(HOOK));
            assert_eq!((CALLS, TICK, owner.armed), (0, 0, true));
            ptr::write((REGISTERED_TABLE as *mut u8).add(0x130) as *mut c_int, 0);

            OWNER_THREAD += 1;
            move_observer(addr_of_mut!(HOOK));
            assert!(!owner.armed && table[SLOT].is_null());

            owner.armed = true;
            table[SLOT] = addr_of_mut!(HOOK) as *mut c_void;
            OWNER_THREAD = GetCurrentThreadId();
            FAIL_CALL = true;
            move_observer(addr_of_mut!(HOOK));
            assert!(!owner.armed && owner.frames == 1 && FAKE_LUA.top == 4);

            owner.armed = true;
            table[SLOT] = addr_of_mut!(HOOK) as *mut c_void;
            FAIL_CALL = false;
            STOP_IN_CALL = true;
            CURRENT = addr_of_mut!(owner);
            move_observer(addr_of_mut!(HOOK));
            assert!(CURRENT.is_null() && STATE.is_null() && table[SLOT].is_null() && UNREFS == 1);

            ptr::write(slots, saved_tls);
            DeleteCriticalSection(ui.as_mut_ptr().add(0x70) as *mut c_void);
        }
    }
}
