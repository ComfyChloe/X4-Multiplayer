//! Offline check of the pin table against the pinned executables on disk.
//!
//! Set `X4MP_LINUX_EXE=/path/to/X4` and/or `X4MP_WINDOWS_EXE=/path/to/X4.exe`. Each check
//! is skipped (with a note on stderr) when its variable is unset; it never reads game
//! files from a default location. For every pinned export it verifies: the file is the
//! pinned build, the export table maps the name to the pinned address, the code head
//! hash matches, and the code is not a `ret` stub. The known stubs must be stubs.

use x4mp_core::pins::{self, ExportPin, Platform};
use x4mp_core::sha256::{hex, Sha256};

fn digest(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    hex(&h.finish())
}

fn u16_at(d: &[u8], at: usize) -> usize {
    u16::from_le_bytes(d[at..at + 2].try_into().unwrap()) as usize
}
fn u32_at(d: &[u8], at: usize) -> usize {
    u32::from_le_bytes(d[at..at + 4].try_into().unwrap()) as usize
}
fn u64_at(d: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(d[at..at + 8].try_into().unwrap())
}
fn cstr_at(d: &[u8], at: usize) -> &[u8] {
    let end = d[at..].iter().position(|b| *b == 0).unwrap();
    &d[at..at + end]
}

fn load(var: &str) -> Option<Vec<u8>> {
    match std::env::var_os(var) {
        Some(path) => Some(std::fs::read(&path).unwrap_or_else(|e| panic!("{var}: {e}"))),
        None => {
            eprintln!("SKIP {var} not set");
            None
        }
    }
}

trait Exe {
    fn export(&self, name: &[u8]) -> Option<u64>;
    fn code(&self, offset: u64, len: usize) -> Option<&[u8]>;
}

struct Elf {
    data: Vec<u8>,
    /// (vaddr, file offset, filesz, executable)
    loads: Vec<(u64, u64, u64, bool)>,
    dynsym: Vec<(Vec<u8>, u64)>,
}

impl Elf {
    fn parse(data: Vec<u8>) -> Elf {
        assert_eq!(&data[..4], b"\x7fELF");
        let phoff = u64_at(&data, 32) as usize;
        let (phentsize, phnum) = (u16_at(&data, 54), u16_at(&data, 56));
        let mut loads = Vec::new();
        for i in 0..phnum {
            let at = phoff + i * phentsize;
            if u32_at(&data, at) == 1 {
                let flags = u32_at(&data, at + 4);
                loads.push((u64_at(&data, at + 16), u64_at(&data, at + 8), u64_at(&data, at + 32), flags & 1 != 0));
            }
        }
        let shoff = u64_at(&data, 40) as usize;
        let (shentsize, shnum) = (u16_at(&data, 58), u16_at(&data, 60));
        let mut dynsym = Vec::new();
        for i in 0..shnum {
            let at = shoff + i * shentsize;
            if u32_at(&data, at + 4) == 11 {
                // SHT_DYNSYM; sh_link = its string table.
                let (off, size, link, entsize) = (
                    u64_at(&data, at + 24) as usize,
                    u64_at(&data, at + 32) as usize,
                    u32_at(&data, at + 40),
                    u64_at(&data, at + 56) as usize,
                );
                let str_at = shoff + link * shentsize;
                let stroff = u64_at(&data, str_at + 24) as usize;
                for s in (off..off + size).step_by(entsize) {
                    let name = cstr_at(&data, stroff + u32_at(&data, s)).to_vec();
                    let info = data[s + 4];
                    let shndx = u16_at(&data, s + 6);
                    if info & 0xf == 2 && shndx != 0 {
                        dynsym.push((name, u64_at(&data, s + 8)));
                    }
                }
            }
        }
        Elf { data, loads, dynsym }
    }
}

impl Exe for Elf {
    fn export(&self, name: &[u8]) -> Option<u64> {
        self.dynsym.iter().find(|(n, _)| n == name).map(|(_, v)| *v)
    }
    fn code(&self, va: u64, len: usize) -> Option<&[u8]> {
        let &(vaddr, off, filesz, _) =
            self.loads.iter().find(|(v, _, s, x)| *x && *v <= va && va + len as u64 <= v + s)?;
        let _ = filesz;
        let start = (off + va - vaddr) as usize;
        self.data.get(start..start + len)
    }
}

struct Pe {
    data: Vec<u8>,
    sections: Vec<(usize, usize, usize, bool)>,
    exports: Vec<(Vec<u8>, u64)>,
    timestamp: usize,
    size_of_image: usize,
}

impl Pe {
    fn rva_to_offset(sections: &[(usize, usize, usize, bool)], rva: usize) -> Option<usize> {
        sections.iter().find(|(va, size, _, _)| *va <= rva && rva < va + size).map(|(va, _, raw, _)| raw + rva - va)
    }

