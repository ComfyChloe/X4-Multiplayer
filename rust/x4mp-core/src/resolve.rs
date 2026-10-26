//! Runtime lookup of X4's exported C functions.
//!
//! - Linux: `dlsym(RTLD_DEFAULT, name)`. The main executable is first in the global
//!   scope, and it is `ET_EXEC`, so the result is the absolute pinned address.
//! - Windows: `GetProcAddress(GetModuleHandleW(NULL), name)`, only on the main module
//!   (`X4.exe`), never on another DLL. Pins are RVAs from that module's base.
//!
//! A [`SymbolSource`] also reads the first code bytes of a resolved function so the
//! binding layer can compare them with the pinned head hash.

use std::ffi::CStr;

use crate::pins::Platform;

pub trait SymbolSource {
    fn platform(&self) -> Platform;
    /// Base the pinned offsets are relative to (0 for the Linux ET_EXEC image).
    fn image_base(&self) -> usize;
    /// Address of the export, or `None` when the symbol does not exist.
    fn lookup(&self, name: &CStr) -> Option<usize>;
    /// `len` bytes of code at `address`, or `None` when that range is not readable.
    fn read(&self, address: usize, len: usize) -> Option<Vec<u8>>;
}

/// Readable `[start, end)` ranges of this process from `/proc/self/maps` text.
pub fn parse_maps(maps: &str) -> Vec<(usize, usize)> {
    maps.lines()
        .filter_map(|line| {
            let mut parts = line.split_whitespace();
            let range = parts.next()?;
            if !parts.next()?.starts_with('r') {
                return None;
            }
            let (a, b) = range.split_once('-')?;
            Some((usize::from_str_radix(a, 16).ok()?, usize::from_str_radix(b, 16).ok()?))
        })
        .collect()
}

pub fn covered(ranges: &[(usize, usize)], address: usize, len: usize) -> bool {
    let Some(end) = address.checked_add(len) else { return false };
    ranges.iter().any(|&(a, b)| a <= address && end <= b)
}

#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
pub use linux::Dlsym;

#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
mod linux {
    use super::*;
    use std::ffi::{c_char, c_void};

    #[link(name = "dl")]
    extern "C" {
        fn dlsym(handle: *mut c_void, name: *const c_char) -> *mut c_void;
    }

    /// The live process. Readable ranges are captured once at construction; binding
    /// happens before the frame callback is armed, so they are current.
    pub struct Dlsym {
        ranges: Vec<(usize, usize)>,
    }

    impl Dlsym {
        pub fn new() -> Self {
            let maps = std::fs::read_to_string("/proc/self/maps").unwrap_or_default();
            Dlsym { ranges: parse_maps(&maps) }
        }
    }

    impl Default for Dlsym {
        fn default() -> Self {
            Self::new()
        }
    }

    impl SymbolSource for Dlsym {
        fn platform(&self) -> Platform {
            Platform::Linux
        }
        fn image_base(&self) -> usize {
            0
        }
        fn lookup(&self, name: &CStr) -> Option<usize> {
            // SAFETY: RTLD_DEFAULT (null on glibc) and a NUL-terminated name.
            let address = unsafe { dlsym(std::ptr::null_mut(), name.as_ptr()) } as usize;
            (address != 0).then_some(address)
        }
        fn read(&self, address: usize, len: usize) -> Option<Vec<u8>> {
            if !covered(&self.ranges, address, len) {
                return None;
            }
            // SAFETY: the whole range is mapped readable in this process.
            Some(unsafe { std::slice::from_raw_parts(address as *const u8, len) }.to_vec())
        }
    }
}

#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
pub use windows::MainModule;

#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
mod windows {
    use super::*;
    use std::ffi::{c_char, c_void};

