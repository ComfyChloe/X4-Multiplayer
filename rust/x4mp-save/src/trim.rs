//! Keep-set computation (reference closure) and pass 2: the filtering writer.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::io::{BufRead, Write};

use quick_xml::events::{BytesStart, Event};
use quick_xml::{Reader, Writer};

use crate::index::{SaveIndex, Target, UnitKind, PERSISTENT};
use crate::io::{parse_dec, parse_hex_id, CountingReader, CountingWriter};
use crate::schema;

#[derive(Clone, Debug)]
pub struct TrimOptions {
    /// Target sector macros (e.g. `cluster_01_sector001_macro`).
    pub sectors: Vec<String>,
    /// Gate/highway hops around the target sectors that also keep full detail.
    pub neighbours: u32,
    /// Faction whose objects are always kept (the joining player).
    pub keep_owner: String,
    /// How component references held in typed script values pin their targets.
    pub script_refs: ScriptRefs,
    /// Client-only transformations (see README "Client mode"): switch the job engine
    /// off (`<jobs enabled="0">`), mark every job inactive, clear first-spawn flags and
    /// spawn timers, and drop the job queue (`job/waiting`, `job/requested`).
    pub client: bool,
}

/// Policy for component references in typed script values (MD cue variables, AI
/// script variables, the script value pool, order parameters, filter lists).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScriptRefs {
    /// Pure reference closure: every script value pins its target.
    Strong,
    /// Script values do not pin; a dropped target leaves the value pointing at a missing
    /// id, the state X4 itself saves for destroyed objects. Structural references
    /// (docking, construction, fleets, trades, orders, posts) still pin.
    Weak,
}

impl std::str::FromStr for ScriptRefs {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        match s {
            "strong" => Ok(ScriptRefs::Strong),
            "weak" => Ok(ScriptRefs::Weak),
            _ => Err(format!("--script-refs must be strong or weak, not '{s}'")),
        }
    }
}

/// Why a unit was kept (first reason found by the breadth-first closure).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Reason {
    Persistent,
    TargetSector,
    NeighbourSector,
    Owner,
    PlayerEntity,
    NotInSector,
    UnreferencedPool,
    EntityWithoutComponent,
    /// Contains a kept nested unit (e.g. a carrier with the player's docked ship).
    ContainsKept,
    /// Strong reference `attr` (interned) from a unit of kind/class `from_class`.
    Ref {
        attr: u32,
        ctx: u32,
        from_kind: u8,
        from_class: u32,
    },
}

pub struct Plan {
    pub client: bool,
    /// Client mode: queued ships kept because something outside the queue references
    /// them (e.g. a shipyard build task for a ship under construction).
    pub client_queue_kept: u64,
    pub keep: Vec<bool>,
    pub reason: Vec<Option<Reason>>,
    /// Sector component id -> hop distance (only kept-detail sectors).
    pub detail_sectors: BTreeMap<u64, u32>,
}

#[derive(Debug)]
pub struct PlanError(pub String);

impl std::fmt::Display for PlanError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for PlanError {}

fn kind_code(k: UnitKind) -> u8 {
    match k {
        UnitKind::Persistent => 0,
        UnitKind::Dynamic => 1,
        UnitKind::Pool => 2,
        UnitKind::Entity => 3,
    }
}

/// Sector adjacency through gates and highways. Nodes are sector ids, plus owner
/// components outside any sector (cluster-level highways) as zero-cost pass-throughs.
pub fn sector_graph(idx: &SaveIndex) -> HashMap<u64, Vec<u64>> {
    let mut g: HashMap<u64, Vec<u64>> = HashMap::new();
    for &(a, b) in &idx.links {
        let (Some(ca), Some(cb)) = (idx.conns.get(&a), idx.conns.get(&b)) else { continue };
        let ta = schema::is_travel_class(idx.syms.name(ca.owner_class));
        let tb = schema::is_travel_class(idx.syms.name(cb.owner_class));
        if !(ta || tb) {
            continue;
        }
        let la = if ca.sector != 0 { ca.sector } else { ca.owner_comp };
        let lb = if cb.sector != 0 { cb.sector } else { cb.owner_comp };
        if la == lb || la == 0 || lb == 0 {
            continue;
        }
        let ea = g.entry(la).or_default();
        if !ea.contains(&lb) {
            ea.push(lb);
        }
        let eb = g.entry(lb).or_default();
        if !eb.contains(&la) {
            eb.push(la);
        }
    }
    g
}

