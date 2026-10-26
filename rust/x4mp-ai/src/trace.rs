//! X4AITRACE v1: the AI decision capture/replay format (text, one record per line).
//!
//! ```text
//! X4AITRACE<TAB>1<TAB>build=23660954<TAB>source=<native|synthetic|handbuilt>[<TAB>key=value...]
//! <tag><TAB><entity><TAB><name>[<TAB>key=value ...][<TAB>-><TAB>key=value ...]
//! ```
//!
//! `#` starts a comment line; empty lines are ignored. `<entity>` is a component value (`c:0x..`) or `n`;
//! values use the encoding of [`crate::val`]. Tags (see reports/REWRITE-AI-ORDERS.md section 6):
//!
//! | tag | name field | meaning | direction |
//! |---|---|---|---|
//! | `fact` | property | world fact about `<entity>` (`v=`): class, sector, order state, ... | input |
//! | `start` | script | an order script starts on `<entity>` with its order parameters | input |
//! | `run` | script | a run of the top script frame begins: `t=` game time, `wake=`, `rng=` TLS+0x318 | input |
//! | `act` | `-` | an action executed: `line=`, optional `roll=` chance outcome | output |
//! | `q` | query kind | engine query: arguments, then `->` and results; `draws=` engine-side RNG steps | args output, results input |
//! | `fx` | effect kind | externally visible action with its arguments | output |
//! | `yield` | script | end of a run: `kind=` wait/moveto/call/return, its arguments, `rng=` at exit | output |
//! | `ev` | event | an engine event reaches the context: params, `rng=` at delivery, `handler=` | input |
//! | `evend` | outcome | end of that delivery: `rng=` after, outcome fields | output |
//! | `abort` | label | the caller aborted the order script (orders.base `abort_called_scripts`) | input |

use crate::val::{Uid, Val};
use std::fmt;

/// One trace record.
#[derive(Clone, Debug, PartialEq)]
pub struct Record {
    /// 1-based line number in the source text (0 for records built in code).
    pub line: usize,
    pub tag: String,
    pub ent: Uid,
    pub name: String,
    pub args: Vec<(String, Val)>,
    pub results: Vec<(String, Val)>,
}

impl Record {
    pub fn new(tag: &str, ent: Uid, name: &str) -> Record {
        Record { line: 0, tag: tag.into(), ent, name: name.into(), args: Vec::new(), results: Vec::new() }
    }
    pub fn arg(mut self, k: &str, v: Val) -> Record {
        self.args.push((k.into(), v));
        self
    }
    pub fn res(mut self, k: &str, v: Val) -> Record {
        self.results.push((k.into(), v));
        self
    }
    pub fn get(&self, k: &str) -> Option<&Val> {
        self.args.iter().find(|(n, _)| n == k).map(|(_, v)| v)
    }
    pub fn result(&self, k: &str) -> Option<&Val> {
        self.results.iter().find(|(n, _)| n == k).map(|(_, v)| v)
    }
    /// A required u64 field (`x:`), e.g. `rng`.
    pub fn u64_field(&self, k: &str) -> Result<u64, String> {
        self.get(k).and_then(|v| v.as_u64()).ok_or_else(|| format!("line {}: {} needs {k}=x:..", self.line, self.tag))
    }
    pub fn f64_field(&self, k: &str) -> Result<f64, String> {
        self.get(k).and_then(|v| v.as_f64()).ok_or_else(|| format!("line {}: {} needs {k}=d:..", self.line, self.tag))
    }
}

impl fmt::Display for Record {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}\t{}\t{}", self.tag, Val::uid(self.ent), self.name)?;
        for (k, v) in &self.args {
            write!(f, "\t{k}={v}")?;
        }
        if !self.results.is_empty() {
            write!(f, "\t->")?;
            for (k, v) in &self.results {
                write!(f, "\t{k}={v}")?;
            }
        }
        Ok(())
    }
}

/// A parsed trace.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Trace {
    pub version: u32,
    pub header: Vec<(String, String)>,
    pub records: Vec<Record>,
}

pub const MAGIC: &str = "X4AITRACE";
pub const VERSION: u32 = 1;
pub const BUILD: &str = "23660954";

impl Trace {
    pub fn new(source: &str) -> Trace {
        Trace {
            version: VERSION,
            header: vec![("build".into(), BUILD.into()), ("source".into(), source.into())],
            records: Vec::new(),
        }
    }