    #[repr(C)]
    struct MemoryBasicInformation {
        base_address: *mut c_void,
        allocation_base: *mut c_void,
        allocation_protect: u32,
        partition_id: u16,
        region_size: usize,
        state: u32,
        protect: u32,
        kind: u32,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetModuleHandleW(name: *const u16) -> *mut c_void;
        fn GetProcAddress(module: *mut c_void, name: *const c_char) -> *mut c_void;
        fn VirtualQuery(address: *const c_void, info: *mut MemoryBasicInformation, length: usize) -> usize;
    }

    const MEM_COMMIT: u32 = 0x1000;
    const READABLE: u32 = 0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80;
    const GUARD_OR_NOACCESS: u32 = 0x100 | 0x01;

    /// `X4.exe`, the main module of this process.
    pub struct MainModule {
        base: usize,
    }

    impl MainModule {
        pub fn new() -> Option<Self> {
            // SAFETY: null asks for the module that created the process.
            let base = unsafe { GetModuleHandleW(std::ptr::null()) } as usize;
            (base != 0).then_some(MainModule { base })
        }
    }

    fn readable(address: usize, len: usize) -> bool {
        let mut at = address;
        let Some(end) = address.checked_add(len) else { return false };
        while at < end {
            // SAFETY: plain query of this process's address space.
            let mut info: MemoryBasicInformation = unsafe { std::mem::zeroed() };
            let size = std::mem::size_of::<MemoryBasicInformation>();
            if unsafe { VirtualQuery(at as *const c_void, &mut info, size) } != size
                || info.state != MEM_COMMIT
                || info.protect & GUARD_OR_NOACCESS != 0
                || info.protect & READABLE == 0
            {
                return false;
            }
            let region_end = (info.base_address as usize).saturating_add(info.region_size);
            if region_end <= at {
                return false;
            }
            at = region_end;
        }
        true
    }

    impl SymbolSource for MainModule {
        fn platform(&self) -> Platform {
            Platform::Windows
        }
        fn image_base(&self) -> usize {
            self.base
        }
        fn lookup(&self, name: &CStr) -> Option<usize> {
            // SAFETY: valid module handle and NUL-terminated name.
            let address = unsafe { GetProcAddress(self.base as *mut c_void, name.as_ptr()) } as usize;
            (address != 0).then_some(address)
        }
        fn read(&self, address: usize, len: usize) -> Option<Vec<u8>> {
            if !readable(address, len) {
                return None;
            }
            // SAFETY: committed, readable, non-guard pages.
            Some(unsafe { std::slice::from_raw_parts(address as *const u8, len) }.to_vec())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_parsing_and_coverage() {
        let maps = "00400000-00500000 r-xp 00000000 08:01 1 /x4\n00500000-00600000 ---p 0 0 0\n7f00-8000 rw-p 0 0 0\n";
        let ranges = parse_maps(maps);
        assert_eq!(ranges, vec![(0x400000, 0x500000), (0x7f00, 0x8000)]);
        assert!(covered(&ranges, 0x400000, 0x100000));
        assert!(!covered(&ranges, 0x4ffff0, 0x20));
        assert!(!covered(&ranges, usize::MAX - 2, 8));
    }

    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    #[test]
    fn dlsym_resolves_libc_and_reads_own_code() {
        let source = Dlsym::new();
        assert!(source.lookup(c"strlen").is_some());
        assert!(source.lookup(c"X4mpCoreDefinitelyMissing").is_none());
        let here = maps_parsing_and_coverage as *const () as usize;
        assert_eq!(source.read(here, 16).map(|b| b.len()), Some(16));
    }

    #[cfg(all(target_os = "windows", target_arch = "x86_64"))]
    #[test]
    fn main_module_lookup_and_guarded_reads() {
        let source = MainModule::new().unwrap();
        assert_ne!(source.image_base(), 0);
        // The test executable exports nothing named like X4's API.
        assert!(source.lookup(c"GetPlayerOccupiedShipID").is_none());
        let here = maps_parsing_and_coverage as *const () as usize;
        assert_eq!(source.read(here, 16).map(|b| b.len()), Some(16));
        assert!(source.read(0x10, 16).is_none(), "null page must be refused");
    }
}