/// Sectors within `hops` gate/highway jumps of `targets` (0-1 BFS; non-sector nodes cost 0).
pub fn neighbour_sectors(idx: &SaveIndex, targets: &[u64], hops: u32) -> BTreeMap<u64, u32> {
    let g = sector_graph(idx);
    let mut dist: HashMap<u64, u32> = HashMap::new();
    let mut dq: VecDeque<u64> = VecDeque::new();
    for &t in targets {
        dist.insert(t, 0);
        dq.push_back(t);
    }
    while let Some(n) = dq.pop_front() {
        let d = dist[&n];
        for &m in g.get(&n).map(|v| v.as_slice()).unwrap_or(&[]) {
            let cost = if idx.sectors.contains_key(&m) { 1 } else { 0 };
            let nd = d + cost;
            if nd > hops {
                continue;
            }
            if dist.get(&m).is_none_or(|&old| nd < old) {
                dist.insert(m, nd);
                if cost == 0 {
                    dq.push_front(m);
                } else {
                    dq.push_back(m);
                }
            }
        }
    }
    dist.into_iter().filter(|(k, _)| idx.sectors.contains_key(k)).collect()
}

/// Resolves sector macros (case-insensitive) to sector ids.
pub fn find_sectors(idx: &SaveIndex, macros: &[String]) -> Result<Vec<u64>, PlanError> {
    let mut out = Vec::new();
    for m in macros {
        let found: Vec<u64> = idx
            .sectors
            .iter()
            .filter(|(_, s)| idx.syms.name(s.macro_).eq_ignore_ascii_case(m))
            .map(|(&id, _)| id)
            .collect();
        if found.is_empty() {
            let mut similar: Vec<&str> = idx
                .sectors
                .values()
                .map(|s| idx.syms.name(s.macro_))
                .filter(|n| n.contains(m.trim_end_matches("_macro")))
                .collect();
            similar.sort();
            similar.truncate(10);
            return Err(PlanError(format!("sector macro '{m}' not found in save (similar: {similar:?})")));
        }
        out.extend(found);
    }
    Ok(out)
}

