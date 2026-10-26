//! x4mp-capdump: inspect, compare and natively cross-check x4mp-sim capture files.
//!
//! ```text
//! x4mp-capdump info  <capture>                       header, dictionary, per-frame summary, STAT
//! x4mp-capdump frame <capture> <frame> [--root UID] [--rows N]   rows of one frame
//! x4mp-capdump diff  <expected> <actual> [--tol COLUMN=ULPS]...  frame-by-frame field compare
//! x4mp-capdump check <capture> <x4-debug-log> [--layout SHA256]  MIRRORCHECK validation
//! ```
//! Exit status: 0 ok / equal / exact, 1 differences or failed check, 2 usage or read error.

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::process::ExitCode;

/// Line output that ends the program quietly when stdout is closed (e.g. piped into `head`).
macro_rules! out {
    ($($t:tt)*) => {{
        use std::io::Write as _;
        if writeln!(std::io::stdout().lock(), $($t)*).is_err() {
            std::process::exit(0);
        }
    }};
}

use x4mp_sim::capture::check::{check_frames, parse_check_line};
use x4mp_sim::capture::{col, mirror, tag_name, CaptureReader, Dict, FrameRecord, Header, SectorRecord};
use x4mp_sim::comp::{ClassId, NavCache, VtableRva};
use x4mp_sim::shadow::{Comparator, Policy, Tol};
use x4mp_sim::Universe;

fn usage() -> ExitCode {
    eprintln!("usage: x4mp-capdump info <capture> | frame <capture> <frame> [--root UID] [--rows N] | diff <expected> <actual> [--tol COLUMN=ULPS] | check <capture> <debug-log> [--layout SHA256]");
    ExitCode::from(2)
}

fn open(path: &str) -> Result<(CaptureReader<BufReader<File>>, Vec<FrameRecord>), String> {
    let f = File::open(path).map_err(|e| format!("{path}: {e}"))?;
    let mut r = CaptureReader::open(BufReader::new(f)).map_err(|e| format!("{path}: {e}"))?;
    let mut frames = Vec::new();
    loop {
        match r.next_frame() {
            Ok(Some(fr)) => frames.push(fr),
            Ok(None) => break,
            Err(e) => {
                eprintln!("{path}: {e} (after {} frames; using what was read)", frames.len());
                break;
            }
        }
    }
    Ok((r, frames))
}

fn print_header(h: &Header) {
    out!("format {}.{} producer={:?}", h.version.0, h.version.1, h.producer);
    out!("pin {}", h.pin.describe());
    out!("layout {}", x4mp_core::sha256::hex(&h.layout_sha256));
    out!("session_seed {:#x} interval {} flags {:#x}", h.session_seed, h.interval, h.flags);
    let cols: Vec<String> =
        h.columns.iter().map(|c| format!("{:#x}:{}:{:?}x{}", c.id, c.name, c.elem, c.count)).collect();
    out!("columns {}", cols.join(" "));
}

fn class_name(c: u8) -> String {
    ClassId(c).name().map_or_else(|| format!("{c:#x}"), str::to_string)
}

fn info(path: &str) -> Result<ExitCode, String> {
    let (r, frames) = open(path)?;
    print_header(r.header());
    for (i, name) in r.dict(Dict::Faction) {
        out!("faction {i} {name}");
    }
    let mut rows = 0u64;
    for f in &frames {
        rows += f.rows();
        let kinds = f.sectors.iter().fold([0usize; 3], |mut k, s| {
            k[usize::from(s.kind.min(2))] += 1;
            k
        });
        out!(
            "frame {} t={:.6} roots={} (sectors {} clusters {} galaxy {}) rows={} flags={:#x}",
            f.frame,
            f.game_time,
            f.sectors.len(),
            kinds[0],
            kinds[1],
            kinds[2],
            f.rows(),
            f.flags
        );
    }
    if let Some(f) = frames.last() {
        let mut by_class = std::collections::BTreeMap::new();
        for s in &f.sectors {
            if let Some(c) = s.column(col::CLASS) {
                for v in c.u8s() {
                    *by_class.entry(v).or_insert(0usize) += 1;
                }
            }
        }
        let top: Vec<String> = by_class.iter().map(|(c, n)| format!("{}={n}", class_name(*c))).collect();
        out!("classes (last frame) {}", top.join(" "));
    }
    match r.stats() {
        Some(s) => {
            out!("STAT frames={} dropped={} bytes={} walk_errors={}", s.frames, s.dropped, s.bytes, s.walk_errors)
        }
        None => out!("STAT missing (capture not closed)"),
    }
    if r.unknown_records() > 0 {
        out!("unknown records {}", r.unknown_records());
    }
    out!("total frames={} rows={}", frames.len(), rows);
    let _ = tag_name;
    Ok(ExitCode::SUCCESS)
}

