//! Replays an `X4ECON` production capture against the economy model and prints the report.
//! Usage: `x4mp-econ-replay [--all] <capture.txt | x4lan-debug.txt>`; `--all` lists every
//! mismatch. Exit status 1 on any mismatch.

fn main() {
    let mut args: Vec<String> = std::env::args().collect();
    let all = args.iter().any(|a| a == "--all");
    args.retain(|a| a != "--all");
    if args.len() != 2 {
        eprintln!("usage: x4mp-econ-replay [--all] <capture file>");
        std::process::exit(2);
    }
    let text = match std::fs::read(&args[1]) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(e) => {
            eprintln!("{}: {e}", args[1]);
            std::process::exit(2);
        }
    };
    let cap = match x4mp_economy::capture::Capture::parse(&text) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{}: {e}", args[1]);
            std::process::exit(2);
        }
    };
    let rep = x4mp_economy::capture::replay(&cap);
    println!(
        "X4ECON replay build={} frames={} transitions={} exact={} exactWithRecordedDraws={} exactWithStorageDecision={} rngDependent={} external={} mismatched={}",
        cap.build,
        cap.frames.len(),
        rep.transitions,
        rep.exact,
        rep.exact_with_recorded_draws,
        rep.exact_with_storage_decision,
        rep.rng_dependent,
        rep.external,
        rep.mismatched
    );
    if all {
        for d in &rep.differences {
            println!("mismatch: {d}");
        }
    } else if let Some(d) = &rep.first_difference {
        println!("first difference: {d}");
    }
    std::process::exit(if rep.mismatched == 0 { 0 } else { 1 });
}
