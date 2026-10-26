//! Shadow-compare framework: exactness policy, field-wise comparison with first-difference
//! reporting, offline replay of recorded frames, online shadow statistics and the feature
//! switch used when a replacement becomes authoritative.
//!
//! Exactness policy (reports/REWRITE-PROGRAM.md): integer and discrete state is always compared
//! exactly; floats are compared **bit-exactly by default** ([`Tol::Exact`]). A per-field
//! tolerance (ULPs and/or absolute) must be configured explicitly in a [`Policy`], documented,
//! and proven by a test. `Tol::Exact` distinguishes `-0.0` from `0.0` and NaN payloads.
//!
//! Every comparison runs through a [`Comparator`] that knows the current frame, sector and
//! entity, keeps per-field statistics (compared, mismatched, max ULP, max abs) keyed by the
//! field path without indices (`hull.current`, `transform[][]`), and records the first
//! difference (and up to `max_diffs` more) with the full path, expected and actual values.

use std::collections::BTreeMap;
use std::fmt;

use crate::id::{EntityId, OrderKey, SectorIx, UniverseId};

/// Float tolerance for one field.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Tol {
    /// Same bits (the default for every float).
    Exact,
    /// At most `n` units in the last place apart (`-0.0 == 0.0`, NaN == NaN).
    Ulps(u64),
    /// `|expected - actual| <= abs`, evaluated in f64.
    Abs(f64),
    /// Either bound suffices.
    AbsOrUlps { abs: f64, ulps: u64 },
}

/// Field-path -> tolerance. Rules match the index-free path by prefix; the longest rule wins.
/// Integer, bool and id fields ignore the policy (always exact).
#[derive(Debug, Clone, Default)]
pub struct Policy {
    rules: Vec<(String, Tol)>,
}

impl Policy {
    /// Everything bit-exact.
    pub fn exact() -> Policy {
        Policy::default()
    }

    /// Adds a rule: fields whose index-free path starts with `prefix` use `tol`.
    pub fn with(mut self, prefix: &str, tol: Tol) -> Policy {
        self.rules.push((prefix.to_string(), tol));
        self
    }

    pub fn tol_for(&self, norm_path: &str) -> Tol {
        let mut best: Option<(usize, Tol)> = None;
        for (prefix, tol) in &self.rules {
            let matches = norm_path == prefix
                || (norm_path.starts_with(prefix.as_str())
                    && matches!(norm_path.as_bytes().get(prefix.len()), Some(b'.') | Some(b'[')));
            if matches && best.is_none_or(|(len, _)| prefix.len() > len) {
                best = Some((prefix.len(), *tol));
            }
        }
        best.map_or(Tol::Exact, |(_, t)| t)
    }
}

/// Where a comparison happens.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct At {
    pub frame: u64,
    pub sector: Option<SectorIx>,
    pub entity: UniverseId,
}

/// One differing field.
#[derive(Debug, Clone, PartialEq)]
pub struct Difference {
    pub at: At,
    /// Full path with indices, e.g. `Transform[0][2]`.
    pub field: String,
    pub expected: String,
    pub actual: String,
    /// ULP distance for float fields.
    pub ulps: Option<u64>,
    /// |expected - actual| for float fields.
    pub abs: Option<f64>,
}

impl fmt::Display for Difference {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "frame={}", self.at.frame)?;
        if let Some(s) = self.at.sector {
            write!(f, " sector={}", s.0)?;
        }
        if !self.at.entity.is_none() {
            write!(f, " entity={}", self.at.entity)?;
        }
        write!(f, " field={} expected={} actual={}", self.field, self.expected, self.actual)?;
        if let Some(u) = self.ulps {
            write!(f, " ulps={u}")?;
        }
        if let Some(a) = self.abs {
            write!(f, " abs={a:e}")?;
        }
        Ok(())
    }
}

/// Per-field statistics (keyed by the index-free path).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct FieldStats {
    pub compared: u64,
    pub mismatched: u64,
    /// Largest ULP distance seen, including within-tolerance values.
    pub max_ulps: u64,
    /// Largest absolute difference seen, including within-tolerance values.
    pub max_abs: f64,
    /// Values that differed in bits but were accepted by the tolerance.
    pub within_tolerance: u64,
}