pub fn plan(idx: &SaveIndex, opts: &TrimOptions) -> Result<Plan, PlanError> {
    let targets = find_sectors(idx, &opts.sectors)?;
    let detail = neighbour_sectors(idx, &targets, opts.neighbours);
    let keep_owner = idx.syms.get(&opts.keep_owner);
    let n = idx.units.len();

    // Strong edges unit -> unit (from representative units).
    let mut edges: Vec<Vec<(u32, u32, u32)>> = vec![Vec::new(); n];
    let mut incoming = vec![0u32; n];
    let mut tmp = Vec::new();
    for r in &idx.refs {
        if r.weak || (r.script_value && opts.script_refs == ScriptRefs::Weak) || (opts.client && r.job_queue) {
            continue;
        }
        let from = idx.unit_rep(r.from);
        idx.resolve(r.target, &mut tmp);
        for &t in &tmp {
            let t = idx.unit_rep(t);
            if t != from {
                edges[from as usize].push((t, r.attr, r.ctx));
                incoming[t as usize] += 1;
            }
        }
    }
    for e in edges.iter_mut() {
        e.sort_unstable();
        e.dedup_by_key(|x| x.0);
    }

    let mut keep = vec![false; n];
    let mut reason: Vec<Option<Reason>> = vec![None; n];
    let mut queue: VecDeque<u32> = VecDeque::new();
    let seed = |u: usize, r: Reason, keep: &mut Vec<bool>, reason: &mut Vec<Option<Reason>>, q: &mut VecDeque<u32>| {
        if !keep[u] {
            keep[u] = true;
            reason[u] = Some(r);
            q.push_back(u as u32);
        }
    };
    seed(PERSISTENT as usize, Reason::Persistent, &mut keep, &mut reason, &mut queue);
    for (i, u) in idx.units.iter().enumerate() {
        match u.kind {
            UnitKind::Persistent => {}
            UnitKind::Dynamic if opts.client && u.job_queue => {}
            UnitKind::Dynamic => {
                let r = match u.sector {
                    None => Some(Reason::NotInSector),
                    Some(s) => match detail.get(&s) {
                        Some(0) => Some(Reason::TargetSector),
                        Some(_) => Some(Reason::NeighbourSector),
                        None => None,
                    },
                };
                let r = r.or_else(|| {
                    if u.has_player {
                        Some(Reason::PlayerEntity)
                    } else if keep_owner.is_some_and(|o| u.owners.contains(&o)) {
                        Some(Reason::Owner)
                    } else {
                        None
                    }
                });
                if let Some(r) = r {
                    seed(i, r, &mut keep, &mut reason, &mut queue);
                }
            }
            UnitKind::Pool => {
                if incoming[i] == 0 {
                    seed(i, Reason::UnreferencedPool, &mut keep, &mut reason, &mut queue);
                }
            }
            UnitKind::Entity => {
                if idx.unit_rep(i as u32) == i as u32 {
                    seed(i, Reason::EntityWithoutComponent, &mut keep, &mut reason, &mut queue);
                }
            }
        }
    }
    while let Some(u) = queue.pop_front() {
        let unit = &idx.units[u as usize];
        // Containment: a kept nested unit keeps its enclosing unit.
        if unit.kind == UnitKind::Dynamic {
            let p = unit.parent as usize;
            if !keep[p] {
                keep[p] = true;
                reason[p] = Some(Reason::ContainsKept);
                queue.push_back(p as u32);
            }
        }
        for &(t, attr, ctx) in &edges[u as usize] {
            let t = t as usize;
            if !keep[t] {
                keep[t] = true;
                reason[t] = Some(Reason::Ref { attr, ctx, from_kind: kind_code(unit.kind), from_class: unit.class });
                queue.push_back(t as u32);
            }
        }
    }
    let client_queue_kept = if opts.client {
        idx.units.iter().enumerate().filter(|(i, u)| u.kind == UnitKind::Dynamic && u.job_queue && keep[*i]).count()
            as u64
    } else {
        0
    };
    // Entity aliases share the fate of their component's unit.
    for (i, u) in idx.units.iter().enumerate() {
        if u.kind == UnitKind::Entity {
            let r = idx.unit_rep(i as u32) as usize;
            if r != i {
                keep[i] = keep[r];
                reason[i] = reason[r];
            }
        }
    }
    Ok(Plan { client: opts.client, client_queue_kept, keep, reason, detail_sectors: detail })
}

pub fn reason_label(idx: &SaveIndex, r: Reason) -> String {
    match r {
        Reason::Persistent => "persistent world".into(),
        Reason::TargetSector => "in target sector".into(),
        Reason::NeighbourSector => "in neighbour sector".into(),
        Reason::Owner => "owned by keep-owner".into(),
        Reason::PlayerEntity => "contains the player entity".into(),
        Reason::NotInSector => "not in a sector (job queue / off-map)".into(),
        Reason::UnreferencedPool => "script pool entry without visible referrer".into(),
        Reason::EntityWithoutComponent => "AI entity without component".into(),
        Reason::ContainsKept => "contains a kept unit".into(),
        Reason::Ref { attr, ctx, from_kind, from_class } => {
            let from = match from_kind {
                0 => format!("persistent {}", idx.syms.name(ctx)),
                1 => idx.syms.name(from_class).to_string(),
                2 => format!("script pool {}", idx.syms.name(from_class)),
                _ => "AI entity".to_string(),
            };
            format!("ref {} from {}", idx.syms.name(attr), from)
        }
    }
}