    pub fn header_value(&self, k: &str) -> Option<&str> {
        self.header.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str())
    }

    pub fn push(&mut self, r: Record) {
        self.records.push(r);
    }

    /// Parse the text form. Fails closed: unknown versions or another build are refused.
    pub fn parse(text: &str) -> Result<Trace, String> {
        let mut lines = text.lines().enumerate();
        let (_, first) = lines.find(|(_, l)| !l.trim().is_empty() && !l.starts_with('#')).ok_or("empty trace")?;
        let hf: Vec<&str> = first.split('\t').collect();
        if hf.first() != Some(&MAGIC) {
            return Err(format!("not an X4AITRACE file (first field {:?})", hf.first()));
        }
        let version: u32 = hf.get(1).and_then(|v| v.parse().ok()).ok_or("missing version")?;
        if version != VERSION {
            return Err(format!("unsupported X4AITRACE version {version}"));
        }
        let mut header = Vec::new();
        for f in &hf[2..] {
            let (k, v) = f.split_once('=').ok_or_else(|| format!("bad header field {f:?}"))?;
            header.push((k.to_string(), v.to_string()));
        }
        let t = Trace { version, header, records: Vec::new() };
        if t.header_value("build") != Some(BUILD) {
            return Err(format!("trace is for build {:?}, this crate models {BUILD}", t.header_value("build")));
        }
        let mut t = t;
        for (i, l) in lines {
            if l.trim().is_empty() || l.starts_with('#') {
                continue;
            }
            t.records.push(parse_record(i + 1, l)?);
        }
        Ok(t)
    }

    /// Records of one entity, in trace order.
    pub fn for_entity(&self, ent: Uid) -> Vec<Record> {
        self.records.iter().filter(|r| r.ent == ent).cloned().collect()
    }

    /// Entities that start an order script, in order of first appearance.
    pub fn started_entities(&self) -> Vec<Uid> {
        let mut out: Vec<Uid> = Vec::new();
        for r in &self.records {
            if r.tag == "start" && !out.contains(&r.ent) {
                out.push(r.ent);
            }
        }
        out
    }
}

impl fmt::Display for Trace {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{MAGIC}\t{}", self.version)?;
        for (k, v) in &self.header {
            write!(f, "\t{k}={v}")?;
        }
        writeln!(f)?;
        for r in &self.records {
            writeln!(f, "{r}")?;
        }
        Ok(())
    }
}

fn parse_record(line: usize, l: &str) -> Result<Record, String> {
    let fields: Vec<&str> = l.split('\t').collect();
    if fields.len() < 3 {
        return Err(format!("line {line}: a record needs tag, entity and name"));
    }
    let ent = Val::parse(fields[1])
        .map_err(|e| format!("line {line}: {e}"))?
        .as_uid()
        .ok_or_else(|| format!("line {line}: entity must be a component or n"))?;
    let mut r =
        Record { line, tag: fields[0].to_string(), ent, name: fields[2].to_string(), args: vec![], results: vec![] };
    let mut in_results = false;
    for f in &fields[3..] {
        if *f == "->" {
            in_results = true;
            continue;
        }
        if f.is_empty() {
            continue;
        }
        let (k, v) = f.split_once('=').ok_or_else(|| format!("line {line}: field without '=': {f:?}"))?;
        let v = Val::parse(v).map_err(|e| format!("line {line}: field {k}: {e}"))?;
        if in_results {
            r.results.push((k.to_string(), v));
        } else {
            r.args.push((k.to_string(), v));
        }
    }
    Ok(r)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::val::Pos;

    #[test]
    fn round_trip_and_fail_closed() {
        let mut t = Trace::new("handbuilt");
        t.push(Record::new("fact", Uid(0x10), "class").arg("v", Val::key("zone")));
        t.push(
            Record::new("q", Uid(0x1), "find_zone")
                .arg("space", Val::Uid(Uid(0x20)))
                .arg("normalzone", Val::Bool(true))
                .res("zone", Val::Uid(Uid(0x10)))
                .res("draws", Val::Int(0)),
        );
        t.push(Record::new("fx", Uid(0x1), "move_to").arg("pos", Val::Pos(Pos::new(1.0, 2.0, 3.0))));
        let s = t.to_string();
        let mut u = Trace::parse(&s).unwrap();
        for r in &mut u.records {
            r.line = 0;
        }
        assert_eq!(u, t);
        assert!(Trace::parse(&s.replace("build=23660954", "build=1")).is_err());
        assert!(Trace::parse(&s.replace("X4AITRACE\t1", "X4AITRACE\t2")).is_err());
    }
}