/// Accumulates comparison results. See the module docs.
///
/// Field paths are interned in a small trie (one node per distinct path without indices), so a
/// leaf comparison costs a child lookup, a bit comparison and two counter increments. The policy
/// tolerance is resolved once per node, and the full path with indices is rendered only when a
/// difference is recorded.
#[derive(Debug, Clone)]
pub struct Comparator {
    policy: Policy,
    at: At,
    nodes: Vec<PathNode>,
    cur: u32,
    /// Indices of the `[i]` segments on the current path, outermost first.
    indices: Vec<usize>,
    diffs: Vec<Difference>,
    max_diffs: usize,
    compared: u64,
    mismatched: u64,
}

const NO_NODE: u32 = u32::MAX;

/// One interned path segment.
#[derive(Debug, Clone)]
struct PathNode {
    parent: u32,
    /// `None` for an index segment (`[i]`; `[]` in the statistics key).
    name: Option<Box<str>>,
    /// Path without indices: statistics key and policy key.
    norm: Box<str>,
    tol: Tol,
    stats: FieldStats,
    /// Field children in first-use order. `cursor` is the position of the last hit; struct
    /// fields are visited in the same order every time, so the next position is tried first.
    fields: Vec<u32>,
    cursor: u32,
    index_child: u32,
}

impl PathNode {
    fn new(parent: u32, name: Option<&str>, norm: String, tol: Tol) -> PathNode {
        PathNode {
            parent,
            name: name.map(Into::into),
            norm: norm.into(),
            tol,
            stats: FieldStats::default(),
            fields: Vec::new(),
            cursor: 0,
            index_child: NO_NODE,
        }
    }
}

impl FieldStats {
    fn merge(&mut self, o: &FieldStats) {
        self.compared += o.compared;
        self.mismatched += o.mismatched;
        self.within_tolerance += o.within_tolerance;
        self.max_ulps = self.max_ulps.max(o.max_ulps);
        if o.max_abs > self.max_abs {
            self.max_abs = o.max_abs;
        }
    }
}

impl Comparator {
    pub fn new(policy: Policy) -> Comparator {
        let root = PathNode::new(NO_NODE, Some(""), String::new(), policy.tol_for(""));
        Comparator {
            policy,
            at: At::default(),
            nodes: vec![root],
            cur: 0,
            indices: Vec::new(),
            diffs: Vec::new(),
            max_diffs: 16,
            compared: 0,
            mismatched: 0,
        }
    }

    /// Keep at most `n` differences (the first is always kept when n >= 1).
    pub fn max_diffs(mut self, n: usize) -> Comparator {
        self.max_diffs = n;
        self
    }

    pub fn set_at(&mut self, at: At) {
        self.at = at;
    }
    pub fn at(&self) -> At {
        self.at
    }

    /// Compares `expected` with `actual` under `name` at the current location.
    #[inline]
    pub fn compare<T: Compare + ?Sized>(&mut self, name: &str, expected: &T, actual: &T) {
        self.field(name, |c| expected.compare(actual, c));
    }

    /// Runs `f` with `.name` appended to the path.
    #[inline]
    pub fn field(&mut self, name: &str, f: impl FnOnce(&mut Comparator)) {
        let prev = self.cur;
        self.cur = self.field_child(prev, name);
        f(self);
        self.cur = prev;
    }

    /// Runs `f` with `[i]` appended to the path (`[]` in the statistics key).
    #[inline]
    pub fn index(&mut self, i: usize, f: impl FnOnce(&mut Comparator)) {
        let prev = self.cur;
        let child = self.nodes[prev as usize].index_child;
        self.cur = if child != NO_NODE { child } else { self.new_child(prev, None) };
        self.indices.push(i);
        f(self);
        self.indices.pop();
        self.cur = prev;
    }

    #[inline]
    fn field_child(&mut self, parent: u32, name: &str) -> u32 {
        let p = &self.nodes[parent as usize];
        let n = p.fields.len();
        if n > 0 {
            let at = p.cursor as usize;
            let next = if at + 1 < n { at + 1 } else { 0 };
            for k in [next, at] {
                let c = p.fields[k];
                if self.nodes[c as usize].name.as_deref() == Some(name) {
                    self.nodes[parent as usize].cursor = k as u32;
                    return c;
                }
            }
            if let Some(k) = p.fields.iter().position(|c| self.nodes[*c as usize].name.as_deref() == Some(name)) {
                let c = p.fields[k];
                self.nodes[parent as usize].cursor = k as u32;
                return c;
            }
        }
        self.new_child(parent, Some(name))
    }

