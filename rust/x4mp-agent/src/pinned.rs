//! The one Linux X4 build this agent supports, and what must be found in it before the
//! agent may touch anything.
//!
//! Target: X4 Foundations 9.00 (611726), Steam build 23660954, Linux ELF64 x86-64
//! `ET_EXEC` (fixed image base 0x400000, no ASLR for the main image). Every address
//! below is an ELF virtual address in that file and is valid **only** for it. None of
//! the Windows RVAs apply, and none of these addresses may be reused on another build.
//!
//! A signature site is `(va, length, SHA-256 of those bytes)`. Raw engine bytes are not
//! stored. Evidence and confidence for each address are in
//! `reports/LINUX-AGENT-MAP-20261025.txt`.

use crate::sha256::{hex, Sha256};

pub const GAME_VERSION: &str = "9.00 (611726)";
pub const STEAM_BUILD: u32 = 23_660_954;
pub const FILE_SIZE: u64 = 60_459_280;
pub const FILE_SHA256: &str = "113f73d74570084f6ce18a659b365009eccf579fb66dbabb5dad2b1718045f20";
/// `.note.gnu.build-id` descriptor (20 bytes) at [`BUILD_ID_NOTE_VA`].
pub const BUILD_ID: &str = "77cf7ac0ff32d88ffd3db7eb6ea2e025aecb711d";
pub const BUILD_ID_NOTE_VA: u64 = 0x4002fc;
pub const IMAGE_BASE: u64 = 0x400000;

// --- Engine data the agent reads or writes (all verified through the sites below) ---

/// Global `XLib::VbiTable*` (in `.bss`). Read by `MoveWorker::Run` at 0x1212653.
pub const VBI_TABLE_GLOBAL: u64 = 0x3dbc4d8;
/// Table: `[0..17)` per-frame handlers, `+0x88` loading bank, `+0x110` render bank.
pub const VBI_SLOTS: usize = 17;
pub const VBI_TABLE_SIZE: usize = 0x138;
pub const VBI_LOADING_BANK: usize = 0x88;
/// `int` loading flag, set to 1 by the game start/load routine (0xf9a050) and cleared
/// on completion (0xf4aa07). While set, `MoveWorker` still calls per-frame handlers
/// that have no loading-bank entry; the agent then counts a loading skip.
pub const VBI_LOADING_FLAG: usize = 0x130;
/// Per-frame slot claimed by the agent. Statically empty on Linux (writers exist only
/// for 1, 3, 4, 6, 8, 9, 10, 11, 14, 16). Windows work uses 5, 7 and 12; 13 runs after
/// slot 3 (GameClient/universe), 6 (UI) and 11 (JobEngine), before 14 (Steam).
pub const AGENT_SLOT: usize = 13;
/// Slot 3 holds the `X4::GameClient` object itself (primary vtable below).
pub const GAME_CLIENT_SLOT: usize = 3;
pub const GAME_CLIENT_VTABLE: u64 = 0x3ab12a0;

/// TimerBase global block. `+0x00` recursive pthread mutex (0x28 bytes); fields follow
/// the Windows layout shifted by +8 (CRITICAL_SECTION is 0x28 vs. 0x30 here).
pub const TIMER_BASE: u64 = 0x3db8280;
pub const TIMER_GAME_TIME: u64 = TIMER_BASE + 0x30; // f64, only FrameTick/SetGameTime write
pub const TIMER_REAL_TIME: u64 = TIMER_BASE + 0x38; // f64, unscaled, runs while paused
pub const TIMER_UNPAUSED_REAL_TIME: u64 = TIMER_BASE + 0x40; // f64
pub const TIMER_SPEED: u64 = TIMER_BASE + 0x58; // f64, 0 paused, 1 normal, <=6 SETA
pub const TIMER_PAUSE_COUNT: u64 = TIMER_BASE + 0x64; // i32
pub const TIMER_TEMP_PAUSE_COUNT: u64 = TIMER_BASE + 0x68; // i32

/// Executable static-TLS offsets (local-exec model, fixed for the ET_EXEC main image).
/// `CurTime` (game time seen by this thread, synced at the start of every MoveWorker
/// iteration) and its previous value.
pub const TLS_CUR_TIME: i32 = -0x2728;
pub const TLS_PREV_TIME: i32 = -0x2628;
/// Component view selector; MoveWorker and Pathing Manager set it to 1 at thread start.
pub const TLS_VIEW_SELECTOR: i32 = -0x2620;
/// `pthread_setname_np` name given by `MoveWorker::Run` (string at 0x2d4eb7e).
pub const MOVE_WORKER_THREAD_NAME: &[u8] = b"Movement worker";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Site {
    pub name: &'static str,
    pub va: u64,
    pub len: usize,
    pub sha256: &'static str,
}

