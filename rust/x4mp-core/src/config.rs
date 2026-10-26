//! Configuration from the environment. Development rule (ENGINE-INSTRUMENTATION.txt,
//! rule 5): the core and each subsystem are **on by default**, each with its own flag
//! (`=0` turns it off). Build pinning and fail-closed binding are unaffected: a feature
//! that is on still never runs without its verified exports. Invalid values refuse the
//! core (fail closed); they never fall back to defaults silently.

pub const ENV_ENABLE: &str = "X4MP_CORE";
pub const ENV_POSE: &str = "X4MP_CORE_POSE";
pub const ENV_MDQ: &str = "X4MP_CORE_MDQ";
pub const ENV_MD_PROBE_S: &str = "X4MP_CORE_MD_PROBE_S";
pub const ENV_BUDGET_US: &str = "X4MP_CORE_BUDGET_US";
pub const ENV_FRAME_BUDGET_US: &str = "X4MP_CORE_FRAME_BUDGET_US";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CoreConfig {
    /// Read-only player pose probe (`X4MP_CORE_POSE`, default 1).
    pub pose: bool,
    /// MD action queue bridge (`X4MP_CORE_MDQ`, default 1). Off: the bridge entry points
    /// answer "disabled" and nothing is queued.
    pub mdq: bool,
    /// Submit one harmless `debug_text` every N seconds (`X4MP_CORE_MD_PROBE_S`, default
    /// 10, 0 = off). Needs MDQ. Without the Lua bridge its actions time out (visible).
    pub md_probe_s: u64,
    /// Per-subsystem budget (µs), 50..=100000, default 500.
    pub budget_us: u64,
    /// Whole-core frame budget (µs), 100..=100000, default 2000.
    pub frame_budget_us: u64,
}

impl Default for CoreConfig {
    fn default() -> Self {
        CoreConfig { pose: true, mdq: true, md_probe_s: 10, budget_us: 500, frame_budget_us: 2000 }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Parsed {
    Disabled,
    Enabled(CoreConfig),
    Invalid(String),
}

fn flag(get: &impl Fn(&str) -> Option<String>, name: &str) -> Result<bool, String> {
    match get(name).as_deref() {
        Some("0") => Ok(false),
        None | Some("1") => Ok(true),
        Some(_) => Err(format!("{name} must be 0 or 1")),
    }
}

fn bounded(get: &impl Fn(&str) -> Option<String>, name: &str, default: u64, lo: u64, hi: u64) -> Result<u64, String> {
    match get(name) {
        None => Ok(default),
        Some(text) => match text.parse::<u64>() {
            Ok(v) if (lo..=hi).contains(&v) => Ok(v),
            _ => Err(format!("{name} must be an integer from {lo} to {hi}")),
        },
    }
}

/// `get` looks up an environment variable (injected for tests).
pub fn parse(get: impl Fn(&str) -> Option<String>) -> Parsed {
    match get(ENV_ENABLE).as_deref() {
        Some("0") => return Parsed::Disabled,
        None | Some("1") => {}
        Some(_) => return Parsed::Invalid(format!("{ENV_ENABLE} must be 0 or 1")),
    }
    let result = (|| {
        let config = CoreConfig {
            pose: flag(&get, ENV_POSE)?,
            mdq: flag(&get, ENV_MDQ)?,
            md_probe_s: bounded(&get, ENV_MD_PROBE_S, 10, 0, 3600)?,
            budget_us: bounded(&get, ENV_BUDGET_US, 500, 50, 100_000)?,
            frame_budget_us: bounded(&get, ENV_FRAME_BUDGET_US, 2000, 100, 100_000)?,
        };
        let explicit_probe = get(ENV_MD_PROBE_S).is_some_and(|v| v != "0");
        if explicit_probe && !config.mdq {
            return Err(format!("{ENV_MD_PROBE_S} requires {ENV_MDQ}=1"));
        }
        // The default probe simply follows the queue.
        Ok(CoreConfig { md_probe_s: if config.mdq { config.md_probe_s } else { 0 }, ..config })
    })();
    match result {
        Ok(config) => Parsed::Enabled(config),
        Err(reason) => Parsed::Invalid(reason),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        move |k| map.get(k).cloned()
    }

    #[test]
    fn on_by_default_and_off_only_with_zero() {
        assert_eq!(parse(env(&[])), Parsed::Enabled(CoreConfig::default()));
        assert_eq!(parse(env(&[(ENV_ENABLE, "1")])), Parsed::Enabled(CoreConfig::default()));
        assert_eq!(parse(env(&[(ENV_ENABLE, "0")])), Parsed::Disabled);
        for v in ["true", " 1", ""] {
            assert!(matches!(parse(env(&[(ENV_ENABLE, v)])), Parsed::Invalid(_)), "{v:?}");
        }
        let d = CoreConfig::default();
        assert!(d.pose && d.mdq && d.md_probe_s == 10);
        let Parsed::Enabled(c) = parse(env(&[(ENV_POSE, "0"), (ENV_MDQ, "0")])) else { panic!() };
        assert!(!c.pose && !c.mdq && c.md_probe_s == 0, "probe follows the queue");
    }

    #[test]
    fn flags_and_bounds() {
        let Parsed::Enabled(c) = parse(env(&[
            (ENV_ENABLE, "1"),
            (ENV_POSE, "1"),
            (ENV_MDQ, "1"),
            (ENV_MD_PROBE_S, "5"),
            (ENV_BUDGET_US, "250"),
        ])) else {
            panic!()
        };
        assert!(c.pose && c.mdq);
        assert_eq!((c.md_probe_s, c.budget_us, c.frame_budget_us), (5, 250, 2000));
        for bad in [
            vec![(ENV_ENABLE, "1"), (ENV_POSE, "yes")],
            vec![(ENV_ENABLE, "1"), (ENV_BUDGET_US, "10")],
            vec![(ENV_ENABLE, "1"), (ENV_MDQ, "0"), (ENV_MD_PROBE_S, "5")],
            vec![(ENV_ENABLE, "1"), (ENV_FRAME_BUDGET_US, "x")],
        ] {
            assert!(matches!(parse(env(&bad)), Parsed::Invalid(_)), "{bad:?}");
        }
    }
}