    #[cold]
    fn new_child(&mut self, parent: u32, name: Option<&str>) -> u32 {
        let pn = &self.nodes[parent as usize].norm;
        let norm = match name {
            Some(n) if pn.is_empty() => n.to_string(),
            Some(n) => format!("{pn}.{n}"),
            None => format!("{pn}[]"),
        };
        let tol = self.policy.tol_for(&norm);
        let id = u32::try_from(self.nodes.len()).expect("comparator path table overflow");
        self.nodes.push(PathNode::new(parent, name, norm, tol));
        let p = &mut self.nodes[parent as usize];
        match name {
            Some(_) => {
                p.fields.push(id);
                p.cursor = (p.fields.len() - 1) as u32;
            }
            None => p.index_child = id,
        }
        id
    }

    /// The full current path with indices, e.g. `transform[0][2]`.
    fn render_path(&self) -> String {
        use std::fmt::Write as _;
        let mut chain = Vec::new();
        let mut n = self.cur;
        while n != 0 {
            chain.push(n);
            n = self.nodes[n as usize].parent;
        }
        let mut out = String::new();
        let mut k = 0;
        for &n in chain.iter().rev() {
            match &self.nodes[n as usize].name {
                Some(name) => {
                    if !out.is_empty() {
                        out.push('.');
                    }
                    out.push_str(name);
                }
                None => {
                    let _ = write!(out, "[{}]", self.indices.get(k).copied().unwrap_or(0));
                    k += 1;
                }
            }
        }
        out
    }

    #[inline]
    fn stats_mut(&mut self) -> &mut FieldStats {
        &mut self.nodes[self.cur as usize].stats
    }

    fn record(&mut self, ok: bool, expected: String, actual: String, ulps: Option<u64>, abs: Option<f64>) {
        self.compared += 1;
        if !ok {
            self.mismatched += 1;
            if self.diffs.len() < self.max_diffs {
                let field = self.render_path();
                self.diffs.push(Difference { at: self.at, field, expected, actual, ulps, abs });
            }
        }
    }

    /// Leaf comparison of a discrete value (always exact).
    #[inline]
    pub fn exact<T: PartialEq + fmt::Debug + ?Sized>(&mut self, expected: &T, actual: &T) {
        if expected == actual {
            self.stats_mut().compared += 1;
            self.compared += 1;
        } else {
            self.exact_mismatch(format!("{expected:?}"), format!("{actual:?}"));
        }
    }

    #[cold]
    #[inline(never)]
    fn exact_mismatch(&mut self, expected: String, actual: String) {
        let s = self.stats_mut();
        s.compared += 1;
        s.mismatched += 1;
        self.record(false, expected, actual, None, None);
    }

    /// Leaf comparison of an f32 under the policy.
    #[inline]
    pub fn f32(&mut self, expected: f32, actual: f32) {
        if expected.to_bits() == actual.to_bits() {
            self.stats_mut().compared += 1;
            self.compared += 1;
        } else {
            self.f32_differs(expected, actual);
        }
    }

    #[inline(never)]
    fn f32_differs(&mut self, expected: f32, actual: f32) {
        let tol = self.nodes[self.cur as usize].tol;
        let (ok, ulps, abs) = judge(
            false,
            expected.is_nan(),
            actual.is_nan(),
            ulps32(expected, actual),
            (f64::from(expected) - f64::from(actual)).abs(),
            tol,
        );
        self.float_record(ok, ulps, abs, || {
            (format!("{expected:e}({:#010x})", expected.to_bits()), format!("{actual:e}({:#010x})", actual.to_bits()))
        });
    }

    /// Leaf comparison of an f64 under the policy.
    #[inline]
    pub fn f64(&mut self, expected: f64, actual: f64) {
        if expected.to_bits() == actual.to_bits() {
            self.stats_mut().compared += 1;
            self.compared += 1;
        } else {
            self.f64_differs(expected, actual);
        }
    }

    #[inline(never)]
    fn f64_differs(&mut self, expected: f64, actual: f64) {
        let tol = self.nodes[self.cur as usize].tol;
        let (ok, ulps, abs) =
            judge(false, expected.is_nan(), actual.is_nan(), ulps64(expected, actual), (expected - actual).abs(), tol);
        self.float_record(ok, ulps, abs, || {
            (format!("{expected:e}({:#018x})", expected.to_bits()), format!("{actual:e}({:#018x})", actual.to_bits()))
        });
    }

