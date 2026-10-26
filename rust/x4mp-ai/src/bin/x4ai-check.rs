//! `x4ai-check <engine-output.log | capture.x4aitrace>`: run the native bracket checks
//! (x4mp_ai::native) on a lane run's collected X4 output and print the report.

fn main() {
    let Some(path) = std::env::args().nth(1) else {
        eprintln!("usage: x4ai-check <engine-output.log | capture>");
        std::process::exit(1);
    };
    let raw = match std::fs::read(&path) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(e) => {
            eprintln!("x4ai-check: {path}: {e}");
            std::process::exit(1);
        }
    };
    let sessions = if raw.contains("AITR\t") { x4mp_ai::native::extract_sessions(&raw) } else { vec![raw] };
    let mut failed = false;
    let mut brackets = 0;
    for (i, text) in sessions.iter().enumerate() {
        println!("== session {}", i + 1);
        match x4mp_ai::native::parse(text) {
            Ok(c) => {
                let report = x4mp_ai::native::check(&c);
                brackets += report.brackets;
                failed |= report.first_wait_mismatch.is_some();
                print!("{}", report.render());
                if report.brackets == 0 {
                    println!("session {}: no complete brackets; excluded from wait validation", i + 1);
                }
            }
            Err(e) => {
                eprintln!("x4ai-check: session {}: {e}", i + 1);
                failed = true;
            }
        }
    }
    if brackets == 0 {
        eprintln!("x4ai-check: no complete interpreter brackets found");
        failed = true;
    }
    if failed {
        std::process::exit(2);
    }
}
