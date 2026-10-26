//! Replay a native flight-model capture (x4mp-flightcap-<model>.log) against the reimplementation.
//! usage: x4mp-flight-replay <capture> [--std-math]
use std::path::PathBuf;

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
    for r in cap.records() {
        cheap += (r.flags & 1) as usize;
        owner += ((r.flags >> 2) & 1) as usize;
    }
    println!("CAPTURE cheap={cheap} owner_thread={owner}");
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
