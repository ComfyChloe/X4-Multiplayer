//! Replays an `X4ECON` production capture against the economy model and prints the report.
//! Usage: `x4mp-econ-replay <capture.txt | x4lan-debug.txt>`; exit status 1 on any mismatch.

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 2 {
        eprintln!("usage: x4mp-econ-replay <capture file>");
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
        "X4ECON replay build={} frames={} transitions={} exact={} exactWithRecordedDraws={} rngDependent={} external={} mismatched={}",
        cap.build,
        cap.frames.len(),
        rep.transitions,
        rep.exact,
        rep.exact_with_recorded_draws,
        rep.rng_dependent,
        rep.external,
        rep.mismatched
    );
    if let Some(d) = &rep.first_difference {
        println!("first difference: {d}");
    }
    std::process::exit(if rep.mismatched == 0 { 0 } else { 1 });
}
