//! Replay a native flight-model capture (x4mp-flightcap-<model>.log) against the reimplementation.
//! usage: x4mp-flight-replay <capture> [--std-math]
use std::path::PathBuf;
use std::collections::BTreeSet;

use x4mp_physics::capture::{replay, FlightCapture, MODEL_NAMES};
use x4mp_physics::flight::{StdMath, X4Math};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(path) = args.iter().find(|a| !a.starts_with("--")).map(PathBuf::from) else {
        eprintln!("usage: x4mp-flight-replay <capture> [--std-math]");
        std::process::exit(2);
    };
    let cap = match FlightCapture::read(&path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    let h = &cap.header;
    let name = format!("flight-{}", MODEL_NAMES[h.model as usize]);
    println!("CAPTURE {} model={} records={} dropped={} record_size={}", path.display(), name, h.records, h.dropped, h.record_size);
    let mut cheap = 0usize;
    let mut owner = 0usize;
    let mut input_poses = BTreeSet::new();
    let mut target_poses = BTreeSet::new();
    let mut dts = BTreeSet::new();
    let mut changed_slots = 0usize;
    let mut changed_models = 0usize;
    for r in cap.records() {
        cheap += (r.flags & 1) as usize;
        owner += ((r.flags >> 2) & 1) as usize;
        input_poses.insert(&r.before[..0x40]);
        target_poses.insert(&r.model_bytes[0x20..0x60]);
        dts.insert(r.dt.to_bits());
        changed_slots += usize::from(r.before != r.after);
        changed_models += usize::from(r.model_after.is_some_and(|after| r.model_bytes != after));
    }
    println!("CAPTURE cheap={cheap} owner_thread={owner}");
    println!("COVERAGE distinct_input_poses={} distinct_target_poses={} distinct_dt={} changed_slots={} model_outputs={} changed_models={}",
        input_poses.len(), target_poses.len(), dts.len(), changed_slots, h.version == 2, changed_models);
    let result = if args.iter().any(|a| a == "--std-math") { replay(&cap, &StdMath) } else { replay(&cap, &X4Math) };
    match result {
        Ok(report) => {
            println!("{}", report.line(&name));
            std::process::exit(if report.mismatched == 0 { 0 } else { 1 });
        }
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    }
}
