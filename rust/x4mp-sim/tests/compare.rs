//! Comparator path interning and the two row-matching paths of `World::compare_with`.

mod common;

use common::build_universe;
use x4mp_sim::comp::{Hull, Transform};
use x4mp_sim::shadow::{At, Comparator, Compare, Policy, Tol};
use x4mp_sim::{Bundle, SectorIx, UniverseId};

#[derive(Clone, Default, Debug, PartialEq)]
struct Part {
    heat: f32,
    ids: Vec<u32>,
}
x4mp_sim::impl_fields!(Part { heat, ids });

#[derive(Clone, Default, Debug, PartialEq)]
struct Ship {
    parts: Vec<Part>,
    grid: [[f32; 2]; 2],
    name: String,
}
x4mp_sim::impl_fields!(Ship { parts, grid, name });

fn ship() -> Ship {
    Ship {
        parts: vec![Part { heat: 1.0, ids: vec![1, 2] }, Part { heat: 2.0, ids: vec![3, 4, 5] }],
        grid: [[0.5, 1.5], [2.5, 3.5]],
        name: "a".into(),
    }
}

#[test]
fn full_paths_with_nested_indices_and_index_free_statistics() {
    let e = ship();
    let mut a = ship();
    a.parts[1].ids[2] = 9;
    a.grid[1][0] = f32::from_bits(2.5f32.to_bits() + 3);
    let mut c = Comparator::new(Policy::exact().with("s.grid", Tol::Ulps(1)));
    c.set_at(At { frame: 4, sector: Some(SectorIx(1)), entity: UniverseId(7) });
    c.compare("s", &e, &a);
    let fields: Vec<&str> = c.diffs().iter().map(|d| d.field.as_str()).collect();
    assert_eq!(fields, ["s.parts[1].ids[2]", "s.grid[1][0]"]);
    assert_eq!(c.diffs()[1].ulps, Some(3));
    // 2 heats + 5 ids + 4 grid cells + 1 name
    assert_eq!((c.compared(), c.mismatched()), (12, 2));
    let stats = c.stats();
    let keys: Vec<&str> = stats.keys().map(String::as_str).collect();
    assert_eq!(keys, ["s.grid[][]", "s.name", "s.parts[].heat", "s.parts[].ids[]"]);
    assert_eq!((stats["s.parts[].ids[]"].compared, stats["s.parts[].ids[]"].mismatched), (5, 1));
    assert_eq!(stats["s.grid[][]"].max_ulps, 3);
    assert_eq!(c.field_stats("s.parts[].heat").map(|s| s.compared), Some(2));
    assert_eq!(c.field_stats("s.parts"), None, "inner nodes carry no statistics");
}

#[test]
fn field_order_changes_and_name_collisions() {
    let mut c = Comparator::new(Policy::exact());
    for round in 0..3u32 {
        // the visiting order changes between rounds; the same names must reach the same nodes
        let order: Vec<&str> = if round == 1 { vec!["c", "a", "b"] } else { vec!["a", "b", "c"] };
        for name in order {
            c.compare(name, &round, &round);
        }
    }
    let stats = c.stats();
    assert_eq!(stats.len(), 3);
    assert!(stats.values().all(|s| s.compared == 3), "{stats:?}");
    // "x.y" as one name and "x" then "y" share one statistics key, as before the interning
    c.compare("x.y", &1u8, &2u8);
    c.field("x", |c| c.compare("y", &1u8, &1u8));
    let s = &c.stats()["x.y"];
    assert_eq!((s.compared, s.mismatched), (2, 1));
    assert_eq!(c.first().unwrap().field, "x.y");
    // names that are equal as strings but come from different allocations
    let dynamic = String::from("a");
    c.compare(&dynamic, &5u32, &5u32);
    assert_eq!(c.stats()["a"].compared, 4);
}

#[test]
fn reset_keeps_the_policy_and_paths_working() {
    let mut c = Comparator::new(Policy::exact().with("v", Tol::Ulps(2)));
    c.compare("v", &1.0f32, &f32::from_bits(1.0f32.to_bits() + 1));
    c.compare("w", &1.0f32, &2.0f32);
    assert_eq!((c.compared(), c.mismatched(), c.diffs().len()), (2, 1, 1));
    c.reset();
    assert_eq!((c.compared(), c.mismatched(), c.diffs().len()), (0, 0, 0));
    assert!(c.stats().is_empty());
    c.compare("v", &1.0f32, &f32::from_bits(1.0f32.to_bits() + 2));
    assert!(c.is_clean(), "the tolerance survives a reset");
    assert_eq!(c.stats()["v"].within_tolerance, 1);
}

#[test]
fn max_diffs_limits_records_not_counts() {
    let e: Vec<u32> = (0..100).collect();
    let a: Vec<u32> = (0..100).map(|v| v + 1).collect();
    let mut c = Comparator::new(Policy::exact()).max_diffs(3);
    c.compare("v", &e, &a);
    assert_eq!((c.mismatched(), c.diffs().len()), (100, 3));
    assert_eq!(c.diffs()[2].field, "v[2]");
    // a length difference is structural and the common prefix is still compared
    let mut c = Comparator::new(Policy::exact());
    e[..3].compare(&e[..2], &mut c);
    assert_eq!(c.first().unwrap().field, "len");
    assert_eq!(c.compared(), 3);
}

/// Row-aligned worlds take the fast path; an extra entity forces matching by UniverseId. Both
/// must report the same value difference.
#[test]
fn aligned_and_matched_world_compares_agree() {
    let expected = build_universe(0x51, 3, 40);
    let mut actual = build_universe(0x51, 3, 40);
    let uid = expected.world(SectorIx(1)).uids()[17];
    let e = actual.ids().by_uid(uid).unwrap();
    actual.get_mut::<Hull>(e).unwrap().current += 1.0;
    actual.get_mut::<Transform>(e).unwrap().0 .0[2][0] = -0.0; // basis row 1: [0, 1, 0, 0]

    let mut aligned = Comparator::new(Policy::exact());
    expected.compare(&actual, &[], 9, &mut aligned);
    let summary = |c: &Comparator| -> Vec<String> { c.diffs().iter().map(|d| d.to_string()).collect() };
    assert_eq!(aligned.mismatched(), 2, "{}", aligned.summary());
    let (he, ha) = (
        expected.get::<Hull>(expected.ids().by_uid(uid).unwrap()).unwrap().current,
        actual.get::<Hull>(e).unwrap().current,
    );
    assert_eq!(
        summary(&aligned),
        [
            format!("frame=9 sector=1 entity={uid} field=transform[2][0] expected=0e0(0x00000000) actual=-0e0(0x80000000) ulps=0 abs=0e0"),
            format!(
                "frame=9 sector=1 entity={uid} field=hull.current expected={he:e}({:#018x}) actual={ha:e}({:#018x}) ulps={} abs=1e0",
                he.to_bits(),
                ha.to_bits(),
                x4mp_sim::shadow::ulps64(he, ha)
            ),
        ]
    );

    let extra = UniverseId(0x7777_0001);
    actual.spawn(SectorIx(1), extra, Bundle::new()).unwrap();
    actual.commit();
    let mut matched = Comparator::new(Policy::exact());
    expected.compare(&actual, &[], 9, &mut matched);
    let d = summary(&matched);
    assert_eq!(d[..2], summary(&aligned)[..], "value differences agree");
    assert_eq!(d.len(), 3);
    assert!(d[2].contains(&format!("entity={extra} field=entity expected=absent actual=extra")), "{}", d[2]);
    assert_eq!(matched.compared(), aligned.compared() + 1);
}