    /// Records a float leaf whose bits differ.
    fn float_record(&mut self, ok: bool, ulps: u64, abs: f64, show: impl FnOnce() -> (String, String)) {
        let s = self.stats_mut();
        s.compared += 1;
        s.max_ulps = s.max_ulps.max(ulps);
        if abs.is_finite() && abs > s.max_abs {
            s.max_abs = abs;
        }
        if ok {
            s.within_tolerance += 1;
        } else {
            s.mismatched += 1;
        }
        let (e, a) = if ok { (String::new(), String::new()) } else { show() };
        self.record(ok, e, a, Some(ulps), Some(abs));
    }

    /// Records a structural difference (missing/extra entity, length mismatch) at the current
    /// path with free-form descriptions.
    pub fn structural(&mut self, what: &str, expected: String, actual: String) {
        self.field(what, |c| {
            let s = c.stats_mut();
            s.compared += 1;
            s.mismatched += 1;
            c.record(false, expected, actual, None, None);
        });
    }

    pub fn compared(&self) -> u64 {
        self.compared
    }
    pub fn mismatched(&self) -> u64 {
        self.mismatched
    }
    pub fn is_clean(&self) -> bool {
        self.mismatched == 0
    }
    pub fn first(&self) -> Option<&Difference> {
        self.diffs.first()
    }
    pub fn diffs(&self) -> &[Difference] {
        &self.diffs
    }

    /// Per-field statistics keyed by the path without indices (`hull.current`,
    /// `transform[][]`), for every path that compared a value since the last
    /// [`Comparator::reset`].
    pub fn stats(&self) -> BTreeMap<String, FieldStats> {
        let mut out: BTreeMap<String, FieldStats> = BTreeMap::new();
        for n in self.nodes.iter().filter(|n| n.stats.compared > 0) {
            out.entry(n.norm.to_string()).or_default().merge(&n.stats);
        }
        out
    }

    /// Statistics of one path without indices, if it compared anything.
    pub fn field_stats(&self, path: &str) -> Option<FieldStats> {
        let mut found: Option<FieldStats> = None;
        for n in self.nodes.iter().filter(|n| n.stats.compared > 0 && &*n.norm == path) {
            found.get_or_insert_with(FieldStats::default).merge(&n.stats);
        }
        found
    }

    pub fn policy(&self) -> &Policy {
        &self.policy
    }

    /// Clears counts, statistics and differences (keeps the policy).
    pub fn reset(&mut self) {
        for n in &mut self.nodes {
            n.stats = FieldStats::default();
        }
        self.diffs.clear();
        self.compared = 0;
        self.mismatched = 0;
    }

    /// Multi-line summary: totals, every field with mismatches or tolerance use, first diffs.
    pub fn summary(&self) -> String {
        let mut out = format!("compared={} mismatched={}\n", self.compared, self.mismatched);
        for (field, s) in &self.stats() {
            if s.mismatched > 0 || s.within_tolerance > 0 {
                out.push_str(&format!(
                    "  {field}: compared={} mismatched={} within_tol={} max_ulps={} max_abs={:e}\n",
                    s.compared, s.mismatched, s.within_tolerance, s.max_ulps, s.max_abs
                ));
            }
        }
        for d in &self.diffs {
            out.push_str(&format!("  DIFF {d}\n"));
        }
        out
    }
}

fn judge(same_bits: bool, e_nan: bool, a_nan: bool, ulps: u64, abs: f64, tol: Tol) -> (bool, u64, f64) {
    if same_bits {
        return (true, 0, 0.0);
    }
    if matches!(tol, Tol::Exact) {
        return (false, ulps, abs);
    }
    if e_nan || a_nan {
        return (e_nan && a_nan, if e_nan && a_nan { 0 } else { u64::MAX }, f64::INFINITY);
    }
    let ok = match tol {
        Tol::Exact => false,
        Tol::Ulps(n) => ulps <= n,
        Tol::Abs(a) => abs <= a,
        Tol::AbsOrUlps { abs: a, ulps: n } => abs <= a || ulps <= n,
    };
    (ok, ulps, abs)
}