#[derive(Default, Debug)]
pub struct WriteStats {
    pub in_bytes: u64,
    pub out_bytes: u64,
    pub skipped_components: u64,
    pub skipped_units: u64,
    pub skipped_entities: u64,
    pub skipped_pool: u64,
    pub pruned_listeners: u64,
    pub pruned_members: u64,
    /// Client mode: `<job>` elements rewritten, job-queue elements removed.
    pub client_jobs_rewritten: u64,
    pub client_queue_elements: u64,
}

/// Pass 2: copy the save event by event, skipping dropped subtrees and pruning weak
/// references (listeners, script group members) that point at dropped units.
pub fn write_trimmed<R: BufRead, W: Write>(
    idx: &SaveIndex,
    plan: &Plan,
    input: R,
    out: W,
) -> Result<(WriteStats, W), Box<dyn std::error::Error>> {
    let mut reader = Reader::from_reader(CountingReader { inner: input, count: 0 });
    reader.config_mut().trim_text(false);
    let mut writer = Writer::new(CountingWriter { inner: out, count: 0 });
    let mut buf = Vec::with_capacity(4096);
    let mut skip_buf = Vec::new();
    let mut depth = 0usize;
    let mut section: Vec<u8> = Vec::new();
    let mut pool_type: u32 = 0;
    let mut in_jobs = false;
    let mut in_waiting = false;
    let mut queue_ship = 0usize;
    let mut st = WriteStats::default();
    let dropped = |unit: u32| !plan.keep[unit as usize];
    let comp_dropped = |id: u64| idx.comps.get(&id).is_some_and(|c| dropped(c.unit));

    loop {
        let ev = reader.read_event_into(&mut buf)?;
        match &ev {
            Event::Start(e) | Event::Empty(e) => {
                let is_start = matches!(ev, Event::Start(_));
                if depth == 1 {
                    section.clear();
                    section.extend_from_slice(e.name().as_ref());
                }
                let mut decision = decide(idx, e, depth, &section, &mut pool_type, &dropped, &comp_dropped);
                if plan.client && decision.is_none() {
                    let name = e.name();
                    let name = name.as_ref();
                    if depth == 2 && section == b"universe" && name == b"jobs" {
                        let new = rewrite_attrs(e, &[(b"enabled", Some(b"0"))]);
                        if is_start {
                            in_jobs = true;
                            depth += 1;
                            writer.write_event(Event::Start(new))?;
                        } else {
                            writer.write_event(Event::Empty(new))?;
                        }
                        buf.clear();
                        continue;
                    }
                    if in_jobs && depth == 3 && name == b"job" {
                        st.client_jobs_rewritten += 1;
                        let new = rewrite_attrs(e, &[(b"active", Some(b"0")), (b"firsttime", None), (b"time", None)]);
                        if is_start {
                            depth += 1;
                            writer.write_event(Event::Start(new))?;
                        } else {
                            writer.write_event(Event::Empty(new))?;
                        }
                        buf.clear();
                        continue;
                    }
                    if in_jobs && depth == 4 && name == b"requested" {
                        decision = Some(Skip::JobQueue);
                    } else if in_jobs && depth == 4 && name == b"waiting" && is_start {
                        in_waiting = true;
                    } else if in_waiting && depth == 5 && name == b"ship" {
                        // Keep only queued ships the closure kept; drop bare mentions.
                        let unit = idx.queue_ships.get(queue_ship).copied().flatten();
                        queue_ship += 1;
                        if unit.is_none_or(dropped) {
                            decision = Some(Skip::JobQueue);
                        }
                    }
                }
                if let Some(kind) = decision {
                    match kind {
                        Skip::Component => {
                            st.skipped_units += 1;
                        }
                        Skip::Entity => st.skipped_entities += 1,
                        Skip::Pool => st.skipped_pool += 1,
                        Skip::Listener => st.pruned_listeners += 1,
                        Skip::Member => st.pruned_members += 1,
                        Skip::JobQueue => st.client_queue_elements += 1,
                    }
                    if is_start {
                        let end = e.to_end().into_owned();
                        skip_buf.clear();
                        // Count components inside the skipped subtree.
                        let mut d = 1usize;
                        if kind == Skip::Component {
                            st.skipped_components += 1;
                        }
                        loop {
                            match reader.read_event_into(&mut skip_buf)? {
                                Event::Start(s) => {
                                    d += 1;
                                    if s.name().as_ref() == b"component" {
                                        st.skipped_components += 1;
                                    }
                                }
                                Event::Empty(s) => {
                                    if s.name().as_ref() == b"component" {
                                        st.skipped_components += 1;
                                    }
                                }
                                Event::End(x) => {
                                    d -= 1;
                                    if d == 0 {
                                        debug_assert_eq!(x.name(), end.name());
                                        break;
                                    }
                                }
                                Event::Eof => return Err("unexpected EOF inside skipped element".into()),
                                _ => {}
                            }
                            skip_buf.clear();
                        }
                    } else if kind == Skip::Component {
                        st.skipped_components += 1;
                    }
                    buf.clear();
                    continue;
                }
                if is_start {
                    depth += 1;
                }
                writer.write_event(ev.borrow())?;
            }
            Event::End(_) => {
                depth -= 1;
                if depth == 2 {
                    in_jobs = false;
                }
                if depth == 4 {
                    in_waiting = false;
                }
                if depth <= 1 {
                    section.clear();
                }
                writer.write_event(ev.borrow())?;
            }
            Event::Eof => break,
            _ => writer.write_event(ev.borrow())?,
        }
        buf.clear();
    }
    let mut inner = writer.into_inner();
    inner.flush()?;
    st.in_bytes = reader.get_ref().count;
    st.out_bytes = inner.count;
    Ok((st, inner.inner))
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Skip {
    Component,
    Entity,
    Pool,
    Listener,
    Member,
    JobQueue,
}

/// Copies a start tag, setting (`Some`) or removing (`None`) the given attributes.
/// Other attributes are copied byte for byte; set attributes keep their position or
/// are appended.
fn rewrite_attrs(e: &BytesStart, changes: &[(&[u8], Option<&[u8]>)]) -> BytesStart<'static> {
    let name = String::from_utf8_lossy(e.name().as_ref()).into_owned();
    let mut out = BytesStart::new(name);
    let mut done = vec![false; changes.len()];
    for a in e.attributes().flatten() {
        let key = a.key.as_ref();
        match changes.iter().position(|(k, _)| *k == key) {
            Some(i) => {
                done[i] = true;
                if let Some(v) = changes[i].1 {
                    out.push_attribute((key, v));
                }
            }
            None => out.push_attribute((key, a.value.as_ref())),
        }
    }
    for (i, (k, v)) in changes.iter().enumerate() {
        if let (false, Some(v)) = (done[i], v) {
            out.push_attribute((*k, *v));
        }
    }
    out
}

