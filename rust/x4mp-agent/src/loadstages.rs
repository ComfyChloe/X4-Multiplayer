//! Load profiler, layer 1: X4's own load-stage timings from `<profile>/progressbar.xml`.
//!
//! X4 keeps one running average per load type (`newgame`, `savedgame`, ...) with a load
//! count `loaded=N`, and one value per stage (`init`, `map`, `scripts`, `jobs`, ...).
//! It uses them to drive the loading bar. Reading the file before and after one load
//! gives that load's stage durations, if the update rule is known:
//!
//! * cumulative mean (expected): `avg' = (N*avg + x) / (N+1)`, so
//!   `x = (N+1)*avg' - N*avg` exactly;
//! * any running average `avg' = avg + w*(x - avg)` gives `x = avg + (avg' - avg)/w`.
//!   With the load's wall time `T` measured independently (the agent's loading flag),
//!   the weight follows from the stage sums: `w = (S' - S) / (T - S)`. A cumulative
//!   mean predicts `w = 1/(N+1)`; the `LOADSTAGES` line reports both, so one native
//!   run settles the rule. When they differ the stages are also given for the implied
//!   weight (`stages_w=`).
//!
//! The exact XML layout is not documented. The parser accepts what X4 is believed to
//! write and a few plausible variants: an element with a `loaded` attribute is a load
//! type (named by its `id`/`name`/`type` attribute, else its tag); its child elements
//! are stages (named the same way, value from `time`/`duration`/`value`/`avg`/... or
//! the only numeric attribute or the text), and other numeric attributes of the type
//! element are stages too. Values are kept in the file's unit (believed seconds).

#[derive(Debug, Clone, PartialEq)]
pub struct LoadType {
    pub name: String,
    pub loaded: u64,
    pub stages: Vec<(String, f64)>,
}

impl LoadType {
    pub fn sum(&self) -> f64 {
        self.stages.iter().map(|s| s.1).sum()
    }
    fn stage(&self, name: &str) -> Option<f64> {
        self.stages.iter().find(|s| s.0 == name).map(|s| s.1)
    }
}

#[derive(Debug, Clone)]
struct Element {
    tag: String,
    attrs: Vec<(String, String)>,
    text: String,
}

impl Element {
    fn attr(&self, key: &str) -> Option<&str> {
        self.attrs.iter().find(|a| a.0 == key).map(|a| a.1.as_str())
    }
    fn label(&self) -> String {
        ["id", "name", "type"].iter().find_map(|k| self.attr(k)).unwrap_or(&self.tag).to_string()
    }
    fn value(&self) -> Option<f64> {
        const KEYS: [&str; 9] = ["time", "duration", "value", "avg", "average", "seconds", "s", "t", "v"];
        for key in KEYS {
            if let Some(v) = self.attr(key).and_then(|v| v.trim().parse::<f64>().ok()) {
                return Some(v);
            }
        }
        let numeric: Vec<f64> = self
            .attrs
            .iter()
            .filter(|a| !matches!(a.0.as_str(), "id" | "name" | "type" | "loaded"))
            .filter_map(|a| a.1.trim().parse::<f64>().ok())
            .collect();
        if numeric.len() == 1 {
            return Some(numeric[0]);
        }
        self.text.trim().parse::<f64>().ok()
    }
}

fn unescape(v: &str) -> String {
    v.replace("&quot;", "\"").replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
}

/// Parse `name="v" name2='w'` attribute text.
fn attributes(mut s: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    loop {
        s = s.trim_start();
        let Some(eq) = s.find('=') else { break };
        let key = s[..eq].trim().to_string();
        let rest = s[eq + 1..].trim_start();
        let Some(quote) = rest.chars().next().filter(|c| *c == '"' || *c == '\'') else { break };
        let Some(end) = rest[1..].find(quote) else { break };
        out.push((key, unescape(&rest[1..1 + end])));
        s = &rest[end + 2..];
    }
    out
}