/// Monotonic integer key of an f32 (`-0.0` and `0.0` map to 0).
fn key32(x: f32) -> i64 {
    let b = x.to_bits();
    if b & 0x8000_0000 != 0 {
        -i64::from(b & 0x7fff_ffff)
    } else {
        i64::from(b)
    }
}

fn key64(x: f64) -> i128 {
    let b = x.to_bits();
    if b & 0x8000_0000_0000_0000 != 0 {
        -i128::from(b & 0x7fff_ffff_ffff_ffff)
    } else {
        i128::from(b)
    }
}

/// ULP distance between two f32 (NaN: `u64::MAX` unless both NaN).
pub fn ulps32(a: f32, b: f32) -> u64 {
    if a.is_nan() || b.is_nan() {
        return if a.is_nan() && b.is_nan() { 0 } else { u64::MAX };
    }
    (key32(a) - key32(b)).unsigned_abs()
}

/// ULP distance between two f64 (NaN: `u64::MAX` unless both NaN).
pub fn ulps64(a: f64, b: f64) -> u64 {
    if a.is_nan() || b.is_nan() {
        return if a.is_nan() && b.is_nan() { 0 } else { u64::MAX };
    }
    u64::try_from((key64(a) - key64(b)).unsigned_abs()).unwrap_or(u64::MAX)
}

/// Field-wise comparison. `self` is the expected (original/recorded) value.
pub trait Compare {
    fn compare(&self, actual: &Self, c: &mut Comparator);
}

macro_rules! exact_compare {
    ($($t:ty),*) => {$(
        impl Compare for $t {
            #[inline]
            fn compare(&self, actual: &Self, c: &mut Comparator) {
                c.exact(self, actual);
            }
        }
    )*};
}
exact_compare!(u8, u16, u32, u64, i8, i16, i32, i64, bool, String, UniverseId, EntityId, SectorIx, OrderKey);

impl Compare for str {
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        c.exact(self, actual);
    }
}

impl Compare for f32 {
    #[inline]
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        c.f32(*self, *actual);
    }
}

impl Compare for f64 {
    #[inline]
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        c.f64(*self, *actual);
    }
}

impl<T: Compare, const N: usize> Compare for [T; N] {
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        for (i, (e, a)) in self.iter().zip(actual.iter()).enumerate() {
            c.index(i, |c| e.compare(a, c));
        }
    }
}

impl<T: Compare + fmt::Debug> Compare for [T] {
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        if self.len() != actual.len() {
            c.structural("len", self.len().to_string(), actual.len().to_string());
        }
        for (i, (e, a)) in self.iter().zip(actual.iter()).enumerate() {
            c.index(i, |c| e.compare(a, c));
        }
    }
}

impl<T: Compare + fmt::Debug> Compare for Vec<T> {
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        self.as_slice().compare(actual.as_slice(), c);
    }
}

impl<T: Compare + fmt::Debug> Compare for Option<T> {
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        match (self, actual) {
            (Some(e), Some(a)) => e.compare(a, c),
            (None, None) => c.exact(&0u8, &0u8),
            _ => c.structural("some", format!("{self:?}"), format!("{actual:?}")),
        }
    }
}

impl<A: Compare, B: Compare> Compare for (A, B) {
    fn compare(&self, actual: &Self, c: &mut Comparator) {
        c.field("0", |c| self.0.compare(&actual.0, c));
        c.field("1", |c| self.1.compare(&actual.1, c));
    }
}

/// Implements [`Compare`] and [`crate::hash::StableHash`] field by field.
///
/// ```
/// #[derive(Clone, Default, Debug)]
/// struct Shot { heat: f32, ammo: u32 }
/// x4mp_sim::impl_fields!(Shot { heat, ammo });
/// #[derive(Clone, Default, Debug)]
/// struct Heat(f32);
/// x4mp_sim::impl_fields!(newtype Heat);
/// ```
/// A newtype compares its inner value under the enclosing path (no extra segment).
#[macro_export]
macro_rules! impl_fields {
    (newtype $t:ty) => {
        impl $crate::shadow::Compare for $t {
            fn compare(&self, actual: &Self, c: &mut $crate::shadow::Comparator) {
                $crate::shadow::Compare::compare(&self.0, &actual.0, c);
            }
        }
        impl $crate::hash::StableHash for $t {
            fn stable_hash(&self, h: &mut $crate::hash::Fnv64) {
                $crate::hash::StableHash::stable_hash(&self.0, h);
            }
        }
    };
    ($t:ty { $($f:ident),* $(,)? }) => {
        impl $crate::shadow::Compare for $t {
            fn compare(&self, actual: &Self, c: &mut $crate::shadow::Comparator) {
                $( c.field(stringify!($f), |c| $crate::shadow::Compare::compare(&self.$f, &actual.$f, c)); )*
            }
        }
        impl $crate::hash::StableHash for $t {
            fn stable_hash(&self, h: &mut $crate::hash::Fnv64) {
                $( $crate::hash::StableHash::stable_hash(&self.$f, h); )*
            }
        }
    };
}

