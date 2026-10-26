//! `stale_census <file.cap>...`: how often the walk iterates a child vector that is not the live
//! child set, and whether the dirty bit 0x2 announces it. For every open visit of the model's walk
//! the vector it iterates is classified (refreshed from the live container in this frame, cached
//! with 0x2 pending, cached clean) and each child's live parent is compared with the owner of the
//! vector (same, none = detached, other = moved). Moved children are where a component can be
//! visited twice in one frame. Also counts components visited more than once.

use std::collections::HashMap;
use x4mp_transform::cache::Event;
use x4mp_transform::capture::{replay, Capture};

fn main() {
    for path in std::env::args().skip(1) {
        let c = Capture::parse(&std::fs::read(&path).unwrap()).unwrap();
        let r = replay(&c);
        let n = c.len();
        let idx = c.idx;
        let mut first_refresh: HashMap<u32, u32> = HashMap::new();
        let mut visits: HashMap<u32, u32> = HashMap::new();
        let mut open = Vec::new();
        for e in &r.log {
            match *e {
                Event::Propagate { node, flags, .. } => {
                    first_refresh.entry(node).or_insert(flags);
                }
                Event::Visit { node, open: o } => {
                    *visits.entry(node).or_default() += 1;
                    if o {
                        open.push(node);
                    }
                }
                _ => {}
            }
        }
        // [source][same, detached, moved, outside]
        let mut t = [[0usize; 4]; 3];
        let mut vectors = [0usize; 3];
        let mut moved_owners = 0;
        for &v in &open {
            let i = v as usize;
            if i >= n {
                continue;
            }
            let node = &c.tree.nodes[i];
            let refreshed = first_refresh.get(&v).is_some_and(|f| f & 2 != 0) && node.dirty[idx] & 2 != 0;
            let (src, kids): (usize, &[usize]) = if refreshed {
                (0, node.live_children.as_deref().unwrap_or(&[]))
            } else if node.dirty[idx] & 2 != 0 {
                (1, &node.children[idx])
            } else {
                (2, &node.children[idx])
            };
            vectors[src] += 1;
            let mut any_moved = false;
            for &k in kids {
                let class = if k >= n {
                    3
                } else {
                    match c.tree.nodes[k].parent {
                        Some(p) if p == i => 0,
                        None => 1,
                        Some(p) if p >= n => 3,
                        Some(_) => 2,
                    }
                };
                any_moved |= class == 2;
                t[src][class] += 1;
            }
            moved_owners += any_moved as usize;
        }
        let twice = visits.values().filter(|&&x| x > 1).count();
        println!(
            "STALE file={path} time={:.1} idx={idx} open_visits={} visited_twice={twice} owners_with_moved_children={moved_owners}",
            c.time,
            open.len()
        );
        for (s, name) in ["refreshed", "cached_dirty2", "cached_clean"].iter().enumerate() {
            println!(
                "  {name}: vectors {} children: same {} detached {} moved {} outside {}",
                vectors[s], t[s][0], t[s][1], t[s][2], t[s][3]
            );
        }
    }
}
