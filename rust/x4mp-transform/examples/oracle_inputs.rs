//! Writes oracle inputs: `oracle_inputs <dir> <scenarios> <seed>` creates `scn_NNNN.scn`
//! (oracle-safe view-cache universes of varied size) and `poses.pos` (pose cases) in <dir>.
//! `native/engine/xform_oracle.py` runs the original engine code on them and
//! `tests/oracle.rs` compares the results with the models.

use x4mp_transform::posecases;
use x4mp_transform::scenario::{generate, Shape};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 {
        eprintln!("usage: oracle_inputs <dir> <scenarios> <seed>");
        std::process::exit(2);
    }
    let dir = std::path::PathBuf::from(&args[1]);
    let count: usize = args[2].parse().expect("scenario count");
    let seed: u64 = args[3].parse().expect("seed");
    std::fs::create_dir_all(&dir).expect("create dir");
    let shapes = [
        Shape { clusters: 2, sectors: 2, zones: 2, objects: 3, depth: 1, frames: 6, oracle_safe: true, stale: false },
        Shape::default(),
        Shape { clusters: 4, sectors: 3, zones: 4, objects: 8, depth: 3, frames: 5, oracle_safe: true, stale: false },
        Shape { clusters: 1, sectors: 1, zones: 1, objects: 25, depth: 4, frames: 8, oracle_safe: true, stale: false },
    ];
    let mut nodes = 0usize;
    for i in 0..count {
        let s = generate(seed.wrapping_mul(1_000_003).wrapping_add(i as u64), &shapes[i % shapes.len()]);
        nodes += s.tree.nodes.len();
        std::fs::write(dir.join(format!("scn_{i:04}.scn")), s.to_bytes()).expect("write scenario");
    }
    let cases = posecases::generate(seed ^ 0x5eed, 20_000);
    std::fs::write(dir.join("poses.pos"), posecases::to_bytes(&cases)).expect("write poses");
    println!("oracle_inputs: {count} scenarios ({nodes} components), {} pose cases in {}", cases.len(), dir.display());
}