fn dump_root(s: &SectorRecord, rows: usize) {
    out!("root {:#x} kind={} rows={}", s.root_uid, s.kind, s.rows);
    let uid = s.column(col::UID).map(|c| c.u64s()).unwrap_or_default();
    let class = s.column(col::CLASS).map(|c| c.u8s()).unwrap_or_default();
    let parent = s.column(col::PARENT).map(|c| c.u64s()).unwrap_or_default();
    let depth = s.column(col::DEPTH).map(|c| c.u16s()).unwrap_or_default();
    let att = s.column(col::ATTENTION).map(|c| c.i32s()).unwrap_or_default();
    let flags = s.column(col::FLAGS).map(|c| c.u8s()).unwrap_or_default();
    let ctrl = s.column(col::CTRL).map(|c| c.u8s()).unwrap_or_default();
    let pose = s.column(col::POSE).map(|c| c.f32s()).unwrap_or_default();
    let hull = s.column(col::HULL).map(|c| c.f64s()).unwrap_or_default();
    let owner = s.column(col::OWNER).map(|c| c.u16s()).unwrap_or_default();
    for r in 0..(s.rows as usize).min(rows) {
        let p =
            if pose.len() >= r * 16 + 3 { [pose[r * 16], pose[r * 16 + 1], pose[r * 16 + 2]] } else { [f32::NAN; 3] };
        out!(
            "  {:>3} uid={:#x} {:<14} parent={:#x} depth={} att={} flags={:#04x} ctrl={} pos={:.3},{:.3},{:.3} hull={} owner={}",
            r,
            uid.get(r).copied().unwrap_or(0),
            class_name(class.get(r).copied().unwrap_or(0xff)),
            parent.get(r).copied().unwrap_or(0),
            depth.get(r).copied().unwrap_or(0),
            att.get(r).copied().unwrap_or(0),
            flags.get(r).copied().unwrap_or(0),
            ctrl.get(r).copied().unwrap_or(0),
            p[0],
            p[1],
            p[2],
            hull.get(r).copied().unwrap_or(f64::NAN),
            owner.get(r).copied().unwrap_or(0xffff)
        );
    }
}

fn frame(path: &str, n: u64, root: Option<u64>, rows: usize) -> Result<ExitCode, String> {
    let (_, frames) = open(path)?;
    let f = frames.iter().find(|f| f.frame == n).ok_or_else(|| format!("frame {n} not in {path}"))?;
    for s in &f.sectors {
        if root.is_none_or(|r| r == s.root_uid) {
            dump_root(s, rows);
        }
    }
    Ok(ExitCode::SUCCESS)
}

fn universe() -> Universe {
    let mut u = Universe::new(0);
    u.register::<VtableRva>().register::<NavCache>();
    u
}

fn diff(a: &str, b: &str, tols: &[(String, u64)]) -> Result<ExitCode, String> {
    let (ra, fa) = open(a)?;
    let (rb, fb) = open(b)?;
    let mut policy = Policy::exact();
    for (c, n) in tols {
        policy = policy.with(c, Tol::Ulps(*n));
    }
    let mut c = Comparator::new(policy).max_diffs(20);
    let (mut ua, mut ub) = (universe(), universe());
    let fda = ra.dict(Dict::Faction);
    let fdb = rb.dict(Dict::Faction);
    let mut frames = 0;
    for (x, y) in fa.iter().zip(&fb) {
        if x.frame != y.frame {
            c.structural("frame", x.frame.to_string(), y.frame.to_string());
            break;
        }
        // Loading into fresh universes per frame keeps the comparison independent of history.
        if mirror(x, &fda, &mut ua).is_err() {
            ua = universe();
            mirror(x, &fda, &mut ua).map_err(|e| format!("{a}: frame {}: {e}", x.frame))?;
        }
        if mirror(y, &fdb, &mut ub).is_err() {
            ub = universe();
            mirror(y, &fdb, &mut ub).map_err(|e| format!("{b}: frame {}: {e}", y.frame))?;
        }
        ua.compare(&ub, &[], x.frame, &mut c);
        frames += 1;
    }
    if fa.len() != fb.len() {
        c.structural("frames", fa.len().to_string(), fb.len().to_string());
    }
    out!("compared frames={frames}");
    out!("{}", c.summary().trim_end());
    Ok(if c.is_clean() { ExitCode::SUCCESS } else { ExitCode::from(1) })
}

fn check(cap: &str, log: &str, layout: Option<&str>) -> Result<ExitCode, String> {
    let (r, frames) = open(cap)?;
    if let Some(l) = layout {
        let got = x4mp_core::sha256::hex(&r.header().layout_sha256);
        if got != l {
            out!("layout mismatch: capture {got} expected {l}");
            return Ok(ExitCode::from(1));
        }
    }
    let f = File::open(log).map_err(|e| format!("{log}: {e}"))?;
    let mut lines = Vec::new();
    for line in BufReader::new(f).split(b'\n') {
        let line = line.map_err(|e| format!("{log}: {e}"))?;
        if let Some(c) = parse_check_line(&String::from_utf8_lossy(&line)) {
            lines.push(c);
        }
    }
    let rep = check_frames(&frames, &lines);
    out!("{}", rep.line());
    for n in &rep.notes {
        out!("  {n}");
    }
    out!("verdict {}", if rep.exact() { "EXACT" } else { "NOT EXACT" });
    Ok(if rep.exact() { ExitCode::SUCCESS } else { ExitCode::from(1) })
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let flag = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let result = match args.first().map(String::as_str) {
        Some("info") if args.len() >= 2 => info(&args[1]),
        Some("frame") if args.len() >= 3 => {
            let Ok(n) = args[2].parse::<u64>() else { return usage() };
            let root = flag("--root").and_then(|v| u64::from_str_radix(v.trim_start_matches("0x"), 16).ok());
            let rows = flag("--rows").and_then(|v| v.parse().ok()).unwrap_or(usize::MAX);
            frame(&args[1], n, root, rows)
        }
        Some("diff") if args.len() >= 3 => {
            let mut tols = Vec::new();
            for (i, a) in args.iter().enumerate() {
                if a == "--tol" {
                    let Some((c, n)) = args.get(i + 1).and_then(|v| v.split_once('=')) else { return usage() };
                    let Ok(n) = n.parse() else { return usage() };
                    tols.push((c.to_string(), n));
                }
            }
            diff(&args[1], &args[2], &tols)
        }
        Some("check") if args.len() >= 3 => check(&args[1], &args[2], flag("--layout").as_deref()),
        _ => return usage(),
    };
    match result {
        Ok(code) => code,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(2)
        }
    }
}
