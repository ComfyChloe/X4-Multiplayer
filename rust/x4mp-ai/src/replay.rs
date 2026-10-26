//! Offline replay of an X4AITRACE against the native behaviours.
//!
//! Inputs from the trace: facts, order starts, the TLS RNG state at the start of every interpreter
//! bracket (`run` = `ExecState_Run`, `irun` = interrupt runner 0xb58ce0), query answers with their
//! engine-side draw counts, event deliveries with the engine's verdict, aborts by orders.base, and
//! everything an opaque (not ported) frame or handler does. Outputs compared bit-exactly: rolls (line,
//! chance, outcome), random range values, query arguments, effects, yields (kind, wake time, called
//! script, return values), interrupt outcomes, and the RNG state at the end of every bracket. The first
//! difference stops that entity's replay and is reported with its trace line.

use crate::orders::patrol::{Patrol, PatrolParams};
use crate::orders::start_params;
use crate::rng::Stream;
use crate::runtime::{Agent, BlockVerdict, Event, Frame, GlobalVerdict, Outcome, RetVals, Yield};
use crate::trace::{Record, Trace};
use crate::val::{OrderId, Time, Uid, Val};
use crate::world::{Answer, Effect, Facts, Io, Prop, Query, Stop, R};
use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};

/// World facts as set by `fact` records.
#[derive(Clone, Debug, Default)]
pub struct FactStore {
    pub now: Time,
    facts: HashMap<(Uid, Prop), Val>,
    order_facts: HashMap<(OrderId, Prop), Val>,
}

impl FactStore {
    /// `fact <subject> <prop> v=<value>`; order facts: `fact n <prop> order=o:.. v=..`.
    pub fn apply(&mut self, r: &Record) -> Result<(), String> {
        let prop =
            Prop::parse(&r.name).ok_or_else(|| format!("line {}: unknown fact property {:?}", r.line, r.name))?;
        let v = r.get("v").cloned().ok_or_else(|| format!("line {}: fact needs v=", r.line))?;
        if let Some(o) = r.get("order") {
            let o = o.as_order().ok_or_else(|| format!("line {}: order= must be o:..", r.line))?;
            self.order_facts.insert((o, prop), v);
        } else {
            self.facts.insert((r.ent, prop), v);
        }
        Ok(())
    }

    pub fn set(&mut self, s: Uid, p: Prop, v: Val) {
        self.facts.insert((s, p), v);
    }

    pub fn set_order(&mut self, o: OrderId, p: Prop, v: Val) {
        self.order_facts.insert((o, p), v);
    }
}

impl Facts for FactStore {
    fn now(&self) -> Time {
        self.now
    }
    fn get(&self, s: Uid, p: Prop) -> Val {
        if p == Prop::Exists {
            // A component exists when it has a class fact and no explicit exists=false.
            if let Some(v) = self.facts.get(&(s, Prop::Exists)) {
                return v.clone();
            }
            return Val::Bool(!s.is_null() && self.facts.contains_key(&(s, Prop::Class)));
        }
        self.facts.get(&(s, p)).cloned().unwrap_or(Val::Null)
    }
    fn order_get(&self, o: OrderId, p: Prop) -> Val {
        self.order_facts.get(&(o, p)).cloned().unwrap_or(Val::Null)
    }
}

/// [`Facts`] over a shared [`FactStore`] (facts may change between the records of one run).
pub struct FactView<'a>(pub &'a RefCell<FactStore>);

impl Facts for FactView<'_> {
    fn now(&self) -> Time {
        self.0.borrow().now
    }
    fn get(&self, s: Uid, p: Prop) -> Val {
        Facts::get(&*self.0.borrow(), s, p)
    }
    fn order_get(&self, o: OrderId, p: Prop) -> Val {
        Facts::order_get(&*self.0.borrow(), o, p)
    }
}

/// Record classes a trace contains (header `records=`); only those are compared.
#[derive(Clone, Copy, Debug)]
pub struct Checks {
    pub roll: bool,
    pub rnd: bool,
    pub query: bool,
    pub effect: bool,
}