/// Parse a progressbar.xml body into its load types (in file order).
pub fn parse(xml: &str) -> Vec<LoadType> {
    let mut types: Vec<LoadType> = Vec::new();
    // Open elements; `Some(index)` marks a load-type element.
    let mut stack: Vec<(Element, Option<usize>)> = Vec::new();
    let mut rest = xml;
    while let Some(lt) = rest.find('<') {
        if let Some(top) = stack.last_mut() {
            top.0.text.push_str(&rest[..lt]);
        }
        rest = &rest[lt..];
        if rest.starts_with("<!--") {
            rest = rest.find("-->").map_or("", |e| &rest[e + 3..]);
            continue;
        }
        let Some(gt) = rest.find('>') else { break };
        let tag_text = &rest[1..gt];
        rest = &rest[gt + 1..];
        if tag_text.starts_with('?') || tag_text.starts_with('!') {
            continue;
        }
        if let Some(name) = tag_text.strip_prefix('/') {
            // Close: pop to the matching element.
            let name = name.trim();
            if stack.iter().any(|s| s.0.tag == name) {
                while let Some((el, ty)) = stack.pop() {
                    let matched = el.tag == name;
                    close(&mut types, &mut stack, el, ty);
                    if matched {
                        break;
                    }
                }
            }
            continue;
        }
        let self_closing = tag_text.ends_with('/');
        let body = tag_text.trim_end_matches('/');
        let (tag, attr_text) = body.split_once(char::is_whitespace).unwrap_or((body, ""));
        let el = Element { tag: tag.to_string(), attrs: attributes(attr_text), text: String::new() };
        let ty = match el.attr("loaded").and_then(|v| v.trim().parse::<u64>().ok()) {
            Some(loaded) => {
                let mut t = LoadType { name: el.label(), loaded, stages: Vec::new() };
                for (k, v) in &el.attrs {
                    if matches!(k.as_str(), "id" | "name" | "type" | "loaded") {
                        continue;
                    }
                    if let Ok(x) = v.trim().parse::<f64>() {
                        t.stages.push((k.clone(), x));
                    }
                }
                types.push(t);
                Some(types.len() - 1)
            }
            None => None,
        };
        if self_closing {
            close(&mut types, &mut stack, el, ty);
        } else {
            stack.push((el, ty));
        }
    }
    while let Some((el, ty)) = stack.pop() {
        close(&mut types, &mut stack, el, ty);
    }
    types
}

