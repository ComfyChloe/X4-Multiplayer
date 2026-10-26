//! The one X4 build this core supports, and the pinned location of every exported
//! function it may call.
//!
//! Target: X4 Foundations 9.00 (611726), Steam build 23660954, both platforms.
//! Linux: ELF64 `ET_EXEC`, no ASLR for the main image, so `linux_va` is the absolute
//! address `dlsym` must return. Windows: `X4.exe` (ASLR), so `win_rva` is the address
//! `GetProcAddress` returns minus the main module base.
//!
//! `*_head` is the SHA-256 of the first [`HEAD_LEN`] code bytes at that address in the
//! pinned file. Address **and** head must match before a function is bound. The values
//! were taken offline from the pinned binaries (`nm -D` / PE export directory plus the
//! file bytes); `tests/pins_offline.rs` and `x4mp-sigcheck` re-check them against a file.
//! None of these values may be reused for another build.

use std::ffi::CStr;

pub const GAME_VERSION: &str = "9.00 (611726)";
pub const STEAM_BUILD: u32 = 23_660_954;
/// Number of code bytes hashed per export.
pub const HEAD_LEN: usize = 16;

/// Pinned Windows `X4.exe` (same values as the hooks crate on `contrib/ferrite-engine-hooks`).
pub mod windows {
    pub const FILE_SIZE: u64 = 55_412_768;
    pub const FILE_SHA256: &str = "19750a6563889a970f434b5566eb396c6b2dc29ff814bd3e336f838176ad6891";
    pub const PE_TIMESTAMP: u32 = 0x6a1e_aded;
    pub const SIZE_OF_IMAGE: u32 = 0x06fa_9000;
}

/// Pinned Linux `X4` (same values as `x4mp-agent::pinned`).
pub mod linux {
    pub const FILE_SIZE: u64 = 60_459_280;
    pub const FILE_SHA256: &str = "113f73d74570084f6ce18a659b365009eccf579fb66dbabb5dad2b1718045f20";
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    Linux,
    Windows,
}

impl Platform {
    pub const fn current() -> Option<Platform> {
        if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
            Some(Platform::Linux)
        } else if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
            Some(Platform::Windows)
        } else {
            None
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Platform::Linux => "linux",
            Platform::Windows => "windows",
        }
    }
}

/// One exported function, pinned on both platforms.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExportPin {
    pub name: &'static CStr,
    pub linux_va: u64,
    pub linux_head: &'static str,
    pub win_rva: u32,
    pub win_head: &'static str,
}

impl ExportPin {
    /// Pinned offset from the image base (`linux_va` with base 0 on the ET_EXEC image).
    pub fn offset(&self, platform: Platform) -> u64 {
        match platform {
            Platform::Linux => self.linux_va,
            Platform::Windows => u64::from(self.win_rva),
        }
    }

    pub fn head(&self, platform: Platform) -> &'static str {
        match platform {
            Platform::Linux => self.linux_head,
            Platform::Windows => self.win_head,
        }
    }
}

macro_rules! pin {
    ($name:literal, $lva:literal, $lhead:literal, $wrva:literal, $whead:literal) => {
        ExportPin { name: $name, linux_va: $lva, linux_head: $lhead, win_rva: $wrva, win_head: $whead }
    };
}