impl Checks {
    pub fn from_trace(t: &Trace) -> Checks {
        match t.header_value("records") {
            None => Checks { roll: true, rnd: true, query: true, effect: true },
            Some(list) => {
                let has = |k: &str| list.split(',').any(|x| x == k);
                Checks { roll: has("roll"), rnd: has("rnd"), query: has("q"), effect: has("fx") }
            }
        }
    }
}

/// [`Io`] that reads the records following the current position, for one entity.
struct TraceIo<'a> {
    recs: &'a [Record],
    pos: usize,
    ent: Uid,
    store: &'a RefCell<FactStore>,
    checks: Checks,
    /// Records met inside this entity's bracket that the driver handles afterwards, in trace order:
    /// other entities' records and this entity's nested deliveries (`ev` with `nested=b:1`).
    deferred: Vec<usize>,
    /// `false` while an opaque bracket's records are consumed without comparison.
    compare: bool,
}

fn mismatch(r: Option<&Record>, msg: String) -> Stop {
    match r {
        Some(r) => Stop::Mismatch(format!("trace line {}: {msg}", r.line)),
        None => Stop::Mismatch(format!("end of trace: {msg}")),
    }
}

impl<'a> TraceIo<'a> {
    /// Next record of this entity with one of `tags`, applying facts and deferring the rest.
    fn next(&mut self, tags: &[&str]) -> R<&'a Record> {
        loop {
            let Some(r) = self.recs.get(self.pos) else {
                return Err(mismatch(None, format!("expected {tags:?}")));
            };
            self.pos += 1;
            if r.tag == "fact" {
                self.store.borrow_mut().apply(r).map_err(Stop::Mismatch)?;
                continue;
            }
            if r.ent != self.ent || (r.tag == "ev" && r.get("nested") == Some(&Val::Bool(true))) {
                self.deferred.push(self.pos - 1);
                continue;
            }
            if tags.contains(&r.tag.as_str()) {
                return Ok(r);
            }
            return Err(mismatch(Some(r), format!("expected {tags:?}, trace has {}", r.tag)));
        }
    }
}

fn fmt_args(a: &[(String, Val)]) -> String {
    a.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join(" ")
}

impl Io for TraceIo<'_> {
    fn roll(&mut self, _ent: Uid, line: u32, chance: f32, pass: bool) -> R<()> {
        if !self.compare || !self.checks.roll {
            return Ok(());
        }
        let r = self.next(&["roll"])?;
        let want = (r.get("line").and_then(|v| v.as_int()), r.get("chance").cloned(), r.get("pass").cloned());
        if want != (Some(line as i64), Some(Val::F32(chance)), Some(Val::Bool(pass))) {
            return Err(mismatch(
                Some(r),
                format!(
                    "roll: native line={line} chance={} pass={pass}, trace {}",
                    Val::F32(chance),
                    fmt_args(&r.args)
                ),
            ));
        }
        Ok(())
    }

    fn rnd(&mut self, _ent: Uid, line: u32, v: f64) -> R<()> {
        if !self.compare || !self.checks.rnd {
            return Ok(());
        }
        let r = self.next(&["rnd"])?;
        let ok = r.get("line").and_then(|v| v.as_int()) == Some(line as i64)
            && r.get("v").and_then(|v| v.as_f64()).map(f64::to_bits) == Some(v.to_bits());
        if !ok {
            return Err(mismatch(
                Some(r),
                format!("rnd: native line={line} v={}, trace {}", Val::F64(v), fmt_args(&r.args)),
            ));
        }
        Ok(())
    }

    fn query(&mut self, _ent: Uid, line: u32, q: &Query, rng: &mut Stream) -> R<Answer> {
        let r = self.next(&["q"])?;
        let mine = q.to_record(self.ent);
        if self.compare && self.checks.query && (r.name != mine.name || r.args != mine.args) {
            return Err(mismatch(
                Some(r),
                format!(
                    "query at L{line}: native {} {}, trace {} {}",
                    mine.name,
                    fmt_args(&mine.args),
                    r.name,
                    fmt_args(&r.args)
                ),
            ));
        }
        let a = q.answer_from(r).map_err(Stop::Mismatch)?;
        let draws = r.result("draws").and_then(|v| v.as_int()).unwrap_or(0);
        rng.skip(draws as u64);
        Ok(a)
    }

    fn effect(&mut self, _ent: Uid, line: u32, e: &Effect, _rng: &mut Stream) -> R<()> {
        if self.compare && self.checks.effect {
            let r = self.next(&["fx"])?;
            let mine = e.to_record(self.ent);
            if r.name != mine.name || r.args != mine.args {
                return Err(mismatch(
                    Some(r),
                    format!(
                        "effect at L{line}: native {} {}, trace {} {}",
                        mine.name,
                        fmt_args(&mine.args),
                        r.name,
                        fmt_args(&r.args)
                    ),
                ));
            }
        }
        Ok(())
    }
}

