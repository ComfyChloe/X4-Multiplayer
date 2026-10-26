//! A 16-byte entry detour: build-pinned, byte-verified, atomic, reversible.
//!
//! The site must start with the exact pinned 16 bytes, be 16-byte aligned and begin with
//! position-independent instructions covering at least the first 15 bytes (the caller
//! states where the first instruction boundary at or after byte 14 is: `relocated`).
//! Install writes, in one `lock cmpxchg16b`, an absolute indirect jump
//! (`jmp [rip+0]` + 8-byte address, 14 bytes) to the detour, `int3` padding up to
//! `relocated`, and keeps the remaining original bytes. The trampoline holds the first
//! `relocated` original bytes followed by an absolute jump back to `site + relocated`.
//! Removal swaps the original 16 bytes back with the same compare-exchange (it fails if
//! anything else changed them) and re-reads them. The trampoline is never freed: a
//! thread may still be inside it.

use std::ffi::c_void;

#[link(name = "kernel32")]
extern "system" {
    fn VirtualAlloc(address: *mut c_void, size: usize, allocation: u32, protect: u32) -> *mut c_void;
    fn VirtualProtect(address: *mut c_void, size: usize, protect: u32, old: *mut u32) -> i32;
    fn FlushInstructionCache(process: *mut c_void, base: *const c_void, size: usize) -> i32;
    fn GetCurrentProcess() -> *mut c_void;
}

const MEM_COMMIT: u32 = 0x1000;
const MEM_RESERVE: u32 = 0x2000;
const PAGE_READWRITE: u32 = 0x04;
const PAGE_EXECUTE_READ: u32 = 0x20;
const PAGE_EXECUTE_READWRITE: u32 = 0x40;
const JUMP: [u8; 6] = [0xff, 0x25, 0, 0, 0, 0];

#[derive(Debug)]
pub struct Patch {
    site: *mut u8,
    original: [u8; 16],
    patched: [u8; 16],
    trampoline: *const u8,
    installed: bool,
}

unsafe impl Send for Patch {}

#[target_feature(enable = "cmpxchg16b")]
unsafe fn swap16(site: *mut u8, expected: [u8; 16], new: [u8; 16]) -> bool {
    let old = u128::from_le_bytes(expected);
    let seen = core::arch::x86_64::cmpxchg16b(
        site as *mut u128,
        old,
        u128::from_le_bytes(new),
        std::sync::atomic::Ordering::SeqCst,
        std::sync::atomic::Ordering::SeqCst,
    );
    seen == old
}

/// Read 16 bytes of code.
///
/// # Safety
/// `site` must be readable for 16 bytes.
pub unsafe fn read16(site: *const u8) -> [u8; 16] {
    let mut out = [0u8; 16];
    std::ptr::copy_nonoverlapping(site, out.as_mut_ptr(), 16);
    out
}

