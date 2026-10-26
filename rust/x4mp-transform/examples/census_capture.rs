//! `census_capture <file.cap>...`: what keeps the view-cache walk busy. Per capture: entries by
//! kind; how many owners the walk reached (their end loop ran) or not; for unreached owners,
//! whether the owner is missing from its live parent's cached child vector (a child list not yet
//! refreshed, so the owner can never be visited and its one-shot entries never count down); and
//! how many open gates exist only because of such entries (components whose +0x1e8 is non-zero
//! only through unreached entries).

use std::collections::HashSet;
use x4mp_transform::cache::Event;
use x4mp_transform::capture::{replay, Capture};

fn main() {
    for path in std::env::args().skip(1) {
        let c = Capture::parse(&std::fs::read(&path).unwrap()).unwrap();
        let r = replay(&c);
        let n = c.len();
        let reached: HashSet<usize> =
            r.log.iter().filter_map(|e| if let Event::EndLoop { node } = e { Some(*node as usize) } else { None }).collect();
        let visited_open: HashSet<usize> =
            r.log.iter().filter_map(|e| if let Event::Visit { node, open: true } = e { Some(*node as usize) } else { None }).collect();
        let mut by_kind = [[0usize; 3]; 10]; // [entries, entries of reached owners, entries of unreached owners]
        let (mut unreached_owners, mut stale, mut repeated) = (0, 0, 0);
        let mut unreached_entries = vec![0i32; n + 1];
        for i in 0..n {
            let Some(list) = &c.tree.nodes[i].spec else { continue };
            let mut kinds: Vec<i32> = list.iter().map(|e| e.kind).collect();
            kinds.sort_unstable();
            if kinds.windows(2).any(|w| w[0] == w[1]) {
                repeated += 1; // the switch refuses such frames (it addresses entries by kind)
            }
            let hit = reached.contains(&i);
            if !hit {
                unreached_owners += 1;
                let parent = c.tree.nodes[i].parent;
                if let Some(p) = parent {
                    if p < n && !c.tree.nodes[p].children[c.idx].contains(&i) {
                        stale += 1;
                    }
                }
                unreached_entries[i] = list.len() as i32;
            }
            for e in list {
                let k = (e.kind as usize).min(9);
                by_kind[k][0] += 1;
                by_kind[k][if hit { 1 } else { 2 }] += 1;
            }
        }
        // propagate unreached entry counts up the live chain to find gates held open only by them
        let mut held = vec![0i32; n + 1];
        for i in 0..n {
            if unreached_entries[i] == 0 {
                continue;
            }
            let mut p = Some(i);
            while let Some(q) = p {
                if q >= n {
                    break;
                }
                held[q] += unreached_entries[i];
                p = c.tree.nodes[q].parent;
            }
        }
        let only_held = visited_open
            .iter()
            .filter(|&&i| i < n && c.tree.nodes[i].count != 0 && c.tree.nodes[i].count == held[i])
            .count();
        println!(
            "CENSUS file={path} time={:.1} components={n} open={} spec_owners={} reached={} unreached={} unreached_not_in_parent_vector={stale} open_gates_held_only_by_unreached={only_held} lists_with_repeated_kind={repeated}",
            c.time,
            visited_open.len(),
            reached.len() + unreached_owners,
            reached.len(),
            unreached_owners
        );
        for (k, v) in by_kind.iter().enumerate() {
            if v[0] > 0 {
                println!("  kind {k}: entries {} reached {} unreached {}", v[0], v[1], v[2]);
            }
        }
    }
}