    fn parse(data: Vec<u8>) -> Pe {
        assert_eq!(&data[..2], b"MZ");
        let pe = u32_at(&data, 0x3c);
        assert_eq!(&data[pe..pe + 4], b"PE\0\0");
        let timestamp = u32_at(&data, pe + 8);
        let nsec = u16_at(&data, pe + 6);
        let optsz = u16_at(&data, pe + 20);
        let opt = pe + 24;
        assert_eq!(u16_at(&data, opt), 0x20b);
        let size_of_image = u32_at(&data, opt + 56);
        let (export_rva, _) = (u32_at(&data, opt + 112), u32_at(&data, opt + 116));
        let mut sections = Vec::new();
        for i in 0..nsec {
            let at = opt + optsz + i * 40;
            let (vsize, va, rsize, raw) =
                (u32_at(&data, at + 8), u32_at(&data, at + 12), u32_at(&data, at + 16), u32_at(&data, at + 20));
            let exec = u32_at(&data, at + 36) & 0x2000_0000 != 0;
            sections.push((va, vsize.max(rsize), raw, exec));
        }
        let ed = Self::rva_to_offset(&sections, export_rva).unwrap();
        let (nnames, funcs, names, ords) =
            (u32_at(&data, ed + 24), u32_at(&data, ed + 28), u32_at(&data, ed + 32), u32_at(&data, ed + 36));
        let (funcs, names, ords) = (
            Self::rva_to_offset(&sections, funcs).unwrap(),
            Self::rva_to_offset(&sections, names).unwrap(),
            Self::rva_to_offset(&sections, ords).unwrap(),
        );
        let mut exports = Vec::new();
        for i in 0..nnames {
            let name = cstr_at(&data, Self::rva_to_offset(&sections, u32_at(&data, names + 4 * i)).unwrap()).to_vec();
            let ordinal = u16_at(&data, ords + 2 * i);
            exports.push((name, u32_at(&data, funcs + 4 * ordinal) as u64));
        }
        Pe { data, sections, exports, timestamp, size_of_image }
    }
}

impl Exe for Pe {
    fn export(&self, name: &[u8]) -> Option<u64> {
        self.exports.iter().find(|(n, _)| n == name).map(|(_, v)| *v)
    }
    fn code(&self, rva: u64, len: usize) -> Option<&[u8]> {
        let rva = rva as usize;
        let &(va, _, raw, exec) = self.sections.iter().find(|(va, size, _, _)| *va <= rva && rva + len <= va + size)?;
        exec.then(|| &self.data[raw + rva - va..raw + rva - va + len])
    }
}

fn check(exe: &dyn Exe, platform: Platform) {
    let mut failures = Vec::new();
    for pin in pins::EXPORTS {
        let ExportPin { name, .. } = *pin;
        let name_bytes = name.to_bytes();
        match exe.export(name_bytes) {
            Some(address) if address == pin.offset(platform) => {}
            other => failures.push(format!("{name:?}: export {other:x?} != pinned 0x{:x}", pin.offset(platform))),
        }
        match exe.code(pin.offset(platform), pins::HEAD_LEN) {
            Some(head) if head[0] == 0xc3 => failures.push(format!("{name:?}: stub")),
            Some(head) if digest(head) == pin.head(platform) => {}
            Some(head) => failures.push(format!("{name:?}: head {}", digest(head))),
            None => failures.push(format!("{name:?}: code not in an executable section")),
        }
    }
    for stub in pins::KNOWN_STUBS {
        let address = exe.export(stub.to_bytes()).unwrap_or_else(|| panic!("{stub:?} not exported"));
        let head = exe.code(address, 3).unwrap();
        assert!(head[0] == 0xc3 || head == [0xc2, 0, 0], "{stub:?} is no longer a stub on {platform:?}");
    }
    assert!(failures.is_empty(), "{platform:?}:\n{}", failures.join("\n"));
    eprintln!("PASS {platform:?}: {} export pins, {} known stubs", pins::EXPORTS.len(), pins::KNOWN_STUBS.len());
}

#[test]
fn linux_pins_match_the_pinned_elf() {
    let Some(data) = load("X4MP_LINUX_EXE") else { return };
    assert_eq!(data.len() as u64, pins::linux::FILE_SIZE);
    assert_eq!(digest(&data), pins::linux::FILE_SHA256);
    check(&Elf::parse(data), Platform::Linux);
}

#[test]
fn windows_pins_match_the_pinned_pe() {
    let Some(data) = load("X4MP_WINDOWS_EXE") else { return };
    assert_eq!(data.len() as u64, pins::windows::FILE_SIZE);
    assert_eq!(digest(&data), pins::windows::FILE_SHA256);
    let pe = Pe::parse(data);
    assert_eq!(
        (pe.timestamp as u32, pe.size_of_image as u32),
        (pins::windows::PE_TIMESTAMP, pins::windows::SIZE_OF_IMAGE)
    );
    check(&pe, Platform::Windows);
}
