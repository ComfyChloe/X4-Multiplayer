//! `inspect_capture <file.cap> <node>...`: prints a captured component's pre/post state, the
//! model's events for it and the trace events for it (diagnostics for replay differences).

use x4mp_transform::capture::{replay, Capture};
use x4mp_transform::cache::Event;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let c = Capture::parse(&std::fs::read(&args[1]).unwrap()).unwrap();
    let r = replay(&c);
    for a in &args[2..] {
        let i: usize = a.parse().unwrap();
        let n = &c.tree.nodes[i];
        let p = &c.post[i];
        println!("node {i} id {:#x} parent {:?} mode {} mask {:#x} flags {:#x} classes {:#x}", n.id, n.parent, n.mode, n.mask, n.flags, n.classes);
        println!("  pre dirty {:#x?} vc_parent {:?} -> post dirty {:#x?} vc_parent {:?}", n.dirty, n.vc_parent, p.dirty, p.vc_parent);
        for e in &r.log {
            let hit = match e {
                Event::Visit { node, .. } | Event::IsClass { node, .. } | Event::Propagate { node, .. } | Event::Get { node, .. } | Event::Expire { node, .. } | Event::EndLoop { node } => *node as usize == i,
            };
            if hit {
                println!("  model {e:?}");
            }
        }
        for t in c.trace.iter().flatten().filter(|t| t[1] as usize == i) {
            println!("  trace kind {} a {:#x} b {:#x}", t[0], t[2], t[3]);
        }
    }
}