// ---------------------------------------------------------------------------------------
// Offline replay

/// A system whose behaviour is compared against recorded engine frames.
pub trait SystemUnderTest {
    type Input;
    type Output: Compare;
    fn name(&self) -> &str;
    /// Computes the output for one recorded input. Must not depend on anything but `input`
    /// and the system's own state (which replay feeds in recorded order).
    fn run(&mut self, input: &Self::Input) -> Self::Output;
}

/// One recorded frame: the engine's input and its output.
#[derive(Debug, Clone)]
pub struct Recorded<I, O> {
    pub frame: u64,
    pub input: I,
    pub expected: O,
}

/// Result of [`replay`].
#[derive(Debug, Clone)]
pub struct ReplayReport {
    pub system: String,
    pub frames: u64,
    pub frames_mismatched: u64,
    pub first_mismatched_frame: Option<u64>,
    pub comparator: Comparator,
}

impl ReplayReport {
    pub fn is_clean(&self) -> bool {
        self.frames_mismatched == 0
    }

    /// One line: `REPLAY <system> frames=.. mismatched_frames=.. compared=.. mismatched=.. first=..`
    pub fn line(&self) -> String {
        let mut s = format!(
            "REPLAY {} frames={} mismatched_frames={} compared={} mismatched={}",
            self.system,
            self.frames,
            self.frames_mismatched,
            self.comparator.compared(),
            self.comparator.mismatched()
        );
        if let Some(d) = self.comparator.first() {
            s.push_str(&format!(" first=[{d}]"));
        }
        s
    }
}

/// Replays recorded frames in order through `sys` and compares each output with the record.
pub fn replay<S, It>(sys: &mut S, frames: It, policy: &Policy) -> ReplayReport
where
    S: SystemUnderTest,
    It: IntoIterator<Item = Recorded<S::Input, S::Output>>,
{
    let mut c = Comparator::new(policy.clone());
    let mut report = ReplayReport {
        system: sys.name().to_string(),
        frames: 0,
        frames_mismatched: 0,
        first_mismatched_frame: None,
        comparator: Comparator::new(policy.clone()),
    };
    for rec in frames {
        c.set_at(At { frame: rec.frame, ..At::default() });
        let before = c.mismatched();
        let out = sys.run(&rec.input);
        c.field(sys.name(), |c| rec.expected.compare(&out, c));
        report.frames += 1;
        if c.mismatched() != before {
            report.frames_mismatched += 1;
            report.first_mismatched_frame.get_or_insert(rec.frame);
        }
    }
    report.comparator = c;
    report
}

// ---------------------------------------------------------------------------------------
// Online shadow mode and the feature switch

/// Online shadow statistics: the original stays authoritative, ours is compared every frame.
#[derive(Debug, Clone)]
pub struct Shadow {
    name: String,
    comparator: Comparator,
    frames: u64,
    frames_mismatched: u64,
    first_mismatched_frame: Option<u64>,
}

impl Shadow {
    pub fn new(name: &str, policy: Policy) -> Shadow {
        Shadow {
            name: name.to_string(),
            comparator: Comparator::new(policy),
            frames: 0,
            frames_mismatched: 0,
            first_mismatched_frame: None,
        }
    }

    /// Compares one frame's outputs; returns true when they agree under the policy.
    pub fn check<T: Compare + ?Sized>(&mut self, at: At, original: &T, ours: &T) -> bool {
        self.comparator.set_at(at);
        let before = self.comparator.mismatched();
        let name = self.name.clone();
        self.comparator.field(&name, |c| original.compare(ours, c));
        self.frames += 1;
        let ok = self.comparator.mismatched() == before;
        if !ok {
            self.frames_mismatched += 1;
            self.first_mismatched_frame.get_or_insert(at.frame);
        }
        ok
    }