/// Per-entity replay result.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct EntityReport {
    pub runs: u64,
    pub opaque_runs: u64,
    pub deliveries: u64,
    pub interrupt_runs: u64,
    pub yields_checked: u64,
    pub rng_checked: u64,
    pub effects: u64,
    pub first_mismatch: Option<String>,
    pub unsupported: Option<String>,
}

/// Whole-trace replay result.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Report {
    pub entities: BTreeMap<u64, EntityReport>,
}

impl Report {
    pub fn ok(&self) -> bool {
        self.entities.values().all(|e| e.first_mismatch.is_none() && e.unsupported.is_none())
    }
    pub fn summary(&self) -> String {
        let mut s = String::new();
        for (e, r) in &self.entities {
            s.push_str(&format!(
                "entity 0x{e:x}: runs={} opaque_runs={} deliveries={} interrupt_runs={} yields={} rng_checks={} effects={} {}\n",
                r.runs,
                r.opaque_runs,
                r.deliveries,
                r.interrupt_runs,
                r.yields_checked,
                r.rng_checked,
                r.effects,
                match (&r.first_mismatch, &r.unsupported) {
                    (Some(m), _) => format!("MISMATCH {m}"),
                    (None, Some(u)) => format!("UNSUPPORTED {u}"),
                    (None, None) => "OK".into(),
                }
            ));
        }
        s
    }
}

fn yield_record(ent: Uid, script: &str, y: &Yield) -> Record {
    let r = Record::new("yield", ent, script).arg("kind", Val::key(y.kind()));
    match y {
        Yield::Wait { until } => r.arg("until", until.map(Val::F64).unwrap_or(Val::Null)),
        Yield::MoveTo => r,
        Yield::Call { script, .. } => r.arg("script", Val::Str((*script).into())),
        Yield::Return(vals) => vals.iter().fold(r, |r, (k, v)| r.arg(k, v.clone())),
    }
}

fn opaque_yield(r: &Record) -> R<Yield> {
    let kind =
        r.get("kind").and_then(|v| v.as_key()).ok_or_else(|| mismatch(Some(r), "yield needs kind=k:..".into()))?;
    Ok(match kind {
        "wait" => Yield::Wait { until: r.get("until").and_then(|v| v.as_f64()) },
        "moveto" => Yield::MoveTo,
        "call" => Yield::Call { script: "opaque", params: Vec::new() },
        "return" => Yield::Return(
            r.args
                .iter()
                .filter(|(k, _)| k != "kind" && k != "rng")
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect::<RetVals>(),
        ),
        other => return Err(mismatch(Some(r), format!("unknown yield kind {other}"))),
    })
}

fn outcome_fields(out: &Outcome) -> (&'static str, Val) {
    match out {
        Outcome::Nothing => ("handled", Val::Null),
        Outcome::AbortCalled { label } => ("abort", label.map(Val::key).unwrap_or(Val::Null)),
        Outcome::BlockDone => ("block", Val::Null),
        Outcome::Resume { label } => ("resume", Val::key(label)),
    }
}

