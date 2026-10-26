//! Data-only hook of one vtable entry: verified original, atomic install and removal.
//!
//! The entry (8 bytes, 8-byte aligned, usually in a read-only data section) must hold the
//! expected original function address. Install makes the page writable for the duration
//! of one `lock cmpxchg` (original -> wrapper) and restores the protection; removal swaps
//! the wrapper back the same way and fails, leaving the entry alone, if anything else has
//! replaced it since (a later hook chained on top). The wrapper code must stay loaded for
//! good: a thread may have read the entry just before the removal.

use std::ffi::c_void;
use std::sync::atomic::{AtomicUsize, Ordering};

#[link(name = "kernel32")]
extern "system" {
    fn VirtualProtect(address: *mut c_void, size: usize, protect: u32, old: *mut u32) -> i32;
}

const PAGE_READWRITE: u32 = 0x04;

#[derive(Debug)]
pub struct EntryHook {
    entry: *mut usize,
    original: usize,
    wrapper: usize,
    installed: bool,
}

unsafe impl Send for EntryHook {}

/// Compare-exchange one entry inside a temporarily writable page.
///
/// # Safety
/// `entry` is a valid, 8-byte aligned pointer slot of this process.
unsafe fn exchange(entry: *mut usize, expected: usize, new: usize) -> Result<bool, &'static str> {
    let mut old = 0u32;
    if VirtualProtect(entry as *mut c_void, 8, PAGE_READWRITE, &mut old) == 0 {
        return Err("VirtualProtect failed");
    }
    let swapped =
        (*(entry as *const AtomicUsize)).compare_exchange(expected, new, Ordering::SeqCst, Ordering::SeqCst).is_ok();
    let mut ignored = 0u32;
    if old != PAGE_READWRITE && VirtualProtect(entry as *mut c_void, 8, old, &mut ignored) == 0 {
        return Err("VirtualProtect restore failed");
    }
    Ok(swapped)
}

impl EntryHook {
    /// Check the entry; nothing is written yet.
    ///
    /// # Safety
    /// `entry` must point into this process's mapped image (a vtable entry).
    pub unsafe fn prepare(entry: *mut usize, original: usize, wrapper: usize) -> Result<EntryHook, &'static str> {
        if entry as usize % 8 != 0 {
            return Err("vtable entry is not 8-byte aligned");
        }
        if (*(entry as *const AtomicUsize)).load(Ordering::SeqCst) != original {
            return Err("vtable entry differs from the pinned build");
        }
        if wrapper == 0 || wrapper == original {
            return Err("invalid wrapper");
        }
        Ok(EntryHook { entry, original, wrapper, installed: false })
    }

    pub fn installed(&self) -> bool {
        self.installed
    }

    /// The entry holds this hook's wrapper (false after removal or when replaced).
    pub fn intact(&self) -> bool {
        // SAFETY: verified slot.
        unsafe { (*(self.entry as *const AtomicUsize)).load(Ordering::SeqCst) == self.wrapper }
    }

    /// original -> wrapper.
    ///
    /// # Safety
    /// The wrapper must be callable with the original's ABI and stay loaded.
    pub unsafe fn install(&mut self) -> Result<(), &'static str> {
        if self.installed {
            return Err("already installed");
        }
        if !exchange(self.entry, self.original, self.wrapper)? {
            return Err("vtable entry changed before install");
        }
        self.installed = true;
        Ok(())
    }

    /// wrapper -> original; refuses (entry untouched) if the wrapper is no longer there.
    ///
    /// # Safety
    /// Called once the hook was installed by [`EntryHook::install`].
    pub unsafe fn remove(&mut self) -> Result<(), &'static str> {
        if !self.installed {
            return Ok(());
        }
        if !exchange(self.entry, self.wrapper, self.original)? {
            return Err("vtable entry was replaced by someone else");
        }
        self.installed = false;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[link(name = "kernel32")]
    extern "system" {
        fn VirtualAlloc(address: *mut c_void, size: usize, allocation: u32, protect: u32) -> *mut c_void;
    }

    #[test]
    fn installs_removes_and_refuses_changes_on_a_read_only_page() {
        // A private page (never freed), read-only like a vtable in .rdata.
        let page = unsafe { VirtualAlloc(std::ptr::null_mut(), 4096, 0x3000, PAGE_READWRITE) } as *mut usize;
        assert!(!page.is_null());
        let entry = unsafe { page.add(7) };
        unsafe { entry.write(0x1111) };
        let mut old = 0;
        assert_ne!(unsafe { VirtualProtect(page as *mut c_void, 4096, 0x02, &mut old) }, 0);
        unsafe {
            assert!(EntryHook::prepare(entry, 0x2222, 0x3333).is_err(), "wrong original refused");
            assert!(EntryHook::prepare((entry as usize + 4) as *mut usize, 0x1111, 0x3333).is_err());
            let mut hook = EntryHook::prepare(entry, 0x1111, 0x3333).unwrap();
            hook.install().unwrap();
            assert_eq!(entry.read_volatile(), 0x3333);
            assert!(hook.intact());
            hook.remove().unwrap();
            assert_eq!(entry.read_volatile(), 0x1111);
            assert!(!hook.intact());
            hook.install().unwrap();
            // someone chains on top: removal must refuse and leave their value
            assert!(exchange(entry, 0x3333, 0x4444).unwrap());
            assert!(hook.remove().is_err());
            assert_eq!(entry.read_volatile(), 0x4444);
            // the page is read-only again after every exchange
            let mut previous = 0;
            assert_ne!(VirtualProtect(entry as *mut c_void, 8, 0x02, &mut previous), 0);
            assert_eq!(previous, 0x02);
        }
    }
}