pub const SITES: &[Site] = &[
    Site { name: "move_worker_run_entry", va: 0x1212490, len: 0x35, sha256: "b09c6bed2ccf279993e1f55142bfa88c501af14f776503a2cdba90023afd3c5d" },
    Site { name: "move_worker_frame_start", va: 0x12125a0, len: 0x80, sha256: "57db462588f8cf62bf9dbac327343157a2e2484b2e367a261bca245102f29d5c" },
    Site { name: "move_worker_vbi_dispatch", va: 0x1212653, len: 0x5d, sha256: "7ff3eedea62f3e1d21f05452ceea7f367f4edf5ab2b9655245ae69d32940680f" },
    Site { name: "vbi_unregister", va: 0x1a3b9c0, len: 0x79, sha256: "b4f1e93fba9acedc979030826b33fb01234dd9899a05a04b2b6d98e25ed584fc" },
    Site { name: "vbi_table_alloc", va: 0xfcecb1, len: 0x56, sha256: "bec46cb484d0dd404ab933fb5920bd03c6933a039a5d256fbfde16ab907cb433" },
    Site { name: "vbi_loading_enter", va: 0xf9a044, len: 0x23, sha256: "80752963d9ea194a82fe4b41d9b96656c9088f4f8b4b05628a067bf3e9a21ee2" },
    Site { name: "vbi_loading_leave", va: 0xf4a9f0, len: 0x2a, sha256: "211c950bdf4effc130c056c3660f5f8932a8d182c4ea94f4389642511482b03c" },
    Site { name: "frame_tick_time_advance", va: 0x2025260, len: 0x95, sha256: "77e65fe042991e20e6af69ae8304a7222b7330fd1eae4ca9e6d85c473c9ae149" },
    Site { name: "timer_is_paused", va: 0x1a3b800, len: 0x42, sha256: "571df2fed03b0652ab04b0c79858d612b220366fdf20192418a96ce862fce53e" },
    Site { name: "export_is_game_paused", va: 0xc1ea20, len: 0xc, sha256: "5a68f009952d0267dde65837f1fbdd56a3b77a483fa07ff6c6890697d5b89166" },
    Site { name: "export_get_current_game_time", va: 0xa41960, len: 0x22, sha256: "71cbbbd1de9b38ea8eb8de9352d579e1e383708fc9b946b745432ab96c7287cd" },
    Site { name: "game_client_vbi_register", va: 0xfcf94f, len: 0x19, sha256: "941bf1e9d4f20e6506b877ba12546a4c83aaf592a2e1457c417008abd9159542" },
    Site { name: "ui_vbi_register", va: 0xa847ff, len: 0x30, sha256: "d5cca94ca0fbadd00e9b30260f066fc63ccc348a116501899e361bf6583c7098" },
    Site { name: "game_client_vtable_head", va: 0x3ab12a0, len: 0x18, sha256: "a4f00599507bf8d9dc0c3cd3e8684dcd3c64541c61882585f21466f497c8f70f" },
    Site { name: "ui_vbi_vtable", va: 0x3ab4978, len: 0x20, sha256: "53a305469a33eddf4bbb44ef521359d55d672b7753953df90aa88a424787f64f" },
    Site { name: "move_worker_vtable_head", va: 0x3b4cd58, len: 0x18, sha256: "268f9c510fedb221e11681efbbad4167f5d1f7cf2a55d2383431553297a9e34b" },
];