fn attr<'a>(e: &'a BytesStart, key: &[u8]) -> Option<std::borrow::Cow<'a, [u8]>> {
    e.attributes().flatten().find(|a| a.key.as_ref() == key).map(|a| a.value)
}

fn decide(
    idx: &SaveIndex,
    e: &BytesStart,
    depth: usize,
    section: &[u8],
    pool_type: &mut u32,
    dropped: &dyn Fn(u32) -> bool,
    comp_dropped: &dyn Fn(u64) -> bool,
) -> Option<Skip> {
    let name = e.name();
    let name = name.as_ref();
    match name {
        b"component" => {
            let id = attr(e, b"id").and_then(|v| parse_hex_id(&v))?;
            let c = idx.comps.get(&id)?;
            let u = &idx.units[c.unit as usize];
            // Only unit roots are decided here; parts follow their root.
            if u.kind == UnitKind::Dynamic && u.id == id && dropped(c.unit) {
                return Some(Skip::Component);
            }
            None
        }
        b"entity" if depth == 2 && section == b"aidirector" => {
            let id = attr(e, b"id").and_then(|v| parse_hex_id(&v))?;
            let &u = idx.entities.get(&id)?;
            dropped(idx.unit_rep(u)).then_some(Skip::Entity)
        }
        b"refs" if depth == 2 && section == b"script" => {
            *pool_type = attr(e, b"type").and_then(|t| idx.syms.get(std::str::from_utf8(&t).ok()?)).unwrap_or(0);
            None
        }
        b"ref" if depth == 3 && section == b"script" => {
            let id = attr(e, b"id").and_then(|v| parse_dec(&v))?;
            let &u = idx.pool.get(&(*pool_type, id))?;
            dropped(u).then_some(Skip::Pool)
        }
        b"listener" => {
            let id = attr(e, b"listener").and_then(|v| parse_hex_id(&v))?;
            let gone = comp_dropped(id) || idx.entities.get(&id).is_some_and(|&u| dropped(idx.unit_rep(u)));
            gone.then_some(Skip::Listener)
        }
        b"member" if section == b"script" => {
            let id = attr(e, b"id").and_then(|v| parse_hex_id(&v))?;
            comp_dropped(id).then_some(Skip::Member)
        }
        _ => None,
    }
}