/// A finished element that is a direct child of a load type is a stage.
fn close(types: &mut [LoadType], stack: &mut [(Element, Option<usize>)], el: Element, ty: Option<usize>) {
    if ty.is_some() {
        return;
    }
    if let Some((_, Some(parent))) = stack.last() {
        if let Some(v) = el.value() {
            types[*parent].stages.push((el.label(), v));
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Delta {
    pub name: String,
    pub before_n: u64,
    pub after_n: u64,
    /// (stage, average before, average after)
    pub stages: Vec<(String, f64, f64)>,
}

impl Delta {
    /// Number of loads between the two snapshots, if the count moved forward.
    pub fn loads(&self) -> Option<u64> {
        (self.after_n > self.before_n).then(|| self.after_n - self.before_n)
    }

    /// Per-stage duration of the new load(s) under the cumulative-mean rule (the mean
    /// of the new loads when more than one happened).
    pub fn cumulative(&self) -> Option<Vec<(String, f64)>> {
        let k = self.loads()? as f64;
        let (n0, n1) = (self.before_n as f64, self.after_n as f64);
        Some(self.stages.iter().map(|(s, a0, a1)| (s.clone(), (n1 * a1 - n0 * a0) / k)).collect())
    }

    /// Per-stage duration under `avg' = avg + w (x - avg)`.
    pub fn with_weight(&self, w: f64) -> Vec<(String, f64)> {
        self.stages.iter().map(|(s, a0, a1)| (s.clone(), a0 + (a1 - a0) / w)).collect()
    }

    /// Weight implied by an independently measured total duration of the load.
    pub fn implied_weight(&self, measured_total: f64) -> Option<f64> {
        let s0: f64 = self.stages.iter().map(|s| s.1).sum();
        let s1: f64 = self.stages.iter().map(|s| s.2).sum();
        let denom = measured_total - s0;
        let w = (s1 - s0) / denom;
        (denom.abs() > 1e-9 && w.is_finite() && w > 0.0).then_some(w)
    }
}

/// Types whose count or averages changed between the snapshots. A type absent before
/// counts as `loaded=0` with zero averages; a count that went down (file reset) also
/// restarts from zero.
pub fn diff(before: &[LoadType], after: &[LoadType]) -> Vec<Delta> {
    let mut out = Vec::new();
    for a in after {
        let b = before.iter().find(|b| b.name == a.name).filter(|b| b.loaded <= a.loaded);
        let changed = match b {
            None => true,
            Some(b) => b.loaded != a.loaded || b.stages != a.stages,
        };
        if !changed {
            continue;
        }
        out.push(Delta {
            name: a.name.clone(),
            before_n: b.map_or(0, |b| b.loaded),
            after_n: a.loaded,
            stages: a.stages.iter().map(|(s, v)| (s.clone(), b.and_then(|b| b.stage(s)).unwrap_or(0.0), *v)).collect(),
        });
    }
    out
}

fn list(stages: &[(String, f64)]) -> String {
    stages.iter().map(|(s, v)| format!("{}:{v:.3}", crate::loadprof::token(s))).collect::<Vec<_>>().join(",")
}

/// One `LOADSTAGES` log line per changed type. `measured` is the agent's own duration
/// of the load (loading flag), when known.
pub fn line(d: &Delta, measured: Option<f64>) -> String {
    let mut out =
        format!("X4MP_AGENT LOADSTAGES type={} loaded={}->{}", crate::loadprof::token(&d.name), d.before_n, d.after_n);
    let cumulative = d.cumulative();
    match &cumulative {
        Some(stages) => {
            let sum: f64 = stages.iter().map(|s| s.1).sum();
            let negative = stages.iter().any(|s| s.1 < -1e-6);
            out.push_str(&format!(
                " rule=cumulative loads={} sum={sum:.3} negative={} stages={}",
                d.loads().unwrap_or(0),
                u8::from(negative),
                list(stages)
            ));
        }
        None => out.push_str(" rule=unknown(count_not_increased)"),
    }
    if let Some(t) = measured {
        out.push_str(&format!(" measured_load_s={t:.3}"));
        let expected = d.loads().filter(|k| *k == 1).map(|_| 1.0 / d.after_n as f64);
        match d.implied_weight(t) {
            Some(w) => {
                out.push_str(&format!(" weight_implied={w:.4}"));
                if let Some(e) = expected {
                    out.push_str(&format!(" weight_cumulative={e:.4}"));
                }
                let agrees = expected.is_some_and(|e| ((w - e) / e).abs() <= 0.25);
                if !agrees {
                    out.push_str(&format!(" stages_w={}", list(&d.with_weight(w))));
                }
            }
            None => out.push_str(" weight_implied=none"),
        }
    }
    out.push_str(&format!(" averages={}", list(&d.stages.iter().map(|s| (s.0.clone(), s.2)).collect::<Vec<_>>())));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const BEFORE: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<!-- assumed layout -->
<progressbar>
  <type id="newgame" loaded="2">
    <stage id="init" time="2.0"/>
    <stage id="map" time="10.0"/>
  </type>
  <type id="savedgame" loaded="3">
    <stage id="init" time="1.0"/>
    <stage id="map" time="6.0"/>
    <stage id="jobs" time="10.0"/>
  </type>
</progressbar>"#;

    const AFTER: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<progressbar>
  <type id="newgame" loaded="2">
    <stage id="init" time="2.0"/>
    <stage id="map" time="10.0"/>
  </type>
  <type id="savedgame" loaded="4">
    <stage id="init" time="1.25"/>
    <stage id="map" time="6.0"/>
    <stage id="jobs" time="10.5"/>
  </type>
</progressbar>"#;

    #[test]
    fn parses_types_and_stages() {
        let t = parse(BEFORE);
        assert_eq!(t.len(), 2);
        assert_eq!(t[1].name, "savedgame");
        assert_eq!(t[1].loaded, 3);
        assert_eq!(t[1].stages, vec![("init".into(), 1.0), ("map".into(), 6.0), ("jobs".into(), 10.0)]);
        assert_eq!(t[1].sum(), 17.0);
    }

    #[test]
    fn parses_layout_variants() {
        // Stages as attributes of the type element.
        let a = parse(r#"<progressbar><savedgame loaded="5" init="1.5" map="2"/></progressbar>"#);
        assert_eq!(
            a,
            vec![LoadType {
                name: "savedgame".into(),
                loaded: 5,
                stages: vec![("init".into(), 1.5), ("map".into(), 2.0)]
            }]
        );
        // Stages as child tags with text or a single numeric attribute.
        let b = parse("<p><newgame loaded='1'><init>0.5</init><map ms=\"3\"/></newgame></p>");
        assert_eq!(b[0].stages, vec![("init".into(), 0.5), ("map".into(), 3.0)]);
        assert!(parse("").is_empty());
        assert!(parse("<p><x a=\"1\"/></p>").is_empty());
    }

    #[test]
    fn cumulative_delta_recovers_the_single_load() {
        let d = diff(&parse(BEFORE), &parse(AFTER));
        assert_eq!(d.len(), 1, "newgame unchanged: {d:?}");
        let d = &d[0];
        assert_eq!((d.before_n, d.after_n, d.loads()), (3, 4, Some(1)));
        // x = 4*avg' - 3*avg
        assert_eq!(d.cumulative().unwrap(), vec![("init".into(), 2.0), ("map".into(), 6.0), ("jobs".into(), 12.0)]);
        // Measured 20 s = the cumulative sum: implied weight 1/4.
        let w = d.implied_weight(20.0).unwrap();
        assert!((w - 0.25).abs() < 1e-12);
        let l = line(d, Some(20.0));
        assert_eq!(
            l,
            "X4MP_AGENT LOADSTAGES type=savedgame loaded=3->4 rule=cumulative loads=1 sum=20.000 negative=0 stages=init:2.000,map:6.000,jobs:12.000 measured_load_s=20.000 weight_implied=0.2500 weight_cumulative=0.2500 averages=init:1.250,map:6.000,jobs:10.500"
        );
    }

    #[test]
    fn exponential_average_is_detected_by_the_measured_total() {
        // EMA with w = 0.5: avg' = (avg + x)/2. x = (3, 6, 14), sum 23.
        let before = vec![LoadType {
            name: "savedgame".into(),
            loaded: 9,
            stages: vec![("a".into(), 1.0), ("b".into(), 6.0), ("c".into(), 10.0)],
        }];
        let after = vec![LoadType {
            name: "savedgame".into(),
            loaded: 10,
            stages: vec![("a".into(), 2.0), ("b".into(), 6.0), ("c".into(), 12.0)],
        }];
        let d = &diff(&before, &after)[0];
        let w = d.implied_weight(23.0).unwrap();
        assert!((w - 0.5).abs() < 1e-12);
        let l = line(d, Some(23.0));
        assert!(
            l.contains(" weight_implied=0.5000 weight_cumulative=0.1000 stages_w=a:3.000,b:6.000,c:14.000 "),
            "{l}"
        );
        // Cumulative reading of the same data is clearly wrong (negative / far off).
        assert!(l.contains(" rule=cumulative loads=1 sum=47.000 "), "{l}");
    }

    #[test]
    fn first_load_and_unchanged_count() {
        let after = parse(r#"<p><type id="savedgame" loaded="1"><s id="init" time="4"/></type></p>"#);
        let d = &diff(&[], &after)[0];
        assert_eq!(d.cumulative().unwrap(), vec![("init".into(), 4.0)]);
        // Count unchanged but values moved: the rule is not cumulative-with-count.
        let b = vec![LoadType { name: "x".into(), loaded: 10, stages: vec![("s".into(), 1.0)] }];
        let a = vec![LoadType { name: "x".into(), loaded: 10, stages: vec![("s".into(), 2.0)] }];
        let d = &diff(&b, &a)[0];
        assert_eq!(d.cumulative(), None);
        assert!(line(d, None).contains("rule=unknown(count_not_increased)"));
        // Nothing changed: nothing to report.
        assert!(diff(&a, &a).is_empty());
    }
}
