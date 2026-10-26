//! Replay of view-cache captures (`X4MP_XFORM_CAPTURE_DIR`: `*.cap` files from the oracle's
//! capture mode or from the in-game probe). Every captured component's state after the
//! original build must equal the model's replay (reference counts excluded, see
//! `capture::Field::Refs`). Skipped with a note when the variable is unset.

use x4mp_transform::capture::{replay, summarize, Capture, Field};

#[test]
fn captures_replay_identically() {
    let Some(dir) = std::env::var_os("X4MP_XFORM_CAPTURE_DIR") else {
        eprintln!("capture replay skipped: X4MP_XFORM_CAPTURE_DIR not set");
        return;
    };
    let mut files: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "cap"))
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no captures");
    let (mut components, mut refreshes) = (0, 0);
    for f in &files {
        let c = Capture::parse(&std::fs::read(f).unwrap()).unwrap_or_else(|e| panic!("{}: {e}", f.display()));
        let r = replay(&c);
        let real: Vec<_> = r.mismatches.iter().filter(|m| m.field != Field::Refs).collect();
        assert!(real.is_empty(), "{}: {:?}; first {:?}", f.display(), summarize(&r.mismatches), real.first());
        components += r.census.components;
        refreshes += r.census.refreshes;
    }
    eprintln!("captures: {} files, {components} components, {refreshes} refreshes replayed identically", files.len());
}