/// Every export the core binds. Order is irrelevant; names are unique.
pub const EXPORTS: &[ExportPin] = &[
    // --- clock and pause ---
    pin!(
        c"GetCurrentGameTime",
        0xa41960,
        "ed1171115a76a8d9dadbf4a668babb9aba365bdc195be5e063d6fd9dd83ef992",
        0xafcdc0,
        "9f015d5b98167c4c2ab4a76da141af2a68998fc089bc2f40e6f4a6cad700639a"
    ),
    pin!(
        c"IsGamePaused",
        0xc1ea20,
        "7abf0bdeb44a08cd801223b06df7b0a62d1b7502f8f8ce7295f774a2b509f226",
        0x1b2d70,
        "e6bb2fe53a3dae88a76f7de85194c62c682b350c26f979bb0d44a53749552b4e"
    ),
    // --- player ---
    pin!(
        c"IsPlayerValid",
        0xc03260,
        "6209a14b5c8dd27904fea8eebbea86a60fd0bfb2c6c4b0719d3083c2574769f8",
        0x1b3a50,
        "423f7fc7772a7ae16db53ccbee038dfbd6e71584d7fbe8e15d2e63731914ccc3"
    ),
    pin!(
        c"GetPlayerID",
        0xbdea50,
        "359fda5fbb299a922bca4091de85619c7f1cfff3f69baec1c1e680f357f197d8",
        0x1a4480,
        "0a012d89eb814b7e1d5cefe9d2371a8d1a3d2a3de38085ba77e64b4f1e0e5e07"
    ),
    pin!(
        c"GetPlayerOccupiedShipID",
        0xa44e40,
        "87f83507725070783f3820990057ccda91c933189157e55514e866bedd8ae483",
        0xb05e30,
        "83ac355b4690ef033804901ad1a929e8d22889c6ed5b5d5a0adecdcb1a7cf373"
    ),
    pin!(
        c"GetPlayerControlledShipID",
        0xc2d060,
        "2a1d27c07e95b27355ce5ce14e8ea2c03b9cc1ca83417a647aba44374ea8b274",
        0x1e9bd0,
        "b0ba564ee5ff94a4151ab794d3e6fa3dbf62502e368834be2b147d8cf3f839fa"
    ),
    pin!(
        c"GetPlayerObjectID",
        0xbdedc0,
        "1ea3aa78d651cd87a6f5bc3c4d80eca85506dae74d8cb51e26c90623745f11af",
        0x1a4850,
        "0fcbc5c859aeb10c049c1135188991a41288f9fb479a8ba60ef4526633a0e96d"
    ),
    pin!(
        c"GetPlayerContainerID",
        0xbde890,
        "0df03eb7885fd40674e569b15a25409e89ac3fe417fd1799d43c02edc9618daf",
        0x1a42a0,
        "4abc8afd3a72eb63eb10847e796697caa888636a7c6ba37ef1c3dc1e1b39d1b2"
    ),
    pin!(
        c"GetPlayerZoneID",
        0xbdf230,
        "cb763f6cb6f965d9e5f5ff429ca569e2a81585879aeb5a3cd59300026b39c735",
        0x1a4f90,
        "c2d61dadd1db7cfe7745aea873e414730e8cbb193a33b6f2f495d0afe673ae44"
    ),
    // --- pose (sector frame) ---
    pin!(
        c"GetObjectPositionInSector",
        0xbefa30,
        "89fbaba2f240bb41bb883e79aca331d2c33c5ef4cb5189e0a4e2447fbdd3078e",
        0x1a2600,
        "0686cecf651023cfbf22096ff1b113ca442998aa347f044e31fedc587f3869ea"
    ),
    pin!(
        c"SetObjectSectorPos",
        0xc0e1c0,
        "32488be898b10fefed4eaf856005acec0e03dd37c84b153ca87fdaa3bcb5da09",
        0x1b8be0,
        "cf6249c3500af694e532564c8adb452934ca050cf77da3fc125eccb6630fff46"
    ),
    pin!(
        c"GetPositionalOffset",
        0xbeffd0,
        "89fbaba2f240bb41bb883e79aca331d2c33c5ef4cb5189e0a4e2447fbdd3078e",
        0x1a5000,
        "3409fe408664a599fc40c3bb98defdf10df55aaf33fb19ce5f88edabd30e6257"
    ),
    pin!(
        c"GetContextByClass",
        0xbe2530,
        "9b3004b508c7a1306a15f0a6722e4bd0270fef89e7c4f10f4cfda8ca1c86599a",
        0x18aab0,
        "998d32a03988291696db79013d638471c6cdeae053a58df3c2215dd1e0b9924e"
    ),
    // --- identity ---
    pin!(
        c"GetComponentName",
        0xbe1c10,
        "21906807d9a0f558c086f5d70f57690fe9a3896973ed6ae8e5b17f976289c958",
        0x189d60,
        "8a8e63d83fa547bdc1d8ab238ac1c4faa8c26c96bcff9903646c5481dfaa12a2"
    ),
    pin!(
        c"GetObjectIDCode",
        0xbfb1f0,
        "5344a82a4b2d4eb459cefc6afc9f68ccb8a90434082a69dfa3f1060fdd2094a9",
        0x1a2430,
        "b746bf01815924e03708838fbf7d885c5a2870ad344a5e035cafd0c9195e1188"
    ),
    pin!(
        c"ConvertStringTo64Bit",
        0xbcf880,
        "8707a62c883ffe87d2ed3a22dcbf8235e2236572406c6ae06dd7ca1dcc62da48",
        0x178260,
        "0da77f515ed19351146b724763bb3ea8be9157451c79755cb74c87c30d6bf244"
    ),
    pin!(
        c"GetComponentClass",
        0xbe16d0,
        "1a128791427d480c298309d06e8a0cf1be0da5918069f9498fde10ce33cae2f1",
        0x1899a0,
        "c063dc841b0423b14b2013ff0e6370d9572a4a5d854182f89ea1b57f4e4806b0"
    ),
    pin!(
        c"IsComponentClass",
        0xc079f0,
        "1bc9aec0c91f720fba34f7c88dc047ebf01e4d3cf8125b53e98c7032c1db4fd6",
        0x1b2230,
        "4d93957473389394ec59f7baa02d334c520689e8e3d2b94de1eabff37894ce85"
    ),
    pin!(
        c"IsValidComponent",
        0xa43530,
        "8217b2410caeacf7df530b5b4310f317b8def7646ee215a420046512735e487d",
        0xb002e0,
        "23141509e6328828e1d4f39336441cc2e4c9f8339f787ba48f2a6b338cbe91e9"
    ),
    pin!(
        c"IsComponentOperational",
        0xc02cb0,
        "f7219db0cdc2cfe50d43d2b0450cb4d7feabdf3747b2948de9af973ea1e41092",
        0x1b2320,
        "f22351f8a454f92ab2454b44a999017ef7d3e1723ea19abc4c86fe3f7dfb5118"
    ),
    pin!(
        c"IsComponentWrecked",
        0xa4c290,
        "9767da39a774cc120509d2533af5097ed70527befd2d49a3a12ab67de2825571",
        0xaff7e0,
        "9cd4cb72f1296f8a215006b95900336b8097f5f977fd76db9181e7a1124aafb8"
    ),
    pin!(
        c"GetComponentDetails",
        0xa49cc0,
        "b50970acebba6355ecc7822f5c9960a4502d87575ad1b34fed9eb382601756be",
        0xafc250,
        "e35ddfbde5381aae0c1f37ff9792b4c7da8da40cc98ae5bfa509dbaba349d99d"
    ),
    // --- owner ---
    pin!(
        c"GetOwnerDetails",
        0xa4af50,
        "cedcb31e8cc90e499dfedee224c20dd64907c2d0a79988600b99ebb31aa711a6",
        0xafdfd0,
        "cc42c0bc351fedeea108f928801f827376f07b4839781d742e1666b043fdbed9"
    ),
    pin!(
        c"GetPlayerFactionName",
        0xbde960,
        "8a0be65d0f83addb8a1b1e254ed2539c8c15c1ecace09573503c2344f91db073",
        0x1a4390,
        "ea699bd5d7afa71701d6d70738a77cc0939f96f9171f13f13f6016e4a9bb597d"
    ),
    // --- version ---
    pin!(
        c"GetGameVersion",
        0xa41a60,
        "20e5b483e408ffcc4eb9e8b923b32623bebe272d181027c6762e46d2786fdf70",
        0x354a30,
        "24f284a324e2bfa77d7204fd9cda1aed1377a528c80b1bac94dd621ede70b8bb"
    ),
];

