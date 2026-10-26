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
#[derive(Debug, Clone)]
pub struct Comparator {
    policy: Policy,
    at: At,
    path: String,
    norm: String,
    stack: Vec<(usize, usize)>,
    stats: BTreeMap<String, FieldStats>,
    diffs: Vec<Difference>,
    max_diffs: usize,
    compared: u64,
    mismatched: u64,
}

impl Comparator {
    pub fn new(policy: Policy) -> Comparator {
        Comparator {
            policy,
            at: At::default(),
            path: String::new(),
            norm: String::new(),
            stack: Vec::new(),
            stats: BTreeMap::new(),
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
    pub fn compare<T: Compare + ?Sized>(&mut self, name: &str, expected: &T, actual: &T) {
        self.field(name, |c| expected.compare(actual, c));
    }

    /// Runs `f` with `.name` appended to the path.
    pub fn field(&mut self, name: &str, f: impl FnOnce(&mut Comparator)) {
        self.stack.push((self.path.len(), self.norm.len()));
        if !self.path.is_empty() {
            self.path.push('.');
            self.norm.push('.');
        }
        self.path.push_str(name);
        self.norm.push_str(name);
        f(self);
        self.pop();
    }

    /// Runs `f` with `[i]` appended to the path (`[]` in the statistics key).
    pub fn index(&mut self, i: usize, f: impl FnOnce(&mut Comparator)) {
        self.stack.push((self.path.len(), self.norm.len()));
        use std::fmt::Write as _;
        let _ = write!(self.path, "[{i}]");
        self.norm.push_str("[]");
        f(self);
        self.pop();
    }

    fn pop(&mut self) {
        let (p, n) = self.stack.pop().expect("comparator path underflow");
        self.path.truncate(p);
        self.norm.truncate(n);
    }

    fn stats_mut(&mut self) -> &mut FieldStats {
        if !self.stats.contains_key(&self.norm) {
            self.stats.insert(self.norm.clone(), FieldStats::default());
        }
        self.stats.get_mut(&self.norm).expect("inserted")
    }

    fn record(&mut self, ok: bool, expected: String, actual: String, ulps: Option<u64>, abs: Option<f64>) {
        self.compared += 1;
        if !ok {
            self.mismatched += 1;
            if self.diffs.len() < self.max_diffs {
                self.diffs.push(Difference { at: self.at, field: self.path.clone(), expected, actual, ulps, abs });
            }
        }
    }

    /// Leaf comparison of a discrete value (always exact).
    pub fn exact<T: PartialEq + fmt::Debug + ?Sized>(&mut self, expected: &T, actual: &T) {
        let ok = expected == actual;
        let s = self.stats_mut();
        s.compared += 1;
        if !ok {
            s.mismatched += 1;
        }
        let (e, a) = if ok { (String::new(), String::new()) } else { (format!("{expected:?}"), format!("{actual:?}")) };
        self.record(ok, e, a, None, None);
    }

    /// Leaf comparison of an f32 under the policy.
    pub fn f32(&mut self, expected: f32, actual: f32) {
        let tol = self.policy.tol_for(&self.norm);
        let (ok, ulps, abs) = judge(
            expected.to_bits() == actual.to_bits(),
            expected.is_nan(),
            actual.is_nan(),
            ulps32(expected, actual),
            (f64::from(expected) - f64::from(actual)).abs(),
            tol,
        );
        self.float_record(ok, ulps, abs, expected.to_bits() == actual.to_bits(), || {
            (format!("{expected:e}({:#010x})", expected.to_bits()), format!("{actual:e}({:#010x})", actual.to_bits()))
        });
    }

    /// Leaf comparison of an f64 under the policy.
    pub fn f64(&mut self, expected: f64, actual: f64) {
        let tol = self.policy.tol_for(&self.norm);
        let (ok, ulps, abs) = judge(
            expected.to_bits() == actual.to_bits(),
            expected.is_nan(),
            actual.is_nan(),
            ulps64(expected, actual),
            (expected - actual).abs(),
            tol,
        );
        self.float_record(ok, ulps, abs, expected.to_bits() == actual.to_bits(), || {
            (format!("{expected:e}({:#018x})", expected.to_bits()), format!("{actual:e}({:#018x})", actual.to_bits()))
        });
    }

    fn float_record(
        &mut self,
        ok: bool,
        ulps: u64,
        abs: f64,
        same_bits: bool,
        show: impl FnOnce() -> (String, String),
    ) {
        let s = self.stats_mut();
        s.compared += 1;
        if !same_bits {
            s.max_ulps = s.max_ulps.max(ulps);
            if abs.is_finite() && abs > s.max_abs {
                s.max_abs = abs;
            }
            if ok {
                s.within_tolerance += 1;
            } else {
                s.mismatched += 1;
            }
        }
        let (e, a) = if ok { (String::new(), String::new()) } else { show() };
        self.record(ok, e, a, (!same_bits).then_some(ulps), (!same_bits).then_some(abs));
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
    pub fn stats(&self) -> &BTreeMap<String, FieldStats> {
        &self.stats
    }
    pub fn policy(&self) -> &Policy {
        &self.policy
    }

    /// Clears counts, statistics and differences (keeps the policy).
    pub fn reset(&mut self) {
        self.stats.clear();
        self.diffs.clear();
        self.compared = 0;
        self.mismatched = 0;
    }

    /// Multi-line summary: totals, every field with mismatches or tolerance use, first diffs.
    pub fn summary(&self) -> String {
        let mut out = format!("compared={} mismatched={}\n", self.compared, self.mismatched);
        for (field, s) in &self.stats {
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