fn event_of(r: &Record) -> Event {
    Event {
        name: r.name.clone(),
        object: r.get("object").and_then(|v| v.as_uid()).unwrap_or(Uid::NULL),
        param: r.get("param").cloned().unwrap_or(Val::Null),
        param2: r.get("param2").cloned().unwrap_or(Val::Null),
        param3: r.get("param3").cloned().unwrap_or(Val::Null),
    }
}

/// Replay one record (and the records of its bracket) for entity `ent`.
fn step(
    r: &Record,
    io: &mut TraceIo<'_>,
    agents: &mut HashMap<Uid, Agent>,
    view: &FactView<'_>,
    rep: &mut EntityReport,
) -> R<()> {
    let ent = r.ent;
    match r.tag.as_str() {
        "start" => {
            let ps = start_params(r);
            let ship = view.uid_of(ent, Prop::Ship);
            let radar = view.length(ship, Prop::MaxRadarRange);
            let frame = match r.name.as_str() {
                "order.fight.patrol" => {
                    Frame::Patrol(Box::new(Patrol::new(PatrolParams::from_params(&ps, radar)?, ship)))
                }
                other => Frame::Opaque { script: other.to_string() },
            };
            let mut a = Agent::new(ent, 0);
            a.start_order(frame, view, io)?;
            agents.insert(ent, a);
        }
        "run" => {
            let a = agents.get_mut(&ent).ok_or_else(|| mismatch(Some(r), "run of an entity without start".into()))?;
            a.rng.reset_to(r.u64_field("rng").map_err(Stop::Mismatch)?);
            io.store.borrow_mut().now = r.f64_field("t").map_err(Stop::Mismatch)?;
            let top = a.top_name().ok_or_else(|| mismatch(Some(r), "run without frames".into()))?.to_string();
            if top != r.name {
                return Err(mismatch(Some(r), format!("run of {} but native top frame is {top}", r.name)));
            }
            let wake = a.next_wake_name();
            if let Some(Val::Key(w)) = r.get("wake") {
                if w != wake {
                    return Err(mismatch(Some(r), format!("wake {w} but native frame expects {wake}")));
                }
            }
            if matches!(a.stack.last(), Some(Frame::Opaque { .. })) {
                // Consume the opaque run up to its yield; take its RNG state and control flow.
                rep.opaque_runs += 1;
                io.compare = false;
                let y = loop {
                    let n = io.next(&["q", "fx", "roll", "rnd", "yield"])?;
                    if n.tag == "yield" {
                        break n;
                    }
                };
                io.compare = true;
                a.rng.reset_to(y.u64_field("rng").map_err(Stop::Mismatch)?);
                let yv = opaque_yield(y)?;
                let _ = a.next_wake();
                match (&yv, y.get("script")) {
                    (Yield::Call { .. }, Some(Val::Str(callee))) => a.call_opaque(callee),
                    _ => a.after_yield(&yv),
                }
            } else {
                rep.runs += 1;
                let y = a.run(view, io)?;
                let yr = io.next(&["yield"])?;
                let mine = yield_record(ent, &top, &y);
                let theirs: Vec<(String, Val)> = yr.args.iter().filter(|(k, _)| k != "rng").cloned().collect();
                if yr.name != top || theirs != mine.args {
                    return Err(mismatch(
                        Some(yr),
                        format!(
                            "yield: native {} {}, trace {} {}",
                            top,
                            fmt_args(&mine.args),
                            yr.name,
                            fmt_args(&theirs)
                        ),
                    ));
                }
                rep.yields_checked += 1;
                let want = yr.u64_field("rng").map_err(Stop::Mismatch)?;
                if a.rng.state() != want {
                    return Err(mismatch(
                        Some(yr),
                        format!(
                            "RNG at yield: native {:016x} after {} draws, trace {want:016x}",
                            a.rng.state(),
                            a.rng.draws()
                        ),
                    ));
                }
                rep.rng_checked += 1;
            }
        }
        "ev" => {
            // Delivery: conditions only (actions run later in an `irun`).
            let a =
                agents.get_mut(&ent).ok_or_else(|| mismatch(Some(r), "event for an entity without start".into()))?;
            rep.deliveries += 1;
            let ev = event_of(r);
            match r.get("path").and_then(|v| v.as_key()) {
                Some("global") => {
                    let verdict = match r.get("verdict") {
                        Some(Val::Key(k)) if k == "none" => GlobalVerdict::None,
                        Some(Val::Str(s)) => {
                            let (sc, h) = s
                                .split_once('/')
                                .ok_or_else(|| mismatch(Some(r), "verdict=s:<script>/<handler>".into()))?;
                            GlobalVerdict::Handler { script: sc.into(), handler: h.into() }
                        }
                        _ => {
                            return Err(mismatch(
                                Some(r),
                                "global ev needs verdict=k:none or s:<script>/<handler>".into(),
                            ))
                        }
                    };
                    a.deliver_global(&ev, &verdict, view).map_err(|e| at(r, e))?;
                    // Handler conditions never draw in base AI.
                    if let (Some(b), Some(after)) =
                        (r.get("rng").and_then(|v| v.as_u64()), r.get("rng_after").and_then(|v| v.as_u64()))
                    {
                        if b != after {
                            return Err(mismatch(Some(r), "global handler conditions drew from the RNG".into()));
                        }
                    }
                }
                Some("block") => {
                    let verdict = match r.get("verdict").and_then(|v| v.as_key()) {
                        Some("none") => BlockVerdict::None,
                        Some("ignored") => BlockVerdict::Ignored,
                        Some("matched") => {
                            BlockVerdict::Matched { detected: r.get("detected").and_then(|v| v.as_uid_list()) }
                        }
                        _ => return Err(mismatch(Some(r), "block ev needs verdict=k:none|k:ignored|k:matched".into())),
                    };
                    a.deliver_block(&ev, &verdict).map_err(|e| at(r, e))?;
                }
                _ => return Err(mismatch(Some(r), "ev needs path=k:global or k:block".into())),
            }
        }
        "irun" => {
            // Interrupt runner bracket in AIDirector::Move.
            let a = agents.get_mut(&ent).ok_or_else(|| mismatch(Some(r), "irun for an entity without start".into()))?;
            rep.interrupt_runs += 1;
            a.rng.reset_to(r.u64_field("rng").map_err(Stop::Mismatch)?);
            if let Some(Val::F64(t)) = r.get("t") {
                io.store.borrow_mut().now = *t;
            }
            let prog = r.get("prog").and_then(|v| v.as_key()).unwrap_or("").to_string();
            let native_out = match prog.as_str() {
                "handler" => {
                    let p = a
                        .pending_global
                        .first()
                        .cloned()
                        .ok_or_else(|| mismatch(Some(r), "irun without a queued handler".into()))?;
                    let owner = a.stack.get(p.depth).map(|f| f.name().to_string());
                    if owner.as_deref() != Some(r.name.as_str()) {
                        return Err(mismatch(
                            Some(r),
                            format!("irun for {} but the queued handler belongs to {owner:?}", r.name),
                        ));
                    }
                    if p.opaque {
                        io.compare = false;
                        a.pending_global.remove(0);
                        None
                    } else {
                        a.run_global_interrupt(view, io)?
                    }
                }
                "block" => Some(a.run_block_interrupt(view, io)?),
                "orders.base" => {
                    // orders.base's own handler (not ported): actions are opaque.
                    io.compare = false;
                    None
                }
                other => return Err(mismatch(Some(r), format!("unknown irun prog {other:?}"))),
            };
            let native = native_out.is_some();
            let end = loop {
                let n = io.next(&["irend", "fx", "roll", "rnd", "q"])?;
                if n.tag == "irend" {
                    break n;
                }
                if native {
                    return Err(mismatch(
                        Some(n),
                        format!("native interrupt actions made fewer records; trace has {}", n.tag),
                    ));
                }
            };
            io.compare = true;
            let want = end.u64_field("rng").map_err(Stop::Mismatch)?;
            match native_out {
                Some((_, out)) => {
                    if a.rng.state() != want {
                        return Err(mismatch(
                            Some(end),
                            format!("RNG after interrupt actions: native {:016x}, trace {want:016x}", a.rng.state()),
                        ));
                    }
                    rep.rng_checked += 1;
                    let (kind, label) = outcome_fields(&out);
                    let tk = end.get("outcome").and_then(|v| v.as_key()).unwrap_or("");
                    let tl = end.get("label").cloned().unwrap_or(Val::Null);
                    if tk != kind || tl != label {
                        return Err(mismatch(
                            Some(end),
                            format!("interrupt outcome: native {kind} label={label}, trace {tk} label={tl}"),
                        ));
                    }
                }
                None => {
                    a.rng.reset_to(want);
                    if prog == "handler" {
                        if let Some(l) = end.get("abort") {
                            let depth = a.stack.iter().position(|f| f.name() == r.name).ok_or_else(|| {
                                mismatch(Some(end), format!("opaque abort for {} which is not on the stack", r.name))
                            })?;
                            let label = l.as_key().map(|s| s.to_string());
                            a.apply_opaque_abort(depth, label.as_deref(), view, io)?;
                        }
                    }
                }
            }
        }
        "abort" => {
            let a =
                agents.get_mut(&ent).ok_or_else(|| mismatch(Some(r), "abort for an entity without start".into()))?;
            a.abort_all(view, io)?;
        }
        "yield" | "q" | "fx" | "roll" | "rnd" | "irend" => {
            return Err(mismatch(Some(r), format!("{} outside a bracket", r.tag)));
        }
        other => return Err(mismatch(Some(r), format!("unknown record tag {other}"))),
    }
    Ok(())
}

