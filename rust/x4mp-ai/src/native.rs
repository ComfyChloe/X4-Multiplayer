//! Native bracket captures (native/engine/aicapture.h, `capture=bracket`) and the checks that need no
//! captured values.
//!
//! A bracket capture has, per interpreter bracket, the game time, the wake mask, the TLS RNG state at
//! both ends, the pc and the blocking action (keyword id, XML line, timed wake), and per event delivery
//! the RNG states around it and the verdict (wake mask, queued handlers). Because the engine LCG is a
//! bijection, the exact number of draws of every bracket follows from its two states; because a `<wait
//! min max>` draws last before it yields, its wake time follows from the end state. That gives three
//! native checks of the model in reports/REWRITE-AI-ORDERS.md without any query value:
//! 1. wake times of every `<wait>` (exact and random) bit-exact;
//! 2. draw counts per bracket class (script, previous yield, this yield) are constant where the
//!    model says they are, and equal to the model's script draws plus a constant engine share;
//! 3. delivery draws (conditions): 0 for global handlers, 0 or 1 for move.seekenemies's move_to.

use crate::rng::{steps_between, Stream, FLOAT_SCALE};
use crate::trace::{Record, Trace};
use crate::val::{Uid, Val};
use std::collections::BTreeMap;
use std::fmt::Write as _;

/// Keyword ids of the blocking actions and events the checks name (x4-re/md_keywords.csv).
pub mod kw {
    pub const WAIT: u32 = 2558;
    pub const MOVE_TO: u32 = 2488;
    pub const RUN_SCRIPT: u32 = 2531;
    pub const RUN_ORDER_SCRIPT: u32 = 2541;
    pub const EV_TARGET_INVALID: i64 = 1340;
    pub const EV_GRAVIDAR_SCANNED: i64 = 1286;
    pub const EV_SIGNALLED: i64 = 1408;
    pub const EV_CHANGED_ATTENTION: i64 = 1261;
    pub const EV_ORDER_READY: i64 = 1281;
    pub const EV_ATTACKED: i64 = 1175;

    pub fn action_name(id: u32) -> &'static str {
        match id {
            WAIT => "wait",
            MOVE_TO => "move_to",
            RUN_SCRIPT => "run_script",
            RUN_ORDER_SCRIPT => "run_order_script",
            0 => "-",
            _ => "other",
        }
    }
}

/// The capture sessions in a lane's engine-output.log (one per `X4AITRACE` header; the probe restarts
/// the capture when it re-arms), each without the `AITR\t` prefix.
pub fn extract_sessions(log: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for l in log.lines() {
        if let Some(rest) = l.strip_prefix("AITR\t") {
            if rest.starts_with("X4AITRACE\t") || out.is_empty() {
                out.push(String::new());
            }
            let cur = out.last_mut().expect("session");
            cur.push_str(rest);
            cur.push('\n');
        }
    }
    out
}

/// Extract the capture lines (`AITR\t...`) from a lane's engine-output.log, without the prefix.
pub fn extract(log: &str) -> String {
    let mut out = String::new();
    for l in log.lines() {
        if let Some(rest) = l.strip_prefix("AITR\t") {
            out.push_str(rest);
            out.push('\n');
        }
    }
    out
}

