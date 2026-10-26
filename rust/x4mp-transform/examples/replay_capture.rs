//! `replay_capture <file.cap>...`: replays view-cache captures with the model and prints, per
//! capture, the census and the mismatches by field (first five of each shown). Exit status 1
//! if any capture differs (reference counts excluded: other threads may change them).

use x4mp_transform::capture::{replay, summarize, Capture, Field};

fn main() {
    let mut failed = false;
    for path in std::env::args().skip(1) {
        let c = match std::fs::read(&path)
            .map_err(|e| e.to_string())
            .and_then(|b| Capture::parse(&b).map_err(|e| e.to_string()))
        {
            Ok(c) => c,
            Err(e) => {
                println!("XFORM REPLAY file={path} error={e}");
                failed = true;
                continue;
            }
        };
        let r = replay(&c);
        let s = &r.census;
        let real: Vec<_> = r.mismatches.iter().filter(|m| m.field != Field::Refs).collect();
        println!(
            "XFORM REPLAY file={path} capture={} time={:.3} idx={} components={} visits={} open={} walker_open={} refreshes={} with_work={} attach={} poses={} velocities={} connections={} isclass={} expired={} kinds={:?} traced={} mismatches={} refs_only={} verdict={}",
            c.number,
            c.time,
            c.idx,
            s.components,
            s.visits,
            s.open,
            c.walker_opens,
            s.refreshes,
            s.refreshes_with_work,
            s.attachment_refreshes,
            s.poses,
            s.velocities,
            s.connections,
            s.is_class,
            s.expired,
            s.spec_kinds,
            c.trace.is_some(),
            real.len(),
            r.mismatches.len() - real.len(),
            if real.is_empty() { "identical" } else { "DIFFERS" }
        );
        for (field, n) in summarize(&r.mismatches) {
            println!("  {field:?}: {n}");
            for m in r.mismatches.iter().filter(|m| m.field == field).take(5) {
                println!(
                    "    node {} id {:#x}: model {} engine {}",
                    m.node,
                    c.ids.get(m.node).copied().unwrap_or(0),
                    m.model,
                    m.engine
                );
            }
        }
        failed |= !real.is_empty();
    }
    std::process::exit(failed as i32);
}
