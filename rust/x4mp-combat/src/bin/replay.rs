//! Replay joint-step captures against the Rust reimplementation.
//!
//! ```text
//! x4mp-combat-replay <capture.x4cj>...           replay; exit 0 only if every record is bit-exact
//! x4mp-combat-replay --oracle <out.x4cj> [--count N] [--seed S] [--exe X4.exe]
//!                                                Windows: record a reference capture from the
//!                                                original machine code of the pinned X4.exe
//! ```
//! The MXCSR of this process is compared with the capture's; a difference is reported
//! (results with subnormal intermediates may then differ legitimately).

use std::path::PathBuf;
use std::process::ExitCode;

use x4mp_combat::capture::{read_capture, replay, Source};

fn usage() -> ExitCode {
    eprintln!("usage: x4mp-combat-replay <capture>... | --oracle <out> [--count N] [--seed S] [--exe PATH]");
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        return usage();
    }
    if args[0] == "--oracle" {
        return oracle(&args[1..]);
    }
    let mut all_exact = true;
    for path in &args {
        let file = match std::fs::File::open(path) {
            Ok(f) => f,
            Err(e) => {
                eprintln!("{path}: {e}");
                return ExitCode::from(2);
            }
        };
        let (header, records) = match read_capture(std::io::BufReader::new(file)) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("{path}: {e}");
                return ExitCode::from(2);
            }
        };
        #[cfg(target_arch = "x86_64")]
        let here = x4mp_combat::mxcsr::get();
        #[cfg(not(target_arch = "x86_64"))]
        let here = header.mxcsr;
        let report = replay(&records);
        println!(
            "{path}: source={:?} build={} mode={} mxcsr={:#x}{} records={} exact={} mismatched={} (value={} velocity={} target={} time={} result={})",
            header.source,
            header.steam_build,
            header.joint_mode,
            header.mxcsr,
            if here == header.mxcsr { String::new() } else { format!(" (replay mxcsr {here:#x})") },
            report.records,
            report.exact(),
            report.mismatched,
            report.value,
            report.velocity,
            report.target,
            report.time,
            report.result
        );
        // the same records through the program's shared shadow framework (x4mp-sim)
        println!("  {}", x4mp_combat::sim::shadow_replay(&records).line());
        for (index, diff) in &report.first {
            let r = &records[*index];
            println!("  #{index} component={:#x} chain={} joint={} diff={diff:?} before={:?} command={:?} clock={:?} after={:?}", r.component, r.chain, r.joint, r.before, r.command, r.clock, r.after);
        }
        if header.joint_mode != 0 {
            println!("  note: joint mode {} is the sub-stepped branch, not reimplemented", header.joint_mode);
        }
        if header.source == Source::Synthetic {
            println!("  note: synthetic capture, not evidence about the original");
        }
        all_exact &= report.exact();
    }
    if all_exact {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
fn oracle(args: &[String]) -> ExitCode {
    use x4mp_combat::capture::{write_capture, Header};
    use x4mp_combat::oracle::{default_exe, Image};
    let Some(out) = args.first() else { return usage() };
    let mut count = 100_000usize;
    let mut seed = 1u64;
    let mut exe = default_exe();
    let mut rest = args[1..].iter();
    while let Some(flag) = rest.next() {
        let value = rest.next();
        match (flag.as_str(), value) {
            ("--count", Some(v)) => count = v.parse().unwrap_or(count),
            ("--seed", Some(v)) => seed = v.parse().unwrap_or(seed),
            ("--exe", Some(v)) => exe = PathBuf::from(v),
            _ => return usage(),
        }
    }
    let image = match Image::load(&exe) {
        Ok(image) => image,
        Err(e) => {
            eprintln!("refused: {e}");
            return ExitCode::from(3);
        }
    };
    let records = x4mp_combat_oracle_records(&image, count, seed);
    let header = Header {
        steam_build: 23_660_954,
        mxcsr: x4mp_combat::mxcsr::get(),
        joint_mode: image.joint_mode(),
        source: Source::Oracle,
    };
    match std::fs::File::create(out).and_then(|f| write_capture(std::io::BufWriter::new(f), &header, &records)) {
        Ok(()) => {
            println!("{out}: {} oracle records from {}", records.len(), exe.display());
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("{out}: {e}");
            ExitCode::from(2)
        }
    }
}

#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
fn x4mp_combat_oracle_records(
    image: &x4mp_combat::oracle::Image,
    count: usize,
    seed: u64,
) -> Vec<x4mp_combat::capture::Record> {
    use x4mp_combat::capture::Record;
    use x4mp_combat::ikjoint::{FrameClock, JointCommand, JointState};
    let mut rng = x4mp_combat::testrng::Rng::new(seed);
    (0..count)
        .map(|i| {
            let now = 50.0 + rng.unit() * 5000.0;
            let clock = FrameClock { now, previous: now - (0.004 + rng.unit() * 0.1) };
            let before = JointState {
                value: rng.range(-4.0, 4.0),
                time: if rng.chance(0.05) { now } else { clock.previous },
                velocity: rng.range(-3.0, 3.0),
                target: rng.range(-4.0, 4.0),
                wrap: rng.chance(0.5),
            };
            let command = JointCommand {
                target: rng.range(-8.0, 8.0),
                min: if rng.chance(0.6) { rng.range(-1.7, 0.2) } else { 0.0 },
                max: if rng.chance(0.6) { rng.range(-0.2, 1.7) } else { 0.0 },
                max_speed: rng.range(0.01, 4.0),
                accel: rng.range(0.0, 10.0),
                feed_forward: if rng.chance(0.4) { rng.range(-1.0, 1.0) } else { 0.0 },
                snap: rng.chance(0.03),
            };
            let (after, result) = image.call_ik_rotate_toward(&before, &command, clock);
            Record { component: i as u64, chain: 0, joint: 0, before, command, clock, after, result }
        })
        .collect()
}

#[cfg(not(all(target_os = "windows", target_arch = "x86_64")))]
fn oracle(_args: &[String]) -> ExitCode {
    eprintln!("--oracle needs Windows x64 and the pinned X4.exe");
    ExitCode::from(3)
}