impl Patch {
    /// Validate the site and build the trampoline; nothing is written to the site yet.
    ///
    /// # Safety
    /// `site` must be executable code of this process whose first `relocated` bytes are
    /// complete, position-independent instructions.
    pub unsafe fn prepare(site: *mut u8, expected: [u8; 16], relocated: usize) -> Result<Patch, &'static str> {
        if !(14..=16).contains(&relocated) {
            return Err("relocated length out of range");
        }
        if site as usize % 16 != 0 {
            return Err("site is not 16-byte aligned");
        }
        let original = read16(site);
        if original != expected {
            return Err("site bytes differ from the pinned build");
        }
        let trampoline = VirtualAlloc(std::ptr::null_mut(), 64, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE) as *mut u8;
        if trampoline.is_null() {
            return Err("trampoline allocation failed");
        }
        let back = (site as u64 + relocated as u64).to_le_bytes();
        let mut code = [0u8; 64];
        code[..relocated].copy_from_slice(&original[..relocated]);
        code[relocated..relocated + 6].copy_from_slice(&JUMP);
        code[relocated + 6..relocated + 14].copy_from_slice(&back);
        std::ptr::copy_nonoverlapping(code.as_ptr(), trampoline, code.len());
        let mut old = 0;
        if VirtualProtect(trampoline as *mut c_void, 64, PAGE_EXECUTE_READ, &mut old) == 0 {
            return Err("trampoline protection failed");
        }
        FlushInstructionCache(GetCurrentProcess(), trampoline as *const c_void, 64);
        let mut patched = original;
        patched[..6].copy_from_slice(&JUMP);
        for byte in patched.iter_mut().take(relocated).skip(14) {
            *byte = 0xcc;
        }
        Ok(Patch { site, original, patched, trampoline, installed: false })
    }

    /// Write the jump to `detour` (same ABI as the site) in one 16-byte compare-exchange.
    ///
    /// # Safety
    /// No thread may be executing the first 16 bytes of the site while it is written (arm
    /// from the only thread that runs it).
    pub unsafe fn arm(&mut self, detour: usize) -> Result<(), &'static str> {
        if self.installed {
            return Err("already armed");
        }
        self.patched[6..14].copy_from_slice(&(detour as u64).to_le_bytes());
        let mut old = 0;
        if VirtualProtect(self.site as *mut c_void, 16, PAGE_EXECUTE_READWRITE, &mut old) == 0 {
            return Err("site protection change failed");
        }
        let swapped = swap16(self.site, self.original, self.patched);
        let mut ignored = 0;
        VirtualProtect(self.site as *mut c_void, 16, old, &mut ignored);
        FlushInstructionCache(GetCurrentProcess(), self.site as *const c_void, 16);
        if !swapped {
            return Err("site changed while patching");
        }
        self.installed = true;
        Ok(())
    }

    /// `prepare` + `arm`.
    ///
    /// # Safety
    /// See [`Patch::prepare`] and [`Patch::arm`].
    pub unsafe fn install(
        site: *mut u8,
        expected: [u8; 16],
        relocated: usize,
        detour: usize,
    ) -> Result<Patch, &'static str> {
        let mut patch = Patch::prepare(site, expected, relocated)?;
        patch.arm(detour)?;
        Ok(patch)
    }

    /// Address to call the original function through.
    pub fn trampoline(&self) -> *const u8 {
        self.trampoline
    }

    pub fn installed(&self) -> bool {
        self.installed
    }

    /// Whether the site still holds exactly the patched bytes.
    pub fn intact(&self) -> bool {
        self.installed && unsafe { read16(self.site) } == self.patched
    }

    /// Restore the original bytes. Fails (and leaves the site alone) if the site does not
    /// hold exactly the patched bytes.
    pub fn remove(&mut self) -> Result<(), &'static str> {
        if !self.installed {
            return Ok(());
        }
        unsafe {
            let mut old = 0;
            if VirtualProtect(self.site as *mut c_void, 16, PAGE_EXECUTE_READWRITE, &mut old) == 0 {
                return Err("site protection change failed");
            }
            let swapped = swap16(self.site, self.patched, self.original);
            let mut ignored = 0;
            VirtualProtect(self.site as *mut c_void, 16, old, &mut ignored);
            FlushInstructionCache(GetCurrentProcess(), self.site as *const c_void, 16);
            if !swapped {
                return Err("site no longer holds the patch");
            }
            if read16(self.site) != self.original {
                return Err("site not restored");
            }
        }
        self.installed = false;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fake function with the same 15-byte prologue as 0xe1f840; it stores XMM1..3 and the
    /// fifth (stack) argument into the first argument and returns 0x5a.
    const FAKE: &[u8] = &[
        0x48, 0x8b, 0xc4, // mov rax,rsp
        0x48, 0x89, 0x58, 0x10, // mov [rax+10h],rbx
        0x48, 0x89, 0x70, 0x18, // mov [rax+18h],rsi
        0x55, // push rbp
        0x57, // push rdi
        0x41, 0x54, // push r12
        0x41, 0x56, // push r14
        0x41, 0x57, // push r15
        0xf3, 0x0f, 0x11, 0x09, // movss [rcx],xmm1
        0xf3, 0x0f, 0x11, 0x51, 0x04, // movss [rcx+4],xmm2
        0xf3, 0x0f, 0x11, 0x59, 0x08, // movss [rcx+8],xmm3
        0xf3, 0x0f, 0x10, 0x44, 0x24, 0x50, // movss xmm0,[rsp+50h] (arg 5 after five pushes)
        0xf3, 0x0f, 0x11, 0x41, 0x0c, // movss [rcx+0ch],xmm0
        0xb0, 0x5a, // mov al,5ah
        0x41, 0x5f, 0x41, 0x5e, 0x41, 0x5c, 0x5f, 0x5d, // pop r15,r14,r12,rdi,rbp
        0xc3, // ret
    ];

    type Fake = unsafe extern "C" fn(*mut f32, f32, f32, f32, f32, f32, f32, u8) -> u8;

    static mut TRAMPOLINE: usize = 0;
    static mut SEEN: [f32; 4] = [0.0; 4];

    #[allow(clippy::too_many_arguments)]
    unsafe extern "C" fn detour(out: *mut f32, a: f32, b: f32, c: f32, d: f32, e: f32, f: f32, g: u8) -> u8 {
        SEEN = [a, b, c, d];
        let original: Fake = std::mem::transmute(TRAMPOLINE);
        original(out, a, b, c, d, e, f, g).wrapping_add(1)
    }

    fn fake() -> *mut u8 {
        unsafe {
            let page = VirtualAlloc(std::ptr::null_mut(), 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE) as *mut u8;
            assert!(!page.is_null());
            std::ptr::copy_nonoverlapping(FAKE.as_ptr(), page, FAKE.len());
            let mut old = 0;
            assert_ne!(VirtualProtect(page as *mut c_void, 4096, PAGE_EXECUTE_READ, &mut old), 0);
            page
        }
    }

    #[test]
    fn detour_passes_arguments_and_restores() {
        let site = fake();
        let expected: [u8; 16] = FAKE[..16].try_into().unwrap();
        let call = |site: *mut u8| unsafe {
            let f: Fake = std::mem::transmute(site);
            let mut out = [0f32; 4];
            let r = f(out.as_mut_ptr(), 1.5, -2.25, 3.0, 4.75, 5.0, 6.0, 1);
            (r, out)
        };
        assert_eq!(call(site), (0x5a, [1.5, -2.25, 3.0, 4.75]));
        let mut patch = unsafe { Patch::prepare(site, expected, 15) }.unwrap();
        unsafe { TRAMPOLINE = patch.trampoline() as usize };
        assert!(!patch.intact());
        unsafe { patch.arm(detour as Fake as usize) }.unwrap();
        assert!(patch.intact());
        let bytes = unsafe { read16(site) };
        assert_eq!(&bytes[..2], &[0xff, 0x25]);
        assert_eq!((bytes[14], bytes[15]), (0xcc, expected[15]));
        assert_eq!(call(site), (0x5b, [1.5, -2.25, 3.0, 4.75]));
        assert_eq!(unsafe { SEEN }, [1.5, -2.25, 3.0, 4.75]);
        patch.remove().unwrap();
        assert_eq!(unsafe { read16(site) }, expected);
        assert_eq!(call(site), (0x5a, [1.5, -2.25, 3.0, 4.75]));
        patch.remove().unwrap(); // idempotent
    }

    #[test]
    fn refuses_unexpected_bytes_or_alignment() {
        let site = fake();
        let mut wrong: [u8; 16] = FAKE[..16].try_into().unwrap();
        wrong[3] ^= 1;
        assert_eq!(
            unsafe { Patch::install(site, wrong, 15, 0) }.unwrap_err(),
            "site bytes differ from the pinned build"
        );
        let expected: [u8; 16] = FAKE[..16].try_into().unwrap();
        assert!(unsafe { Patch::install(site.add(1), expected, 15, 0) }.is_err());
        assert_eq!(unsafe { read16(site) }, expected, "nothing written on refusal");
    }

    #[test]
    fn removal_refuses_foreign_bytes() {
        let site = fake();
        let expected: [u8; 16] = FAKE[..16].try_into().unwrap();
        let mut patch = unsafe { Patch::install(site, expected, 15, detour as Fake as usize) }.unwrap();
        unsafe {
            let mut old = 0;
            VirtualProtect(site as *mut c_void, 16, PAGE_EXECUTE_READWRITE, &mut old);
            *site.add(13) = 0x90; // someone else changed the patch
            VirtualProtect(site as *mut c_void, 16, old, &mut old);
        }
        assert!(!patch.intact());
        assert_eq!(patch.remove().unwrap_err(), "site no longer holds the patch");
    }
}