/// Summary of a plan: dropped/kept units by class and kept reasons.
pub struct PlanSummary {
    pub dropped_by_class: BTreeMap<String, (u64, u64)>,
    pub kept_dynamic_by_reason: BTreeMap<String, u64>,
    pub kept_pool_by_reason: BTreeMap<String, u64>,
    pub dropped_pool: u64,
    pub dropped_entities: u64,
    pub dynamic_total: u64,
    pub dynamic_kept: u64,
    pub dropped_by_sector: BTreeMap<String, u64>,
    pub kept_distant_by_sector: BTreeMap<String, u64>,
}

pub fn summarize(idx: &SaveIndex, plan: &Plan) -> PlanSummary {
    let mut s = PlanSummary {
        dropped_by_class: BTreeMap::new(),
        kept_dynamic_by_reason: BTreeMap::new(),
        kept_pool_by_reason: BTreeMap::new(),
        dropped_pool: 0,
        dropped_entities: 0,
        dynamic_total: 0,
        dynamic_kept: 0,
        dropped_by_sector: BTreeMap::new(),
        kept_distant_by_sector: BTreeMap::new(),
    };
    // A dynamic unit nested in a dropped unit is dropped implicitly; count roots only.
    for (i, u) in idx.units.iter().enumerate() {
        match u.kind {
            UnitKind::Dynamic => {
                s.dynamic_total += 1;
                let sector = u.sector.map(|x| idx.sector_macro(x).to_string()).unwrap_or_else(|| "-".into());
                if plan.keep[i] {
                    s.dynamic_kept += 1;
                    let label = reason_label(idx, plan.reason[i].unwrap());
                    *s.kept_dynamic_by_reason.entry(label).or_default() += 1;
                    if u.sector.is_some_and(|x| !plan.detail_sectors.contains_key(&x)) {
                        *s.kept_distant_by_sector.entry(sector).or_default() += 1;
                    }
                } else {
                    let e = s.dropped_by_class.entry(idx.syms.name(u.class).to_string()).or_default();
                    e.0 += 1;
                    e.1 += u.components as u64;
                    *s.dropped_by_sector.entry(sector).or_default() += 1;
                }
            }
            UnitKind::Pool => {
                if plan.keep[i] {
                    let label = reason_label(idx, plan.reason[i].unwrap());
                    *s.kept_pool_by_reason.entry(label).or_default() += 1;
                } else {
                    s.dropped_pool += 1;
                }
            }
            UnitKind::Entity => {
                if !plan.keep[i] {
                    s.dropped_entities += 1;
                }
            }
            UnitKind::Persistent => {}
        }
    }
    s
}

/// Namespace name of a reference target.
pub fn target_kind(t: &Target) -> &'static str {
    match t {
        Target::Component(_) => "component",
        Target::Connection(_) => "connection",
        Target::Local(..) => "local",
        Target::Pool(..) => "pool",
    }
}