    pub fn frames(&self) -> u64 {
        self.frames
    }
    pub fn frames_mismatched(&self) -> u64 {
        self.frames_mismatched
    }
    pub fn comparator(&self) -> &Comparator {
        &self.comparator
    }

    /// `SHADOW <name> frames=.. mismatched_frames=.. compared=.. mismatched=.. [first=[...]]`
    pub fn line(&self) -> String {
        let mut s = format!(
            "SHADOW {} frames={} mismatched_frames={} compared={} mismatched={}",
            self.name,
            self.frames,
            self.frames_mismatched,
            self.comparator.compared(),
            self.comparator.mismatched()
        );
        if let Some(d) = self.comparator.first() {
            s.push_str(&format!(" first=[{d}]"));
        }
        s
    }
}

/// State of a replacement behind its switch (method step 4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SwitchState {
    /// The original runs; ours does not.
    Off,
    /// The original is authoritative; ours runs and is compared.
    Shadow,
    /// Ours is authoritative.
    Authoritative,
    /// Refused or failed; the original is authoritative for the rest of the session.
    Disabled(String),
}

/// Feature switch with the loud refusal line required by the program rules.
#[derive(Debug, Clone)]
pub struct FeatureSwitch {
    name: String,
    state: SwitchState,
}

impl FeatureSwitch {
    pub fn new(name: &str, state: SwitchState) -> FeatureSwitch {
        FeatureSwitch { name: name.to_string(), state }
    }

    pub fn state(&self) -> &SwitchState {
        &self.state
    }

    pub fn ours_runs(&self) -> bool {
        matches!(self.state, SwitchState::Shadow | SwitchState::Authoritative)
    }

    pub fn ours_authoritative(&self) -> bool {
        self.state == SwitchState::Authoritative
    }

    /// Disables the feature (sticky) and returns the log line
    /// `FEATURE <name> DISABLED reason=<reason>` (whitespace in the reason becomes `_`).
    pub fn disable(&mut self, reason: &str) -> String {
        let reason: String = reason.chars().map(|c| if c.is_whitespace() { '_' } else { c }).collect();
        self.state = SwitchState::Disabled(reason.clone());
        format!("FEATURE {} DISABLED reason={}", self.name, reason)
    }