#[derive(Clone, Debug, PartialEq)]
pub struct Bracket {
    pub ent: Uid,
    pub irq: bool,
    pub script: String,
    pub t: f64,
    pub mask: u32,
    pub rng_in: u64,
    pub rng_out: u64,
    pub pc_in: u64,
    pub pc_out: u64,
    pub yielded: bool,
    pub kw: u32,
    pub line: u32,
    pub until: f64,
    /// Exact draws, if within the cap.
    pub steps: Option<u64>,
    pub trace_line: usize,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Delivery {
    pub ent: Uid,
    pub kw: i64,
    pub global: bool,
    pub t: f64,
    pub steps: Option<u64>,
    pub mask: u32,
    pub mask_after: u32,
    pub matched: Vec<String>,
    /// Script of the entity's last bracket before the delivery (the frame on top).
    pub top: String,
    pub trace_line: usize,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Capture {
    pub entities: Vec<Uid>,
    pub brackets: Vec<Bracket>,
    pub deliveries: Vec<Delivery>,
    pub unmatched: u64,
}

const STEP_CAP: u64 = 1 << 20;

fn f(r: &Record, k: &str) -> Result<f64, String> {
    r.f64_field(k)
}
fn u(r: &Record, k: &str) -> Result<u64, String> {
    r.u64_field(k)
}
fn i(r: &Record, k: &str) -> i64 {
    r.get(k).and_then(|v| v.as_int()).unwrap_or(0)
}

/// Parse a bracket capture (the extracted text). Brackets are paired per entity (`run`..`yield`,
/// `irun`..`irend`, nesting by entity because a run cannot interleave with another of the same entity).
pub fn parse(text: &str) -> Result<Capture, String> {
    let t = Trace::parse(text)?;
    if t.header_value("capture") != Some("bracket") || t.header_value("source") != Some("native") {
        return Err("not a native bracket capture".into());
    }
    let mut c = Capture::default();
    let mut open: BTreeMap<u64, Vec<Bracket>> = BTreeMap::new();
    let mut top: BTreeMap<u64, String> = BTreeMap::new();
    for r in &t.records {
        match r.tag.as_str() {
            "track" => c.entities.push(r.ent),
            "run" | "irun" => {
                top.insert(r.ent.0, r.name.clone());
                let b = Bracket {
                    ent: r.ent,
                    irq: r.tag == "irun",
                    script: r.name.clone(),
                    t: f(r, "t")?,
                    mask: u(r, "mask")? as u32,
                    rng_in: u(r, "rng")?,
                    rng_out: 0,
                    pc_in: i(r, "pc") as u64,
                    pc_out: 0,
                    yielded: false,
                    kw: 0,
                    line: 0,
                    until: -1.0,
                    steps: None,
                    trace_line: r.line,
                };
                open.entry(r.ent.0).or_default().push(b);
            }
            "yield" | "irend" => {
                let Some(mut b) = open.get_mut(&r.ent.0).and_then(|v| v.pop()) else {
                    c.unmatched += 1;
                    continue;
                };
                b.rng_out = u(r, "rng")?;
                b.pc_out = i(r, "pc") as u64;
                if r.tag == "yield" {
                    b.yielded = r.get("yielded") == Some(&Val::Bool(true));
                    b.kw = i(r, "kw") as u32;
                    b.line = i(r, "line") as u32;
                    b.until = f(r, "until")?;
                }
                b.steps = steps_between(b.rng_in, b.rng_out, STEP_CAP);
                c.brackets.push(b);
            }
            "ev" => {
                let kwid = r.name.strip_prefix("kw:").and_then(|s| s.parse::<i64>().ok()).unwrap_or(-1);
                let matched = r
                    .get("matched")
                    .and_then(|v| v.as_list())
                    .map(|l| {
                        l.iter().filter_map(|x| if let Val::Str(s) = x { Some(s.clone()) } else { None }).collect()
                    })
                    .unwrap_or_default();
                c.deliveries.push(Delivery {
                    ent: r.ent,
                    kw: kwid,
                    global: r.get("path").and_then(|v| v.as_key()) == Some("global"),
                    t: f(r, "t")?,
                    steps: steps_between(u(r, "rng")?, u(r, "rng_after")?, STEP_CAP),
                    mask: u(r, "mask")? as u32,
                    mask_after: u(r, "mask_after")? as u32,
                    matched,
                    top: top.get(&r.ent.0).cloned().unwrap_or_else(|| "?".into()),
                    trace_line: r.line,
                });
            }
            _ => {}
        }
    }
    c.unmatched += open.values().map(|v| v.len() as u64).sum::<u64>();
    Ok(c)
}

/// `<wait>` actions of the ported scripts and orders.base: (script, XML line) -> duration rule.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum WaitRule {
    Exact(f64),
    Range(f64, f64),
    Forever,
}

pub fn wait_rule(script: &str, line: u32) -> Option<WaitRule> {
    Some(match (script, line) {
        ("order.fight.patrol", 317) => WaitRule::Range(0.001, 1.0),
        ("order.fight.patrol", 480 | 509) => WaitRule::Range(1.0, 2.0),
        ("move.seekenemies", 146) => WaitRule::Forever,
        ("orders.base", 121) => WaitRule::Exact(60.0),
        ("orders.base", 144 | 386) => WaitRule::Exact(0.1),
        ("orders.base", 366) => WaitRule::Range(0.25, 1.0),
        _ => return None,
    })
}

/// The wake time the model predicts for a bracket that yielded on a `<wait>`: the wait's random draw
/// (if any) is the last draw of the bracket, so it used the end state.
pub fn predicted_until(rule: WaitRule, t: f64, rng_out: u64) -> f64 {
    match rule {
        WaitRule::Forever => -1.0,
        WaitRule::Exact(d) => d + t,
        WaitRule::Range(min, max) => {
            let r = ((rng_out as u32) as f32) * FLOAT_SCALE * ((max - min) as f32);
            ((r as f64) + min) + t
        }
    }
}

/// Outcome of a chance roll made from `state_before` (the interpreter's comparison, see `rng`).
fn chance_pass(state_before: u64, chance: f32) -> bool {
    Stream::from_state(state_before).chance(chance)
}

/// Results of the checks.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct NativeReport {
    pub brackets: u64,
    pub deliveries: u64,
    pub entities: usize,
    pub unmatched: u64,
    /// (script, line) -> (checked, matched).
    pub waits: BTreeMap<(String, u32), (u64, u64)>,
    pub first_wait_mismatch: Option<String>,
    /// Bracket class "script prev->end" -> steps histogram.
    pub classes: BTreeMap<String, BTreeMap<u64, u64>>,
    /// Patrol leg (L509 wait -> L493 call): engine draws = steps - model script draws.
    pub patrol_leg_engine_draws: BTreeMap<i64, u64>,
    /// Delivery class "path kw verdict" -> steps histogram.
    pub delivery_classes: BTreeMap<String, BTreeMap<u64, u64>>,
}

