//! Opt-in configuration from the environment. The agent is off unless `X4MP_AGENT=1`.

use std::path::PathBuf;

pub const ENV_ENABLE: &str = "X4MP_AGENT";
pub const ENV_LOG: &str = "X4MP_AGENT_LOG";
pub const ENV_CADENCE_MS: &str = "X4MP_AGENT_CADENCE_MS";
pub const ENV_SETTLE_MS: &str = "X4MP_AGENT_SETTLE_MS";
pub const ENV_DISARM_FILE: &str = "X4MP_AGENT_DISARM_FILE";
/// Load profiler (per-thread CPU, I/O, memory during the load). On unless `0`.
pub const ENV_LOADPROF: &str = "X4MP_LOADPROF";
pub const ENV_LOADPROF_TAIL_S: &str = "X4MP_LOADPROF_TAIL_S";
pub const ENV_LOADPROF_MAX_S: &str = "X4MP_LOADPROF_MAX_S";
pub const ENV_LOADPROF_TOP: &str = "X4MP_LOADPROF_TOP";
/// Load-stage times from progressbar.xml. On unless `0`.
pub const ENV_LOADSTAGES: &str = "X4MP_LOADSTAGES";
pub const ENV_LOADSTAGES_FILE: &str = "X4MP_LOADSTAGES_FILE";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LoadProf {
    /// Keep sampling this long after the engine leaves loading. 0..=600 s, default 10.
    pub tail_s: u64,
    /// Hard stop at this process age. 10..=7200 s, default 900.
    pub max_s: u64,
    /// Threads listed per `LOADPROF` row. 1..=32, default 6.
    pub top: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Cadence/diagnostic log. Default `$HOME/x4mp-agent/agent-<pid>.log`.
    pub log: PathBuf,
    /// Period of `CADENCE` summary lines (wall clock), 100..=60000 ms, default 1000.
    pub cadence_ms: u64,
    /// The game must be out of loading, with game time advancing, for this long before
    /// the frame callback is registered. 500..=600000 ms, default 2000.
    pub settle_ms: u64,
    /// When this file exists the agent disarms permanently. Default `<log>.disarm`.
    pub disarm_file: PathBuf,
    /// `None` when `X4MP_LOADPROF=0`.
    pub loadprof: Option<LoadProf>,
    /// `X4MP_LOADSTAGES` is not `0`.
    pub loadstages: bool,
    /// Explicit progressbar.xml; default `$HOME/.config/EgoSoft/X4/<personalfolderid>/progressbar.xml`.
    pub loadstages_file: Option<PathBuf>,
}