/// Prefix a runtime mismatch with the record's line.
fn at(r: &Record, e: Stop) -> Stop {
    match e {
        Stop::Mismatch(m) => Stop::Mismatch(format!("trace line {}: {m}", r.line)),
        other => other,
    }
}

/// Replay a whole trace. Every entity with a `start` record gets an agent.
pub fn replay(trace: &Trace) -> Report {
    let checks = Checks::from_trace(trace);
    let store = RefCell::new(FactStore::default());
    let view = FactView(&store);
    let recs = &trace.records;
    let mut agents: HashMap<Uid, Agent> = HashMap::new();
    let mut report = Report::default();
    let mut dead: Vec<Uid> = Vec::new();
    // Work list (a stack popping ascending indices); deferred records are pushed back to run next.
    let mut queue: Vec<usize> = (0..recs.len()).rev().collect();
    let mut consumed = vec![false; recs.len()];

    while let Some(i) = queue.pop() {
        if consumed[i] {
            continue;
        }
        consumed[i] = true;
        let r = &recs[i];
        if r.tag == "fact" {
            if let Err(e) = store.borrow_mut().apply(r) {
                report.entities.entry(0).or_default().first_mismatch.get_or_insert(e);
            }
            continue;
        }
        if dead.contains(&r.ent) {
            continue;
        }
        let ent = r.ent;
        let mut io = TraceIo { recs, pos: i + 1, ent, store: &store, checks, deferred: Vec::new(), compare: true };
        let rep = report.entities.entry(ent.0).or_default();
        let res = step(r, &mut io, &mut agents, &view, rep);
        rep.effects += (i + 1..io.pos).filter(|&k| recs[k].ent == ent && recs[k].tag == "fx").count() as u64;
        for (k, c) in consumed.iter_mut().enumerate().take(io.pos).skip(i + 1) {
            if !io.deferred.contains(&k) {
                *c = true;
            }
        }
        for &k in io.deferred.iter().rev() {
            queue.push(k);
        }
        match res {
            Ok(()) => {}
            Err(Stop::Mismatch(m)) => {
                rep.first_mismatch = Some(m);
                dead.push(ent);
            }
            Err(Stop::Unsupported { script, line, what }) => {
                rep.unsupported = Some(format!("{script} L{line}: {what}"));
                dead.push(ent);
            }
        }
    }
    report
}