impl NativeReport {
    pub fn render(&self) -> String {
        let mut s = String::new();
        let _ = writeln!(
            s,
            "brackets={} deliveries={} entities={} unmatched={}",
            self.brackets, self.deliveries, self.entities, self.unmatched
        );
        for ((sc, l), (n, ok)) in &self.waits {
            let _ = writeln!(s, "wait {sc} L{l}: {ok}/{n} wake times bit-exact");
        }
        if let Some(m) = &self.first_wait_mismatch {
            let _ = writeln!(s, "first wait mismatch: {m}");
        }
        for (k, h) in &self.classes {
            let _ = writeln!(s, "class {k}: steps {}", hist(h));
        }
        if !self.patrol_leg_engine_draws.is_empty() {
            let mut t = String::new();
            for (d, n) in &self.patrol_leg_engine_draws {
                let _ = write!(t, "{d}:{n} ");
            }
            let _ = writeln!(s, "patrol leg engine draws (create_position + get_safe_pos): {}", t.trim_end());
        }
        for (k, h) in &self.delivery_classes {
            let _ = writeln!(s, "delivery {k}: steps {}", hist(h));
        }
        s
    }
}

fn hist(h: &BTreeMap<u64, u64>) -> String {
    h.iter().map(|(k, v)| format!("{k}:{v}")).collect::<Vec<_>>().join(" ")
}

fn end_label(b: &Bracket) -> String {
    if b.yielded {
        format!("{}@{}", kw::action_name(b.kw), b.line)
    } else if b.irq {
        "irq".into()
    } else {
        "return".into()
    }
}