/// Exports that are compiled-out stubs (`ret`) in this build on both platforms. They are
/// never bound, even though the symbol exists. Full list of 39 shared stubs: FFI-INVENTORY.md.
pub const KNOWN_STUBS: &[&CStr] = &[c"MakePlayerOwnerOf", c"MovePlayerToSectorPos"];

pub fn find(pins: &[ExportPin], name: &CStr) -> Option<ExportPin> {
    pins.iter().find(|pin| pin.name == name).copied()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn is_hex64(text: &str) -> bool {
        text.len() == 64 && text.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    }

    #[test]
    fn export_table_is_well_formed() {
        let mut names = std::collections::HashSet::new();
        let mut linux = std::collections::HashSet::new();
        let mut windows = std::collections::HashSet::new();
        for pin in EXPORTS {
            assert!(names.insert(pin.name), "duplicate {:?}", pin.name);
            assert!(linux.insert(pin.linux_va), "duplicate linux address {:?}", pin.name);
            assert!(windows.insert(pin.win_rva), "duplicate windows rva {:?}", pin.name);
            assert!(is_hex64(pin.linux_head) && is_hex64(pin.win_head), "{:?}", pin.name);
            // Linux ET_EXEC .text and Windows .text ranges of the pinned files.
            assert!((0x40_0000..0x300_0000).contains(&pin.linux_va), "{:?}", pin.name);
            assert!((0x1000..0x2a0_0000).contains(&pin.win_rva), "{:?}", pin.name);
            assert!(!KNOWN_STUBS.contains(&pin.name), "stub bound: {:?}", pin.name);
        }
        assert!(is_hex64(windows::FILE_SHA256) && is_hex64(linux::FILE_SHA256));
    }

    #[test]
    fn lookup_by_name() {
        let pin = find(EXPORTS, c"IsGamePaused").unwrap();
        assert_eq!(pin.offset(Platform::Linux), 0xc1ea20);
        assert_eq!(pin.offset(Platform::Windows), 0x1b2d70);
        assert!(find(EXPORTS, c"MakePlayerOwnerOf").is_none());
    }
}
