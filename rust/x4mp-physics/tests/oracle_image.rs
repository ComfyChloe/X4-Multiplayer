//! The oracle maps exactly the pinned build: the native probe's pinned code hashes
//! (native/engine/signatures.py PHYSICS_SITES) must match the mapped bytes.
#![cfg(all(windows, target_arch = "x86_64"))]

use x4mp_physics::oracle::{default_exe, Image};

fn image() -> Image {
    let path = default_exe();
    Image::load(&path).unwrap_or_else(|error| {
        panic!("oracle coverage is required; set X4MP_X4_EXE to the pinned executable: {error}")
    })
}

#[test]
fn pinned_code_hashes_match_the_mapped_image() {
    let image = image();
    // (rva, length, sha256) copied from signatures.py PHYSICS_SITES.
    let sites: [(u32, usize, &str); 4] = [
        (0xC5940, 24, "ea41983c8853e368253719bebeee39612ea260cb5719e633669388722b04aaa6"),
        (0xC5960, 32, "90b58768bed65170d856bb9eadac6760434f70cde60c08a1ac629c29e0b1706f"),
        (0xB4100, 111, "c7c3156704013bcbb39109714376978ceb299fbc1edf753582cf55f214604159"),
        (0xEAED43, 198, "c047f7b9fc40c34a0d9b1fdbac1c9963907035a93965441b4e2b048e7686f3b0"),
    ];
    for (rva, length, expected) in sites {
        assert_eq!(image.sha256_at(rva, length), expected, "code hash at {rva:#x}");
    }
}

#[test]
fn a_wrong_file_is_refused() {
    let path = std::env::temp_dir().join("x4mp-physics-not-x4.exe");
    std::fs::write(&path, b"MZ not the pinned build").unwrap();
    assert!(Image::load(&path).is_err());
    let _ = std::fs::remove_file(&path);
}