/// Run the checks on a parsed capture.
pub fn check(c: &Capture) -> NativeReport {
    let mut rep = NativeReport {
        brackets: c.brackets.len() as u64,
        deliveries: c.deliveries.len() as u64,
        entities: c.entities.len(),
        unmatched: c.unmatched,
        ..Default::default()
    };
    // previous end per (entity, script frame)
    let mut prev: BTreeMap<(u64, String), String> = BTreeMap::new();
    let mut next_resupply: BTreeMap<u64, f64> = BTreeMap::new();
    for b in &c.brackets {
        let key = (b.ent.0, b.script.clone());
        let end = end_label(b);
        // A fresh instance (start wake at pc 0) has no previous yield.
        let fresh = !b.irq && b.pc_in == 0 && b.mask & 1 != 0;
        let before =
            if fresh { "start".to_string() } else { prev.get(&key).cloned().unwrap_or_else(|| "start".into()) };
        if !b.irq {
            prev.insert(key, end.clone());
        }
        let class = format!("{} {}{} -> {}", b.script, if b.irq { "irq " } else { "" }, before, end);
        if let Some(n) = b.steps {
            *rep.classes.entry(class).or_default().entry(n).or_default() += 1;
        }
        // 1. wait wake times
        if b.yielded && b.kw == kw::WAIT {
            if let Some(rule) = wait_rule(&b.script, b.line) {
                let want = predicted_until(rule, b.t, b.rng_out);
                let e = rep.waits.entry((b.script.clone(), b.line)).or_default();
                e.0 += 1;
                if want.to_bits() == b.until.to_bits() {
                    e.1 += 1;
                } else if rep.first_wait_mismatch.is_none() {
                    rep.first_wait_mismatch = Some(format!(
                        "capture line {}: {} L{} t={} until={} ({:016x}) predicted {} ({:016x})",
                        b.trace_line,
                        b.script,
                        b.line,
                        b.t,
                        b.until,
                        b.until.to_bits(),
                        want,
                        want.to_bits()
                    ));
                }
            }
        }
        // 2. patrol leg after the L509 wait: L511 roll, [L512], L430, L485 range, + engine draws
        if b.script == "order.fight.patrol" && !b.irq && before == "wait@509" && end == "run_script@493" {
            if let Some(n) = b.steps {
                let pass = chance_pass(b.rng_in, 30.0);
                let due = b.t > next_resupply.get(&b.ent.0).copied().unwrap_or(0.0);
                let script = 1 + u64::from(pass && due) + 1 + 1;
                if pass && due {
                    next_resupply.insert(b.ent.0, b.t + 1800.0);
                }
                *rep.patrol_leg_engine_draws.entry(n as i64 - script as i64).or_default() += 1;
            }
        }
    }
    for d in &c.deliveries {
        let verdict = if d.global {
            if d.matched.is_empty() {
                "none".to_string()
            } else {
                format!("matched {}", d.matched.join(","))
            }
        } else if d.mask_after & 4 != 0 && d.mask & 4 == 0 {
            "interrupt".into()
        } else if d.mask_after & 2 != 0 && d.mask & 2 == 0 {
            "done".into()
        } else {
            "none".into()
        };
        let k = format!("{} kw{} top={} {}", if d.global { "global" } else { "block" }, d.kw, d.top, verdict);
        if let Some(n) = d.steps {
            *rep.delivery_classes.entry(k).or_default().entry(n).or_default() += 1;
        }
    }
    rep
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rng::step;

    fn bits(v: f64) -> String {
        format!("{:016x}", v.to_bits())
    }

    /// A synthetic capture built from the model itself: tests parsing, pairing and arithmetic only.
    fn synthetic() -> String {
        let mut s = String::from(
            "X4AITRACE\t1\tbuild=23660954\tsource=native\tcapture=bracket\tfilter=order.fight.patrol\tmax=8\n",
        );
        s.push_str("track\tc:0x7001\torder.fight.patrol\tslot=i:0\n");
        // start bracket: L199 roll, L317 wait (2 draws)
        let r0 = 0x1234_5678_9abc_def0u64;
        let r1 = step(step(r0));
        let t0 = 100.0;
        let until = predicted_until(WaitRule::Range(0.001, 1.0), t0, r1);
        s.push_str(&format!(
            "run\tc:0x7001\torder.fight.patrol\tt=d:{}\tmask=x:1\trng=x:{r0:016x}\tpc=i:0\tctx=x:1\n",
            bits(t0)
        ));
        s.push_str(&format!(
            "yield\tc:0x7001\torder.fight.patrol\trng=x:{r1:016x}\tpc=i:12\tyielded=b:1\tkw=i:2558\tline=i:317\tuntil=d:{}\n",
            bits(until)
        ));
        // after seek returned: L509 wait (1 draw)
        let r2 = 0x0bad_cafe_0000_0001u64;
        let r3 = step(r2);
        let t1 = 130.0;
        let until2 = predicted_until(WaitRule::Range(1.0, 2.0), t1, r3);
        s.push_str(&format!(
            "run\tc:0x7001\torder.fight.patrol\tt=d:{}\tmask=x:20\trng=x:{r2:016x}\tpc=i:40\tctx=x:1\n",
            bits(t1)
        ));
        s.push_str(&format!(
            "yield\tc:0x7001\torder.fight.patrol\trng=x:{r3:016x}\tpc=i:41\tyielded=b:1\tkw=i:2558\tline=i:509\tuntil=d:{}\n",
            bits(until2)
        ));
        // leg: L511 roll, [L512], L430, L485 + 3 engine draws
        let r4 = 0x0bad_cafe_0000_0002u64;
        let pass = chance_pass(r4, 30.0);
        let n = 3 + u64::from(pass) + 3;
        let mut r5 = r4;
        for _ in 0..n {
            r5 = step(r5);
        }
        s.push_str(&format!(
            "run\tc:0x7001\torder.fight.patrol\tt=d:{}\tmask=x:2\trng=x:{r4:016x}\tpc=i:41\tctx=x:1\n",
            bits(132.0)
        ));
        s.push_str(&format!(
            "yield\tc:0x7001\torder.fight.patrol\trng=x:{r5:016x}\tpc=i:60\tyielded=b:1\tkw=i:2531\tline=i:493\tuntil=d:{}\n",
            bits(-1.0)
        ));
        // deliveries: a gravidar scan without enemy draws once (distress roll); a global target invalid
        let r6 = 0x0123_0000_0000_0001u64;
        s.push_str(&format!(
            "ev\tc:0x7001\tkw:1286\tpath=k:block\tt=d:{}\trng=x:{r6:016x}\trng_after=x:{:016x}\tobject=c:0x7001\tparam=n\tparam2=n\tparam3=n\tmask=x:0\tmask_after=x:0\tmatched=l:[]\n",
            bits(133.0),
            step(r6)
        ));
        s.push_str(&format!(
            "ev\tc:0x7001\tkw:1340\tpath=k:global\tt=d:{}\trng=x:{r6:016x}\trng_after=x:{r6:016x}\tobject=c:0x7001\tparam=n\tparam2=n\tparam3=n\tmask=x:0\tmask_after=x:0\tmatched=l:[s:order.fight.patrol/9]\n",
            bits(133.0)
        ));
        s.push_str("# end lines=9\n");
        s
    }

    #[test]
    fn sessions_split_at_headers() {
        let log = "AITR\tX4AITRACE\t1\tbuild=23660954\nAITR\ttrack\tc:0x1\tx\tslot=i:0\nnoise\nAITR\tX4AITRACE\t1\tbuild=23660954\nAITR\t# end\n";
        let s = extract_sessions(log);
        assert_eq!(s.len(), 2);
        assert!(s[0].ends_with("slot=i:0\n") && s[1].starts_with("X4AITRACE"));
    }

    #[test]
    fn extract_strips_the_prefix_and_ignores_other_output() {
        let log = "Starting X4\nAITR\tX4AITRACE\t1\tbuild=23660954\nnoise\nAITR\ttrack\tc:0x1\tx\tslot=i:0\n";
        assert_eq!(extract(log), "X4AITRACE\t1\tbuild=23660954\ntrack\tc:0x1\tx\tslot=i:0\n");
    }

    #[test]
    fn synthetic_capture_checks_out() {
        let c = parse(&synthetic()).unwrap();
        assert_eq!(c.brackets.len(), 3);
        assert_eq!(c.deliveries.len(), 2);
        assert_eq!(c.unmatched, 0);
        let r = check(&c);
        assert_eq!(r.waits[&("order.fight.patrol".to_string(), 317)], (1, 1));
        assert_eq!(r.waits[&("order.fight.patrol".to_string(), 509)], (1, 1));
        assert!(r.first_wait_mismatch.is_none());
        assert_eq!(r.patrol_leg_engine_draws.get(&3), Some(&1));
        assert_eq!(r.classes["order.fight.patrol start -> wait@317"].get(&2), Some(&1));
        assert_eq!(r.delivery_classes["block kw1286 top=order.fight.patrol none"].get(&1), Some(&1));
        assert_eq!(
            r.delivery_classes["global kw1340 top=order.fight.patrol matched order.fight.patrol/9"].get(&0),
            Some(&1)
        );
        let text = r.render();
        assert!(text.contains("wait order.fight.patrol L317: 1/1"), "{text}");
    }

    #[test]
    fn a_wrong_wake_time_is_reported() {
        let s: Vec<String> = synthetic()
            .lines()
            .map(|l| match l.find("until=d:") {
                Some(p) if l.contains("line=i:317") => format!("{}until=d:{}", &l[..p], bits(1.0)),
                _ => l.to_string(),
            })
            .collect();
        let r = check(
            &parse(&s.join(
                "
",
            ))
            .unwrap(),
        );
        assert_eq!(r.waits[&("order.fight.patrol".to_string(), 317)], (1, 0));
        assert!(r.first_wait_mismatch.unwrap().contains("L317"));
    }
}
