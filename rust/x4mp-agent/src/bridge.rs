//! C entry points for the thin Lua bridge of the MD action queue (Linux).
//!
//! The agent is `LD_PRELOAD`ed, so these symbols are in the process's global scope and
//! the game's LuaJIT finds them through `ffi.C` after a matching `ffi.cdef`
//! (`rust/x4mp-core/executor/x4mp_core_bridge.lua`). Both are called from the UI
//! `onUpdate` on the MoveWorker thread; the core refuses any other thread. They never
//! unwind into LuaJIT and never block.
//!
//! ```c
//! intptr_t x4mp_core_md_take(uint8_t* buf, size_t cap);   // bytes, 0 = none, <0 refused
//! intptr_t x4mp_core_md_result(uint64_t id, int status);  // 0 ok, 1 unknown id, <0 refused
//! ```

use std::panic::catch_unwind;

use x4mp_core::runtime;

/// # Safety
/// `buf` is null or points to `cap` writable bytes owned by the caller (LuaJIT).
#[no_mangle]
pub unsafe extern "C" fn x4mp_core_md_take(buf: *mut u8, cap: usize) -> isize {
    if buf.is_null() || cap == 0 || cap > 1 << 20 {
        return runtime::BRIDGE_BAD_ARGUMENT;
    }
    catch_unwind(|| {
        // SAFETY: per the function contract, `cap` writable bytes.
        let slice = unsafe { std::slice::from_raw_parts_mut(buf, cap) };
        runtime::md_take(slice)
    })
    .unwrap_or(runtime::BRIDGE_BAD_ARGUMENT)
}

#[no_mangle]
pub extern "C" fn x4mp_core_md_result(id: u64, status: i32) -> isize {
    catch_unwind(|| runtime::md_result(id, status)).unwrap_or(runtime::BRIDGE_BAD_ARGUMENT)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuses_bad_buffers_and_is_inert_without_core() {
        let mut buf = [0u8; 16];
        unsafe {
            assert_eq!(x4mp_core_md_take(std::ptr::null_mut(), 16), runtime::BRIDGE_BAD_ARGUMENT);
            assert_eq!(x4mp_core_md_take(buf.as_mut_ptr(), 0), runtime::BRIDGE_BAD_ARGUMENT);
            assert_eq!(x4mp_core_md_take(buf.as_mut_ptr(), buf.len()), runtime::BRIDGE_DISABLED);
        }
        assert_eq!(x4mp_core_md_result(1, 0), runtime::BRIDGE_DISABLED);
    }
}