    /// Promotes from shadow to authoritative; refused once disabled.
    pub fn promote(&mut self) -> Result<String, String> {
        match &self.state {
            SwitchState::Disabled(r) => Err(format!("FEATURE {} DISABLED reason={}", self.name, r)),
            _ => {
                self.state = SwitchState::Authoritative;
                Ok(format!("FEATURE {} ENABLED authoritative", self.name))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Debug, Clone, Default, PartialEq)]
    struct Probe {
        hull: f64,
        pos: [f32; 3],
        count: u32,
    }
    crate::impl_fields!(Probe { hull, pos, count });

    #[test]
    fn ulp_distance() {
        assert_eq!(ulps32(1.0, 1.0), 0);
        assert_eq!(ulps32(1.0, f32::from_bits(1.0f32.to_bits() + 1)), 1);
        assert_eq!(ulps32(0.0, -0.0), 0);
        assert_eq!(ulps32(f32::from_bits(1), -f32::from_bits(1)), 2);
        assert_eq!(ulps32(f32::NAN, f32::NAN), 0);
        assert_eq!(ulps32(f32::NAN, 1.0), u64::MAX);
        assert_eq!(ulps64(1.0, f64::from_bits(1.0f64.to_bits() + 3)), 3);
        assert_eq!(ulps64(-1.0, 1.0), 2 * 0x3ff0_0000_0000_0000);
    }

    #[test]
    fn exact_by_default_and_zero_sign_matters() {
        let mut c = Comparator::new(Policy::exact());
        c.compare("v", &0.0f32, &-0.0f32);
        assert_eq!(c.mismatched(), 1);
        let mut c = Comparator::new(Policy::exact().with("v", Tol::Ulps(0)));
        c.compare("v", &0.0f32, &-0.0f32);
        assert!(c.is_clean());
        assert_eq!(c.stats()["v"].within_tolerance, 1);
    }

    #[test]
    fn struct_paths_stats_and_first_difference() {
        let e = Probe { hull: 100.0, pos: [1.0, 2.0, 3.0], count: 4 };
        let mut a = e.clone();
        a.pos[1] = f32::from_bits(2.0f32.to_bits() + 2);
        a.count = 5;
        let mut c = Comparator::new(Policy::exact().with("p.pos", Tol::Ulps(1)));
        c.set_at(At { frame: 7, sector: Some(SectorIx(2)), entity: UniverseId(0x99) });
        c.compare("p", &e, &a);
        assert_eq!(c.compared(), 5);
        assert_eq!(c.mismatched(), 2);
        let first = c.first().unwrap();
        assert_eq!(first.field, "p.pos[1]");
        assert_eq!(first.ulps, Some(2));
        assert_eq!(first.at.frame, 7);
        assert!(first.to_string().starts_with("frame=7 sector=2 entity=0x99 field=p.pos[1]"), "{first}");
        assert_eq!(c.diffs()[1].field, "p.count");
        assert_eq!(c.stats()["p.pos[]"].mismatched, 1);
        assert_eq!(c.stats()["p.pos[]"].max_ulps, 2);
        // A looser rule accepts it.
        let mut c = Comparator::new(Policy::exact().with("p.pos", Tol::Ulps(2)));
        c.compare("p", &e, &Probe { count: 4, ..a });
        assert!(c.is_clean());
    }

    #[test]
    fn policy_longest_prefix_and_boundaries() {
        let p = Policy::exact().with("a", Tol::Ulps(4)).with("a.b", Tol::Abs(0.5));
        assert_eq!(p.tol_for("a.c"), Tol::Ulps(4));
        assert_eq!(p.tol_for("a.b[]"), Tol::Abs(0.5));
        assert_eq!(p.tol_for("ab"), Tol::Exact, "prefix must end at a segment boundary");
        assert_eq!(p.tol_for("x"), Tol::Exact);
    }

    #[test]
    fn abs_and_nan_rules() {
        let mut c = Comparator::new(Policy::exact().with("v", Tol::AbsOrUlps { abs: 1e-3, ulps: 0 }));
        c.compare("v", &1.0f64, &1.0005f64);
        c.compare("v", &f64::NAN, &f64::NAN);
        assert!(c.is_clean());
        c.compare("v", &f64::NAN, &1.0);
        assert_eq!(c.mismatched(), 1);
    }

    struct Doubler;
    impl SystemUnderTest for Doubler {
        type Input = Vec<f32>;
        type Output = Vec<f32>;
        fn name(&self) -> &str {
            "doubler"
        }
        fn run(&mut self, input: &Vec<f32>) -> Vec<f32> {
            input.iter().map(|v| v * 2.0).collect()
        }
    }

    #[test]
    fn replay_reports_first_mismatched_frame() {
        let frames = (0..5u64).map(|f| {
            let input = vec![f as f32, 1.5];
            let mut expected: Vec<f32> = input.iter().map(|v| v * 2.0).collect();
            if f == 3 {
                expected[1] = 3.25;
            }
            Recorded { frame: f, input, expected }
        });
        let r = replay(&mut Doubler, frames, &Policy::exact());
        assert_eq!((r.frames, r.frames_mismatched, r.first_mismatched_frame), (5, 1, Some(3)));
        assert!(
            r.line().contains("first=[frame=3 field=doubler[1] expected=3.25e0(0x40500000) actual=3e0(0x40400000)"),
            "{}",
            r.line()
        );
    }

    #[test]
    fn shadow_and_switch() {
        let mut s = Shadow::new("hull", Policy::exact());
        assert!(s.check(At { frame: 1, ..At::default() }, &1.0f64, &1.0f64));
        assert!(!s.check(At { frame: 2, ..At::default() }, &1.0f64, &2.0f64));
        assert_eq!((s.frames(), s.frames_mismatched()), (2, 1));
        assert!(s.line().starts_with("SHADOW hull frames=2 mismatched_frames=1"));
        let mut sw = FeatureSwitch::new("hull", SwitchState::Shadow);
        assert!(sw.ours_runs() && !sw.ours_authoritative());
        assert_eq!(sw.disable("first mismatch at frame 2"), "FEATURE hull DISABLED reason=first_mismatch_at_frame_2");
        assert!(!sw.ours_runs());
        assert!(sw.promote().is_err());
    }
}
