//! Comparison with the original engine code (offline oracle).
//!
//! `X4MP_XFORM_ORACLE_DIR` names a directory prepared by `native/engine/xform_oracle.py`:
//! `scn_*.scn` scenarios with the oracle's `*.res` results, and `poses.pos` with
//! `poses.por`. Every event the oracle observed (IsClass and getter calls, in order, per
//! frame) and the final state of every component must equal the model's bit for bit, and
//! every pose must be bit-identical. Without the variable the test is skipped with a note:
//! it needs a verified local copy of the pinned X4.exe, which is never part of the repository.

use std::path::Path;
use x4mp_transform::posecases;
use x4mp_transform::scenario::{parse_result, Scenario};

fn first_state_difference(a: &[u8], b: &[u8]) -> usize {
    a.iter().zip(b).position(|(x, y)| x != y).unwrap_or(a.len().min(b.len()))
}

fn check_scenario(scn: &Path) -> (usize, usize) {
    let s = Scenario::from_bytes(&std::fs::read(scn).unwrap()).unwrap();
    let res = scn.with_extension("res");
    let oracle =
        parse_result(&std::fs::read(&res).unwrap_or_else(|_| panic!("missing {}", res.display())), s.tree.nodes.len())
            .unwrap();
    let (model, _) = s.run_model().expect("model overrun");
    assert_eq!(model.frames.len(), oracle.frames.len(), "{}: frame count", scn.display());
    let mut events = 0;
    for (k, (m, o)) in model.frames.iter().zip(&oracle.frames).enumerate() {
        if m != o {
            let at = m.iter().zip(o).position(|(a, b)| a != b).unwrap_or(m.len().min(o.len()));
            panic!(
                "{} frame {k}: event {at} differs: model {:?} oracle {:?} (lengths {} / {})",
                scn.display(),
                m.get(at),
                o.get(at),
                m.len(),
                o.len()
            );
        }
        events += m.len();
    }
    if model.state != oracle.state {
        panic!(
            "{}: final state differs at byte {}",
            scn.display(),
            first_state_difference(&model.state, &oracle.state)
        );
    }
    (events, s.tree.nodes.len())
}

#[test]
fn model_matches_original_engine_code() {
    let Some(dir) = std::env::var_os("X4MP_XFORM_ORACLE_DIR") else {
        eprintln!("oracle comparison skipped: X4MP_XFORM_ORACLE_DIR not set (run native/engine/xform_oracle.py)");
        return;
    };
    let dir = Path::new(&dir);
    let mut scenarios: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "scn"))
        .collect();
    scenarios.sort();
    assert!(!scenarios.is_empty(), "no scenarios in {}", dir.display());
    let (mut events, mut nodes) = (0, 0);
    for scn in &scenarios {
        let (e, n) = check_scenario(scn);
        events += e;
        nodes += n;
    }
    let cases = posecases::from_bytes(&std::fs::read(dir.join("poses.pos")).unwrap()).unwrap();
    let results = posecases::results_from_bytes(&std::fs::read(dir.join("poses.por")).unwrap()).unwrap();
    assert_eq!(cases.len(), results.len());
    let mut by_kind = [0usize; 3];
    for (i, (c, r)) in cases.iter().zip(&results).enumerate() {
        let e = c.expected();
        assert!(e.bit_eq(r), "pose case {i} (kind {}): model {:?} oracle {:?}", c.kind, e.to_bits(), r.to_bits());
        by_kind[c.kind as usize] += 1;
    }
    eprintln!(
        "oracle: {} scenarios, {nodes} components, {events} observed engine calls identical; {} compose and {} relative poses bit-identical",
        scenarios.len(),
        by_kind[1],
        by_kind[2]
    );
}