/// Read access to the executable by virtual address: the live process in the agent,
/// the ELF file on disk in the offline checker, or a fake in tests.
pub trait Image {
    fn read(&self, va: u64, len: usize) -> Option<Vec<u8>>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Check {
    Ok,
    Unreadable,
    Mismatch(String),
}

pub fn digest(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex(&hasher.finish())
}

pub fn check_site(image: &dyn Image, site: &Site) -> Check {
    match image.read(site.va, site.len) {
        Some(bytes) if bytes.len() == site.len => {
            let found = digest(&bytes);
            if found == site.sha256 {
                Check::Ok
            } else {
                Check::Mismatch(found)
            }
        }
        _ => Check::Unreadable,
    }
}

pub fn check_build_id(image: &dyn Image) -> Check {
    // Elf64_Nhdr {namesz=4, descsz=20, type=NT_GNU_BUILD_ID(3)} "GNU\0" + 20-byte id.
    match image.read(BUILD_ID_NOTE_VA, 36) {
        Some(note) if note.len() == 36 => {
            let header_ok = note[..16] == [4, 0, 0, 0, 20, 0, 0, 0, 3, 0, 0, 0, b'G', b'N', b'U', 0];
            let found = hex(&note[16..]);
            if header_ok && found == BUILD_ID {
                Check::Ok
            } else {
                Check::Mismatch(found)
            }
        }
        _ => Check::Unreadable,
    }
}

/// Number of verified sites when the build-id and every site match; otherwise the first
/// failing check as `name:reason`.
pub fn verify_sites(image: &dyn Image) -> Result<usize, String> {
    match check_build_id(image) {
        Check::Ok => {}
        Check::Unreadable => return Err("build_id:unreadable".into()),
        Check::Mismatch(found) => return Err(format!("build_id:mismatch:{found}")),
    }
    for site in SITES {
        match check_site(image, site) {
            Check::Ok => {}
            Check::Unreadable => return Err(format!("{}:unreadable", site.name)),
            Check::Mismatch(found) => return Err(format!("{}:mismatch:{found}", site.name)),
        }
    }
    Ok(SITES.len())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    /// Synthetic image holding bytes whose hashes we control.
    struct Fake(HashMap<u64, Vec<u8>>);
    impl Image for Fake {
        fn read(&self, va: u64, len: usize) -> Option<Vec<u8>> {
            self.0.get(&va).filter(|b| b.len() == len).cloned()
        }
    }

    #[test]
    fn site_table_is_well_formed() {
        let mut names = std::collections::HashSet::new();
        for site in SITES {
            assert!(names.insert(site.name), "duplicate {}", site.name);
            assert_eq!(site.sha256.len(), 64);
            assert!(site.sha256.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()));
            assert!(site.len >= 8 && site.len <= 0x200, "{}", site.name);
            assert!(site.va >= IMAGE_BASE && site.va < 0x3e00000, "{}", site.name);
        }
        for (i, a) in SITES.iter().enumerate() {
            for b in &SITES[i + 1..] {
                let disjoint = a.va + a.len as u64 <= b.va || b.va + b.len as u64 <= a.va;
                assert!(disjoint, "{} overlaps {}", a.name, b.name);
            }
        }
        assert!(AGENT_SLOT < VBI_SLOTS && ![5, 7, 12, 1, 3, 4, 6, 8, 9, 10, 11, 14, 16].contains(&AGENT_SLOT));
        assert_eq!(VBI_LOADING_BANK, VBI_SLOTS * 8);
    }

    #[test]
    fn same_build_as_the_core_pins() {
        assert_eq!(STEAM_BUILD, x4mp_core::pins::STEAM_BUILD);
        assert_eq!(FILE_SIZE, x4mp_core::pins::linux::FILE_SIZE);
        assert_eq!(FILE_SHA256, x4mp_core::pins::linux::FILE_SHA256);
        assert_eq!(GAME_VERSION, x4mp_core::pins::GAME_VERSION);
    }

    #[test]
    fn build_id_check_requires_exact_note() {
        let mut note = vec![4, 0, 0, 0, 20, 0, 0, 0, 3, 0, 0, 0, b'G', b'N', b'U', 0];
        let id: Vec<u8> = (0..20).map(|i| u8::from_str_radix(&BUILD_ID[i * 2..i * 2 + 2], 16).unwrap()).collect();
        note.extend_from_slice(&id);
        let ok = Fake([(BUILD_ID_NOTE_VA, note.clone())].into());
        assert_eq!(check_build_id(&ok), Check::Ok);
        note[35] ^= 1;
        assert!(matches!(check_build_id(&Fake([(BUILD_ID_NOTE_VA, note)].into())), Check::Mismatch(_)));
        assert_eq!(check_build_id(&Fake(HashMap::new())), Check::Unreadable);
    }

    #[test]
    fn verify_fails_closed_on_first_bad_or_missing_site() {
        // No real engine bytes here: every site is unreadable, so verification refuses.
        let mut note = vec![4, 0, 0, 0, 20, 0, 0, 0, 3, 0, 0, 0, b'G', b'N', b'U', 0];
        note.extend((0..20).map(|i| u8::from_str_radix(&BUILD_ID[i * 2..i * 2 + 2], 16).unwrap()));
        let image = Fake([(BUILD_ID_NOTE_VA, note)].into());
        assert_eq!(verify_sites(&image), Err(format!("{}:unreadable", SITES[0].name)));
        let site = Site { name: "t", va: 0x1000, len: 3, sha256: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" };
        assert_eq!(check_site(&Fake([(0x1000, b"abc".to_vec())].into()), &site), Check::Ok);
        assert!(matches!(check_site(&Fake([(0x1000, b"abd".to_vec())].into()), &site), Check::Mismatch(_)));
    }
}