/// A feature switch: absent or `1` is on, `0` is off, anything else is refused.
fn switch(value: Option<String>, name: &str) -> Result<bool, String> {
    match value.as_deref() {
        None | Some("1") => Ok(true),
        Some("0") => Ok(false),
        Some(_) => Err(format!("{name} must be 0 or 1")),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Parsed {
    /// `X4MP_AGENT` is not exactly `1`: do nothing at all.
    Disabled,
    Enabled(Config),
    /// Opted in, but the configuration is unusable: refuse (fail closed).
    Invalid(String),
}

fn bounded(value: Option<String>, name: &str, default: u64, lo: u64, hi: u64) -> Result<u64, String> {
    match value {
        None => Ok(default),
        Some(text) => match text.parse::<u64>() {
            Ok(v) if (lo..=hi).contains(&v) => Ok(v),
            _ => Err(format!("{name} must be an integer from {lo} to {hi}")),
        },
    }
}

fn absolute(text: &str, name: &str) -> Result<PathBuf, String> {
    let path = PathBuf::from(text);
    if text.is_empty() || !path.is_absolute() || text.contains('\0') || text.contains('\n') {
        return Err(format!("{name} must be an absolute path"));
    }
    Ok(path)
}

/// `get` looks up an environment variable (injected for tests).
pub fn parse(get: impl Fn(&str) -> Option<String>, pid: u32) -> Parsed {
    if get(ENV_ENABLE).as_deref() != Some("1") {
        return Parsed::Disabled;
    }
    let result = (|| {
        let log = match get(ENV_LOG) {
            Some(text) => absolute(&text, ENV_LOG)?,
            None => {
                let home = get("HOME").ok_or("neither X4MP_AGENT_LOG nor HOME is set")?;
                absolute(&home, "HOME")?.join("x4mp-agent").join(format!("agent-{pid}.log"))
            }
        };
        let disarm_file = match get(ENV_DISARM_FILE) {
            Some(text) => absolute(&text, ENV_DISARM_FILE)?,
            None => {
                let mut name = log.clone().into_os_string();
                name.push(".disarm");
                PathBuf::from(name)
            }
        };
        Ok(Config {
            log,
            cadence_ms: bounded(get(ENV_CADENCE_MS), ENV_CADENCE_MS, 1000, 100, 60_000)?,
            settle_ms: bounded(get(ENV_SETTLE_MS), ENV_SETTLE_MS, 2000, 500, 600_000)?,
            disarm_file,
            loadprof: if switch(get(ENV_LOADPROF), ENV_LOADPROF)? {
                Some(LoadProf {
                    tail_s: bounded(get(ENV_LOADPROF_TAIL_S), ENV_LOADPROF_TAIL_S, 10, 0, 600)?,
                    max_s: bounded(get(ENV_LOADPROF_MAX_S), ENV_LOADPROF_MAX_S, 900, 10, 7200)?,
                    top: bounded(get(ENV_LOADPROF_TOP), ENV_LOADPROF_TOP, 6, 1, 32)?,
                })
            } else {
                None
            },
            loadstages: switch(get(ENV_LOADSTAGES), ENV_LOADSTAGES)?,
            loadstages_file: get(ENV_LOADSTAGES_FILE).map(|t| absolute(&t, ENV_LOADSTAGES_FILE)).transpose()?,
        })
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
    fn off_unless_exactly_one() {
        for value in [None, Some("0"), Some("true"), Some("yes"), Some(" 1"), Some("")] {
            let pairs: Vec<(&str, &str)> = value.map(|v| vec![(ENV_ENABLE, v)]).unwrap_or_default();
            assert_eq!(parse(env(&pairs), 1), Parsed::Disabled, "{value:?}");
        }
    }

    #[test]
    fn defaults_under_home() {
        let Parsed::Enabled(c) = parse(env(&[(ENV_ENABLE, "1"), ("HOME", "/srv/x4")]), 42) else { panic!() };
        assert_eq!(c.log, PathBuf::from("/srv/x4/x4mp-agent/agent-42.log"));
        assert_eq!(c.disarm_file, PathBuf::from("/srv/x4/x4mp-agent/agent-42.log.disarm"));
        assert_eq!((c.cadence_ms, c.settle_ms), (1000, 2000));
        assert_eq!(c.loadprof, Some(LoadProf { tail_s: 10, max_s: 900, top: 6 }));
        assert!(c.loadstages);
        assert_eq!(c.loadstages_file, None);
    }

    #[test]
    fn load_profiling_switches() {
        let base = [(ENV_ENABLE, "1"), (ENV_LOG, "/w/a.log")];
        let with = |extra: &[(&'static str, &'static str)]| {
            let mut pairs = base.to_vec();
            pairs.extend_from_slice(extra);
            parse(env(&pairs), 1)
        };
        let Parsed::Enabled(c) = with(&[(ENV_LOADPROF, "0"), (ENV_LOADSTAGES, "0")]) else { panic!() };
        assert_eq!((c.loadprof, c.loadstages), (None, false));
        let Parsed::Enabled(c) = with(&[(ENV_LOADPROF_TAIL_S, "0"), (ENV_LOADPROF_MAX_S, "60"), (ENV_LOADPROF_TOP, "12"), (ENV_LOADSTAGES_FILE, "/p/progressbar.xml")]) else {
            panic!()
        };
        assert_eq!(c.loadprof, Some(LoadProf { tail_s: 0, max_s: 60, top: 12 }));
        assert_eq!(c.loadstages_file, Some(PathBuf::from("/p/progressbar.xml")));
        for bad in [
            [(ENV_LOADPROF, "yes")],
            [(ENV_LOADSTAGES, "2")],
            [(ENV_LOADPROF_MAX_S, "5")],
            [(ENV_LOADPROF_TOP, "0")],
            [(ENV_LOADSTAGES_FILE, "progressbar.xml")],
        ] {
            assert!(matches!(with(&bad), Parsed::Invalid(_)), "{bad:?}");
        }
        // The feature flags never enable the agent by themselves.
        assert_eq!(parse(env(&[(ENV_LOADPROF, "1"), (ENV_LOADSTAGES, "1")]), 1), Parsed::Disabled);
    }

    #[test]
    fn explicit_values_and_refusals() {
        let Parsed::Enabled(c) = parse(
            env(&[(ENV_ENABLE, "1"), (ENV_LOG, "/w/engine-agent.log"), (ENV_CADENCE_MS, "250"), (ENV_SETTLE_MS, "500")]),
            1,
        ) else {
            panic!()
        };
        assert_eq!((c.log.to_str().unwrap(), c.cadence_ms, c.settle_ms), ("/w/engine-agent.log", 250, 500));
        for bad in [
            vec![(ENV_ENABLE, "1")],
            vec![(ENV_ENABLE, "1"), (ENV_LOG, "relative.log")],
            vec![(ENV_ENABLE, "1"), (ENV_LOG, "/a"), (ENV_CADENCE_MS, "5")],
            vec![(ENV_ENABLE, "1"), (ENV_LOG, "/a"), (ENV_SETTLE_MS, "x")],
            vec![(ENV_ENABLE, "1"), (ENV_LOG, "/a"), (ENV_DISARM_FILE, "rel")],
        ] {
            assert!(matches!(parse(env(&bad), 1), Parsed::Invalid(_)), "{bad:?}");
        }
    }
}
